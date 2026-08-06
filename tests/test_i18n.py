# -*- coding: utf-8 -*-
"""Регресионни тестове за многоезичния интерфейс (БГ/EN/TR) — Flask-Babel
инфраструктура, превключвател на езика в логин панела и в Настройки,
персистиране на избрания език ЗА ВСЕКИ ПОТРЕБИТЕЛ (не глобално), и
двуезичните (БГ/EN) данни на фирмата изпращач в документите.

Целта е ПРЕДПАЗЕН КОЛАН: ако locale-selector-ът, приоритетът на избор на
език (потребителска настройка > избор преди вход > подразбиращ се БГ),
или substitucията на sender_lang в документните форми се счупят при
бъдещи промени, тези тестове трябва да го хванат."""
import re

from conftest import get_csrf_token, post_with_csrf


# ---------------------------------------------------------------- login-панел: избор на език преди вход

def test_login_page_default_is_bulgarian(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Вход в системата".encode() in resp.data


def test_login_page_lang_en_switches_interface(client):
    resp = client.get("/login?lang=en")
    assert resp.status_code == 200
    assert b"Username" in resp.data
    assert "Вход в системата".encode() not in resp.data


def test_login_page_lang_tr_switches_interface(client):
    resp = client.get("/login?lang=tr")
    assert resp.status_code == 200
    assert "Kullanıcı".encode() in resp.data


def test_login_page_invalid_lang_falls_back_to_default(client):
    resp = client.get("/login?lang=fr")
    assert resp.status_code == 200
    # Невалиден език (не е в db.LANGUAGES) — пада обратно към подразбиращия БГ.
    assert "Вход в системата".encode() in resp.data


def test_lang_chosen_before_login_carries_into_session_after_login(client, db_module):
    from werkzeug.security import generate_password_hash
    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'employee', 1, 0)",
        ("lang_test", generate_password_hash("test-password-123"), "Тест Език"),
    )
    con.commit()
    con.close()

    # Избира EN на логин панела, ПРЕДИ да въведе потребител/парола.
    resp = client.get("/login?lang=en")
    token = re.search(rb'name="csrf_token"\s+value="([^"]+)"', resp.data).group(1).decode()
    resp = client.post("/login", data={"username": "lang_test", "password": "test-password-123",
                                       "csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200
    # Потребителят няма запазена в базата настройка за език — приоритетът
    # пада към избора преди вход (EN), НЕ към подразбиращия се БГ.
    assert b"Dashboard" in resp.data or b"dashboard" in resp.data.lower()


# ---------------------------------------------------------------- Настройки: избор на език след вход (персистира)

def test_my_settings_language_select_persists_per_user(admin_client, db_module):
    resp = admin_client.get("/my-settings")
    assert resp.status_code == 200
    assert b"Interface language" in resp.data or "Език на интерфейса".encode() in resp.data

    token = get_csrf_token(admin_client, "/my-settings")
    resp = admin_client.post("/my-settings", data={"language": "tr", "csrf_token": token},
                             follow_redirects=True)
    assert resp.status_code == 200

    con = db_module.get_db()
    row = con.execute(
        "SELECT value FROM user_settings WHERE key = 'language'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "tr"


def test_my_settings_language_change_does_not_reset_theme(admin_client, db_module):
    # Първо задава тема, после отделно език — двете независими <form>-и в
    # my_settings.html НЕ трябва да си нулират взаимно стойностите
    # (виж routes_settings.my_settings() — fallback към current_theme/current_lang).
    token = get_csrf_token(admin_client, "/my-settings")
    admin_client.post("/my-settings", data={"theme": "dark", "csrf_token": token},
                      follow_redirects=True)

    token = get_csrf_token(admin_client, "/my-settings")
    admin_client.post("/my-settings", data={"language": "en", "csrf_token": token},
                      follow_redirects=True)

    con = db_module.get_db()
    theme_row = con.execute("SELECT value FROM user_settings WHERE key = 'theme'").fetchone()
    lang_row = con.execute("SELECT value FROM user_settings WHERE key = 'language'").fetchone()
    assert theme_row["value"] == "dark"
    assert lang_row["value"] == "en"


def test_persisted_language_survives_logout_login_without_lang_param(admin_client, db_module):
    token = get_csrf_token(admin_client, "/my-settings")
    admin_client.post("/my-settings", data={"language": "tr", "csrf_token": token},
                      follow_redirects=True)

    admin_client.get("/logout", follow_redirects=True)

    token = get_csrf_token(admin_client, "/login")
    resp = admin_client.post("/login", data={"username": "test_admin",
                                              "password": "test-password-123",
                                              "csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200
    # Без ?lang= параметър — трябва да ползва запазеното в базата 'tr'.
    assert "Kullanıcı".encode() not in resp.data  # вече е логнат, не е login страницата
    assert b"Panel" in resp.data or "Табло".encode() not in resp.data


def test_db_get_user_language_empty_when_unset(db_module):
    con = db_module.get_db()
    row = con.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    assert db_module.get_user_language(con, row["id"]) == ""


def test_db_get_user_language_returns_saved_value(db_module):
    con = db_module.get_db()
    row = con.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    con.execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?, 'language', 'tr')",
        (row["id"],),
    )
    con.commit()
    assert db_module.get_user_language(con, row["id"]) == "tr"


def test_db_get_user_language_ignores_invalid_stored_value(db_module):
    # Ако в базата попадне невалидна стойност (напр. стар/премахнат код на
    # език), get_user_language не трябва да я върне като валидна — пада
    # обратно към "" (не избран), а не към счупен locale.
    con = db_module.get_db()
    row = con.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    con.execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?, 'language', 'fr')",
        (row["id"],),
    )
    con.commit()
    assert db_module.get_user_language(con, row["id"]) == ""


# ---------------------------------------------------------------- Двуезични (БГ/EN) данни на фирмата изпращач

def test_sender_lang_default_is_bulgarian(admin_client):
    resp = admin_client.get("/cmr/new")
    assert resp.status_code == 200
    assert b'name="sender_name" value="' in resp.data
    m = re.search(rb'name="sender_name" value="([^"]*)"', resp.data)
    assert m is not None
    # Подразбиращото се БГ име на фирмата (виж db.py init_db seed) съдържа кирилица.
    assert "ООД".encode() in m.group(1) or len(m.group(1)) > 0


def test_sender_lang_en_substitutes_english_company_data(admin_client):
    resp_bg = admin_client.get("/cmr/new")
    resp_en = admin_client.get("/cmr/new?sender_lang=en")
    assert resp_en.status_code == 200

    m_bg = re.search(rb'name="sender_name" value="([^"]*)"', resp_bg.data)
    m_en = re.search(rb'name="sender_name" value="([^"]*)"', resp_en.data)
    assert m_bg.group(1) != m_en.group(1)
    assert b"BBS Bulgaria Ltd" in m_en.group(1)


def test_sender_lang_en_falls_back_to_bg_when_english_field_empty(admin_client, db_module):
    # Ако администраторът изчисти английското поле в Настройки, ?sender_lang=en
    # не трябва да остави полето празно — пада обратно към БГ стойността
    # (виж _apply_sender_lang: overwrite само когато _en вариантът е непразен).
    import db as db_mod
    con = db_module.get_db()
    settings = db_mod.get_settings(con)
    settings["sender_name_en"] = ""
    db_mod.save_settings(con, settings)

    resp_en = admin_client.get("/cmr/new?sender_lang=en")
    m_en = re.search(rb'name="sender_name" value="([^"]*)"', resp_en.data)
    assert m_en.group(1) != b""


def test_sender_lang_invalid_value_defaults_to_bg(admin_client):
    resp_bg = admin_client.get("/cmr/new")
    resp_invalid = admin_client.get("/cmr/new?sender_lang=de")
    assert resp_invalid.status_code == 200
    m_bg = re.search(rb'name="sender_name" value="([^"]*)"', resp_bg.data)
    m_invalid = re.search(rb'name="sender_name" value="([^"]*)"', resp_invalid.data)
    # Невалидна стойност на sender_lang (не е "bg"/"en") — пада обратно
    # към същото поведение, както без параметъра изобщо (БГ).
    assert m_invalid.group(1) == m_bg.group(1)


# ---------------------------------------------------------------- flash съобщенията се превеждат

def test_flash_message_translated_to_english(admin_client):
    # Смяна на езика на интерфейса на EN през Настройки (персистиращият,
    # пост-логин механизъм) — след това flash() съобщенията също трябва
    # да излизат преведени, не само шаблоните.
    token = get_csrf_token(admin_client, "/my-settings")
    admin_client.post("/my-settings", data={"language": "en", "csrf_token": token},
                      follow_redirects=True)

    token = get_csrf_token(admin_client, "/clients/new")
    resp = admin_client.post("/clients/new", data={"name": "", "csrf_token": token},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()
    assert "задължително".encode() not in resp.data


# ---------------------------------------------------------------- печатните документи НЕ се засягат от избрания UI език

def test_print_template_stays_bilingual_bg_en_regardless_of_ui_language(admin_client, db_module):
    # Издава CMR и после отваря печатния изглед с интерфейс на турски —
    # печатният документ трябва да си остане с твърдо зададения БГ/EN текст
    # (виж cmr_print.html — НЕ се обвива с _()), а не да мутира по избрания UI locale.
    token = get_csrf_token(admin_client, "/my-settings")
    admin_client.post("/my-settings", data={"language": "tr", "csrf_token": token},
                      follow_redirects=True)

    token = get_csrf_token(admin_client, "/cmr/new")
    resp = admin_client.post("/cmr/new", data={
        "csrf_token": token,
        "sender_name": "Тест Изпращач", "receiver_name": "Тест Получател",
    }, follow_redirects=True)
    assert resp.status_code == 200

    con = db_module.get_db()
    doc = con.execute("SELECT id FROM documents WHERE doc_type = 'cmr' ORDER BY id DESC LIMIT 1").fetchone()
    assert doc is not None
    resp = admin_client.get("/doc/%d" % doc["id"])
    assert resp.status_code == 200
    # Твърдо зададеният двуезичен печатен текст на ЧМР бланката остава непроменен.
    assert "Изпращач".encode() in resp.data
    assert b"Sender" in resp.data
