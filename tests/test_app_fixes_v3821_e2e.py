# -*- coding: utf-8 -*-
"""v3.82.1, група APPFIX — проверки в истински браузър (pytest -m e2e):
десетичният знак при автоматично попълване (APPFIX-9), пренасянето на дълъг
път на телефон (APPFIX-8), филтърът „Клиент“ при живо търсене (APPFIX-5) и
търсенето без диакритика (APPFIX-10)."""
import json

import pytest

from conftest import e2e_login

pytestmark = pytest.mark.e2e


def _set_tare(db_module, tare):
    con = db_module.get_db()
    db_module.save_settings(con, {"packing_pallet_tare": tare})
    con.commit()
    con.close()


def _add(db_module, doc_type, data, number):
    con = db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, 2026, ?, ?, ?, ?, 1)",
        (doc_type, number, int(number[-4:]), "E2E-" + number, "e2e-" + number,
         json.dumps(data, ensure_ascii=False)))
    con.commit()
    con.close()
    return cur.lastrowid


def test_pallet_net_keeps_comma_for_grouped_gross_and_comma_tare(page, live_server, db_module):
    e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill("#f-gross", "1 250,75")
    assert page.input_value("#f-net") == "1230,75"     # досега „1230.75“
    _set_tare(db_module, "25,5")
    page.goto(live_server + "/pallet/new")
    page.fill("#f-gross", "1250")
    assert page.input_value("#f-net") == "1224,5"      # цяло бруто — знакът на тарата
    page.fill("#f-gross", "1250.5")
    assert page.input_value("#f-net") == "1225"


def _row(page, n=0):
    return page.locator("#packing-items tbody tr").nth(n)


def _fill(row, **values):
    for field, value in values.items():
        row.locator('input[data-field="%s"]' % field).fill(value)


def _val(page, selector):
    return page.input_value(selector)


def test_packing_auto_values_follow_the_comma_of_the_form(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/packing/new")
    row = _row(page)
    # Размерите първо (обемът тръгва с точка), после брутото със запетая.
    _fill(row, qty="1", length="1200", width="800", height="1500")
    assert row.locator('input[data-field="volume"]').input_value() == "1.44"
    _fill(row, gross="1 250,75")
    assert row.locator('input[data-field="net"]').input_value() == "1230,75"
    assert row.locator('input[data-field="volume"]').input_value() == "1,44"
    assert _val(page, "[name=total_gross]") == "1250,75"
    assert _val(page, "[name=total_net]") == "1230,75"
    assert _val(page, "[name=total_volume]") == "1,44"
    # Цяло бруто във форма със запетаи — сборът пак е със запетая.
    _fill(row, gross="1250")
    assert row.locator('input[data-field="net"]').input_value() == "1230"
    assert _val(page, "[name=total_volume]") == "1,44"


def test_packing_auto_values_keep_the_dot_of_a_dot_form(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/packing/new")
    row = _row(page)
    _fill(row, qty="1", gross="300.5", length="1200", width="800", height="1500")
    assert row.locator('input[data-field="net"]').input_value() == "280.5"
    assert row.locator('input[data-field="volume"]').input_value() == "1.44"
    assert _val(page, "[name=total_volume]") == "1.44"


def test_long_path_on_system_page_does_not_scroll_on_a_phone(live_server, e2e_context_factory):
    ctx = e2e_context_factory(viewport={"width": 390, "height": 844}, is_mobile=True,
                              has_touch=True, device_scale_factor=2)
    page = ctx.new_page()
    e2e_login(page, live_server)
    page.goto(live_server + "/admin/system")
    page.evaluate("""() => document.querySelectorAll('main code').forEach(c => {
        c.textContent = '\\\\\\\\SERVER\\\\Shared\\\\PHLogistics\\\\DataFolderForTheWholeOffice\\\\pacho_logistic.db';
    })""")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_docs_client_filter_survives_live_search_and_accents_are_ignored(page, live_server, db_module):
    mu1 = _add(db_module, "cmr", {"consignee_name": "Müller & Söhne GmbH", "goods": "Schalter"}, "2026/0001")
    mu2 = _add(db_module, "cmr", {"consignee_name": "MÜLLER & SÖHNE GMBH", "goods": "Kabel"}, "2026/0002")
    other = _add(db_module, "cmr", {"consignee_name": "Ünal Lojistik", "goods": "Schalter"}, "2026/0003")
    e2e_login(page, live_server)
    page.goto(live_server + "/docs?client=" + "muller %26 sohne gmbh")
    rows = lambda: sorted(int(x) for x in page.eval_on_selector_all(
        "#docs-results tr[data-row-id]", "trs => trs.map(t => t.dataset.rowId)"))
    assert rows() == sorted([mu1, mu2])
    assert page.is_visible("#docs-results .filter-chip:has-text('Клиент')")
    page.fill("form[data-live-search] .searchbar-input", "schalter")
    page.wait_for_function("() => document.querySelectorAll('#docs-results tr[data-row-id]').length === 1")
    assert rows() == [mu1]                                  # клиентът остава в сила
    page.click("#docs-results .filter-chip:has-text('Клиент')")
    page.wait_for_load_state("networkidle")
    assert rows() == sorted([mu1, other])
    page.goto(live_server + "/docs")
    page.fill("form[data-live-search] .searchbar-input", "unal")
    page.wait_for_function("() => document.querySelectorAll('#docs-results tr[data-row-id]').length === 1")
    assert rows() == [other]
