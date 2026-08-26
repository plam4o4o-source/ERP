# -*- coding: utf-8 -*-
"""Регресионни тестове за задълбочения одит (12.08.2026, „оправи всичките“)
— 38 находки от ERP_ОДИТ_2026_08_12.md, докладван на потребителя и
поправен изцяло в тази версия. Всяка находка, непокрита от по-специфичен
тестов файл (test_backup_local.py, test_icons.py, test_desktop.py
addendum, test_remote_tunnel.py addendum, test_pallet_redesign.py
addendum), е тествана тук."""
import os
import sqlite3
import stat

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

import appcore
import db
import login_guard
import materials
import updater
from conftest import post_with_csrf


# ---------------------------------------------------------------- находка №1: .secret_key права

# Одит (17.08.2026): двата chmod теста тук са POSIX-only по същество —
# на Windows os.chmod() управлява САМО read-only флага, никога пълните
# POSIX права (0600/0644 просто не съществуват там; наблюдавано на
# Windows runner-а: винаги 0666). Самият продуктов код е наясно с това
# (`except OSError: pass` + коментар „файлова система без POSIX права“ в
# db._harden_secret_key_permissions) — защитата е смислена там, където
# ОС-ът я поддържа, и безвредна там, където не я поддържа.
_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="os.chmod(0600) няма POSIX семантика на Windows")


@_posix_only
def test_get_secret_key_hardens_permissions_even_on_existing_file(tmp_path, monkeypatch):
    """Одит (находка №1, критична): преди поправката os.chmod(0600) се
    изпълняваше САМО в клона, в който файлът се създава за пръв път — за
    ВЕЧЕ съществуващ файл (upgrade от по-стара версия, възстановен от
    архив) правата никога не се проверяваха повторно. Симулираме точно
    този случай: файл, вече съществуващ с широки права (0644)."""
    secret_path = str(tmp_path / ".secret_key")
    with open(secret_path, "w", encoding="utf-8") as f:
        f.write("вече-съществуващ-ключ-с-широки-права")
    os.chmod(secret_path, 0o644)
    monkeypatch.setattr(db, "SECRET_PATH", secret_path)

    key = db.get_secret_key()

    assert key == "вече-съществуващ-ключ-с-широки-права"
    mode = stat.S_IMODE(os.stat(secret_path).st_mode)
    assert mode == 0o600, "правата трябва да са заздравени ДОРИ за вече съществуващ файл"


@_posix_only
def test_get_secret_key_hardens_permissions_on_fresh_file(tmp_path, monkeypatch):
    secret_path = str(tmp_path / ".secret_key")
    monkeypatch.setattr(db, "SECRET_PATH", secret_path)
    db.get_secret_key()
    mode = stat.S_IMODE(os.stat(secret_path).st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------- находка №4: pallet_total_qty закръгляне

def test_pallet_total_qty_rounds_away_float_artifacts():
    """Одит (находка №4, high): 0.1 + 0.2 връщаше буквално
    "0.30000000000000004" — сега минава през _fmt_amount(decimals=3),
    същото закръгляне като JS еквивалента (sumQtyForDisplay)."""
    assert appcore.pallet_total_qty([{"qty": "0.1"}, {"qty": "0.2"}]) == "0.3"


def test_pallet_total_qty_still_rejects_negative_rows():
    """С1 остава непокътната — тази поправка засяга САМО форматирането,
    не филтрирането на отрицателни редове."""
    assert appcore.pallet_total_qty([{"qty": "5"}, {"qty": "-2"}]) == "5"


# ---------------------------------------------------------------- находка №3: отрицателни редове предупреждение

def test_negative_item_rows_detects_negative_qty_price_weight():
    assert appcore.negative_item_rows([{"qty": "5"}]) == []
    assert appcore.negative_item_rows([{"qty": "-5"}]) == [1]
    assert appcore.negative_item_rows([{"qty": "5", "unit_price": "-1"}]) == [1]
    assert appcore.negative_item_rows([{"qty": "5"}, {"net_weight": "-3"}]) == [2]
    assert appcore.negative_item_rows([{"qty": "not-a-number"}]) == []


def test_pallet_new_flashes_warning_for_negative_qty_row(admin_client):
    """Одит (находка №3): преди поправката отрицателно количество се
    показваше СУРОВО на бланката, но изчезваше от сборовете, без никакво
    предупреждение при запис."""
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "sender_name": "Тест", "pallet_no": "1 от 1",
        "items_json": '[{"qty":"-5","code":"X"}]',
    }, csrf_source_url="/pallet/new", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "отрицателно количество" in body


# ---------------------------------------------------------------- находка №5: групиране по клиент през страници

def test_paginate_documents_client_grouping_spans_page_boundary(admin_client, db_module):
    """Одит (находка №5, high critical): преди поправката групирането по
    клиент ставаше В PYTHON СЛЕД пагинацията — документи на един и същ
    клиент, разпределени на различни страници, изобщо не се събираха
    заедно. Сега сортирането е част от самата SQL заявка (ПРЕДИ LIMIT/
    OFFSET) — тестваме директно с малък page_size, за да пресъздадем
    обстоятелството без да создаваме >100 документа."""
    import json
    con = db_module.get_db()
    # Два документа на клиент "Апельсин" (алфавитно ПРЕДИ "Круша"), един
    # на "Круша" — вмъкнати в ред, който би ги разбъркал по чист "id DESC"
    # ред (Круша вмъкнат ПРЪВ, но трябва да излезе СЛЕД двата документа на
    # Апельсин при сортиране по клиент).
    for client_name, doc_no in [("Круша", "1"), ("Апельсин", "2"), ("Апельсин", "3")]:
        con.execute(
            "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
            " VALUES ('cmr', ?, 2026, 1, ?, ?, ?, NULL)",
            (doc_no, "BC-%s" % doc_no, "TOK-%s" % doc_no,
             json.dumps({"consignee_name": client_name}, ensure_ascii=False)))
    con.commit()

    client_name_sql = (
        "COALESCE(NULLIF(TRIM(json_extract(d.data,'$.consignee_name')),''),"
        "NULLIF(TRIM(json_extract(d.data,'$.receiver_name')),''),"
        "NULLIF(TRIM(json_extract(d.data,'$.client_name')),''),'')"
    )
    order_by = ("(%s = '') ASC, LOWER(%s) ASC, %s ASC, d.id DESC"
               % (client_name_sql, client_name_sql, client_name_sql))
    # page_size=2 — "Апельсин" (2 документа, алфавитно ПЪРВИ) трябва да
    # излезе ЗАЕДНО на страница 1 (сортирани по клиент), "Круша" на
    # страница 2. Преди поправката (сортиране СЛЕД пагинацията) страница 1
    # би върнала произволна двойка по "id DESC" (Апельсин-3, Круша-1),
    # разцепвайки Апельсин на две страници.
    docs_p1, page1, total_pages, total_count = appcore.paginate_documents(
        con, "WHERE d.doc_type = 'cmr'", [], 1, page_size=2, order_by=order_by)
    docs_p2, page2, _, _ = appcore.paginate_documents(
        con, "WHERE d.doc_type = 'cmr'", [], 2, page_size=2, order_by=order_by)

    assert total_count == 3
    metas_p1 = [appcore.safe_json_data(d["data"]) for d in docs_p1]
    assert all(m["consignee_name"] == "Апельсин" for m in metas_p1)
    metas_p2 = [appcore.safe_json_data(d["data"]) for d in docs_p2]
    assert metas_p2[0]["consignee_name"] == "Круша"


def test_documents_group_by_client_route_still_works(admin_client):
    """Смок тест: ?group=client не гърми и връща 200 (маршрутно ниво)."""
    resp = admin_client.get("/docs?group=client")
    assert resp.status_code == 200


# ---------------------------------------------------------------- находка №10: реален bound порт

def test_runtime_port_defaults_to_fallback_when_unset():
    appcore._RUNTIME_STATE["port"] = None
    assert appcore.get_runtime_port(5000) == 5000


def test_runtime_port_uses_set_value_when_available():
    appcore.set_runtime_port(5050)
    try:
        assert appcore.get_runtime_port(5000) == 5050
    finally:
        appcore._RUNTIME_STATE["port"] = None


# ---------------------------------------------------------------- находка №13: UNIQUE(doc_type, number)

def test_migration_creates_unique_index_when_no_duplicates(db_module):
    # Одит (16.08.2026, находка №16): _m005_document_number_unique_per_year
    # ЗАМЕНИ идекса от тази находка (idx_documents_type_number, БЕЗ година)
    # с idx_documents_type_year_number (С година) — виж db._m005 за пълния
    # разказ защо старият индекс блокираше легитимна годишно рестартираща
    # номерация. На чиста нова база сега стои НОВИЯТ индекс, старият вече
    # не се създава изобщо (DROP INDEX IF EXISTS в _m005).
    con = db_module.get_db()
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_type_year_number'"
    ).fetchone()
    assert row is not None, "миграцията трябва да е създала индекса за чиста нова база"


def test_migration_skips_unique_index_when_duplicates_already_exist(tmp_path, monkeypatch):
    """Одит (находка №13): безопасно за вече съществуващи бази с
    ИСТОРИЧЕСКИ дублирани номера — миграцията НЕ трябва да гърми, само да
    пропусне добавянето на индекса и да логне предупреждение."""
    db_path = str(tmp_path / "dupes.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "SECRET_PATH", db_path + ".secret")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    # Прилагаме схемата РЪЧНО, без миграциите, после вмъкваме дублирани
    # номера, после пускаме миграциите — симулира upgrade на стара база.
    con.executescript(db.SCHEMA)
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_by)"
        " VALUES ('cmr', 'DUP-1', 2026, 1, 'BC-1', '{}', NULL)")
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_by)"
        " VALUES ('cmr', 'DUP-1', 2026, 2, 'BC-2', '{}', NULL)")
    con.commit()
    # Не трябва да гърми, въпреки съществуващия дубликат.
    db._apply_migrations(con)
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_type_number'"
    ).fetchone()
    assert row is None, "индексът НЕ трябва да се създаде, докато има дублирани номера"
    con.close()


def test_duplicate_manual_invoice_number_is_rejected_with_friendly_error(admin_client, db_module):
    """Одит (находка №13): при вече наложения UNIQUE индекс, два документа
    със СЪЩИЯ ръчен номер водят до ясна грешка (flash + redirect), не гол
    500 IntegrityError."""
    # Одит (16.08.2026, находка №16): виж бележката в test_migration_creates_
    # unique_index_when_no_duplicates по-горе — проверяваме НОВИЯ индекс.
    con = db_module.get_db()
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_type_year_number'"
    ).fetchone()
    assert row is not None  # предпоставка на теста — индексът трябва да го има

    data = {
        "sender_name": "Тест", "consignee_name": "Клиент",
        "invoice_number": "INV-DUP-1", "items_json": "[]",
    }
    resp1 = post_with_csrf(admin_client, "/invoice-br/new", data,
                           csrf_source_url="/invoice-br/new", follow_redirects=False)
    assert resp1.status_code == 302

    resp2 = post_with_csrf(admin_client, "/invoice-br/new", data,
                           csrf_source_url="/invoice-br/new", follow_redirects=True)
    assert resp2.status_code == 200
    body = resp2.data.decode("utf-8")
    assert "вече е зает" in body


# ---------------------------------------------------------------- находка №14/№15: login throttle/timing

def test_global_login_throttle_engages_after_threshold():
    login_guard.reset_global()
    try:
        for _ in range(login_guard._GLOBAL_MAX_ATTEMPTS):
            login_guard.register_global_attempt()
            assert login_guard.is_globally_throttled() is False
        login_guard.register_global_attempt()
        assert login_guard.is_globally_throttled() is True
    finally:
        login_guard.reset_global()


def test_login_route_rejects_when_globally_throttled(client, monkeypatch):
    monkeypatch.setattr(login_guard, "is_globally_throttled", lambda: True)
    token = None
    resp = client.get("/login")
    import re
    m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', resp.data)
    token = m.group(1).decode()
    resp = client.post("/login", data={
        "username": "admin", "password": "wrong", "csrf_token": token,
    })
    assert resp.status_code == 200
    assert "Твърде много опити" in resp.data.decode("utf-8")


def test_check_password_hash_runs_for_nonexistent_username(client, monkeypatch):
    """Одит (находка №15): при липсващ потребител СЪЩО трябва да се
    изпълни check_password_hash (върху dummy хеш) — не short-circuit."""
    import routes_auth
    calls = []
    real_check = routes_auth.check_password_hash

    def spy(pw_hash, password):
        calls.append(pw_hash)
        return real_check(pw_hash, password)

    monkeypatch.setattr(routes_auth, "check_password_hash", spy)
    import re
    resp = client.get("/login")
    m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', resp.data)
    token = m.group(1).decode()
    client.post("/login", data={
        "username": "напълно-несъществуващ-потребител-xyz",
        "password": "каквото-и-да-е", "csrf_token": token,
    })
    assert len(calls) == 1
    assert calls[0] == routes_auth._DUMMY_PASSWORD_HASH


# ---------------------------------------------------------------- находка №17
# Бележка (25.08.2026): тестовете за проверката „частно ли е GitHub
# хранилището“ и предупреждението при публично хранилище отпаднаха заедно с
# премахнатата GitHub синхронизация.


# ---------------------------------------------------------------- находка №18: security headers

def test_response_has_security_headers(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


# ---------------------------------------------------------------- находка №19: materials.parse_number строгост

def test_materials_parse_number_rejects_nan_and_inf():
    assert materials.parse_number("nan") is None
    assert materials.parse_number("inf") is None
    assert materials.parse_number("-inf") is None


def test_materials_parse_number_accepts_comma_decimal_and_nbsp_thousands():
    assert materials.parse_number("5,5") == 5.5
    assert materials.parse_number("1\xa0234") == 1234.0


def test_materials_parse_number_still_accepts_plain_numbers():
    assert materials.parse_number("42") == 42.0
    assert materials.parse_number(None) is None
    assert materials.parse_number("") is None


# ---------------------------------------------------------------- находка №20: invoices_list дата филтър + споделена пагинация

def test_invoices_list_supports_date_range_filter(admin_client):
    resp = admin_client.get("/invoices?from=2020-01-01&to=2020-01-02")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert 'name="from"' in body
    assert 'name="to"' in body


# ---------------------------------------------------------------- находка №21: copies=-1

def test_view_document_rejects_negative_copies(admin_client):
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "sender_name": "Тест", "pallet_no": "1 от 1",
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_url = resp.headers["Location"]
    resp2 = admin_client.get(doc_url + "?copies=-1")
    assert resp2.status_code == 200
    # Отрицателен брой копия трябва да падне към 1, не към празна страница.
    body = resp2.data.decode("utf-8")
    assert "plt-head" in body or "ПАЛЕТНА КАРТА" in body


# ---------------------------------------------------------------- находка №22: delete_document 404

def test_delete_nonexistent_document_returns_404(admin_client):
    resp = post_with_csrf(admin_client, "/doc/999999/delete", {},
                          csrf_source_url="/docs", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------- находка №28: login_guard почистване

def test_login_guard_cleanup_removes_stale_entries():
    login_guard.reset_all()
    try:
        now = 1_000_000.0
        # Един стар (изтекъл) запис.
        login_guard.register_failure("stale-user", now=now - login_guard.WINDOW_SECONDS - 100)
        # N-1 допълнителни повиквания, за да не задействаме почистването преждевременно.
        for i in range(login_guard._CLEANUP_EVERY_N_CALLS - 2):
            login_guard.is_locked_out("someone-else-%d" % i, now=now)
        assert "stale-user" in login_guard._attempts
        # Едно повикване повече — трябва да задейства почистването.
        login_guard.is_locked_out("trigger", now=now)
        assert "stale-user" not in login_guard._attempts
    finally:
        login_guard.reset_all()


# ---------------------------------------------------------------- находка №29: change_password rate limit

def test_change_password_locks_out_after_repeated_wrong_current_password(admin_client):
    login_guard.reset_all()
    try:
        for _ in range(login_guard.MAX_ATTEMPTS):
            resp = post_with_csrf(admin_client, "/password", {
                "current": "грешна-парола", "new": "newpassword123", "repeat": "newpassword123",
            }, csrf_source_url="/password", follow_redirects=True)
            assert "Текущата парола е грешна" in resp.data.decode("utf-8")
        resp = post_with_csrf(admin_client, "/password", {
            "current": "грешна-парола-пак", "new": "newpassword123", "repeat": "newpassword123",
        }, csrf_source_url="/password", follow_redirects=True)
        body = resp.data.decode("utf-8")
        assert "Твърде много грешни опити" in body
    finally:
        login_guard.reset_all()


# ---------------------------------------------------------------- находка №36: cloudflared кеш преверификация

def test_ensure_binary_redownloads_when_cached_file_has_wrong_magic(tmp_path, monkeypatch):
    import remote_tunnel
    path = os.path.join(str(tmp_path), "bin", "cloudflared")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"CORRUPTED-NOT-A-VALID-BINARY" + b"\x00" * 200000)
    monkeypatch.setattr(remote_tunnel, "_binary_path", lambda: path)

    class _FakeResponse:
        def __init__(self, data):
            self._data = data
            self._pos = 0
            self.headers = {"Content-Length": str(len(data))}

        def read(self, n=-1):
            if self._pos >= len(self._data):
                return b""
            chunk = self._data[self._pos:self._pos + n] if n and n > 0 else self._data[self._pos:]
            self._pos += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # Одит (17.08.2026): _expected_magic(), НЕ хардкоднат b"\x7fELF" —
    # магическите байтове зависят от платформата (ELF на Linux, "MZ" на
    # Windows), а pytest порталът в release.yml вече кара тези тестове
    # да вървят и на истински Windows runner (там ELF payload биваше
    # правилно отхвърлян от самата проверка, която тестът тества).
    magic = remote_tunnel._expected_magic()
    fresh_payload = magic + b"\x00" * 200000
    calls = []

    def fake_urlopen(req, timeout=60):
        calls.append(req)
        return _FakeResponse(fresh_payload)

    monkeypatch.setattr(remote_tunnel.net, "urlopen", fake_urlopen)
    result = remote_tunnel.ensure_binary()
    assert len(calls) == 1, "кешираният файл с грешни магически байтове трябва да предизвика ново изтегляне"
    assert result == path
    with open(path, "rb") as f:
        assert f.read(len(magic)) == magic


def test_ensure_binary_reuses_valid_cached_file_without_network(tmp_path, monkeypatch):
    import remote_tunnel
    path = os.path.join(str(tmp_path), "bin", "cloudflared")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        # _expected_magic() вместо хардкоднат ELF — вижте коментара в
        # test_ensure_binary_redownloads_when_cached_file_has_wrong_magic.
        f.write(remote_tunnel._expected_magic() + b"\x00" * 200000)
    monkeypatch.setattr(remote_tunnel, "_binary_path", lambda: path)

    def _boom(req, timeout=60):
        raise AssertionError("валиден кеширан файл не бива да предизвиква мрежова заявка")
    monkeypatch.setattr(remote_tunnel.net, "urlopen", _boom)

    result = remote_tunnel.ensure_binary()
    assert result == path


# ---------------------------------------------------------------- находка №37: updater .exe.new почистване

def test_install_update_cleans_up_stale_new_exe_before_downloading(tmp_path, monkeypatch):
    fake_exe = str(tmp_path / "PachoLogistic.exe")
    with open(fake_exe, "wb") as f:
        f.write(b"MZ" + b"\x00" * 100)
    stale_new = fake_exe + ".new"
    with open(stale_new, "wb") as f:
        f.write(b"stale-leftover-from-previous-failed-attempt")

    monkeypatch.setattr(updater.sys, "executable", fake_exe)
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)

    # Изтеглянето само по себе си ще гръмне (невалиден payload) — целта на
    # теста е само да потвърди, че СТАРИЯТ .new файл е бил изчистен ПРЕДИ
    # новото изтегляне да започне (иначе щеше да наследи старото му
    # съдържание вместо да пише отначало).
    class _FakeResponse:
        headers = {}

        def read(self, n=-1):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _FakeResponse())
    with pytest.raises(RuntimeError, match="повреден"):
        updater.install_update("http://example.invalid/fake.exe")
    # new_exe е (пре)записан с изтегленото (празно, после отхвърлено и
    # изтрито от валидацията) съдържание, НЕ старото — потвърждаваме, че
    # старият remnant вече не съществува с непроменено съдържание (или
    # изобщо не съществува — и двете доказват, че НЕ е бил просто оставен
    # непипнат от предишния неуспешен опит).
    assert not (os.path.exists(stale_new)
               and open(stale_new, "rb").read() == b"stale-leftover-from-previous-failed-attempt")
