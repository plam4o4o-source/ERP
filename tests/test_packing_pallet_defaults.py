# -*- coding: utf-8 -*-
"""Заявка (13.10.2026): опаковъчен лист — „не се вмъква дължина и широчина на
палета, нетно тегло да е −20 кг от бруто на палета, описание да е HV
Switchgear cabinets material (Disassemble), обем да се изчислява автоматично
от дължина, широчина и височина“."""
import json

import pytest

from conftest import post_with_csrf
from test_workflow_shortcuts import _issue_pallets, _pull, _rows

import routes_pallet_extra


@pytest.mark.parametrize("pallet_type,height,expected", [
    ("120×80", "150", ("1200", "800", "1500")),
    ("120*80", "150", ("1200", "800", "1500")),
    ("120×80 см", "150", ("1200", "800", "1500")),
    ("EUR 120×80", "150", ("1200", "800", "1500")),
    ("120/80", "150", ("1200", "800", "1500")),
    ("120×80×150", "", ("1200", "800", "1500")),  # височината — от третото число
    ("1200×800", "150", ("1200", "800", "1500")),  # вече в мм
    ("EUR", "150", ("1200", "800", "1500")),
    ("120,5×80", "150", ("1205", "800", "1500")),
])
def test_pallet_dimensions_are_read_from_any_written_form(pallet_type, height, expected):
    dims = routes_pallet_extra._pallet_dims_mm({"pallet_type": pallet_type, "height": height})
    assert (dims.get("length"), dims.get("width"), dims.get("height")) == expected


def test_unknown_pallet_type_gives_no_length_or_width():
    dims = routes_pallet_extra._pallet_dims_mm({"pallet_type": "Друг", "height": "150"})
    assert dims == {"height": "1500"}


def _patch_last_pallet(db_module, **fields):
    con = db_module.get_db()
    row = con.execute("SELECT id, data FROM documents WHERE doc_type='pallet' ORDER BY id DESC LIMIT 1").fetchone()
    data = json.loads(row["data"])
    data.update(fields)
    con.execute("UPDATE documents SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))
    con.commit()
    con.close()


def test_pulled_row_has_fixed_description_net_gross_minus_20_and_dimensions(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))])
    _patch_last_pallet(db_module, pallet_type="120*80", height="150", gross="520,5")
    res = _pull(admin_client, "1")
    row = res["row"]
    assert row["description"] == "HV Switchgear cabinets material (Disassemble)"
    assert row["net"] == "500,5" and row["gross"] == "520,5"
    assert (row["length"], row["width"], row["height"]) == ("1200", "800", "1500")
    assert res["net_source"] == "tare" and res["fixed_description"] is True
    assert "20 кг" in res["note"]


def test_range_of_cards_returns_every_card_with_the_same_description(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2")), _rows(("A2", "d2", "3"))])
    res = _pull(admin_client, "1-2")
    assert [r["row"]["description"] for r in res["results"]] == \
        ["HV Switchgear cabinets material (Disassemble)"] * 2
    # Еднаквото описание не бива да прави втората карта „повторение“ (app.js).
    assert all(r["fixed_description"] for r in res["results"])
    assert [r["row"]["net"] for r in res["results"]] == ["80", "80"]  # бруто 100 − 20


def test_settings_change_description_and_tare(admin_client, db_module):
    con = db_module.get_db()
    db_module.save_settings(con, {"packing_pallet_description": "Spare parts", "packing_pallet_tare": "25"})
    con.commit()
    con.close()
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))])
    row = _pull(admin_client, "1")["row"]
    assert row["description"] == "Spare parts" and row["net"] == "75"


def test_empty_settings_restore_the_previous_behaviour(admin_client, legacy_packing_pull):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))])
    res = _pull(admin_client, "1")
    assert res["row"]["description"].startswith("Pallet 1")
    assert "net" not in res["row"] and res["fixed_description"] is False


def test_net_is_not_negative_when_gross_is_below_the_tare(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))])
    _patch_last_pallet(db_module, gross="15")
    assert "net" not in _pull(admin_client, "1")["row"]


def test_settings_page_shows_defaults_and_saves_them(admin_client, db_module):
    body = admin_client.get("/settings").get_data(as_text=True)
    assert 'value="HV Switchgear cabinets material (Disassemble)"' in body
    assert 'name="packing_pallet_tare"' in body and 'value="20"' in body
    resp = post_with_csrf(admin_client, "/settings", {
        "packing_pallet_tare": "22",
        "orig_packing_pallet_tare": "20",
    }, csrf_source_url="/settings")
    assert resp.status_code in (200, 302)
    con = db_module.get_db()
    s = db_module.get_settings(con)
    con.close()
    assert s.get("packing_pallet_tare") == "22"
    # Формата без тези полета (стара/скрипт) не нулира описанието.
    assert "packing_pallet_description" not in s


def test_packing_form_carries_the_tare_for_the_browser(admin_client):
    body = admin_client.get("/packing/new").get_data(as_text=True)
    assert 'data-pallet-tare="20"' in body


@pytest.mark.e2e
def test_browser_rows_get_net_and_volume_and_both_cards_are_added(page, live_server, db_module, admin_client):
    """В Chromium: две карти с еднакво описание се добавят и двете; ръчно
    бруто дава нето = бруто − 20, Д×Ш×В — обем; ръчно нето не се презаписва."""
    pytest.importorskip("playwright.sync_api")
    from conftest import e2e_login
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2")), _rows(("A2", "d2", "3"))])
    e2e_login(page, live_server)
    page.goto(live_server + "/packing/new")
    page.fill("#pull-pallet-code", "1-2")
    with page.expect_response(lambda r: "pull-pallet" in r.url):
        page.locator("#pull-pallet-code").press("Enter")
    rows = page.locator("#packing-items tbody tr")
    page.wait_for_function("() => document.querySelectorAll('#packing-items tbody tr').length === 2")
    first = rows.nth(0)
    assert first.locator("[data-field=length]").input_value() == "1200"
    assert first.locator("[data-field=width]").input_value() == "800"
    assert first.locator("[data-field=volume]").input_value() == "1.44"
    assert first.locator("[data-field=net]").input_value() == "80"
    page.click("[data-add-row=packing-items]")
    last = rows.last
    last.locator("[data-field=length]").fill("1200")
    last.locator("[data-field=width]").fill("800")
    last.locator("[data-field=height]").fill("1000")
    last.locator("[data-field=gross]").fill("310,5")
    assert last.locator("[data-field=volume]").input_value() == "0.96"
    assert last.locator("[data-field=net]").input_value() == "290,5"
    last.locator("[data-field=net]").fill("250")
    last.locator("[data-field=gross]").fill("400")
    assert last.locator("[data-field=net]").input_value() == "250"
