# -*- coding: utf-8 -*-
"""v3.83.0, група CUSTOMS — в истински браузър (pytest -m e2e): калкулатор
LDM (ЧМР, опаковъчен лист, отделната страница), CN/TARIC проверка в редовете
на фактура, проверката за Норвегия на изгледа, ЧМР с MRN/EORI остава 1 лист."""
import io
import re

import pytest

from conftest import e2e_login
from test_print_layout import _doc

pytestmark = pytest.mark.e2e


def test_ldm_calculator_page_formulas_and_manual_override(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/customs/ldm")
    page.fill('[data-ldm="qty"]', "33")
    page.fill("#f-ldm_gross", "5000")
    assert page.input_value("#f-ldm") == "13,20"
    assert page.input_value("#f-pallet_places") == "33"
    assert page.input_value("#f-chargeable_weight") == "24420"
    assert page.is_visible('label[for="f-ldm"] .packing-auto-badge')
    page.select_option("#f-ldm_factor", "1750")
    assert page.input_value("#f-chargeable_weight") == "23100"
    page.select_option('[data-ldm="stack"]', "2")
    assert page.input_value("#f-ldm") == "6,80"  # ⌈33/2⌉ = 17 основи × 0,4
    # индустриален палет — фиксирани размери 1200×1000
    page.click("[data-ldm-add]")
    row = page.locator(".ldm-lines tbody tr").nth(1)
    row.locator('[data-ldm="type"]').select_option("ind")
    row.locator('[data-ldm="qty"]').fill("2")
    assert row.locator('[data-ldm="width"]').input_value() == "1000"
    assert page.input_value("#f-ldm") == "7,80"
    # ръчно въведеното не се презаписва
    page.fill("#f-pallet_places", "30")
    row.locator('[data-ldm="qty"]').fill("4")
    assert page.input_value("#f-pallet_places") == "30"
    assert page.input_value("#f-ldm") == "8,80"
    assert "%" in page.text_content("[data-ldm-summary]")


def test_cmr_ldm_saved_and_restored_on_edit(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill("#f-consignee_name", "Клиент")
    page.fill("#f-weight", "3000")
    page.fill('[data-ldm="qty"]', "10")
    page.fill("#f-mrn", "26bg 0020 0400 1234 e5")
    page.locator("#f-carrier").click()
    assert page.input_value("#f-mrn") == "26BG002004001234E5"
    assert page.input_value("#f-chargeable_weight") == "7400"
    page.click("button[type=submit]:has-text('Издай ЧМР')")
    page.wait_for_url(re.compile(r".*/doc/\d+$"))
    doc_url = page.url
    assert "LDM: " in page.text_content("#customs-panel")
    page.goto(doc_url + "/edit")
    assert page.locator(".ldm-lines tbody tr").count() == 1
    assert page.locator('[data-ldm="qty"]').input_value() == "10"
    assert page.input_value("#f-ldm") == "4,00"
    assert page.is_visible('label[for="f-ldm"] .packing-auto-badge')


def test_packing_ldm_from_row_dimensions(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/packing/new")
    row = page.locator("#packing-items tbody tr").first
    row.locator('[data-field="length"]').fill("1200")
    row.locator('[data-field="width"]').fill("800")
    row.locator('[data-field="qty"]').fill("5")
    assert page.input_value("#f-ldm") == "0,40"
    page.select_option("#f-ldm_row_mode", "qty")
    assert page.input_value("#f-ldm") == "2,00"
    page.select_option("#f-ldm_stack", "2")
    assert page.input_value("#f-ldm") == "1,20"  # ⌈5/2⌉ = 3 основи
    page.click('[data-add-row="packing-items"]')
    row2 = page.locator("#packing-items tbody tr").nth(1)
    row2.locator('[data-field="length"]').fill("1200")
    row2.locator('[data-field="width"]').fill("1000")
    row2.locator('[data-field="qty"]').fill("1")
    assert page.input_value("#f-ldm") == "1,70"


def test_invoice_hs_code_validation_and_taric_link(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/invoice-no/new")
    cell = page.locator('#invoice-no-items [data-field="hs_code"]').first
    assert cell.input_value() == "85389099"
    cell.fill("8538.90")
    page.locator('#invoice-no-items [data-field="description"]').first.click()
    assert "cn-invalid" in cell.get_attribute("class")
    assert cell.get_attribute("aria-invalid") == "true"
    assert "Редове с невалиден CN код" in page.text_content(".cn-taric-hint")
    cell.fill("8538 90 99 00")
    page.locator('#invoice-no-items [data-field="description"]').first.click()
    assert cell.input_value() == "8538909900"
    assert "cn-invalid" not in cell.get_attribute("class")
    link = page.locator(".cn-taric-hint a.taric-link")
    assert "Taric=8538909900" in link.get_attribute("href")
    assert link.get_attribute("target") == "_blank"


def test_material_cn_code_fills_invoice_row(page, live_server, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO materials (code, description, net_weight, cn_code)"
                " VALUES ('MAT-9', 'Шкаф', '1', '85371091')")
    con.commit()
    con.close()
    e2e_login(page, live_server)
    page.goto(live_server + "/invoice-dubai/new")
    row = page.locator("#invoice-dubai-items tbody tr").first
    row.locator('[data-field="material_code"]').fill("MAT-9")
    row.locator('[data-field="qty"]').click()
    page.wait_for_function("() => document.querySelector('#invoice-dubai-items [data-field=hs_code]').value === '85371091'")


def test_norway_checklist_on_view_and_print_page(page, live_server, db_module):
    doc_id = _doc(db_module, "cmr", {"consignee_name": "ABB AS", "consignee_country": "Norway",
                                     "weight": "1000", "packages": "2", "stat_no": "85389099"})
    e2e_login(page, live_server)
    page.goto(live_server + "/doc/%d" % doc_id)
    panel = page.locator("#customs-panel")
    assert "Норвегия (Digitoll): липсват" in panel.text_content()
    assert panel.locator("li.bad").count() >= 4 and panel.locator("li.ok").count() >= 2
    # панелът не се печата
    page.emulate_media(media="print")
    assert not panel.is_visible()
    page.emulate_media(media="screen")
    page.click("a:has-text('Проверка преди товарене (печат)')")
    page.wait_for_url(re.compile(r".*/customs-check$"))
    assert page.locator(".customs-check-table tr.bad").count() >= 4
    bg = page.evaluate("getComputedStyle(document.querySelector('.customs-check-table tr.bad td')).backgroundColor")
    assert bg != "rgba(0, 0, 0, 0)"


def test_cmr_with_customs_data_still_prints_on_one_page(page, live_server, db_module):
    pypdf = pytest.importorskip("pypdf")
    long = "Müller-Lüdenscheidt Straßenbau GmbH & Co. KG Zweigniederlassung Großröhrsdorf"
    data = {k: "Стойност — " + long for k in (
        "sender_name", "sender_address", "consignee_name", "consignee_address", "place_delivery",
        "place_loading", "attached_docs", "carrier")}
    data.update(sender_instructions="Да се пази от влага. Keep dry. " * 4,
                goods="\n".join("Ред %d HV Switchgear" % i for i in range(1, 9)),
                mrn="26BG002004001234E5", no_declaration_ref="NO-REF-2026-000123",
                sender_eori="BG123456789", consignee_eori="NO987654321",
                ldm="13,20", pallet_places="33", chargeable_weight="24420")
    doc_id = _doc(db_module, "cmr", data)
    e2e_login(page, live_server)
    page.goto(live_server + "/doc/%d" % doc_id)
    page.wait_for_load_state("load")
    page.emulate_media(media="print")
    pdf = page.pdf(print_background=True, prefer_css_page_size=True)
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text() or ""
    assert "26BG002004001234E5" in text and "Митнически данни" not in text
