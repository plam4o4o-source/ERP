# -*- coding: utf-8 -*-
"""Одит (15.10.2026, QR): QR кодовете и публичният достъп до документи са
премахнати окончателно — заявка: „премахни qr кодовете от всякъде, спиране на
функцията окончателно“ (+ изборът на потребителя: махни и публичния адрес
/p/<token> на документите).

Проверява се: нито една печатна бланка (всички типове, „Печат на цялата
пратка“, груповият печат на палетни карти) няма QR код/публичен панел, а
баркодът (Code128) си остава; /doc/<id>/public-link/renew|revoke не
съществуват; /p/<каквото и да е> е само заглушка 410 без данни от
документа; няма карта „Постоянен публичен адрес“; `qrcode` вече не е
зависимост. e2e: ЧМР и етикетът на палетната карта (100×150) се побират на
един лист, баркодът е с размер за сканиране."""
import io
import json
import os
import re
import secrets

import pytest

from conftest import post_with_csrf as _post_with_csrf, read_source, with_required

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ONE_INVOICE_ITEM = json.dumps([{"material_code": "TEST-ITEM", "qty": "1", "unit_price": "1"}])
_PALLET_ITEMS = json.dumps([{"order_no": "PO-1", "pos": "10", "reference": "REF-1",
                             "reference_desc": "Материал", "qty": "5"}])

#: Всички видове документи, издадени през истинските форми.
ISSUE = [
    ("cmr", "/cmr/new", {"sender_name": "Изпращач ЕООД", "consignee_name": "Тайна Клиент ЕООД"}),
    ("packing", "/packing/new", {"sender_name": "Изпращач ЕООД", "receiver_name": "Тайна Клиент ЕООД"}),
    ("pallet", "/pallet/new", {"pallet_no": "1", "client_name": "Тайна Клиент ЕООД",
                               "items_format": "orders", "items_json": _PALLET_ITEMS}),
    ("waybill", "/waybill/new", {"sender_name": "Изпращач ЕООД", "consignee_name": "Тайна Клиент ЕООД"}),
    ("dualuse", "/dualuse/new", {"sender_name": "Тайна Клиент ЕООД"}),
    ("export_it", "/export-it/new", {"declarant_name": "Тайна Клиент ЕООД"}),
    ("invoice_br", "/invoice-br/new", {"consignee_name": "Тайна Клиент ЕООД",
                                       "items_json": _ONE_INVOICE_ITEM}),
    ("invoice_no", "/invoice-no/new", {"consignee_name": "Тайна Клиент ЕООД",
                                       "items_json": _ONE_INVOICE_ITEM}),
    ("invoice_dubai", "/invoice-dubai/new", {"consignee_name": "Тайна Клиент ЕООД",
                                             "items_json": _ONE_INVOICE_ITEM}),
]
BARCODED = {"cmr", "packing", "pallet", "waybill", "dualuse", "export_it"}

#: Следи от QR кода/публичния достъп, които не бива да има никъде.
QR_MARKERS = ('class="doc-qr', 'alt="QR"', "doc-qr-hint", "Сканирайте за директен преглед",
              "Scan to view", "Публичен достъп", "public-link", "/p/", "data:image/png;base64",
              "Постоянен публичен адрес")


def _issue(admin_client, url, data):
    resp = _post_with_csrf(admin_client, url, with_required(url, data), csrf_source_url=url,
                           follow_redirects=False)
    assert resp.status_code == 302, (url, resp.data[:300])
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def _assert_no_qr(html, where):
    for marker in QR_MARKERS:
        assert marker not in html, (where, marker)
    assert not re.search(r"<img[^>]*\bqr\b", html, re.I), where


def _assert_barcode(html, barcode, where):
    assert barcode in html, where
    assert re.search(r'<svg[^>]*>.*?<rect[^>]*fill="#000"', html, re.S), where


def _row(db_module, doc_id):
    con = db_module.get_db()
    try:
        return con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------- печатни изгледи
@pytest.mark.parametrize("doc_type,url,data", ISSUE, ids=[i[0] for i in ISSUE])
def test_print_view_has_no_qr_and_keeps_the_barcode(admin_client, db_module, doc_type, url, data):
    doc_id = _issue(admin_client, url, data)
    row = _row(db_module, doc_id)
    # Новите документи вече не получават публичен токен/срок.
    assert row["public_token"] is None and row["public_token_expires_at"] is None
    queries = [""]
    if doc_type == "pallet":
        queries.append("?format=label")
    if doc_type == "cmr":
        queries.append("?copies=3")
    for q in queries:
        resp = admin_client.get("/doc/%d%s" % (doc_id, q))
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        _assert_no_qr(html, doc_type + q)
        if doc_type in BARCODED:
            _assert_barcode(html, row["barcode"], doc_type + q)


def test_shipment_and_pallet_bulk_print_have_no_qr(admin_client, db_module):
    ids = {t: _issue(admin_client, u, d) for t, u, d in ISSUE if t in BARCODED}
    pallet2 = _issue(admin_client, "/pallet/new", dict(ISSUE[2][2], pallet_no="2"))
    bundle = "/shipment/print?ids=%s&from=%d" % (",".join("%d:1" % i for i in ids.values()), ids["cmr"])
    html = admin_client.get(bundle).get_data(as_text=True)
    _assert_no_qr(html, "shipment")
    for t, doc_id in ids.items():
        _assert_barcode(html, _row(db_module, doc_id)["barcode"], "shipment/" + t)

    resp = admin_client.get("/pallet/bulk-print?ids=%d,%d" % (ids["pallet"], pallet2))
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_no_qr(html, "pallet bulk")
    assert html.count('class="plt-head-barcode"') == 2
    _assert_barcode(html, _row(db_module, pallet2)["barcode"], "pallet bulk")


def test_normal_document_view_still_requires_login(client, admin_client):
    doc_id = _issue(admin_client, "/cmr/new", ISSUE[0][2])
    resp = client.get("/doc/%d" % doc_id, follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert 'id="sidebar"' in body and 'class="doc-toolbar' in body


# ---------------------------------------------------------------- публичният достъп
def test_public_link_renew_and_revoke_routes_are_gone(admin_client, flask_app):
    doc_id = _issue(admin_client, "/cmr/new", ISSUE[0][2])
    for action in ("renew", "revoke"):
        url = "/doc/%d/public-link/%s" % (doc_id, action)
        assert _post_with_csrf(admin_client, url, {}, csrf_source_url="/doc/%d" % doc_id).status_code == 404
        assert admin_client.get(url).status_code == 404
    endpoints = {r.endpoint for r in flask_app.url_map.iter_rules()}
    assert not endpoints & {"public_link_renew", "public_link_revoke"}


def _doc_with_live_token(db_module, doc_type="cmr"):
    """Документ, издаден ПРЕДИ спирането — с валиден (неизтекъл) токен."""
    token = secrets.token_hex(16)
    con = db_module.get_db()
    try:
        number, year, seq, barcode = db_module.next_number(con, doc_type)
        data = {"number": number, "barcode": barcode, "consignee_name": "Тайна Клиент ЕООД",
                "sender_name": "Секретен Изпращач"}
        con.execute(
            "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
            " public_token_expires_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_type, number, year, seq, barcode, token, "2099-01-01 00:00:00",
             json.dumps(data, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()
    return token, number, barcode


@pytest.mark.parametrize("who", ["anonymous", "admin"])
def test_public_url_is_a_410_stub_without_document_data(client, admin_client, db_module, who):
    token, number, barcode = _doc_with_live_token(db_module)
    viewer = client if who == "anonymous" else admin_client
    for path in ("/p/" + token, "/p/" + secrets.token_hex(16), "/p/няма-такъв"):
        resp = viewer.get(path, follow_redirects=False)
        assert resp.status_code == 410, path
        html = resp.get_data(as_text=True)
        assert "Публичният достъп до документи е спрян. Свържете се с изпращача." in html
        for secret in (number, barcode, "Тайна Клиент", "Секретен Изпращач", "ЧМР / CMR", token):
            assert secret not in html, (path, secret)
        assert 'id="sidebar"' not in html and "doc-toolbar" not in html
        _assert_no_qr(html, path)


def test_company_logo_needs_login_again(client, admin_client):
    """/logo.img беше публичен само заради /p/<token> (находка №5, 26.09.2026)."""
    resp = client.get("/logo.img", follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


# ---------------------------------------------------------------- настройки
def test_settings_have_no_public_address_card(admin_client, db_module):
    for url in ("/my-settings", "/admin/system"):
        html = admin_client.get(url).get_data(as_text=True)
        assert "public_base_url" not in html, url
        assert 'id="public-url"' not in html and 'href="#public-url"' not in html, url
        assert "Постоянен публичен адрес" not in html and "QR" not in html, url
    # Стара форма, изпратена към сървъра, вече нищо не записва.
    _post_with_csrf(admin_client, "/admin/system",
                    {"form": "public_base_url", "public_base_url": "https://firma.example.com"},
                    csrf_source_url="/admin/system")
    con = db_module.get_db()
    try:
        assert "public_base_url" not in db_module.get_settings(con)
    finally:
        con.close()


def test_remote_access_card_is_still_there(admin_client):
    """Отдалеченият достъп (Cloudflare тунел) е ОТДЕЛНА функция и остава."""
    html = admin_client.get("/admin/system").get_data(as_text=True)
    assert 'id="remote"' in html
    assert "/admin/system/remote-start" in html


# ---------------------------------------------------------------- код и зависимости
def _lines(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return [ln.split("#", 1)[0].strip().lower() for ln in f]


def test_qrcode_is_no_longer_a_dependency():
    for name in ("requirements.txt", "constraints-release.txt"):
        assert not [ln for ln in _lines(name) if re.match(r"qrcode\b", ln)], name
    assert not os.path.exists(os.path.join(ROOT, "qr_code.py"))
    for fname in os.listdir(ROOT):
        if fname.endswith(".py"):
            src = read_source(fname)
            assert not re.search(r"^\s*(import qrcode|from qrcode|import qr_code)", src, re.M), fname
    release = read_source(".github", "workflows", "release.yml")
    assert "qrcode" not in release


def test_frontend_has_no_qr_code_left():
    js = read_source("static", "app.js")
    for gone in ("relocateQrHints", "publicDocPath", '"qr_code"', "initCopyButtons", "doc-qr"):
        assert gone not in js, gone
    assert 'var wanted = ["code_128", "ean_13"];' in js
    css = read_source("static", "style.css")
    assert not re.search(r"\.doc-qr|--cmr-qr|\.copy-ok", css)
    macros = read_source("templates", "_macros.html")
    assert "macro doc_qr" not in macros and "macro public_link_panel" not in macros
    base = read_source("templates", "base.html")
    assert "camera_foreign_qr" not in base and "'camera_foreign_code':" in base


# ---------------------------------------------------------------- e2e: побиране на листа
MM = 96 / 25.4


def _pdf_pages(page):
    pypdf = pytest.importorskip("pypdf")
    data = page.pdf(print_background=True, prefer_css_page_size=True)
    return pypdf.PdfReader(io.BytesIO(data)).pages


@pytest.mark.e2e
def test_e2e_cmr_and_pallet_label_still_fit_one_page(page, live_server, db_module):
    pytest.importorskip("playwright.sync_api")
    from conftest import e2e_login
    from test_print_layout import _doc

    cmr = _doc(db_module, "cmr", {"sender_name": "ПХ Логистик ЕООД", "consignee_name": "Müller GmbH",
                                  "marks": "PL-001\nPL-002", "goods": "Стомана\nЛагери",
                                  "packages": "2", "weight": "1200", "carrier": "Транс ООД"})
    pallet = _doc(db_module, "pallet", {"client_name": "Müller Logistik GmbH", "client_city": "München",
                                        "pallet_no": "3/12", "gross": "456.7", "pallet_type": "120×80"})
    e2e_login(page, live_server)

    # ЧМР — един лист A4, най-едрата степен, без QR; баркод ≥ 8мм.
    page.goto(live_server + "/doc/%d" % cmr)
    page.wait_for_load_state("load")
    page.set_viewport_size({"width": 733, "height": 1000})
    page.emulate_media(media="print")
    m = page.evaluate("""() => {
        const bar = document.querySelector('.cmr-head .no svg rect[fill="#000"]').getBoundingClientRect();
        return {fit: document.querySelector('.cmr-page').dataset.cmrFit,
                qr: document.querySelectorAll('.doc-qr, img[alt="QR"]').length, bar: bar.height};
    }""")
    assert m["fit"] == "0" or m["fit"] == "1", m
    assert m["qr"] == 0 and m["bar"] / MM >= 8, m
    assert len(_pdf_pages(page)) == 1

    # Палетна карта A4 — баркодът зае освободеното от QR място.
    page.emulate_media(media="screen")
    page.goto(live_server + "/doc/%d" % pallet)
    page.wait_for_load_state("load")
    page.emulate_media(media="print")
    w = page.evaluate("() => document.querySelector('.plt-head-barcode svg').getBoundingClientRect().width")
    assert w / MM >= 95, w
    assert len(_pdf_pages(page)) == 1

    # Етикет 100×150 — един лист с точния размер; баркод ≥ 20мм висок.
    page.emulate_media(media="screen")
    page.goto(live_server + "/doc/%d?format=label" % pallet)
    page.wait_for_load_state("load")
    page.emulate_media(media="print")
    bar = page.evaluate("""() => document.querySelector('.plt-head-barcode svg rect[fill="#000"]')
                                .getBoundingClientRect().height""")
    assert bar / MM >= 20, bar
    pages = _pdf_pages(page)
    assert len(pages) == 1
    box = pages[0].mediabox
    assert abs(float(box.width) - 100 * 72 / 25.4) < 2 and abs(float(box.height) - 150 * 72 / 25.4) < 2
