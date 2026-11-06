# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, v3.83.0, SHIP): статус на пратката (чернова →
натоварена → доставена → POD получен → фактурирана), POD като прикачен
файл, разход/приход/марж, CO2e по данни на превозвача, шаблон по клиент и
„Повтори пратка“ (shipment_status.py, db._m015_shipment_status)."""
import io
import json
import re
import sqlite3

import pytest

from conftest import post_with_csrf
from test_shipment_print import PO, _add, weg  # noqa: F401 -- weg е fixture

WEG = "WEG Equipamentos Eletricos S.A."
PDF = b"%PDF-1.4\n% test pod\n"


def _step(c, anchor, step, action="set", date="2026-11-05", **extra):
    data = dict({"step": step, "action": action, "date": date}, **extra)
    return post_with_csrf(c, "/shipment/%d/step" % anchor, data,
                          csrf_source_url="/doc/%d" % anchor, follow_redirects=False)


def _st(con, anchor):
    row = con.execute("SELECT * FROM shipment_status WHERE document_id = ?", (anchor,)).fetchone()
    return dict(row) if row else None


def _fresh(db_module):
    return db_module.get_db()


# ---------------------------------------------------------------- миграция и помощни

def test_migration_creates_tables_and_pod_kind(db_module):
    con = db_module.get_db()
    cols = {r["name"] for r in con.execute("PRAGMA table_info(shipment_status)")}
    assert {"document_id", "status", "loaded_at", "loaded_by", "delivered_at", "pod_at",
            "invoiced_at", "invoiced_auto", "transport_cost_cents", "other_cost_cents",
            "co2e_kg", "revenue_cents", "invoice_ids"} <= cols
    assert {r["name"] for r in con.execute("PRAGMA table_info(shipment_members)")} == {
        "member_id", "anchor_id"}
    assert "kind" in {r["name"] for r in con.execute("PRAGMA table_info(document_attachments)")}
    indexes = {r["name"] for r in con.execute("PRAGMA index_list(shipment_status)")}
    assert "idx_shipment_status_status" in indexes
    # Идемпотентна (споделена база, повторен старт)
    db_module._m015_shipment_status(con)
    con.close()


def test_helpers_parse_and_derive():
    import shipment_status as ss
    assert ss.parse_amount_cents("1 200,50") == 120050
    assert ss.parse_amount_cents("1.200,5 €") == 120050
    assert ss.parse_amount_cents("99.99") == 9999
    assert ss.parse_amount_cents("") is None
    for bad in ("-5", "abc", "1e400", "200000000"):
        with pytest.raises(ValueError):
            ss.parse_amount_cents(bad)
    assert ss.parse_co2e("245,5 кг") == 245.5
    with pytest.raises(ValueError):
        ss.parse_co2e("-1")
    assert ss.derive_status({}) == "draft"
    assert ss.derive_status({"loaded_at": "2026-11-01", "pod_at": "2026-11-03"}) == "pod"
    assert ss.fmt_cents(-1250) == "−12.50 €"


# ---------------------------------------------------------------- лентата и етапите

def test_status_bar_on_cmr_and_employee_marks_loaded(employee_client, db_module, weg):
    html = employee_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert 'class="ship-bar no-print"' in html
    for label in ("Чернова", "Натоварена", "Доставена", "POD получен", "Фактурирана"):
        assert label in html
    assert "Повтори пратка" in html
    # Служителят не вижда разхода/маржа (по подразбиране)
    assert "Марж" not in html and 'name="transport_cost"' not in html
    resp = _step(employee_client, weg["cmr"], "loaded", date="2026-11-04")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/doc/%d" % weg["cmr"])
    con = _fresh(db_module)
    st = _st(con, weg["cmr"])
    assert st["status"] == "loaded" and st["loaded_at"] == "2026-11-04" and st["loaded_by"]
    # Членовете на пратката са записани (за значката в /docs)
    members = {r[0] for r in con.execute("SELECT member_id FROM shipment_members WHERE anchor_id = ?",
                                        (weg["cmr"],))}
    assert {weg["cmr"], weg["packing"], weg["invoice"], *weg["pallets"]} <= members
    assert weg["other_cmr"] not in members
    con.close()
    html = employee_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert "04.11" in html and 'aria-current="step"' in html


def test_invalid_step_or_date_is_refused(admin_client, db_module, weg):
    assert _step(admin_client, weg["cmr"], "bogus").status_code == 400
    resp = _step(admin_client, weg["cmr"], "loaded", date="2026-13-45")
    assert resp.status_code == 302
    con = _fresh(db_module)
    assert _st(con, weg["cmr"]) is None
    # Пратката се води само към ЧМР/опаковъчен лист
    assert post_with_csrf(admin_client, "/shipment/%d/step" % weg["invoice"],
                          {"step": "loaded"}).status_code == 404
    con.close()


def test_delivered_auto_invoiced_with_revenue(admin_client, db_module, weg):
    """Има свързана фактура → при „Доставена“ пратката става „Фактурирана“
    (авто), а приходът е сумата на фактурата (appcore.invoice_totals)."""
    import appcore
    resp = _step(admin_client, weg["cmr"], "delivered", date="2026-11-06")
    assert resp.status_code == 302
    con = _fresh(db_module)
    st = _st(con, weg["cmr"])
    assert st["delivered_at"] == "2026-11-06"
    assert st["status"] == "invoiced" and st["invoiced_auto"] == 1 and st["invoiced_by"] is None
    inv = json.loads(con.execute("SELECT data FROM documents WHERE id = ?", (weg["invoice"],)).fetchone()[0])
    assert st["revenue_cents"] == int(appcore.invoice_totals(inv["items"])["price"].replace(".", ""))
    assert st["invoice_ids"] == str(weg["invoice"])
    # Ръчно отменено „фактурирана“ не се връща само при преглед
    _step(admin_client, weg["cmr"], "invoiced", action="clear")
    admin_client.get("/doc/%d" % weg["cmr"])
    st = _st(con, weg["cmr"])
    assert st["invoiced_at"] is None and st["status"] == "delivered" and st["invoiced_auto"] == -1
    con.close()


def test_delivered_without_invoice_and_filters_and_dashboard(admin_client, db_module, con):
    cmr = _add(con, "cmr", "0100/2026", {"consignee_name": "Solo Trade OOD"})
    other = _add(con, "cmr", "0101/2026", {"consignee_name": "Other OOD"})
    _step(admin_client, cmr, "delivered")
    c2 = _fresh(db_module)
    assert _st(c2, cmr)["status"] == "delivered"
    c2.close()
    html = admin_client.get("/docs?ship=uninvoiced").get_data(as_text=True)
    assert "0100/2026" in html and "0101/2026" not in html
    assert "ship-badge--delivered" in html and "Пратка:" in html  # етикет на филтъра
    assert "0100/2026" in admin_client.get("/docs?ship=wait_pod").get_data(as_text=True)
    assert "0100/2026" in admin_client.get("/docs?ship=delivered").get_data(as_text=True)
    draft = admin_client.get("/docs?ship=draft").get_data(as_text=True)
    assert "0101/2026" in draft and "0100/2026" not in draft
    # Непознат филтър се пренебрегва
    assert "0101/2026" in admin_client.get("/docs?ship=xyz").get_data(as_text=True)
    dash = admin_client.get("/").get_data(as_text=True)
    m = re.search(r'ship=wait_pod">\s*<div class="num">(\d+)</div>', dash)
    assert m and m.group(1) == "1"
    m = re.search(r'ship=uninvoiced">\s*<div class="num">(\d+)</div>', dash)
    assert m and m.group(1) == "1"
    # Excel износът на списъка следва филтъра
    resp = admin_client.get("/docs/export.xlsx?ship=uninvoiced")
    assert resp.status_code == 200
    assert other  # другото ЧМР е само шум


def test_pod_upload_marks_attachment_and_list_icon(employee_client, db_module, con):
    cmr = _add(con, "cmr", "0200/2026", {"consignee_name": "POD Client OOD"})
    data = {"step": "pod", "action": "set", "date": "2026-11-06",
            "pod_file": (io.BytesIO(PDF), "signed-cmr.pdf")}
    data["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', employee_client.get(
        "/doc/%d" % cmr).get_data(as_text=True)).group(1)
    resp = employee_client.post("/shipment/%d/step" % cmr, data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    c2 = _fresh(db_module)
    att = c2.execute("SELECT * FROM document_attachments WHERE document_id = ?", (cmr,)).fetchone()
    assert att["kind"] == "pod" and att["filename"] == "signed-cmr.pdf"
    assert _st(c2, cmr)["status"] == "pod"
    c2.close()
    html = employee_client.get("/docs").get_data(as_text=True)
    assert "ship-pod-icon" in html and "ship-badge--pod" in html
    view = employee_client.get("/doc/%d" % cmr).get_data(as_text=True)
    assert "ship-pod-link" in view and "ship-pod-badge" in view  # и в картата „Прикачени“


def test_pod_without_file_warns_and_existing_attachment_can_be_marked(admin_client, db_module, con):
    cmr = _add(con, "cmr", "0201/2026", {"consignee_name": "POD Client OOD"})
    resp = _step(admin_client, cmr, "pod")
    assert resp.status_code == 302
    page = admin_client.get("/doc/%d" % cmr).get_data(as_text=True)
    assert "без прикачен файл" in page
    # Вече прикачен файл → „отбележи като POD“
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    admin_client.post("/doc/%d/attachments" % cmr, data={
        "csrf_token": token, "attachment": (io.BytesIO(PDF), "scan.pdf")},
        content_type="multipart/form-data")
    c2 = _fresh(db_module)
    att_id = c2.execute("SELECT id FROM document_attachments WHERE document_id = ?", (cmr,)).fetchone()[0]
    resp = post_with_csrf(admin_client, "/shipment/%d/pod/%d" % (cmr, att_id), {},
                          csrf_source_url="/doc/%d" % cmr)
    assert resp.status_code == 302
    assert c2.execute("SELECT kind FROM document_attachments WHERE id = ?", (att_id,)).fetchone()[0] == "pod"
    post_with_csrf(admin_client, "/shipment/%d/pod/%d" % (cmr, att_id), {"action": "unmark"},
                   csrf_source_url="/doc/%d" % cmr)
    assert c2.execute("SELECT kind FROM document_attachments WHERE id = ?", (att_id,)).fetchone()[0] == ""
    # Обикновен прикачен файл не е POD — без иконата в списъка
    assert "ship-pod-icon" not in admin_client.get("/docs").get_data(as_text=True)
    # Чуждо прикачено (друг документ) — 404
    other = _add(con, "cmr", "0202/2026", {"consignee_name": "X"})
    assert post_with_csrf(admin_client, "/shipment/%d/pod/%d" % (other, att_id), {},
                          csrf_source_url="/doc/%d" % cmr).status_code == 404
    c2.close()


# ---------------------------------------------------------------- разход, марж, права

def test_costs_and_margin_admin_only_by_default(admin_client, employee_client, db_module, weg):
    resp = post_with_csrf(admin_client, "/shipment/%d/details" % weg["cmr"], {
        "transport_cost": "1,50", "other_cost": "0,25", "cost_note": "Trans Express",
        "co2e_kg": "245,5"}, csrf_source_url="/doc/%d" % weg["cmr"])
    assert resp.status_code == 302
    con = _fresh(db_module)
    st = _st(con, weg["cmr"])
    assert (st["transport_cost_cents"], st["other_cost_cents"], st["co2e_kg"]) == (150, 25, 245.5)
    html = admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    # приход 4,00 € (4 × 1) − разход 1,75 € = 2,25 €
    assert "Марж" in html and "2.25" in html and "245.5" in html and "по данни на превозвача" in html
    emp = employee_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert "Марж" not in emp and "245.5" in emp
    # Служителят не може да смени разхода (полето се пренебрегва), CO2 — може
    post_with_csrf(employee_client, "/shipment/%d/details" % weg["cmr"], {
        "transport_cost": "999", "co2e_kg": "100"}, csrf_source_url="/doc/%d" % weg["cmr"])
    st = _st(con, weg["cmr"])
    assert st["transport_cost_cents"] == 150 and st["co2e_kg"] == 100.0
    # Невалидна сума — нищо не се записва
    post_with_csrf(admin_client, "/shipment/%d/details" % weg["cmr"], {
        "transport_cost": "-3", "co2e_kg": "1"}, csrf_source_url="/doc/%d" % weg["cmr"])
    assert _st(con, weg["cmr"])["co2e_kg"] == 100.0
    # Настройката (само администратор) — служителят вижда маржа
    assert post_with_csrf(employee_client, "/settings/shipment", {"money_for_employees": "1"}
                          ).status_code in (302, 403)
    assert "Марж" not in employee_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    settings = admin_client.get("/settings").get_data(as_text=True)
    assert 'id="shipment-settings"' in settings
    post_with_csrf(admin_client, "/settings/shipment", {"money_for_employees": "1"})
    assert db_module.get_settings(con)["shipment_money_for_employees"] == "1"
    assert "Марж" in employee_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    con.close()


def test_reports_columns_cost_revenue_margin_co2(admin_client, employee_client, db_module, weg):
    month = _fresh(db_module).execute("SELECT substr(created_at, 1, 7) FROM documents WHERE id = ?",
                                      (weg["cmr"],)).fetchone()[0]
    url = "/reports?from=%s&to=%s&group=client" % (month, month)
    before = admin_client.get(url).get_data(as_text=True)
    assert "Марж, EUR" not in before and "CO2e, кг" not in before  # без данни — както досега
    post_with_csrf(admin_client, "/shipment/%d/details" % weg["cmr"], {
        "transport_cost": "10", "co2e_kg": "245,5"}, csrf_source_url="/doc/%d" % weg["cmr"])
    html = admin_client.get(url).get_data(as_text=True)
    for text in ("Разход, EUR", "Приход, EUR", "Марж, EUR", "CO2e, кг", "10.00", "4.00", "−6.00", "245.5"):
        assert text in html, text
    emp = employee_client.get(url).get_data(as_text=True)
    assert "Марж, EUR" not in emp and "CO2e, кг" in emp
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(admin_client.get(url.replace("/reports", "/reports/export.xlsx")).data))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header[-5:] == ["Разход, EUR", "Приход, EUR", "Марж, EUR", "CO2e, кг", "Забележка"]
    row = [c.value for c in ws[2]]
    assert row[header.index("Марж, EUR")] == -6.0 and row[header.index("CO2e, кг")] == 245.5
    wb = load_workbook(io.BytesIO(employee_client.get(url.replace("/reports", "/reports/export.xlsx")).data))
    assert "Марж, EUR" not in [c.value for c in wb.active[1]]


# ---------------------------------------------------------------- котва без ЧМР, палетни карти

def test_packing_without_cmr_is_anchor_then_cmr_adopts(admin_client, db_module, con):
    pk = _add(con, "packing", "0300/2026", {"receiver_name": "Adopt OOD", "order_no": "PO-ADOPT-1"})
    html = admin_client.get("/doc/%d" % pk).get_data(as_text=True)
    assert "/shipment/%d/step" % pk in html
    _step(admin_client, pk, "loaded")
    c2 = _fresh(db_module)
    assert _st(c2, pk)["status"] == "loaded"
    cmr = _add(con, "cmr", "0300/2026", {"consignee_name": "Adopt OOD",
                                        "attached_docs": "Packing list 0300/2026"})
    # Прегледът на листа вече сочи ЧМР-то (само за четене), редът минава към ЧМР-то
    html = admin_client.get("/doc/%d" % pk).get_data(as_text=True)
    assert '<a href="/doc/%d">ЧМР товарителница № 0300/2026</a>' % cmr in html
    assert "/shipment/%d/step" % pk not in html
    assert _st(c2, cmr)["status"] == "loaded" and _st(c2, pk) is None
    assert "/shipment/%d/step" % cmr in admin_client.get("/doc/%d" % cmr).get_data(as_text=True)
    c2.close()


def test_pallet_card_shows_status_read_only(admin_client, db_module, weg):
    _step(admin_client, weg["cmr"], "loaded")
    html = admin_client.get("/doc/%d" % weg["pallets"][0]).get_data(as_text=True)
    assert 'class="ship-bar no-print"' in html and "ЧМР товарителница № 0765/2026" in html
    assert "/shipment/" not in html.split('class="ship-bar no-print"')[1].split("</section>")[0] \
        .replace("/shipment/repeat", "")
    docs = admin_client.get("/docs").get_data(as_text=True)
    assert "is-member" in docs  # значката и на картите/листа на пратката


def test_preview_has_no_status_bar(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/preview", {"consignee_name": "Prev OOD"},
                          csrf_source_url="/cmr/new", follow_redirects=True)
    assert resp.status_code == 200
    assert 'class="ship-bar no-print"' not in resp.get_data(as_text=True)


# ---------------------------------------------------------------- шаблон по клиент

def test_client_template_offers_last_cmr_fields(admin_client, con):
    _add(con, "cmr", "0400/2026", {"consignee_name": "Template Client GmbH", "carrier": "Old Carrier"})
    _add(con, "cmr", "0401/2026", {
        "consignee_name": "Template Client GmbH", "carrier": "Trans Express EOOD", "driver": "Ivan",
        "truck_reg": "CB1234AB", "trailer_reg": "CB5678EE", "place_loading": "Yavorets",
        "place_delivery": "Munich", "goods": "HV Switchgear", "attached_docs": "Invoice 1234",
        "reservations": "none", "weight": "1000"})
    data = admin_client.get("/shipment/client-template?type=cmr&client=template client gmbh").get_json()
    assert data["found"] is True and "0401/2026" in data["source"]
    assert data["fields"]["place_delivery"] == "Munich" and data["fields"]["goods"] == "HV Switchgear"
    # Данните на конкретната пратка не се предлагат; превозвачът/шофьорът —
    # от група PARTIES (carriers.last_for_client), не оттук.
    for key in ("attached_docs", "reservations", "weight", "number",
                "carrier", "driver", "truck_reg", "trailer_reg"):
        assert key not in data["fields"]
    assert data["texts"]["badge"] == "авто"
    assert admin_client.get("/shipment/client-template?type=cmr&client=Nobody").get_json() == {"found": False}
    assert admin_client.get("/shipment/client-template?type=invoice_br&client=x").get_json() == {"found": False}


def test_client_template_packing_incoterms(employee_client, con):
    _add(con, "packing", "0410/2026", {"receiver_name": "Inco SA", "terms_delivery": "FCA Yavorets",
                                       "transport_type": "Truck", "invoice_no": "777", "order_no": "PO1"})
    data = employee_client.get("/shipment/client-template?type=packing&client=Inco SA").get_json()
    assert data["fields"] == {"terms_delivery": "FCA Yavorets", "transport_type": "Truck"}


def test_client_template_requires_login(client):
    assert client.get("/shipment/client-template?type=cmr&client=x").status_code == 302


# ---------------------------------------------------------------- „Повтори пратка“

def test_repeat_shipment_opens_prefilled_forms_one_after_another(admin_client, weg):
    page = admin_client.get("/shipment/%d/repeat" % weg["cmr"]).get_data(as_text=True)
    assert "ЧМР товарителница № 0765/2026" in page and "Опаковъчен лист № 0498/2026" in page
    boxes = dict(re.findall(r'name="doc" value="(\d+)"( checked)?', page))
    assert boxes[str(weg["cmr"])] == " checked" and boxes[str(weg["packing"])] == " checked"
    assert boxes[str(weg["invoice"])] == ""  # фактурата — по избор
    resp = post_with_csrf(admin_client, "/shipment/%d/repeat" % weg["cmr"],
                          {"doc": [str(weg["cmr"]), str(weg["packing"])]},
                          csrf_source_url="/shipment/%d/repeat" % weg["cmr"])
    assert resp.headers["Location"].endswith("/shipment/repeat/next")
    resp = admin_client.get("/shipment/repeat/next")
    assert resp.headers["Location"].endswith("/doc/%d/copy" % weg["cmr"])
    resp = admin_client.get("/doc/%d/copy" % weg["cmr"])
    assert "/cmr/new?restore=" in resp.headers["Location"]
    form = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Trans Express EOOD" in form          # данните са копирани
    assert "Packing list 0498/2026" not in form  # „Приложени документи“ — не
    # На изгледа на документ — лентата „продължи“
    view = admin_client.get("/doc/%d" % weg["other_cmr"]).get_data(as_text=True)
    assert "ship-repeat-banner" in view and "остават 1 от 2" in view
    resp = admin_client.get("/shipment/repeat/next")
    assert resp.headers["Location"].endswith("/doc/%d/copy" % weg["packing"])
    assert "ship-repeat-banner" not in admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)


def test_repeat_cancel_and_no_open_redirect(admin_client, weg):
    post_with_csrf(admin_client, "/shipment/%d/repeat" % weg["cmr"], {"doc": [str(weg["cmr"])]},
                   csrf_source_url="/shipment/%d/repeat" % weg["cmr"])
    resp = post_with_csrf(admin_client, "/shipment/repeat/cancel", {"next": "//evil.example/x"})
    assert resp.headers["Location"].endswith("/docs")
    assert "ship-repeat-banner" not in admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)


# ---------------------------------------------------------------- бързодействие и стара база

def test_docs_list_has_no_n_plus_one(admin_client, db_module, con, monkeypatch):
    ids = [_add(con, "cmr", "%04d/2026" % (500 + i), {"consignee_name": "Perf %d" % i}) for i in range(40)]
    for i in ids:
        con.execute("INSERT INTO shipment_status (document_id, status, delivered_at) VALUES (?, 'delivered', '2026-11-01')", (i,))
        con.execute("INSERT INTO shipment_members (member_id, anchor_id) VALUES (?, ?)", (i, i))
    con.commit()
    import appcore
    real = appcore.get_db
    seen = []

    def traced():
        c = real()
        try:
            c.set_trace_callback(seen.append)
        except sqlite3.ProgrammingError:
            pass
        return c
    monkeypatch.setattr(appcore, "get_db", traced)
    import shipment_status
    monkeypatch.setattr(shipment_status, "get_db", traced)
    html = admin_client.get("/docs").get_data(as_text=True)
    assert html.count("ship-badge--delivered") == 40
    ship_sql = [s for s in seen if "shipment_" in s]
    assert len(ship_sql) <= 3, ship_sql


def test_old_database_without_tables_still_works(admin_client, db_module, weg):
    con = db_module.get_db()
    con.execute("DROP TABLE shipment_members")
    con.execute("DROP TABLE shipment_status")
    con.commit()
    con.close()
    assert admin_client.get("/docs").status_code == 200
    assert admin_client.get("/doc/%d" % weg["cmr"]).status_code == 200
    assert admin_client.get("/").status_code == 200
    assert admin_client.get("/reports").status_code == 200


def test_view_refresh_writes_after_the_read_snapshot(admin_client, db_module, weg, con):
    """Нова фактура към вече отбелязана пратка: прегледът на ЧМР-то обновява
    прихода (запис в after_request — изгледът е read_snapshot)."""
    _step(admin_client, weg["cmr"], "loaded")
    inv2 = _add(con, "invoice_no", "0000099999", {"consignee_name": WEG, "items": [
        {"po_no": PO, "qty": "2", "unit_price": "10"}]})
    con.execute("UPDATE documents SET data = json_set(data, '$.attached_docs',"
                " 'Invoice 0000012955, Invoice 0000099999, Packing list 0498/2026') WHERE id = ?",
                (weg["cmr"],))
    con.commit()
    admin_client.get("/doc/%d" % weg["cmr"])
    st = _st(con, weg["cmr"])
    assert str(inv2) in st["invoice_ids"].split(",") and st["revenue_cents"] == 400 + 2000
    assert st["status"] == "loaded"  # не е доставена — без авто „фактурирана“


def test_contract_price_from_cmr_is_default_transport_cost(admin_client, db_module, weg, con):
    """Договорената цена към превозвача (to_price_eur, група PARTIES) е
    разходът за превоз, докато в лентата не е въведен друг."""
    con.execute("UPDATE documents SET data = json_set(data, '$.to_price_eur', '1,25') WHERE id = ?",
                (weg["cmr"],))
    con.commit()
    _step(admin_client, weg["cmr"], "loaded")
    html = admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert "Договорена цена към превозвача по ЧМР: 1.25 €" in html
    assert "Марж <b>2.75 €</b>" in html  # 4,00 € − 1,25 €
    month = con.execute("SELECT substr(created_at, 1, 7) FROM documents WHERE id = ?",
                        (weg["cmr"],)).fetchone()[0]
    rep = admin_client.get("/reports?from=%s&to=%s" % (month, month)).get_data(as_text=True)
    assert "1.25\xa0€" in rep and "2.75\xa0€" in rep
    # Въведеният разход има предимство
    post_with_csrf(admin_client, "/shipment/%d/details" % weg["cmr"], {"transport_cost": "2"},
                   csrf_source_url="/doc/%d" % weg["cmr"])
    assert "Марж <b>2.00 €</b>" in admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
