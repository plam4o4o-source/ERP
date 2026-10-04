# -*- coding: utf-8 -*-
"""Одит (04.10.2026, R1/F4) — в истински браузър: попълнено ЧМР, изпратено
след изход в друг раздел или след изтекла/изтрита бисквитка, не се губи —
след повторен вход формата се връща попълнена. Плюс: всички основни
страници работят с новата Content-Security-Policy (S1)."""
import pytest

from conftest import E2E_PASSWORD, E2E_USERNAME, e2e_login

pytestmark = pytest.mark.e2e


def _fill_cmr(pg, base_url):
    pg.goto(base_url + "/cmr/new")
    pg.fill('input[name="consignee_name"]', "Спасен Получател ООД")
    pg.fill('input[name="consignee_city"]', "Пловдив")
    pg.fill('input[name="truck_reg"]', "PB 1234 AB")


def _login_again(pg, base_url):
    assert "/login" in pg.url
    assert "попълнената форма е запазена" in pg.content()
    pg.fill('input[name="username"]', E2E_USERNAME)
    pg.fill('input[name="password"]', E2E_PASSWORD)
    pg.click('main button[type="submit"]')
    pg.wait_for_url("**/cmr/new?restore=*")


def _assert_restored(pg):
    assert pg.input_value('input[name="consignee_name"]') == "Спасен Получател ООД"
    assert pg.input_value('input[name="consignee_city"]') == "Пловдив"
    assert pg.input_value('input[name="truck_reg"]') == "PB 1234 AB"
    assert "Попълнената форма е възстановена" in pg.content()


def test_cmr_survives_logout_in_another_tab(page, live_server):
    e2e_login(page, live_server)
    _fill_cmr(page, live_server)
    other = page.context.new_page()            # втори раздел, същите бисквитки
    other.goto(live_server + "/")
    other.click('.sidebar-user button[type="submit"]')   # „Изход“
    other.wait_for_url("**/login")
    page.click('#main-doc-form button[type="submit"]:not([formaction])')
    page.wait_for_url("**/login?**")
    _login_again(page, live_server)
    _assert_restored(page)


def test_cmr_survives_a_deleted_session_cookie(page, live_server):
    e2e_login(page, live_server)
    _fill_cmr(page, live_server)
    page.context.clear_cookies()                # изтекла 12-часова сесия
    page.click('#main-doc-form button[type="submit"]:not([formaction])')
    page.wait_for_url("**/login?**")
    _login_again(page, live_server)
    _assert_restored(page)
    # И формата наистина се издава след възстановяването.
    page.click('#main-doc-form button[type="submit"]:not([formaction])')
    page.wait_for_url("**/doc/*")


def test_pages_work_with_the_content_security_policy(page, live_server):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    e2e_login(page, live_server)
    for path in ("/", "/cmr/new", "/packing/new", "/pallet/new", "/invoice-br/new", "/docs",
                 "/clients", "/clients/new", "/materials", "/my-settings", "/settings",
                 "/admin/users", "/admin/system", "/invoices", "/invoices/clients"):
        resp = page.goto(live_server + path)
        assert resp.status == 200, path
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
    # Предварителен преглед (POST към друг адрес през formaction) и печатът.
    page.goto(live_server + "/cmr/new")
    page.fill('input[name="consignee_name"]', "CSP Проба")
    page.click('#main-doc-form button[formaction]')
    page.wait_for_url("**/preview/*")
    assert "CSP Проба" in page.content()
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
