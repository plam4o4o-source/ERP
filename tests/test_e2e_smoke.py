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

_XLSX_HEADERS = ["Due Date", "Order No", "Pos", "Project", "Reference",
                 "Reference Desc", "Open Qty", "Unit", "Stock", ""]
_XLSX_ROW = ["2026-09-01", "E2E-ORD-1", "10", "PRJ-1", "REF-1",
            "Материал за Е2Е тест", 6, "PCS", "WH1", 1]

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


def test_cmr_load_place_from_sender_button_fills_place_loading(page, live_server):
    """Заявка: „товарен пункт да се зарежда от фирма изпращач, но да има
    опция и ръчно въвеждане“ — бутон „Зареди от изпращача“ до поле 4
    „Товарен пункт“ (JS, initCmrPlaces в app.js) копира текущите стойности
    на поле 1 „Изпращач“ в текстовото поле place_loading, САМО при
    натискане (не автоматично), и полето остава свободно за ръчна промяна
    след това. Чисто клиентско JS поведение — Flask test client не
    изпълнява истински JavaScript, затова е нужен реален браузър тук."""
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill('input[name="sender_name"]', "Товарач ЕООД")
    page.fill('input[name="sender_address"]', "ул. Складова 5")
    page.fill('input[name="sender_city"]', "4000 Пловдив")
    page.fill('input[name="sender_country"]', "България")

    # Преди натискане на бутона полето за товарен пункт не е пипано.
    assert page.locator('input[name="place_loading"]').input_value() == ""

    page.click("#load-place-from-sender-btn")
    assert page.locator('input[name="place_loading"]').input_value() == \
        "Товарач ЕООД — ул. Складова 5, 4000 Пловдив, България"

    # Ръчно въвеждане/промяна остава напълно свободно и след бутона.
    page.fill('input[name="place_loading"]', "Друг склад, ръчно въведен")
    assert page.locator('input[name="place_loading"]').input_value() == "Друг склад, ръчно въведен"


def test_pallet_bulk_import_preview_keeps_loaded_data(page, live_server, tmp_path):
    """Регресия за реален бъг: при импорт на Excel файл в палетна карта и
    последващ „Предварителен преглед“, заредените редове изчезваха и се
    показваше „Няма палетни карти за преглед“ — заявка: „при зареждане на
    файл в палетна карта и избор на преглед, изтрива се заредената
    информация“.

    Причината беше чисто клиентска (JS): initItemsTable-ът в
    pallet_bulk_review.html не задаваше table.dataset.hiddenField, а
    handler-ът при submit четеше единствено него (с твърдо закодиран
    резервен вариант "items_json", който не съществува тук — истинските
    полета са "items_json_1", "items_json_2" и т.н.) — Flask test client
    тестовете не изпълняват JS и затова не хващаха това, изисква се
    истински браузър (виж static/app.js, initItemsTable)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(_XLSX_HEADERS)
    ws.append(_XLSX_ROW)
    xlsx_path = tmp_path / "poruchki.xlsx"
    wb.save(str(xlsx_path))

    _login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.set_input_files('input[name="excel_file"]', str(xlsx_path))
    page.click('button:has-text("Зареди и раздели по палети")')
    page.wait_for_url(live_server + "/pallet/bulk-import" + "*", timeout=10000)

    # Редът от файла трябва да се вижда в самата таблица за преглед/редакция
    # (стойността е зададена през JS .value, не HTML value= атрибут, затова
    # четем я през input_value(), не CSS [value=...] селектор).
    first_input = page.locator("#pallet-items-1 tbody tr").first.locator("input").first
    assert first_input.input_value() == "E2E-ORD-1"

    page.fill('input[name="client_name"]', "Клиент Е2Е Bulk")
    page.click('button:has-text("Предварителен преглед")')
    page.wait_for_load_state("networkidle")

    body = page.content()
    assert "Няма палетни карти за преглед" not in body
    assert "E2E-ORD-1" in body
    assert "Материал за Е2Е тест" in body


def test_pallet_label_barcode_fits_within_label_width(page, live_server):
    """Не беше в обхвата на патча: v3.38.0 удължи баркода (вече съдържа и
    пълната дата — „PAL-07082026-0001“ вместо „PAL-2026-0001“), което го
    прави ~22% по-широк. Долният голям баркод на палетната карта се рисува
    БЕЗ responsive=True (фиксирана ширина в px), а етикетният формат е само
    100мм широк — затова тук измерваме реално в браузър, че баркодът се
    събира в листа и няма да се отреже при печат.

    Заглавният баркод е защитен от `.plt-head-barcode svg { max-width:100% }`
    в style.css; долният нямаше такова правило."""
    _login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.fill('input[name="client_name"]', "Клиент Етикет")
    # Страницата има ДВЕ форми (импортът от Excel е отделна) — издаването
    # е това на главната форма, затова селекторът е с #main-doc-form.
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")

    page.goto(page.url + "?format=label")
    page.emulate_media(media="print")

    box = page.eval_on_selector(
        ".plt-big-barcode svg",
        "el => { const r = el.getBoundingClientRect();"
        " const p = el.closest('.print-page').getBoundingClientRect();"
        " return {svg: r.width, page: p.width}; }",
    )
    assert box["svg"] <= box["page"], (
        "баркодът (%.0fpx) прелива извън етикета (%.0fpx) и ще се отреже при печат"
        % (box["svg"], box["page"])
    )
