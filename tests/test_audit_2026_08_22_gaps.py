# -*- coding: utf-8 -*-
"""Собствени регресионни тестове (не от архитект патча) за находките от
одита на 22.08.2026, които `tests/test_audit_2026_08_22.py` НЕ покрива:
№2 (постоянен публичен QR адрес), №3 (таван на PDF катинара), №4
(NumberingExhaustedError на ниво маршрут), №7 (маскиране на паролата в
лога) и №9 (собствена страница за разминаване на схемата).

Проверено при преглед на самия патч: CHANGELOG-ът твърди „36 нови теста за
находките от одита“, но `tests/test_audit_2026_08_22.py` съдържа тестове
само за находки №6/№8/№10/№11 (32 теста); находка №1 е покрита с
МОДИФИЦИРАН вече съществуващ тест в `tests/test_audit_2026_08.py` (не нов).
За находки №2/№3/№4/№7/№9 не съществуваше НИКАКЪВ тест никъде в
хранилището — потвърдено чрез `git diff` (нула нови `def test_` функции в
`test_audit_2026_08.py`) и grep за характерни имена/константи
(`NumberingExhaustedError`, `_RENDER_LOCK_TIMEOUT`, `public_base_url`,
`is_schema_mismatch_error`, маскиране на парола) в целия `tests/` пакет
преди тези тестове."""
import sqlite3
import threading
import time

import pytest

from conftest import get_csrf_token, post_with_csrf

import db
import pdf_export
import routes_auth
import routes_clients


# ==================================================================== №4
# NumberingExhaustedError вече стига до потребителя с обяснение и запазен
# въведен текст, вместо гол 500 и изгубен документ.

def test_numbering_exhausted_redirects_with_restore_token_not_a_crash(
        admin_client, db_module, monkeypatch):
    monkeypatch.setattr(db, "_MAX_SEQ_SKIPS", 3)
    con = db_module.get_db()
    for n in range(1, 6):
        con.execute(
            "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
            " data, created_by) VALUES ('cmr', ?, 2026, ?, ?, ?, '{}', 1)",
            ("%04d/2026" % n, n, "B-exh-%d" % n, "t-exh-%d" % n))
    con.commit()
    before = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE doc_type = 'cmr'").fetchone()["c"]
    con.close()

    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Претоварена номерация ООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)

    assert resp.status_code == 302, (
        "изчерпан таван на номерацията трябва да пренасочи с обяснение, "
        "не да гърми със сурова грешка")
    location = resp.headers["Location"]
    assert location.startswith("/cmr/new")
    assert "restore=" in location, "въведеното трябва да е възстановимо, не изгубено"

    con = db_module.get_db()
    after = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE doc_type = 'cmr'").fetchone()["c"]
    con.close()
    assert after == before, "не биваше да се запише документ при изчерпана номерация"

    page = admin_client.get(location)
    body = page.data.decode()
    assert "свободен номер" in body, "конкретното съобщение на грешката трябва да се вижда"
    assert "Претоварена номерация ООД" in body, (
        "въведените данни трябва да са възстановени във формата, не изгубени")


# ==================================================================== №3
# PDF катинарът вече не блокира завинаги — таван от _RENDER_LOCK_TIMEOUT.

def test_pdf_render_lock_gives_up_instead_of_blocking_forever(flask_app, monkeypatch):
    monkeypatch.setattr(pdf_export, "_RENDER_LOCK_TIMEOUT", 0.2)
    held = threading.Event()
    release = threading.Event()

    def _holder():
        pdf_export._render_lock.acquire()
        held.set()
        release.wait(timeout=5)
        pdf_export._render_lock.release()

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert held.wait(timeout=2), "фоновата нишка не успя да вземе катинара"
    try:
        with flask_app.test_request_context():
            with pytest.raises(RuntimeError, match="твърде дълго"):
                pdf_export.generate_document_pdf("Тест", "0001/2026", "B1", [], [], [])
    finally:
        release.set()
        t.join(timeout=2)


def test_pdf_render_lock_is_released_after_a_successful_render(flask_app):
    """Проверка по конструкция: катинарът наистина се пуска — иначе ВСЕКИ
    следващ износ би падал с „твърде дълго“, независимо от таймаута."""
    with flask_app.test_request_context():
        pdf_export.generate_document_pdf("Тест 1", "0001/2026", "B1", [], [], [])
        pdf_export.generate_document_pdf("Тест 2", "0002/2026", "B2", [], [], [])
    assert not pdf_export._render_lock.locked()


# ==================================================================== №2
# Постоянен публичен адрес в QR кода на печатната бланка.

def test_permanent_public_url_is_used_on_the_print_page_when_configured(
        flask_app, admin_client, db_module):
    con = db_module.get_db()
    db.save_settings(con, {"public_base_url": "https://pacho.example.com"})
    con.commit()
    con.close()

    import routes_documents
    with flask_app.test_request_context():
        url, is_local = routes_documents._public_doc_url("sometoken123", for_print=True)
    assert url.startswith("https://pacho.example.com/"), (
        "печатната бланка трябва да носи настроения постоянен адрес")
    assert is_local is False


def test_print_page_falls_back_to_lan_hint_without_a_permanent_url(flask_app, db_module):
    """Без настроен постоянен адрес (подразбиране) печатната бланка НЕ
    бива да носи 127.0.0.1 — точно регресията, поправена от находка №2."""
    import routes_documents
    with flask_app.test_request_context():
        url, _is_local = routes_documents._public_doc_url("sometoken123", for_print=True)
    assert "127.0.0.1" not in url


def test_public_base_url_setting_rejects_addresses_with_spaces(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/admin/system", {
        "form": "public_base_url", "public_base_url": "https://pacho example.com",
    }, csrf_source_url="/my-settings", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "не изглежда валиден" in body
    con = db_module.get_db()
    saved = db.get_settings(con).get("public_base_url", "")
    con.close()
    assert saved == "", "невалиден адрес не биваше да се запише"


# ==================================================================== №7
# Паролата, набрана погрешно в полето за потребителско име, вече не влиза
# пълна в одитния лог при неуспешен вход.

def test_failed_login_masks_a_password_typed_into_the_username_field(client, db_module, capsys):
    secret_password = "MoyataTajnaParola123!"
    csrf = get_csrf_token(client, "/login")
    resp = client.post("/login", data={
        "username": secret_password, "password": "grешно", "csrf_token": csrf,
    })
    assert resp.status_code == 200
    out = capsys.readouterr().out
    assert secret_password not in out, (
        "пълната стойност на полето за потребител не бива да стига до лога "
        "при неуспешен вход — може да е паролата, набрана в грешното поле")
    assert "неуспешен вход" in out


def test_mask_login_keeps_only_a_short_prefix_and_length():
    assert routes_auth._mask_login("") == "(празно)"
    assert routes_auth._mask_login("ab") == "** (дължина 2)"
    masked = routes_auth._mask_login("supersecret")
    assert masked.startswith("su")
    assert "supersecret" not in masked
    assert "дължина 11" in masked


# ==================================================================== №9
# Разминаване на схемата (стар бекъп) получава собствена, различна
# страница от общата „базата е недостъпна“.

def test_schema_mismatch_gets_its_own_page_not_the_generic_unavailable_one(
        admin_client, db_module, monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("no such column: session_epoch")

    monkeypatch.setattr(routes_clients, "paginate_clients", boom)
    resp = admin_client.get("/clients", follow_redirects=False)

    assert resp.status_code == 503
    body = resp.data.decode()
    assert "изисква обновяване" in body.lower() or "структурата" in body.lower(), (
        "схема-разминаването трябва да показва СВОЙ текст, не общото "
        "„базата е недостъпна“/„мрежовия диск“ съобщение")
    assert "НЕ възстановявайте архив" in body or "не възстановявайте архив" in body.lower()
