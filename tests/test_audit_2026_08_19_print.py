# -*- coding: utf-8 -*-
"""Регресионни тестове за четири находки от одита на 19.08.2026:

* №11 (висока) — ЧМР се разливаше на ВТОРА страница при обичайно дълго
  описание на стоката (официалната бланка е едностранична);
* №12 (висока) — многостранични таблици без повтарящ се заглавен ред, без
  идентификация на документа и с физически разрязани редове;
* №33 (средна) — модалите се обявяваха като `role="dialog" aria-modal`, но
  нямаха никакво управление на фокуса;
* №34 (средна) — хоризонтално преливане на страницата на телефон (375px)
  точно в екраните, в които се сканира и въвежда в движение.

Тестовете за оформление са РЕАЛНИ (Playwright + истински рендиран PDF през
`page.pdf(prefer_css_page_size=True)` + pypdf, тоест същият печатен движок,
който потребителят получава при „Печат/PDF“) — маркирани са с `e2e` и
тръгват само с `pytest -m e2e`. До тях стоят и бързи проверки върху самите
шаблони/CSS (без браузър), които пазят конкретните механизми на поправките
да не бъдат премахнати при бъдещо разместване.
"""
import io
import json
import os
import re
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- без браузър
# №11 — механизмът на побирането в един лист

def test_cmr_fit_levels_and_measuring_helper_exist_in_css():
    """Одит (19.08.2026, находка №11): степените на побиране са цялата
    поправка — без тях JS-ът няма какво да приложи и ЧМР пак се разлива на
    2 страници. Проверява се и помощният клас за измерването, който
    фиксира ширината на страницата (иначе измерването зависи от размера на
    прозореца и избира прекалено едра степен)."""
    css = _read("static", "style.css")
    for level in range(1, 6):
        assert ".cmr-fit-%d .cmr {" % level in css, "липсва степен .cmr-fit-%d" % level
    assert ".cmr-measuring {" in css
    assert "width: 210mm !important" in css, \
        "измерването трябва да е при ширината на хартията, не на прозореца"
    assert ".cmr-measuring .no-print { display: none !important; }" in css


def test_cmr_default_sizes_are_unchanged_by_the_fit_variables():
    """Степените работят през CSS променливи; стойностите ПО ПОДРАЗБИРАНЕ
    трябва да са същите като преди поправката, за да не се промени
    внимателно настроеният изглед на бланката без нужда."""
    css = _read("static", "style.css")
    for declaration in ("--cmr-cap: 8.5px", "--cmr-cap-b: 9.5px", "--cmr-val: 12px",
                        "--cmr-box-pad: 3px 6px", "--cmr-cell-pad: 3px 5px",
                        "--cmr-sign-h: 80px", "--cmr-qr: 64px", "--cmr-title: 20px"):
        assert declaration in css, "променена стойност по подразбиране: %s" % declaration


def test_print_container_padding_is_reset_with_important():
    """Одит (19.08.2026, находка №11): печатната страница е ~734 CSS px
    широка, тоест при печат важи и `@media (max-width: 980px) { .container
    { padding: 16px 16px 40px } }` — то стои ПО-НАДОЛУ във файла и печелеше
    над нулирането за печат. Резултат: 56px допълнителна височина и втора,
    почти празна страница при публичния преглед по QR код (той ползва
    <main class="container">)."""
    css = _read("static", "style.css")
    idx = css.index("@media print {")
    block = css[idx:css.index("\n}\n", idx)]
    assert "padding: 0 !important" in block


def test_cmr_fit_is_measured_at_load_and_not_on_beforeprint():
    """Измерването при `beforeprint` е измамно: тогава страницата вече е в
    печатен режим и .print-page е `width:auto` (по ширината на прозореца),
    описанието се пренася на по-малко редове и се избира прекалено едра
    степен — доказано, точно това връщаше ЧМР с 400 знака на 2 страници."""
    js = _read("static", "app.js")
    assert "function initCmrPrintFit()" in js
    assert "initCmrPrintFit();" in js, "функцията трябва да се вика при DOMContentLoaded"
    assert 'window.addEventListener("load", fitCmrPages)' in js
    assert 'addEventListener("beforeprint"' not in js


# №12 — заглавен ред и идентификация на всяка страница

_PRINT_TABLE_TEMPLATES = {
    "packing_print.html": "Опаковъчен лист / Packing list",
    "pallet_print.html": "Палетна карта / Pallet card",
    "invoice_br_print.html": "INVOICE / Фактура",
    "invoice_no_print.html": "COMMERCIAL INVOICE / Фактура",
    "invoice_dubai_print.html": "COMMERCIAL INVOICE / Фактура",
}


@pytest.mark.parametrize("template,ident", sorted(_PRINT_TABLE_TEMPLATES.items()))
def test_print_tables_use_thead_and_a_repeating_document_identifier(template, ident):
    """Одит (19.08.2026, находка №12): при 35 реда стр. 2 започваше направо
    от следващия ред — без имена на колоните и без номер на документа.
    Браузърът повтаря заглавния ред само ако той е в <thead>, а
    идентификатора — само ако е в <tfoot> (виж макроса print_table_ident)."""
    html = _read("templates", template)
    assert "<thead>" in html, "заглавният ред трябва да е в <thead>, за да се повтаря"
    assert "<tbody>" in html
    assert "m.print_table_ident(" in html
    assert ident in html
    assert "doc.number" in html.split("m.print_table_ident(")[1][:120], \
        "повтарящият се ред трябва да носи НОМЕРА на документа"


def test_print_table_rows_are_not_split_across_pages():
    """Измерено при одита: ред 25 на опаковъчен лист с 35 реда е бил
    физически разрязан по границата на листа (top=1038 / bottom=1066 при
    граница 1062)."""
    css = _read("static", "style.css")
    # Одит (03.09.2026, находка №4): селекторът вече включва и .twb
    # (товарителницата беше последната непокрита бланка), затова се търси по
    # НАЧАЛОТО на правилото, не по целия му, вече по-дълъг текст.
    idx = css.index(".pkl table.goods tr, .plt table.goods tr, .inv table.goods tr")
    rule = css[idx:css.index("}", idx)]
    assert "break-inside: avoid" in rule
    assert "page-break-inside: avoid" in rule
    assert ".pkl table.goods thead" in css and "table-header-group" in css
    assert ".pkl table.goods tfoot" in css and "table-footer-group" in css


def test_packing_print_renders_thead_and_identifier(admin_client):
    """Същото, но проверено върху РЕАЛНО рендиран документ (а не върху
    изходния текст на шаблона)."""
    from tests.conftest import post_with_csrf

    items = [{"packing": "Палет", "description": "Материал %d" % i, "qty": i,
              "length": "1200", "width": "800", "height": "900",
              "volume": "0.86", "net": "120", "gross": "145"} for i in range(1, 6)]
    resp = post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател ЕООД",
        "items_json": json.dumps(items),
    }, csrf_source_url="/packing/new", follow_redirects=True)
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "<thead>" in body
    assert "<tfoot>" in body
    assert "print-ident" in body
    m = re.search(r"ОПАКОВЪЧЕН ЛИСТ № (\S+?)<", body)
    assert m, "не е намерен номерът на издадения документ"
    assert "Опаковъчен лист / Packing list № %s" % m.group(1) in body


# №33 — управление на фокуса в модалите

def test_modal_focus_helper_covers_all_four_requirements():
    js = _read("static", "app.js")
    assert "function activateModal(" in js
    # фокус вътре в модала при отваряне
    assert "if (target) target.focus();" in js
    # циклиране на Tab
    assert 'if (e.key !== "Tab") return;' in js
    # фонът е inert (с резервно aria-hidden)
    assert "shell.inert = true;" in js
    assert 'shell.setAttribute("aria-hidden", "true")' in js
    # връщане на фокуса
    assert "restoreTo.focus();" in js


@pytest.mark.parametrize("call", [
    "activateModal(modal, cancelBtn)",            # #confirm-modal
    "activateModal(camModal, camClose)",          # #camera-scan-modal
    "activateModal(modal, lengthInput, returnFocusTo)",  # #pallet-type-modal
])
def test_all_three_modals_activate_focus_management(call):
    assert call in _read("static", "app.js"), "модал без управление на фокуса: %s" % call


@pytest.mark.parametrize("template", ["pallet_form.html", "pallet_bulk_review.html"])
def test_pallet_type_modal_is_declared_as_dialog_and_lives_outside_app_shell(template):
    """`inert` се наследява надолу: модал, останал в {% block content %}
    (тоест ВЪТРЕ в .app-shell), би станал inert заедно с фона и не би
    приемал нито клик, нито въвеждане. Затова маркъпът е в {% block modals %},
    който base.html рендира извън обвивката — там, където вече са другите
    два модала."""
    html = _read("templates", template)
    assert "{% block modals %}" in html
    modal_at = html.index('id="pallet-type-modal"')
    block_at = html.index("{% block modals %}", html.index("{% extends"))
    content_at = html.index("{% block content %}")
    assert block_at < modal_at < content_at or modal_at > html.index("{% block modals %}"), \
        "модалът трябва да е в блока modals, не в content"
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'aria-labelledby="pallet-type-modal-title"' in html
    assert '<span id="pallet-type-modal-title">' in html


def test_base_template_renders_the_modals_block_outside_the_app_shell():
    html = _read("templates", "base.html")
    shell_close = html.index("{% block modals %}")
    # Блокът стои след затварянето на .app-shell (там, където са и
    # #confirm-modal/#camera-scan-modal), не вътре в <main>.
    assert html.index('<div id="confirm-modal"') < shell_close
    assert html.index('<div id="camera-scan-modal"') < shell_close
    assert shell_close < html.index("{% else %}\n<main")


# №34 — хоризонтално преливане на телефон

def test_mobile_rules_stop_the_page_from_scrolling_sideways():
    """Одит (19.08.2026, находка №34): измерено при 375px — /packing/new
    scrollWidth 670, /invoice-br/new 434, /pallet/new 421. Таблицата с
    артикули става плъзгаща се област, а <fieldset> вече може да се свие
    (стандартният му `min-inline-size: min-content` беше истинската
    причина цялата страница да се разпъва)."""
    css = _read("static", "style.css")
    block = css[css.index("@media (max-width: 700px) {"):]
    block = block[:block.index("\n}\n")]
    assert "table.items {" in block
    assert "display: block;" in block
    assert "overflow-x: auto;" in block
    assert "fieldset { min-width: 0; }" in block
    assert ".btn-small { min-height: 44px; }" in block, \
        "докосваемите цели бяха 27–29px — под минимума 44px (WCAG 2.5.5)"


# ---------------------------------------------------------------- с браузър
pdf_module = pytest.importorskip("pypdf")
playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright


@pytest.fixture
def live_server(flask_app, db_module):
    """Истински HTTP сървър срещу същото приложение и временна база —
    същият модел като в tests/test_e2e_smoke.py (виж коментара там)."""
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
        ("audit_admin", generate_password_hash("audit-test-password-123"), "Одит Тест"),
    )
    con.commit()
    con.close()

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


@pytest.fixture
def phone_page(live_server):
    """Същото, но с екран на телефон (375×760 — iPhone SE/8, най-тесният
    реалистичен случай, с който е мерена находка №34)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 375, "height": 760})
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def _login(pg, base_url):
    pg.goto(base_url + "/login")
    pg.fill('input[name="username"]', "audit_admin")
    pg.fill('input[name="password"]', "audit-test-password-123")
    pg.click('button[type="submit"]')
    pg.wait_for_url(base_url + "/")


def _pdf_pages(pg):
    """Реален PDF през печатния движок на Chromium. prefer_css_page_size —
    за да важи `@page { size: A4 portrait; margin: 8mm }` от style.css, а
    не подразбиращият се Letter формат на page.pdf()."""
    data = pg.pdf(print_background=True, prefer_css_page_size=True)
    reader = pdf_module.PdfReader(io.BytesIO(data))
    texts = [(p.extract_text() or "") for p in reader.pages]
    return texts


def _compact(text):
    """pypdf вмъква допълнителни интервали заради кернинга на шрифта —
    сравняваме без интервали изобщо, за да е стабилно."""
    return text.replace(" ", "").replace("\n", "")


def _issue_cmr(pg, base_url, goods):
    pg.goto(base_url + "/cmr/new")
    pg.fill('input[name="sender_name"]', "Изпращач ЕООД")
    pg.fill('input[name="consignee_name"]', "Дълъг Товар ЕООД")
    if goods:
        pg.fill('textarea[name="goods"]', goods)
    pg.click("#main-doc-form button[type=\"submit\"]")
    pg.wait_for_url(base_url + "/doc/*")


# №11 — ЧМР винаги на един лист

@pytest.mark.e2e
@pytest.mark.parametrize("length", [44, 88, 132, 176, 264, 400])
def test_cmr_prints_on_a_single_page_for_realistic_goods_descriptions(page, live_server, length):
    """Одит (19.08.2026, находка №11). Измерено ПРЕДИ поправката с точно
    тези дължини: 176 знака вече даваха 2 страници, на втората от които
    оставаха само кутии 22/23/24 (подписите) — мрежата се късаше по
    средата, без рамки и без номер на документа; при „4 екземпляра“ това
    са 8 листа половин-бланки. Официалната ЧМР бланка Е едностранична.

    400 знака е двойно над най-лошия случай в одита — нарочно, защото
    поправката трябва да има запас, а не да е нагласена по прага."""
    goods = ("Стоманени профили за монтаж на разпределително табло " * 20)[:length]
    _login(page, live_server)
    _issue_cmr(page, live_server, goods)

    level = page.evaluate("() => document.querySelector('.cmr-page').dataset.cmrFit")
    texts = _pdf_pages(page)
    assert len(texts) == 1, (
        "ЧМР с %d знака в поле 6–9 се разля на %d страници (избрана степен на "
        "побиране: %s)" % (length, len(texts), level))
    # Съдържанието НЕ е изрязано — краят на описанието е на листа.
    assert _compact(goods[-20:]) in _compact(texts[0])
    # И бланката е цялата там, не само горната ѝ половина.
    assert "22" in texts[0] and "24" in texts[0]


@pytest.mark.e2e
def test_cmr_four_copies_are_exactly_four_pages(page, live_server):
    """Бутонът „4 екземпляра“ при преливаща бланка даваше 8 листа
    половин-бланки — за превозвача това е неизползваем комплект."""
    _login(page, live_server)
    _issue_cmr(page, live_server, "Стоманени профили за електрически табла, 12 палета")
    page.goto(page.url + "?copies=4")
    texts = _pdf_pages(page)
    assert len(texts) == 4, "4 екземпляра трябва да са 4 листа, не %d" % len(texts)


@pytest.mark.e2e
def test_public_qr_view_of_a_cmr_also_prints_on_a_single_page(page, live_server, db_module):
    """Разпечатката по QR кода (телефон на шофьора/получателя, БЕЗ вход) е
    равноправен печатен път на същата бланка. Там <main> е с class
    „container“ (не „container-print“) и екранните отстъпи за тесен екран
    се прилагаха и на хартията — бланката тръгваше 16px по-ниско и
    прескачаше на втора страница дори когато се събира."""
    _login(page, live_server)
    _issue_cmr(page, live_server, "Стоманени профили за монтаж на табло, 12 палета")

    con = db_module.get_db()
    row = con.execute("SELECT public_token FROM documents WHERE data LIKE ?",
                      ("%Дълъг Товар ЕООД%",)).fetchone()
    con.close()
    assert row and row["public_token"]

    context = page.context.browser.new_context()
    try:
        anon = context.new_page()
        anon.goto(live_server + "/p/" + row["public_token"])
        texts = _pdf_pages(anon)
        assert len(texts) == 1, "публичният преглед се печата на %d страници" % len(texts)
        assert "22" in texts[0] and "24" in texts[0], "бланката трябва да е ЦЯЛАТА на листа"
    finally:
        context.close()


@pytest.mark.e2e
def test_cmr_with_extreme_description_overflows_instead_of_clipping(page, live_server):
    """Границата на поправката, съзнателно: при НАИСТИНА екстремно описание
    (тук ~1800 знака) и най-дребната степен бланката пак прелива на втора
    страница. Това е нарочният избор — мълчаливото изрязване е по-лошият
    дефект (виж находка К5 от предишния одит): нищо от текста не бива да
    изчезва от разпечатката, дори с цената на втори лист."""
    long_goods = "\n".join("Ред %02d — палет с материали за тест на препълване" % i
                           for i in range(1, 41))
    _login(page, live_server)
    _issue_cmr(page, live_server, long_goods)
    texts = _pdf_pages(page)
    compact = _compact("".join(texts))
    assert len(texts) > 1
    assert "Ред01" in compact
    assert "Ред40" in compact, "нито един ред не бива да се губи от разпечатката"


# №12 — многостранични таблици

def _fill_rows(pg, table_id, count, fields):
    for i in range(count):
        if i:
            pg.click('[data-add-row="%s"]' % table_id)
        row = pg.locator("#%s tbody tr" % table_id).nth(i)
        for field, value in fields(i):
            row.locator('input[data-field="%s"]' % field).fill(value)


@pytest.mark.e2e
def test_packing_list_with_35_rows_repeats_header_and_number_on_every_page(page, live_server):
    """Одит (19.08.2026, находка №12): при 35 реда стр. 2 започваше направо
    от следващия ред — БЕЗ заглавен ред (получателят гледа гола решетка от
    числа) и БЕЗ номер на документа (изгубен лист не се забелязва)."""
    _login(page, live_server)
    page.goto(live_server + "/packing/new")
    page.fill('input[name="receiver_name"]', "Получател ЕООД")
    _fill_rows(page, "packing-items", 35, lambda i: [
        ("packing", "Палет"), ("description", "Материал ред %02d" % (i + 1)),
        ("qty", str(i + 1)), ("length", "1200"), ("width", "800"),
        ("height", "900"), ("volume", "0.86"), ("net", "120"), ("gross", "145")])
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")
    number = re.search(r"ОПАКОВЪЧЕН ЛИСТ № (\S+?)<", page.content()).group(1)

    texts = _pdf_pages(page)
    assert len(texts) > 1, "35 реда трябва да заемат повече от един лист (иначе тестът е безсмислен)"
    for i, text in enumerate(texts):
        compact = _compact(text)
        assert "Описаниенаматериала" in compact, \
            "стр. %d е без заглавен ред на колоните" % (i + 1)
        assert _compact("Опаковъчен лист / Packing list № " + number) in compact, \
            "стр. %d е без номер на документа" % (i + 1)
    # Нито един ред не е изчезнал по границите между листата.
    all_text = _compact("".join(texts))
    for i in range(1, 36):
        assert _compact("Материал ред %02d" % i) in all_text, "липсва ред %d" % i
    # Редът ОБЩО/TOTAL е цял и на един лист.
    assert sum(1 for t in texts if "TOTAL" in _compact(t)) == 1


@pytest.mark.e2e
def test_long_table_rows_are_never_split_by_a_page_break(page, live_server):
    """Одит (19.08.2026, находка №12), втората половина: ред 25 беше
    физически разрязан по границата на листа. Тук проверяваме самото
    правило, приложено ВЪРХУ РЕАЛНИТЕ редове в браузъра (а не само
    присъствието му в CSS файла) — Chromium не пренася ред с
    `break-inside: avoid` през граница на страница."""
    _login(page, live_server)
    page.goto(live_server + "/packing/new")
    page.fill('input[name="receiver_name"]', "Получател ЕООД")
    _fill_rows(page, "packing-items", 35, lambda i: [
        ("packing", "Палет"), ("description", "Материал ред %02d" % (i + 1)),
        ("qty", str(i + 1))])
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")
    page.emulate_media(media="print")
    breaks = page.evaluate(
        """() => Array.from(document.querySelectorAll('.pkl table.goods tbody tr'))
              .map(tr => getComputedStyle(tr).breakInside)""")
    assert breaks and all(b == "avoid" for b in breaks), \
        "редове без break-inside: avoid — ще бъдат разрязани по границата на листа: %r" % breaks


@pytest.mark.e2e
def test_brazil_invoice_with_35_rows_repeats_header_and_number_on_every_page(page, live_server):
    """Същият дефект по същия път и при фактурите — там липсващата
    идентификация на стр. 2 е още по-скъпа (митница/счетоводство)."""
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    page.fill('input[name="invoice_number"]', "0000012955")
    page.fill('input[name="consignee_name"]', "ABB ELETRIFICACAO LTDA")
    _fill_rows(page, "invoice-br-items", 35, lambda i: [
        ("po_no", "4700200362"), ("pos", str(10 * (i + 1))), ("net_weight", "2.21"),
        ("material_code", "GLBK4000%04d" % i), ("qty", "20"), ("unit_price", "13.66")])
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")

    texts = _pdf_pages(page)
    assert len(texts) > 1
    for i, text in enumerate(texts):
        compact = _compact(text)
        assert "Materialcode" in compact, "стр. %d е без заглавен ред" % (i + 1)
        assert "0000012955" in compact, "стр. %d е без номер на фактурата" % (i + 1)


# №33 — фокус в модалите

_FOCUS_INFO = """() => {
  const a = document.activeElement;
  const modals = ['#confirm-modal', '#camera-scan-modal', '#pallet-type-modal']
    .map(s => document.querySelector(s)).filter(Boolean);
  return {
    id: a ? a.id : null,
    tag: a ? a.tagName : null,
    inModal: !!a && modals.some(m => m.contains(a)),
    shellInert: document.querySelector('.app-shell').hasAttribute('inert')
  };
}"""


@pytest.mark.e2e
def test_confirm_modal_traps_focus_and_returns_it_on_escape(page, live_server):
    """Одит (19.08.2026, находка №33). Измерено преди поправката: при
    отваряне фокусът оставаше на бутона ЗАД наслагването, 8 последователни
    Tab-а минаваха през елементите на страницата отдолу (клавиатурният
    потребител „натиска“ бутони, които не вижда), фонът не беше inert, а
    след Escape фокусът не се връщаше на задействащия елемент."""
    _login(page, live_server)
    page.goto(live_server + "/invoice-br/new")
    page.fill('input[name="invoice_number"]', "ФОКУС-1")
    page.fill('input[name="consignee_name"]', "Фокусов Клиент ЕООД")
    page.click('#main-doc-form button[type="submit"]')
    page.wait_for_url(live_server + "/doc/*")

    page.goto(live_server + "/invoices")
    row = page.locator("tr", has_text="ФОКУС-1")
    row.get_by_text("Изтрий").click()
    modal = page.locator("#confirm-modal")
    modal.wait_for(state="visible", timeout=5000)

    info = page.evaluate(_FOCUS_INFO)
    assert info["inModal"], "фокусът остава извън модала: %r" % info
    assert info["shellInert"], "фонът (.app-shell) не е inert — Tab/мишка стигат до него"

    for _ in range(8):
        page.keyboard.press("Tab")
    assert page.evaluate(_FOCUS_INFO)["inModal"], "Tab извежда зад наслагването"
    page.keyboard.press("Shift+Tab")
    assert page.evaluate(_FOCUS_INFO)["inModal"], "Shift+Tab извежда зад наслагването"

    page.keyboard.press("Escape")
    modal.wait_for(state="hidden", timeout=5000)
    after = page.evaluate(_FOCUS_INFO)
    assert not after["shellInert"], "фонът остава inert след затваряне — страницата е мъртва"
    assert not after["inModal"]
    assert page.evaluate(
        """() => document.activeElement && document.activeElement.textContent.indexOf('Изтрий') >= 0"""
    ), "фокусът не се върна на бутона, който отвори диалога"


@pytest.mark.e2e
def test_pallet_type_modal_traps_focus_and_returns_it_to_the_select(page, live_server):
    _login(page, live_server)
    page.goto(live_server + "/pallet/new")
    page.locator("select.pallet-type-select").first.select_option("__other__")
    modal = page.locator("#pallet-type-modal")
    modal.wait_for(state="visible", timeout=5000)

    assert page.evaluate(
        """() => !document.querySelector('.app-shell').contains(
             document.querySelector('#pallet-type-modal'))"""), \
        "модалът е ВЪТРЕ в .app-shell — inert-ът на фона би обхванал и него"
    info = page.evaluate(_FOCUS_INFO)
    assert info["id"] == "pallet-type-modal-length", \
        "фокусът трябва да е в първото поле за въвеждане: %r" % info
    assert info["shellInert"]

    for _ in range(6):
        page.keyboard.press("Tab")
    assert page.evaluate(_FOCUS_INFO)["inModal"]

    page.keyboard.press("Escape")
    modal.wait_for(state="hidden", timeout=5000)
    after = page.evaluate(_FOCUS_INFO)
    assert not after["shellInert"]
    assert after["id"] == "f-pallet_type", \
        "фокусът трябва да се върне на падащото меню „Тип палет“: %r" % after


@pytest.mark.e2e
def test_camera_modal_traps_focus_and_returns_it_to_the_scan_button(page, live_server):
    _login(page, live_server)
    page.goto(live_server + "/")
    page.click("#camera-scan-btn")
    modal = page.locator("#camera-scan-modal")
    modal.wait_for(state="visible", timeout=5000)

    info = page.evaluate(_FOCUS_INFO)
    assert info["id"] == "camera-scan-close", "фокусът не е в модала: %r" % info
    assert info["shellInert"]
    for _ in range(4):
        page.keyboard.press("Tab")
    assert page.evaluate(_FOCUS_INFO)["inModal"]

    page.keyboard.press("Escape")
    modal.wait_for(state="hidden", timeout=5000)
    after = page.evaluate(_FOCUS_INFO)
    assert not after["shellInert"]
    assert after["id"] == "camera-scan-btn", \
        "фокусът трябва да се върне на бутона „Сканирай с камера“: %r" % after


# №34 — телефон

@pytest.mark.e2e
@pytest.mark.parametrize("path", ["/packing/new", "/invoice-br/new", "/pallet/new",
                                  "/invoice-no/new", "/invoice-dubai/new", "/waybill/new"])
def test_forms_do_not_overflow_horizontally_on_a_phone(phone_page, live_server, path):
    """Одит (19.08.2026, находка №34): при 375px измерено ПРЕДИ поправката —
    /packing/new scrollWidth 670, /invoice-br/new 434, /pallet/new 421.
    Прелива целият <fieldset>, тоест страницата се влачи настрани при всяко
    докосване — точно в екраните, в които се сканира с една ръка."""
    _login(phone_page, live_server)
    phone_page.goto(live_server + path)
    size = phone_page.evaluate(
        """() => ({sw: document.documentElement.scrollWidth,
                   cw: document.documentElement.clientWidth})""")
    assert size["sw"] == size["cw"] == 375, \
        "%s прелива хоризонтално: scrollWidth=%s при clientWidth=%s" % (
            path, size["sw"], size["cw"])


@pytest.mark.e2e
def test_items_table_stays_readable_by_scrolling_inside_itself(phone_page, live_server):
    """Таблицата не бива просто да се смачка до нечетимост: тя става
    собствена плъзгаща се област (съдържанието ѝ остава по-широко от
    екрана, но плъзгането е ВЪТРЕ в нея, не на цялата страница)."""
    _login(phone_page, live_server)
    phone_page.goto(live_server + "/packing/new")
    table = phone_page.evaluate(
        """() => { const t = document.getElementById('packing-items');
                   const s = getComputedStyle(t);
                   return {display: s.display, overflowX: s.overflowX,
                           width: Math.round(t.getBoundingClientRect().width),
                           scrollWidth: t.scrollWidth}; }""")
    assert table["display"] == "block"
    assert table["overflowX"] == "auto"
    assert table["scrollWidth"] > table["width"], \
        "таблицата трябва да се плъзга вътре в себе си, а не да смачква колоните"

    # `display: block` върху <table> е промяна на форматиращия контекст —
    # проверяваме, че въвеждането в нея продължава да работи както преди
    # (нов ред от бутона, попълване на клетка) и страницата пак не прелива.
    rows_before = phone_page.locator("#packing-items tbody tr").count()
    phone_page.click('[data-add-row="packing-items"]')
    assert phone_page.locator("#packing-items tbody tr").count() == rows_before + 1
    cell = phone_page.locator("#packing-items tbody tr").last.locator(
        'input[data-field="description"]')
    cell.fill("Материал от телефона")
    assert cell.input_value() == "Материал от телефона"
    assert phone_page.evaluate("() => document.documentElement.scrollWidth") == 375


@pytest.mark.e2e
def test_small_buttons_are_large_enough_to_tap_on_a_phone(phone_page, live_server):
    """Свързана бележка към находка №34: .btn-small бяха 27–29px високи —
    под минимума 44×44px за уверено докосване (WCAG 2.5.5). Това са точно
    бутоните „+ Добави ред“/„Премахни“, които се натискат при въвеждане в
    движение."""
    _login(phone_page, live_server)
    phone_page.goto(live_server + "/packing/new")
    heights = phone_page.evaluate(
        """() => Array.from(document.querySelectorAll('.btn-small'))
              .filter(b => b.offsetParent !== null)
              .map(b => Math.round(b.getBoundingClientRect().height))""")
    assert heights, "страницата трябва да има поне един .btn-small"
    assert all(h >= 44 for h in heights), "твърде ниски бутони на телефон: %r" % heights
