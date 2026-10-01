# -*- coding: utf-8 -*-
"""Регресионни тестове за пълния тест на 09.09.2026 — находки №1–№7 плюс
подобренията в работния поток (№1–№6), докладвани в
ERP_ТЕСТ_2026_09_09.md срещу production v3.72.0 (`ab132a8`).

Всяка функция е ЗАКЛЮЧВАЩА за конкретна находка/подобрение: преди
поправката пада, след нея минава — проверено лично и в двете посоки с
реално изпълнение (Flask test client / статичен анализ на CSS със
самостоятелно преизчислен WCAG contrast ratio).

Находка №1 (недостъпен `table.list` на телефон) и подобрения №1–№2, №4 и
№6 (клавиатурна навигация, локално търсене на клиент, бутон „Зареди от
изпращача“, кликваема подсказка за сбор) изискват истински браузър —
живеят в tests/test_e2e_smoke.py, по установената конвенция (виж
docstring-а на test_audit_2026_09_05.py)."""
import json
import re

import pytest

from conftest import post_with_csrf as _post_with_csrf

# Одит (01.10.2026, U5): фактура без нито един ред със стока вече не се издава.
# Тестовете тук не проверяват самите редове — получават един служебен ред.
_ONE_INVOICE_ITEM = json.dumps([{"material_code": "TEST-ITEM", "qty": "1", "unit_price": "1"}])


def post_with_csrf(client, url, data, *args, **kwargs):
    if (url in ("/invoice-br/new", "/invoice-no/new", "/invoice-dubai/new")
            and data.get("items_json", "[]") == "[]"):
        data = dict(data, items_json=_ONE_INVOICE_ITEM)
    return _post_with_csrf(client, url, data, *args, **kwargs)

ROOT_STYLE = "static/style.css"


def _read_style():
    with open(ROOT_STYLE, encoding="utf-8") as f:
        return f.read()


def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hexcolor):
    hexcolor = hexcolor.lstrip("#")
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (_srgb_to_linear(v) for v in (r, g, b))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex1, hex2):
    """WCAG contrast ratio — независимо преизчисление (не взето наготово
    от доклад на агент), по формулата (L1+0.05)/(L2+0.05), L1 по-светлия."""
    l1 = _relative_luminance(hex1)
    l2 = _relative_luminance(hex2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _theme_block(css, theme):
    """Извлича съдържанието на `:root[data-theme="<theme>"] { ... }`,
    БЕЗ коментарите вътре (за да не хващаме исторически hex стойности,
    споменати само в текста на одитен коментар) — самостоятелен regex, не
    зависи от реда на правилата в файла."""
    m = re.search(
        r':root\[data-theme="%s"\]\s*\{([^}]*)\}' % re.escape(theme), css)
    assert m, "темата %r не е намерена в %s" % (theme, ROOT_STYLE)
    return re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)


def _var(block, name):
    """Стойността на CSS променлива `name` в блока — hex цвят ИЛИ друга
    CSS стойност (напр. rgba(...) за --sidebar-hover), до следващата `;`."""
    m = re.search(r'%s:\s*([^;]+);' % re.escape(name), block)
    assert m, "променливата %r не е намерена в темата" % name
    return m.group(1).strip()


# --------------------------------------------------------------------- №1
def test_style_table_list_scrolls_horizontally_under_700px():
    """Находка №1 (тежка): `table.list` (списъкът с документи, /docs) няма
    механизъм за скрол на телефон — `overflow: hidden` без `overflow-x:
    auto`, за разлика от `table.items`, коeто получи поправката на находка
    №34 (19.08.2026). CSS-регресията се хваща и без браузър: проверяваме,
    че вътре в `@media (max-width: 700px) { ... }` съществува блок за
    `table.list` с `display: block` и `overflow-x: auto`.

    Реалната недостижимост (потвърдено с Playwright — scrollLeft на
    table/card/window остават 0 без тази поправка) се проверява в
    tests/test_e2e_smoke.py, за да покрие и структурния маркъп на
    /docs, не само CSS правилото."""
    css = _read_style()
    m = re.search(r'@media \(max-width:\s*700px\)\s*\{(.*)\n\}\n', css, re.S)
    assert m, "медия заявката max-width:700px не е намерена"
    mobile_block = m.group(1)

    m2 = re.search(r'table\.list\s*\{([^}]*)\}', mobile_block)
    assert m2, (
        "находка №1: table.list няма собствено правило вътре в "
        "@media (max-width: 700px) — списъкът с документи остава "
        "недостижим на телефон (overflow: hidden без overflow-x: auto)")
    rule = m2.group(1)
    assert "display: block" in rule or "display:block" in rule
    assert "overflow-x: auto" in rule or "overflow-x:auto" in rule


# --------------------------------------------------------------------- №2
def test_docs_screen_hides_invoice_types_from_filter_and_does_not_show_them(
        admin_client):
    """Находка №2 (средна): „Всички документи“ показваше и трите фактурни
    типа в падащото меню „Тип документ“ (`doc_types=db.DOC_TYPES`), макар
    заявката винаги да изключва фактурите (`WHERE d.doc_type NOT IN
    (...)`) — изборът водеше до видимо избран тип и празен списък под
    него, вместо реалните документи. Лично възпроизведено и с двете
    заявки: /invoices?type=invoice_br намира фактурата, /docs?type=
    invoice_br — не."""
    post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "ИНВОЙС ЗАКЛЮЧВАЩ ТЕСТ ЕООД",
    }, csrf_source_url="/invoice-br/new", follow_redirects=True)

    resp_invoices = admin_client.get("/invoices?type=invoice_br")
    assert resp_invoices.status_code == 200
    assert b"INVBR" in resp_invoices.data or "ИНВОЙС ЗАКЛЮЧВАЩ ТЕСТ".encode() in resp_invoices.data

    resp_docs = admin_client.get("/docs")
    body = resp_docs.data.decode("utf-8")
    assert 'value="invoice_br"' not in body, (
        "находка №2: фактурен тип все още се показва като опция в "
        "падащото меню на /docs, макар заявката никога да не може да "
        "върне ред за него")
    assert 'value="invoice_no"' not in body
    assert 'value="invoice_dubai"' not in body

    resp_filtered = admin_client.get("/docs?type=invoice_br")
    # Одит (собствена проверка при прилагането на v3.73.0): проверката на
    # `status_code` тук е ЗАДЪЛЖИТЕЛНА, не козметична — БЕЗ `follow_redirects`,
    # за да не се скрие точно провалът, който трябва да хванем. `doc_type`
    # минава през ДВЕ отделни защити (guard-ът в началото на `documents()` И
    # стесненият `doc_types`, подаден на шаблона) — реконструирах ги
    # поотделно с revert-observe-restore и открих, че връщането само на
    # guard-а (докато стесненият речник остава) чупи заявката с
    # `UndefinedError: 'dict object' has no attribute 'invoice_br'` на
    # `doc_types[sel_type].title` в documents.html — хванато от общия
    # обработчик на грешки и превърнато в 302 редирект към таблото. Проверка
    # само по `b"INVBR" not in resp.data` минава И за коректния празен
    # списък, И за тази 302 страница (и двете нямат думата "INVBR") — тоест
    # не заключваше нищо съществено.
    assert resp_filtered.status_code == 200, (
        "находка №2: /docs?type=invoice_br хвърля грешка (302 —"
        " %r), вместо да покаже празен списък" % resp_filtered.headers.get("Location"))
    assert b"INVBR" not in resp_filtered.data, (
        "находка №2: /docs?type=invoice_br не бива да показва фактурата — "
        "тя се управлява само от /invoices")


# --------------------------------------------------------------------- №3
def test_dark_theme_button_colors_meet_contrast_threshold():
    """Находка №3 (средна): --accent (3.68:1) и --danger (3.42:1) в тъмна
    тема бяха под прага 4.5:1 срещу белия текст на бутоните — засяга
    всеки основен бутон за действие и всеки бутон за изтриване."""
    block = _theme_block(_read_style(), "dark")
    accent = _var(block, "--accent")
    danger = _var(block, "--danger")
    assert _contrast_ratio(accent, "#ffffff") >= 4.5, (
        "тъмна тема: --accent (%s) все още е под 4.5:1" % accent)
    assert _contrast_ratio(danger, "#ffffff") >= 4.5, (
        "тъмна тема: --danger (%s) все още е под 4.5:1" % danger)


# --------------------------------------------------------------------- №4
def test_contrast_theme_danger_color_meets_threshold():
    """Находка №4 (средна): --danger в темата „Висок контраст“ (#ff6b5e,
    2.79:1) беше най-ниският контраст от всичките шест теми — иронично
    именно темата за максимална четимост."""
    block = _theme_block(_read_style(), "contrast")
    danger = _var(block, "--danger")
    assert _contrast_ratio(danger, "#ffffff") >= 4.5, (
        "тема „Висок контраст“: --danger (%s) все още е под 4.5:1" % danger)


# --------------------------------------------------------------------- №5
def test_green_theme_accent_and_sidebar_active_meet_threshold():
    """Находка №5 (средна): --sidebar-active и --accent в зелена тема бяха
    еднакви #2f8f45 (4.09:1) — под прага, засягайки едновременно активния
    елемент от менюто и основните бутони."""
    block = _theme_block(_read_style(), "green")
    accent = _var(block, "--accent")
    sidebar_active = _var(block, "--sidebar-active")
    assert _contrast_ratio(accent, "#ffffff") >= 4.5, (
        "зелена тема: --accent (%s) все още е под 4.5:1" % accent)
    assert _contrast_ratio(sidebar_active, "#ffffff") >= 4.5, (
        "зелена тема: --sidebar-active (%s) все още е под 4.5:1" % sidebar_active)
    # Регресия срещу самата поправка: докато я правех, случайно изтрих
    # реда --sidebar-bg/--sidebar-fg-dim от същия блок (хванато от
    # test_scanner_field_placeholder_is_readable_in_every_theme). Тук
    # проверяваме изрично, че блокът пази ВСИЧКИТЕ си оригинални
    # променливи, не само новите две стойности.
    for var in ("--bg", "--fg", "--card-bg", "--border", "--sidebar-bg",
               "--sidebar-bg2", "--sidebar-fg", "--sidebar-fg-dim",
               "--sidebar-hover", "--sidebar-active-fg", "--accent-fg",
               "--accent-hover", "--accent-soft", "--muted"):
        _var(block, var)  # хвърля assert грешка, ако липсва


# --------------------------------------------------------------------- №6
def test_sepia_theme_fg_soft_meets_threshold_against_page_background():
    """Находка №6 (средна): --fg-soft в sepia тема (#7a6a52, 4.38:1 срещу
    фона на СТРАНИЦАТА) е под прага точно там, където пада `.page-lead`
    (подзаглавието под H1 на почти всеки екран) — срещу фона на картите
    (#fbf3e6) даваше 4.75, над прага, но проблемът е конкретно на
    страницата. --muted е исторически същата стойност в тази тема и
    трябва да е обновена заедно, за да не се разминат."""
    block = _theme_block(_read_style(), "sepia")
    fg_soft = _var(block, "--fg-soft")
    bg = _var(block, "--bg")
    muted = _var(block, "--muted")
    assert _contrast_ratio(fg_soft, bg) >= 4.5, (
        "sepia тема: --fg-soft (%s) срещу фона на страницата (%s) все още "
        "е под 4.5:1" % (fg_soft, bg))
    assert muted == fg_soft, (
        "sepia тема: --muted (%s) се е разминала с --fg-soft (%s), макар "
        "исторически да са еднакви" % (muted, fg_soft))


# --------------------------------------------------------------------- №7
def test_pallet_bulk_print_skips_non_pallet_document_ids(admin_client):
    """Находка №7 (дребна): груповият печат на палетни карти четеше
    документа БЕЗ `AND d.doc_type = 'pallet'` — id на фактура минаваше
    през и се рендираше в шаблона на палетна карта с безсмислени/празни
    полета. Сега такъв id просто се пропуска (не намира ред), а не
    отваря картата на друг тип документ."""
    resp = post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "ФАКТУРА ЗА НАХОДКА 7 ЕООД",
    }, csrf_source_url="/invoice-br/new", follow_redirects=False)
    location = resp.headers.get("Location", "")
    invoice_id = int(re.search(r"/doc/(\d+)", location).group(1))

    resp2 = post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Палетен Клиент За Находка 7",
        "gross": "1",
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    pallet_id = int(re.search(r"/doc/(\d+)", resp2.headers.get("Location", "")).group(1))

    # Само id-то на фактурата — не бива да намери документ за печат.
    resp = admin_client.get("/pallet/bulk-print?ids=%d" % invoice_id,
                            follow_redirects=True)
    assert resp.status_code == 200
    assert "ФАКТУРА ЗА НАХОДКА 7".encode() not in resp.data, (
        "находка №7: фактурата се е показала през шаблона на палетна карта")
    assert "Няма намерени документи за печат".encode() in resp.data

    # Смесени id-та — само палетната карта трябва да мине.
    resp = admin_client.get(
        "/pallet/bulk-print?ids=%d,%d" % (invoice_id, pallet_id))
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Палетен Клиент За Находка 7" in body
    assert "ФАКТУРА ЗА НАХОДКА 7" not in body


# ------------------------------------------------------------- подобрение №3
def test_materials_lookup_q_param_returns_live_search_results(admin_client, con):
    """Подобрение №3: `/materials/lookup?q=` — живо търсене по код ИЛИ
    описание, докато операторът пише, вместо проверка едва при напускане
    на полето (`?code=`). Захранва `<datalist>` за полето за код на
    материал в редовете на фактура/опаковъчен лист (виж
    attachMaterialCodeSearch в app.js, e2e тест в test_e2e_smoke.py)."""
    import materials
    materials.replace_catalog(con, [
        ("MAT-AAA-1", "Профил алуминиев", "0.500"),
        ("MAT-AAA-2", "Профил стоманен", "1.250"),
        ("MAT-ZZZ-9", "Друг артикул", "0.100"),
    ])
    con.commit()

    resp = admin_client.get("/materials/lookup?q=MAT-AAA")
    assert resp.status_code == 200
    data = resp.get_json()
    codes = sorted(m["code"] for m in data["materials"])
    assert codes == ["MAT-AAA-1", "MAT-AAA-2"], (
        "подобрение №3: търсенето по код не намери очакваните редове: %r" % codes)

    # Търсене по описание, не само по код.
    resp2 = admin_client.get("/materials/lookup?q=Профил")
    codes2 = sorted(m["code"] for m in resp2.get_json()["materials"])
    assert codes2 == ["MAT-AAA-1", "MAT-AAA-2"]

    # Старата точна употреба (?code=) остава непроменена.
    resp3 = admin_client.get("/materials/lookup?code=MAT-ZZZ-9")
    data3 = resp3.get_json()
    assert data3["ok"] is True
    assert data3["net_weight"] == "0.100" or float(data3["net_weight"]) == 0.1


# ------------------------------------------------------------- подобрение №4
def test_waybill_form_has_load_place_from_sender_button(admin_client):
    """Подобрение №4: товарителницата нямаше бутон „Зареди от изпращача“
    за полето „Място на натоварване“, макар ЧМР да има точно това за
    аналогично поле и товарителницата вече да предпопълва друго свое поле
    (established_place) от същия адрес. Кликването е JS поведение,
    проверено в браузър в test_e2e_smoke.py — тук проверяваме, че бутонът
    и целевото поле реално присъстват в рендирания HTML."""
    resp = admin_client.get("/waybill/new")
    body = resp.data.decode("utf-8")
    assert 'id="load-place-from-sender-btn"' in body
    assert 'id="f-place_loading"' in body
    assert 'id="f-sender_name"' in body
    assert 'id="f-sender_address"' in body


# ------------------------------------------------------------- подобрение №5
def test_style_items_table_mobile_scroll_has_edge_shadow_hint():
    """Подобрение №5: скролът на `table.items` на телефон работеше
    коректно (находка №34, 19.08.2026), но нямаше визуален знак, че вдясно
    има скрито съдържание. Вътрешна сянка на десния ръб (color-mix със
    --fg, видима във всичките шест теми без отделна стойност за всяка)."""
    css = _read_style()
    m = re.search(r'@media \(max-width:\s*700px\)\s*\{(.*)\n\}\n', css, re.S)
    assert m
    m2 = re.search(r'table\.items\s*\{([^}]*)\}', m.group(1))
    assert m2, "table.items няма правило в @media (max-width: 700px)"
    rule = m2.group(1)
    assert "box-shadow" in rule and "inset" in rule, (
        "подобрение №5: липсва вътрешна сянка, сигнализираща скрито "
        "съдържание вдясно на скролиращата се таблица")
    assert "color-mix(in srgb, var(--fg)" in rule, (
        "подобрение №5: сянката трябва да е изведена от --fg (color-mix), "
        "за да остава видима във всичките шест теми, не фиксиран hex")
