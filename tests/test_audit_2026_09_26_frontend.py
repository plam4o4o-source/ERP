# -*- coding: utf-8 -*-
"""Одит (26.09.2026) — бързи (без браузър) регресионни проверки за
клиентските находки №1–№7. Истинското поведение в браузъра се проверява в
tests/test_e2e_2026_09_26.py (Playwright, маркер e2e); тук стоят евтините
проверки, които тръгват в основния `pytest`: рендираната обвивка на
печатните изгледи (логнат и публичен по QR) и ключовите места в app.js."""
import os
import re

import pytest

from conftest import post_with_csrf, app_js_source as _app_js

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _function_body(src, name):
    start = src.index("function %s(" % name)
    nxt = src.find("\nfunction ", start + 1)
    return src[start:nxt if nxt != -1 else len(src)]


# ---------------------------------------------------------------- находки №1/№2

def test_enter_guard_covers_the_bulk_review_form():
    src = _app_js()
    m = re.search(r'var DOCUMENT_FORM_SELECTOR = "([^"]+)"', src)
    assert m, "DOCUMENT_FORM_SELECTOR липсва"
    selectors = [s.strip() for s in m.group(1).split(",")]
    assert "#main-doc-form" in selectors and "#bulk-form" in selectors
    with open(os.path.join(ROOT, "templates", "pallet_bulk_review.html"), encoding="utf-8") as f:
        assert 'id="bulk-form"' in f.read()
    # ...и се вика при зареждане на всяка страница.
    assert re.search(r"\n\s+initEnterGuards\(\);", src)


def test_enter_guard_skips_file_input_but_still_blocks_checkbox():
    src = _app_js()
    m = re.search(r"var ENTER_GUARD_SKIP_TYPES = \[([^\]]*)\]", src)
    assert m
    skipped = re.findall(r'"([a-z]+)"', m.group(1))
    assert "file" in skipped
    # В Chromium Enter върху checkbox/radio/range ИЗПРАЩА формата.
    for t in ("checkbox", "radio", "range", "text", "number", "date"):
        assert t not in skipped


# ---------------------------------------------------------------- находка №3

def test_row_defaults_only_apply_to_new_rows():
    body = _function_body(_app_js(), "initItemsTable")
    assert "var isNewRow = !item;" in body
    assert "(isNewRow ? (rowDefaults[col] || \"\") : \"\")" in body


# ---------------------------------------------------------------- находка №4

def test_material_lookup_remembers_the_autofilled_value():
    body = _function_body(_app_js(), "bindInvoiceMaterialLookup")
    assert "target.dataset.autofilledValue = target.value.trim();" in body
    assert "current !== target.dataset.autofilledValue" in body


# ---------------------------------------------------------------- находка №5

def test_pdf_busy_link_prevents_default_and_always_clears_cookie():
    body = _function_body(_app_js(), "initPdfExportBusy")
    assert 'if (link.classList.contains("btn-busy")) { e.preventDefault(); return; }' in body
    done = body[body.index("function done()"):body.index("timer = setInterval")]
    assert 'document.cookie = "pacho_pdf_ready=; Max-Age=0; path=/";' in done


# ---------------------------------------------------------------- находка №6

_PUBLIC_TYPES = [
    ("/cmr/new", {"sender_name": "Изпращач", "consignee_name": "Клиент"}),
    ("/waybill/new", {"consignee_name": "Клиент"}),
    ("/packing/new", {"receiver_name": "Клиент"}),
    ("/pallet/new", {"client_name": "Клиент"}),
    ("/dualuse/new", {"invoice_numbers": "INV-1"}),
    ("/export-it/new", {"invoice_no": "INV-1"}),
]


@pytest.mark.parametrize("path,fields", _PUBLIC_TYPES)
def test_print_views_use_the_scrolling_print_container(admin_client, client, db_module, path, fields):
    resp = post_with_csrf(admin_client, path, fields, csrf_source_url=path, follow_redirects=False)
    assert resp.status_code == 302, resp.data
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    html = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert '<main class="container-print">' in html

    con = db_module.get_db()
    token = con.execute("SELECT public_token FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    con.close()
    public = client.get("/p/" + token)
    assert public.status_code == 200
    assert '<main class="container-print">' in public.get_data(as_text=True)


# ---------------------------------------------------------------- находка №7

def test_edit_prefill_injects_a_legacy_terms_of_delivery_value():
    body = _function_body(_app_js(), "initDocumentForm")
    assert "form.querySelector('select[name=\"terms_delivery\"]')" in body
    assert "injectAndSelectOption(termsSelect, editData.terms_delivery)" in body
