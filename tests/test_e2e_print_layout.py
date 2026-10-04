# -*- coding: utf-8 -*-
"""Одит (04.10.2026) — печатни бланки (група PRINT), измерено в истински
печатен движок (Playwright/Chromium: emulate_media('print'), page.pdf + pypdf).
Пускат се изрично: `python3 -m pytest -m e2e tests/test_e2e_print_layout.py`.

Измерванията при печатна медия са с прозорец 733px — точно ширината на
печатното поле на A4 при `@page { margin: 8mm }` (194мм); иначе .print-page
(`width: auto` при печат) се разпъва по прозореца и дава измамни размери.
Измерванията на екран ползват самия .print-page (210мм с 8мм отстъп — същата
полезна ширина)."""
import io
import os

import pytest

from conftest import e2e_login
from test_print_layout import LONG_CODE, _doc

pytestmark = pytest.mark.e2e

pdf_module = pytest.importorskip("pypdf")
pytest.importorskip("playwright.sync_api")

PRINT_W = 733  # 194мм в CSS px
MM = 96 / 25.4


def _pdf(page, **kw):
    data = page.pdf(print_background=True, prefer_css_page_size=True, **kw)
    reader = pdf_module.PdfReader(io.BytesIO(data))
    return [(p.extract_text() or "") for p in reader.pages]


def _open(page, base, doc_id, query="", print_media=False):
    page.goto(base + "/doc/%d%s" % (doc_id, query))
    page.wait_for_load_state("load")
    if print_media:
        page.set_viewport_size({"width": PRINT_W, "height": 1000})
        page.emulate_media(media="print")


def _logo(db_module):
    """Широко лого (4:1) — точно това раздуваше заглавната лента (P4)."""
    from PIL import Image
    img = Image.new("RGB", (1200, 300), (10, 60, 140))
    img.save(os.path.join(os.path.dirname(db_module.DB_PATH), "company_logo.png"))


def _items_inv(n):
    return [dict(hs_code="7326909890", po_no="4500%06d" % i, pos=str(i * 10), net_weight="%d.125" % i,
                 material_code=LONG_CODE if i % 7 == 0 else "MAT-%05d" % i,
                 description="Стоманен детайл Größe %d — %s" % (i, "дълго описание " * (i % 4)),
                 pallet_no=str(1 + i // 5), qty=str(i * 1000), unit_price="%d.5" % (i * 1234))
            for i in range(1, n + 1)]


# ---------------------------------------------------------------- P3 / P19
@pytest.mark.parametrize("doc_type", ["invoice_br", "invoice_no", "invoice_dubai"])
def test_invoice_with_long_codes_stays_inside_the_page(page, live_server, db_module, doc_type):
    """P3: дълъг код без интервали разширяваше таблицата — „Total Price“ и
    общата сума излизаха извън листа (измерено: таблицата ~900px при 733px
    печатно поле). P19: числовите заглавия — вдясно."""
    doc_id = _doc(db_module, doc_type, {"invoice_number": "X-1", "doc_date": "2026-10-04",
                                        "items": _items_inv(30)})
    e2e_login(page, live_server)
    _open(page, live_server, doc_id, print_media=True)
    m = page.evaluate("""() => {
        const t = document.querySelector('.inv table.goods').getBoundingClientRect();
        const cells = [...document.querySelectorAll('.inv table.goods td')];
        const cut = cells.filter(td => td.scrollWidth > td.clientWidth + 1).length;
        const flex = [...document.querySelectorAll('.inv table.goods thead th')]
            .filter(th => !th.style.width)[0].getBoundingClientRect().width;
        const thn = [...document.querySelectorAll('.inv table.goods thead th.n')]
            .map(th => getComputedStyle(th).textAlign);
        return {right: t.right, vw: document.documentElement.clientWidth,
                sw: document.documentElement.scrollWidth, cut, flex, thn};
    }""")
    assert m["right"] <= m["vw"] + 0.5, m
    assert m["sw"] <= m["vw"], m
    assert m["cut"] == 0, m
    assert m["flex"] >= 170, m  # описанието/кодът не се свива до дума на ред
    assert m["thn"] and set(m["thn"]) == {"right"}, m


def test_norway_invoice_with_65_rows_prints_on_few_pages(page, live_server, db_module):
    """P3: описанието се свиваше до една дума на ред — 65 реда → 8 листа."""
    doc_id = _doc(db_module, "invoice_no", {"invoice_number": "NO-9", "items": _items_inv(65)})
    e2e_login(page, live_server)
    _open(page, live_server, doc_id)
    page.emulate_media(media="print")
    texts = _pdf(page)
    assert len(texts) <= 5, len(texts)
    assert "TOTAL" in texts[-1]


# ---------------------------------------------------------------- P4 / P8 / P9: товарителница
def _wb_items(n, long=False):
    desc = ("Стоманени профили студеноогънати 80×40×3 мм, дължина 6 м, горещо поцинковани, партида %05d"
            if long else "Стока %d")
    return [dict(description=desc % i, packing="палет EUR", marks="M-%02d" % i, weight="850", qty="3")
            for i in range(1, n + 1)]


def test_waybill_full_size_header_with_logo_fits(page, live_server, db_module):
    """P4: пълноразмерна товарителница (> 8 реда) с лого — заглавната лента
    преливаше с 53px, QR кодът се режеше наполовина, дясната рамка липсваше."""
    _logo(db_module)
    doc_id = _doc(db_module, "waybill", {"sender_name": "А", "consignee_name": "Б",
                                         "items": _wb_items(30)})
    e2e_login(page, live_server)
    _open(page, live_server, doc_id, print_media=True)
    m = page.evaluate("""() => {
        const h = document.querySelector('.twb-head');
        const q = h.querySelector('.doc-qr img').getBoundingClientRect();
        const r = h.getBoundingClientRect();
        return {sw: h.scrollWidth, cw: h.clientWidth, qr: q.right, head: r.right,
                twoUp: !!document.querySelector('.twb-2up')};
    }""")
    assert not m["twoUp"]
    assert m["sw"] <= m["cw"], m
    assert m["qr"] <= m["head"] - 2, m


def test_waybill_two_up_follows_measured_height_not_row_count(page, live_server, db_module):
    """P8: 8 реда с дълги описания → преди: смалени копия, всяко на свой
    лист (сървърът гледаше само броя редове). Сега: по едно пълноразмерно
    копие на лист. Обратно: 9 кратки реда (над сървърния праг от 8) се
    събират по две на лист."""
    long8 = _doc(db_module, "waybill", {"sender_name": "А", "consignee_name": "Б",
                                        "notes": "Стоката е проверена при товарене. " * 3,
                                        "items": _wb_items(8, long=True)})
    short9 = _doc(db_module, "waybill", {"sender_name": "А", "consignee_name": "Б",
                                         "items": _wb_items(9)})
    e2e_login(page, live_server)

    _open(page, live_server, long8)
    assert page.locator(".twb-2up").count() == 0
    assert page.locator(".print-page").count() == 2
    assert page.locator(".print-page > .twb").count() == 2
    page.emulate_media(media="print")
    texts = _pdf(page)
    assert len(texts) == 2, len(texts)
    assert all(t.count("ТОВАРИТЕЛНИЦА") == 1 for t in texts)

    page.emulate_media(media="screen")
    _open(page, live_server, short9)
    assert page.locator(".twb-2up").count() == 1
    assert page.locator(".twb-2up > .twb").count() == 2
    assert page.locator(".twb-2up > .twb-cut").count() == 1
    page.emulate_media(media="print")
    texts = _pdf(page)
    assert len(texts) == 1, len(texts)
    assert texts[0].count("ТОВАРИТЕЛНИЦА") == 2


def test_waybill_60_rows_has_no_sheet_with_only_the_footer(page, live_server, db_module):
    """P9: 60 реда → 8 листа, 2 от които носеха САМО реда на колонтитула.
    Данните са точно тези от одита (всички полета + дълъг текст, лого)."""
    _logo(db_module)
    long = "Акционерно дружество „Гьокхан Шимшек Индустри ве Тиджарет“ Анонимна Ширкети — Mü"
    base = dict(sender_name="ПачоЛогистик ЕООД", sender_address="ул. Индустриална 12, Габрово",
                carrier_name="Транс Експрес ООД", carrier_address="бул. България 1, София",
                consignee_name="Строймат АД", consignee_city="Пловдив", consignee_country="България",
                place_loading="Габрово", place_delivery="Пловдив", vehicle_make="Mercedes",
                vehicle_model="Actros", vehicle_reg="ЕВ1234АВ", route_sheet_no="000123",
                carrier_instructions="Без претоварване", notes="Стоката е проверена при товарене.",
                established_place="Габрово")
    data = {k: v + " " + long for k, v in base.items()}
    data.update(date_loading="2026-10-04", date_delivery="2026-10-05", loading_date="2026-10-04",
                unloading_date="2026-10-05", established_date="2026-10-04", mileage="12345",
                transport_price="1234567.89", extra_costs="98765.43", loading_from="08:00",
                loading_to="09:30", unloading_from="10:00", unloading_to="11:00",
                consignee_address="Organize Sanayi Bölgesi, 4. Kısım, Atatürk Bulvarı No: 123/A-B, Kat 7, "
                                  "Daire 42, Çerkezköy / Tekirdağ — ул. „Цар Иван Шишман“ № 1234, вх. Б, "
                                  "ет. 15, ап. 78, ж.к. „Младост 4“",
                items=[dict(description="Стоманени профили тип Ğüşİçö Größe %d — партида %05d %s"
                            % (i, i * 37, "дълго описание " * (i % 3)), packing="палет EUR",
                            marks="M-%03d" % i, weight="%d.25" % (1234 * i), qty=str(i * 3))
                       for i in range(1, 61)])
    doc_id = _doc(db_module, "waybill", data)
    e2e_login(page, live_server)
    _open(page, live_server, doc_id)
    page.emulate_media(media="print")
    texts = [t.replace(" ", "").replace("\n", "") for t in _pdf(page)]
    for i, t in enumerate(texts):
        if "съставенав" in t:
            assert "Подписипечатнаизпращача" in t, "лист %d носи само колонтитула" % (i + 1)


# ---------------------------------------------------------------- ЧМР: P5 / P6 / P7 / P13 / P26
def test_cmr_goods_keep_newlines_and_values_sit_under_their_headings(page, live_server, db_module):
    """P5: новите редове в 6–9 се губеха. P7: стойностите плуваха насред
    кутията (разтегнат заглавен ред). P26: надписът под QR кода опираше в
    рамката."""
    doc_id = _doc(db_module, "cmr", {"sender_name": "А", "consignee_name": "Б",
                                     "marks": "PL-001\nPL-002\nPL-003", "goods": "Стомана\nЛагери",
                                     "packages": "3", "weight": "1200"})
    e2e_login(page, live_server)
    _open(page, live_server, doc_id)
    m = page.evaluate("""() => {
        const td = document.querySelector('.cmr-grid .goods tr:nth-child(2) td');
        const rng = document.createRange(); rng.selectNodeContents(td);
        const tops = new Set([...rng.getClientRects()].map(r => Math.round(r.top)));
        // P7: долният ръб на заглавния ред спрямо края на текста в него —
        // разтегнатият ред оставяше десетки px празно под заглавията.
        const hdr = document.querySelector('.cmr-grid .goods tr:first-child');
        const th = hdr.getBoundingClientRect();
        let textBottom = 0;
        hdr.querySelectorAll('th').forEach(c => { const r = document.createRange();
            r.selectNodeContents(c); [...r.getClientRects()].forEach(x => {
                textBottom = Math.max(textBottom, x.bottom); }); });
        const box = document.querySelector('.cmr-grid .goods').getBoundingClientRect();
        const lab = document.querySelector('.cmr .doc-qr-label').getBoundingClientRect();
        const head = document.querySelector('.cmr-head').getBoundingClientRect();
        return {lines: tops.size, slack: th.bottom - textBottom, boxH: box.height, gap: head.right - lab.right};
    }""")
    assert m["lines"] == 3, m
    assert m["boxH"] > 100 and m["slack"] < 8, m
    assert m["gap"] >= 4, m


def test_extreme_cmr_has_no_stretched_empty_boxes_and_keeps_qr_and_barcode_readable(
        page, live_server, db_module):
    """P6: когато и най-малката степен не стига, на втори лист оставаха
    огромни празни кутии (1fr редове), последният ред на поле 9 се
    застъпваше с кутия 14. P13: QR падаше до ~10мм, черти на баркода 5.2мм."""
    long = ("Акционерно дружество „Гьокхан Шимшек Индустри ве Тиджарет“ Анонимна Ширкети — "
            "Müller-Lüdenscheidt Straßenbau GmbH & Co. KG Zweigniederlassung Großröhrsdorf")
    data = {k: "Стойност — " + long for k in (
        "sender_name", "sender_address", "consignee_name", "consignee_address", "place_delivery",
        "place_loading", "attached_docs", "carrier", "packing", "payment_instructions")}
    data.update(goods="\n".join("%d. Стоманени детайли тип Ğüşİçö-%d, Größe %d×%d mm, партида ЖЪЮЯ-%05d"
                                % (i, i, i * 10, i * 7, i) for i in range(1, 16)),
                marks="\n".join("PL-%03d" % i for i in range(1, 16)),
                sender_instructions="Да се пази от влага. Keep dry. " * 8,
                special_agreements="Доставка до 08:00 часа. " * 6, reservations="Без резерви. " * 6)
    doc_id = _doc(db_module, "cmr", data)
    e2e_login(page, live_server)
    _open(page, live_server, doc_id)
    m = page.evaluate("""() => {
        const pg = document.querySelector('.cmr-page');
        const td = document.querySelector('.cmr-grid .goods tr:nth-child(2) td:nth-child(4)');
        const box9 = document.querySelector('.cmr-grid .goods').getBoundingClientRect();
        const rng = document.createRange(); rng.selectNodeContents(td);
        const rects = [...rng.getClientRects()];
        const svg = document.querySelector('.cmr-head .no svg');
        const bar = svg.querySelector('rect[fill="#000"]').getBoundingClientRect();
        return {fit: pg.dataset.cmrFit, overflow: pg.classList.contains('cmr-overflow'),
                lastLine: rects[rects.length - 1].bottom, box9: box9.bottom,
                rows: getComputedStyle(document.querySelector('.cmr-grid')).gridAutoRows,
                qr: document.querySelector('.cmr .doc-qr img').getBoundingClientRect().width,
                bar: bar.height};
    }""")
    assert m["fit"] == "5" and m["overflow"], m
    assert m["rows"] == "auto", m
    assert m["lastLine"] <= m["box9"], m
    assert m["qr"] / MM >= 12, m
    assert m["bar"] / MM >= 8, m
    page.emulate_media(media="print")
    texts = _pdf(page)
    assert len(texts) == 1, len(texts)
    assert "PL-015" in texts[0]


# ---------------------------------------------------------------- P12: етикет 100×150
def test_pallet_label_fills_the_label_and_has_a_scannable_barcode(page, live_server, db_module):
    """P12: етикетът ползваше ~55% от височината, баркодът беше 79×5.9мм."""
    doc_id = _doc(db_module, "pallet", {"client_name": "Müller Logistik GmbH", "client_city": "München",
                                        "pallet_no": "3/12", "gross": "456.7", "pallet_type": "120×80"})
    e2e_login(page, live_server)
    _open(page, live_server, doc_id, "?format=label")
    m = page.evaluate("""() => {
        const svg = document.querySelector('.plt-head-barcode svg');
        const bar = svg.querySelector('rect[fill="#000"]').getBoundingClientRect();
        return {plt: document.querySelector('.plt').getBoundingClientRect().height,
                bar: bar.height, w: svg.getBoundingClientRect().width};
    }""")
    assert m["plt"] / MM >= 135, m
    assert m["bar"] / MM >= 20, m
    assert m["w"] / MM >= 80, m
    page.emulate_media(media="print")
    assert len(_pdf(page)) == 1
