# -*- coding: utf-8 -*-
"""Одит (13.10.2026, група FORMS, v3.81.0) — в истински браузър: авто сборът
на опаковъчния лист (десетична запетая, бележка за ред без размери, следване
на редовете при редакция), диапазон от палетни карти (едно съобщение,
ненамерените остават в полето) и партида палетни карти, изпратена с изтекла
сесия — възстановена след входа, а `next` пази параметрите на адреса."""
import pytest
from openpyxl import Workbook

from conftest import E2E_PASSWORD, E2E_USERNAME, e2e_login

pytestmark = pytest.mark.e2e


def _cell(page, row, field):
    return page.locator("#packing-items tbody tr").nth(row).locator(
        'input[data-field="%s"]' % field)


def _fill_row(page, row, **values):
    for field, value in values.items():
        _cell(page, row, field).fill(value)


def test_packing_auto_totals_follow_rows_with_comma_and_explain_gaps(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/packing/new")
    page.fill("#f-receiver_name", "Тест Получател")
    _fill_row(page, 0, description="Палет 1", length="1200", width="800", height="1000",
              net="280,25", gross="300,5")
    page.click('[data-add-row="packing-items"]')
    _fill_row(page, 1, description="Палет 2", length="1200", width="800", height="1500",
              net="1200", gross="1302")
    assert page.input_value("[name=total_gross]") == "1602,5", "без поправката: 1602.5"
    # Одит (21.10.2026, APPFIX-9): формата е със запетаи — и обемът (от Д×Ш×В) с запетая.
    assert page.input_value("[name=total_volume]") == "2,4"
    assert page.locator('[data-packing-sum="gross"]').inner_text() == "Сбор от редовете: 1602,5"
    page.click('[data-add-row="packing-items"]')
    _fill_row(page, 2, description="Кашон", net="1,5", gross="2")
    assert page.input_value("[name=total_volume]") == ""
    note = page.locator("#f-total_volume ~ .packing-auto-note")
    assert note.is_visible() and "№ 3" in note.inner_text() and "размерите" in note.inner_text()
    page.click('#main-doc-form button[type=submit]:not([formaction])')
    page.wait_for_url(live_server + "/doc/*")

    page.goto(page.url + "/edit")
    assert page.input_value("[name=total_gross]") == "1604,5"
    assert page.locator('label[for="f-total_gross"] .packing-auto-badge').is_visible()
    _cell(page, 2, "gross").fill("10")
    assert page.input_value("[name=total_gross]") == "1612,5", \
        "без поправката общото остава 1604,5 и сървърът предупреждава за разминаване"
    page.fill("[name=total_net]", "1500")             # ръчно общо — не се пипа
    _cell(page, 2, "net").fill("3")
    assert page.input_value("[name=total_net]") == "1500"


def test_pallet_range_gives_one_message_and_keeps_missing_cards(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill("[name=client_name]", "Клиент П")
    first = page.locator("table.items tbody tr").first
    first.locator('input[data-field="order_no"]').fill("4700")
    first.locator('input[data-field="qty"]').fill("5")
    page.click('#main-doc-form button[type=submit]:not([formaction])')
    page.wait_for_url(live_server + "/doc/*")

    page.goto(live_server + "/packing/new")
    code = page.locator("#pull-pallet-code")
    code.fill("1-3")
    code.press("Enter")
    page.wait_for_function("() => document.body.innerText.includes('Няма палетни карти')")
    body = page.inner_text("main")
    assert "Добавен 1 ред от палетна карта" in body
    assert body.count("Няма палетн") == 1 and "2, 3" in body
    assert code.input_value() == "2, 3", "ненамерените остават в полето"
    code.fill("1-500")
    code.press("Enter")
    page.wait_for_function("() => document.body.innerText.includes('твърде голям')")
    assert code.input_value() == "1-500"


def test_bulk_pallet_batch_survives_an_expired_session(page, live_server, tmp_path):
    wb = Workbook()
    ws = wb.active
    for r in (["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", "Pallet"],
              ["4700", "10", "A1", "d1", 5, 1], ["4700", "20", "A2", "d2", 6, 2]):
        ws.append(r)
    path = str(tmp_path / "orders.xlsx")
    wb.save(path)
    e2e_login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.set_input_files("input[name=excel_file]", path)
    page.click("text=Зареди и раздели по палети")
    page.wait_for_selector("#bulk-form")
    page.fill("#bulk-form input[name=client_name]", "Клиент Партида ЕООД")
    page.fill("#f-ref_cmr", "CMR-2026-55")
    page.select_option("#bulk-fill-pallet-type", "120×80")
    page.fill("#bulk-fill-height", "150")
    page.click("[data-fill-apply]")
    page.fill('input[name="gross_1"]', "351,5")
    page.fill('input[name="gross_2"]', "222")
    page.context.clear_cookies()                       # изтекла сесия
    page.click("#bulk-form button[type=submit]:not([formaction])")
    page.wait_for_url("**/login?**")
    page.fill('input[name="username"]', E2E_USERNAME)
    page.fill('input[name="password"]', E2E_PASSWORD)
    page.click('main button[type="submit"]')
    page.wait_for_url("**/pallet/bulk-review/restore/**")
    assert page.input_value('input[name="gross_1"]') == "351,5"
    assert page.input_value('input[name="gross_2"]') == "222"
    assert page.input_value("#f-ref_cmr") == "CMR-2026-55"
    assert page.input_value('input[name="height_2"]') == "150"
    page.click("#bulk-form button[type=submit]:not([formaction])")
    page.wait_for_url("**/pallet/bulk-result?ids=*")

    result = page.url                                  # FORMS-5: next с ?ids=
    page.context.clear_cookies()
    page.goto(result)
    page.wait_for_url("**/login?**")
    page.fill('input[name="username"]', E2E_USERNAME)
    page.fill('input[name="password"]', E2E_PASSWORD)
    page.click('main button[type="submit"]')
    page.wait_for_url(result)
