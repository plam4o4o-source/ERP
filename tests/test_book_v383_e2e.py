# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, група BOOK, v3.83.0) — в истински браузър: глобалното
търсене (Ctrl+K / „/“, падащ списък по групи, стрелки/Enter/Esc, страницата
/search, телефон 390px) и единният адресник (запис за фактури от клиент,
предупреждението „фактурата е за друг клиент от този на пратката“)."""
import json

import pytest

from conftest import e2e_login

pytestmark = pytest.mark.e2e


def _seed(db_module):
    con = db_module.get_db()
    abb = con.execute("INSERT INTO clients (name, city) VALUES ('ABB Brasil Ltda', 'Sorocaba')").lastrowid
    con.execute("INSERT INTO clients (name, city) VALUES ('Müller & Söhne GmbH', 'Köln')")
    entry = con.execute("INSERT INTO invoice_clients (name, billing_name, client_id) VALUES"
                        " ('ABB BR', 'ABB Brasil Ltda', ?)", (abb,)).lastrowid
    for n, (doc_type, data) in enumerate([
            ("pallet", {"client_name": "Müller & Söhne GmbH", "items_format": "orders", "items": [
                {"order_no": "4700201619", "pos": "10", "reference": "1VL1", "reference_desc": "Cab", "qty": "1"}]}),
            ("cmr", {"consignee_name": "Müller & Söhne GmbH", "consignee_address": "Hauptstraße 5, Köln"}),
            ("invoice_br", {"consignee_name": "ABB Brasil Ltda", "items": [{"po_no": "4700201619"}]})], start=1):
        con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
                    " VALUES (?, ?, 2026, ?, ?, ?, ?, 1)",
                    (doc_type, "%04d/2026" % (500 + n), 500 + n, "E2E-BK-%d" % n, "e2e-bk-%d" % n,
                     json.dumps(data, ensure_ascii=False)))
    con.commit()
    con.close()
    return abb, entry


def test_global_search_keyboard_dropdown_and_page(live_server, db_module, e2e_context_factory):
    _seed(db_module)
    page = e2e_context_factory(viewport={"width": 1366, "height": 768}).new_page()
    e2e_login(page, live_server)
    page.keyboard.press("Control+k")
    assert page.evaluate("document.activeElement.id") == "gs-input"
    page.keyboard.type("muller koln")
    page.wait_for_selector("#gs-results:not([hidden]) .gs-item")
    groups = page.locator("#gs-results .gs-group").all_inner_texts()
    assert [g.strip().lower() for g in groups] == ["документи", "адресна книга"]
    first = page.locator("#gs-results .gs-item").first
    assert "Намерено в:" in first.inner_text() and "получател" in first.inner_text()
    assert page.get_attribute("#gs-input", "aria-expanded") == "true"
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    target = page.locator("#gs-results .is-active").get_attribute("href")
    assert page.get_attribute("#gs-input", "aria-activedescendant") == "gs-opt-1"
    page.keyboard.press("Enter")
    page.wait_for_url(live_server + target)
    # „/“ извън поле, Esc затваря, Enter без избор → /search
    page.locator("h1").first.click()
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "gs-input"
    page.fill("#gs-input", "")
    page.keyboard.type("4700201619")
    page.wait_for_selector("#gs-results:not([hidden]) .gs-item")
    page.keyboard.press("Escape")
    assert page.is_hidden("#gs-results")
    page.keyboard.press("Enter")
    page.wait_for_url(live_server + "/search?q=4700201619")
    assert page.locator(".gs-page-group").count() == 2  # документи + фактури
    assert "PO / поръчка" in page.locator(".gs-chip").first.inner_text()


def test_slash_does_not_steal_typing_in_fields(live_server, db_module, page):
    e2e_login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.click("#f-consignee_name")
    page.keyboard.type("A/B")
    assert page.input_value("#f-consignee_name") == "A/B"
    assert page.evaluate("document.activeElement.id") == "f-consignee_name"


def test_global_search_on_phone(live_server, db_module, e2e_context_factory):
    _seed(db_module)
    page = e2e_context_factory(viewport={"width": 390, "height": 844}, is_mobile=True,
                               has_touch=True).new_page()
    e2e_login(page, live_server)
    page.click("#gs-mobile-btn")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'gs-input'")
    assert "open" in page.get_attribute("#sidebar", "class")
    page.keyboard.type("muller")
    page.wait_for_selector("#gs-results:not([hidden]) .gs-item")
    left, right = page.eval_on_selector("#gs-results", "e => [e.getBoundingClientRect().left,"
                                                         " e.getBoundingClientRect().right]")
    assert left >= 0 and right <= 390
    page.goto(live_server + "/search?q=muller")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_invoice_warns_when_shipment_is_another_client(live_server, db_module, e2e_context_factory):
    _abb, entry = _seed(db_module)
    page = e2e_context_factory(viewport={"width": 1366, "height": 900}).new_page()
    e2e_login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    page.select_option(".invoice-client-select", str(entry))
    assert page.is_hidden(".book-mismatch")
    page.fill("#f-pull-invoice-code", "0501/2026")
    page.click(".invoice-pull-btn")
    page.wait_for_selector(".book-mismatch:not([hidden])")
    text = page.inner_text(".book-mismatch")
    assert "фактурата е за друг клиент от този на пратката" in text
    assert "Müller & Söhne GmbH" in text and "ABB Brasil Ltda" in text
    page.select_option(".invoice-client-select", "")
    page.wait_for_selector(".book-mismatch[hidden]", state="attached")


def test_create_invoice_entry_from_client_page(live_server, db_module, page):
    abb, _entry = _seed(db_module)
    e2e_login(page, live_server)
    page.goto(live_server + "/clients/%d/edit" % abb)
    assert "ABB BR" in page.inner_text("#book-links")
    page.click("#book-links button:has-text('Създай запис за фактури от този клиент')")
    page.wait_for_url(live_server + "/invoices/clients/*/edit")
    assert page.input_value("#f-billing_name") == "ABB Brasil Ltda"
    assert "Свързан с клиента:" in page.inner_text("#book-links")
