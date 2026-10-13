# -*- coding: utf-8 -*-
"""Одит (13.10.2026, v3.81.0, SHIP): „Печат на пратката“ в истински браузър
— колидиращи номера при трима клиенти, изтекла сесия при „добави по
номер“, изборът с клиента и датата, екземпляри на палетните карти и
целите за докосване на ↑/↓ на телефон.
Пускат се изрично: `python3 -m pytest -m e2e tests/test_shipment_v381_e2e.py`."""
import pytest

from conftest import e2e_login
from test_shipment_print import _add, weg  # noqa: F401 -- weg е fixture
from test_shipment_v381 import DSP, FERRO, UNAL, three_clients  # noqa: F401 -- fixture

pytestmark = pytest.mark.e2e
pytest.importorskip("playwright.sync_api")

PRINT_STUB = "window.__printed = 0; window.print = function () { window.__printed += 1; };"


def _open_dialog(page, base, doc_id):
    page.goto(base + "/doc/%d" % doc_id)
    page.wait_for_load_state("load")
    page.click("[data-shipment-open]")
    page.wait_for_selector("#shipment-modal", state="visible")


def _rows(page):
    return page.eval_on_selector_all(
        "#shipment-modal .ship-row",
        "rs => rs.map(r => ({ids: r.dataset.ids, type: r.dataset.type, title: r.dataset.title,"
        " checked: r.querySelector('.ship-check').checked,"
        " reason: r.querySelector('.ship-reason').textContent}))")


def test_colliding_numbers_of_three_clients_are_not_checked(page, live_server, con, three_clients):  # noqa: F811
    cmr = _add(con, "cmr", "0001/2026", {"consignee_name": UNAL,
                                        "attached_docs": "Packing list 0002/2026, Invoice 0000012955"})
    e2e_login(page, live_server)
    page.goto(live_server + "/doc/%d" % cmr)
    assert "Печат на пратката (1)" in page.inner_text("[data-shipment-open]")
    _open_dialog(page, live_server, cmr)
    rows = _rows(page)
    assert [r for r in rows if r["checked"]] == [rows[0]] and rows[0]["ids"] == str(cmr)
    by_id = {int(i): r for r in rows for i in r["ids"].split(",")}
    assert "друг клиент: %s" % DSP in by_id[three_clients["dsp_packing"]]["reason"]
    assert three_clients["ferro_pallet2"] not in by_id
    assert three_clients["dsp_waybill1"] not in by_id and three_clients["dsp_export1"] not in by_id


def test_add_by_number_choices_show_client_and_expired_session(page, live_server, con, three_clients):  # noqa: F811
    lonely = _add(con, "cmr", "0050/2026", {"consignee_name": UNAL})
    e2e_login(page, live_server)
    _open_dialog(page, live_server, lonely)
    # „12955“ намира и „0000012955“ (два клиента) — изборът показва клиента.
    page.fill(".ship-add-input", "12955")
    page.keyboard.press("Enter")
    page.wait_for_selector(".ship-choice")
    choices = page.eval_on_selector_all(".ship-choice", "bs => bs.map(b => b.textContent)")
    assert len(choices) == 3
    assert any(FERRO in c for c in choices) and any(DSP in c for c in choices)
    assert all("." in c.split(" · ")[-1] for c in choices)       # датата ДД.ММ.ГГГГ
    page.click(".ship-choice >> text=%s" % DSP)
    assert page.locator('.ship-row[data-type="invoice_dubai"]').count() == 1
    # Изтекла сесия → ясното съобщение, не „проверете връзката“.
    page.context.clear_cookies()
    page.fill(".ship-add-input", "0002/2026")
    page.click(".ship-add-btn")
    # (без wait_for_function — CSP script-src 'self' забранява eval в страницата)
    page.locator(".ship-add-msg", has_text="Сесията").wait_for()
    text = page.inner_text(".ship-add-msg")
    assert "Сесията е изтекла" in text and "връзката" not in text


def test_pallet_copies_reach_the_bundle(page, live_server, weg):  # noqa: F811
    page.context.add_init_script(PRINT_STUB)
    e2e_login(page, live_server)
    _open_dialog(page, live_server, weg["cmr"])
    assert "(7)" in page.inner_text("[data-shipment-open]")
    page.locator('.ship-row[data-type="pallet"] .ship-copies').select_option("2")
    assert "×2" in page.inner_text(".ship-summary")
    with page.context.expect_page() as popup:
        page.click(".ship-print")
    bundle = popup.value
    bundle.wait_for_load_state("load")
    assert bundle.locator('.shipment-doc[data-doc-type="pallet"]').count() == 6
    assert "документи: 7" in bundle.inner_text(".shipment-toolbar")
    assert "3 карти / cards × 2" in bundle.inner_text(".shipment-cover")


def test_move_buttons_are_touch_sized_on_phone(e2e_context_factory, live_server, weg):  # noqa: F811
    page = e2e_context_factory(viewport={"width": 390, "height": 844}, has_touch=True).new_page()
    e2e_login(page, live_server)
    _open_dialog(page, live_server, weg["cmr"])
    page.wait_for_timeout(600)       # анимацията на отваряне (scale) да свърши
    for box in page.eval_on_selector_all(
            ".ship-up, .ship-down", "bs => bs.map(b => b.getBoundingClientRect().toJSON())"):
        assert box["width"] >= 44 and box["height"] >= 44, box
    assert page.eval_on_selector("html", "h => h.scrollWidth <= window.innerWidth")
