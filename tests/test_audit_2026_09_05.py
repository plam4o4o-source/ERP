# -*- coding: utf-8 -*-
"""Регресионни тестове за пълния тест на 05.09.2026 — находки №1–№18
плюс подобренията в работата.

Всяка функция е ЗАКЛЮЧВАЩА за конкретна находка: преди поправката пада,
след нея минава. Проверено и в двете посоки с реално изпълнение.

Находки №8, №15 и №16 (известието върху бутоните, страничната лента,
плъзгането на страницата) живеят в tests/test_e2e_smoke.py — виждат се
само в истински браузър с реални размери.

ЧЕТИРИ от поправените находки (№1, №2, №4, №6) са дупки в поправките от
03.09; №1 беше РЕГРЕСИЯ. Затова тук се заключват не само поведението, а и
конкретните механизми, които ги допуснаха.
"""
import io
import json
import os
import re

import pypdf
import pytest

from conftest import get_edit_doc_version, post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------- №1
def test_edit_conflict_keeps_the_rows_not_only_the_header_fields(admin_client,
                                                                 db_module):
    """Находка №1 (висока, РЕГРЕСИЯ от находка №15 на 03.09): конфликтният
    клон пазеше само `form_data()`, а тя ИЗРИЧНО изключва `items_json` —
    редовете идват единствено през `parse_items()`. При GET с `?restore=`
    подаденото ЗАМЕСТВА данните от базата, значи формата се рендираше с
    ПРАЗНА таблица (нито въведените редове, нито съществуващите), докато
    съобщението твърди „презаредена с актуалните данни“. Оператор, който
    натисне „Запази“ втори път, ЗАНУЛЯВАШЕ редовете на издаден документ.

    Преди поправката от 03.09 конфликтът просто пренасочваше и редовете си
    стояха — тоест поправката направи нещата ПО-ЛОШИ."""
    items = [{"code": "СТАР-1", "description": "стар ред", "qty": "5"}]
    post_with_csrf(admin_client, "/pallet/new",
                   {"client_name": "Клиент",
                    "items_json": json.dumps(items, ensure_ascii=False)},
                   csrf_source_url="/pallet/new", follow_redirects=True)
    version = get_edit_doc_version(admin_client, "/doc/1/edit")

    con = db_module.get_db()      # друг оператор записва пръв
    con.execute("UPDATE documents SET version = version + 1 WHERE id = 1")
    con.commit()
    con.close()

    new_items = [{"code": "НОВ-1", "description": "нов ред", "qty": "9"}]
    resp = post_with_csrf(admin_client, "/doc/1/edit", {
        "client_name": "Клиент", "notes": "БЕЛЕЖКА",
        "edit_doc_version": version,
        "items_json": json.dumps(new_items, ensure_ascii=False),
    }, follow_redirects=False)
    location = resp.headers.get("Location", "")
    assert "restore=" in location, "конфликтът трябва да пази въведеното"
    body = admin_client.get(location).data.decode()
    assert "БЕЛЕЖКА" in body, "заглавните полета се губят"
    assert "НОВ-1" in body, (
        "находка №1: РЕДОВЕТЕ се губят при конфликт — формата се връща с "
        "празна таблица, а съобщението твърди, че показва актуалните данни")

    # И най-важното: повторният запис от тази форма НЕ бива да занули
    # редовете на вече издадения документ.
    second_version = get_edit_doc_version(admin_client, location)
    post_with_csrf(admin_client, "/doc/1/edit", {
        "client_name": "Клиент", "edit_doc_version": second_version,
        "items_json": json.dumps(new_items, ensure_ascii=False),
    }, follow_redirects=True)
    con = db_module.get_db()
    saved = json.loads(
        con.execute("SELECT data FROM documents WHERE id = 1").fetchone()["data"])
    con.close()
    assert [i.get("code") for i in saved.get("items", [])] == ["НОВ-1"], (
        "находка №1: повторният запис ЗАНУЛИ редовете на издаден документ: %r"
        % saved.get("items"))


# --------------------------------------------------------------------- №2
def test_pdf_never_splits_a_number_in_half(admin_client):
    """Находка №2 (висока, остатък от находка №1 на 03.09): поправката
    премахна срутването на колоната, но остави равните ширини `100/n %` и
    `-pdf-word-wrap: CJK` върху ВСИЧКИ клетки. CJK чупи където и да е —
    включително между цифрите на едно число. Проверено: „3750.0“ + „0“ на
    следващия ред, „12.500“ + „0“, „120.50“ + „00“, HS кодът „842139“ +
    „90“. На екрана бланката изглежда безупречно."""
    items = [{"hs_code": "84213990", "po_no": "PO-2026-0001", "pos": "10",
              "material_code": "3HAC-7001-1", "net_weight": "120.5000",
              "qty": "300", "unit_price": "12.5000"}]
    post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "BR-2026-001",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/invoice-br/new", follow_redirects=True)
    reader = pypdf.PdfReader(io.BytesIO(admin_client.get("/doc/1/export.pdf").data))
    lines = [ln.strip() for page in reader.pages
             for ln in (page.extract_text() or "").splitlines() if ln.strip()]
    for value in ("84213990", "PO-2026-0001", "120.5000", "12.5000", "3750.00"):
        assert value in lines, (
            "находка №2: %r е разкъсано между редовете в PDF-а" % value)


def test_pdf_wraps_only_the_free_text_columns():
    """Същата находка, механизмът: CJK пренасянето важи само за колоните
    със свободен текст, а ширините са пропорционални на съдържанието."""
    import pdf_export

    css = open(os.path.join(ROOT, "templates", "pdf_export.html"),
               encoding="utf-8").read()
    assert ".items-table td.txt" in css and "-pdf-word-wrap: CJK" in css
    assert re.search(r"\.items-table th, \.items-table td \{[^}]*\}", css)
    body = re.search(r"\.items-table th, \.items-table td \{([^}]*)\}", css).group(1)
    assert "-pdf-word-wrap" not in body, (
        "находка №2: CJK пренасянето пак важи за ВСИЧКИ клетки, значи числата "
        "могат да се късат по средата")

    layout = pdf_export.pdf_column_layout(
        [("hs_code", "HS"), ("description", "Описание"), ("qty", "Кол")])
    widths = {key: (width, is_text) for key, _l, width, is_text in layout}
    assert widths["description"][1] is True and widths["qty"][1] is False
    assert widths["description"][0] > widths["qty"][0], (
        "находка №2: свободният текст трябва да получава повече ширина от "
        "числовата колона, не поравно")


# --------------------------------------------------------------------- №3
def test_a_document_with_five_thousand_rows_can_be_issued(admin_client, db_module):
    """Находка №3 (висока): Werkzeug 3.1 въведе ОТДЕЛЕН таван за нефайловите
    полета (`MAX_FORM_MEMORY_SIZE`, 500 000 байта по подразбиране), а
    програмата вдигаше само `MAX_CONTENT_LENGTH`. Всички редове пътуват в
    `items_json`. Проверено: 1500 реда (255 KB тяло!) вече не се издаваха —
    операторът виждаше „Файлът е твърде голям… Изберете по-малък файл“ при
    положение че няма никакъв файл. А Excel импортът изрично поддържа 5000
    реда, тоест програмата приемаше файла, казваше „Заредени 5000 реда“ и
    при „Издай“ губеше всичко."""
    items = [{"code": "МАТ-%05d" % i, "description": "Описание на артикул %d" % i,
              "qty": "10", "net": "1.5", "gross": "2.0", "packing": "кашон",
              "po_no": "ORD-%04d" % i} for i in range(5000)]
    resp = post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/packing/new", follow_redirects=True)
    assert resp.status_code == 200
    con = db_module.get_db()
    row = con.execute("SELECT data FROM documents ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    assert row is not None, "находка №3: документът изобщо не е записан"
    assert len(json.loads(row["data"]).get("items", [])) == 5000, (
        "находка №3: не всички редове са записани")


def test_too_large_request_message_matches_the_actual_cause(admin_client):
    """Същата находка: съобщението различава голям ФАЙЛ от твърде много
    РЕДОВЕ. „Изберете по-малък файл“ при заявка без файл активно насочваше
    оператора да търси несъществуващ файл."""
    import appcore

    source = open(os.path.join(ROOT, "appcore.py"), encoding="utf-8").read()
    assert "MAX_FORM_MEMORY_SIZE" in source, (
        "находка №3: тавана за полетата на формата пак го няма")
    assert "твърде много редове" in source, (
        "находка №3: съобщението пак говори само за файл")
    # Обработчикът НЕ бива да пипа request.files/request.form — това
    # стартира разбора на формата и хвърля същото изключение отново, вече
    # ВЪТРЕ в обработчика на грешката.
    handler = source[source.index("def _request_too_large("):]
    handler = handler[:handler.index("\n\n\n")]
    # Само ИЗПЪЛНИМИЯТ код — коментарът вътре нарочно СПОМЕНАВА двете имена,
    # за да предупреди следващия, който пипне обработчика.
    code = "\n".join(ln for ln in handler.splitlines()
                     if not ln.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert "request.files" not in code and "request.form" not in code, (
        "находка №3: обработчикът пак пипа формата и ще гръмне сам")
    assert "request.content_type" in code, (
        "находка №3: причината вече не се познава по типа на заявката")


# --------------------------------------------------------------------- №4
@pytest.mark.parametrize("doc_type", [
    "cmr", "packing", "pallet", "waybill", "dualuse", "export_it",
    "invoice_br", "invoice_no", "invoice_dubai", "zzz", "%27",
])
def test_invoice_list_survives_any_type_parameter(admin_client, doc_type):
    """Находка №4 (средна, дупка в находка №9 от 03.09): пазачът беше
    копиран дословно от routes_documents и проверяваше срещу `db.DOC_TYPES`,
    но ТОЗИ шаблон получава СТЕСНЕН речник (само фактурните типове). Шестте
    нефактурни типа минаваха проверката и гърмяха в шаблона с
    UndefinedError — тоест съставим отвън линк, който сваля страница на
    логнат служител, оставаше отворен, при това с по-невинен на вид адрес
    (`type=cmr`) от този в теста на самата находка (`type='`)."""
    assert admin_client.get("/invoices?type=" + doc_type).status_code == 200


# --------------------------------------------------------------------- №5
def test_lockout_is_extended_by_attempts_made_while_it_is_active(monkeypatch):
    """Находка №5 (средна, дупка в находка №11 от 03.09): `register_failure`
    започва с нулиране „изтекъл прозорец за броене“, а `first_ts` никога не
    се мести. Новото място на извикване (опит ПРИ заключен акаунт) беше
    първото, което достига този клон при вдигнат катинар — тоест собственият
    опит на нападателя изчистваше `locked_until`.

    Сценарият, при който катинарът е още жив в момента на изтичане на
    прозореца: четири опита в началото, петият — късно в прозореца."""
    import login_guard

    login_guard.reset_all()
    now = [1000.0]
    monkeypatch.setattr(login_guard.time, "time", lambda: now[0])
    try:
        for _ in range(login_guard.MAX_ATTEMPTS - 1):
            login_guard.register_failure("boss")
            now[0] += 1
        now[0] = 1000.0 + login_guard.WINDOW_SECONDS - 100
        login_guard.register_failure("boss")
        locked, _wait = login_guard.is_locked_out("boss")
        assert locked, "пробата изисква акаунтът да е заключен"

        now[0] = 1000.0 + login_guard.WINDOW_SECONDS + 1   # прозорецът изтича
        assert login_guard.is_locked_out("boss")[0], "катинарът трябва да е още жив"
        login_guard.register_failure("boss")               # опит на нападателя
        still_locked, wait = login_guard.is_locked_out("boss")
        assert still_locked, (
            "находка №5: опитът по време на заключване го ОТКЛЮЧИ — "
            "заключването имаше таван от %d сек. вместо да се удължава"
            % login_guard.WINDOW_SECONDS)
        assert wait > login_guard.LOCKOUT_SECONDS - 60, (
            "заключването трябва да е презаредено, а остават %s сек." % wait)
    finally:
        login_guard.reset_all()


# --------------------------------------------------------------------- №6
def test_bulk_issue_checks_the_per_card_header_numbers(admin_client):
    """Находка №6 (средна, дупка в находка №6 от 03.09): проверката на
    заглавните числа беше добавена само в единичното издаване и в
    редакцията. Груповото издаване вече носи ДВЕТЕ огледални проверки на
    редовете, добавени специално за него на 19.08 с коментара „груповото
    издаване не правеше НИТО ЕДНА от проверките, които единичното вече
    прави“ — третата не стигна дотам.

    Точно там е най-нужна: партида от десетки карти от Excel импорт, в
    която операторът няма как да прегледа всяко поле."""
    body = post_with_csrf(admin_client, "/pallet/bulk-issue", {
        "client_name": "Клиент", "groups": "1",
        "items_json_1": json.dumps([{"order_no": "O1", "pos": "10", "qty": "5"}],
                                   ensure_ascii=False),
        "gross_1": "-500", "height_1": "1.234,56",
        "pallet_type_1": "120x80", "packaging_type_1": "палет",
    }, csrf_source_url="/pallet/new", follow_redirects=True).data.decode()
    assert "не може да бъде разчетена" in body, (
        "находка №6: нечетимо бруто/височина минава мълчаливо при ГРУПОВО издаване")
    assert "отрицателна" in body, (
        "находка №6: отрицателно бруто минава мълчаливо при ГРУПОВО издаване")


# --------------------------------------------------------------------- №7
def test_total_packages_is_not_compared_against_piece_counts(admin_client):
    """Находка №7 (средна): проверката свързваше `total_packages` с колоната
    `qty`, но заглавието на колоната е „Брой / Qty, **pcs**“ (парчета), а
    полето е „Общо колети / Total **packages**“. Различни единици.
    Проверено: два реда по 24 и 12 броя, опаковани в 2 колета (вярната
    стойност) → предупреждение „въведено 2, а сборът дава 36“. Тоест
    ПРАВИЛНО попълненият документ винаги алармираше; ако операторът
    „поправи“ на 36, ЧМР-то ще казва 5 колета, а листът — 36."""
    items = [{"packing": "Палет", "description": "стока", "qty": "24",
              "volume": "0.5", "net": "100", "gross": "110"},
             {"packing": "Кашон", "description": "стока", "qty": "12",
              "volume": "0.2", "net": "40", "gross": "45"}]
    body = post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател", "total_packages": "2",
        "total_volume": "0.7", "total_net": "140", "total_gross": "155",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/packing/new", follow_redirects=True).data.decode()
    assert "Общо колети" not in body or "сборът на редовете" not in body, (
        "находка №7: правилно попълненият документ пак получава предупреждение")

    # Останалите три обобщения си имат точно съответстваща редова колона и
    # ТРЯБВА да продължат да предупреждават.
    body2 = post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател", "total_volume": "9.9",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/packing/new", follow_redirects=True).data.decode()
    assert "Общо обем" in body2 and "сборът на редовете" in body2, (
        "сверката на обема не бива да отпада заедно с тази на колетите")


# --------------------------------------------------------------------- №9
def test_pdf_queue_timeout_matches_the_measured_render_time():
    """Находка №9 (оптимизация): `_RENDER_LOCK_TIMEOUT = 20` беше обосновано
    в кода с „300 реда ≈ 2 сек“. Измерено: 300 реда = 5.8 сек след
    поправката на находка №2 (и 7.8 сек преди нея), 500 реда = 10.1 сек, а
    при по-широка таблица и 26 сек. Втори служител, натиснал бутона секунда
    по-късно, опираше в тавана и получаваше „опашката е заета“ при напълно
    изправна програма."""
    import pdf_export
    assert pdf_export._RENDER_LOCK_TIMEOUT >= 60, (
        "находка №9: таванът на опашката (%s сек.) е под реалното време за "
        "голям документ" % pdf_export._RENDER_LOCK_TIMEOUT)


# -------------------------------------------------------------------- №10
def test_search_is_correct_and_does_not_lowercase_whole_documents(admin_client):
    """Находка №10 (оптимизация): `haystack.lower()` правеше КОПИЕ на цялото
    JSON тяло на документа (средно 8 KB) за всеки ред, при всяко натискане
    на клавиш. Профил при 20 000 документа: 192 960 извиквания на
    `str.lower()` = 2 154 от 2 807 ms; една заявка четеше 170 MB от файла.
    Плюс `paginate_documents` изпълняваше COUNT и SELECT с една и съща
    WHERE клауза, тоест всеки ред се обхождаше ДВА пъти.

    Измерено след поправката при 12 000 документа: търсене по номер 856 →
    111 ms, по текст 463 → 107 ms. Тук се заключва КОРЕКТНОСТТА (регексът
    трябва да сгъва кирилицата точно както `str.lower()`).

    ЗАБЕЛЕЖКА: този тест НАРОЧНО минава и преди, и след поправката — той е
    предпазителят, че оптимизацията не е променила ОТГОВОРИТЕ. Самата
    оптимизация се заключва от `test_first_page_of_a_list_needs_only_one_pass`
    и `test_client_name_column_is_maintained_and_used`, които падат преди."""
    import db

    for name, number in (("Иван Петров", "A-1"), ("иван петров", "A-2"),
                         ("ABB Швеция", "A-3"), ("Прекъсвач ЕООД", "A-4")):
        post_with_csrf(admin_client, "/invoice-br/new", {
            "consignee_name": name, "invoice_number": number,
            "items_json": json.dumps(
                [{"material_code": "МАТ-%s" % number,
                  "description": "Дълго описание " * 40}], ensure_ascii=False),
        }, csrf_source_url="/invoice-br/new", follow_redirects=True)

    for needle, expected in (("иван", 2), ("ИВАН", 2), ("abb", 1),
                             ("Прекъсвач", 1), ("МАТ-A-3", 1), ("няма такова", 0)):
        body = admin_client.get("/invoices?q=" + needle).data.decode()
        found = sum(1 for i in range(1, 5) if ("A-%d" % i) in body)
        assert found == expected, (
            "находка №10: търсенето на %r намери %d вместо %d документа"
            % (needle, found, expected))

    # Дългият низ минава през регекса, късият — по стария път; и двата дават
    # един и същ отговор.
    long_haystack = "х" * 300 + "ПрЕкЪсВаЧ" + "у" * 300
    assert db._ci_contains(long_haystack, "прекъсвач") is True
    assert db._ci_contains("Иван Петров", "иван") is True
    assert db._ci_contains("х" * 300, "няма") is False
    # Специалните знаци НЕ се тълкуват като регекс.
    assert db._ci_contains("а" * 300 + "A.B*C", "a.b*c") is True
    assert db._ci_contains("а" * 300 + "AXBYC", "a.b*c") is False


def test_long_haystack_actually_uses_the_compiled_regex_path():
    """Одит (собствена проверка при прилагането на v3.72.0, находка №10):
    предишният тест заключва само ОТГОВОРИТЕ (нарочно — самата оптимизация
    не бива да променя резултата). Но нищо в патча не заключваше, че
    регексовият път изобщо се ИЗПЪЛНЯВА за дълги низове — `db._ci_pattern`
    можеше тихо да отпадне (напр. при бъдещо преработване) и тестът за
    коректност пак щеше да минава, защото старият `.lower() in .lower()`
    дава СЪЩИЯ отговор. Тук се проверява самият МЕХАНИЗЪМ: кешът на
    `_ci_pattern` расте само за дълги игли, точно над/под прага."""
    import db

    db._ci_pattern.cache_clear()
    needle = "уникална-игла-%d" % os.getpid()
    # Общата ДЪЛЖИНА на низа е прагът, не само подложката отпред — иначе
    # needle-ът сам я избутва над прага и „късият“ случай не е кратък.
    short_haystack = "х" * (db._CI_REGEX_MIN_HAYSTACK - 1 - len(needle)) + needle
    long_haystack = "х" * db._CI_REGEX_MIN_HAYSTACK + needle
    assert len(short_haystack) < db._CI_REGEX_MIN_HAYSTACK
    assert len(long_haystack) >= db._CI_REGEX_MIN_HAYSTACK

    db._ci_contains(short_haystack, needle)
    assert db._ci_pattern.cache_info().misses == 0, (
        "находка №10: късият низ пак минава през регекса — прагът не важи")

    db._ci_contains(long_haystack, needle)
    assert db._ci_pattern.cache_info().misses == 1, (
        "находка №10: дългият низ НЕ мина през компилирания регекс — "
        "оптимизацията е изчезнала, макар отговорите да съвпадат случайно")


def test_first_page_of_a_list_needs_only_one_pass(admin_client, db_module):
    """Втората половина на находка №10: при първа страница, която се побира
    изцяло, вече не се прави отделен COUNT."""
    import appcore

    queries = []
    con = db_module.get_db()
    con.set_trace_callback(queries.append)
    try:
        docs, page, pages, total = appcore.paginate_documents(
            con, "", [], 1, page_size=100)
    finally:
        con.set_trace_callback(None)
        con.close()
    assert page == 1 and pages == 1 and total == len(docs)
    assert not any("COUNT(*)" in q for q in queries), (
        "находка №10: първата страница пак прави втори пълен пас за броене")


# -------------------------------------------------------------------- №11
def test_client_name_column_is_maintained_and_used(admin_client, db_module):
    """Находка №11 (оптимизация): три екрана вадеха името на клиента от JSON
    тялото при всяко зареждане — групирането (441 ms и 170 MB при 20 000
    документа), таблото (176 ms, 730 извиквания на `json.loads`) и картата
    на клиент (`LIKE '%име%'`, до 2 576 ms при студен кеш). Постоянната,
    индексирана колона ги сваля до десетки милисекунди и под 2 MB четене —
    на мрежов диск това е най-голямата практическа полза."""
    post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "Аутолив АД"},
                   csrf_source_url="/cmr/new", follow_redirects=True)
    con = db_module.get_db()
    row = con.execute("SELECT client_name FROM documents WHERE id = 1").fetchone()
    assert row["client_name"] == "Аутолив АД", (
        "находка №11: колоната не се попълва при вмъкване (липсва тригер?)")

    # И при промяна на данните — тригерът за UPDATE.
    con.execute("UPDATE documents SET data = ? WHERE id = 1",
                (json.dumps({"consignee_name": "Друга фирма"}, ensure_ascii=False),))
    con.commit()
    row = con.execute("SELECT client_name FROM documents WHERE id = 1").fetchone()
    assert row["client_name"] == "Друга фирма", (
        "находка №11: колоната не се обновява при промяна на данните")

    plan = [r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM documents WHERE client_name = 'x'")]
    con.close()
    assert any("idx_documents_client_name" in step for step in plan), (
        "находка №11: индексът по client_name не се ползва: %s" % plan)

    assert admin_client.get("/docs?group=client").status_code == 200
    assert admin_client.get("/").status_code == 200


# -------------------------------------------------------------------- №12
def test_invoice_address_book_is_not_embedded_whole(admin_client, db_module):
    """Находка №12 (оптимизация): формите за фактура вграждаха ЦЯЛАТА
    адресна книга — веднъж като <option>-и и втори път като JSON. Измерено:
    500 записа = 357 KB HTML, 2 000 записа = 1 103 KB на ВСЯКО отваряне.
    Общата адресна книга получи таван и сървърно търсене на 19.08 (находка
    №25); тази остана непокрита — същият дефектен клас."""
    import invoice_clients_module

    con = db_module.get_db()
    con.executemany(
        "INSERT INTO invoice_clients (name, delivery_name, delivery_address,"
        " billing_name, billing_address) VALUES (?, ?, ?, ?, ?)",
        [("Клиент %04d" % i, "Д %04d" % i, "адрес " * 12,
          "Ф %04d" % i, "адрес " * 12) for i in range(2000)])
    con.commit()
    con.close()

    body = admin_client.get("/invoice-dubai/new").data
    assert len(body) < 400 * 1024, (
        "находка №12: формата пак носи цялата книга — %d KB" % (len(body) // 1024))

    resp = admin_client.get("/invoices/clients/lookup?q=Клиент 0007")
    data = resp.get_json()
    assert resp.status_code == 200 and len(data["clients"]) == 1
    # Отговорът трябва да носи ПЪЛНИТЕ полета, за да работи попълването на
    # двата адресни блока еднакво.
    for field in ("delivery_name", "delivery_address", "billing_name",
                  "billing_address"):
        assert field in data["clients"][0], (
            "находка №12: липсва %s — автопопълването ще се разминава между "
            "вградените и намерените записи" % field)
    assert invoice_clients_module.EMBED_LIMIT <= 500


# -------------------------------------------------------------------- №13
def test_active_navigation_item_is_readable_in_every_theme():
    """Находка №13 (графична, висока): `.nav-item.active`, `.avatar` и
    езиковият превключвател закодираха твърдо `color: #fff`, а в темата
    „висок контраст“ активният фон е #ffe600 — измерен контраст 1.27:1 при
    изискване 4.5. Иконата е `currentColor`, значи и тя беше бяла върху
    жълто. Темата съществува единствено заради слабо зрение, а служителят
    губеше точно указателя КОЙ екран е отворен."""
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    assert "--sidebar-active-fg" in css
    assert ".nav-item.active { background: var(--sidebar-active); color: var(--sidebar-active-fg); }" in css, (
        "находка №13: активният пункт пак е с твърдо закодиран цвят")

    # Всяка тема трябва да дефинира жетона, иначе някоя остава без него.
    themes = re.findall(r"--sidebar-active:\s*#[0-9a-fA-F]+;[^\n]*", css)
    assert themes, "не са намерени темите"
    for line in themes:
        assert "--sidebar-active-fg" in line, (
            "находка №13: тема без --sidebar-active-fg: %s" % line.strip()[:70])

    # Контрастът в контрастната тема — изчислен, не на око.
    contrast_block = css[css.index('[data-theme="contrast"]'):]
    contrast_block = contrast_block[:contrast_block.index("}")]
    assert "--sidebar-active-fg: #000" in contrast_block, (
        "находка №13: в контрастната тема текстът върху жълтото трябва да е черен")


def _contrast(fg, bg):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(hex_color):
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_scanner_field_placeholder_is_readable_in_every_theme():
    """Находка №14 (графична): плейсхолдърът на полето за баркод ползва
    `--sidebar-fg-dim` върху фона на кутията за сканиране. В сепия темата
    контрастът беше 4.01:1 — под прага 4.5. Това е най-натиснатото поле в
    програмата: скенерът пише в него по цял ден.

    (Първоначалното твърдение в доклада — 1.66:1 в няколко теми — НЕ се
    потвърди: то беше измерено спрямо бял фон, а кутията стои върху тъмната
    лента. Реалните стойности са между 4.70 и 14.54; единствено сепия беше
    под прага.)"""
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    # Съкратеният запис (#000 в контрастната тема) е СЪЩО валиден CSS —
    # регулярен израз само за шестцифрен запис изпуска цяла тема.
    # Шестцифреният запис е ПЪРВИ в редуването: обратният ред реже #a3c2e8
    # до #a3c и мери контраст на несъществуващ цвят.
    hex_re = r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b"
    pairs = re.findall(
        r"--sidebar-bg:\s*(%s);[^\n]*?--sidebar-fg-dim:\s*(%s)" % (hex_re, hex_re),
        css)
    assert len(pairs) >= 6, "очакват се шест теми, намерени %d" % len(pairs)

    def _full(value):
        body = value.lstrip("#")
        return "#" + ("".join(ch * 2 for ch in body) if len(body) == 3 else body)

    for bg, dim in pairs:
        bg, dim = _full(bg), _full(dim)
        # Кутията е rgba(255,255,255,.08) върху фона на лентата.
        blended = "#%02x%02x%02x" % tuple(
            round(255 * 0.08 + int(bg.lstrip("#")[i:i + 2], 16) * 0.92)
            for i in (0, 2, 4))
        ratio = _contrast(dim, blended)
        assert ratio >= 4.5, (
            "находка №14: плейсхолдърът %s върху %s дава %.2f:1 (нужни 4.5)"
            % (dim, blended, ratio))


# -------------------------------------------------------------------- №17
def test_only_one_qr_hint_is_shown_at_a_time(admin_client):
    """Находка №17 (графична): `print_qr_is_local` и `local_hint` са
    ЕДНОВРЕМЕННО истина в подразбиращото се състояние (няма постоянен
    публичен адрес + тунелът е спрян) — тоест при ВСЕКИ документ излизаха
    две жълти кутии, които казват едно и също с различни думи и сочат две
    различни настройки. Измерено: 90px + 103px в 277px висок QR блок."""
    post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "Получател"},
                   csrf_source_url="/cmr/new", follow_redirects=True)
    body = admin_client.get("/doc/1").data.decode()
    hints = body.count('class="doc-qr-hint no-print"')
    assert hints <= 1, (
        "находка №17: %d подсказки за QR кода наведнъж — операторът спира да "
        "ги чете" % hints)


# -------------------------------------------------------------------- №18
def test_buttons_share_one_height():
    """Находка №18: `button`/`.btn` бяха с `border: 0`, `.btn-secondary` с
    1px, `.btn-outline` с 1.5px — а рамката добавя височина. Измерено в
    един и същи ред: 36.0 / 38.0 / 40.3 / 42.3 px."""
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    base = re.search(r"\nbutton, \.btn \{([^}]*)\}", css).group(1)
    assert "border: 1px solid transparent" in base, (
        "находка №18: основният бутон пак е без рамка, значи е по-нисък от съседа си")
    outline = re.search(r"\.btn-outline \{([^}]*)\}", css).group(1)
    assert "1.5px" not in outline, (
        "находка №18: .btn-outline пак е с 1.5px рамка")


def test_printed_numbers_are_right_aligned(admin_client):
    """Находка №18: числата в бланките бяха центрирани, а заглавията им —
    вляво, тоест теглата стояха с блуждаещи десетични запетаи, а редът ОБЩО
    не се подреждаше под колоната си. Митничарят събира надолу по колоната."""
    items = [{"packing": "кашон", "description": "стока", "qty": "3",
              "length": "1", "width": "2", "height": "3", "volume": "0.884",
              "net": "15.7500", "gross": "123.0000"}]
    post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/packing/new", follow_redirects=True)
    body = admin_client.get("/doc/1").data.decode()
    assert '<td class="n">' in body, (
        "находка №18: числовите клетки пак нямат собствен клас")

    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    assert ".print-page table td.n" in css and "text-align: right" in css
    assert "font-variant-numeric: tabular-nums" in css, (
        "находка №18: цифрите пак са с различна ширина в една колона")


def test_document_view_has_padding_and_its_own_scroll():
    """Находка №18 + №16: `.container-print` съществуваше в деветте печатни
    шаблона, но нямаше НИТО ЕДНО CSS правило — лентата с действия беше
    залепена за горния ръб, а бланката (210mm) плъзгаше цялата страница."""
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    rule = re.search(r"\.container-print \{([^}]*)\}", css)
    assert rule, "находка №18: `.container-print` пак няма нито едно правило"
    assert "padding" in rule.group(1) and "overflow-x: auto" in rule.group(1)
    # …и при печат отстъпите отпадат, иначе се връща находка №11 от 19.08.
    print_block = css[css.index("@media print {"):]
    reset = re.search(r"\.container-print \{([^}]*)\}", print_block)
    assert reset, (
        "находка №18: `.container-print` не е нулиран при печат — на хартията "
        "ще останат отстъпи и бланката ще стане по-висока от листа")
    assert "padding: 0 !important" in reset.group(1), (
        "находка №18: при печат отстъпите на `.container-print` остават — "
        "връща се находка №11 от 19.08 (бланката прелива на втори лист)")


def test_file_picker_button_follows_the_theme():
    """Находка №18: вграденият бутон на `input[type=file]` не се сменяше с
    темата — измерено `rgb(239,239,239)` върху черна карта с жълта рамка
    във всичките шест теми."""
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    assert "::file-selector-button" in css, (
        "находка №18: бутонът за избор на файл пак е браузърният по подразбиране")


# ------------------------------------------------------- подобрения в работата
def test_pull_from_pallet_accepts_the_short_number(admin_client):
    """Подобрение: съвпадението беше ТОЧНО (`barcode = ? OR number = ?`),
    затова „1“, „0001“ и „1/2026“ даваха „Няма документ“, а работеше само
    пълното „0001/2026“. На самата карта обаче пише „Палет № 1 от 3“ —
    операторът пише точно краткия вид."""
    post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Клиент",
        "items_json": json.dumps([{"code": "A", "description": "стока", "qty": "4"}],
                                 ensure_ascii=False),
    }, csrf_source_url="/pallet/new", follow_redirects=True)
    for code in ("1", "0001", "1/2026", "0001/2026"):
        data = post_with_csrf(admin_client, "/packing/pull-pallet", {"code": code},
                              csrf_source_url="/packing/new").get_json()
        assert data.get("ok"), (
            "подобрение: краткият номер %r не се разпознава (%s)"
            % (code, data.get("error")))
    missing = post_with_csrf(admin_client, "/packing/pull-pallet", {"code": "9999"},
                             csrf_source_url="/packing/new").get_json()
    assert not missing["ok"] and "Пълният номер" in missing["error"], (
        "съобщението трябва да подсказва формата, не само че няма такъв документ")


def test_rejected_public_url_stays_in_the_field(admin_client):
    """Подобрение: сгрешеният адрес изчезваше и трябваше да се пише отначало,
    докато съседните форми точно в такъв случай пазят въведеното."""
    body = post_with_csrf(admin_client, "/admin/system", {
        "form": "public_base_url", "public_base_url": "не е адрес!!",
    }, csrf_source_url="/my-settings", follow_redirects=True).data.decode()
    assert "не изглежда валиден" in body
    assert "не е адрес!!" in body, (
        "подобрение: въведеното изчезва и трябва да се пише отначало")


def test_bulk_import_carries_the_already_entered_client(admin_client):
    """Подобрение: формата за качване е ОТДЕЛЕН <form> над основната, затова
    всичко въведено под нея се изхвърляше при качването — операторът
    избираше клиент, качваше Excel-а и се озоваваше с празно поле „Фирма“."""
    source = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    assert "bindCarryOverForms" in source
    template = open(os.path.join(ROOT, "templates", "pallet_form.html"),
                    encoding="utf-8").read()
    assert 'data-carry-from="main-doc-form"' in template
    assert "client_name" in template.split('data-carry-fields="')[1].split('"')[0]

    route = open(os.path.join(ROOT, "routes_pallet_extra.py"), encoding="utf-8").read()
    assert "carried" in route and "shared=carried" in route, (
        "подобрение: сървърът не подава пренесените данни към екрана за преглед")


def test_bulgarian_singular_forms_exist():
    """Подобрение: „Намерени 1 резултата“, „Заредени 1 реда“ и „ORD-5001 — 1
    реда“ се виждаха при всяко търсене с едно попадение."""
    source = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    assert "function tfp(" in source, "липсва помощната за единствено число"
    for key in ("live_search_found_one", "po_rows_count_one",
                "loaded_from_file_one", "loaded_from_pallet_one"):
        assert key in source, "подобрение: липсва ключ %s" % key
    base = open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    for key in ("live_search_found_one", "po_rows_count_one",
                "loaded_from_file_one", "loaded_from_pallet_one"):
        assert ("'%s'" % key) in base, (
            "подобрение: ключът %s го няма в речника, значи ще остане на "
            "български при EN/TR" % key)
