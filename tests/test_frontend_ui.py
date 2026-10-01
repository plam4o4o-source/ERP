# -*- coding: utf-8 -*-
"""Интерфейсни поправки от 01.10.2026 (агент E) — проверки на рендера
(Flask test client). Поведението в браузър е в test_e2e_ui_2026_10.py."""
import json
import os
import re

from conftest import post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _issue(admin_client, url, data):
    r = post_with_csrf(admin_client, url, data, csrf_source_url=url, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    return int(r.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


# ---------------------------------------------------------------- Q4
def test_dead_css_selectors_removed():
    css = _read("static", "style.css")
    for sel in (".topheader", ".container-narrow", ".btn-icon"):
        assert sel not in css, sel


# ---------------------------------------------------------------- U3
def test_waybill_barcode_is_scannable_size(admin_client):
    doc_id = _issue(admin_client, "/waybill/new", {"consignee_name": "Получател"})
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    head = body[body.index('class="twb-head-no"'):]
    svg = re.search(r'<svg[^>]*viewBox="0 0 (\d+) (\d+)"', head)
    width, height = int(svg.group(1)), int(svg.group(2))
    # При 60 мм ширина височината трябва да е поне ~12 мм.
    assert 60 * height / width >= 12
    css = _read("static", "style.css")
    assert re.search(r"\.twb-2up \.twb-head-no svg \{[^}]*width: 60mm", css)


# ---------------------------------------------------------------- U8
def test_cmr_box4_without_place_shows_only_date(admin_client):
    doc_id = _issue(admin_client, "/cmr/new", {
        "consignee_name": "Получател", "place_loading": "", "date_loading": "2026-10-01",
        "established_place": "", "established_date": "2026-10-01"})
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert "— 01.10.2026" not in body
    assert '<div class="val">, 01.10.2026' not in body
    assert "01.10.2026" in body


def test_cmr_box4_with_place_keeps_dash(admin_client):
    doc_id = _issue(admin_client, "/cmr/new", {
        "consignee_name": "Получател", "place_loading": "Яворец", "date_loading": "2026-10-01"})
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert "Яворец — 01.10.2026" in body


# ---------------------------------------------------------------- U12
def test_cmr_copies_buttons_have_three_and_active_state(admin_client):
    doc_id = _issue(admin_client, "/cmr/new", {"consignee_name": "Получател"})
    body = admin_client.get("/doc/%d?copies=3" % doc_id).get_data(as_text=True)
    assert "3 екземпляра" in body
    assert body.count('class="print-page cmr-page"') == 3
    active = re.findall(r'<a class="btn btn-secondary btn-copies is-active"[^>]*>([^<]+)</a>', body)
    assert [a.strip() for a in active] == ["3 екземпляра"]
    assert 'aria-current="true"' in body


# ---------------------------------------------------------------- U11
def test_dualuse_declarant_prefilled_from_sender_person(admin_client, db_module):
    con = db_module.get_db()
    db_module.save_settings(con, {"sender_person": "Иван Петров"})
    con.commit()
    con.close()
    body = admin_client.get("/dualuse/new").get_data(as_text=True)
    assert re.search(r'name="declarant_name" value="Иван Петров"', body)


# ---------------------------------------------------------------- U9
def test_pallet_card_head_is_outside_legend_and_cards_share_markup(admin_client):
    body = admin_client.get("/pallet/new").get_data(as_text=True)
    for legend in re.findall(r"<legend>.*?</legend>", body, re.S):
        assert "pallet-card-remove" not in legend
    template = body[body.index('id="pallet-card-template"'):body.index("</template>")]
    assert 'class="pallet-card" data-card=""' in template
    assert 'class="pallet-card-head" hidden' in template
    assert 'id="pallet-issue-btn"' in body and 'class="pallet-issue-label"' in body


def test_pallet_tables_have_materials_lookup(admin_client):
    body = admin_client.get("/pallet/new").get_data(as_text=True)
    assert body.count('data-lookup-key="reference"') == 2
    assert body.count('data-lookup-fill="reference_desc"') == 2


# ---------------------------------------------------------------- P9 / P12
def test_skip_link_and_main_target(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert '<a class="skip-link no-print" href="#main-content">' in body
    assert '<div id="main-content" class="no-print" tabindex="-1"></div>' in body
    assert body.index('<main class="container">') < body.index('id="main-content"')
    assert body.index("skip-link") < body.index('id="sidebar"')


def _sidebar(body):
    return body.split('<aside', 1)[1].split('</aside>', 1)[0]


def test_system_nav_item_for_admin_only(admin_client, employee_client):
    # Само страничната лента: таблото също може да сочи /admin/system
    # (предупреждение за архив), а тестът е за пункта в навигацията.
    assert 'href="/admin/system"' in _sidebar(admin_client.get("/").get_data(as_text=True))
    assert 'href="/admin/system"' not in _sidebar(employee_client.get("/").get_data(as_text=True))


# ---------------------------------------------------------------- i18n
def test_new_js_strings_are_in_the_translated_dictionary():
    base = _read("templates", "base.html")
    js = _read("static", "app.js")
    for key in ("pallet_issue_n", "pallet_already_added", "add_anyway", "copied", "copy_failed"):
        assert "'%s':" % key in base, key
        assert '"%s"' % key in js, key


def test_prefill_never_overrides_invoice_currency():
    js = _read("static", "app.js")
    m = re.search(r"PREFILL_SKIP_KEYS = (\[[^\]]*\])", js)
    assert m and "currency" in json.loads(m.group(1))
