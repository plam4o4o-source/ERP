# -*- coding: utf-8 -*-
"""Регресионни тестове за пълния тест на 22.09.2026 — находки №1–№8 и №10,
докладвани в ERP_ТЕСТ_2026_09_22.md срещу production v3.73.0 (`e9008ba`).

Всяка функция е ЗАКЛЮЧВАЩА за конкретна находка: преди поправката пада,
след нея минава — проверено лично и в двете посоки с реално изпълнение.

Находки №1 (загуба на редове в палетна карта), №2 (индикатор при PDF
износ), №3 (Enter издава празни декларации), №4 (печат на товарителница)
и №6/№7 (тъч цел и сянка за скрол) се виждат само в истински браузър —
живеят в tests/test_e2e_smoke.py, по установената конвенция. Тук са
техните проверими без браузър половини (CSS правила, сървърни отговори,
атрибути в шаблоните).

Находка №9 от доклада („продължението на многостранична палетна карта
губи номера/баркода“) НЕ е поправяна и няма тест тук: при личната
проверка се оказа НЕВЯРНА. Механизмът съществува от одита на 19.08.2026
(находка №12, единична карта) и 31.08.2026 (находка №17, групов печат) —
`_macros.print_table_ident` слага номера на картата в <tfoot>, който
браузърът повтаря на всеки лист. Проверено с реален печат (Chromium →
PDF, 50 реда, 2 листа): и на двата листа присъстват и „Палетна карта“, и
номерът „0007/2026“. Вместо тест за несъществуващ дефект, по-долу стои
тест, който ЗАКЛЮЧВА работещия механизъм, за да не се загуби наистина.
"""
import io
import json
import re

import pytest

from conftest import post_with_csrf

ROOT_STYLE = "static/style.css"


def _read_style():
    with open(ROOT_STYLE, encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------- WCAG помощни
def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hexcolor):
    hexcolor = hexcolor.lstrip("#")
    if len(hexcolor) == 3:
        hexcolor = "".join(ch * 2 for ch in hexcolor)
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (_srgb_to_linear(v) for v in (r, g, b))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex1, hex2):
    """WCAG contrast ratio — независимо преизчисление (не взето наготово
    от доклад на агент), по формулата (L1+0.05)/(L2+0.05)."""
    l1 = _relative_luminance(hex1)
    l2 = _relative_luminance(hex2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _theme_block(css, theme):
    """Съдържанието на `:root[data-theme="<theme>"] { ... }` БЕЗ
    коментарите (за да не се хващат исторически hex стойности, споменати
    само в текста на одитен коментар). Селекторът на светлата тема е
    съставен (`, :root:not([data-theme])`), затова `[^{]*` преди `{`."""
    m = re.search(
        r':root\[data-theme="%s"\][^{]*\{([^}]*)\}' % re.escape(theme), css)
    assert m, "темата %r не е намерена в %s" % (theme, ROOT_STYLE)
    return re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)


def _var(block, name):
    m = re.search(r'%s:\s*(#[0-9a-fA-F]{3,6})' % re.escape(name), block)
    assert m, "променливата %r не е намерена в темата" % name
    return m.group(1)


THEMES = ("light", "dark", "blue", "green", "contrast", "sepia")


# --------------------------------------------------------------------- №1
def test_pallet_card_row_inputs_are_not_given_a_form_name():
    """Находка №1 (ТЕЖКА, загуба на данни): `suffixBlock` в
    initPalletMultiCard задаваше `name` на ВСЕКИ `[data-field]` елемент в
    картата — включително input-ите в редовете на `table.items`, които
    НАРОЧНО нямат `name` (пътуват сериализирани в items_json). Така всички
    редове получаваха ЕДИН И СЪЩ `name` по колона, а при „Предварителен
    преглед → Назад към формата“ съдържанието на първия ред се появяваше
    във всички следващи; „Издай“ записваше подменените редове и вторият
    артикул изчезваше безвъзвратно.

    Поведението в браузъра се заключва в test_e2e_smoke.py; тук се
    заключва самият механизъм — че редовете се изключват от
    преименуването."""
    with open("static/app.js", encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"function suffixBlock\(block, n, multi\) \{(.*?)\n  \}", js, re.S)
    assert m, "suffixBlock не е намерена в static/app.js"
    body = m.group(1)
    loop = re.search(
        r'block\.querySelectorAll\("\[data-field\]"\).*?\}\);', body, re.S)
    assert loop, "цикълът по [data-field] не е намерен в suffixBlock"
    assert 'closest("table.items")' in loop.group(0), (
        "находка №1: suffixBlock пак преименува ВСИЧКИ [data-field] елементи — "
        "редовете на table.items трябва да се прескачат, иначе всички редове "
        "получават еднакъв `name` и браузърът ги слива при връщане от преглед")


# --------------------------------------------------------------------- №3
@pytest.mark.parametrize("url,field", [
    ("/dualuse/new", "invoice_numbers"),
    ("/export-it/new", "invoice_no"),
])
def test_declaration_forms_have_a_required_field(admin_client, url, field):
    """Находка №3 (ТЕЖКА): двете декларации нямаха НИТО ЕДНО задължително
    поле — единствената причина другите седем типа документи да не се
    издават при случаен Enter беше, че при тях native HTML5 валидацията
    има какво да блокира. Лично възпроизведено в реален браузър: текст в
    първото поле + Enter издаваше номерирана декларация с празни фактура,
    държава и декларатор.

    Декларацията е твърдение ЗА КОНКРЕТНА ФАКТУРА — номерът ѝ е
    задължителен. Общият предпазител (Enter в поле не изпраща формата) се
    заключва в test_e2e_smoke.py."""
    body = admin_client.get(url).data.decode("utf-8")
    m = re.search(r'<input[^>]*name="%s"[^>]*>' % re.escape(field), body)
    assert m, "полето %r не е намерено в %s" % (field, url)
    assert "required" in m.group(0), (
        "находка №3: %s няма `required` на %s — случаен Enter издава реален "
        "номериран документ с празни полета" % (url, field))


def test_every_document_form_has_at_least_one_required_field(admin_client):
    """Находка №3, класовата половина: нито една от деветте форми не бива
    да остава без задължително поле — иначе се връща точно капанът от
    находката, само при следващия нов тип документ."""
    forms = ["/cmr/new", "/packing/new", "/pallet/new", "/waybill/new",
             "/dualuse/new", "/export-it/new", "/invoice-br/new",
             "/invoice-no/new", "/invoice-dubai/new"]
    without = []
    for url in forms:
        body = admin_client.get(url).data.decode("utf-8")
        form = re.search(r'<form[^>]*id="main-doc-form".*?</form>', body, re.S)
        assert form, "главната форма не е намерена в %s" % url
        if "required" not in form.group(0):
            without.append(url)
    assert not without, (
        "находка №3: форми без нито едно задължително поле: %s" % without)


# --------------------------------------------------------------------- №2
def test_pdf_export_returns_ready_cookie_only_for_a_safe_token(admin_client):
    """Находка №2: изтеглянето на PDF е обикновен линк — няма събитие
    „готово“, на което индикаторът да спре. Маршрутът връща бисквитка със
    същия токен, подаден от клиента (?dl=), за да може JS да махне
    индикатора точно когато файлът пристигне.

    Токенът идва отвън и влиза в заглавна част на отговора, затова тук се
    заключва и че се приема САМО безобиден кратък низ."""
    resp = post_with_csrf(admin_client, "/cmr/new",
                          {"consignee_name": "PDF Готовност ЕООД"},
                          csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = int(re.search(r"/doc/(\d+)", resp.headers["Location"]).group(1))

    plain = admin_client.get("/doc/%d/export.pdf" % doc_id)
    assert plain.status_code == 200
    assert plain.data[:4] == b"%PDF"
    assert "pacho_pdf_ready" not in plain.headers.get("Set-Cookie", ""), (
        "без ?dl= не бива да се връща бисквитка — изтеглянето без JS работи "
        "точно както преди")

    ok = admin_client.get("/doc/%d/export.pdf?dl=abc123" % doc_id)
    assert ok.status_code == 200
    assert "pacho_pdf_ready=abc123" in ok.headers.get("Set-Cookie", ""), (
        "находка №2: маршрутът не връща сигнал „готово“ — индикаторът върху "
        "бутона няма как да спре в момента, в който файлът пристигне")

    bad = admin_client.get("/doc/%d/export.pdf?dl=%s"
                           % (doc_id, "a\r\nX-Evil: 1"))
    assert bad.status_code == 200
    assert "pacho_pdf_ready" not in bad.headers.get("Set-Cookie", ""), (
        "токен с нечифрово-буквени знаци НЕ бива да стига до заглавната част")
    assert "X-Evil" not in dict(bad.headers)

    long_token = admin_client.get("/doc/%d/export.pdf?dl=%s"
                                  % (doc_id, "a" * 80))
    assert ("pacho_pdf_ready=" + "a" * 32 + ";") in \
        long_token.headers.get("Set-Cookie", ""), "токенът трябва да се реже до 32"


def test_pdf_export_button_is_marked_for_the_busy_indicator(admin_client):
    """Находка №2: без `data-pdf-export` на линка индикаторът изобщо не се
    закача (виж initPdfExportBusy в app.js)."""
    resp = post_with_csrf(admin_client, "/cmr/new",
                          {"consignee_name": "Индикатор ЕООД"},
                          csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = int(re.search(r"/doc/(\d+)", resp.headers["Location"]).group(1))
    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    m = re.search(r'<a[^>]*export_document_pdf[^>]*>|<a[^>]*/export\.pdf"[^>]*>', body)
    assert m, "бутонът „Изтегли PDF“ не е намерен"
    assert "data-pdf-export" in m.group(0), (
        "находка №2: линкът за PDF няма data-pdf-export — операторът не вижда "
        "нищо да се случва през измерените ~13.7 сек при 600 реда и натиска пак")


# --------------------------------------------------------------------- №4
def test_waybill_switches_to_one_full_copy_per_sheet_when_rows_grow():
    """Находка №4: форматът „2 копия на един лист“ държи до 8 реда стока
    (измерено с реален печат в Chromium). От 10 реда нататък копията
    преливат, а при 45 реда се получаваха ЧЕТИРИ листа, от които ДВА
    почти празни — носеха само опашката на предходното копие.

    Над прага шаблонът минава на „едно пълноразмерно копие на лист“."""
    with open("templates/waybill_print.html", encoding="utf-8") as f:
        tpl = f.read()
    assert "waybill_rows" in tpl, (
        "находка №4: шаблонът не брои редовете — пак ще реже смалени копия "
        "през границата на листа")
    m = re.search(r"\{%\s*if waybill_rows <= (\d+)\s*%\}", tpl)
    assert m, "липсва условието за превключване на формата"
    threshold = int(m.group(1))
    assert 1 <= threshold <= 8, (
        "прагът (%d) трябва да е в измерените граници: до 8 реда двете копия "
        "се побират на един лист" % threshold)
    two_up = tpl.index('class="print-page twb-2up"')
    assert tpl.count('class="print-page"') >= 2, (
        "находка №4: липсва вариантът с по едно пълноразмерно копие на лист")
    assert tpl.count("twb_body()") >= 4, (
        "и двата варианта трябва да печатат по ДВЕ копия за подпис")
    assert two_up > 0


# --------------------------------------------------------------------- №5
def test_login_error_message_is_readable_in_every_theme():
    """Находка №5а: `.error` (съобщението при грешен вход) ползваше
    „бутонните“ --danger/--danger-soft. Като ТЕКСТ върху мекия фон те
    дават 4.36 (light/blue/green), 3.41 (dark) и 3.86 (contrast) — под
    прага 4.5:1 в пет от шестте теми. Всяка тема има готова тройка ИМЕННО
    за съобщения за грешка (--err-*), която минава навсякъде."""
    css = _read_style()
    rule = re.search(r'\n\.error \{([^}]*)\}', css)
    assert rule, "правилото .error не е намерено"
    assert "var(--err-fg)" in rule.group(1) and "var(--err-bg)" in rule.group(1), (
        "находка №5а: .error пак ползва бутонните --danger/--danger-soft "
        "вместо тройката за съобщения за грешка")
    for theme in THEMES:
        block = _theme_block(css, theme)
        fg, bg = _var(block, "--err-fg"), _var(block, "--err-bg")
        ratio = _contrast_ratio(fg, bg)
        assert ratio >= 4.5, (
            "тема %s: съобщението за грешка е %.2f:1 (под 4.5)" % (theme, ratio))


def test_button_hover_text_is_readable_in_every_theme():
    """Находка №5б: в тъмната тема --accent-hover беше #5b95f7 — ПО-СВЕТЪЛ
    от самия --accent, тоест белият текст падаше на 2.96:1 при посочване,
    по-нечетимо от състоянието в покой (4.67:1). Тъмната тема беше
    единствената, която светлее при hover."""
    css = _read_style()
    for theme in THEMES:
        block = _theme_block(css, theme)
        fg, bg = _var(block, "--accent-fg"), _var(block, "--accent-hover")
        ratio = _contrast_ratio(fg, bg)
        assert ratio >= 4.5, (
            "тема %s: текстът на бутона при hover е %.2f:1 (под 4.5) — "
            "--accent-fg %s върху --accent-hover %s" % (theme, ratio, fg, bg))


def test_soft_accent_background_has_readable_text_in_every_theme():
    """Находка №5в: баджът (и .qa .ic, и .btn-outline:hover) рисуваше
    --accent върху --accent-soft — 3.19:1 в тъмната и 4.05:1 в зелената
    тема. --accent не може да се пипне (счупва вече поправените бутони),
    затова текстът върху мекия фон има своя променлива."""
    css = _read_style()
    for theme in THEMES:
        block = _theme_block(css, theme)
        fg, bg = _var(block, "--accent-soft-fg"), _var(block, "--accent-soft")
        ratio = _contrast_ratio(fg, bg)
        assert ratio >= 4.5, (
            "тема %s: текстът върху --accent-soft е %.2f:1 (под 4.5)"
            % (theme, ratio))
    for selector in (r'\.badge \{[^}]*\}', r'\.btn-outline:hover \{[^}]*\}',
                     r'\.qa \.ic \{[^}]*\}'):
        rule = re.search(selector, css)
        assert rule, "правилото %r не е намерено" % selector
        assert "var(--accent-soft-fg)" in rule.group(0), (
            "находка №5в: %r пак ползва --accent върху мекия фон" % selector)


def test_placeholder_text_is_themed_and_readable_in_every_theme():
    """Находка №5г: подсказващият текст в празните полета оставаше с
    БРАУЗЪРНИЯ подразбиращ се сив (#757575) — темата не го пипаше. Измерено
    срещу фона на полето: 3.80:1 в тъмната и 4.32:1 в sepia темата."""
    css = _read_style()
    assert re.search(r'input::placeholder[^{]*\{[^}]*var\(--muted\)', css), (
        "находка №5г: няма тематизирано правило за ::placeholder — остава "
        "браузърният сив #757575")
    for theme in THEMES:
        block = _theme_block(css, theme)
        fg, bg = _var(block, "--muted"), _var(block, "--input-bg")
        ratio = _contrast_ratio(fg, bg)
        assert ratio >= 4.5, (
            "тема %s: подсказващият текст е %.2f:1 (под 4.5)" % (theme, ratio))


# --------------------------------------------------------------------- №8
def test_items_table_header_is_readable_in_every_theme():
    """Находка №8: заглавният ред на артикулните таблици (--fg-soft върху
    --table-head) даваше 4.22:1 в sepia темата."""
    css = _read_style()
    for theme in THEMES:
        block = _theme_block(css, theme)
        fg, bg = _var(block, "--fg-soft"), _var(block, "--table-head")
        ratio = _contrast_ratio(fg, bg)
        assert ratio >= 4.5, (
            "тема %s: заглавният ред на table.items е %.2f:1 (под 4.5)"
            % (theme, ratio))


def test_sepia_muted_stays_in_sync_with_fg_soft():
    """Одит 09.09.2026 (находка №6) въведе правилото, че в sepia темата
    --muted и --fg-soft са една и съща стойност. Находка №8 промени
    --fg-soft — двете трябва да си останат заедно."""
    block = _theme_block(_read_style(), "sepia")
    assert _var(block, "--muted") == _var(block, "--fg-soft"), (
        "sepia: --muted (%s) се е разминала с --fg-soft (%s)"
        % (_var(block, "--muted"), _var(block, "--fg-soft")))


# --------------------------------------------------------------------- №6
def test_mobile_menu_button_meets_the_touch_target_minimum():
    """Находка №6: хамбургер бутонът беше измерено 34×34px — под минимума
    44×44px (WCAG 2.5.5), който одитите от 19.08 и 25.08.2026 наложиха на
    всички останали докосваеми контроли. На телефон той е единственият път
    до цялата навигация."""
    css = _read_style()
    m = re.search(r'\.mobile-topbar button \{([^}]*)\}', css)
    assert m, "правилото .mobile-topbar button не е намерено"
    rule = m.group(1)
    assert "min-width: 44px" in rule and "min-height: 44px" in rule, (
        "находка №6: хамбургер бутонът пак е под тъч прага 44×44px")


# --------------------------------------------------------------------- №7
def test_list_tables_show_a_scroll_hint_on_narrow_screens():
    """Находка №7 (непокритата половина на подобрение №5 от 09.09.2026):
    сянката, която подсказва „вдясно има още“, отиде само на table.items.
    table.list (/docs, /invoices, /clients) я нямаше, макар измерено при
    390px да показва под една трета от реда си."""
    css = _read_style()
    m = re.search(r'@media \(max-width:\s*700px\)\s*\{(.*)\n\}\n', css, re.S)
    assert m, "медия заявката max-width:700px не е намерена"
    mobile = m.group(1)
    for selector in ("table.items", "table.list"):
        rule = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', mobile)
        assert rule, "%s няма правило в мобилния блок" % selector
        assert "box-shadow" in rule.group(1) and "inset" in rule.group(1), (
            "находка №7: %s няма визуален знак за скрито съдържание" % selector)
        assert "color-mix(in srgb, var(--fg)" in rule.group(1), (
            "сянката трябва да е изведена от --fg, за да е видима в шестте теми")


# -------------------------------------------------------------------- №10
def test_row_cap_warning_tells_the_operator_what_to_do(admin_client):
    """Находка №10: и трите Excel импорта казваха КАКВО е пропуснато, но не
    и какво да направи операторът. Реален ценоразпис на голям доставчик
    минава тавана от 5000 реда, а справочникът е НАТРУПВАЩ — значи качване
    на части наистина работи и трябва да се каже."""
    import materials
    import routes_invoices
    import routes_pallet_extra
    import routes_materials

    for mod in (routes_invoices, routes_pallet_extra, routes_materials):
        with open(mod.__file__, encoding="utf-8") as f:
            src = f.read()
        # Съобщението е разделено на няколко реда в изходния код — сглобяваме
        # съседните низови литерали, за да търсим в ТЕКСТА, не в начина, по
        # който е пренесен.
        joined = re.sub(r'"\s*\n\s*"', '', src)
        assert "Разделете файла на части до %d реда" in joined, (
            "находка №10: %s пак не казва какво да направи операторът при "
            "надхвърлен таван" % mod.__name__)
        assert "следващо качване се добавя към вече заредените" in joined, (
            "%s: липсва обяснението, че качването на части е натрупващо"
            % mod.__name__)


def test_row_cap_message_stays_word_for_word_identical_in_all_three_imports():
    """Трите Excel импорта НАРОЧНО ползват един и същи msgid — един превод
    и еднакъв текст пред оператора, независимо кой файл качва. Находка №10
    промени текста и на трите места; тестът пази това занапред."""
    texts = []
    for path in ("routes_invoices.py", "routes_pallet_extra.py",
                 "routes_materials.py"):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'_\(\s*((?:\s*"[^"]*"\s*)+)\)\s*\n?\s*%\s*\(?[^)]*'
                      r'_MAX_IMPORT_DATA_ROWS|_\(\s*((?:\s*"[^"]*"\s*)+)\)\s*\n?\s*%\s*\(max_rows',
                      src)
        assert m, "съобщението за таван не е намерено в %s" % path
        raw = m.group(1) or m.group(2)
        texts.append("".join(re.findall(r'"([^"]*)"', raw)))
    assert texts[0] == texts[1] == texts[2], (
        "съобщенията за таван се разминаха между трите импорта:\n%s"
        % "\n".join(repr(t) for t in texts))


# --------------------------------------------------- №9 (НЕВЯРНА находка)
def test_multipage_pallet_card_keeps_its_number_on_every_sheet():
    """Докладът от 22.09.2026 съдържаше находка №9 („продължението на
    многостранична палетна карта губи номера/баркода“). При личната
    проверка тя се оказа НЕВЯРНА: механизмът съществува от одита на
    19.08.2026 (находка №12) и 31.08.2026 (находка №17) — номерът се
    повтаря през <tfoot> (browsers повтарят tfoot на всеки лист).
    Проверено с реален печат: 50 реда → 2 листа, и на двата присъстват
    „Палетна карта“ и номерът.

    Тестът заключва работещия механизъм и в двата шаблона, за да не бъде
    премахнат по погрешка при бъдеща промяна."""
    for path, expected in (("templates/pallet_print.html", 2),
                           ("templates/pallet_bulk_print.html", 2)):
        with open(path, encoding="utf-8") as f:
            tpl = f.read()
        found = tpl.count("print_table_ident")
        assert found >= 1, (
            "%s вече не слага номера на картата в <tfoot> — лист 2 на дълга "
            "карта остава без нищо, по което да бъде разпознат" % path)
