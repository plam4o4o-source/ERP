# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, v3.83.0, SHIP): браузърни проверки — лентата на
пратката (етап от менюто, разход/CO2), шаблонът по клиент във формата за
ЧМР („авто“ + „Махни предложенията“) и телефонният изглед (390 px) без
хоризонтално превъртане."""
import pytest

from conftest import e2e_login
from test_shipment_print import _add, weg  # noqa: F401 -- weg е fixture

pytestmark = pytest.mark.e2e


def test_status_bar_steps_and_costs_in_browser(page, live_server, weg):
    e2e_login(page, live_server)
    page.goto(live_server + "/doc/%d" % weg["cmr"])
    page.click(".ship-step--loaded summary")
    assert page.is_visible(".ship-step--loaded .ship-step-form")
    # Отварянето на друго меню затваря първото
    page.click(".ship-step--delivered summary")
    assert not page.is_visible(".ship-step--loaded .ship-step-form")
    page.keyboard.press("Escape")
    assert not page.is_visible(".ship-step--delivered .ship-step-form")
    page.click(".ship-step--loaded summary")
    page.click(".ship-step--loaded button[value=set]")
    page.wait_for_selector(".ship-step--loaded.is-done")
    page.click(".ship-step--delivered summary")
    page.click(".ship-step--delivered button[value=set]")
    # Свързаната фактура → „Фактурирана“ (авто)
    page.wait_for_selector(".ship-step--invoiced.is-current .ship-auto")
    page.click(".ship-details-menu summary")
    page.fill("#ship-transport-cost", "1,50")
    page.fill("#ship-co2e", "12,5")
    page.click(".ship-details-menu button[type=submit]")
    page.wait_for_selector(".ship-figures")
    text = page.inner_text(".ship-figures")
    assert "1.50" in text and "2.50" in text and "12.5" in text  # приход 4 € − 1,50 €
    page.goto(live_server + "/docs?ship=invoiced")
    assert page.is_visible("text=0765/2026")


def test_client_template_fills_empty_fields_with_auto_badge(page, live_server, con):
    _add(con, "cmr", "0901/2026", {"consignee_name": "Template Client GmbH",
                                   "goods": "HV Switchgear", "stat_no": "8537 20 91",
                                   "place_delivery": "Munich", "attached_docs": "Invoice 1"})
    e2e_login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill('[name="stat_no"]', "Ръчно въведен")
    page.fill('[name="consignee_name"]', "template client gmbh")
    page.press('[name="consignee_name"]', "Tab")
    page.wait_for_selector(".ship-template-note")
    assert page.input_value('[name="goods"]') == "HV Switchgear"
    assert page.input_value('[name="place_delivery"]') == "Munich"
    assert page.input_value('[name="stat_no"]') == "Ръчно въведен"  # ръчното не се пипа
    assert page.input_value('[name="attached_docs"]') == ""
    assert "0901/2026" in page.inner_text(".ship-template-note")
    assert page.locator(".ship-template-badge").count() >= 2
    page.click(".ship-template-note button")
    assert page.input_value('[name="goods"]') == ""
    assert page.locator(".ship-template-note").count() == 0


def test_phone_width_status_bar_menu_fits(e2e_context_factory, live_server, weg):
    ctx = e2e_context_factory(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    e2e_login(pg, live_server)
    pg.goto(live_server + "/doc/%d" % weg["cmr"])
    pg.click(".ship-step--pod summary")
    box = pg.locator(".ship-step--pod .ship-step-form").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    pg.goto(live_server + "/docs")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
