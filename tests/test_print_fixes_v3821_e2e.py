# -*- coding: utf-8 -*-
"""Одит (21.10.2026, PRINT-1…14): печатните бланки, измерени в истински
печатен движок (Chromium: emulate_media('print'), page.pdf с
prefer_css_page_size + pypdf). `python3 -m pytest -m e2e tests/test_print_fixes_v3821_e2e.py`."""
import io

import pytest

from conftest import e2e_login
from test_print_layout import _doc

pytestmark = pytest.mark.e2e

pdf_module = pytest.importorskip("pypdf")
pytest.importorskip("playwright.sync_api")

PRINT_W = 733  # 194мм в CSS px — печатното поле на A4 при 8мм полета
MM = 96 / 25.4
CODE80 = ("MAT-ЖЪЮЯ-ĞŞİÇ-" + "0123456789" * 7)[:80]
LONG = ("Акционерно дружество „Гьокхан Шимшек Индустри ве Тиджарет“ Анонимна Ширкети — Müller-Lüdenscheidt "
        "Straßenbau GmbH & Co. KG Zweigniederlassung Großröhrsdorf")
LONGADDR = ("Organize Sanayi Bölgesi, 4. Kısım, Atatürk Bulvarı No: 123/A-B, Kat 7, Daire 42, Çerkezköy / Tekirdağ "
            "— ул. „Цар Иван Шишман“ № 1234, вх. Б, ет. 15, ап. 78, ж.к. „Младост 4“")


def _texts(page):
    data = page.pdf(print_background=True, prefer_css_page_size=True)
    return [(p.extract_text() or "") for p in pdf_module.PdfReader(io.BytesIO(data)).pages]


def _open(page, base, url, print_media=False):
    page.goto(base + url)
    page.wait_for_load_state("load")
    if print_media:
        page.set_viewport_size({"width": PRINT_W, "height": 1000})
        page.emulate_media(media="print")


def _bars_mm(page, selector):
    """(ширина на SVG, височина на чертите) в мм — от реалния размер на SVG и viewBox."""
    return page.evaluate("""(sel) => {
        const svg = document.querySelector(sel);
        const r = svg.getBoundingClientRect();
        const vb = svg.viewBox.baseVal;
        const bar = svg.querySelector('rect[fill="#000"]').height.baseVal.value;
        return [r.width / (96 / 25.4), r.height * bar / vb.height / (96 / 25.4)];
    }""", selector)


# ---------------------------------------------------------------- PRINT-1: товарителница
def test_waybill_long_code_stays_inside_the_sheet(page, live_server, db_module):
    items = [dict(description=CODE80, packing=CODE80[:30], marks=CODE80, weight="12345.678", qty="999999")] * 3
    wb = _doc(db_module, "waybill", {"sender_name": "А", "consignee_name": "Б", "items": items})
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d" % wb, print_media=True)
    m = page.evaluate("""() => [...document.querySelectorAll('.twb table.twb-goods')].map(t => {
        const ths = [...t.querySelectorAll('thead th')];
        return {right: t.getBoundingClientRect().right, last: ths[ths.length - 1].getBoundingClientRect().right,
                vw: document.documentElement.clientWidth,
                cut: [...t.querySelectorAll('td')].filter(td => td.scrollWidth > td.clientWidth + 1).length};
    })""")
    assert len(m) == 2
    for t in m:
        assert t["right"] <= t["vw"] + 0.5 and t["last"] <= t["right"] + 0.5, t
        assert t["cut"] == 0, t
    texts = _texts(page)
    assert all("Тегло, кг" in t and "Брой" in t for t in texts if "ТОВАРИТЕЛНИЦА" in t)


def test_waybill_two_up_still_follows_measured_height(page, live_server, db_module):
    one = _doc(db_module, "waybill", {"sender_name": "А", "consignee_name": "Б", "items": [
        dict(description="Стоманени профили", packing="палет", marks="SN-4500012345-000010-ABCDEFGH",
             weight="850.5", qty="3")]})
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d" % one)
    assert page.locator(".twb-2up > .twb").count() == 2
    page.emulate_media(media="print")
    assert len(_texts(page)) == 1


# ---------------------------------------------------------------- PRINT-2: етикет 100×150
@pytest.mark.parametrize("extra", [
    {},
    {"net": ""},
    {"ref_cmr": CODE80, "pallet_type": CODE80[:40], "height": "12345"},
])
def test_extreme_pallet_label_prints_on_one_label(page, live_server, db_module, extra):
    data = dict(client_name=LONG, client_address=LONGADDR, client_city="Çerkezköy / Tekirdağ — Бургас",
                client_country="Türkiye Cumhuriyeti", gross="123456.789", net="123436.789",
                pallet_no="123456 от 999999", packaging_type="Дървена каса / Wooden crate / Holzkiste",
                pallet_type="120×80", height="145", items_format="orders", items=[])
    data.update(extra)
    doc_id = _doc(db_module, "pallet", data)
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d?format=label" % doc_id)
    page.emulate_media(media="print")
    texts = _texts(page)
    assert len(texts) == 1, len(texts)
    assert "123436.789" in texts[0] or not data["net"]
    assert "стр." not in texts[0]  # етикетът е без номер на страница
    w, h = _bars_mm(page, ".plt-head-barcode svg")
    assert h >= 20 and w >= 85, (w, h)


def test_ordinary_pallet_label_is_not_shrunk(page, live_server, db_module):
    doc_id = _doc(db_module, "pallet", {"client_name": "Müller Logistik GmbH", "client_address": "Hauptstraße 5",
                                        "pallet_no": "3 от 12", "gross": "456.7", "net": "436.7",
                                        "items_format": "orders", "items": []})
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d?format=label" % doc_id)
    assert page.locator(".plt[data-label-zoom='1']").count() == 1


# ---------------------------------------------------------------- PRINT-3: A4 палетна карта
def test_pallet_card_barcode_keeps_its_size_with_cmr_ref(page, live_server, db_module):
    base = {"client_name": "Müller Logistik GmbH", "items_format": "orders", "items": []}
    plain = _doc(db_module, "pallet", base)
    refs = [_doc(db_module, "pallet", dict(base, ref_cmr=r))
            for r in ("0001/2026, 0002/2026, 0003/2026", "CMR-2026-000123456789-BG-TR-ISTANBUL", CODE80)]
    e2e_login(page, live_server)
    sizes = []
    for doc_id in [plain] + refs:
        _open(page, live_server, "/doc/%d" % doc_id, print_media=True)
        sizes.append(_bars_mm(page, ".plt-head-barcode svg"))
        assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    w0, h0 = sizes[0]
    assert 104 <= w0 <= 109 and h0 >= 10.5, sizes
    assert all(abs(w - w0) < 0.5 and abs(h - h0) < 0.2 for w, h in sizes), sizes
    page.goto(live_server + "/pallet/bulk-print?ids=%d,%d" % (plain, refs[-1]))
    page.wait_for_load_state("load")
    page.set_viewport_size({"width": PRINT_W, "height": 1000})
    page.emulate_media(media="print")
    widths = page.evaluate("""() => [...document.querySelectorAll('.plt-head-barcode svg')]
        .map(s => s.getBoundingClientRect().width / (96 / 25.4))""")
    assert len(widths) == 2 and abs(widths[0] - widths[1]) < 0.5 and widths[0] >= 104, widths


# ---------------------------------------------------------------- PRINT-4: № на реда
def _single_line_row_numbers(page):
    return page.evaluate("""() => [...document.querySelectorAll('td.rn')].filter(td => {
        const r = document.createRange(); r.selectNodeContents(td);
        return r.getClientRects().length > 1 || td.scrollWidth > td.clientWidth;
    }).map(td => td.textContent)""")


@pytest.mark.parametrize("doc_type", ["packing", "invoice_br", "invoice_no", "invoice_dubai"])
def test_row_numbers_over_100_do_not_wrap(page, live_server, db_module, doc_type):
    if doc_type == "packing":
        items = [dict(packing="Pallet", description="Steel %d" % i, qty="1", gross="100") for i in range(1, 121)]
        data = {"receiver_name": "К", "items": items}
    else:
        items = [dict(hs_code="7326909890", po_no="PO-77881", pos=str(i * 10), net_weight="1.125",
                      material_code="MAT-%05d" % i, description="Steel bracket %d" % i, pallet_no="1",
                      qty="3", unit_price="2.55") for i in range(1, 121)]
        data = {"invoice_number": "X-1", "consignee_name": "К", "items": items}
    doc_id = _doc(db_module, doc_type, data)
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d" % doc_id, print_media=True)
    assert page.locator("td.rn").count() == 120
    assert _single_line_row_numbers(page) == []


def test_norway_invoice_65_rows_still_on_4_sheets(page, live_server, db_module):
    from test_e2e_print_layout import _items_inv
    doc_id = _doc(db_module, "invoice_no", {"invoice_number": "NO-9", "items": _items_inv(65)})
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d" % doc_id)
    page.emulate_media(media="print")
    texts = _texts(page)
    assert len(texts) <= 4 and "TOTAL" in texts[-1], len(texts)


# ---------------------------------------------------------------- PRINT-5: номер на поръчка
def test_packing_long_order_number_wraps_inside_the_sheet(page, live_server, db_module):
    doc_id = _doc(db_module, "packing", {"receiver_name": "К", "invoice_no": CODE80, "order_no": "4500012345" * 10,
                                         "items": [{"description": "x", "qty": "1"}]})
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d" % doc_id, print_media=True)
    m = page.evaluate("""() => [...document.querySelectorAll('.pkl-meta > div')]
        .map(d => d.getBoundingClientRect().right)""")
    vw = page.evaluate("() => document.documentElement.clientWidth")
    assert m and max(m) <= vw + 0.5, (m, vw)


# ---------------------------------------------------------------- PRINT-6/7: заглавен лист
def _ui_lang(page, base, lang):
    page.goto(base + "/my-settings")
    page.select_option("#f-language", lang)
    page.locator("form:has(#f-language) button[type=submit]").first.click()
    page.wait_for_load_state("load")


def test_shipment_cover_turkish_ui_and_total_on_last_cover_page(page, live_server, db_module):
    first = _doc(db_module, "cmr", {"consignee_name": "Müller Logistik GmbH", "carrier": "Транс"})
    ids = [first] + [_doc(db_module, "export_it", {"declarant_name": "А", "invoice_no": str(i)})
                     for i in range(24)]
    e2e_login(page, live_server)
    _ui_lang(page, live_server, "tr")
    try:
        page.goto(live_server + "/shipment/print?ids=%s&cover=1&from=%d"
                  % (",".join("%d:1" % i for i in ids), first))
        page.wait_for_load_state("load")
        page.emulate_media(media="print")
        texts = _texts(page)
    finally:
        page.emulate_media(media="screen")
        _ui_lang(page, live_server, "bg")
    cover = [t for t in texts if "Документ / Document".upper() in t.upper() and "DOCUMENT" in t]
    assert len(cover) >= 2, [t[:80] for t in texts[:3]]
    assert "CONSIGNEE" in texts[0] and "İ" not in texts[0] and "COPİES" not in "".join(cover)
    assert [("Общо / Total" in t) for t in cover] == [False] * (len(cover) - 1) + [True]


# ---------------------------------------------------------------- PRINT-13: телефон
def test_shipment_toolbar_wraps_on_phone(live_server, db_module, e2e_context_factory):
    a = _doc(db_module, "cmr", {"consignee_name": "К"})
    ids = [a] + [_doc(db_module, "packing", {"receiver_name": "К", "items": [{"description": "x", "qty": "1"}]})
                 for _ in range(5)]
    page = e2e_context_factory(viewport={"width": 390, "height": 844}, is_mobile=True,
                               has_touch=True).new_page()
    e2e_login(page, live_server)
    page.goto(live_server + "/shipment/print?ids=%s&cover=1&from=%d" % (",".join("%d:1" % i for i in ids), a))
    page.wait_for_load_state("load")
    m = page.evaluate("""() => {
        const t = document.querySelector('.shipment-toolbar');
        const li = [...t.querySelectorAll('.shipment-included li')].map(l => l.getBoundingClientRect().right);
        return {sw: t.scrollWidth, cw: t.clientWidth, right: Math.max(...li), vw: window.innerWidth};
    }""")
    assert m["sw"] <= m["cw"] + 1 and m["right"] <= m["vw"], m


# ---------------------------------------------------------------- PRINT-14: номер на страница
def test_page_numbers_on_a4_but_not_on_cmr(page, live_server, db_module):
    items = [dict(packing="Pallet", description="Steel %d" % i, qty="1", gross="100") for i in range(1, 61)]
    pk = _doc(db_module, "packing", {"receiver_name": "К", "items": items})
    cmr = _doc(db_module, "cmr", {"consignee_name": "К"})
    e2e_login(page, live_server)
    _open(page, live_server, "/doc/%d" % pk)
    page.emulate_media(media="print")
    texts = _texts(page)
    n = len(texts)
    assert n >= 2 and all("стр. %d / %d" % (i + 1, n) in t for i, t in enumerate(texts)), [t[-40:] for t in texts]
    page.emulate_media(media="screen")
    _open(page, live_server, "/doc/%d?copies=3" % cmr)
    page.emulate_media(media="print")
    texts = _texts(page)
    assert len(texts) == 3 and not any("стр." in t for t in texts)
