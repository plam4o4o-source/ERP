# -*- coding: utf-8 -*-
"""Одит (26.09.2026) — регресионни тестове с истински браузър (Playwright)
за клиентските находки №1–№7: предпазителят срещу Enter във всички форми,
които издават документи, клавиатурният достъп до полето за Excel файл,
изтритият HS code, автоматично попълненото тегло/описание при поправен код
на материал, повторното изтегляне на PDF с Enter, хоризонталното
плъзгане на печатните изгледи на телефон и старата стойност на „Terms of
Delivery“ при редакция.

Сървърът, браузърът и входът са общите от conftest.py (live_server, page,
e2e_login). Пускат се изрично: `python3 -m pytest -m e2e
tests/test_e2e_2026_09_26.py`."""
import json
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("playwright.sync_api")

from conftest import e2e_login as _login  # noqa: E402

_ISSUE_BTN = '#main-doc-form button[type="submit"]:not([formaction])'
_PREVIEW_BTN = "#main-doc-form button[formaction]"
_ORDERS_HEADERS = ["Due Date", "Order No", "Pos", "Project", "Reference",
                   "Reference Desc", "Open Qty", "Unit", "Stock", ""]


def _doc_count(db_module):
    con = db_module.get_db()
    try:
        return con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        con.close()


def _doc_data(db_module, doc_id):
    con = db_module.get_db()
    try:
        return json.loads(con.execute("SELECT data FROM documents WHERE id = ?",
                                      (doc_id,)).fetchone()[0])
    finally:
        con.close()


def _three_pallet_xlsx(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(_ORDERS_HEADERS)
    for n in (1, 2, 3):
        ws.append(["2026-09-01", "ORD-1", str(n * 10), "P", "REF-%d" % n,
                   "Описание %d" % n, n * 2, "PCS", "WH", n])
    path = tmp_path / "tri_paleta.xlsx"
    wb.save(str(path))
    return str(path)


def _open_bulk_review(page, live_server, tmp_path):
    _login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill("#f-client_name", "Е2Е Импорт Клиент")
    page.set_input_files("#f-excel_file-1", _three_pallet_xlsx(tmp_path))
    with page.expect_navigation():
        page.click('button:has-text("Зареди и раздели по палети")')
    assert page.locator("#bulk-form table.items").count() == 3


# ---------------------------------------------------------------- находка №1

def test_enter_in_bulk_review_cell_does_not_issue_any_pallet_card(page, live_server, db_module, tmp_path):
    """Enter в клетка „Кол.“ на карта 2 издаваше и трите палетни карти."""
    _open_bulk_review(page, live_server, tmp_path)
    url_before = page.url
    cell = page.locator('#pallet-items-2 tbody input[data-field="qty"]').first
    cell.click()
    cell.fill("3")
    page.keyboard.press("Enter")
    page.locator("#f-ref_cmr").click()
    page.keyboard.type("CMR-1")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1200)
    assert page.url == url_before
    assert _doc_count(db_module) == 0

    # Бутонът за издаване си работи от клавиатурата (Enter върху самия бутон).
    page.locator('#bulk-form button[type="submit"]:not([formaction])').focus()
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert _doc_count(db_module) == 3


def test_enter_in_bulk_review_restored_from_preview_does_not_issue(page, live_server, db_module, tmp_path):
    """Същото след „Предварителен преглед“ → „Назад към формата“."""
    _open_bulk_review(page, live_server, tmp_path)
    with page.expect_navigation():
        page.click('#bulk-form button[formaction]')
    with page.expect_navigation():
        page.click("text=Назад към формата")
    assert page.locator("#bulk-form").count() == 1
    url_before = page.url
    page.locator('input[name="gross_1"]').click()
    page.keyboard.type("5")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1200)
    assert page.url == url_before
    assert _doc_count(db_module) == 0


# ---------------------------------------------------------------- находка №2

def test_enter_on_invoice_excel_file_input_opens_the_file_chooser(page, live_server, db_module):
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    page.fill("#f-consignee_name", "Е2Е Клиент")
    page.fill("#f-invoice_number", "E2E-1")
    file_input = page.locator("#main-doc-form input.invoice-excel-file").first
    file_input.focus()
    with page.expect_file_chooser(timeout=3000):
        page.keyboard.press("Enter")
    # Предпазителят за текстовите полета си остава.
    page.locator("#f-consignee_name").press("Enter")
    page.wait_for_timeout(800)
    assert page.url.endswith("/invoice-br/new")
    assert _doc_count(db_module) == 0


# ---------------------------------------------------------------- находка №3

@pytest.mark.parametrize("path,table_id", [
    ("/invoice-br/new", "invoice-br-items"),
    ("/invoice-no/new", "invoice-no-items"),
    ("/invoice-dubai/new", "invoice-dubai-items"),
])
def test_cleared_hs_code_is_not_refilled_after_preview_or_edit(page, live_server, db_module, path, table_id):
    _login(page, live_server)
    page.goto(live_server + path)
    page.evaluate("() => document.querySelectorAll('#main-doc-form [required]')"
                  ".forEach(e => { if (!e.value) e.value = 'X'; })")
    row = page.locator("#%s tbody tr" % table_id).first
    assert row.locator('input[data-field="hs_code"]').input_value() == "85389099"
    row.locator('input[data-field="hs_code"]').fill("")
    row.locator('input[data-field="material_code"]').fill("FREIGHT")
    row.locator('input[data-field="qty"]').fill("1")
    row.locator('input[data-field="unit_price"]').fill("100")

    with page.expect_navigation():
        page.click(_PREVIEW_BTN)
    with page.expect_navigation():
        page.click("text=Назад към формата")
    row = page.locator("#%s tbody tr" % table_id).first
    assert row.locator('input[data-field="hs_code"]').input_value() == ""

    with page.expect_navigation():
        page.click(_ISSUE_BTN)
    doc_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    assert _doc_data(db_module, doc_id)["items"][0]["hs_code"] == ""

    page.goto(live_server + "/doc/%d/edit" % doc_id)
    row = page.locator("#%s tbody tr" % table_id).first
    assert row.locator('input[data-field="hs_code"]').input_value() == ""
    # Нов ред („+ Добави ред“) все така получава подразбиращия се код.
    page.click('[data-add-row="%s"]' % table_id)
    new_row = page.locator("#%s tbody tr" % table_id).last
    assert new_row.locator('input[data-field="hs_code"]').input_value() == "85389099"


# ---------------------------------------------------------------- находка №4

def _load_catalog(page, live_server, tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["ABB part ID", "Description", "Net weight\n[KG/pc]"])
    ws.append(["MAT-1180", "Профил 1180", 2.5])
    ws.append(["MAT-1108", "Профил 1108", 9.75])
    path = tmp_path / "katalog.xlsx"
    wb.save(str(path))
    page.goto(live_server + "/materials")
    page.set_input_files('input[name="excel_file"]', str(path))
    page.click('button:has-text("Зареди справочника")')
    page.wait_for_load_state("networkidle")


def _set_code(page, row, code):
    cell = row.locator('input[data-field="material_code"]')
    cell.fill(code)
    cell.blur()  # попълването тръгва при change


@pytest.mark.parametrize("path,table_id,field,first,second", [
    ("/invoice-br/new", "invoice-br-items", "net_weight", "2.5", "9.75"),
    ("/invoice-no/new", "invoice-no-items", "description", "Профил 1180", "Профил 1108"),
])
def test_correcting_material_code_replaces_autofilled_value_but_keeps_manual(
        page, live_server, tmp_path, path, table_id, field, first, second):
    _login(page, live_server)
    _load_catalog(page, live_server, tmp_path)
    page.goto(live_server + path)
    row = page.locator("#%s tbody tr" % table_id).first
    target = row.locator('input[data-field="%s"]' % field)
    sel = '#%s tbody tr input[data-field="%s"]' % (table_id, field)

    _set_code(page, row, "MAT-1180")
    page.wait_for_function("(v) => document.querySelector(%r).value === v" % sel, arg=first, timeout=5000)
    _set_code(page, row, "MAT-1108")   # поправена печатна грешка
    page.wait_for_function("(v) => document.querySelector(%r).value === v" % sel, arg=second, timeout=5000)
    assert target.input_value() == second

    # Ръчно въведеното печели и при следваща смяна на кода.
    target.fill("7")
    _set_code(page, row, "MAT-1180")
    page.wait_for_timeout(1000)
    assert target.input_value() == "7"


# ---------------------------------------------------------------- находка №5

def test_pdf_link_enter_while_busy_does_not_download_again(page, live_server, monkeypatch):
    import pdf_export
    original = pdf_export.generate_document_pdf

    # Изтеглянето „виси“, докато тестът не натисне Enter (като голям
    # документ). Със събитие вместо фиксирана пауза: под натоварване PDF-ът
    # понякога завършваше преди натисканията и второто изтегляне беше
    # легитимно — нестабилен тест.
    release = threading.Event()

    def slow_generate(*args, **kwargs):
        release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(pdf_export, "generate_document_pdf", slow_generate)

    context = page.context.browser.new_context(accept_downloads=True)
    try:
        pg = context.new_page()
        _login(pg, live_server)
        pg.goto(live_server + "/cmr/new")
        pg.fill('input[name="sender_name"]', "Изпращач ЕООД")
        pg.fill('input[name="consignee_name"]', "PDF Е2Е Клиент")
        with pg.expect_navigation():
            pg.click(_ISSUE_BTN)
        requests = []
        pg.on("request", lambda r: requests.append(r.url) if "export.pdf" in r.url else None)
        link = pg.locator("a[data-pdf-export]")
        link.click()
        assert "btn-busy" in (link.get_attribute("class") or "")
        link.focus()
        pg.keyboard.press("Enter")
        pg.keyboard.press("Enter")
        release.set()
        pg.wait_for_function(
            "() => !document.querySelector('a[data-pdf-export]').classList.contains('btn-busy')",
            timeout=20000)
        pg.wait_for_timeout(1500)
        assert len(requests) == 1, requests
        assert "pacho_pdf_ready" not in pg.evaluate("document.cookie")
    finally:
        context.close()


# ---------------------------------------------------------------- находка №6

_PUBLIC_TYPES = [
    ("/cmr/new", {"sender_name": "Изпращач", "consignee_name": "Телефон Клиент"}),
    ("/waybill/new", {"consignee_name": "Телефон Клиент"}),
    ("/packing/new", {"receiver_name": "Телефон Клиент"}),
    ("/pallet/new", {"client_name": "Телефон Клиент"}),
    ("/dualuse/new", {"invoice_numbers": "INV-1", "destination_country": "Турция",
                      "declarant_name": "Иван Петров"}),
    ("/export-it/new", {"invoice_no": "INV-1"}),
]

_OVERFLOW_JS = ("() => ({sw: document.documentElement.scrollWidth,"
                " cw: document.documentElement.clientWidth})")


def test_print_views_do_not_scroll_sideways_on_a_phone(page, live_server, db_module):
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    token = page.locator('#main-doc-form input[name="csrf_token"]').input_value()
    for path, fields in _PUBLIC_TYPES:
        resp = page.request.post(live_server + path, form=dict(fields, csrf_token=token))
        assert resp.ok, path
    con = db_module.get_db()
    rows = con.execute("SELECT id, doc_type, public_token FROM documents ORDER BY id").fetchall()
    con.close()
    assert len(rows) == len(_PUBLIC_TYPES)

    phone = dict(viewport={"width": 375, "height": 740}, is_mobile=True, has_touch=True)
    logged = page.context.browser.new_context(**phone)
    anon = page.context.browser.new_context(**phone)
    try:
        lp = logged.new_page()
        _login(lp, live_server)
        ap = anon.new_page()
        for r in rows:
            for pg, url in ((lp, "/doc/%d" % r["id"]), (ap, "/p/" + r["public_token"])):
                pg.goto(live_server + url)
                m = pg.evaluate(_OVERFLOW_JS)
                assert m["sw"] <= m["cw"] + 1, (r["doc_type"], url, m)
    finally:
        logged.close()
        anon.close()


# ---------------------------------------------------------------- находка №7

def test_editing_invoice_keeps_a_legacy_terms_of_delivery_value(page, live_server, db_module):
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    page.fill("#f-consignee_name", "Стара Фактура ЕООД")
    page.fill("#f-invoice_number", "OLD-1")
    # Одит (01.10.2026, U5): фактура без ред със стока вече не се издава.
    page.fill('table.invoice-items tbody tr:first-child input[data-field="material_code"]', "E2E-MAT")
    with page.expect_navigation():
        page.click(_ISSUE_BTN)
    doc_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    # Фактура отпреди менюто FCA/DAP (v3.42.0) — свободен текст.
    data = _doc_data(db_module, doc_id)
    data["terms_delivery"] = "EXW"
    con = db_module.get_db()
    con.execute("UPDATE documents SET data = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), doc_id))
    con.commit()
    con.close()

    page.goto(live_server + "/doc/%d/edit" % doc_id)
    assert page.locator("#f-terms_delivery").input_value() == "EXW"
    with page.expect_navigation():
        page.click(_PREVIEW_BTN)
    assert "Terms of Delivery: EXW" in page.locator("body").inner_text()
    with page.expect_navigation():
        page.click("text=Назад към формата")
    assert page.locator("#f-terms_delivery").input_value() == "EXW"
    with page.expect_navigation():
        page.click(_ISSUE_BTN)
    assert _doc_data(db_module, doc_id)["terms_delivery"] == "EXW"
