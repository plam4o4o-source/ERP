# -*- coding: utf-8 -*-
"""Одит (04.10.2026) — графични подобрения (група UI), измерени в истински
браузър (Playwright/Chromium). Пускат се изрично:
`python3 -m pytest -m e2e tests/test_e2e_ui_layout.py`."""
import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("playwright.sync_api")

from test_e2e_smoke import _login, live_server, page  # noqa: F401,E402

_ISSUE_BTN = '#main-doc-form button[type="submit"]:not([formaction])'
_LONG = "Получател с много дълго име за проверка на ширината ЕООД"


def _issue_cmr(page, base, consignee=_LONG):
    page.goto(base + "/cmr/new")
    page.fill("#f-consignee_name", consignee)
    page.click(_ISSUE_BTN)
    page.wait_for_url(base + "/doc/*")
    return int(page.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])


def _overflow(page):
    return page.evaluate("""() => [...document.querySelectorAll('table.list')].map(t => {
        const w = t.parentElement; return [w.scrollWidth, w.clientWidth]; })""")


# ---------------------------------------------------------------- P1/B1
@pytest.mark.parametrize("width", [1366, 1920])
def test_lists_do_not_overflow_and_delete_is_visible(page, live_server, db_module, width):
    page.set_viewport_size({"width": width, "height": 900})
    _login(page, live_server)
    _issue_cmr(page, live_server)
    con = db_module.get_db()
    # типовете с най-дълги имена — точно те разпъваха колоната „Тип“ (B1)
    for i, t in enumerate(("dualuse", "export_it", "waybill")):
        con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_at)"
                    " VALUES (?, '0001/2026', 2026, 1, ?, ?, '2026-10-04 16:51:00')",
                    (t, "LONG-%d-04102026-0001" % i, '{"consignee_name": "%s"}' % _LONG))
    con.execute("INSERT INTO clients (name, alias, address, city, country, eik, vat, phone)"
                " VALUES (?, 'Псевдоним', 'бул. Дълъг адрес 123', 'София', 'България',"
                " '123456789', 'BG123456789', '+359 2 987 6543')", (_LONG,))
    con.commit()
    con.close()
    for url in ("/docs", "/clients", "/"):
        page.goto(live_server + url)
        for sw, cw in _overflow(page):
            assert sw <= cw, (url, width, sw, cw)
    page.goto(live_server + "/docs")
    box = page.evaluate("""() => { const b = document.querySelector('td.row-actions form button').getBoundingClientRect();
        const w = document.querySelector('table.list').parentElement.getBoundingClientRect();
        return [b.left, b.right, w.left, w.right]; }""")
    assert box[0] >= box[2] and box[1] <= box[3], box


# ---------------------------------------------------------------- P3/B3
def test_filter_bar_is_one_row_aligned_with_fields(page, live_server):
    page.set_viewport_size({"width": 1366, "height": 800})
    _login(page, live_server)
    _issue_cmr(page, live_server)
    page.goto(live_server + "/docs")
    m = page.evaluate("""() => { const sel = document.querySelector('#f-type').getBoundingClientRect();
        return [...document.querySelectorAll('.searchbar-actions > *')].map(e => {
          const r = e.getBoundingClientRect(); return [Math.round(r.bottom), Math.round(r.height)]; })
          .concat([[Math.round(sel.bottom), Math.round(sel.height)]]); }""")
    assert len(m) == 5                                   # превключвател, Търси, Изчисти, Excel + полето
    assert len({b for b, _ in m}) == 1, m                # общо дъно
    assert {h for _, h in m} == {38}, m                  # еднаква височина
    # превключвателят пуска живото търсене с групиране
    page.check("input[name=group]")
    page.wait_for_selector("#docs-results tr.list-group-row")
    assert "group=client" in page.url


# ---------------------------------------------------------------- P7/B6/B7, B2
def test_controls_share_height_and_sidebar_scan_is_transparent(page, live_server):
    page.set_viewport_size({"width": 1366, "height": 800})
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    heights = page.evaluate("""() => ['input[type=text]:not(#sidebar-scan-input)', 'select', 'input[type=date]']
        .map(s => Math.round(document.querySelector('main ' + s).getBoundingClientRect().height))""")
    assert heights == [38, 38, 38], heights
    scan = page.evaluate("""() => { const cs = getComputedStyle(document.getElementById('sidebar-scan-input'));
        return [cs.backgroundColor, cs.borderTopWidth]; }""")
    assert scan == ["rgba(0, 0, 0, 0)", "0px"], scan
    page.focus("#sidebar-scan-input")
    ring = page.evaluate("getComputedStyle(document.querySelector('.sidebar-scan')).boxShadow")
    assert ring and ring != "none"


# ---------------------------------------------------------------- P4
def test_form_actions_stick_to_the_bottom(page, live_server):
    page.set_viewport_size({"width": 1366, "height": 768})
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.evaluate("window.scrollTo(0, 900)")
    r = page.evaluate("""() => { const r = document.querySelector('form .card > .actions:last-child').getBoundingClientRect();
        return [r.top, r.bottom]; }""")
    assert r[1] <= 768 + 1 and r[0] > 600, r


# ---------------------------------------------------------------- P8/B11/B12
def test_document_view_sticky_toolbar_and_cards_below(page, live_server):
    page.set_viewport_size({"width": 1366, "height": 768})
    _login(page, live_server)
    _issue_cmr(page, live_server)
    page.evaluate("window.scrollTo(0, 700)")
    page.wait_for_function("window.scrollY > 600")
    assert page.evaluate("Math.round(document.querySelector('.doc-toolbar').getBoundingClientRect().top)") == 0
    order = page.evaluate("""() => { const p = [...document.querySelectorAll('.print-page')].pop().getBoundingClientRect();
        return [...document.querySelectorAll('.doc-extra-card')].map(c => c.getBoundingClientRect().top > p.bottom); }""")
    assert order == [True, True]
    # „Прикачи“ е на реда на полето за файл
    tops = page.evaluate("""() => ['.attach-form input[type=file]', '.attach-form button']
        .map(s => Math.round(document.querySelector(s).getBoundingClientRect().top))""")
    assert abs(tops[0] - tops[1]) <= 2, tops
    # P6: полето само за четене (публичният адрес) не изглежда редактируемо
    ro = page.evaluate("""() => { const cs = getComputedStyle(document.querySelector('.public-link-url input[readonly]'));
        return [cs.borderTopStyle, cs.cursor]; }""")
    assert ro == ["dashed", "not-allowed"], ro


# ---------------------------------------------------------------- P10/B10/B16
def test_phone_lists_become_cards(page, live_server):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, live_server)
    _issue_cmr(page, live_server)
    page.goto(live_server + "/docs")
    m = page.evaluate("""() => {
        const head = document.querySelector('table.list tr');
        const row = document.querySelector('table.list tr:not(.list-group-row) td.row-actions').parentElement;
        const view = row.querySelector('.btn-view').getBoundingClientRect();
        const icon = row.querySelector('.icon-btn').getBoundingClientRect();
        const label = getComputedStyle(row.querySelector('td[data-label="Номер"]'), '::before').content;
        return {head: getComputedStyle(head).display, row: getComputedStyle(row).display,
                view: view.width, icon: [icon.width, icon.height], label: label,
                sw: document.documentElement.scrollWidth,
                shadow: getComputedStyle(document.getElementById('sidebar')).boxShadow}; }""")
    assert m["head"] == "none" and m["row"] == "grid", m
    assert m["view"] >= 150 and m["icon"] == [44, 44], m
    assert m["label"] == '"Номер"', m
    assert m["sw"] <= 390, m
    assert m["shadow"] == "none", m          # затворената лента не хвърля сянка


def test_phone_document_toolbar_scrolls_instead_of_wrapping(page, live_server):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, live_server)
    _issue_cmr(page, live_server)
    # всички видими бутони — на един ред (общ вертикален център)
    m = page.evaluate("""() => { const t = document.querySelector('.doc-toolbar');
        const mids = new Set([...t.children].filter(c => c.getBoundingClientRect().width > 2)
          .map(c => { const r = c.getBoundingClientRect(); return Math.round((r.top + r.bottom) / 2); }));
        return [mids.size, t.scrollWidth > t.clientWidth, document.documentElement.scrollWidth]; }""")
    assert m[0] == 1 and m[1] and m[2] <= 390, m


# ---------------------------------------------------------------- e2e: иконните бутони се натискат по текст
def test_icon_buttons_still_reachable_by_their_text(page, live_server):
    _login(page, live_server)
    _issue_cmr(page, live_server, consignee="Икона Текст ЕООД")
    page.goto(live_server + "/docs")
    row = page.locator("tr", has_text="Икона Текст ЕООД")
    row.get_by_role("link", name="Копирай като нов").click()
    page.wait_for_url(live_server + "/cmr/new*")
    assert page.input_value("#f-consignee_name") == "Икона Текст ЕООД"
