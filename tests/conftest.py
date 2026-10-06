# -*- coding: utf-8 -*-
"""Общи fixtures за тестовете.

Основната цел тук е ИЗОЛАЦИЯ: тестовете никога не пипат реалната база
данни или конфигурационния файл (`ph_config.json`/`pacho_config.json`) на
разработчика. Всеки тест, който има нужда
от база, получава чисто нова временна SQLite база във временна папка.

Модулът `db` изчислява `DB_PATH` при импорт (от `config.resolve_db_path`),
затова го пренасочваме към временния файл чрез monkeypatch на атрибута на
модула, преди да извикаме `db.init_db()`.
"""
import os
import re
import sys
import threading

import pytest
import werkzeug.security as _wsecurity

# Коренът на проекта (папката над tests/) трябва да е в пътя, за да се
# импортират db, config, barcode128, updater и т.н.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Одит (01.10.2026, Q1): scrypt (~0.1 s на хеш, ~6 на тест) беше основната цена
# на пакета. Подменяме ПОДРАЗБИРАЩИЯ се метод на самата функция, за да важи и
# за вече внесените `from werkzeug.security import generate_password_hash`.
PRODUCTION_HASH_DEFAULTS = _wsecurity.generate_password_hash.__defaults__
_FAST_HASH_METHOD = "pbkdf2:sha256:1"
_FAST_DUMMY_HASH = _wsecurity.generate_password_hash("dummy", method=_FAST_HASH_METHOD)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_password_hash: без евтиното тестово хеширане на пароли (истински scrypt)")


@pytest.fixture(autouse=True)
def _fast_password_hashing(request, monkeypatch):
    if request.node.get_closest_marker("real_password_hash"):
        return
    monkeypatch.setattr(_wsecurity.generate_password_hash, "__defaults__",
                        (_FAST_HASH_METHOD,) + PRODUCTION_HASH_DEFAULTS[1:])
    routes_auth = sys.modules.get("routes_auth")
    if routes_auth is not None:
        # dummy хешът се смята при импорт (scrypt) и се проверява при всеки вход
        # с непознато име — без подмяна всеки такъв тест плаща scrypt.
        monkeypatch.setattr(routes_auth, "_DUMMY_PASSWORD_HASH", _FAST_DUMMY_HASH)


def read_source(*parts):
    """Текстът на файл от проекта (път спрямо корена)."""
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def app_js_source():
    return read_source("static", "app.js")


@pytest.fixture
def tmp_db_path(tmp_path):
    """Път до нова временна база данни (файлът още не съществува)."""
    return os.path.join(str(tmp_path), "test_pacho.db")


@pytest.fixture
def db_module(tmp_db_path, monkeypatch):
    """Модулът `db`, пренасочен към временна база и инициализиран със схемата.

    Връща самия модул, за да могат тестовете да ползват db.next_number,
    db.get_db, db.save_settings и т.н. срещу изолирана база.
    """
    import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_db_path)
    monkeypatch.setattr(db_mod, "SECRET_PATH", tmp_db_path + ".secret")
    db_mod.init_db()
    return db_mod


@pytest.fixture
def con(db_module):
    """Отворена връзка към временната база; затваря се автоматично след теста."""
    c = db_module.get_db()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path_factory, monkeypatch):
    """Одит (26.09.2026, находка №8): всеки тест вижда ВРЕМЕНЕН
    конфигурационен файл. Досега тест, пращащ формата „Мрежови настройки“ без
    изрично пренасочване (test_network_settings_accept_a_valid_port),
    презаписваше истинския файл в корена на проекта. Отделна папка (не
    `tmp_path`), за да не се появява неочакван файл в папката на теста;
    тестовете, които сами пренасочват CONFIG_PATH, продължават да го правят."""
    import config as appconfig
    cfg_dir = tmp_path_factory.mktemp("cfg")
    # Одит (06.10.2026): новото име — каквото получава всяка нова инсталация
    # (старото се проверява изрично в test_rename_migration.py).
    monkeypatch.setattr(appconfig, "CONFIG_PATH", str(cfg_dir / "ph_config.json"))


@pytest.fixture(autouse=True)
def _reset_login_guard_state():
    """Одит (16.08.2026, находка №6): login_guard пази състоянието си (за
    заключване по потребителско име, ГЛОБАЛНИЯ праг и НОВИЯ per-IP праг —
    виж login_guard.py) в модулни речници/списъци, НЕ в базата данни —
    затова НЕ се изчиства автоматично от db_module fixture-а по-горе.
    Flask test client-ът праща всяка заявка от ЕДИН И СЪЩ (фалшив) IP адрес
    — десетки тестове, всеки логващ се по веднъж БЕЗ пауза между тях,
    лесно надвишават ДОРИ реалистичен per-IP праг само защото се
    изпълняват за части от секундата, докато реалният праг цели минути
    между легитимни опити. Изрична чистка ПРЕДИ всеки тест изолира
    тестовете от натрупаното състояние на предишните — прагът остава
    непроменен (реалистична стойност), само тестовата плътност вече не му
    противоречи."""
    import login_guard
    login_guard.reset_all()
    yield
    login_guard.reset_all()


@pytest.fixture(autouse=True)
def _no_network_update_check(monkeypatch):
    """dashboard.html показва updater.check_cached() резултата — без тази
    fixture всеки тест, зареждащ таблото, би направил истинска мрежова
    заявка към GitHub (бавно и нестабилно в изолирана тестова среда).
    Предварително запълваме кеша, за да не се извиква мрежата изобщо."""
    import updater
    monkeypatch.setitem(updater._cache, "info", {
        "available": False, "current": "0.0.0-test", "latest": "0.0.0-test",
        "download": None, "expected_sha256": None,
    })
    monkeypatch.setitem(updater._cache, "last_error", None)
    import time as _time
    monkeypatch.setitem(updater._cache, "time", _time.time())


@pytest.fixture
def flask_app(db_module, monkeypatch):
    """Пълното Flask приложение (appcore.create_app + всички routes_*
    модули — точно както ги регистрира app.py), сглобено СРЕЩУ временната
    база на db_module. run_boot_tasks=False пропуска GitHub bootstrap
    опита (без мрежа по време на тест). Тази fixture е ИМЕННО причината,
    поради която appcore.py въведе фабричния create_app() модел във Фаза 3
    — преди това app.py създаваше Flask app-а И викаше db.init_db() на
    ниво модул при самия импорт, което правеше пълно end-to-end тестване
    през Flask test client невъзможно без да се пипне реалната инсталация."""
    import appcore
    import routes_admin
    import routes_auth
    import routes_clients
    import routes_dashboard
    import routes_documents
    import routes_invoices
    import routes_materials
    import routes_pallet_extra
    import routes_settings

    app = appcore.create_app(run_boot_tasks=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    for mod in (routes_auth, routes_dashboard, routes_documents,
               routes_pallet_extra, routes_invoices, routes_materials,
               routes_clients, routes_settings, routes_admin):
        mod.register(app)

    from flask import flash, redirect, render_template, session, url_for

    @app.route("/preview/<token>")
    @appcore.login_required
    def preview_document(token):
        payload = appcore._get_preview(token, "doc")
        if payload is None:
            flash("Прегледът е изтекъл или вече е използван — генерирайте го отново от формата.")
            return redirect(url_for("dashboard"))
        doc_type, data, edit_doc_id = payload[0], payload[1], payload[2]
        draft_doc = appcore.build_draft_doc(
            doc_type, data, session.get("full_name") or session.get("username"))
        return render_template(appcore.PRINT_TEMPLATES[doc_type], doc=draft_doc, d=data,
                               copies=1, preview=True, label_format=False, token=token,
                               edit_doc_id=edit_doc_id)

    return app


def get_csrf_token(test_client, url="/login"):
    """Извлича истинския CSRF токен от скрито поле в реално рендерирана
    страница (GET към url) — appcore._check_csrf изисква токен, съвпадащ
    с този в сесията (виж appcore.py), затова тестовете не могат просто да
    ползват произволен низ: трябва първо GET, който да генерира и вгради
    токена (csrf_token() шаблонна функция), точно както прави истински
    браузър, зареждащ формата, преди да я изпрати."""
    resp = test_client.get(url)
    m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', resp.data)
    assert m, "csrf_token скрито поле не е намерено в отговора на %s" % url
    return m.group(1).decode()


def get_edit_doc_version(test_client, edit_url):
    """Стойността на скритото поле `edit_doc_version` от реално рендирана
    форма за редакция — точно както прави браузърът.

    Одит (19.08.2026, находка №10): оптимистичното заключване вече е
    fail-closed (липсващо поле = конфликт, вместо мълчаливо пропускане на
    проверката), затова тестовете, които редактират документ, трябва да
    подават полето като истински клиент."""
    resp = test_client.get(edit_url)
    m = re.search(rb'name="edit_doc_version"\s+value="([^"]*)"', resp.data)
    return m.group(1).decode() if m else ""


def post_with_csrf(test_client, url, data, csrf_source_url="/", **kwargs):
    """POST с автоматично добавен валиден csrf_token (взет от GET на
    csrf_source_url в СЪЩАТА сесия) — удобство за тестовете, за да не
    повтарят ръчно get_csrf_token навсякъде.

    При POST към /doc/<id>/edit автоматично добавя и `edit_doc_version`
    (пак от реално рендираната форма) — виж get_edit_doc_version по-горе."""
    data = dict(data)
    data.setdefault("csrf_token", get_csrf_token(test_client, csrf_source_url))
    if url.endswith("/edit") and "edit_doc_version" not in data:
        data["edit_doc_version"] = get_edit_doc_version(test_client, url)
    return test_client.post(url, data=data, **kwargs)


@pytest.fixture
def client(flask_app):
    """Flask test client срещу flask_app, БЕЗ логнат потребител."""
    return flask_app.test_client()


@pytest.fixture
def admin_client(flask_app, db_module):
    """Flask test client, логнат като администратор с известна парола (не
    подразбиращата се admin123/admin — тук е чист тестов акаунт), за да
    не се задейства _enforce_password_change по средата на теста."""
    from werkzeug.security import generate_password_hash

    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
        ("test_admin", generate_password_hash("test-password-123"), "Тест Админ"),
    )
    con.commit()
    con.close()

    c = flask_app.test_client()
    token = get_csrf_token(c, "/login")
    resp = c.post("/login", data={"username": "test_admin",
                                  "password": "test-password-123",
                                  "csrf_token": token})
    assert resp.status_code == 302
    return c


@pytest.fixture
def employee_client(flask_app, db_module):
    """Flask test client, логнат като обикновен служител (не admin)."""
    from werkzeug.security import generate_password_hash

    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'employee', 1, 0)",
        ("test_emp", generate_password_hash("test-password-123"), "Тест Служител"),
    )
    con.commit()
    con.close()

    c = flask_app.test_client()
    token = get_csrf_token(c, "/login")
    resp = c.post("/login", data={"username": "test_emp",
                                  "password": "test-password-123",
                                  "csrf_token": token})
    assert resp.status_code == 302
    return c


#: Одит (04.10.2026, R5/Б4): задължителните полета на формите (атрибутът
#: `required`), които сървърът вече проверява и сам — routes_documents.
#: _REQUIRED_FIELDS. Стойности по подразбиране за тестовете, на които
#: конкретното поле не е предмет на проверката.
REQUIRED_FIELDS_BY_URL = {
    "/cmr/new": {"consignee_name": "Получател ЕООД"},
    "/packing/new": {"receiver_name": "Получател ЕООД"},
    "/pallet/new": {"client_name": "Клиент ЕООД"},
    "/waybill/new": {"consignee_name": "Получател ЕООД"},
    "/dualuse/new": {"invoice_numbers": "0000001234", "destination_country": "Турция",
                     "declarant_name": "Иван Петров"},
    "/export-it/new": {"invoice_no": "0000001234"},
}


def with_required(url, fields=None):
    """Данните на формата, допълнени с непопълнените задължителни полета
    (виж REQUIRED_FIELDS_BY_URL); изрично подадените стойности не се пипат."""
    data = dict(fields or {})
    for key, value in REQUIRED_FIELDS_BY_URL.get(url, {}).items():
        if not str(data.get(key) or "").strip():
            data[key] = value
    return data


def issue_cmr(test_client, consignee_name="Клиент ЕООД", sender_name="Изпращач"):
    """Издава ЧМР през истинската форма и връща id-то на новия документ."""
    resp = post_with_csrf(test_client, "/cmr/new", {
        "sender_name": sender_name, "consignee_name": consignee_name,
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


# ---------------------------------------------------------------- e2e (Playwright)
# Одит (01.10.2026, Q2): един Chromium за цялата сесия, нов context + страница
# за всеки тест. Пускането на браузър беше ~1/3 от времето на e2e пакета.
# ВНИМАНИЕ: докато сесийният браузър е жив, второ `sync_playwright()` в същия
# процес гърми („Sync API inside the asyncio loop“) — ползвайте тези fixtures.
E2E_USERNAME = "e2e_admin"
E2E_PASSWORD = "e2e-test-password-123"


@pytest.fixture
def live_server(flask_app, db_module):
    """Истински HTTP сървър (werkzeug, фонова нишка, случаен порт) срещу
    flask_app и временната база, с администратор E2E_USERNAME. CSRF не се
    изключва — браузърът праща истинския токен от формата."""
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'admin', 1, 0)",
        (E2E_USERNAME, generate_password_hash(E2E_PASSWORD), "E2E Тест"),
    )
    con.commit()
    con.close()

    server = make_server("127.0.0.1", 0, flask_app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def e2e_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    browser = pw.chromium.launch()
    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


@pytest.fixture
def e2e_context_factory(e2e_browser):
    """Създава нови (изолирани) browser context-и; затваря всички след теста,
    вкл. създадените от самия тест през page.context.browser.new_context()."""
    before = set(e2e_browser.contexts)

    def make(**kwargs):
        return e2e_browser.new_context(**kwargs)

    yield make
    for ctx in e2e_browser.contexts:
        if ctx not in before:
            ctx.close()


@pytest.fixture
def page(live_server, e2e_context_factory):
    return e2e_context_factory().new_page()


def e2e_login(pg, base_url, username=E2E_USERNAME, password=E2E_PASSWORD):
    pg.goto(base_url + "/login")
    pg.fill('input[name="username"]', username)
    pg.fill('input[name="password"]', password)
    pg.click('main button[type="submit"]')
    pg.wait_for_url(base_url + "/")
