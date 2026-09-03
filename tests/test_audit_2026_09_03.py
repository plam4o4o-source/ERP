# -*- coding: utf-8 -*-
"""Регресионни тестове за единайсетия одит (03.09.2026) — находки №1–№25.

Всяка функция тук е ЗАКЛЮЧВАЩА за конкретна находка: преди поправката пада,
след нея минава. Проверено е и в двете посоки с реално изпълнение (`git
stash` на съответния файл), не по прочит.

Находки №16, №17 и №20 (пазач срещу двойно зареждане, цвят на съобщенията,
свързани етикети) живеят и в tests/test_e2e_smoke.py — те се виждат само в
истински браузър.
"""
import io
import json
import os
import re
import sqlite3
import threading

import openpyxl
import pypdf
import pytest
from werkzeug.security import generate_password_hash

from conftest import get_csrf_token, get_edit_doc_version, post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------- №1
def _issue_dubai_invoice(admin_client, rows=20):
    items = [{"hs_code": "84213990", "po_no": "PO-2026-%04d" % i, "pos": str(i * 10),
              "material_code": "MC-%06d" % i, "qty": "10", "unit_price": "1.25"}
             for i in range(1, rows + 1)]
    post_with_csrf(admin_client, "/invoice-dubai/new", {
        "consignee_name": "ABB Dubai", "invoice_number": "INV-PDF-1",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/invoice-dubai/new", follow_redirects=True)


def test_empty_cell_does_not_collapse_a_pdf_column(admin_client):
    """Находка №1 (КРИТИЧНА): xhtml2pdf пренаписва ширината на колоната за
    ВСЯКА клетка, а за клетка БЕЗ дъщерни възли слага само двата padding-а —
    9pt, тоест един знак. Последната обработена клетка печели, значи ЕДНА
    празна клетка свива ЦЯЛАТА колона, а `-pdf-word-wrap: CJK` разлива
    текста вертикално, буква на ред. При трите фактури това се случваше
    ВИНАГИ, защото редът TOTAL е списък от празни низове с попълнени
    три-четири клетки."""
    _issue_dubai_invoice(admin_client)
    resp = admin_client.get("/doc/1/export.pdf")
    assert resp.status_code == 200
    reader = pypdf.PdfReader(io.BytesIO(resp.data))
    assert len(reader.pages) <= 4, (
        "находка №1: 20-редова фактура се разля на %d страници — колоните са "
        "свити до един знак" % len(reader.pages))
    text = "".join(p.extract_text() or "" for p in reader.pages)
    # Свитата колона разлива текста вертикално — буква на ред. Броим редовете
    # от ЕДИН знак: при здрава таблица те са единици (цифри в тесни колони),
    # при свита — стотици.
    single_char_lines = [ln for ln in text.splitlines() if len(ln.strip()) == 1]
    assert len(single_char_lines) < 40, (
        "находка №1: %d реда от по един знак — колона е свита до ширината на "
        "своя padding" % len(single_char_lines))
    # Заглавията на колоните трябва да остават четими, не разкъсани.
    assert "HS code" in text


# --------------------------------------------------------------------- №2
def test_pdf_barcode_bars_have_the_exact_module_width():
    """Находка №2 (висока): `PIL.ImageDraw.rectangle` рисува ВКЛЮЧИТЕЛНО
    крайната координата, затова всяка лента излизаше с 1 пиксел по-широка от
    модула си, а следващата празнина — с 1 по-тясна. При module_width=2 това
    е систематична грешка от ±0.5 модула на ВСЕКИ елемент, далеч извън
    толеранса на Code128 — тоест баркодът в PDF износа не се четеше от
    скенер, докато същият баркод на хартиената бланка (SVG) се четеше."""
    import base64

    from PIL import Image

    import barcode128

    module_width = 2
    uri = barcode128.code128_png_data_uri("PAL-03092026-0001",
                                          module_width=module_width, height=40)
    img = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("L")
    px = img.load()
    width, height = img.size
    row = [1 if px[x, height // 2] < 128 else 0 for x in range(width)]
    runs, current, count = [], row[0], 0
    for value in row:
        if value == current:
            count += 1
        else:
            runs.append(count)
            current, count = value, 1
    runs.append(count)
    runs = runs[1:-1]  # без тихите зони
    bad = [n for n in runs if n % module_width]
    assert not bad, (
        "находка №2: ширини, които не са кратни на модула (%d): %s"
        % (module_width, bad[:10]))


# --------------------------------------------------------------------- №3
def test_pdf_repeats_the_header_row_and_the_document_number(admin_client):
    """Находка №3 (висока): поправката на находка №12 от 19.08 (повтарящ се
    заглавен ред + идентификация на лист 2) стигна до всичките шест печатни
    шаблона, но не и до PDF износа. Лист 2 и нататък беше гола решетка от
    числа — без имена на колоните и без нищо, по което да бъде върнат при
    своя документ."""
    items = [{"packing": "кашон", "description": "стока %d" % i, "qty": "1",
              "length": "1", "width": "2", "height": "3", "volume": "0.006",
              "net": "4", "gross": "5"} for i in range(1, 41)]
    post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/packing/new", follow_redirects=True)
    reader = pypdf.PdfReader(io.BytesIO(admin_client.get("/doc/1/export.pdf").data))
    assert len(reader.pages) > 1, "пробата трябва да е многостранична"
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        assert "0001/2026" in text, (
            "находка №3: страница %d няма номера на документа" % index)
        if index > 1:
            assert "Бруто" in text, (
                "находка №3: страница %d няма заглавния ред на таблицата" % index)


# --------------------------------------------------------------------- №4
def test_waybill_table_repeats_its_header_and_number(admin_client):
    """Находка №4 (ниска): последната непокрита половина на находка №12 от
    19.08 — товарителницата беше единственият печатен шаблон с таблица
    редове, който не получи `<thead>` + `print_table_ident`. Форматът ѝ е
    „две копия на един A4“, тоест преливането настъпва по-рано."""
    items = [{"description": "стока %d" % i, "packing": "кашон",
              "marks": "M%d" % i, "weight": "1.5", "qty": "2"} for i in range(3)]
    post_with_csrf(admin_client, "/waybill/new", {
        "consignee_name": "Получател",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/waybill/new", follow_redirects=True)
    body = admin_client.get("/doc/1").data.decode()
    assert "<thead>" in body, "находка №4: товарителницата няма заглавен ред в <thead>"
    assert "print-ident" in body, "находка №4: товарителницата не повтаря номера си"
    assert "Товарителница №" in body

    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    assert ".twb table.twb-goods thead" in css, (
        "находка №4: CSS правилото за повтарящ се заглавен ред не покрива .twb")


# --------------------------------------------------------------------- №5
def test_excel_keeps_the_real_precision_of_weights(admin_client):
    """Находка №5 (висока): маската „0.###“ режеше показаните тегла/обеми до
    три знака, докато бланката и PDF-ът (fmt_num пази въведената точност)
    казват пълната стойност. Справочникът материали записва теглата с
    „%.6f“, тоест 4–6 знака са норма — получателят на Excel файла смяташе
    100 × 0.088 = 8.8 кг, а хартията казваше 8.75."""
    items = [{"hs_code": "8421", "po_no": "PO1", "pos": "10", "material_code": "M1",
              "net_weight": "0.0875", "qty": "100", "unit_price": "1.25"},
             {"hs_code": "8421", "po_no": "PO1", "pos": "20", "material_code": "M2",
              "net_weight": "0.087135", "qty": "10", "unit_price": "2.50"}]
    post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "MASK-1",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/invoice-br/new", follow_redirects=True)
    ws = openpyxl.load_workbook(
        io.BytesIO(admin_client.get("/doc/1/export.xlsx").data)).active
    weights = [c for row in ws.iter_rows() for c in row
               if isinstance(c.value, float) and abs(c.value - 0.087135) < 1e-9]
    assert weights, "тестовата стойност не е намерена в изнесения файл"
    for cell in weights:
        shown = _excel_decimals(cell.number_format)
        assert shown >= 6, (
            "находка №5: тегло 0.087135 се ПОКАЗВА с %d знака (маска %r) — "
            "получателят чете друго число от това на бланката"
            % (shown, cell.number_format))


def _excel_decimals(number_format):
    if "." not in (number_format or ""):
        return 0
    return len(number_format.split(".", 1)[1].rstrip("%"))


# --------------------------------------------------------------------- №6
@pytest.mark.parametrize("path,fields,csrf_url", [
    ("/cmr/new", {"consignee_name": "Получател", "weight": "1.234,56",
                  "volume": "-3"}, "/cmr/new"),
    ("/pallet/new", {"client_name": "Клиент", "gross": "1.234,56",
                     "height": "-10"}, "/pallet/new"),
])
def test_header_numbers_are_checked_too(admin_client, path, fields, csrf_url):
    """Находка №6 (висока): двете проверки за числа обхождаха САМО редовете.
    ЧМР изобщо няма редове, тоест кутии 11 „Бруто тегло“ и 12 „Обем“ — самите
    товарни показатели на митническия превозен документ — не се проверяваха
    от нищо. Издаваше се с „1.234,56“ (число, което може да се прочете и
    като 1,23456, и като 1234,56) или с отрицателно тегло, без нито едно
    предупреждение."""
    body = post_with_csrf(admin_client, path, fields,
                          csrf_source_url=csrf_url, follow_redirects=True).data.decode()
    assert "не може да бъде разчетена" in body, (
        "находка №6: нечетимо заглавно число мина без предупреждение")
    assert "отрицателна" in body, (
        "находка №6: отрицателно заглавно число мина без предупреждение")


# --------------------------------------------------------------------- №7
def test_expired_preview_token_on_a_new_document_says_so(admin_client):
    """Находка №7 (висока): поправката на находка №31 от 16.08 (предупреждение
    при изтекъл `?restore=`) стигна само до РЕДАКЦИЯТА. При издаване на НОВ
    документ клонът беше без `else` — формата се рендираше празна, без нито
    дума. Токенът изчезва по три битови причини: изтичане, рестарт на
    процеса (при авто-обновяване това става само́) и изхвърляне по брой."""
    import appcore

    resp = post_with_csrf(admin_client, "/invoice-br/preview", {
        "consignee_name": "ABB", "invoice_number": "INV-777",
        "items_json": json.dumps([{"po_no": "PO1", "qty": "5"}], ensure_ascii=False),
    }, csrf_source_url="/invoice-br/new", follow_redirects=False)
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    with appcore._preview_lock:
        appcore._preview_store.clear()
    body = admin_client.get("/invoice-br/new?restore=%s" % token).data.decode()
    assert "вече не са налични" in body, (
        "находка №7: изтеклият преглед връща празна форма БЕЗ никакво "
        "съобщение — операторът не разбира, че данните са били живи")


def test_preview_store_survives_several_operators():
    """Същата находка, втора половина: таванът от 20 записа беше под реалната
    употреба. В мрежов режим ВСИЧКИ оператори споделят един процес и един
    общ таван, а всяко натискане на „Предварителен преглед“ и всяко
    запазване при грешка заема по един запис."""
    import appcore
    assert appcore._PREVIEW_MAX_ENTRIES >= 100, (
        "находка №7: таванът на прегледите (%d) е под реалната употреба при "
        "няколко оператора" % appcore._PREVIEW_MAX_ENTRIES)


# --------------------------------------------------------------------- №8
def test_rows_without_a_pallet_number_are_reported():
    """Находка №8 (средна): всеки ред с празна/нечетима групираща клетка
    падаше към карта №1 — без брояч и без предупреждение, докато всяко друго
    тихо влошаване в същия импорт се съобщава изрично. Палетна карта №1,
    която отива при клиента и се лепи на палета, изброяваше стока, физически
    намираща се другаде, и обявяваше завишен „Общ брой“."""
    from routes_pallet_extra import _parse_group_numbers
    assert _parse_group_numbers("1") == ([1], False)
    assert _parse_group_numbers("1+3") == ([1, 3], False)
    assert _parse_group_numbers("") == ([1], True)
    assert _parse_group_numbers(None) == ([1], True)
    assert _parse_group_numbers("абв") == ([1], True)


# --------------------------------------------------------------------- №9
@pytest.mark.parametrize("path", ["/docs", "/invoices"])
def test_unknown_document_type_does_not_crash(admin_client, path):
    """Находка №9 (висока), първа половина: `type` се валидираше само за SQL
    филтъра, а суровата стойност стигаше до шаблона, който прави
    `doc_types[sel_type].title` → UndefinedError. Тоест анонимно съставим
    линк сваляше 500-ка на всяка от двете страници."""
    resp = admin_client.get(path + "?type=%27",
                            headers={"Referer": "https://evil.example.com/phish"})
    assert resp.status_code == 200, (
        "находка №9: непознат тип документ още сваля страницата (%d)"
        % resp.status_code)


def test_error_redirect_never_leaves_the_application(flask_app, db_module):
    """Находка №9, втора половина: `request.referrer` минаваше в
    `redirect()` без никаква проверка — само сравнение с текущия път.
    Всяка необработена грешка ставаше отворено пренасочване: линк към
    гърмяща страница, изпратен на логнат служител от сайт с
    `Referrer-Policy: unsafe-url`, го изхвърляше на ЧУЖД домейн, където
    копие на екрана за вход прибира паролата му."""
    import appcore

    @flask_app.route("/tmp-boom-external")
    @appcore.login_required
    def _tmp_boom_external():
        raise RuntimeError("гърми")

    con = db_module.get_db()
    con.execute("INSERT INTO users (username, password_hash, full_name, role,"
                " active, must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
                ("redir_admin", generate_password_hash("test-password-123"), "А"))
    con.commit()
    con.close()
    client = flask_app.test_client()
    post_with_csrf(client, "/login",
                   {"username": "redir_admin", "password": "test-password-123"},
                   csrf_source_url="/login")

    for evil in ("https://evil.example.com/phish", "//evil.example.com/x",
                 "/\\evil.example.com/x"):
        resp = client.get("/tmp-boom-external", headers={"Referer": evil})
        target = resp.headers.get("Location", "")
        assert "evil.example.com" not in target, (
            "находка №9: пренасочване към ЧУЖД домейн след грешка (%r)" % target)


# -------------------------------------------------------------------- №10
def test_logout_revokes_the_cookie_not_just_the_browser_copy(flask_app, db_module):
    """Находка №10 (висока): `logout()` правеше само `session.clear()` —
    тоест триеше бисквитката В БРАУЗЪРА. Сесиите са подписани бисквитки,
    сървърът не пази нищо, значи всяко снето копие оставаше валидно до
    изтичането на 12-часовия срок. Механизмът за отнемане (`session_epoch`)
    съществува и се ползва при смяна на парола — само най-често
    използваният начин за прекратяване на сесия не го викаше."""
    con = db_module.get_db()
    con.execute("INSERT INTO users (username, password_hash, full_name, role,"
                " active, must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
                ("boss", generate_password_hash("test-password-123"), "Шеф"))
    con.commit()
    con.close()
    client = flask_app.test_client()
    post_with_csrf(client, "/login",
                   {"username": "boss", "password": "test-password-123"},
                   csrf_source_url="/login")
    stolen = client.get_cookie("session")
    assert stolen is not None
    assert client.get("/").status_code == 200
    client.get("/logout")

    thief = flask_app.test_client()
    thief.set_cookie("session", stolen.value, domain="localhost")
    assert thief.get("/").status_code != 200, (
        "находка №10: откраднатата бисквитка още работи СЛЕД изход")


# -------------------------------------------------------------------- №11
def test_account_lockout_is_extended_by_further_attempts(flask_app, db_module,
                                                         monkeypatch):
    """Находка №11 (висока): `register_failure` стоеше само в `else` клона,
    тоест докато акаунтът е заключен, опитите НЕ се броят — заключването
    изтичаше след точно 5 минути и броенето започваше от нула. Проверено:
    след 40 грешни опита оставаха 297 от 300 секунди, тоест 35-те опита след
    петия не бяха добавили нищо, а MAX_ATTEMPTS/LOCKOUT_SECONDS не
    ограничаваха нищо."""
    import login_guard

    monkeypatch.setattr(login_guard, "_IP_MAX_ATTEMPTS", 10 ** 9, raising=False)
    monkeypatch.setattr(login_guard, "_GLOBAL_MAX_ATTEMPTS", 10 ** 9, raising=False)
    con = db_module.get_db()
    con.execute("INSERT INTO users (username, password_hash, full_name, role,"
                " active, must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
                ("boss2", generate_password_hash("test-password-123"), "Шеф"))
    con.commit()
    con.close()
    client = flask_app.test_client()

    for _ in range(login_guard.MAX_ATTEMPTS):
        post_with_csrf(client, "/login",
                       {"username": "boss2", "password": "грешна"},
                       csrf_source_url="/login")
    _, right_after_lock = login_guard.is_locked_out("boss2")

    fake_now = [None]
    real_time = login_guard.time.time

    monkeypatch.setattr(login_guard.time, "time",
                        lambda: (real_time() + 120) if fake_now[0] else real_time())
    fake_now[0] = True
    post_with_csrf(client, "/login", {"username": "boss2", "password": "грешна"},
                   csrf_source_url="/login")
    _, after_more = login_guard.is_locked_out("boss2")
    fake_now[0] = False

    assert after_more > right_after_lock - 120, (
        "находка №11: опитите по време на заключване не го удължават "
        "(остават %s сек. вместо да е презаредено)" % after_more)


# -------------------------------------------------------------------- №12
def test_audit_log_cannot_be_forged_with_newlines(capsys):
    """Находка №12 (ниска, но обезсилва единствената следа при инцидент):
    `log_audit` режеше дължината, но не пипаше новите редове, а част от
    `detail` идва от ВЪВЕДЕНОТО (номерът на фактура е свободен текст).
    Обикновен служител можеше да сложи в дневника ред, неразличим по форма
    от истински — с чуждо име, чуждо IP и чуждо действие."""
    import applog

    applog.log_audit(
        "издаден документ",
        "id=1 №2026-01-01\n[2026-01-01 09:00:00] ОДИТ: kolega(id=7) от "
        "192.168.1.9 | изтрит документ | id=42")
    out = capsys.readouterr().out
    audit_lines = [ln for ln in out.splitlines() if "ОДИТ:" in ln]
    assert len(audit_lines) == 1, (
        "находка №12: едно събитие произведе %d реда в дневника — вторият е "
        "подправен" % len(audit_lines))


# -------------------------------------------------------------------- №13
def test_busy_database_keeps_the_entered_document(admin_client, db_module,
                                                  monkeypatch):
    """Находка №13 (висока): при трайно заета база `db.next_number` хвърля
    `RuntimeError` с точното обяснение („базата е заета от друг едновременен
    запис“). То не е `sqlite3.*`, затова не се разпознаваше нито от
    `_document_new`, нито от класификатора на грешки — заявката падаше в
    общия клон („Възникна неочаквана грешка… съобщете на администратор“) и
    цялото въведено ЧМР/фактура изчезваше. Груповото издаване отдавна има
    такъв клон; единичното — не."""
    def boom(*args, **kwargs):
        raise RuntimeError("Не успяхме да генерираме следващия номер — базата "
                           "данни е заета от друг едновременен запис "
                           "(опитайте отново): database is locked")

    monkeypatch.setattr(db_module, "next_number", boom)
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "consignee_name": "Получател Специален", "weight": "1200",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    location = resp.headers.get("Location", "")
    assert "restore=" in location, (
        "находка №13: при заета база въведеното се губи (Location=%r)" % location)
    body = admin_client.get(location).data.decode()
    assert "Получател Специален" in body, (
        "находка №13: възстановената форма е празна")


# -------------------------------------------------------------------- №14
def test_stale_settings_tab_does_not_revert_someone_elses_change(admin_client,
                                                                 db_module):
    """Находка №14 (висока): формата „Фирма изпращач“ изпращаше ВСИЧКИТЕ 21
    ключа наведнъж, а `save_settings` правеше безусловен upsert на всеки —
    тоест всеки запис „замразяваше“ състоянието отпреди зареждането на
    страницата. Админ Б с отворен стар таб връщаше назад IBAN-а, който админ
    А току-що е поправил, и виждаше зелено „запазени“. Оттам всяка издадена
    фактура тръгва към клиента с грешен банков ред."""
    page = admin_client.get("/settings").data.decode()
    originals = dict(re.findall(r'name="orig_(\w+)" value="([^"]*)"', page))
    assert originals, "находка №14: формата не носи оригиналните стойности"
    token = get_csrf_token(admin_client, "/settings")

    con = db_module.get_db()
    db_module.save_settings(con, {"sender_iban": "BG80BNBG96611020345678"})
    con.commit()
    con.close()

    data = {"csrf_token": token}
    for key, value in originals.items():
        data[key] = value
        data["orig_" + key] = value
    data["sender_phone"] = "0888123456"
    admin_client.post("/settings", data=data, follow_redirects=True)

    con = db_module.get_db()
    settings = db_module.get_settings(con)
    con.close()
    assert settings.get("sender_iban") == "BG80BNBG96611020345678", (
        "находка №14: чуждата поправка на IBAN беше презаписана от стар таб")
    assert settings.get("sender_phone") == "0888123456", (
        "собствената промяна трябва да се запише")


# -------------------------------------------------------------------- №15
def test_edit_conflict_keeps_what_was_typed(admin_client, db_module):
    """Находка №15 (ниска): оптимистичното заключване работи правилно, но и
    двата конфликтни изхода правеха само flash + redirect, без да запазят
    въведеното — а съседният `IntegrityError` клон ползва точно този
    механизъм от 31.08. При фактура с 200 реда това е преписване наново."""
    post_with_csrf(admin_client, "/pallet/new",
                   {"client_name": "Клиент",
                    "items_json": json.dumps([{"code": "A"}])},
                   csrf_source_url="/pallet/new", follow_redirects=True)
    version = get_edit_doc_version(admin_client, "/doc/1/edit")
    con = db_module.get_db()
    con.execute("UPDATE documents SET version = version + 1 WHERE id = 1")
    con.commit()
    con.close()

    resp = post_with_csrf(admin_client, "/doc/1/edit", {
        "client_name": "Клиент",
        "notes": "МНОГО ДЪЛГА БЕЛЕЖКА, писана десет минути",
        "edit_doc_version": version,
        "items_json": json.dumps([{"code": "A"}]),
    }, follow_redirects=False)
    location = resp.headers.get("Location", "")
    assert "restore=" in location, (
        "находка №15: конфликтът при редакция изхвърля въведеното")
    body = admin_client.get(location).data.decode()
    assert "МНОГО ДЪЛГА БЕЛЕЖКА" in body


# --------------------------------------------------------------- №16 + №17
def test_all_three_row_loaders_guard_against_a_second_click():
    """Находка №16 (висока): деветият одит добави пазача `inFlight` +
    видимо „заето“ на ДВАТА програмни зареждача на редове; третият —
    Excel импортът във фактурата, стоящ в СЪЩАТА форма един блок по-долу —
    остана непокрит. При забавен сървър два клика зареждаха редовете ДВА
    ПЪТИ, а съобщението казваше „Заредени 1 реда“: търговска фактура за
    клиент и митница с удвоено количество, стойност и тегло.

    Одит (собствена проверка при прилагането на патча): оригиналната
    проверка тук търсеше само подниза „InFlight“ някъде в първите 3000
    знака на функцията — а той стои и в РЕДА, който накрая връща флага на
    `false` (`loadInFlight = false;` в `.then()` след заявката). Тоест
    тестът минаваше дори когато самата РАННА ПРОВЕРКА (`if (loadInFlight)
    return;`) в началото на `load()` е изцяло премахната заедно с
    декларацията ѝ. Сега се търси именно ранната проверка — регекс за
    `if (<флаг>) return;` веднага след `function load`/сигнатурата."""
    src = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    for func in ("function initPullFromPallet",
                 "function bindInvoicePullPallet",
                 "function bindInvoiceExcelImport"):
        start = src.index(func)
        body = src[start:start + 3000]
        assert "btn-busy" in body, "находка №16: %s не показва „заето“" % func
        guard = re.search(r"if\s*\(\s*(\w*InFlight)\s*\)\s*return", body)
        assert guard, (
            "находка №16: %s няма РАННА проверка „if (…InFlight) return;“ в "
            "началото на зареждащата функция — само наличие на думата "
            "„InFlight“ някъде по-долу (напр. в реда, който я връща на "
            "false след заявката) не е достатъчно" % func)
        flag = guard.group(1)
        assert re.search(r"\bvar\s+%s\s*=\s*false" % re.escape(flag), body), (
            "находка №16: %s няма деклариран флаг „var %s = false;“ преди "
            "проверката" % (func, flag))


def test_import_messages_can_actually_be_coloured():
    """Находка №17 (висока): `.msg-err` се слагаше върху елемент с ВГРАДЕН
    `style="color:var(--fg-soft)"`, а вграденият стил бие всеки класов
    селектор — тоест от поправката оцеляваше само `font-weight`. Проверено в
    браузър: грешка и успех излизаха с точно един и същ цвят. При работа със
    скенер операторът не забелязва отказа и опаковъчният лист тръгва към
    митницата без цял палет."""
    for name in ("_invoice_macros.html", "packing_form.html"):
        html = open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
        for cls in ("invoice-pull-msg", "invoice-excel-msg", "pull-pallet-msg"):
            for match in re.finditer(r'<div[^>]*%s[^>]*>' % cls, html):
                assert "color:" not in match.group(0), (
                    "находка №17: %s в %s още носи вграден цвят, който бие "
                    ".msg-err" % (cls, name))
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    assert ".import-msg" in css, "находка №17: неутралният цвят не е изнесен в клас"

    src = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    start = src.index("function initPullFromPallet")
    body = src[start:start + 3000]
    assert "setImportMsg" in body, (
        "находка №17: изтеглянето от палет още пише направо в textContent, "
        "значи грешката изглежда като успех")


# -------------------------------------------------------------------- №18
def test_document_list_title_is_clean(admin_client):
    """Находка №18 (ниска): затварящият таг на `#docs-results` беше попаднал
    в блока за ЗАГЛАВИЕ — оттам „</div>“ излизаше буквално в `<title>`, тоест
    в раздела на браузъра, в отметките и в горния колонтитул при печат."""
    body = admin_client.get("/docs").data.decode()
    title = re.search(r"<title>(.*?)</title>", body, re.S).group(1)
    assert "</div>" not in title, "находка №18: заглавието съдържа %r" % title
    for path in ("/invoices", "/clients", "/materials"):
        other = admin_client.get(path).data.decode()
        other_title = re.search(r"<title>(.*?)</title>", other, re.S).group(1)
        assert "</div>" not in other_title


# -------------------------------------------------------------------- №19
def test_print_toolbar_labels_are_translatable():
    """Находка №19 (ниска): бутоните, които избират КОЛКО екземпляра ЧМР
    излизат от принтера и дали палетната карта се печата като A4 или като
    етикет 100×150мм, не минаваха през `_()` — англо/турскоезичният оператор
    виждаше кирилица точно там, където изборът има последствие за хартията.
    (Самите печатни бланки СЪЗНАТЕЛНО остават двуезични БГ/EN.)"""
    checks = {
        "cmr_print.html": ["1 екземпляр", "4 екземпляра", "5 екземпляра"],
        "pallet_print.html": ["Пълен формат A4", "Печат като етикет (100×150мм)"],
    }
    for name, labels in checks.items():
        html = open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
        for label in labels:
            assert (">%s<" % label) not in html, (
                "находка №19: „%s“ в %s не минава през _()" % (label, name))
            assert ("_('%s')" % label) in html


# -------------------------------------------------------------------- №20
def test_every_form_control_has_a_label():
    """Находка №20 (ниска, достъпност): полето за баркод на опаковъчния лист
    и падащите списъци с клиенти нямаха нито свързан етикет, нито
    `aria-label` — екранен четец обявяваше „текстово поле, празно“ точно за
    полето, в което пише физическият скенер, а кликът върху етикета не
    фокусираше полето (по-голяма мишена, важна при работа с ръкавици)."""
    expectations = {
        "packing_form.html": ['for="pull-pallet-code"', 'for="f-client-select-packing"'],
        "cmr_form.html": ['for="unload-point-select"', 'for="f-client-select-cmr"',
                          'aria-label='],
        "pallet_form.html": ['for="f-client-select-pallet"'],
        "dualuse_form.html": ['for="f-client-select-dualuse"'],
        "export_it_form.html": ['for="f-client-select-export-it"'],
        "pallet_bulk_review.html": ['for="f-client-select-bulk"'],
    }
    for name, needles in expectations.items():
        html = open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
        for needle in needles:
            assert needle in html, "находка №20: %s липсва в %s" % (needle, name)


# -------------------------------------------------------------------- №21
def test_restart_script_decides_by_the_move_result():
    """Находка №21 (висока): скриптът за рестарт решаваше успех/провал по
    това дали новото .exe още стои на диска. „Файлът го няма“ обаче има ДВЕ
    причини: `move` е успял, ИЛИ файлът е изчезнал без `move` изобщо да е
    минал (антивирус карантинира прясно свалено неподписано .exe). Във
    втория случай се пишеше „OK: updated successfully“, ТРИЕШЕ СЕ маркерът
    за провалена инсталация и се стартираше СТАРОТО .exe — оттам старият
    процес пита GitHub 2 секунди по-късно и сваля пак, тоест точно
    безкрайният цикъл, срещу който са писани находки №6 (31.08) и №5
    (02.09)."""
    import textwrap

    import updater

    src = open(updater.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    start = src.index("    marker = _failed_marker_name()")
    end = src.index("    with open(bat_path", start)
    namespace = {"_failed_marker_name": updater._failed_marker_name}
    exec(textwrap.dedent(src[start:end]), namespace)  # nosec B102 -- собствен код
    bat = namespace["bat_content"]

    assert bat.isascii(), "скриптът трябва да остане чист ASCII (кирилски пътища)"
    assert "&& set MOVED=1" in bat, (
        "находка №21: успехът още се решава по наличието на файла, не по "
        "резултата на move")
    assert "if defined MOVED (echo OK" in bat, (
        "находка №21: OK клонът не проверява дали move наистина е минал")
    assert ":missing" in bat and bat.index(":missing") < bat.index(":done"), (
        "находка №21: липсващият файл трябва да има собствен клон, който "
        "ЗАПИСВА маркера за провал")


# -------------------------------------------------------------------- №22
def test_interrupted_backup_cannot_be_mistaken_for_a_real_archive(db_module,
                                                                  tmp_path):
    """Находка №22 (висока): двете защити на находка №8 (трием частичния
    файл при изключение; проверяваме цялостта накрая) живеят В ПРОЦЕСА, а
    програмата има два пътя на РЯЗЪК изход (`os._exit(0)`) плюс изключването
    на Windows. Часовият архив върви в демон-нишка. Проверено с изпълнение
    върху база от 23 MB, прекъсната на 87%: оставаше файл с легитимно име и
    дата, 18,7 MB, който `PRAGMA integrity_check` обявява за „ok“ и в който
    няма НИТО ЕДНА таблица — а понеже е най-новият, ротацията го пази и
    точно него взима човекът, който възстановява."""
    import backup

    dest = str(tmp_path / "arhiv")
    os.makedirs(dest)
    path = backup.local_backup(dest)
    assert os.listdir(dest) == [os.path.basename(path)], (
        "след успешен архив в папката не бива да остава нищо друго")

    # Прекъснатото копие носи разширение, което ротацията НЕ разпознава.
    assert not backup._BACKUP_NAME_RE.match(os.path.basename(path) + ".partial")
    assert backup._BACKUP_NAME_RE.match(os.path.basename(path))

    # Празна, но „валидна“ база НЕ бива да мине за архив.
    empty = os.path.join(dest, "empty.db")
    sqlite3.connect(empty).close()
    con = sqlite3.connect(empty)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", (
            "самата проба разчита на това, че празната база минава "
            "integrity_check — точно затова тя не е достатъчна проверка")
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 0
    finally:
        con.close()

    source = open(backup.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert "sqlite_master" in source, (
        "находка №22: проверката още се доверява само на integrity_check, "
        "който НЕ различава празна база от пълна")
    assert "os.replace(partial_path, dest_path)" in source, (
        "находка №22: архивът още се пише направо в крайното си име")


# -------------------------------------------------------------------- №23
def test_cloudflared_temp_name_separates_machines(monkeypatch, tmp_path):
    """Находка №23 (ниска): поправката от 02.09 е формулирана като проблем
    МЕЖДУ МАШИНИ, но ползва `os.getpid()` — PID-ът е уникален само в рамките
    на един компютър (под Windows са малки числа, кратни на 4). Освен това
    при прекъсната връзка по средата на 39-те мегабайта частичният файл
    оставаше на диска завинаги: следващото пускане има друг PID, значи друго
    име, значи никога не го презаписва и не го трие."""
    import platform

    import remote_tunnel

    real_node = platform.node
    try:
        platform.node = lambda: "STANCIA-A"
        first = remote_tunnel._machine_suffix()
        platform.node = lambda: "STANCIA-B"
        second = remote_tunnel._machine_suffix()
    finally:
        platform.node = real_node
    assert first != second, (
        "находка №23: временното име не различава две машини")

    target = str(tmp_path / "cloudflared")
    leftover = target + ".deadbeef.4288.download"
    open(leftover, "wb").write(b"x" * 1000)
    remote_tunnel._clean_stale_downloads(target)
    assert not os.path.exists(leftover), (
        "находка №23: недосвалените остатъци не се чистят и се трупат в "
        "споделената папка по десетки мегабайта")


# -------------------------------------------------------------------- №24
def test_ci_reads_the_startup_log_from_the_right_folder():
    """Находка №24 (ниска): smoke стъпката пускаше `./dist/PachoLogistic.exe`
    от корена, а програмата пише лога/базата/ключа ДО .EXE-ТО. Глобът
    `pacho_startup*.log` в корена не съвпадаше с нищо и беше заглушен с
    `|| true`, а `exe_output.log` е гарантирано празен (`--windowed`). Тоест
    при провал на билда се отпечатваше НИЩО — точно логът, който обяснява
    защо .exe-то не тръгва."""
    path = os.path.join(ROOT, ".github", "workflows", "release.yml")
    src = open(path, encoding="utf-8").read()
    assert "dist/pacho_startup*.log" in src, (
        "находка №24: CI още чете лога от грешната папка")
    assert "rm -f dist/pacho_logistic.db" in src, (
        "находка №24: почистването също сочи в грешната папка, тоест базата "
        "от smoke теста влиза в публикувания архив")


# -------------------------------------------------------------------- №25
def test_instance_lock_is_machine_wide_not_per_user():
    """Находка №25 (ниска): докстрингът твърдеше, че временната папка е
    „per-machine по дефиниция“ — под Windows `%TEMP%` живее в
    ПОТРЕБИТЕЛСКИЯ профил. Две едновременни сесии на един компютър (бърза
    смяна на потребители, RDP) получаваха два различни катинара и минаваха и
    двете. Последицата не е само „две копия“: `_machine_suffix` хешира името
    на КОМПЮТЪРА, значи двата процеса ползват един и същ `<exe>.new`, един и
    същ bat и един и същ маркер — сценарият на находка №6 от 02.09, който по
    конструкция не може да бъде различен.

    Одит (собствена проверка при прилагането на патча): оригиналният тест
    тук само търсеше подниза „ProgramData“ някъде във файла — а той стои и в
    докстринга на функцията (обяснението защо е избран), тоест тестът
    минаваше дори при ИЗЦЯЛО върнат код (само докстрингът остава). Сега се
    вика самата функция с монkeypatch-нат `os.name`/environ и се проверява
    РЕЗУЛТАТЪТ — процеса, който истински differentiатор между двете
    Windows сесии."""
    import single_instance

    real_name = os.name
    real_environ = dict(os.environ)
    try:
        os.name = "nt"
        os.environ["ProgramData"] = str.__new__(str, "/tmp/fake_programdata_%d" % os.getpid())
        os.environ.pop("ALLUSERSPROFILE", None)
        result = single_instance._default_dir()
    finally:
        os.name = real_name
        os.environ.clear()
        os.environ.update(real_environ)
    assert "fake_programdata" in result, (
        "находка №25: _default_dir() под Windows не използва ProgramData — "
        "все още връща временната ПОТРЕБИТЕЛСКА папка, тоест две Windows "
        "сесии на един компютър се разминават (резултат: %r)" % (result,))
    import shutil
    shutil.rmtree(result, ignore_errors=True)


# ------------------------------------------------------------------ общи
def test_no_finding_reintroduced_in_the_pdf_template():
    """Пазач срещу връщане на находки №1 и №3 при бъдещо разместване на
    PDF шаблона — това е повтарящият се дефектен клас в този проект."""
    html = open(os.path.join(ROOT, "templates", "pdf_export.html"),
                encoding="utf-8").read()
    assert "&nbsp;" in html, "находка №1: празните клетки пак са наистина празни"
    assert "<thead>" in html and 'repeat="1"' in html, (
        "находка №3: заглавният ред пак не се повтаря")
    assert "pdf:pagenumber" in html, (
        "находка №3: страниците пак нямат номер на документа")
