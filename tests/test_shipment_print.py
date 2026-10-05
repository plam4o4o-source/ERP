# -*- coding: utf-8 -*-
"""Одит (05.10.2026): „Печат на цялата пратка“ — намиране на свързаните
документи (shipment.related_documents), бутонът и диалогът в лентата на
документа, страницата /shipment/print (заглавен лист + бланките, всяка
рендирана от собствения си печатен шаблон) и /shipment/lookup."""
import json
import re
import secrets

import pytest

from conftest import issue_cmr, post_with_csrf, read_source, with_required

#: Пратката от одобрения макет (WEG): ЧМР → опаковъчен лист (същото PO и
#: посочен в кл. 5) → фактура (посочена в кл. 5) → декларация (по фактурата)
#: → палетни карти („Свързано ЧМР №“).
PO = "4500012345"


def _add(con, doc_type, number, data, created_at=None):
    """Документ направо в базата (номер/баркод/токен — уникални)."""
    data = dict(data, number=number)
    year = int(number.rsplit("/", 1)[1]) if "/" in number else 2026
    seq = int(re.sub(r"\D", "", number.split("/")[0]) or 0)
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now','localtime')))",
        (doc_type, number, year, seq, "%s-%s" % (doc_type.upper(), secrets.token_hex(6).upper()),
         secrets.token_hex(16), json.dumps(data, ensure_ascii=False), created_at))
    con.commit()
    return cur.lastrowid


def _pallet(con, number, ref_cmr, po=PO, **kw):
    return _add(con, "pallet", number, dict({
        "client_name": "WEG Equipamentos Eletricos S.A.", "ref_cmr": ref_cmr,
        "items_format": "orders", "pallet_no": "1 от 3",
        "items": [{"order_no": po, "pos": "10", "reference": "1VL1", "reference_desc": "Busbar",
                   "qty": "4"}]}, **kw.pop("extra", {})), **kw)


@pytest.fixture
def weg(con):
    """Пратката + шум: друго ЧМР на същия клиент в същия ден, палетна карта
    към ДРУГО ЧМР със същото PO, фактура извън прозореца от ±30 дни."""
    ids = {}
    ids["cmr"] = _add(con, "cmr", "0765/2026", {
        "consignee_name": "WEG Equipamentos Eletricos S.A.", "consignee_address": "Av. Prefeito 3300",
        "consignee_city": "Jaragua do Sul", "consignee_country": "Brazil",
        "attached_docs": "Invoice 0000012955, Packing list 0498/2026",
        "carrier": "Trans Express EOOD", "truck_reg": "CB1234AB", "trailer_reg": "CB5678EE",
        "driver": "Ivan Petrov", "packages": "3", "packing": "Палети", "weight": "9870",
        "volume": "17.3", "place_loading": "Yavorets", "date_loading": "2026-10-04"})
    ids["packing"] = _add(con, "packing", "0498/2026", {
        "receiver_name": "WEG Equipamentos Eletricos S.A.", "order_no": PO,
        "invoice_no": "0000012955", "items": [{"description": "Палет 1 от 3", "qty": "4"}]})
    ids["invoice"] = _add(con, "invoice_br", "0000012955", {
        "consignee_name": "WEG Equipamentos Eletricos S.A.",
        "items": [{"po_no": PO, "pos": "10", "material_code": "1VL1", "qty": "4",
                   "unit_price": "1", "net_weight": "1"}]})
    ids["dualuse"] = _add(con, "dualuse", "0246/2026", {"invoice_numbers": "0000012955"})
    ids["pallets"] = [_pallet(con, "%04d/2026" % n, "0765/2026") for n in (2747, 2748, 2749)]
    ids["other_cmr"] = _add(con, "cmr", "0764/2026", {
        "consignee_name": "WEG Equipamentos Eletricos S.A.", "attached_docs": "Packing list 0001/2026"})
    ids["foreign_pallet"] = _pallet(con, "2750/2026", "0999/2026")
    ids["old_invoice"] = _add(con, "invoice_br", "0000011111", {
        "items": [{"po_no": PO}]}, created_at="2025-01-01 10:00:00")
    return ids


def _rows(flask_app, con, doc_id):
    import shipment
    row = con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    with flask_app.test_request_context("/"):
        return shipment.related_documents(con, row, json.loads(row["data"]))


# ---------------------------------------------------------------- related_documents

def test_related_documents_finds_the_whole_shipment_in_order(flask_app, con, weg):
    rows = _rows(flask_app, con, weg["cmr"])
    checked = [r for r in rows if r["checked"]]
    assert [r["doc_type"] for r in checked] == ["cmr", "packing", "invoice_br", "dualuse", "pallet"]
    assert checked[0]["id"] == weg["cmr"] and checked[0]["is_start"]
    assert checked[0]["default_copies"] == 3 and checked[0]["copies_options"] == (1, 3, 4, 5)
    # Палетните карти — един ред, но с всички id-та; само A4 (без избор).
    pallets = checked[-1]
    assert pallets["ids"] == weg["pallets"]
    assert pallets["copies_options"] is None
    assert "2747–2749/2026" in pallets["title"] and "(3" in pallets["title"]
    assert "0765/2026" in pallets["reason"]
    assert "0000012955" in checked[3]["reason"]          # декларацията — „по фактура“


def test_related_documents_weak_suggestions_are_unchecked(flask_app, con, weg):
    rows = _rows(flask_app, con, weg["cmr"])
    by_id = {i: r for r in rows for i in r["ids"]}
    other = by_id[weg["other_cmr"]]
    assert other["checked"] is False
    assert "същия клиент, същия ден" in other["reason"]
    # Картата към ДРУГО ЧМР не е от тази пратка — изобщо не се предлага,
    # нито фактурата извън прозореца от ±30 дни.
    assert weg["foreign_pallet"] not in by_id
    assert weg["old_invoice"] not in by_id


def test_related_documents_is_transitive_from_any_document(flask_app, con, weg):
    """От опаковъчния лист: ЧМР-то го посочва в кл. 5 → палетните карти по
    „Свързано ЧМР“ (3 кръга), а другото ЧМР остава предложение."""
    rows = _rows(flask_app, con, weg["packing"])
    checked_ids = {i for r in rows if r["checked"] for i in r["ids"]}
    assert {weg["cmr"], weg["invoice"], weg["dualuse"], *weg["pallets"]} <= checked_ids
    assert weg["other_cmr"] not in checked_ids
    assert rows[0]["id"] == weg["packing"]


def test_related_documents_single_document_has_no_shipment(flask_app, con):
    lonely = _add(con, "cmr", "0001/2026", {"consignee_name": "Самотен ЕООД"})
    rows = _rows(flask_app, con, lonely)
    assert [r["id"] for r in rows] == [lonely]


def test_short_numbers_do_not_link_documents(flask_app, con):
    """„1 от 3“ в редовете или позиция „10“ не са ключ за свързване."""
    a = _add(con, "packing", "0001/2026", {"order_no": "10", "items": [{"description": "Палет 1 от 3"}]})
    _add(con, "invoice_br", "0002/2026", {"items": [{"po_no": "10"}]})
    _pallet(con, "0003/2026", "", po="10")
    assert [r["id"] for r in _rows(flask_app, con, a)] == [a]


# ---------------------------------------------------------------- бутон и диалог

def test_toolbar_button_and_dialog_on_issued_document(flask_app, admin_client, con, weg):
    html = admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert "Печат на пратката (5)" in html
    assert 'id="shipment-modal"' in html and 'role="dialog"' in html
    # 5 отметнати + другото ЧМР (предложение) + шаблонът за добавяне
    assert html.count('class="ship-row') == 6 + 1
    assert 'data-ids="%s"' % ",".join(str(i) for i in weg["pallets"]) in html
    assert "Заглавен лист със съдържание" in html
    # „Двустранен печат“ и отделното „Изтегли PDF“ от макета нарочно липсват.
    assert "Двустранен" not in html


def test_no_button_without_related_documents(admin_client):
    doc_id = issue_cmr(admin_client, consignee_name="Самотен ЕООД")
    html = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert "data-shipment-open" not in html and "shipment-modal" not in html


def test_no_button_in_preview_or_public_view(admin_client, client, con, weg):
    resp = post_with_csrf(admin_client, "/cmr/preview", with_required("/cmr/new", {
        "consignee_name": "WEG Equipamentos Eletricos S.A.",
        "attached_docs": "Invoice 0000012955"}), csrf_source_url="/cmr/new")
    preview = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "data-shipment-open" not in preview
    token = con.execute("SELECT public_token FROM documents WHERE id = ?",
                        (weg["cmr"],)).fetchone()[0]
    public = client.get("/p/%s" % token).get_data(as_text=True)
    assert "data-shipment-open" not in public and "shipment-modal" not in public


# ---------------------------------------------------------------- /shipment/print

def _bundle_url(weg, cover=1, cmr_copies=3):
    ids = ["%d:%d" % (weg["cmr"], cmr_copies), "%d:1" % weg["packing"], "%d:2" % weg["invoice"],
           "%d:1" % weg["dualuse"]] + ["%d:1" % i for i in weg["pallets"]]
    return "/shipment/print?ids=%s&cover=%d&from=%d" % (",".join(ids), cover, weg["cmr"])


def test_bundle_renders_each_document_from_its_own_print_template(admin_client, weg):
    resp = admin_client.get(_bundle_url(weg))
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    sections = re.findall(r'<section class="shipment-doc" data-doc-id="(\d+)" data-doc-type="(\w+)"', html)
    # фактурата 2 екз. → две секции; ЧМР — една секция със своите 3 екземпляра
    assert [t for _i, t in sections] == ["cmr", "packing", "invoice_br", "invoice_br", "dualuse",
                                         "pallet", "pallet", "pallet"]
    assert html.count('class="print-page cmr-page"') == 3
    assert "3. Екземпляр за превозвача / Copy for carrier" in html
    assert html.count("ПАЛЕТНА КАРТА №") == 3
    # Само съдържанието: без лентата на документа, прикачените, публичния панел.
    assert "Копирай като нов" not in html and "Прикачени снимки" not in html
    assert "Публичен достъп през QR кода" not in html and "doc-qr-hint" not in html
    # Екранната лента на пакета — „Печат / PDF“ и връщане към документа.
    assert "Назад към документа" in html and 'href="/doc/%d"' % weg["cmr"] in html
    # Фактура за Бразилия — без „Total weight“ (изрично изискване).
    assert "Total weight" not in html


def test_bundle_cover_sheet_is_bilingual_with_contents(admin_client, weg):
    html = admin_client.get(_bundle_url(weg)).get_data(as_text=True)
    cover = html[html.index('class="print-page shipment-cover"'):html.index('<section class="shipment-doc"')]
    for text in ("Пратка / Shipment", "Получател / Consignee", "WEG Equipamentos Eletricos S.A.",
                 "Поръчка / PO", PO, "Превозвач / Carrier", "CB1234AB / CB5678EE", "Ivan Petrov",
                 "Товар / Cargo", "9870", "Натоварване / Loading", "ЧМР / CMR № 0765/2026",
                 "Палетни карти / Pallet cards № 2747–2749/2026", "3 карти / cards",
                 "изпращач, получател, превозвач"):
        assert text in cover, text
    # Брой листове НЕ се печата (не може да се знае сървърно) — само екземпляри.
    assert "Листа" not in cover and "Sheets" not in cover
    assert "ПачоЛогистик" not in cover
    assert admin_client.get(_bundle_url(weg, cover=0)).get_data(as_text=True).count(
        "shipment-cover") == 0


def test_bundle_validates_ids_and_copies(admin_client, weg):
    assert admin_client.get("/shipment/print").status_code == 404
    assert admin_client.get("/shipment/print?ids=abc,-3,0").status_code == 404
    assert admin_client.get("/shipment/print?ids=999999").status_code == 404
    # непознатите/невалидните се пропускат, повторенията — също; екземплярите са ограничени
    html = admin_client.get("/shipment/print?ids=999999,%d:99,%d:9,x:2,%d:7" % (
        weg["cmr"], weg["packing"], weg["cmr"])).get_data(as_text=True)
    assert html.count('class="print-page cmr-page"') == 5
    assert html.count('data-doc-type="packing"') == 3
    pallet = admin_client.get("/shipment/print?ids=%d:3" % weg["pallets"][0]).get_data(as_text=True)
    assert pallet.count('data-doc-type="pallet"') == 1


def test_bundle_is_capped(admin_client, monkeypatch, weg):
    import shipment
    monkeypatch.setattr(shipment, "MAX_PRINT_DOCS", 2)
    html = admin_client.get(_bundle_url(weg, cover=0)).get_data(as_text=True)
    assert html.count('<section class="shipment-doc"') == 1 + 1   # ЧМР + опаковъчен лист


def test_bundle_requires_login(client, weg):
    resp = client.get(_bundle_url(weg))
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get("/shipment/lookup?code=0765/2026").status_code == 302


def test_employee_sees_the_same_documents_as_in_view_document(employee_client, weg):
    """Правата са като на view_document: служителят отваря и фактурите."""
    assert employee_client.get("/doc/%d" % weg["invoice"]).status_code == 200
    html = employee_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert "Печат на пратката (5)" in html
    bundle = employee_client.get(_bundle_url(weg)).get_data(as_text=True)
    assert 'data-doc-type="invoice_br"' in bundle


def test_view_document_still_uses_the_same_context(admin_client, weg):
    """Рефакторът към _print_context не променя самостоятелния изглед."""
    html = admin_client.get("/doc/%d?copies=3" % weg["cmr"]).get_data(as_text=True)
    assert html.count('class="print-page cmr-page"') == 3
    assert "Прикачени снимки" in html and "Копирай като нов" in html


# ---------------------------------------------------------------- /shipment/lookup

def test_lookup_by_number_and_barcode(admin_client, con, weg):
    res = admin_client.get("/shipment/lookup?code=0246/2026").get_json()
    assert res["ok"] and res["rows"][0]["id"] == weg["dualuse"]
    assert res["rows"][0]["copies_options"] == [1, 2, 3]
    barcode = con.execute("SELECT barcode FROM documents WHERE id = ?",
                          (weg["pallets"][0],)).fetchone()[0]
    res = admin_client.get("/shipment/lookup?code=%s" % barcode.lower()).get_json()
    assert res["ok"] and res["rows"][0]["ids"] == [weg["pallets"][0]]
    assert res["rows"][0]["copies_options"] is None
    bad = admin_client.get("/shipment/lookup?code=NOPE-123").get_json()
    assert bad["ok"] is False and bad["error"]
    assert admin_client.get("/shipment/lookup?code=").get_json()["ok"] is False


def test_scan_still_finds_documents_after_refactor(admin_client, weg):
    resp = post_with_csrf(admin_client, "/scan", {"code": "0498/2026"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/doc/%d" % weg["packing"])


# ---------------------------------------------------------------- производителност

def test_related_documents_query_is_bounded_by_the_window(flask_app, con, weg):
    """Търсенето е само в ±30 дни около документа (индексът по created_at)."""
    import shipment
    seen = []
    con.set_trace_callback(seen.append)
    try:
        _rows(flask_app, con, weg["cmr"])
    finally:
        con.set_trace_callback(None)
    scans = [s for s in seen if "instr(" in s]
    assert scans and all("created_at >=" in s and "created_at <=" in s for s in scans)
    assert shipment.WINDOW_DAYS == 30


def test_frontend_pieces_are_in_place():
    js = read_source("static", "app.js")
    assert "function initShipmentPrint()" in js and "function initShipmentBundle()" in js
    fit = js[js.index("function fitWaybillPages()"):js.index("function initWaybillPrintFit")]
    assert "var scope = (root && root.querySelectorAll) ? root : document;" in fit
    assert 'isVisibleModal(document.getElementById("shipment-modal"))' in js
    css = read_source("static", "style.css")
    assert ".shipment-cover + .shipment-doc, .shipment-doc + .shipment-doc { break-before: page;" in css
