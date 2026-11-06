# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, група PARTIES, v3.83.0): проверка на ДДС номер във
VIES, регистър на превозвачите + „Поръчка за превоз“, запомнен превозвач по
клиент, палетни сметки. Мрежата към VIES е подменена (без мрежа)."""
import io
import json
import socket
import urllib.error

import pytest

from conftest import get_edit_doc_version, issue_cmr, post_with_csrf


# ---------------------------------------------------------------- помощници

def _vies_ok(name="ACME GMBH", address="STR 1\n10115 BERLIN", valid=True):
    def fake(url, payload, timeout):
        fake.calls.append((url, payload, timeout))
        return 200, {"countryCode": payload["countryCode"], "vatNumber": payload["vatNumber"],
                     "requestDate": "2026-11-06T10:00:00.000Z", "valid": valid,
                     "requestIdentifier": "WAPIAAAAX", "name": name, "address": address}
    fake.calls = []
    return fake


@pytest.fixture
def vies_mock(monkeypatch):
    import vies
    fake = _vies_ok()
    monkeypatch.setattr(vies, "_http_post_json", fake)
    return fake


@pytest.fixture
def audit(monkeypatch):
    import applog
    calls = []
    monkeypatch.setattr(applog, "log_audit", lambda action, detail="": calls.append((action, detail)))
    return calls


def _add_client(db_module, name="Клиент ЕООД", **extra):
    con = db_module.get_db()
    cols = ["name"] + list(extra)
    cur = con.execute("INSERT INTO clients (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
                      [name] + list(extra.values()))
    con.commit()
    cid = cur.lastrowid
    con.close()
    return cid


def _add_carrier(client, name="Транс ЕООД", trucks=("CA1234AB",), trailers=("CA5678XX",),
                 drivers=(("Иван Иванов", "0888", "ЛК 1"),), **extra):
    data = {"name": name, "vat": "BG123456789", "city": "София", "country": "България",
            "trucks_json": json.dumps([{"plate": t} for t in trucks]),
            "trailers_json": json.dumps([{"plate": t} for t in trailers]),
            "drivers_json": json.dumps([{"name": n, "phone": p, "id_doc": i} for n, p, i in drivers])}
    data.update(extra)
    resp = post_with_csrf(client, "/carriers/new", data, csrf_source_url="/carriers/new")
    assert resp.status_code == 302, resp.data[:2000]
    return int(resp.headers["Location"].split("hl=")[1])


def _issue(client, **fields):
    data = {"consignee_name": "Клиент ЕООД", "sender_name": "Изпращач"}
    data.update(fields)
    resp = post_with_csrf(client, "/cmr/new", data, csrf_source_url="/cmr/new")
    assert resp.status_code == 302, resp.data[:2000]
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def _ledger(db_module):
    con = db_module.get_db()
    rows = [dict(r) for r in con.execute("SELECT * FROM pallet_ledger ORDER BY id")]
    con.close()
    return rows


# ---------------------------------------------------------------- миграция

def test_migration_creates_tables_and_vies_columns_idempotently(con, db_module):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"carriers", "carrier_vehicles", "carrier_drivers", "pallet_ledger"} <= tables
    for table in ("clients", "invoice_clients", "carriers"):
        cols = {r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)}
        assert {"vies_vat", "vies_valid", "vies_name", "vies_address", "vies_checked_at"} <= cols
    db_module._m016_parties(con)   # повторно — без грешка
    db_module._m016_parties(con)


# ---------------------------------------------------------------- VIES: нормализация и заявка

@pytest.mark.parametrize("raw, hint, expected", [
    ("BG 175 074 752", "", ("BG", "175074752")),
    ("bg-175.074.752", "", ("BG", "175074752")),
    ("GR094014201", "", ("EL", "094014201")),           # Гърция е EL във VIES
    ("EL 094014201", "", ("EL", "094014201")),
    ("175074752", "България", ("BG", "175074752")),      # без префикс — от държавата
    ("123456789", "Germany", ("DE", "123456789")),
    ("IT 0123 4567 890", "", ("IT", "01234567890")),
    ("NL123456789B01", "", ("NL", "123456789B01")),
])
def test_normalize_vat(raw, hint, expected):
    import vies
    assert vies.normalize_vat(raw, hint) == expected


@pytest.mark.parametrize("raw, hint, code", [
    ("", "", "EMPTY"), ("123456789", "", "NO_PREFIX"), ("123456789", "Турция", "NO_PREFIX"),
    ("US123456", "", "COUNTRY"), ("BG", "", "FORMAT"), ("BG12#34", "", "FORMAT"),
])
def test_normalize_vat_rejects(raw, hint, code):
    import vies
    with pytest.raises(vies.ViesError) as exc:
        vies.normalize_vat(raw, hint)
    assert exc.value.kind == "input" and exc.value.code == code


def test_check_vat_parses_valid_answer_and_hides_undisclosed_name(monkeypatch):
    import vies
    fake = _vies_ok(name="---", address="---")
    monkeypatch.setattr(vies, "_http_post_json", fake)
    r = vies.check_vat("de 123 456 789")
    assert fake.calls[0][1] == {"countryCode": "DE", "vatNumber": "123456789"}
    assert fake.calls[0][0] == vies.VIES_CHECK_URL and fake.calls[0][2] == vies.VIES_TIMEOUT
    assert r["valid"] is True and r["vat"] == "DE123456789"
    assert r["name"] == "" and r["address"] == ""
    assert r["request_id"] == "WAPIAAAAX" and len(r["checked_at"]) == 19


@pytest.mark.parametrize("status, body, kind", [
    (500, {"actionSucceed": False, "errorWrappers": [{"error": "MS_UNAVAILABLE"}]}, "unavailable"),
    (200, {"isValid": False, "userError": "TIMEOUT"}, "unavailable"),
    (400, {"actionSucceed": False, "errorWrappers": [{"error": "INVALID_INPUT"}]}, "input"),
    (403, {"errorWrappers": [{"error": "IP_BLOCKED"}]}, "blocked"),
    (503, {}, "unavailable"),
    (200, {"something": 1}, "unknown"),
])
def test_check_vat_errors(monkeypatch, status, body, kind):
    import vies
    monkeypatch.setattr(vies, "_http_post_json", lambda u, p, t: (status, body))
    with pytest.raises(vies.ViesError) as exc:
        vies.check_vat("BG175074752")
    assert exc.value.kind == kind


@pytest.mark.parametrize("error", [socket.timeout("timed out"), urllib.error.URLError("no route"),
                                   ConnectionResetError()])
def test_check_vat_network_errors(monkeypatch, error):
    import vies

    def boom(u, p, t):
        raise error
    monkeypatch.setattr(vies, "_http_post_json", boom)
    with pytest.raises(vies.ViesError) as exc:
        vies.check_vat("BG175074752")
    assert exc.value.kind == "network"


def test_http_post_json_goes_through_net_urlopen_with_timeout(monkeypatch):
    """Истинската функция (без мрежа): net.urlopen с POST JSON и таймаут;
    HTTP грешката връща тялото ѝ (VIES описва причината вътре)."""
    import net
    import vies
    seen = {}

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=8):
        seen.update(url=request.full_url, method=request.get_method(), timeout=timeout,
                    body=json.loads(request.data), ctype=request.headers.get("Content-type"))
        return Resp(b'{"valid": true}')
    monkeypatch.setattr(net, "urlopen", fake_urlopen)
    assert vies._http_post_json(vies.VIES_CHECK_URL, {"countryCode": "BG", "vatNumber": "1"}, 10) == \
        (200, {"valid": True})
    assert seen == {"url": vies.VIES_CHECK_URL, "method": "POST", "timeout": 10,
                    "body": {"countryCode": "BG", "vatNumber": "1"}, "ctype": "application/json"}

    def http_error(request, timeout=8):
        raise urllib.error.HTTPError(request.full_url, 500, "err", {},
                                     io.BytesIO(b'{"errorWrappers":[{"error":"MS_UNAVAILABLE"}]}'))
    monkeypatch.setattr(net, "urlopen", http_error)
    assert vies._http_post_json(vies.VIES_CHECK_URL, {}, 10)[0] == 500


# ---------------------------------------------------------------- VIES: маршрут и кеш

def test_vies_check_route_caches_result_on_client_and_audits(admin_client, db_module, vies_mock, audit):
    cid = _add_client(db_module, vat="DE123456789", country="Germany")
    page = admin_client.get("/clients/%d/edit" % cid).data.decode()
    assert "data-vies" in page and "Провери във VIES" in page
    assert vies_mock.calls == [], "проверката никога не тръгва сама при отваряне на картата"
    resp = post_with_csrf(admin_client, "/vies/check",
                          {"vat": "DE 123456789", "country": "Germany", "target": "client",
                           "target_id": str(cid)}, csrf_source_url="/clients/%d/edit" % cid)
    data = resp.get_json()
    assert data["ok"] and data["saved"] and data["result"]["valid"]
    assert data["result"]["name"] == "ACME GMBH"
    con = db_module.get_db()
    row = con.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
    con.close()
    assert row["vies_vat"] == "DE123456789" and row["vies_valid"] == 1
    assert row["vies_name"] == "ACME GMBH" and row["vies_checked_at"]
    assert any(a == "проверка във VIES" and "DE123456789" in d for a, d in audit)
    page = admin_client.get("/clients/%d/edit" % cid).data.decode()
    assert "Валиден във VIES" in page and "ACME GMBH" in page and "проверен на" in page


def test_vies_check_invalid_and_error_messages(employee_client, db_module, monkeypatch, audit):
    import vies
    cid = _add_client(db_module)
    monkeypatch.setattr(vies, "_http_post_json", _vies_ok(valid=False, name="", address=""))
    data = post_with_csrf(employee_client, "/vies/check",
                          {"vat": "BG999999999", "target": "client", "target_id": str(cid)}).get_json()
    assert data["ok"] and data["result"]["valid"] is False
    monkeypatch.setattr(vies, "_http_post_json",
                        lambda u, p, t: (_ for _ in ()).throw(urllib.error.URLError("x")))
    data = post_with_csrf(employee_client, "/vies/check", {"vat": "BG999999999"}).get_json()
    assert data["ok"] is False and "Няма връзка с VIES" in data["error"]
    data = post_with_csrf(employee_client, "/vies/check", {"vat": "999"}).get_json()
    assert data["ok"] is False and "префикса" in data["error"]
    assert any(a == "проверка във VIES — неуспешна" for a, _d in audit)


def test_vies_check_requires_login_and_csrf(client, flask_app):
    assert client.post("/vies/check", data={"vat": "BG1"}).status_code in (302, 400)


def test_vies_on_invoice_client_and_carrier(admin_client, db_module, vies_mock):
    con = db_module.get_db()
    eid = con.execute("INSERT INTO invoice_clients (name) VALUES ('ABB Brasil')").lastrowid
    con.commit()
    con.close()
    page = admin_client.get("/invoices/clients/%d/edit" % eid).data.decode()
    assert 'id="f-vies-vat"' in page and "Провери във VIES" in page
    data = post_with_csrf(admin_client, "/vies/check", {"vat": "IT01234567890", "target": "invoice_client",
                                                        "target_id": str(eid)}).get_json()
    assert data["saved"]
    page = admin_client.get("/invoices/clients/%d/edit" % eid).data.decode()
    assert 'value="IT01234567890"' in page and "Валиден във VIES" in page
    carrier_id = _add_carrier(admin_client)
    data = post_with_csrf(admin_client, "/vies/check", {"vat": "BG123456789", "target": "carrier",
                                                        "target_id": str(carrier_id)}).get_json()
    assert data["saved"]
    assert "Валиден във VIES" in admin_client.get("/carriers/%d/edit" % carrier_id).data.decode()


# ---------------------------------------------------------------- регистър на превозвачите

def test_carrier_crud_children_search_and_audit(admin_client, db_module, audit):
    cid = _add_carrier(admin_client, name="Транс Експрес", eori="BG123EORI",
                       trucks=("EB1234AB", "EB5678KK"))
    import carriers
    con = db_module.get_db()
    c = carriers.get(con, cid)
    con.close()
    assert [t["plate"] for t in c["trucks"]] == ["EB1234AB", "EB5678KK"]
    assert c["trailers"][0]["plate"] == "CA5678XX" and c["drivers"][0]["phone"] == "0888"
    assert c["eori"] == "BG123EORI"
    assert ("нов превозвач", "id=%d „Транс Експрес“" % cid) in audit
    page = admin_client.get("/carriers?q=eb5678").data.decode()
    assert "Транс Експрес" in page, "търсене по рег. № на камион"
    assert "Транс Експрес" in admin_client.get("/carriers?q=иван").data.decode()
    assert "Транс Експрес" not in admin_client.get("/carriers?q=нищо").data.decode()
    # редакция с правилната версия
    resp = post_with_csrf(admin_client, "/carriers/%d/edit" % cid,
                          {"name": "Транс Експрес 2", "version": "1", "trucks_json": "[]",
                           "trailers_json": "[]", "drivers_json": "[]"})
    assert resp.status_code == 302
    con = db_module.get_db()
    c = carriers.get(con, cid)
    con.close()
    assert c["name"] == "Транс Експрес 2" and c["trucks"] == [] and c["version"] == 2


def test_carrier_edit_conflict_keeps_typed_values(admin_client, db_module):
    cid = _add_carrier(admin_client)
    resp = post_with_csrf(admin_client, "/carriers/%d/edit" % cid,
                          {"name": "Моята версия", "version": "7", "trucks_json": "[]",
                           "trailers_json": "[]", "drivers_json": "[]"})
    page = resp.data.decode()
    assert resp.status_code == 200 and "променен от друг потребител" in page
    assert 'value="Моята версия"' in page and 'name="version" value="1"' in page
    con = db_module.get_db()
    assert con.execute("SELECT name FROM carriers WHERE id = ?", (cid,)).fetchone()[0] == "Транс ЕООД"
    con.close()


def test_carrier_name_required_and_delete_is_admin_only(admin_client, employee_client, db_module, audit):
    resp = post_with_csrf(employee_client, "/carriers/new", {"name": " ", "vat": "X1"},
                          csrf_source_url="/carriers/new")
    assert resp.status_code == 200 and "задължително" in resp.data.decode()
    assert 'value="X1"' in resp.data.decode(), "въведеното се връща"
    cid = _add_carrier(employee_client)                   # служителят добавя
    assert employee_client.get("/carriers").status_code == 200
    assert post_with_csrf(employee_client, "/carriers/%d/delete" % cid, {}).status_code == 403
    assert "Изтрий" not in employee_client.get("/carriers").data.decode()
    assert post_with_csrf(admin_client, "/carriers/%d/delete" % cid, {}).status_code == 302
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM carrier_vehicles").fetchone()[0] == 0, "каскадно"
    con.close()
    assert any(a == "изтрит превозвач" for a, _d in audit)


def test_carrier_quick_add_from_cmr(employee_client, db_module):
    resp = post_with_csrf(employee_client, "/carriers/quick-add",
                          {"name": "Бърз Транс", "vat": "RO123", "truck_reg": "B 77 XYZ",
                           "driver": "Петър"}, csrf_source_url="/cmr/new")
    data = resp.get_json()
    assert data["ok"] and not data["existing"]
    assert data["carrier"]["trucks"][0]["plate"] == "B 77 XYZ"
    assert data["carrier"]["drivers"][0]["name"] == "Петър" and data["carrier"]["trailers"] == []
    again = post_with_csrf(employee_client, "/carriers/quick-add", {"name": "бърз  транс"}).get_json()
    assert again["existing"] and again["carrier"]["id"] == data["carrier"]["id"]
    bad = post_with_csrf(employee_client, "/carriers/quick-add", {"name": ""})
    assert bad.status_code == 400


def test_cmr_form_embeds_carrier_picker_and_pallet_fields(admin_client):
    _add_carrier(admin_client, name="Ала Бала Транс")
    page = admin_client.get("/cmr/new").data.decode()
    assert "data-carrier-picker" in page and "Ала Бала Транс" in page
    assert 'name="carrier_id"' in page and 'name="pallet_eur_given"' in page
    assert 'name="pallet_party"' in page and 'name="to_price_eur"' in page
    assert "parties.js" in page


def test_carriers_lookup_json(admin_client):
    _add_carrier(admin_client, name="Северен Транс")
    _add_carrier(admin_client, name="Южен Транс", trucks=("PB0001AA",))
    data = admin_client.get("/carriers/lookup?q=pb0001").get_json()
    assert [c["name"] for c in data["carriers"]] == ["Южен Транс"]


# ---------------------------------------------------------------- запомнен превозвач по клиент

def test_last_for_client_by_id_and_name(admin_client, db_module):
    import carriers
    cid = _add_client(db_module, name="Müller GmbH")
    carrier_id = _add_carrier(admin_client)
    _issue(admin_client, consignee_name="Müller GmbH", carrier="Стар превозвач", truck_reg="OLD1")
    _issue(admin_client, consignee_name="Друг клиент", carrier="Чужд", truck_reg="X")
    _issue(admin_client, consignee_name="müller gmbh", carrier="Транс ЕООД, София",
           carrier_id=str(carrier_id), truck_reg="CA1234AB", trailer_reg="CA5678XX",
           driver="Иван Иванов", date_loading="2026-11-05")
    _issue(admin_client, consignee_name="Müller GmbH")      # без превозвач — прескача се
    con = db_module.get_db()
    by_id = carriers.last_for_client(con, cid)
    by_name = carriers.last_for_client(con, "MÜLLER GMBH")
    assert carriers.last_for_client(con, "Няма такъв") is None
    assert carriers.last_for_client(con, 999999) is None
    con.close()
    assert by_id == by_name
    assert by_id["truck_reg"] == "CA1234AB" and by_id["driver"] == "Иван Иванов"
    assert by_id["carrier_id"] == carrier_id and by_id["carrier_record"]["name"] == "Транс ЕООД"
    assert by_id["doc_date"] == "2026-11-05"
    data = admin_client.get("/carriers/last-for-client?client_id=%d" % cid).get_json()
    assert data["found"] and data["last"]["trailer_reg"] == "CA5678XX"
    assert admin_client.get("/carriers/last-for-client?name=").get_json()["found"] is False


def test_last_for_client_without_register_record(admin_client, db_module):
    import carriers
    _issue(admin_client, consignee_name="Клиент Х", carrier="Ръчен превозвач", driver="Г. Георгиев")
    con = db_module.get_db()
    last = carriers.last_for_client(con, "Клиент Х")
    con.close()
    assert last["carrier"] == "Ръчен превозвач" and last["carrier_id"] is None
    assert last["carrier_record"] is None


# ---------------------------------------------------------------- поръчка за превоз

def test_transport_order_print_and_pdf(admin_client, db_module, audit):
    carrier_id = _add_carrier(admin_client, name="Транс Поръчка", eori="BGEORI1", phone="+359 1")
    doc_id = _issue(admin_client, consignee_name="Получател АД", carrier="Транс Поръчка",
                    carrier_id=str(carrier_id), truck_reg="CA1234AB", driver="Иван Иванов",
                    place_loading="Габрово", date_loading="2026-11-10", place_delivery="Milano",
                    to_date_delivery="2026-11-13", goods="Табла", packages="12", weight="5400",
                    to_price_eur="1250", pallet_eur_given="12", pallet_eur_exchanged="10")
    view = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert "/doc/%d/transport-order" % doc_id in view, "бутон в изгледа на ЧМР"
    page = admin_client.get("/doc/%d/transport-order" % doc_id).data.decode()
    for text in ("ПОРЪЧКА ЗА ПРЕВОЗ", "TRANSPORT ORDER", "Транс Поръчка", "BGEORI1", "CA1234AB",
                 "тел. / tel. 0888", "ЛК 1", "Габрово", "10.11.2026", "13.11.2026", "Milano",
                 "Табла", "5400", "1250", "EUR: 12", "exchange 10", "Конвенцията CMR",
                 "30 дни след получаване"):
        assert text in page, text
    blank = page.split('class="print-page')[1].split('class="to-sign"')[0].lower()
    assert "qr" not in blank and "<svg" not in blank, "без QR код и без баркод"
    resp = admin_client.get("/doc/%d/transport-order.pdf" % doc_id)
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert resp.data[:4] == b"%PDF"
    from pypdf import PdfReader
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(resp.data)).pages)
    assert "Транс Поръчка" in text and "Transport order" in text and "1250" in text
    assert any(a == "поръчка за превоз (PDF)" for a, _d in audit)


def test_transport_order_only_for_cmr(admin_client):
    resp = post_with_csrf(admin_client, "/waybill/new", {"consignee_name": "П"},
                          csrf_source_url="/waybill/new")
    wid = int(resp.headers["Location"].rsplit("/", 1)[-1])
    assert admin_client.get("/doc/%d/transport-order" % wid).status_code == 404
    assert admin_client.get("/doc/%d/transport-order.pdf" % wid).status_code == 404
    assert admin_client.get("/doc/999999/transport-order").status_code == 404


def test_transport_order_settings_admin_only(admin_client, employee_client, db_module):
    assert employee_client.get("/carriers/settings").status_code == 403
    assert 'href="/carriers/settings"' in admin_client.get("/settings").data.decode()
    assert post_with_csrf(employee_client, "/carriers/settings",
                          {"transport_order_terms": "X"}).status_code == 403
    resp = post_with_csrf(admin_client, "/carriers/settings",
                          {"transport_order_terms": "Моите условия / My terms",
                           "transport_order_payment_terms": "7 дни"})
    assert resp.status_code == 302
    doc_id = _issue(admin_client, carrier="Някой")
    page = admin_client.get("/doc/%d/transport-order" % doc_id).data.decode()
    assert "Моите условия / My terms" in page and "7 дни" in page
    assert "Конвенцията CMR" not in page
    doc2 = _issue(admin_client, carrier="Някой", to_payment_terms="Авансово")
    assert "Авансово" in admin_client.get("/doc/%d/transport-order" % doc2).data.decode()


# ---------------------------------------------------------------- палетни сметки

def test_cmr_pallets_go_to_ledger_and_follow_edit_and_delete(admin_client, db_module):
    cid = _add_client(db_module, name="Палет Клиент")
    doc_id = _issue(admin_client, consignee_name="Палет Клиент", pallet_eur_given="33",
                    pallet_eur_exchanged="30", pallet_epal_returned="2", date_loading="2026-11-01")
    rows = _ledger(db_module)
    assert [(r["pallet_type"], r["given"], r["returned"], r["exchanged"]) for r in rows] == \
        [("EUR", 33, 0, 30), ("EPAL", 0, 2, 0)]
    assert {r["party_key"] for r in rows} == {"c%d" % cid} and rows[0]["client_id"] == cid
    assert rows[0]["entry_date"] == "2026-11-01" and rows[0]["source"] == "cmr"
    import pallet_ledger
    con = db_module.get_db()
    bal = pallet_ledger.party_balance(con, "client", ["c%d" % cid])
    con.close()
    assert (bal["bal_eur"], bal["bal_epal"], bal["bal_total"]) == (3, -2, 1)
    # редакция — презаписва, не дублира
    edit_url = "/doc/%d/edit" % doc_id
    resp = post_with_csrf(admin_client, edit_url, {
        "consignee_name": "Палет Клиент", "pallet_eur_given": "10", "pallet_eur_exchanged": "",
        "pallet_epal_returned": "", "edit_doc_version": get_edit_doc_version(admin_client, edit_url)})
    assert resp.status_code == 302
    rows = _ledger(db_module)
    assert [(r["pallet_type"], r["given"], r["exchanged"]) for r in rows] == [("EUR", 10, 0)]
    # изтриване на ЧМР — движенията изчезват
    assert post_with_csrf(admin_client, "/doc/%d/delete" % doc_id, {}).status_code == 302
    assert _ledger(db_module) == []


def test_cmr_pallets_to_carrier_account_and_invalid_numbers(admin_client, db_module):
    carrier_id = _add_carrier(admin_client, name="Палет Транс")
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "consignee_name": "К", "carrier": "Палет Транс, София", "carrier_id": str(carrier_id),
        "pallet_party": "carrier", "pallet_eur_given": "5", "pallet_epal_given": "2,5",
        "pallet_other_given": "-1"}, csrf_source_url="/cmr/new", follow_redirects=True)
    assert "невалидно количество в 2 поле" in resp.data.decode()
    rows = _ledger(db_module)
    assert [(r["party_kind"], r["party_key"], r["pallet_type"], r["given"]) for r in rows] == \
        [("carrier", "r%d" % carrier_id, "EUR", 5)]
    # превозвач без запис в регистъра — по име (първата част на кутия 16)
    _issue(admin_client, carrier="Непознат ООД, Русе", pallet_party="carrier", pallet_eur_given="1")
    assert _ledger(db_module)[-1]["party_key"] == "n:непознат оод"
    page = admin_client.get("/carriers/%d/edit" % carrier_id).data.decode()
    assert "Палетна сметка" in page and "Движения по сметката" in page


def test_cmr_without_pallets_writes_nothing(admin_client, db_module):
    issue_cmr(admin_client)
    assert _ledger(db_module) == []


def test_copy_cmr_does_not_copy_pallets(admin_client, db_module):
    doc_id = _issue(admin_client, pallet_eur_given="4", carrier="X", truck_reg="T1")
    resp = admin_client.get("/doc/%d/copy" % doc_id)
    token = resp.headers["Location"].split("restore=")[1]
    page = admin_client.get("/cmr/new?restore=" + token).data.decode()
    import html
    import re
    restored = json.loads(html.unescape(re.search(r"data-edit='([^']*)'", page).group(1)))
    assert restored["truck_reg"] == "T1" and restored["carrier"] == "X"
    assert not any(k.startswith("pallet_") for k in restored), "палетите са на конкретната пратка"


def test_pallet_pages_balances_party_manual_entries(admin_client, employee_client, db_module, audit):
    cid = _add_client(db_module, name="Баланс Клиент")
    _issue(admin_client, consignee_name="Баланс Клиент", pallet_eur_given="20")
    page = employee_client.get("/pallets").data.decode()
    assert "Баланс Клиент" in page and ">20<" in page
    assert "Ръчна корекция" not in page, "корекцията е само за администратор"
    party = "/pallets/party?kind=client&key=c%d" % cid
    assert employee_client.get(party).status_code == 200
    # служителят не може да коригира
    assert post_with_csrf(employee_client, "/pallets/entry", {
        "party_kind": "client", "party_key": "c%d" % cid, "pallet_type": "EUR",
        "returned": "5"}).status_code == 403
    resp = post_with_csrf(admin_client, "/pallets/entry", {
        "party_kind": "client", "party_key": "c%d" % cid, "pallet_type": "EUR", "returned": "5",
        "entry_date": "2026-11-06", "note": "върнати с камион"}, csrf_source_url=party)
    assert resp.status_code == 302
    import pallet_ledger
    con = db_module.get_db()
    assert pallet_ledger.party_balance(con, "client", ["c%d" % cid])["bal_eur"] == 15
    con.close()
    assert any(a == "палетна сметка — ръчна корекция" for a, _d in audit)
    page = admin_client.get(party).data.decode()
    assert "върнати с камион" in page and "Ръчна корекция" in page
    # невалидни корекции
    for bad in ({"returned": "abc"}, {}, {"returned": "1", "pallet_type": "XXX"}):
        data = {"party_kind": "client", "party_key": "c%d" % cid, "pallet_type": "EUR"}
        data.update(bad)
        post_with_csrf(admin_client, "/pallets/entry", data)
    assert len(_ledger(db_module)) == 2
    # ръчна корекция по име (нов партньор)
    post_with_csrf(admin_client, "/pallets/entry", {"party_kind": "carrier", "party_name": "Нов Транс",
                                                   "pallet_type": "EPAL", "given": "3"})
    assert _ledger(db_module)[-1]["party_key"] == "n:нов транс"
    # изтриване: ръчната — да; от ЧМР — не
    manual = [r for r in _ledger(db_module) if r["source"] == "manual"][0]
    from_cmr = [r for r in _ledger(db_module) if r["source"] == "cmr"][0]
    assert post_with_csrf(employee_client, "/pallets/entry/%d/delete" % manual["id"], {}).status_code == 403
    post_with_csrf(admin_client, "/pallets/entry/%d/delete" % from_cmr["id"], {})
    post_with_csrf(admin_client, "/pallets/entry/%d/delete" % manual["id"], {})
    ids = {r["id"] for r in _ledger(db_module)}
    assert from_cmr["id"] in ids and manual["id"] not in ids
    # филтри
    assert "Баланс Клиент" not in admin_client.get("/pallets?kind=carrier").data.decode()
    assert "Нов Транс" in admin_client.get("/pallets?kind=carrier").data.decode()
    assert "Баланс Клиент" in admin_client.get("/pallets?q=баланс").data.decode()


def test_pallet_balance_shown_on_client_card(admin_client, db_module):
    cid = _add_client(db_module, name="Карта Клиент")
    assert "Още няма палетни движения" in admin_client.get("/clients/%d/edit" % cid).data.decode()
    _issue(admin_client, consignee_name="Карта Клиент", pallet_eur_given="7", pallet_epal_given="1")
    page = admin_client.get("/clients/%d/edit" % cid).data.decode()
    assert "Движения по сметката" in page and "<b>7</b>" in page and "<b>8</b>" in page


def test_pallet_excel_export(admin_client, db_module, audit):
    from openpyxl import load_workbook
    cid = _add_client(db_module, name="Ексел Клиент")
    _issue(admin_client, consignee_name="Ексел Клиент", pallet_eur_given="9", pallet_eur_returned="2")
    resp = admin_client.get("/pallets/export.xlsx")
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.data))
    bal, moves = wb.worksheets
    assert bal.cell(row=2, column=2).value == "Ексел Клиент" and bal.cell(row=2, column=3).value == 7
    assert moves.cell(row=2, column=5).value == 9 and moves.cell(row=2, column=8).value == 7
    one = load_workbook(io.BytesIO(admin_client.get(
        "/pallets/export.xlsx?kind=client&key=c%d" % cid).data))
    assert one.worksheets[0].max_row == 2
    assert any(a == "износ на палетните сметки в Excel" for a, _d in audit)


def test_pallet_party_unknown_is_404(admin_client):
    assert admin_client.get("/pallets/party?kind=client&key=c999").status_code == 404
    assert admin_client.get("/pallets/party?kind=x&key=c1").status_code == 404


def test_nav_has_carriers_and_pallets(employee_client):
    page = employee_client.get("/").data.decode()
    assert 'href="/carriers"' in page and 'href="/pallets"' in page
