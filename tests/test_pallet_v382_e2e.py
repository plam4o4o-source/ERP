# -*- coding: utf-8 -*-
"""v3.82.0, група PALLET — проверки в истински браузър (pytest -m e2e):
нето = бруто − тара на палетната карта (PALLET-7), „Опаковъчен лист от тази
поръчка (PO)“ (PALLET-10) и премигването на реда в списъка след запис
(PALLET-8)."""
import re

import pytest

from conftest import e2e_login

pytestmark = pytest.mark.e2e

FLASHED = ("(id) => { const r = document.querySelector('tr[data-row-id=\"' + id + '\"]');"
           " return r ? r.classList.contains('ph-row-flash') : null; }")


def _issue_card(page, base, client="Клиент ЕООД", po="4500012345", gross="520,5", net=None):
    page.goto(base + "/pallet/new")
    page.fill("#f-client_name", client)
    page.fill("#f-gross", gross)
    if net is not None:
        page.fill("#f-net", net)
    row = page.locator("#pallet-items tbody tr").first
    row.locator("[data-field=order_no]").fill(po)
    row.locator("[data-field=reference]").fill("REF-1")
    row.locator("[data-field=qty]").fill("2")
    page.click("#pallet-issue-btn")
    page.wait_for_url(re.compile(r".*/doc/\d+$"))
    return int(page.url.rsplit("/", 1)[1])


def test_net_is_gross_minus_tare_and_manual_value_is_kept(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill("#f-gross", "520,5")
    assert page.input_value("#f-net") == "500,5"          # десетичният знак следва брутото
    assert page.is_visible('label[for="f-net"] .packing-auto-badge')
    page.fill("#f-gross", "600")
    assert page.input_value("#f-net") == "580"            # авто следва брутото
    page.fill("#f-net", "570")
    page.fill("#f-gross", "610")
    assert page.input_value("#f-net") == "570"            # ръчното не се презаписва
    assert not page.is_visible('label[for="f-net"] .packing-auto-badge')
    page.click("#pallet-add-card-btn")
    card = page.locator(".pallet-card").nth(1)
    card.locator('[data-field="gross"]').fill("300.5")
    net = card.locator('[data-field="net"]')
    assert net.input_value() == "280.5" and net.get_attribute("name") == "net_2"


def test_same_po_button_opens_packing_list_with_card_nets(page, live_server):
    e2e_login(page, live_server)
    first = _issue_card(page, live_server, gross="520,5")
    _issue_card(page, live_server, gross="300", net="290")
    _issue_card(page, live_server, client="Друг клиент", gross="400")
    page.goto(live_server + "/doc/%d" % first)
    page.click("a:has-text('Опаковъчен лист от тази поръчка (PO)')")
    page.wait_for_url(re.compile(r".*/packing/new.*"))
    page.wait_for_function(
        "() => document.querySelectorAll('#packing-items tbody tr[data-pallet-number]').length >= 2")
    rows = page.eval_on_selector_all(
        "#packing-items tbody tr[data-pallet-number]",
        "trs => trs.map(tr => tr.querySelector('[data-field=net]').value)")
    assert rows == ["500,5", "290"]
    assert page.input_value('[name="receiver_name"]') == "Клиент ЕООД"
    assert page.input_value('[name="order_no"]') == "4500012345"


def test_list_flashes_last_saved_document_once(page, live_server):
    e2e_login(page, live_server)
    doc_id = _issue_card(page, live_server)
    other = _issue_card(page, live_server, po="999")
    # редакция на първата → изгледът → списъкът от менюто (без ?hl)
    page.goto(live_server + "/doc/%d/edit" % doc_id)
    page.fill("#f-gross", "333")
    page.click("button:has-text('Запази промените')")
    page.wait_for_url(re.compile(r".*/doc/%d$" % doc_id))
    page.click(".sidebar a[href='/docs']") if page.locator(".sidebar a[href='/docs']").count() \
        else page.goto(live_server + "/docs")
    page.wait_for_selector("tr[data-row-id]")
    assert page.evaluate(FLASHED, str(doc_id)) is True
    assert page.evaluate(FLASHED, str(other)) is False
    page.goto(live_server + "/docs")
    assert page.evaluate(FLASHED, str(doc_id)) is False   # само веднъж
    # само преглед (без запис) не се помни; бутонът към списъка носи ?hl
    page.goto(live_server + "/doc/%d" % other)
    page.click("a[data-doc-list-link]")
    page.wait_for_selector("tr[data-row-id]")
    assert page.evaluate(FLASHED, str(other)) is True
    page.goto(live_server + "/docs")
    assert page.evaluate(FLASHED, str(other)) is False
