# -*- coding: utf-8 -*-
"""Интерфейсни поправки от 01.10.2026 (агент E) — поведение в истински
браузър (Playwright/Chromium). Пускат се изрично:
`python3 -m pytest -m e2e tests/test_e2e_ui_2026_10.py`."""
import io
import json

import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("playwright.sync_api")

from test_e2e_smoke import _login, live_server, page  # noqa: F401,E402

_ISSUE_BTN = '#main-doc-form button[type="submit"]:not([formaction])'
_PREVIEW_BTN = "#main-doc-form button[formaction]"


def _sql(db_module, sql, args=()):
    con = db_module.get_db()
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _client_with_points(db_module, name, points):
    cid = _sql(db_module, "INSERT INTO clients (name, address, city, postcode, country)"
                          " VALUES (?, 'Main 1', 'Milano', '20100', 'Italy')", (name,))
    for label, addr in points:
        _sql(db_module, "INSERT INTO client_unload_points (client_id, label, address, city,"
                        " postcode, country) VALUES (?, ?, ?, 'Town', '1000', 'Italy')",
             (cid, label, addr))
    return cid


def _issue(page, base):
    page.click(_ISSUE_BTN)
    page.wait_for_url(base + "/doc/*")
    return int(page.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])


# ---------------------------------------------------------------- R5
def test_edit_old_invoice_keeps_currency_euro(page, live_server, db_module):
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    url = page.evaluate("""async () => { const fd = new FormData(document.getElementById('main-doc-form'));
        fd.set('consignee_name', 'Клиент'); fd.set('invoice_number', 'INV-1');
        fd.set('items_json', JSON.stringify([{description: 'Стока', qty: '1', unit_price: '2'}]));
        const r = await fetch(location.pathname, {method: 'POST', body: fd}); return r.url; }""")
    doc_id = int(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
    con = db_module.get_db()
    data = json.loads(con.execute("SELECT data FROM documents WHERE id=?", (doc_id,)).fetchone()[0])
    data["currency"] = "USD"
    con.execute("UPDATE documents SET data=? WHERE id=?", (json.dumps(data), doc_id))
    con.commit()
    con.close()
    page.goto(live_server + "/doc/%d/edit" % doc_id)
    assert page.input_value("#f-currency") == "EURO"


# ---------------------------------------------------------------- U6 + P4
def test_cmr_client_and_unload_points_restored_after_preview_and_edit(page, live_server, db_module):
    cid = _client_with_points(db_module, "Склад Клиент ООД",
                              [("Склад А", "Via A 1"), ("Склад Б", "Via B 2")])
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.select_option("#f-client-select-cmr", value=str(cid))
    page.select_option("#unload-point-select", index=3)   # [—, централен, А, Б]
    page.fill("#f-consignee_name", "Склад Клиент ООД")
    page.fill("#f-consignee_address", "РЪЧНО ПОПРАВЕН АДРЕС")
    page.click(_PREVIEW_BTN)
    page.wait_for_url("**/preview/**")
    page.click("text=Назад към формата")
    page.wait_for_load_state()
    assert page.input_value("#f-client-select-cmr") == str(cid)
    assert page.locator("#unload-point-select option").count() == 4
    assert "Via B 2" in page.input_value("#unload-point-select")
    page.select_option("#unload-point-select", index=2)   # друг склад
    assert page.input_value("#f-consignee_address") == "РЪЧНО ПОПРАВЕН АДРЕС"
    assert "Via A 1" in page.input_value("#place_delivery")
    doc_id = _issue(page, live_server)
    page.goto(live_server + "/doc/%d/edit" % doc_id)
    assert page.input_value("#f-client-select-cmr") == str(cid)
    assert "Via A 1" in page.input_value("#unload-point-select")


def test_cmr_smart_defaults_loading_place_and_single_unload_point(page, live_server, db_module):
    one = _client_with_points(db_module, "Един Склад ЕООД", [("Единствен", "Via Uno 1")])
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    city = page.input_value("#f-sender_city")
    country = page.input_value("#f-sender_country")
    assert page.input_value("#place_loading") == ", ".join(v for v in (city, country) if v)
    page.fill("#f-sender_city", "Пловдив")
    assert page.input_value("#place_loading").startswith("Пловдив")
    page.fill("#place_loading", "Ръчно място")
    page.fill("#f-sender_city", "София")
    assert page.input_value("#place_loading") == "Ръчно място"
    page.select_option("#f-client-select-cmr", value=str(one))
    assert "Via Uno 1" in page.input_value("#place_delivery")


# ---------------------------------------------------------------- U4 + P4 (опаковъчен лист)
def _issue_pallet(page, base):
    page.goto(base + "/pallet/new")
    page.fill("#f-client_name", "Клиент")
    page.fill("#pallet-items tbody tr:first-child input[data-field='reference']", "REF-1")
    page.fill("#pallet-items tbody tr:first-child input[data-field='qty']", "4")
    page.fill("#f-gross", "120")
    page.fill("#f-height", "150")
    return _issue(page, base)


def test_packing_pull_same_pallet_twice_asks(page, live_server):
    _login(page, live_server)
    _issue_pallet(page, live_server)
    page.goto(live_server + "/packing/new")
    rows = "#packing-items tbody tr"
    for _ in range(2):
        page.fill("#pull-pallet-code", "0001/" + page.evaluate("new Date().getFullYear()+''"))
        page.click("#pull-pallet-btn")
        page.wait_for_function("() => !document.querySelector('#pull-pallet-btn.btn-busy')")
        page.wait_for_timeout(100)
    # Одит (04.10.2026, UX-6): началният празен ред се маха преди издърпаните
    # редове — остава само редът от палета (вторият опит пита, не добавя).
    assert page.locator(rows).count() == 1
    pulled = rows + ":nth-child(1) input[data-field='%s']"
    # Размерите на палета (мм, от сървъра) и обемът от тях (P4).
    assert [page.input_value(pulled % f) for f in ("length", "width", "height", "volume")] == \
        ["1200", "800", "1500", "1.44"]
    assert "вече е добавена" in page.inner_text("#pull-pallet-msg")
    page.click("#pull-pallet-msg button")
    assert page.locator(rows).count() == 2


def test_packing_volume_and_total_packages_defaults(page, live_server):
    _login(page, live_server)
    page.goto(live_server + "/packing/new")
    cell = "#packing-items tbody tr:first-child input[data-field='%s']"
    page.fill(cell % "description", "Кашон")
    for f, v in (("length", "1200"), ("width", "800"), ("height", "1500")):
        page.fill(cell % f, v)
    assert page.input_value(cell % "volume") == "1.44"
    assert page.input_value("#f-total_packages") == "1"
    page.fill(cell % "volume", "2")
    page.fill(cell % "height", "1000")
    assert page.input_value(cell % "volume") == "2"          # ръчното печели
    page.fill("#f-total_packages", "9")
    page.click('[data-add-row="packing-items"]')
    page.fill("#packing-items tbody tr:nth-child(2) input[data-field='description']", "Още")
    assert page.input_value("#f-total_packages") == "9"


# ---------------------------------------------------------------- U9 + P4 (палетна карта)
def test_pallet_multi_card_label_head_and_materials_lookup(page, live_server, db_module):
    _sql(db_module, "INSERT INTO materials (code, description, net_weight) VALUES ('MAT-77', 'Автомат 16A', '0.3')")
    _login(page, live_server)
    page.goto(live_server + "/pallet/new")
    label = "#pallet-issue-btn .pallet-issue-label"
    single = page.inner_text(label)
    ref = "#pallet-items tbody tr:first-child input[data-field='reference']"
    page.fill(ref, "mat-77")
    page.press(ref, "Tab")
    page.wait_for_function("() => document.querySelector(\"#pallet-items tbody tr input[data-field='reference_desc']\").value !== ''")
    assert page.input_value("#pallet-items tbody tr:first-child input[data-field='reference_desc']") == "Автомат 16A"
    page.click("#pallet-add-card-btn")
    page.click("#pallet-add-card-btn")
    assert page.inner_text(label).startswith("Издай 3 палетни карти")
    overlaps = page.evaluate("""() => [...document.querySelectorAll('.pallet-card')].map(c => {
        const b = c.querySelector('.pallet-card-remove').getBoundingClientRect();
        const l = c.querySelector('legend').getBoundingClientRect();
        return !(b.bottom <= l.top || b.top >= l.bottom || b.right <= l.left || b.left >= l.right); })""")
    assert overlaps == [False, False, False]
    page.locator(".pallet-card-remove").nth(2).click()
    page.locator(".pallet-card-remove").nth(1).click()
    assert page.inner_text(label) == single
    assert page.locator(".pallet-card-head").first.is_hidden()


# ---------------------------------------------------------------- U8 / U3
def test_qr_hint_moved_out_of_document_header(page, live_server):
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill("#f-consignee_name", "Получател")
    _issue(page, live_server)
    assert page.locator(".print-page .doc-qr-hint").count() == 0
    hints = page.locator(".doc-qr-hints .doc-qr-hint").count()
    assert hints >= 1


def test_waybill_two_up_barcode_size_and_single_sheet(page, live_server):
    from pypdf import PdfReader
    _login(page, live_server)
    page.goto(live_server + "/waybill/new")
    items = [{"description": "Стока %d" % i, "packing": "Палет", "marks": "M",
              "weight": "450", "qty": "2"} for i in range(8)]
    url = page.evaluate("""async (items) => { const fd = new FormData(document.getElementById('main-doc-form'));
        fd.set('consignee_name', 'Получател'); fd.set('items_json', JSON.stringify(items)); fd.set('notes', 'Бележка');
        const r = await fetch('/waybill/new', {method: 'POST', body: fd}); return r.url; }""", items)
    page.goto(url)
    page.emulate_media(media="print")
    size = page.evaluate("""() => { const r = document.querySelector('.twb-head-no svg').getBoundingClientRect();
        return [r.width / 96 * 25.4, r.height / 96 * 25.4]; }""")
    assert size[0] >= 55 and size[1] >= 12, size
    pdf = page.pdf(format="A4")
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 1


# ---------------------------------------------------------------- U7 / U13
def test_documents_list_actions_not_clipped_at_1366(page, live_server):
    page.set_viewport_size({"width": 1366, "height": 768})
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill("#f-consignee_name", "Получател с дълго име ЕООД")
    _issue(page, live_server)
    page.goto(live_server + "/docs")
    sw, cw = page.evaluate("() => { const w = document.querySelector('table.list').parentElement; return [w.scrollWidth, w.clientWidth]; }")
    assert sw <= cw


def test_document_scales_to_phone_width(page, live_server):
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill("#f-consignee_name", "Получател")
    _issue(page, live_server)
    page.set_viewport_size({"width": 375, "height": 800})
    page.reload()
    right = page.evaluate("() => document.querySelector('.print-page').getBoundingClientRect().right")
    assert right <= 375
    fit_phone = page.evaluate("() => document.querySelector('.cmr-page').dataset.cmrFit")
    page.set_viewport_size({"width": 1366, "height": 800})
    page.reload()
    assert page.evaluate("() => document.querySelector('.cmr-page').dataset.cmrFit") == fit_phone
    page.emulate_media(media="print")
    assert page.evaluate("() => getComputedStyle(document.querySelector('.print-page')).zoom") == "1"


# ---------------------------------------------------------------- P6 / P9
def test_copy_helper_and_skip_link(page, live_server):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _login(page, live_server)
    page.goto(live_server + "/")
    page.evaluate("""() => { const b = document.createElement('button'); b.type = 'button'; b.id = 'cp';
        b.textContent = 'Копирай'; b.setAttribute('data-copy-text', 'https://example.test/p/abc');
        document.querySelector('main').appendChild(b); }""")
    page.click("#cp")
    page.wait_for_function("() => document.getElementById('cp').textContent === 'Копирано'")
    assert page.evaluate("navigator.clipboard.readText()") == "https://example.test/p/abc"
    page.wait_for_function("() => document.getElementById('cp').textContent === 'Копирай'", timeout=4000)

    page.goto(live_server + "/cmr/new")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className").startswith("skip-link")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main-content"
