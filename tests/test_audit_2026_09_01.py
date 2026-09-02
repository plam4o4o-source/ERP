# -*- coding: utf-8 -*-
"""Регресионни тестове за деветия дълбочинен одит (01.09.2026), v3.69.2.

Всяка функция тук е ЗАКЛЮЧВАЩА за конкретна находка: проверена е и в двете
посоки с реално изпълнение (връщане на стария код → тестът пада; върната
поправка → минава), не само по прочит.

Находките №12–№16 са изцяло фронтенд (bfcache, програмно попълнени полета,
надпревара между fetch заявки) — там тестовете пазят конкретния МЕХАНИЗЪМ в
static/app.js, по модела на tests/test_audit_2026_08_25_ui.py, защото Python
HTTP клиент не може да ги възпроизведе.
"""
import io
import json
import os
import re

import pytest

from conftest import post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------- №1
def test_resetting_password_of_a_deleted_user_is_404_not_a_green_lie(
        admin_client, db_module):
    """Находка №1: `admin_user_password` стреляше сляп UPDATE. Админ Б със
    стар отворен таб нулира паролата на служител, когото админ А е изтрил →
    0 засегнати реда → зелено „Паролата е сменена“ + одитен запис за
    НЕСЪЩЕСТВУВАЩ потребител. Съседните два маршрута върху същия обект
    (toggle/delete) отдавна правят проверката и дават 404."""
    con = db_module.get_db()
    uid = con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES ('uvolnen', 'x', 'У', 'employee', 1, 0)"
    ).lastrowid
    con.execute("DELETE FROM users WHERE id = ?", (uid,))
    con.commit()
    con.close()

    resp = post_with_csrf(admin_client, "/admin/users/%d/password" % uid,
                          {"password": "NovaParola123"},
                          csrf_source_url="/admin/users", follow_redirects=False)
    assert resp.status_code == 404, (
        "находка №1: нулиране на паролата на изтрит служител мина за успех "
        "(status=%s)" % resp.status_code)


# --------------------------------------------------------------------- №2
def test_failed_bulk_issue_keeps_the_whole_batch(admin_client, db_module,
                                                 monkeypatch):
    """Находка №2: при провал по средата на групово издаване операторът
    отиваше на ПРАЗНАТА /pallet/new — цялата партида (N карти, до 5000 реда
    от Excel импорт плюс ръчните корекции) изчезваше. Механизмът за
    възстановяване (_store_preview("bulk_pallet") + pallet_bulk_review_restore)
    вече съществува в същия файл и се ползва от съседния маршрут."""
    import routes_pallet_extra

    def boom(*a, **k):
        raise RuntimeError("симулирана грешка по средата на партидата")

    monkeypatch.setattr(routes_pallet_extra, "save_document", boom)

    items = [{"order_no": "1", "pos": "10", "reference": "R",
              "reference_desc": "стока", "qty": "5"}]
    resp = post_with_csrf(admin_client, "/pallet/bulk-issue", {
        "client_name": "Клиент за партидата",
        "groups": "g1",
        "items_format_g1": "orders",
        "items_json_g1": json.dumps(items, ensure_ascii=False),
        "gross_g1": "100",
    }, csrf_source_url="/pallet/new", follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers.get("Location", "")
    assert "/pallet/bulk-review/restore/" in location, (
        "находка №2: партидата не се възстановява — операторът отива на "
        "празна форма и губи всичко въведено (Location=%r)" % location)
    body = admin_client.get(location).get_data(as_text=True)
    assert "Клиент за партидата" in body, (
        "находка №2: възстановеният преглед не съдържа въведеното")


# --------------------------------------------------------------------- №3
def test_invoice_client_form_keeps_input_when_name_is_missing(admin_client):
    """Находка №3: шаблонът рендираше само от `entry` — при празно име
    всичките осем полета (двата многоредови адреса, телефоните, бележката)
    се връщаха празни."""
    resp = post_with_csrf(admin_client, "/invoices/clients/new", {
        "name": "   ",                       # само интервали — HTML required не го лови
        "delivery_name": "ABB Sorocaba",
        "delivery_address": "Av. Industrial 1234\nSorocaba, SP",
        "delivery_phone": "+55 15 1234-5678",
        "billing_name": "ABB Brasil Ltda",
        "billing_address": "Rua Central 9\nSão Paulo",
        "billing_phone": "+55 11 9999-0000",
        "notes": "плаща на 60 дни",
    }, csrf_source_url="/invoices/clients/new", follow_redirects=True)

    body = resp.get_data(as_text=True)
    for kept in ("ABB Sorocaba", "Av. Industrial 1234", "+55 15 1234-5678",
                 "ABB Brasil Ltda", "Rua Central 9", "плаща на 60 дни"):
        assert kept in body, (
            "находка №3: въведеното „%s“ изчезна от върнатата форма" % kept)


def test_invoice_client_edit_keeps_the_new_input_not_the_stored_one(
        admin_client, db_module):
    """По-коварната половина на находка №3: при РЕДАКЦИЯ шаблонът връщаше
    СТАРИТЕ стойности от базата, значи редакциите на оператора тихо
    изчезваха и изглеждаха „неприети“, без нищо да го каже."""
    post_with_csrf(admin_client, "/invoices/clients/new",
                   {"name": "Оригинал", "delivery_name": "Старо име"},
                   csrf_source_url="/invoices/clients/new", follow_redirects=True)
    con = db_module.get_db()
    entry_id = con.execute("SELECT id FROM invoice_clients").fetchone()["id"]
    con.close()

    resp = post_with_csrf(admin_client, "/invoices/clients/%d/edit" % entry_id, {
        "name": "",                          # предизвиква отказа
        "delivery_name": "НОВО ИМЕ ОТ ОПЕРАТОРА",
    }, csrf_source_url="/invoices/clients/%d/edit" % entry_id, follow_redirects=True)

    body = resp.get_data(as_text=True)
    assert "НОВО ИМЕ ОТ ОПЕРАТОРА" in body, (
        "находка №3: редакцията се изгуби — формата се върна със старата стойност")


# --------------------------------------------------------------------- №4
def test_client_form_keeps_input_and_unload_points_when_name_is_missing(
        admin_client):
    """Находка №4: разтоварните пунктове се възстановяваха ОТ БАЗАТА, не от
    подаденото `unload_points_json` — при пропуснато име на фирмата
    операторът губеше 11 полета И всички добавени пунктове (по 5 полета)."""
    points = [{"label": "Склад 1", "address": "ул. Първа 1",
               "postcode": "1000", "city": "София", "country": "България"},
              {"label": "Склад 2", "address": "ул. Втора 2",
               "postcode": "4000", "city": "Пловдив", "country": "България"}]
    resp = post_with_csrf(admin_client, "/clients/new", {
        "name": "",                          # предизвиква отказа
        "address": "бул. Тестов 15",
        "city": "Габрово",
        "eik": "123456789",
        "phone": "062 123456",
        "contact": "Иван Иванов",
        "unload_points_json": json.dumps(points, ensure_ascii=False),
    }, csrf_source_url="/clients/new", follow_redirects=True)

    body = resp.get_data(as_text=True)
    for kept in ("бул. Тестов 15", "Габрово", "123456789", "Иван Иванов"):
        assert kept in body, "находка №4: полето „%s“ изчезна" % kept
    assert "Склад 1" in body and "Склад 2" in body, (
        "находка №4: разтоварните пунктове изчезнаха — най-скъпият за "
        "пресъздаване вход в тази форма")


# --------------------------------------------------------------------- №5
def test_entry_id_zero_is_404_not_a_silent_new_record(admin_client, db_module):
    """Находка №5: `if entry_id` третираше 0 като „нов запис“ — GET даваше
    200 с празна форма вместо 404, а POST тихо СЪЗДАВАШЕ нов запис в
    адресната книга (invoice_clients_module.save има същото truthiness)."""
    assert admin_client.get("/invoices/clients/0/edit").status_code == 404, (
        "находка №5: /invoices/clients/0/edit дава форма „нов запис“ вместо 404")

    con = db_module.get_db()
    before = con.execute("SELECT COUNT(*) AS c FROM invoice_clients").fetchone()["c"]
    con.close()
    post_with_csrf(admin_client, "/invoices/clients/0/edit",
                   {"name": "Промъкнал се запис"},
                   csrf_source_url="/invoices/clients/new", follow_redirects=True)
    con = db_module.get_db()
    after = con.execute("SELECT COUNT(*) AS c FROM invoice_clients").fetchone()["c"]
    con.close()
    assert after == before, (
        "находка №5: POST към несъществуващ id=0 тихо създаде НОВ запис")


# --------------------------------------------------------------------- №6
@pytest.mark.parametrize("url, source", [
    ("/invoice/pull-pallet", "/invoice-br/new"),
    ("/packing/pull-pallet", "/packing/new"),
])
def test_pull_pallet_survives_a_broken_row_already_stored(
        admin_client, db_module, url, source):
    """Находка №6: непокритата третина на находка №3 (29.08). Тя добави
    филтъра при ВХОДА и втора защита в ИЗНОСА — изрично „за вече записани
    документи с развален ред“. Двата pull-pallet маршрута четат точно такива
    данни и викаха it.get() без проверка: AttributeError → 500, а картата
    оставаше непрехвърляема завинаги, без втора врата като при износа."""
    items = [{"code": "A1", "description": "стока", "qty": "2"}]
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Клиент",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    con = db_module.get_db()
    row = con.execute("SELECT number, data FROM documents WHERE id = ?",
                      (doc_id,)).fetchone()
    number = row["number"]
    data = json.loads(row["data"])
    data["items"] = [{"code": "A1", "description": "стока", "qty": "2"}, "развален-ред"]
    con.execute("UPDATE documents SET data = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), doc_id))
    con.commit()
    con.close()

    r = post_with_csrf(admin_client, url, {"code": number},
                       csrf_source_url=source, follow_redirects=False)
    assert r.status_code == 200, (
        "находка №6: %s пада с 500 при развален ред — картата остава "
        "непрехвърляема" % url)
    assert r.get_json().get("ok") is True


# --------------------------------------------------------------------- №7
def test_dualuse_export_includes_the_place_country(admin_client):
    """Находка №7: `place_country` излизаше САМО на бланката („Габрово,
    България“), а Excel и PDF показваха само „Габрово“ — стойността се
    записва от формата, но никога не стигаше до износа."""
    import routes_documents
    keys = [key for _label, key in routes_documents._XLSX_FIELDS["dualuse"]]
    assert "place_country" in keys, (
        "находка №7: place_country липсва от износа на декларацията "
        "за двойна употреба")


# --------------------------------------------------------------------- №8
def test_bulk_result_screen_normalizes_numbers_like_the_card():
    """Находка №8: третият екран по маршрута за групово издаване. Находки
    №17/№18 (31.08) нормализираха груповия ПЕЧАТ и груповия ПРЕГЛЕД, но
    списъкът „Издадени палетни карти“ остана със суровите стойности."""
    html = _read("templates", "pallet_bulk_result.html")
    assert "fmt_num(d.height)" in html, (
        "находка №8: височината се показва сурова в списъка с издадените карти")
    assert "fmt_num(d.gross)" in html, (
        "находка №8: брутото се показва сурово в списъка с издадените карти")


# --------------------------------------------------------------------- №9
def test_unit_price_keeps_its_precision_in_excel(admin_client):
    """Находка №9: маската „0.00“ за ВСИЧКИ парични колони (находка №10 от
    31.08) отряза единичната цена 0.0125 до „0.01“, докато бланката и PDF-ът
    показват 0.0125 — получателят пресмята 1000 × 0.01 = 10.00, а редът
    TOTAL твърди 12.50. Точно противоречието, срещу което е писана самата
    находка №10, само в обратната посока."""
    import openpyxl

    items = [{"description": "Стока", "qty": "1000", "unit_price": "0.0125",
              "net_weight": "1"}]
    r = post_with_csrf(admin_client, "/invoice-br/new",
                       {"consignee_name": "Клиент",
                        "items_json": json.dumps(items, ensure_ascii=False)},
                       csrf_source_url="/invoice-br/new", follow_redirects=False)
    doc_id = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    xr = admin_client.get("/doc/%d/export.xlsx" % doc_id)
    assert xr.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(xr.data)).active

    price_cell = None
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, float) and abs(cell.value - 0.0125) < 1e-12:
                price_cell = cell
    assert price_cell is not None, "клетката с единичната цена не бе намерена"
    assert price_cell.number_format != "0.00", (
        "находка №9: маската „0.00“ показва 0.0125 като „0.01“ — Excel и "
        "бланката се разминават")
    assert price_cell.number_format.startswith("0.00"), (
        "паричната колона трябва да пази поне два знака (счетоводният вид)")


# -------------------------------------------------------------------- №10
def test_fallback_db_repair_form_requires_a_token(monkeypatch, tmp_path):
    """Находка №10 (СИГУРНОСТ): резервното приложение е самостоятелен Flask
    обект — appcore._register_hooks (CSRF + защитните хедъри) не се закача за
    него. Единствената защита беше remote_addr==127.0.0.1, което при CSRF е
    изпълнено по дефиниция. Външна страница можеше да пренасочи пътя до
    базата → следващият старт прави ПРАЗНА база с admin/admin123."""
    # Резервният режим се вдига само когато базата е недостъпна (и излиза от
    # процеса при отказ) — вместо да го симулираме, пазим самия механизъм.
    source = _read("app.py")
    assert "hmac.compare_digest" in source, (
        "находка №10: POST-ът на формата за поправка не сверява токен")
    assert 'csrf_token=_fix_csrf_token' in source, (
        "находка №10: токенът не се подава на шаблона")
    assert "_fallback_security_headers" in source, (
        "находка №10: резервният режим няма защитни хедъри (clickjacking)")
    template = _read("templates", "db_path_repair.html")
    assert 'name="csrf_token"' in template, (
        "находка №10: формата не изпраща токен")


# -------------------------------------------------------------------- №11
def test_init_db_closes_the_connection_even_when_it_fails(monkeypatch, tmp_path):
    """Находка №11: `init_db` нямаше try/finally. `con.commit()` може да
    гръмне със SQLITE_BUSY (друг компютър държи катинар — точно сценарият, за
    който резервният режим съществува); тогава транзакцията оставаше активна
    с ВЗЕТ писателски катинар, а изключението се пази в closure-а на
    `_db_unavailable` до края на процеса — значи и `con` остава жив, и
    катинарът не се пуска: собственият процес блокира и себе си, и всеки друг
    компютър, до ръчно спиране."""
    import db as db_module_local

    closed = []

    class _Con:
        def __init__(self):
            self.row_factory = None

        def executescript(self, *a, **k):
            raise RuntimeError("симулирана грешка по средата на init_db")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(db_module_local, "get_db", lambda *a, **k: _Con())
    with pytest.raises(RuntimeError):
        db_module_local.init_db()
    assert closed, (
        "находка №11: връзката не е затворена по грешката — писателският "
        "катинар остава държан до края на процеса")


# -------------------------------------------------------------------- №12
def test_lock_lives_per_machine_not_in_the_shared_folder(monkeypatch, tmp_path):
    """Находка №12 (ВИСОКА): катинарът стоеше ДО .exe-то. При документираната
    мрежова инсталация („сложи .exe в споделената папка и всички го пускат
    оттам“) това е ЕДИН общ файл на дяла, а байтовите катинари на Windows са
    мандаторни и се налагат през SMB. Компютър B получаваше отказ, app.py го
    тълкуваше като „вече работи на ТОЗИ компютър“ и отваряше порта на A —
    тоест в офиса можеше да работи само една машина наведнъж."""
    import tempfile as _tempfile

    import config as appconfig
    import single_instance

    shared = str(tmp_path / "mrezhov_dyal")
    os.makedirs(shared)
    monkeypatch.setattr(appconfig, "CONFIG_PATH",
                        os.path.join(shared, "pacho_config.json"))

    where = single_instance._default_dir()
    assert os.path.abspath(where) != os.path.abspath(shared), (
        "находка №12: катинарът пак живее в папката на инсталацията — два "
        "компютъра, пускащи същото .exe от общ дял, се блокират взаимно")
    assert os.path.abspath(where) == os.path.abspath(_tempfile.gettempdir())


def test_two_installs_on_one_machine_do_not_share_a_lock(monkeypatch, tmp_path):
    """Обратната половина: локално инсталирано копие и второ, пуснато от
    мрежовия дял, сочат към РАЗЛИЧНИ бази — те са две различни програми, не
    двойно щракване, и не бива да се блокират взаимно."""
    import config as appconfig
    import single_instance

    monkeypatch.setattr(appconfig, "CONFIG_PATH",
                        os.path.join(str(tmp_path), "a", "pacho_config.json"))
    name_a = single_instance._default_filename()
    monkeypatch.setattr(appconfig, "CONFIG_PATH",
                        os.path.join(str(tmp_path), "b", "pacho_config.json"))
    name_b = single_instance._default_filename()
    assert name_a != name_b, (
        "находка №12: две различни инсталации на един компютър делят един "
        "катинар — втората би била отказана и би отворила чуждия порт")


# -------------------------------------------------------------------- №13
def test_busy_form_flag_is_cleared_when_the_page_returns_from_bfcache():
    """Находка №13 (ВИСОКА): `dataset.submitting` се нулираше единствено от
    презареждане. Но находка №41 НАРОЧНО премахна `beforeunload`, за да остане
    страницата годна за bfcache, а хедърите не слагат `Cache-Control:
    no-store` — значи страницата се възстановява с `submitting="1"` и формата
    става НАВЕЧНО неизпращаема: „Издай“ е мъртъв, „Предварителен преглед“ е
    некликаем с вечно въртящ се индикатор, без нито едно съобщение."""
    js = _read("static", "app.js")
    assert 'addEventListener("pageshow"' in js, (
        "находка №13: няма нулиране при връщане от bfcache")
    block = js[js.find('addEventListener("pageshow"'):]
    block = block[:block.find("\n}")]
    assert "persisted" in block, "трябва да реагира само на възстановена страница"
    assert 'submitting = ""' in block or "submitting = ''" in block
    assert "btn-busy" in block, "въртящият се индикатор също трябва да се махне"


# -------------------------------------------------------------------- №14
def test_import_buttons_guard_against_double_trigger():
    """Находка №14: скенерът вкарва баркода и сам изпраща Enter; при бавна
    мрежа операторът натиска втори път → addRow се изпълнява ДВА пъти. За
    фактурата това вкарва ЦЯЛАТА палетна карта повторно (удвоена стойност и
    тегло на търговски документ), а съобщението за успех се презаписва, значи
    няма и следа."""
    js = _read("static", "app.js")
    assert js.count("pullInFlight") >= 6, (
        "находка №14: липсва пазач срещу повторно задействане на импортите")
    for fn in ("function initPullFromPallet", "function bindInvoicePullPallet"):
        idx = js.find(fn)
        assert idx != -1, "%s изчезна" % fn
        body = js[idx:idx + 4000]
        assert "if (pullInFlight) return;" in body, (
            "%s няма проверка за вече летяща заявка" % fn)


# -------------------------------------------------------------------- №15
def test_material_lookup_ignores_a_response_for_an_edited_code():
    """Находка №15: единственият fetch без защита срещу разминат ред на
    отговорите. Операторът пише „MAT-1180“, tab, връща се, поправя на
    „MAT-1108“, tab. Отговорът за първия код се връща последен и попълва
    теглото на ДРУГ материал, при това с зелено премигване „намерено“."""
    js = _read("static", "app.js")
    assert "function currentCodeOf" in js, (
        "находка №15: няма проверка, че кодът в реда още е същият")
    idx = js.find("function bindInvoiceMaterialLookup")
    body = js[idx:js.find("function bindInvoiceTotals", idx)]
    assert body.count("currentCodeOf(tr) !== key") >= 2, (
        "проверката трябва да пази И успешния, И грешния път")


# -------------------------------------------------------------------- №16
def test_autofilled_weight_updates_the_live_totals():
    """Находка №16: справочникът попълва теглото ПРОГРАМНО (`target.value =
    ...`), без `input`/`change` — а bindInvoiceTotals слуша точно тези две
    събития. „Общо нето тегло“ оставаше на старата стойност, докато полето
    видимо мига в зелено с реално тегло."""
    js = _read("static", "app.js")
    assert "function bindInvoiceMaterialLookup(table, onChanged)" in js, (
        "находка №16: справочникът не приема известяване")
    idx = js.find("markAutofilled(target);")
    assert idx != -1
    assert "onChanged()" in js[idx:idx + 800], (
        "находка №16: попълненото тегло не влиза в живите суми")
    # И редът на връзване: bindInvoiceTotals ПЪРВО, за да съществува update.
    init = js[js.find("function initInvoiceForm"):]
    init = init[:init.find("\n}")]
    assert init.find("bindInvoiceTotals") < init.find("bindInvoiceMaterialLookup"), (
        "bindInvoiceTotals трябва да се извика преди справочника, за да има "
        "какво да му се подаде")
