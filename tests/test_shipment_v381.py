# -*- coding: utf-8 -*-
"""Одит (13.10.2026, v3.81.0, SHIP): „Печат на пратката“ — свързване само по
съвместим вид документ и само в рамките на един клиент; палетните карти
със същото PO без свързано ЧМР — без отметка; „Ref. ЧМР №“ на декларацията
за износ; заглавният лист без ЧМР/опаковъчен лист; броят документи на
бутона/лентата; бутонът и при един документ; екземпляри на палетните карти;
добавяне по номер без регистър/водещи нули и с клиента в избора.

Номерата на всеки вид документ започват от 0001/ГГГГ — затова сценариите
нарочно имат ЕДНАКВИ номера при трима различни клиенти."""
import pytest

from conftest import read_source
from test_shipment_print import PO, _add, _pallet, _rows, weg  # noqa: F401 -- weg е fixture

UNAL = "Ünal Lojistik A.Ş."
DSP = "DSP Logistics GmbH"
FERRO = "Ferro Trade SRL"
FERRO_PO = "4500077777"


def _checked(rows):
    return {i for r in rows if r["checked"] for i in r["ids"]}


def _offered(rows):
    return {i: r for r in rows for i in r["ids"]}


@pytest.fixture
def three_clients(con):
    """Трима клиенти с колидиращи номера (както в браузърния тест)."""
    ids = {}
    ids["dsp_packing"] = _add(con, "packing", "0002/2026", {
        "receiver_name": DSP, "order_no": "PO-DSP-77", "items": [{"description": "Box"}]})
    ids["ferro_pallet2"] = _add(con, "pallet", "0002/2026", {
        "client_name": FERRO, "ref_cmr": "", "items_format": "orders",
        "items": [{"order_no": FERRO_PO, "qty": "1"}]})
    ids["ferro_packing1"] = _add(con, "packing", "0001/2026", {
        "receiver_name": FERRO, "order_no": FERRO_PO, "invoice_no": "0000012955"})
    ids["ferro_inv_no"] = _add(con, "invoice_no", "0000012955", {
        "consignee_name": FERRO, "items": [{"po_no": FERRO_PO}]})
    ids["ferro_dualuse"] = _add(con, "dualuse", "0001/2026", {
        "dest_name": FERRO, "invoice_numbers": "0000012955"})
    ids["dsp_waybill1"] = _add(con, "waybill", "0001/2026", {"consignee_name": DSP})
    ids["dsp_export1"] = _add(con, "export_it", "0001/2026", {"receiver_name": DSP})
    ids["ferro_pallet1"] = _add(con, "pallet", "0001/2026", {
        "client_name": FERRO, "items": [{"order_no": FERRO_PO}]})
    ids["dsp_inv_dubai"] = _add(con, "invoice_dubai", "0000012955", {"consignee_name": DSP})
    ids["ferro_inv_dubai_short"] = _add(con, "invoice_dubai", "12955", {"consignee_name": FERRO})
    return ids


def test_free_text_mention_links_only_the_compatible_type_and_client(flask_app, con, three_clients):
    """Бъг 1а: „Packing list 0002/2026“ в ЧМР на Ünal отмяташе палетната
    карта 0002/2026 на Ferro, после по PO — целия Ferro."""
    cmr = _add(con, "cmr", "0001/2026", {"consignee_name": UNAL,
                                        "attached_docs": "Packing list 0002/2026"})
    rows = _rows(flask_app, con, cmr)
    assert _checked(rows) == {cmr}
    offered = _offered(rows)
    # Видът съвпада (опаковъчен лист), клиентът — не: само предложение с причина.
    dsp = offered[three_clients["dsp_packing"]]
    assert dsp["checked"] is False and "друг клиент" in dsp["reason"] and DSP in dsp["reason"]
    # Палетна карта със същия номер не е „Packing list“ — изобщо не се предлага,
    # нито документите на Ferro, стигнати по PO на тази карта.
    for key in ("ferro_pallet2", "ferro_packing1", "ferro_inv_no", "ferro_dualuse"):
        assert three_clients[key] not in offered, key


def test_invoice_and_packing_mentions_of_other_clients_are_not_checked(flask_app, con, three_clients):
    """Бъг 1б: „Invoice 0000012955, Packing list 0001/2026“ дърпаше
    товарителница, декларация за износ, палетна карта 0001 и фактурите за
    Норвегия/Дубай (вкл. „12955“) на други клиенти."""
    own_invoice = _add(con, "invoice_br", "0000012955", {
        "consignee_name": UNAL, "items": [{"po_no": "PO-UNAL-1"}]})
    cmr = _add(con, "cmr", "0003/2026", {
        "consignee_name": UNAL, "attached_docs": "Invoice 0000012955, Packing list 0001/2026"})
    rows = _rows(flask_app, con, cmr)
    assert _checked(rows) == {cmr, own_invoice}
    offered = _offered(rows)
    for key in ("dsp_waybill1", "dsp_export1", "ferro_pallet1", "ferro_pallet2"):
        assert three_clients[key] not in offered, key
    # Същият вид, друг клиент (и декларацията на Ferro „по фактура 0000012955“)
    # — само предложение с причина.
    for key in ("ferro_inv_no", "dsp_inv_dubai", "ferro_inv_dubai_short", "ferro_packing1",
                "ferro_dualuse"):
        assert offered[three_clients[key]]["checked"] is False, key
        assert "друг клиент" in offered[three_clients[key]]["reason"], key


def test_bare_number_without_document_kind_is_only_a_suggestion(flask_app, con):
    cmr = _add(con, "cmr", "0005/2026", {"consignee_name": UNAL, "attached_docs": "0007/2026"})
    packing = _add(con, "packing", "0007/2026", {"receiver_name": UNAL})
    rows = _rows(flask_app, con, cmr)
    assert _checked(rows) == {cmr}
    row = _offered(rows)[packing]
    assert row["checked"] is False and "без вид на документа" in row["reason"]


def test_mention_keywords_in_bulgarian_and_lists(flask_app, con):
    """Кирилски ключови думи и списък след една ключова дума („Фактури 1, 2“)."""
    inv1 = _add(con, "invoice_br", "0000000101", {"consignee_name": UNAL})
    inv2 = _add(con, "invoice_br", "0000000102", {"consignee_name": UNAL})
    packing = _add(con, "packing", "0011/2026", {"receiver_name": UNAL})
    pallet = _add(con, "pallet", "0011/2026", {"client_name": UNAL})
    cmr = _add(con, "cmr", "0012/2026", {
        "consignee_name": UNAL, "attached_docs": "Фактури 0000000101, 0000000102; Опаковъчен лист 0011/2026"})
    rows = _rows(flask_app, con, cmr)
    assert _checked(rows) == {cmr, inv1, inv2, packing}
    assert pallet not in _offered(rows)


def test_mentions_parser_types():
    import shipment
    refs, amb = shipment._mentions("Invoice No. 0000012955, Packing list 0498/2026; 0777/2026\n"
                                   "Dual-use declaration 0246/2026, CMR 0765/2026, Палетна карта 2747/2026")
    assert refs["12955"] == set(shipment.db.INVOICE_DOC_TYPES)
    assert refs["498/2026"] == {"packing"}
    assert refs["246/2026"] == {"dualuse"}
    assert refs["765/2026"] == {"cmr"}
    assert refs["2747/2026"] == {"pallet"}
    assert amb == {"777/2026"}
    # Непознат документ след номер („Certificate of origin“) не наследява вида.
    refs, amb = shipment._mentions("Invoice 0000012955 Certificate of origin 0003/2026")
    assert "3/2026" not in refs and amb == {"3/2026"}


def test_same_po_pallets_of_another_batch_are_unchecked_from_any_start(flask_app, con):
    """Бъг 2: от палетна карта картите на друга партида със същото PO и без
    свързано ЧМР идваха отметнати."""
    cmr = _add(con, "cmr", "0765/2026", {"consignee_name": "WEG"})
    packing = _add(con, "packing", "0498/2026", {"receiver_name": "WEG", "order_no": PO})
    batch_a = [_pallet(con, "%04d/2026" % n, "") for n in (2747, 2748)]
    # Опаковъчният лист е стигнат по PO, ЧМР-то — едва после (по кл. 5).
    con.execute("UPDATE documents SET data = json_set(data, '$.attached_docs', 'Packing list 0498/2026')"
                " WHERE id = ?", (cmr,))
    con.commit()
    batch_b = [_pallet(con, "%04d/2026" % n, "") for n in (2801, 2802)]
    for start in (batch_a[0], batch_b[0]):
        rows = _rows(flask_app, con, start)
        checked = _checked(rows)
        others = [i for i in batch_a + batch_b if i != start]
        assert not (set(others) & checked), (start, rows)
        offered = _offered(rows)
        for i in others:
            assert offered[i]["checked"] is False
            assert "без свързано ЧМР" in offered[i]["reason"]
        assert {packing, cmr} <= checked


def test_same_linked_cmr_number_links_pallets_even_without_the_cmr(flask_app, con):
    cards = [_pallet(con, "%04d/2026" % n, "0900/2026") for n in (3001, 3002, 3003)]
    rows = _rows(flask_app, con, cards[1])
    assert set(cards) <= _checked(rows)


def test_export_declaration_ref_cmr_links_both_ways(flask_app, con):
    """Бъг 3: „Ref. ЧМР №“ на декларацията за износ (Италия) се пренебрегваше."""
    cmr = _add(con, "cmr", "0765/2026", {"consignee_name": "WEG"})
    export = _add(con, "export_it", "0031/2026", {"receiver_name": "WEG", "ref_cmr": "0765/2026"})
    # Шум: ЧМР със същия номер не съществува в друг тип; друга декларация към друго ЧМР.
    other = _add(con, "export_it", "0032/2026", {"receiver_name": "WEG", "ref_cmr": "0766/2026"})
    rows = _rows(flask_app, con, cmr)
    assert export in _checked(rows) and other not in _checked(rows)
    assert "посочва ЧМР" in _offered(rows)[export]["reason"]
    rows = _rows(flask_app, con, export)
    assert cmr in _checked(rows)
    assert "посочено в" in _offered(rows)[cmr]["reason"]


def test_start_reason_only_for_the_start_card(flask_app, con, weg):
    rows = _rows(flask_app, con, weg["pallets"][0])
    group = rows[0]
    assert group["ids"] == weg["pallets"]
    assert "2747/2026" in group["reason"] and "този документ" in group["reason"]
    assert "свързано ЧМР № 0765/2026" in group["reason"]


def test_pallet_cards_have_copies(flask_app, con, admin_client, weg):
    rows = _rows(flask_app, con, weg["cmr"])
    pallets = [r for r in rows if r["doc_type"] == "pallet"][0]
    assert pallets["copies_options"] == (1, 2, 3) and pallets["default_copies"] == 1
    html = admin_client.get("/shipment/print?ids=%s&cover=1" % ",".join(
        "%d:2" % i for i in weg["pallets"])).get_data(as_text=True)
    assert html.count('data-doc-type="pallet"') == 6
    cover = html[html.index('class="print-page shipment-cover"'):html.index('<section class="shipment-doc"')]
    assert "3 карти / cards × 2" in cover and ">6<" in cover


def test_button_counts_documents_and_shows_from_one_document(admin_client, con, weg):
    html = admin_client.get("/doc/%d" % weg["cmr"]).get_data(as_text=True)
    assert "Печат на пратката (7)" in html            # 4 бланки + 3 палетни карти
    lonely = _add(con, "waybill", "0042/2026", {"consignee_name": "Самотен ЕООД"})
    html = admin_client.get("/doc/%d" % lonely).get_data(as_text=True)
    assert "Печат на пратката (1)" in html and 'id="shipment-modal"' in html


def test_bundle_bar_counts_documents(admin_client, weg):
    ids = ["%d:1" % weg["cmr"]] + ["%d:1" % i for i in weg["pallets"]]
    html = admin_client.get("/shipment/print?ids=%s" % ",".join(ids)).get_data(as_text=True)
    assert "документи: 4" in html


def test_cover_falls_back_to_pallets_and_waybill(admin_client, con):
    """Бъг 9: без ЧМР/опаковъчен лист заглавният лист беше само „—“."""
    cards = [_add(con, "pallet", "%04d/2026" % n, {
        "client_name": FERRO, "client_address": "Via Roma 1", "client_city": "Milano",
        "client_country": "Italy", "gross": "512,5",
        "items": [{"order_no": FERRO_PO}]}) for n in (1, 2, 3)]
    html = admin_client.get("/shipment/print?ids=%s&cover=1" % ",".join(
        "%d:1" % i for i in cards)).get_data(as_text=True)
    cover = html[html.index('class="print-page shipment-cover"'):html.index('<section class="shipment-doc"')]
    for text in (FERRO, "Milano", FERRO_PO, "1537.5", "3 палета / pallets"):
        assert text in cover, text
    waybill = _add(con, "waybill", "0009/2026", {
        "consignee_name": DSP, "consignee_city": "Sofia", "carrier_name": "Trans BG",
        "vehicle_reg": "CB1234AB", "place_loading": "Yavorets", "date_loading": "2026-10-04",
        "items": [{"description": "x", "qty": "2", "packing": "палета", "weight": "800"}]})
    dual = _add(con, "dualuse", "0009/2026", {"dest_name": DSP, "invoice_numbers": "0000000009"})
    html = admin_client.get("/shipment/print?ids=%d:1,%d:1&cover=1" % (waybill, dual)).get_data(as_text=True)
    cover = html[html.index('class="print-page shipment-cover"'):html.index('<section class="shipment-doc"')]
    for text in (DSP, "Sofia", "Trans BG", "CB1234AB", "Yavorets", "04.10.2026", "800"):
        assert text in cover, text


def test_lookup_is_case_insensitive_and_leading_zero_tolerant(admin_client, con, three_clients):
    res = admin_client.get("/shipment/lookup?code=12955").get_json()
    assert res["ok"]
    found = {r["id"] for r in res["rows"]}
    assert {three_clients["ferro_inv_no"], three_clients["dsp_inv_dubai"],
            three_clients["ferro_inv_dubai_short"]} <= found
    manual = _add(con, "invoice_br", "REQ-BR", {"consignee_name": UNAL})
    res = admin_client.get("/shipment/lookup?code=req-br").get_json()
    assert res["ok"] and res["rows"][0]["id"] == manual
    # Изборът при няколко документа показва клиента и датата.
    res = admin_client.get("/shipment/lookup?code=0001/2026").get_json()
    clients = {r["client_name"] for r in res["rows"]}
    assert {FERRO, DSP} <= clients and all(r["date"] for r in res["rows"])


def test_frontend_v381_pieces():
    js = read_source("static", "app.js")
    body = js[js.index("function initShipmentPrint()"):js.index("function initShipmentBundle()")]
    assert "fetchJsonSafe(url" in body and "fetch(url" not in body
    assert "sessionExpired" in body and "client_name" in body
    css = read_source("static", "style.css")
    narrow = css[css.index(".ship-handle { display: none; }") - 400:css.index(".ship-handle { display: none; }")]
    assert "min-height: 44px" in narrow and "min-width: 44px" in narrow


def test_related_documents_stays_bounded_with_typed_mentions(flask_app, con, weg):
    """Типизираните споменавания не добавят нови сканирания извън прозореца."""
    seen = []
    con.set_trace_callback(seen.append)
    try:
        _rows(flask_app, con, weg["cmr"])
    finally:
        con.set_trace_callback(None)
    scans = [s for s in seen if "instr(" in s]
    assert scans and all("created_at >=" in s for s in scans)
