# -*- coding: utf-8 -*-
"""Общи fixtures за тестовете.

Основната цел тук е ИЗОЛАЦИЯ: тестовете никога не пипат реалната база
данни или `pacho_config.json` на разработчика. Всеки тест, който има нужда
от база, получава чисто нова временна SQLite база във временна папка.

Модулът `db` изчислява `DB_PATH` при импорт (от `config.resolve_db_path`),
затова го пренасочваме към временния файл чрез monkeypatch на атрибута на
модула, преди да извикаме `db.init_db()`.
"""
import os
import re
import sys

import pytest

# Коренът на проекта (папката над tests/) трябва да е в пътя, за да се
# импортират db, config, barcode128, updater и т.н.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


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
    import routes_pallet_extra
    import routes_settings

    app = appcore.create_app(run_boot_tasks=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    for mod in (routes_auth, routes_dashboard, routes_documents,
               routes_pallet_extra, routes_clients, routes_settings, routes_admin):
        mod.register(app)

    from datetime import datetime
    from flask import flash, redirect, render_template, session, url_for

    @app.route("/preview/<token>")
    @appcore.login_required
    def preview_document(token):
        payload = appcore._get_preview(token, "doc")
        if payload is None:
            flash("Прегледът е изтекъл или вече е използван — генерирайте го отново от формата.")
            return redirect(url_for("dashboard"))
        doc_type, data = payload
        draft_doc = {
            "id": 0, "doc_type": doc_type,
            "number": "ПРЕДВАРИТЕЛЕН ПРЕГЛЕД / DRAFT", "barcode": "DRAFT-PREVIEW",
            "author": session.get("full_name") or session.get("username"),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        return render_template(appcore.PRINT_TEMPLATES[doc_type], doc=draft_doc, d=data,
                               copies=1, preview=True, label_format=False)

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


def post_with_csrf(test_client, url, data, csrf_source_url="/", **kwargs):
    """POST с автоматично добавен валиден csrf_token (взет от GET на
    csrf_source_url в СЪЩАТА сесия) — удобство за тестовете, за да не
    повтарят ръчно get_csrf_token навсякъде."""
    data = dict(data)
    data.setdefault("csrf_token", get_csrf_token(test_client, csrf_source_url))
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
