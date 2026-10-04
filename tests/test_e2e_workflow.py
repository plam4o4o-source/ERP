# -*- coding: utf-8 -*-
"""Одит (04.10.2026, група WORKFLOW): браузърната част на бързите пътища —
виж tests/test_workflow_shortcuts.py за сървърната. Пуска се с `-m e2e`."""
import json

import pytest

import conftest
from test_workflow_shortcuts import _issue_pallets, _pallet_docs, _rows, _xlsx

pytestmark = pytest.mark.e2e


def _count_pallets(db_module):
    return len(_pallet_docs(db_module))


def test_e2e_multi_card_form_survives_browser_back_and_is_not_issued_twice(page, live_server,
                                                                          db_module):
    """F5: „Назад“ след преглед на няколко карти връщаше една празна карта;
    F6: „Назад“ + повторно „Издай“ издаваше партидата втори път."""
    conftest.e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill("[name=client_name]", "ACME Ltd")
    page.fill("[name=notes]", "fragile")
    page.fill("#pallet-items tbody tr:first-child input[data-field=order_no]", "PO1")
    page.fill("#pallet-items tbody tr:first-child input[data-field=qty]", "1")
    page.click("#pallet-add-card-btn")
    page.click("#pallet-add-card-btn")
    cards = page.locator(".pallet-card")
    for i in (1, 2):
        cards.nth(i).locator("input[data-field=order_no]").first.fill("PO%d" % (i + 1))
        cards.nth(i).locator("input[data-field=qty]").first.fill(str(i + 1))
        cards.nth(i).locator("input[data-field=gross]").fill(str(100 + i))
    assert page.input_value(".pallet-card >> nth=0 >> input[data-field=pallet_no]") == "1 of 3"
    with page.expect_navigation():
        page.click("button:has-text('Предварителен преглед')")
    page.go_back()
    page.wait_for_load_state("load")
    assert page.locator(".pallet-card").count() == 3
    assert page.input_value("[name=client_name]") == "ACME Ltd"
    assert page.input_value("[name=notes]") == "fragile"
    assert page.eval_on_selector_all(
        ".pallet-card table.items tbody input[data-field=order_no]",
        "e => e.map(x => x.value)") == ["PO1", "PO2", "PO3"]
    assert page.eval_on_selector_all(".pallet-card input[data-field=gross]",
                                     "e => e.map(x => x.value)") == ["", "101", "102"]

    with page.expect_navigation():
        page.click("#pallet-issue-btn")
    result_url = page.url
    assert "/pallet/bulk-result" in result_url and _count_pallets(db_module) == 3
    page.go_back()
    page.wait_for_load_state("load")
    with page.expect_navigation():
        page.click("#pallet-issue-btn")
    assert page.url == result_url
    assert _count_pallets(db_module) == 3
    assert "pallet_no" in json.loads(_pallet_docs(db_module)[0]["data"])


def test_e2e_bulk_review_fill_all_and_back_after_issue(page, live_server, db_module, tmp_path):
    """Б5: „Попълни за всички карти“; F6: след издаване „Назад“ не дава
    ERR_CACHE_MISS и повторното „Издай всички“ не създава нови карти."""
    path = tmp_path / "orders.xlsx"
    path.write_bytes(_xlsx([["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", "Pallet"],
                            ["4700", "10", "A1", "d1", 5, 1], ["4700", "20", "A2", "d2", 6, 2],
                            ["4700", "30", "A3", "d3", 7, 3]]))
    conftest.e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill('input[name="client_name"]', "Клиент Е2Е")
    page.set_input_files('input[name="excel_file"]', str(path))
    with page.expect_navigation():
        page.click('button:has-text("Зареди и раздели по палети")')
    page.select_option("#bulk-fill-pallet-type", "120×100")
    page.fill("#bulk-fill-height", "155")
    page.select_option("#bulk-fill-packaging", index=2)
    page.click("[data-fill-apply]")
    assert page.eval_on_selector_all("input[name^=height_]", "e => e.map(x => x.value)") == ["155"] * 3
    assert page.eval_on_selector_all("select[name^=pallet_type_]",
                                     "e => e.map(x => x.value)") == ["120×100"] * 3
    with page.expect_navigation():
        page.click('button:has-text("Издай всички палетни карти")')
    result_url = page.url
    docs = [json.loads(r["data"]) for r in _pallet_docs(db_module)]
    assert [d["height"] for d in docs] == ["155"] * 3
    assert [d["pallet_no"] for d in docs] == ["1 of 3", "2 of 3", "3 of 3"]
    page.go_back()
    page.wait_for_load_state("load")
    assert page.url == result_url          # прегледът води към вече издадения резултат
    assert _count_pallets(db_module) == 3


def test_e2e_packing_list_from_issued_cards_with_auto_totals(page, live_server, admin_client,
                                                             db_module):
    """Б2 + Б3 + UX-6 + Б8/F9: „Опаковъчен лист от тези карти“ зарежда
    всички карти, без празен първи ред; общите суми се попълват сами и не
    презаписват ръчно въведеното."""
    resp = _issue_pallets(admin_client, [_rows(("A1", "d1", "5")), _rows(("A2", "d2", "6"))],
                          client_name="WEG")
    conftest.e2e_login(page, live_server)
    page.goto(live_server + resp.headers["Location"])
    with page.expect_navigation():
        page.click("text=Опаковъчен лист от тези карти")
    page.wait_for_function("document.querySelectorAll('#packing-items tbody tr').length === 2")
    assert "pull=" not in page.url
    assert page.input_value("[name=receiver_name]") == "WEG"
    descs = page.eval_on_selector_all("#packing-items input[data-field=description]",
                                      "e => e.map(x => x.value)")
    assert descs[0].startswith("Pallet 1 of 2 — d1")
    # Бруто 100 на карта (виж _issue_pallets) → сбор 200, със значка „авто“.
    assert page.input_value("[name=total_gross]") == "200"
    assert page.is_visible("label[for=f-total_gross] .packing-auto-badge")
    assert page.input_value("[name=total_volume]") == "2.88"
    page.fill("[name=total_gross]", "230")
    # Ръчно въведеното маха значката „авто“ (тя не бива да се вижда и при
    # празно поле — .badge има display, който надделява над [hidden]).
    assert not page.is_visible("label[for=f-total_gross] .packing-auto-badge")
    page.locator("#packing-items tbody tr").first.locator("input[data-field=gross]").fill("90")
    assert page.input_value("[name=total_gross]") == "230"   # ръчното не се пипа
    # Повторно добавяне на диапазон — вече добавените се пропускат.
    page.fill("#pull-pallet-code", "1-2")
    page.click("#pull-pallet-btn")
    page.wait_for_function("document.getElementById('pull-pallet-msg').textContent.length > 0"
                           " && !document.getElementById('pull-pallet-msg').textContent.includes('Търсене')")
    assert "пропуснати" in page.inner_text("#pull-pallet-msg")
    assert page.locator("#packing-items tbody tr").count() == 2


def test_e2e_invoice_consignee_from_bill_to_and_full_name_in_picker(page, live_server, db_module):
    """UX-5 и Б10/Д4."""
    con = db_module.get_db()
    con.execute("INSERT INTO invoice_clients (name, billing_name, billing_address) VALUES (?, ?, ?)",
                ("ONLYBILL", "Billing Only GmbH", "Street 1\nBerlin"))
    con.commit()
    con.close()
    conftest.e2e_login(page, live_server)
    page.goto(live_server + "/invoice-no/new")
    label = page.eval_on_selector("#f-invoice-client-select option:nth-child(2)", "o => o.textContent")
    assert label == "ONLYBILL — Billing Only GmbH"
    page.select_option("#f-invoice-client-select", index=1)
    assert page.input_value("[name=consignee_name]") == "Billing Only GmbH"
    assert page.input_value("[name=consignee_address]") == "Street 1\nBerlin"
    page.fill("[name=consignee_name]", "")
    page.click("#copy-billto-to-consignee-btn")
    assert page.input_value("[name=consignee_name]") == "Billing Only GmbH"


def test_e2e_negative_packages_blocked_in_cmr(page, live_server):
    """UX-11: отрицателен „Брой колети“ минаваше без предупреждение."""
    conftest.e2e_login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill("[name=packages]", "-3")
    assert page.eval_on_selector("[name=packages]", "e => e.validity.valid") is False
    page.fill("[name=packages]", "3")
    assert page.eval_on_selector("[name=packages]", "e => e.validity.valid") is True
