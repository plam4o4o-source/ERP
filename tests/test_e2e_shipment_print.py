# -*- coding: utf-8 -*-
"""Одит (05.10.2026): „Печат на цялата пратка“ в истински браузър
(Playwright/Chromium) — диалогът (отметки, екземпляри, подредба с
клавиатура и с плъзгане, добавяне по номер, Escape/фокус), отварянето на
пакета в нов раздел и автоматичният прозорец за печат, поведението в
настолния прозорец (pywebview — същият прозорец) и самият печат: PDF-ът
започва със заглавния лист и всяка бланка е на нов лист.
Пускат се изрично: `python3 -m pytest -m e2e tests/test_e2e_shipment_print.py`."""
import io
import re

import pytest

from conftest import e2e_login
from test_shipment_print import _add, weg  # noqa: F401 -- weg е fixture

pytestmark = pytest.mark.e2e

pdf_module = pytest.importorskip("pypdf")
pytest.importorskip("playwright.sync_api")

#: window.print() не отваря нищо в headless режим — броим извикванията.
PRINT_STUB = "window.__printed = 0; window.print = function () { window.__printed += 1; };"


def _open_dialog(page, base, doc_id):
    e2e_login(page, base)
    page.goto(base + "/doc/%d" % doc_id)
    page.wait_for_load_state("load")
    page.click("[data-shipment-open]")
    page.wait_for_selector("#shipment-modal", state="visible")


def _row_titles(page):
    return page.eval_on_selector_all("#shipment-modal .ship-row",
                                     "rows => rows.map(r => r.dataset.title)")


def test_dialog_select_reorder_add_and_print(page, live_server, con, weg):  # noqa: F811
    waybill = _add(con, "waybill", "0099/2026", {"consignee_name": "Друг ЕООД",
                                                 "items": [{"description": "x"}]})
    page.context.add_init_script(PRINT_STUB)
    _open_dialog(page, live_server, weg["cmr"])
    modal = page.locator("#shipment-modal")
    assert modal.get_attribute("role") == "dialog"
    # Фокусът е в диалога (върху „Печат / PDF“), фонът — inert.
    assert page.evaluate("document.activeElement.classList.contains('ship-print')")
    assert page.evaluate("document.querySelector('.app-shell').inert") is True
    titles = _row_titles(page)
    assert titles[0].endswith("0765/2026") and "0764/2026" in titles[-1]
    assert page.locator(".ship-row").nth(len(titles) - 1).locator(".ship-check").is_checked() is False

    # Без декларацията — обобщението се обновява на живо.
    dualuse = page.locator('.ship-row[data-type="dualuse"]')
    dualuse.locator(".ship-check").uncheck()
    assert "0246/2026" not in page.inner_text(".ship-summary")
    assert "заглавен лист" in page.inner_text(".ship-summary")

    # Клавиатура: Alt+↓ върху опаковъчния лист го слага след фактурата.
    page.locator('.ship-row[data-type="packing"] .ship-check').focus()
    page.keyboard.press("Alt+ArrowDown")
    order = page.eval_on_selector_all(".ship-row", "rs => rs.map(r => r.dataset.type)")
    assert order.index("invoice_br") < order.index("packing")
    assert page.evaluate("document.activeElement.closest('.ship-row').dataset.type") == "packing"

    # ЧМР — 1 екземпляр; добавяне на товарителница по номер.
    page.locator('.ship-row[data-type="cmr"]').first.locator(".ship-copies").select_option("1")
    page.fill(".ship-add-input", "0099/2026")
    page.keyboard.press("Enter")
    page.wait_for_selector('.ship-row[data-type="waybill"]')
    assert "0099/2026" in _row_titles(page)[-1]
    page.fill(".ship-add-input", "0099/2026")
    page.click(".ship-add-btn")
    page.wait_for_function("document.querySelector('.ship-add-msg').textContent.length > 0")
    assert page.locator('.ship-row[data-type="waybill"]').count() == 1

    # Escape затваря и връща фокуса на бутона; отваряме пак.
    page.keyboard.press("Escape")
    assert modal.is_hidden()
    assert page.evaluate("document.activeElement.hasAttribute('data-shipment-open')")
    page.click("[data-shipment-open]")

    with page.context.expect_page() as popup:
        page.click(".ship-print")
    bundle = popup.value
    bundle.wait_for_load_state("load")
    bundle.wait_for_function("window.__printed === 1", timeout=5000)
    assert "autoprint" not in bundle.url        # не се отпечатва пак при презареждане
    sections = bundle.eval_on_selector_all(
        ".shipment-doc", "s => s.map(x => x.dataset.docType + ':' + x.dataset.docId)")
    types = [s.split(":")[0] for s in sections]
    assert types[:3] == ["cmr", "invoice_br", "packing"]
    assert "dualuse" not in types and types[-1] == "waybill"
    assert sections[-1] == "waybill:%d" % waybill
    assert types.count("pallet") == 3
    assert bundle.locator(".cmr-page").count() == 1
    assert bundle.locator(".shipment-cover").count() == 1
    bundle.click("text=Назад към документа")
    bundle.wait_for_url(re.compile(r".*/doc/%d$" % weg["cmr"]))


def test_drag_and_drop_reorders_rows(page, live_server, weg):  # noqa: F811
    _open_dialog(page, live_server, weg["cmr"])
    page.drag_and_drop('.ship-row[data-type="pallet"] .ship-handle', '.ship-row[data-type="cmr"]',
                       target_position={"x": 20, "y": 3})
    order = page.eval_on_selector_all(".ship-row", "rs => rs.map(r => r.dataset.type)")
    assert order[0] == "pallet", order
    assert page.inner_text(".ship-summary").index("Палетни карти") < \
        page.inner_text(".ship-summary").index("0765/2026")


def test_desktop_window_opens_bundle_in_the_same_window(page, live_server, weg):  # noqa: F811
    """pywebview/WebView2: новите прозорци отиват в системния браузър (без
    вход) — затова пакетът се отваря в същия прозорец."""
    page.context.add_init_script(PRINT_STUB + "window.pywebview = {api: {}};")
    _open_dialog(page, live_server, weg["cmr"])
    pages_before = len(page.context.pages)
    page.click(".ship-print")
    page.wait_for_url(re.compile(r".*/shipment/print\?.*"))
    page.wait_for_function("window.__printed === 1", timeout=5000)
    assert len(page.context.pages) == pages_before
    assert page.locator("text=Назад към документа").is_visible()


def test_bundle_prints_cover_then_each_document_on_new_sheets(page, live_server, weg):  # noqa: F811
    e2e_login(page, live_server)

    def pdf_texts(url):
        page.emulate_media(media="screen")
        page.goto(live_server + url)
        page.wait_for_load_state("load")
        page.emulate_media(media="print")
        data = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        return [(p.extract_text() or "") for p in pdf_module.PdfReader(io.BytesIO(data)).pages]

    single = {}
    for key, url in (("cmr", "/doc/%d?copies=3" % weg["cmr"]), ("packing", "/doc/%d" % weg["packing"]),
                     ("invoice", "/doc/%d" % weg["invoice"]), ("dualuse", "/doc/%d" % weg["dualuse"])):
        single[key] = len(pdf_texts(url))
    ids = ["%d:3" % weg["cmr"], "%d:1" % weg["packing"], "%d:1" % weg["invoice"],
           "%d:1" % weg["dualuse"]] + ["%d:1" % i for i in weg["pallets"]]
    texts = pdf_texts("/shipment/print?ids=%s&cover=1&from=%d" % (",".join(ids), weg["cmr"]))
    assert len(texts) == 1 + sum(single.values()) + len(weg["pallets"])
    assert "Пратка / Shipment" in texts[0] and "Pallet cards" in texts[0]
    # Всеки документ започва на нов лист — в началото на точно този лист.
    starts = [1, 1 + single["cmr"], 1 + single["cmr"] + single["packing"]]
    assert "Екземпляр за изпращача" in texts[starts[0]] and "0765/2026" in texts[starts[0]]
    assert "0498/2026" in texts[starts[1]]
    assert "0000012955" in texts[starts[2]]
    dual_start = starts[2] + single["invoice"]
    assert "0246/2026" in texts[dual_start]
    for k, number in enumerate(("2747/2026", "2748/2026", "2749/2026")):
        assert "ПАЛЕТНА КАРТА" in texts[dual_start + 1 + k] and number in texts[dual_start + 1 + k]
    assert all(t.strip() for t in texts), "празен лист в пакета"
    # Екранната лента не се печата.
    assert not any("Назад към документа" in t for t in texts)
