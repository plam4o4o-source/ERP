# -*- coding: utf-8 -*-
"""Импорт на палетни карти от справка за поръчки → преглед → издаване, и
„Добави от палета“ в опаковъчния лист (одит 01.10.2026: U1, U4, U10)."""
import html
import io
import json
import re
from datetime import date

import pytest
from openpyxl import Workbook

from conftest import post_with_csrf

# Подразбиращите се Настройки на английски — палетните карти са с английски
# изпращач по подразбиране, както единичната палетна карта.
SENDER = "BBS Bulgaria Ltd"
SENDER_CITY = "Yavorets, Bulgaria"


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_ROWS = [["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", "Pallet"],
         ["4700", "10", "A1", "d1", 5, 1],
         ["4700", "20", "A2", "d2", 6, 2]]


def _import(client, rows=_ROWS, **carried):
    data = {"excel_file": (io.BytesIO(_xlsx(rows)), "orders.xlsx")}
    data.update(carried)
    return post_with_csrf(client, "/pallet/bulk-import", data, csrf_source_url="/pallet/new",
                          content_type="multipart/form-data", follow_redirects=False)


def _value(body, name):
    m = re.search(r'name="%s" value="([^"]*)"' % re.escape(name), body)
    assert m, "полето %s липсва" % name
    return html.unescape(m.group(1))


def _review_form(body, groups):
    """Полетата, които браузърът праща от екрана за преглед (items_json_N
    попълва JS-ът — тук директно от групите)."""
    form = {k: _value(body, k) for k in ("sender_name", "sender_city", "client_name",
                                         "doc_date", "groups")}
    form["notes"] = re.search(r'name="notes"[^>]*>([^<]*)<', body).group(1)
    for g, items in groups.items():
        form["items_json_%s" % g] = json.dumps(items, ensure_ascii=False)
        form["items_format_%s" % g] = "orders"
    return form


def _pallets(db_module):
    con = db_module.get_db()
    try:
        return [json.loads(r["data"]) for r in con.execute(
            "SELECT data FROM documents WHERE doc_type = 'pallet' ORDER BY id")]
    finally:
        con.close()


# ---------------------------------------------------------------- U1

def test_import_review_shows_sender_and_issued_cards_keep_it(admin_client, db_module):
    """U1: `shared` (пренесените клиент/дата) беше непразно, а изпращачът
    в него — празен; шаблонът показваше празното вместо Настройките и
    всяка карта от импорта се издаваше БЕЗ изпращач."""
    resp = _import(admin_client, client_name="Клиент Импорт",
                   doc_date=date.today().isoformat())
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert _value(body, "sender_name") == SENDER
    assert _value(body, "sender_city") == SENDER_CITY
    assert _value(body, "client_name") == "Клиент Импорт"

    groups = {"1": [{"order_no": "4700", "pos": "10", "reference": "A1",
                     "reference_desc": "d1", "qty": "5"}],
              "2": [{"order_no": "4700", "pos": "20", "reference": "A2",
                     "reference_desc": "d2", "qty": "6"}]}
    post_with_csrf(admin_client, "/pallet/bulk-issue", _review_form(body, groups),
                   csrf_source_url="/pallet/new", follow_redirects=True)
    cards = _pallets(db_module)
    assert len(cards) == 2
    assert all(c["sender_name"] == SENDER and c["sender_city"] == SENDER_CITY for c in cards)


def test_operator_supplied_sender_is_kept(admin_client):
    body = _import(admin_client, sender_name="Друга фирма", sender_city="Русе",
                   client_name="К").get_data(as_text=True)
    assert _value(body, "sender_name") == "Друга фирма"
    assert _value(body, "sender_city") == "Русе"


def test_empty_sender_on_issue_falls_back_to_settings(admin_client, db_module):
    items = [{"order_no": "1", "pos": "10", "reference": "R", "reference_desc": "x", "qty": "5"}]
    post_with_csrf(admin_client, "/pallet/bulk-issue", {
        "sender_name": "", "sender_city": "", "client_name": "К", "groups": "1",
        "items_format_1": "orders", "items_json_1": json.dumps(items)},
        csrf_source_url="/pallet/new", follow_redirects=True)
    card = _pallets(db_module)[-1]
    assert card["sender_name"] == SENDER and card["sender_city"] == SENDER_CITY


def test_preview_back_restores_sender(admin_client):
    items = [{"order_no": "1", "pos": "10", "reference": "R", "reference_desc": "x", "qty": "5"}]
    resp = post_with_csrf(admin_client, "/pallet/bulk-preview", {
        "sender_name": "", "client_name": "К", "doc_date": "", "groups": "1",
        "items_format_1": "orders", "items_json_1": json.dumps(items)},
        csrf_source_url="/pallet/new", follow_redirects=False)
    token = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
    body = admin_client.get("/pallet/bulk-review/restore/%s" % token).get_data(as_text=True)
    assert _value(body, "sender_name") == SENDER
    assert _value(body, "client_name") == "К"
    # Същият дефект и при датата: непразно `shared` без дата → празно поле.
    assert _value(body, "doc_date") == date.today().isoformat()


def test_carried_client_without_date_still_gets_todays_date(admin_client):
    body = _import(admin_client, client_name="К").get_data(as_text=True)
    assert _value(body, "doc_date") == date.today().isoformat()


# ---------------------------------------------------------------- U10 (c)

def test_review_flags_unparsable_qty_before_issue(admin_client):
    rows = _ROWS[:2] + [["4700", "20", "A2", "d2", "abc", 1], ["4700", "30", "A3", "d3", 7, 2]]
    body = _import(admin_client, client_name="К").get_data(as_text=True)
    assert 'class="pallet-qty-warn" data-group="1"' in body
    body = _import(admin_client, rows=rows, client_name="К").get_data(as_text=True)
    warn = re.search(r'<p class="pallet-qty-warn" data-group="1".*?</p>', body, re.S).group(0)
    assert "display:none" not in warn
    assert "Ред(ове) №2:" in warn
    ok = re.search(r'<p class="pallet-qty-warn" data-group="2".*?</p>', body, re.S).group(0)
    assert "display:none" in ok
    # Живата проверка в браузъра (маркира реда при редакция).
    assert "function markBadQtyRows" in body and "markBadQtyRows(table, warn)" in body


def test_restored_review_flags_negative_qty(admin_client):
    items = [{"order_no": "1", "pos": "10", "reference": "R", "reference_desc": "x", "qty": "-3"}]
    resp = post_with_csrf(admin_client, "/pallet/bulk-preview", {
        "client_name": "К", "groups": "1", "items_format_1": "orders",
        "items_json_1": json.dumps(items)}, csrf_source_url="/pallet/new")
    token = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
    body = admin_client.get("/pallet/bulk-review/restore/%s" % token).get_data(as_text=True)
    warn = re.search(r'<p class="pallet-qty-warn" data-group="1".*?</p>', body, re.S).group(0)
    assert "display:none" not in warn and "Ред(ове) №1:" in warn


# ---------------------------------------------------------------- U4

def _issue_pallet(client, **fields):
    items = [{"order_no": "1", "pos": "10", "reference": "R", "reference_desc": "x", "qty": "5"}]
    data = {"client_name": "К", "groups": "1", "items_format_1": "orders",
            "items_json_1": json.dumps(items)}
    data.update(fields)
    post_with_csrf(client, "/pallet/bulk-issue", data, csrf_source_url="/pallet/new")


def _pull(client, number):
    return post_with_csrf(client, "/packing/pull-pallet", {"code": number},
                          csrf_source_url="/packing/new").get_json()


def test_pull_pallet_returns_dimensions_in_mm(admin_client, db_module):
    """U4: размерите на картата (см) се губеха — редът на опаковъчния лист
    (мм) идваше без Дължина/Широчина/Височина."""
    _issue_pallet(admin_client, pallet_type_1="120×80", height_1="110", gross_1="250")
    res = _pull(admin_client, "1")
    assert res["ok"], res
    assert (res["row"]["length"], res["row"]["width"], res["row"]["height"]) == \
        ("1200", "800", "1100")
    assert res["row"]["gross"] == "250"


@pytest.mark.parametrize("pallet_type,height,expected", [
    ("120x100", "95,5", {"length": "1200", "width": "1000", "height": "955"}),
    ("__other__", "", {}),
    ("", "abc", {}),
    ("80×60", "", {"length": "800", "width": "600"}),
])
def test_pull_pallet_dimensions_skip_what_cannot_be_read(admin_client, db_module,
                                                        pallet_type, height, expected):
    _issue_pallet(admin_client, pallet_type_1=pallet_type, height_1=height)
    row = _pull(admin_client, "1")["row"]
    assert {k: row[k] for k in ("length", "width", "height") if k in row} == expected


# ---------------------------------------------------------------- e2e

@pytest.mark.e2e
def test_e2e_review_highlights_bad_qty_and_issues_with_sender(page, live_server, tmp_path,
                                                             db_module):
    """Истински браузър: прегледът показва изпращача от Настройки, маркира
    реда с „abc“ още преди издаване и маркировката изчезва след поправка;
    издадените карти носят изпращача."""
    import conftest

    xlsx_path = tmp_path / "orders.xlsx"
    xlsx_path.write_bytes(_xlsx(_ROWS[:2] + [["4700", "20", "A2", "d2", "abc", 1]]))
    conftest.e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill('input[name="client_name"]', "Клиент Е2Е")
    page.set_input_files('input[name="excel_file"]', str(xlsx_path))
    page.click('button:has-text("Зареди и раздели по палети")')
    page.wait_for_url(live_server + "/pallet/bulk-import*", timeout=10000)

    assert page.input_value("#f-sender_name") == SENDER
    assert page.input_value("#f-client_name") == "Клиент Е2Е"
    qty = page.locator('#pallet-items-1 tbody tr').nth(1).locator('input[data-field="qty"]')
    warn = page.locator('.pallet-qty-warn[data-group="1"]')
    assert qty.get_attribute("aria-invalid") == "true"
    assert warn.is_visible() and "№2" in warn.inner_text()

    qty.fill("7")
    assert qty.get_attribute("aria-invalid") is None
    assert not warn.is_visible()

    page.click('button:has-text("Издай всички палетни карти")')
    page.wait_for_url(live_server + "/pallet/bulk-result*", timeout=10000)
    cards = _pallets(db_module)
    assert cards and all(c["sender_name"] == SENDER for c in cards)
