# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, FIN): износът към счетоводството, е-фактурата и
„Отвори като имейл“ в истински браузър (Chromium): менюто „За
счетоводството“ в „Издадени фактури“ сваля файловете на филтрирания списък,
бутонът „Е-фактура (XML)“ — XML, а „Отвори като имейл“ — .eml чернова.
Снимки — в FIN_SHOTS_DIR, ако е зададена."""
import csv
import email
import email.policy
import io
import os
import xml.etree.ElementTree as ET  # nosec B405 -- собственият изход

import pytest

from conftest import e2e_login
from test_fin_v383 import SELLER

pytestmark = pytest.mark.e2e


def _shot(page, name):
    folder = os.environ.get("FIN_SHOTS_DIR")
    if folder:
        os.makedirs(folder, exist_ok=True)
        page.screenshot(path=os.path.join(folder, name), full_page=False)


def _seed(db_module):
    import json
    con = db_module.get_db()
    db_module.save_settings(con, SELLER)
    con.execute("INSERT INTO clients (name, email) VALUES (?, ?)",
                ("ABB ELETRIFICACAO LTDA", "compras@abb-br.example"))
    for num, doc_type, cons, addr, created in (
            ("BR-0001", "invoice_br", "ABB ELETRIFICACAO LTDA", "Sorocaba - SP\nBrasil",
             "2026-11-02 10:00:00"),
            ("NO-0001", "invoice_no", "ABB AS", "Billingstad\nNorway", "2026-10-01 10:00:00")):
        data = {"invoice_number": num, "doc_date": created[:10], "consignee_name": cons,
                "consignee_address": addr, "currency": "EURO", "terms_payment": "Net in 90 Days",
                "items": [{"hs_code": "85389099", "po_no": "4700200362", "pos": "30",
                           "material_code": "GLBK400002P0012", "net_weight": "2.21",
                           "qty": "20", "unit_price": "13.66"}]}
        con.execute("INSERT INTO documents (doc_type, number, seq, year, barcode, client_name, data,"
                    " created_at) VALUES (?, ?, 0, 2026, ?, ?, ?, ?)",
                    (doc_type, num, "FIN-" + num, cons, json.dumps(data), created))
    con.commit()
    doc_id = con.execute("SELECT id FROM documents WHERE number = 'BR-0001'").fetchone()[0]
    con.close()
    return doc_id


def test_invoices_accounting_menu_downloads_filtered_csv(page, live_server, db_module):
    _seed(db_module)
    e2e_login(page, live_server)
    page.goto(live_server + "/invoices?type=invoice_br")
    page.click(".fin-export > summary")
    assert page.locator(".fin-export-menu").is_visible()
    _shot(page, "fin_invoices_menu.png")
    with page.expect_download() as dl:
        page.click('[data-fin-export="csv"]')
    path = dl.value.path()
    rows = list(csv.reader(io.StringIO(open(path, encoding="utf-8-sig").read()), delimiter=";"))
    assert dl.value.suggested_filename.endswith(".csv")
    assert [r[1] for r in rows[1:]] == ["BR-0001"]       # само филтрираният вид
    assert "ДДС № на купувача" in rows[0]
    with page.expect_download() as dl2:
        page.click('[data-fin-export="einvoice"]')
    assert dl2.value.suggested_filename.endswith(".zip")


def test_invoice_view_einvoice_and_email_draft(page, live_server, db_module):
    doc_id = _seed(db_module)
    e2e_login(page, live_server)
    page.goto(live_server + "/doc/%d" % doc_id)
    assert page.locator("[data-fin-einvoice]").is_visible()
    _shot(page, "fin_invoice_toolbar.png")
    with page.expect_download() as dl:
        page.click("[data-fin-einvoice]")
    root = ET.parse(dl.value.path()).getroot()
    assert root.tag.endswith("Invoice")
    assert "weight" not in open(dl.value.path(), encoding="utf-8").read().lower()
    page.click("[data-fin-email]")
    page.wait_for_url("**/email/compose**")
    assert page.input_value("#f-mail-to") == "compras@abb-br.example"
    assert "Invoice No. BR-0001" in page.input_value("#f-mail-subject")
    assert page.locator("[data-fin-send]").count() == 0   # без SMTP — само .eml
    _shot(page, "fin_email_compose.png")
    page.click('[data-fin-lang="bg"]')
    page.wait_for_url("**lang=bg**")
    assert page.input_value("#f-mail-subject").startswith("Фактура № BR-0001")
    with page.expect_download() as dl3:
        page.click("[data-fin-eml]")
    msg = email.message_from_bytes(open(dl3.value.path(), "rb").read(), policy=email.policy.default)
    assert msg["X-Unsent"] == "1" and msg["To"] == "compras@abb-br.example"
    assert [a.get_content_type() for a in msg.iter_attachments()] == ["application/pdf"]


def test_email_compose_on_phone_width(page, live_server, db_module):
    doc_id = _seed(db_module)
    e2e_login(page, live_server)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server + "/email/compose?ids=%d" % doc_id)
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 392, width
    _shot(page, "fin_email_compose_phone.png")


@pytest.mark.parametrize("width", [1366, 390])
def test_invoices_export_menu_does_not_overflow(page, live_server, db_module, width):
    _seed(db_module)
    e2e_login(page, live_server)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server + "/invoices")
    page.click(".fin-export > summary")
    menu = page.locator(".fin-export-menu").bounding_box()
    sizes = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sizes[0] <= sizes[1], sizes
    assert menu["x"] >= 0 and menu["x"] + menu["width"] <= width, menu
    _shot(page, "fin_invoices_menu_%d.png" % width)
