# -*- coding: utf-8 -*-
"""Заявка (16.11.2026): „Като се зареди файл от Excel във фактури да остава
зареден и само да се избира съответната поръчка, не като сега всеки път да
се зарежда файла“ (invoice_import_cache.py, routes_invoices.invoice_import_items)."""
import io
import json

import pytest
from openpyxl import Workbook

from conftest import post_with_csrf

HEADERS = ("Order No", "Pos", "Reference", "Reference Desc", "Open Qty", "")


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(list(HEADERS))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


TWO_POS = [("4700201619", "10", "GLBK400002P0012", "C-Profile", 20, 1),
           ("4700201619", "20", "GLBK400001P0200", "C-Profile 2", 40, 1),
           ("4700223566", "10", "GLBK400003P0001", "Busbar", 5, 1)]


def _upload(client, rows=TWO_POS, name="poruchki.xlsx", **extra):
    data = {"excel_file": (_xlsx(rows), name)}
    data.update(extra)
    return post_with_csrf(client, "/invoice/import-items", data, csrf_source_url="/invoice-br/new",
                          content_type="multipart/form-data").get_json()


def _from_cache(client, po=None):
    data = {"use_cache": "1"}
    if po is not None:
        data["po_no"] = po
    return post_with_csrf(client, "/invoice/import-items", data, csrf_source_url="/invoice-br/new").get_json()


def test_upload_is_remembered_and_lists_the_orders(admin_client, db_module):
    payload = _upload(admin_client)
    assert payload["ok"] and payload["choose_po"]
    cache = payload["cache"]
    assert cache["filename"] == "poruchki.xlsx" and cache["rows"] == 3
    assert [(p["po_no"], p["count"], p["invoiced"]) for p in cache["pos"]] == [
        ("4700201619", 2, []), ("4700223566", 1, [])]
    row = db_module.get_db().execute("SELECT filename FROM invoice_import_cache").fetchone()
    assert row["filename"] == "poruchki.xlsx"


def test_next_order_loads_without_uploading_the_file_again(admin_client):
    _upload(admin_client)
    first = _from_cache(admin_client, "4700201619")
    assert first["ok"] and first["count"] == 2 and first["loaded_po"] == "4700201619"
    assert [r["pos"] for r in first["rows"]] == ["10", "20"]
    second = _from_cache(admin_client, "4700223566")
    assert second["count"] == 1 and second["rows"][0]["material_code"] == "GLBK400003P0001"
    assert [p["po_no"] for p in second["remaining"]] == ["4700201619"]


def test_catalog_weights_are_looked_up_at_load_time(admin_client):
    from test_invoices import _load_catalog
    _upload(admin_client)
    _load_catalog(admin_client)  # справочникът е зареден СЛЕД качването на файла
    rows = _from_cache(admin_client, "4700201619")["rows"]
    assert rows[0]["net_weight"] != ""


def test_without_a_remembered_file_the_user_is_asked_for_one(admin_client):
    payload = _from_cache(admin_client, "4700201619")
    assert payload["ok"] is False and "Няма запомнен" in payload["error"]


def test_remembered_file_is_per_user(admin_client, employee_client):
    _upload(admin_client)
    assert _from_cache(employee_client, "4700201619")["ok"] is False
    page = employee_client.get("/invoice-br/new").get_data(as_text=True)
    assert "poruchki.xlsx" not in page


def test_form_shows_the_remembered_file_after_reopening(admin_client):
    page = admin_client.get("/invoice-br/new").get_data(as_text=True)
    assert 'class="invoice-excel-cached" hidden' in page
    _upload(admin_client)
    for url in ("/invoice-br/new", "/invoice-no/new", "/invoice-dubai/new"):
        page = admin_client.get(url).get_data(as_text=True)
        assert 'class="invoice-excel-cached" hidden' not in page
        assert "poruchki.xlsx" in page and "Зареди поръчката" in page and "Друг Excel файл" in page


def test_orders_with_an_issued_invoice_are_marked(admin_client, db_module):
    _upload(admin_client)
    con = db_module.get_db()
    con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
                " VALUES ('invoice_br', '0000000042', 2026, 42, 'INVBR-TEST-42', ?)",
                (json.dumps({"items": [{"po_no": "4700223566", "qty": "5"}]}),))
    # Само споменат номер (не като P.O NO на ред) — не се брои.
    con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
                " VALUES ('invoice_no', '0000000043', 2026, 43, 'INVNO-TEST-43', ?)",
                (json.dumps({"notes": "4700201619", "items": []}),))
    con.commit()
    pos = {p["po_no"]: p["invoiced"] for p in _from_cache(admin_client)["cache"]["pos"]}
    assert pos == {"4700201619": [], "4700223566": ["0000000042"]}


def test_single_order_file_loads_directly_and_is_remembered(admin_client):
    payload = _upload(admin_client, rows=TWO_POS[:2], name="edna.xlsx")
    assert payload["count"] == 2 and payload["cache"]["filename"] == "edna.xlsx"
    again = _from_cache(admin_client)
    assert again["count"] == 2


def test_new_upload_replaces_and_forget_clears(admin_client, db_module):
    _upload(admin_client)
    _upload(admin_client, rows=TWO_POS[2:], name="druga.xlsx")
    assert _from_cache(admin_client)["filename"] == "druga.xlsx"
    resp = post_with_csrf(admin_client, "/invoice/import-cache/clear", {}, csrf_source_url="/invoice-br/new")
    assert resp.get_json() == {"ok": True}
    assert db_module.get_db().execute("SELECT COUNT(*) FROM invoice_import_cache").fetchone()[0] == 0
    assert _from_cache(admin_client)["ok"] is False


def test_clear_requires_login(client):
    assert client.post("/invoice/import-cache/clear").status_code in (302, 400)


@pytest.mark.e2e
def test_choose_order_from_the_remembered_file_in_the_browser(page, live_server, tmp_path):
    pytest.importorskip("playwright.sync_api")
    from conftest import e2e_login
    path = tmp_path / "poruchki.xlsx"
    path.write_bytes(_xlsx(TWO_POS).getvalue())
    e2e_login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    assert not page.is_visible(".invoice-excel-cached")
    page.set_input_files(".invoice-excel-file", str(path))
    page.click(".invoice-excel-btn")
    page.wait_for_selector(".invoice-excel-cached:not([hidden])")
    assert "poruchki.xlsx" in page.inner_text(".invoice-excel-cached")
    assert page.locator(".invoice-excel-po option").count() == 2
    page.select_option(".invoice-excel-po", "4700201619")
    page.click(".invoice-excel-load-po")
    page.wait_for_function("document.querySelectorAll('#invoice-br-items tbody tr').length >= 2")
    # Нова фактура: файлът е там — само се избира поръчката.
    page.goto(live_server + "/invoice-br/new")
    page.wait_for_selector(".invoice-excel-cached:not([hidden])")
    page.select_option(".invoice-excel-po", "4700223566")
    page.click(".invoice-excel-load-po")
    page.wait_for_function("document.querySelector('.invoice-excel-msg').textContent.includes('4700223566')")
    assert page.evaluate("document.documentElement.scrollWidth") <= page.viewport_size["width"]
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
