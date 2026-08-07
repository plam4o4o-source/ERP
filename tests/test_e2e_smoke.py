# -*- coding: utf-8 -*-
"""End-to-end „дим“ тестове с истински headless браузър (Playwright) —
заявка: „направи всичко което предлагаш“ (CI подобрения: Playwright в CI).

За разлика от останалите тестове (Flask test client — вика view функциите
директно, без реален HTTP/браузър), тук стартираме ИСТИНСКИ работещ сървър
(werkzeug make_server, фонова нишка) и го управляваме с истински Chromium
през Playwright — единственият начин да хванем проблеми, които test client-ът
структурно не може: чупещ се JavaScript (напр. initItemsTable в
client_form.html/pallet_form.html), CSS оформление, и — най-важното тук —
дали печатните шаблони РЕАЛНО изглеждат като документ при емулация на
print media (виж ПЛАН_ЗА_РАЗРАБОТКА.md реда за ръчната Playwright проверка
на печатните шаблони, направена еднократно от архитекта — тук е
автоматизирана и тръгва при всеки push).

Изключени по подразбиране от бързия `pytest` (виж pytest.ini/marker "e2e")
— пускат се изрично в CI job "e2e" (.github/workflows/ci.yml), защото
изискват изтеглен браузър и са значително по-бавни от unit/интеграционните
тестове."""
import threading

import pytest

pytestmark = pytest.mark.e2e

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright


@pytest.fixture
def live_server(flask_app, db_module):
    """Истински работещ HTTP сървър (werkzeug), в фонова нишка, срещу
    СЪЩОТО пълно приложение и временна база, каквито ползва `flask_app`
    (виж conftest.py) — само транспортът е реален TCP/HTTP, не Flask test
    client. Портът е 0 (случаен свободен), за да не се сблъска с друг
    паралелно пуснат тест/процес."""
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
        ("e2e_admin", generate_password_hash("e2e-test-password-123"), "E2E Тест"),
    )
    con.commit()
    con.close()

    # НЕ пипаме CSRF защитата — за разлика от Flask test client тестовете
    # (виж conftest.post_with_csrf), тук истински браузър зарежда истинска
    # страница с вече вградения `csrf_token()` в скрито поле на формата и
    # го изпраща естествено при submit; appcore._check_csrf е собствена
    # проверка, не през Flask-WTF, затова WTF_CSRF_ENABLED не важи за нея.
    server = make_server("127.0.0.1", 0, flask_app)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def page(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def _login(page, base_url):
    page.goto(base_url + "/login")
    page.fill('input[name="username"]', "e2e_admin")
    page.fill('input[name="password"]', "e2e-test-password-123")
    page.click('button[type="submit"]')
    page.wait_for_url(base_url + "/")


def test_login_and_dashboard_loads(page, live_server):
    _login(page, live_server)
    assert "Табло" in page.title()
    assert page.locator("text=Статистика за текущия месец").count() == 1


def test_issue_cmr_end_to_end_and_barcode_renders(page, live_server):
    """Пълен път: вход → форма за ново ЧМР → издаване → печатна страница —
    точно потокът, който Playwright ръчно провери еднократно при
    първоначалния одит (виж ПЛАН_ЗА_РАЗРАБОТКА.md), сега автоматизиран."""
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill('input[name="sender_name"]', "Изпращач ЕООД")
    page.fill('input[name="consignee_name"]', "Е2Е Тест Клиент ЕООД")
    page.click('button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")
    assert "ЧМР" in page.content()
    assert "Е2Е Тест Клиент ЕООД" in page.content()
    # Баркодът е вграден SVG (barcode128.code128_svg през appcore.barcode_filter,
    # виж appcore.py) — реално рендериран от браузъра, не само присъстващ в HTML.
    assert page.locator("svg rect").count() > 0


def test_print_media_emulation_renders_document(page, live_server):
    """Емулация на print media (истинската проверка, направена ръчно от
    архитекта при одита) — печатната бланка трябва да остане видима и без
    екранните елементи (лентата с бутони е class="no-print")."""
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill('input[name="sender_name"]', "Изпращач ЕООД")
    page.fill('input[name="consignee_name"]', "Печатен Тест ЕООД")
    page.click('button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")
    page.emulate_media(media="print")
    assert page.locator(".cmr").first.is_visible()
    assert not page.locator(".doc-toolbar").first.is_visible()


def test_client_history_card_renders_in_browser(page, live_server):
    """Регресия за v3.33.0 (История на документите от картата на клиента)
    — реално рендерирана в браузър, не само през test client."""
    _login(page, live_server)
    page.goto(live_server + "/clients/new")
    page.fill('input[name="name"]', "Е2Е Браузър Клиент ЕООД")
    page.click('button[type="submit"]')
    page.wait_for_url(live_server + "/clients")
    row = page.locator("tr", has_text="Е2Е Браузър Клиент ЕООД")
    row.get_by_text("Редакция").click()
    assert page.locator("text=Последни документи на този клиент").count() == 1
    assert page.locator("text=Все още няма издадени документи").count() == 1
