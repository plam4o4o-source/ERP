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
# ---------------------------------------------------------------- фактури
# Автоматичното попълване от справочника материали и зареждането на всички
# редове от палетна карта са чисто клиентски (JS + fetch) — Flask test
# client не изпълнява JavaScript, затова истинският браузър е ЕДИНСТВЕНИЯТ
# начин да се провери, че реално работят на екрана.

def _load_materials_catalog(page, live_server, tmp_path, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["ABB part ID", "Description", "Net weight\n[KG/pc]"])
    for row in rows:
        ws.append(list(row))
    path = tmp_path / "kg.xlsx"
    wb.save(str(path))

    page.goto(live_server + "/materials")
    page.set_input_files('input[name="excel_file"]', str(path))
    page.click('button:has-text("Зареди справочника")')
    page.wait_for_load_state("networkidle")


def test_invoice_material_code_autofills_net_weight_from_catalog(page, live_server, tmp_path):
    """Заявка: „от файла с килограмите автоматично да се извличат
    съответните килограми във фактурата“ — при въвеждане на код на
    материала полето „Net weight“ се попълва само."""
    _login(page, live_server)
    _load_materials_catalog(page, live_server, tmp_path,
                            [("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21)])

    page.goto(live_server + "/invoice-br/new")
    row = page.locator("#invoice-br-items tbody tr").first
    code_input = row.locator('input[data-field="material_code"]')
    weight_input = row.locator('input[data-field="net_weight"]')
    assert weight_input.input_value() == ""

    code_input.fill("GLBK400002P0012")
    code_input.blur()  # попълването се задейства при change, не при всеки натиснат клавиш
    page.wait_for_function(
        """() => document.querySelector('#invoice-br-items tbody tr input[data-field="net_weight"]').value !== ''""",
        timeout=5000)
    assert weight_input.input_value() == "2.21"


def test_invoice_autofill_never_overwrites_a_manually_typed_value(page, live_server, tmp_path):
    """Ръчно въведеното тегло е за конкретната пратка и трябва да
    надделява над справочника — иначе операторът би го губил при всяко
    поправяне на кода."""
    _login(page, live_server)
    _load_materials_catalog(page, live_server, tmp_path,
                            [("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21)])

    page.goto(live_server + "/invoice-br/new")
    row = page.locator("#invoice-br-items tbody tr").first
    row.locator('input[data-field="net_weight"]').fill("9.99")
    code_input = row.locator('input[data-field="material_code"]')
    code_input.fill("GLBK400002P0012")
    code_input.blur()
    page.wait_for_timeout(600)
    assert row.locator('input[data-field="net_weight"]').input_value() == "9.99"


def test_invoice_loads_all_rows_from_an_issued_pallet_card(page, live_server, tmp_path):
    """Заявка: „фактурата да може да се зарежда, както се зареждат
    палетните карти в опаковъчния лист“ — но с ВСИЧКИ редове поотделно и с
    тегло, изтеглено от справочника."""
    _login(page, live_server)
    _load_materials_catalog(page, live_server, tmp_path, [
        ("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21),
        ("GLBK400001P0200", "C-PROFILE 2    200MM", 0.383),
    ])

    # Издаваме палетна карта във формат „поръчки“ по реалния път — импорт
    # на справка за поръчки от Excel (форматът „поръчки“ се получава само
    # оттам, виж pallet_form.html), после масово издаване.
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", ""])
    ws.append(["4700200362", "30", "GLBK400002P0012", "C-Profile", 20, 1])
    ws.append(["4700200362", "40", "GLBK400001P0200", "C-Profile 2", 400, 1])
    orders_path = tmp_path / "poruchki_invoice.xlsx"
    wb.save(str(orders_path))

    page.goto(live_server + "/pallet/new")
    page.set_input_files('input[name="excel_file"]', str(orders_path))
    page.click('button:has-text("Зареди и раздели по палети")')
    page.wait_for_url(live_server + "/pallet/bulk-import*", timeout=10000)
    page.fill('input[name="client_name"]', "ABB")
    page.click('button:has-text("Издай всички палетни карти")')
    page.wait_for_load_state("networkidle")

    number = page.locator("table.list tbody tr td, table.list tr td").first.inner_text().strip()
    assert "/" in number, "очаква се номер на издадена палетна карта, а не %r" % number

    # Зареждаме я във фактурата.
    page.goto(live_server + "/invoice-br/new")
    page.fill("#f-pull-invoice-code", number)
    page.click(".invoice-pull-btn")
    page.wait_for_function(
        """() => document.querySelectorAll('#invoice-br-items tbody tr').length >= 3""",
        timeout=8000)

    # Първият ред е празният начален — двата заредени идват след него.
    loaded = page.locator("#invoice-br-items tbody tr")
    codes = [loaded.nth(i).locator('input[data-field="material_code"]').input_value()
             for i in range(loaded.count())]
    assert "GLBK400002P0012" in codes
    assert "GLBK400001P0200" in codes

    idx = codes.index("GLBK400002P0012")
    filled = loaded.nth(idx)
    assert filled.locator('input[data-field="po_no"]').input_value() == "4700200362"
    assert filled.locator('input[data-field="pos"]').input_value() == "30"
    assert filled.locator('input[data-field="qty"]').input_value() == "20"
    assert filled.locator('input[data-field="net_weight"]').input_value() == "2.21", \
        "теглото трябва да е изтеглено автоматично от справочника"


def test_invoice_address_book_selection_fills_both_address_blocks(page, live_server):
    """Заявка: „в раздел Фактури добави адресна книга; да съдържа данните за
    фактуриране на клиентите и също да има адрес за доставка“ — един избор
    попълва И блока Consignee, И блока Bill To. Чисто клиентско JS
    поведение (bindInvoiceClientSelect в app.js)."""
    _login(page, live_server)
    page.goto(live_server + "/invoices/clients/new")
    page.fill('input[name="name"]', "ABB Бразилия — Sorocaba")
    page.fill('input[name="delivery_name"]', "ABB ELETRIFICACAO LTDA")
    page.fill('textarea[name="delivery_address"]', "Rod.Sen. KM 11\nSorocaba - SP")
    page.fill('input[name="delivery_phone"]', "+55 11 97613-8155")
    page.fill('input[name="billing_name"]', "ABB ELETRIFICACAO LTDA - CNPJ")
    page.fill('textarea[name="billing_address"]', "Fakturamottak\n18087-125 Sorocaba")
    page.fill('input[name="billing_phone"]', "+55 15 3330-6465")
    page.click('button[type="submit"]')
    page.wait_for_url(live_server + "/invoices/clients")

    page.goto(live_server + "/invoice-br/new")
    page.select_option("#f-invoice-client-select", label="ABB Бразилия — Sorocaba")

    assert page.locator('input[name="consignee_name"]').input_value() == "ABB ELETRIFICACAO LTDA"
    assert "Sorocaba - SP" in page.locator('textarea[name="consignee_address"]').input_value()
    assert page.locator('input[name="consignee_phone"]').input_value() == "+55 11 97613-8155"
    assert page.locator('input[name="billto_name"]').input_value() == "ABB ELETRIFICACAO LTDA - CNPJ"
    assert "Fakturamottak" in page.locator('textarea[name="billto_address"]').input_value()
    assert page.locator('input[name="billto_phone"]').input_value() == "+55 15 3330-6465"


def test_invoice_excel_import_adds_rows_without_losing_typed_data(page, live_server, tmp_path):
    """Заявка: „зареждането на материалите във фактурата могат да се
    зареждат и от Excel файл“ — файлът се качва през fetch, за да НЕ се
    губи вече въведеното в останалите полета на формата (номер, получател).
    Точно това се проверява тук."""
    from openpyxl import Workbook

    _login(page, live_server)
    _load_materials_catalog(page, live_server, tmp_path,
                            [("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21)])

    wb = Workbook()
    ws = wb.active
    ws.append(["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", "Unit Price"])
    ws.append(["4700200362", "30", "GLBK400002P0012", "C-Profile", 20, 13.66])
    path = tmp_path / "invoice_items.xlsx"
    wb.save(str(path))

    page.goto(live_server + "/invoice-br/new")
    page.fill('input[name="invoice_number"]', "0000012955")
    page.fill('input[name="consignee_name"]', "ABB ELETRIFICACAO LTDA")

    page.set_input_files("#f-invoice-excel-invoice-br-items", str(path))
    page.click(".invoice-excel-btn")
    page.wait_for_function(
        """() => document.querySelectorAll('#invoice-br-items tbody tr').length >= 2""",
        timeout=8000)

    rows = page.locator("#invoice-br-items tbody tr")
    codes = [rows.nth(i).locator('input[data-field="material_code"]').input_value()
             for i in range(rows.count())]
    assert "GLBK400002P0012" in codes
    loaded = rows.nth(codes.index("GLBK400002P0012"))
    assert loaded.locator('input[data-field="qty"]').input_value() == "20"
    assert loaded.locator('input[data-field="unit_price"]').input_value() == "13.66"
    assert loaded.locator('input[data-field="net_weight"]').input_value() == "2.21"

    # Вече въведеното НЕ е загубено — това е причината да е през fetch.
    assert page.locator('input[name="invoice_number"]').input_value() == "0000012955"
    assert page.locator('input[name="consignee_name"]').input_value() == "ABB ELETRIFICACAO LTDA"


def test_issued_invoice_is_absent_from_general_documents_list(page, live_server):
    """Заявка: „само там да се появяват издадените фактури“ — проверено в
    реален браузър от край до край (издаване → двата списъка)."""
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    page.fill('input[name="invoice_number"]', "E2E-INV-1")
    page.fill('input[name="consignee_name"]', "Е2Е Фактурен Клиент")
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")

    page.goto(live_server + "/invoices")
    assert "E2E-INV-1" in page.content()

    page.goto(live_server + "/docs")
    body = page.content()
    assert "E2E-INV-1" not in body
    assert "Е2Е Фактурен Клиент" not in body


def test_manual_pallet_entry_uses_order_columns_and_feeds_invoices(page, live_server, tmp_path):
    """Заявка: „палетна карта да съдържа в съдържание на палета Order No,
    Pos, Reference, Reference Desc, Qty“ — при РЪЧНО въвеждане. Реалният
    браузър е нужен, защото имената на полетата (вкл. items_format=orders)
    се задават от JS (initPalletMultiCard/renumber) чак при зареждане на
    страницата — test client не би хванал счупване там. Проверява и
    най-ценната последица: ръчно въведената карта се влива във фактура с
    Reference → Material code и тегло от справочника."""
    _login(page, live_server)
    _load_materials_catalog(page, live_server, tmp_path,
                            [("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21)])

    page.goto(live_server + "/pallet/new")
    tr = page.locator("#pallet-items tbody tr").first
    for field, value in (("order_no", "4700200362"), ("pos", "30"),
                         ("reference", "GLBK400002P0012"),
                         ("reference_desc", "C-Profile"), ("qty", "20")):
        tr.locator('input[data-field="%s"]' % field).fill(value)
    page.fill('input[name="client_name"]', "Ръчен Е2Е Клиент")
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")

    body = page.content()
    assert "Order No" in body
    assert "GLBK400002P0012" in body
    # НЕ през text= локатор — той е нечувствителен към регистъра и хваща
    # първо flash съобщението „Палетна карта № … е издадена“, чиято опашка
    # разваля номера. Заглавието на бланката е с главни букви и е уникално
    # в суровия HTML.
    number = body.split("ПАЛЕТНА КАРТА № ")[1][:9].strip()

    # Ръчната карта се зарежда във фактура точно като импортираната.
    page.goto(live_server + "/invoice-br/new")
    page.fill("#f-pull-invoice-code", number)
    page.click(".invoice-pull-btn")
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll(
              '#invoice-br-items tbody tr input[data-field="material_code"]'))
              .some(i => i.value === 'GLBK400002P0012')""",
        timeout=8000)
    rows = page.locator("#invoice-br-items tbody tr")
    codes = [rows.nth(i).locator('input[data-field="material_code"]').input_value()
             for i in range(rows.count())]
    loaded = rows.nth(codes.index("GLBK400002P0012"))
    assert loaded.locator('input[data-field="po_no"]').input_value() == "4700200362"
    assert loaded.locator('input[data-field="qty"]').input_value() == "20"
    assert loaded.locator('input[data-field="net_weight"]').input_value() == "2.21", \
        "теглото идва от справочника — ръчната карта е пълноценен формат orders"


def test_invoice_new_rows_get_hs_code_prefilled(page, live_server):
    """Заявка: „в фактурите по подразбиране винаги да се поставя
    автоматично HS code 85389099“ — и началният празен ред, и ред от
    „+ Добави ред“. Попълва се от JS (initItemsTable/data-row-defaults),
    затова е нужен реален браузър."""
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")

    first = page.locator("#invoice-br-items tbody tr").first
    assert first.locator('input[data-field="hs_code"]').input_value() == "85389099"
    # Останалите колони на празния ред са си празни — подразбирането важи
    # само за HS кода.
    assert first.locator('input[data-field="material_code"]').input_value() == ""

    page.click('[data-add-row="invoice-br-items"]')
    rows = page.locator("#invoice-br-items tbody tr")
    assert rows.count() == 2
    assert rows.nth(1).locator('input[data-field="hs_code"]').input_value() == "85389099"


def test_invoice_pull_with_two_orders_shows_picker_and_loads_one(page, live_server, tmp_path):
    """Заявка: „във фактури един номер на поръчка да бъде на една фактура“
    — палетна карта с 2 поръчки НЕ се излива наведнъж: появява се избор
    (чист JS, renderInvoicePoChoice), зареждат се само редовете на
    избраната поръчка, а съобщението казва коя остава за отделна фактура."""
    _login(page, live_server)
    _load_materials_catalog(page, live_server, tmp_path, [
        ("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21),
        ("1TFL151621P0550", "transverse section 06  folded", 2.74),
    ])

    # Ръчна палетна карта с редове от ДВЕ поръчки.
    page.goto(live_server + "/pallet/new")
    rows = [("4700201619", "10", "GLBK400002P0012", "C-Profile", "20"),
            ("4700223566", "10", "1TFL151621P0550", "Секция", "7")]
    for i, (order, pos, ref, desc, qty) in enumerate(rows):
        if i:
            page.click('[data-add-row="pallet-items"]')
        tr = page.locator("#pallet-items tbody tr").nth(i)
        for field, value in (("order_no", order), ("pos", pos), ("reference", ref),
                             ("reference_desc", desc), ("qty", qty)):
            tr.locator('input[data-field="%s"]' % field).fill(value)
    page.fill('input[name="client_name"]', "Двупоръчков Клиент")
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")
    number = page.content().split("ПАЛЕТНА КАРТА № ")[1][:9].strip()

    page.goto(live_server + "/invoice-br/new")
    page.fill("#f-pull-invoice-code", number)
    page.click(".invoice-pull-btn")

    # Появява се изборът на поръчка, нищо още не е заредено.
    picker = page.locator(".invoice-pull-msg select")
    picker.wait_for(timeout=8000)
    options = picker.locator("option")
    assert options.count() == 2
    assert "4700201619" in options.nth(0).inner_text()
    assert "4700223566" in options.nth(1).inner_text()
    material_inputs = page.locator(
        '#invoice-br-items tbody tr input[data-field="material_code"]')
    values = [material_inputs.nth(i).input_value() for i in range(material_inputs.count())]
    assert all(v == "" for v in values), "преди избора не се зарежда нищо"

    # Избираме втората поръчка — зареждат се САМО нейните редове.
    picker.select_option("4700223566")
    page.click(".invoice-pull-msg button")
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll(
              '#invoice-br-items tbody tr input[data-field="material_code"]'))
              .some(i => i.value === '1TFL151621P0550')""",
        timeout=8000)
    values = [material_inputs.nth(i).input_value() for i in range(material_inputs.count())]
    assert "1TFL151621P0550" in values
    assert "GLBK400002P0012" not in values, "другата поръчка НЕ се зарежда тук"

    msg = page.locator(".invoice-pull-msg").inner_text()
    assert "4700201619" in msg, "съобщението казва коя поръчка остава за отделна фактура"


def test_transportation_way_dropdown_defaults_to_airfreight_and_can_switch_to_maritime(page, live_server):
    """Заявка: „за фактурите за Бразилия да се добави Transportation Way:
    AIRFREIGHT / FCA, Transportation Way: Maritime / FCA. По подразбиране
    да излиза AIRFREIGHT / FCA.“ Проверено в реален браузър, защото самият
    избор (стойността, реално подадена при submit) минава през нативен
    <select>, не просто присъствие на текст в HTML-а."""
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    select = page.locator('select[name="transport_way"]')
    assert select.input_value() == "AIRFREIGHT / FCA"

    select.select_option("Maritime / FCA")
    page.fill('input[name="invoice_number"]', "МОРСКИ-1")
    page.fill('input[name="consignee_name"]', "Морски Клиент ЕООД")
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")
    assert "Transportation Way:" in page.content()
    assert "Maritime / FCA" in page.content()


def test_editing_brazil_invoice_with_a_non_standard_transport_way_shows_it_selected(page, live_server):
    """Стара/нестандартна стойност на „Вид транспорт“ (извън двата
    варианта в менюто — напр. фактура, издадена преди то да съществува)
    не бива тихо да изчезне при отваряне за редакция: JS-ът
    (injectAndSelectOption в app.js) добавя стойността като допълнителна
    опция в менюто и я маркира като избрана, вместо селектът да остане
    без видим избор."""
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    token = page.locator('#main-doc-form input[name="csrf_token"]').input_value()
    # Пращаме директно през HTTP (заобикаляйки самото падащо меню в UI) —
    # симулира стойност, записана преди менюто да съществува.
    page.request.post(live_server + "/invoice-br/new", form={
        "csrf_token": token,
        "invoice_number": "ЛЕГАСИ-1",
        "consignee_name": "Легаси Клиент ЕООД",
        "transport_way": "SEAROUTE / DAP",
    })

    page.goto(live_server + "/invoices")
    row = page.locator("tr", has_text="Легаси Клиент ЕООД")
    row.get_by_text("Редактирай").click()
    select = page.locator('select[name="transport_way"]')
    select.wait_for(timeout=8000)
    assert select.input_value() == "SEAROUTE / DAP"
    assert select.locator('option[value="SEAROUTE / DAP"]').count() == 1


def test_scanning_the_qr_public_link_opens_the_document_without_login(page, live_server, db_module):
    """Заявка: „всеки, който сканира с телефон баркода на някой от
    документите, да му се зареди директно документа, без да има нужда от
    домейна, който е в програмата“ + уточнение „само документа, нищо друго
    да не вижда“.

    Вместо реално OCR/QR декодиране на картинката (излишна тежка
    зависимост само за теста), взима public_token директно от базата —
    точно каквото QR картинката носи кодирано (виж qr_code.qr_png_data_uri)
    — и го отваря в СЪВСЕМ НОВ браузърен контекст, БЕЗ бисквитки от
    логнатата сесия по-горе — точно сценарият на чужд телефон, никога не
    влизал в програмата."""
    _login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill('input[name="sender_name"]', "Изпращач ЕООД")
    page.fill('input[name="consignee_name"]', "QR Е2Е Клиент ЕООД")
    page.click('button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")

    # QR-ът трябва да се вижда РЕАЛНО в браузъра — валиден data: URI, не
    # счупена картинка (проверка, която суров HTML тест не покрива).
    qr_img = page.locator(".doc-qr img")
    assert qr_img.count() == 1
    assert qr_img.get_attribute("src").startswith("data:image/png;base64,")

    con = db_module.get_db()
    row = con.execute(
        "SELECT public_token FROM documents WHERE data LIKE ?",
        ("%QR Е2Е Клиент ЕООД%",),
    ).fetchone()
    con.close()
    assert row and row["public_token"]

    context = page.context.browser.new_context()
    try:
        anon_page = context.new_page()
        anon_page.goto(live_server + "/p/" + row["public_token"])
        assert "QR Е2Е Клиент ЕООД" in anon_page.content()
        assert "ЧМР" in anon_page.content()
        assert anon_page.locator("#sidebar").count() == 0
        assert anon_page.locator(".doc-toolbar").count() == 0
    finally:
        context.close()


def test_unknown_qr_token_shows_404_without_login(page, live_server):
    context = page.context.browser.new_context()
    try:
        anon_page = context.new_page()
        resp = anon_page.goto(live_server + "/p/no-such-token-exists")
        assert resp.status == 404
    finally:
        context.close()
