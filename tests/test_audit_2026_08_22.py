# -*- coding: utf-8 -*-
"""Регресионни тестове за четирите находки от одита на 22.08.2026, които
се поправят тук:

  №6  (средна)  — миграцията `_m010` триеше редове от справочника материали
                  без видима следа, а критерият „най-скоро обновеният“ на
                  практика не работеше (еднакъв `updated_at` в целия
                  каталог след един Excel импорт).
  №8  (средна)  — публичният QR токен изтича след 180 дни, но операторът
                  нито виждаше срока, нито можеше да го поднови/отнеме, а
                  изтеклата бланка даваше гол 404.
  №10 (дребна)  — `_reschedule_debounce` не отменяше `retry_timer` и при
                  заето качване правеше busy-poll на всеки 8 секунди.
  №11 (дребна)  — преводен низ стигаше до `innerHTML` в static/app.js.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from conftest import post_with_csrf

import applog
import backup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==================================================================== №6
# Слетите кодове на материали вече имат резервно копие и ВИДИМО
# предупреждение.

def _legacy_materials_db(tmp_db_path, monkeypatch, rows):
    """База „отпреди миграцията“ (user_version = 0) само с таблица
    `materials` — точно както изглежда реална v3.41 инсталация. `rows` са
    кортежи (code, description, net_weight, updated_at)."""
    import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_db_path)
    monkeypatch.setattr(db_mod, "SECRET_PATH", tmp_db_path + ".secret")

    raw = sqlite3.connect(tmp_db_path)
    raw.execute(
        "CREATE TABLE materials (code TEXT PRIMARY KEY,"
        " description TEXT NOT NULL DEFAULT '', net_weight TEXT NOT NULL DEFAULT '',"
        " updated_at TEXT NOT NULL DEFAULT '')")
    raw.executemany("INSERT INTO materials VALUES (?, ?, ?, ?)", rows)
    raw.commit()
    raw.close()

    db_mod.init_db()
    return db_mod


def test_merged_material_rows_are_backed_up_before_being_deleted(tmp_db_path, monkeypatch):
    """Ядрото на находка №6б: миграцията ИЗТРИВА редове необратимо, а
    единствената следа беше `applog.log_warning` в pacho_startup.log — файл,
    който потребител на .exe никога не отваря. Сега преди DELETE всички
    варианти се копират в `materials_merged_backup`."""
    same_ts = "2026-08-01 12:00:00"   # един Excel импорт → един и същ час
    db_mod = _legacy_materials_db(tmp_db_path, monkeypatch, [
        ("abc-1", "вариант 1", "1.500", same_ts),
        ("ABC-1", "вариант 2", "9.750", same_ts),
        ("Abc-1", "вариант 3", "4.250", same_ts),
    ])
    con = db_mod.get_db()
    try:
        assert con.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"] == 1
        backup_rows = con.execute(
            "SELECT code, net_weight, kept, weight_conflict"
            " FROM materials_merged_backup ORDER BY code").fetchall()
        # ВСИЧКИТЕ три варианта са запазени, не само изтритите два.
        assert len(backup_rows) == 3
        assert {r["code"] for r in backup_rows} == {"abc-1", "ABC-1", "Abc-1"}
        assert sum(r["kept"] for r in backup_rows) == 1
        # Различни тегла = по-опасният случай, отбелязан изрично.
        assert all(r["weight_conflict"] == 1 for r in backup_rows)
    finally:
        con.close()


def test_merge_raises_a_visible_flag_in_settings(tmp_db_path, monkeypatch):
    """Находка №6б: следата вече не е само в лог файла — има флаг в
    `settings`, който интерфейсът чете."""
    db_mod = _legacy_materials_db(tmp_db_path, monkeypatch, [
        ("dup-9", "а", "1", "2026-08-01 12:00:00"),
        ("DUP-9", "б", "2", "2026-08-01 12:00:00"),
    ])
    con = db_mod.get_db()
    try:
        assert db_mod.MATERIALS_MERGE_NOTICE_KEY in db_mod.get_settings(con)
        notice = db_mod.merged_materials_notice(con)
        assert len(notice) == 1
        group = notice[0]
        assert group["code_upper"] == "DUP-9"
        assert group["weight_conflict"] is True
        assert group["kept"] is not None
        assert len(group["removed"]) == 1
    finally:
        con.close()


def test_clean_catalog_raises_no_flag_and_no_backup(tmp_db_path, monkeypatch):
    """Обратната посока — база без дубликати по регистър не бива да
    вдига предупреждение (иначе всеки оператор би го игнорирал)."""
    db_mod = _legacy_materials_db(tmp_db_path, monkeypatch, [
        ("aa-1", "а", "1", "2026-08-01 12:00:00"),
        ("bb-2", "б", "2", "2026-08-01 12:00:00"),
    ])
    con = db_mod.get_db()
    try:
        assert db_mod.MATERIALS_MERGE_NOTICE_KEY not in db_mod.get_settings(con)
        assert db_mod.merged_materials_notice(con) == []
        assert con.execute(
            "SELECT COUNT(*) c FROM materials_merged_backup").fetchone()["c"] == 0
    finally:
        con.close()


def test_identical_timestamps_keep_the_variant_that_actually_has_a_weight(
        tmp_db_path, monkeypatch):
    """Находка №6а: `ORDER BY updated_at DESC, rowid DESC` при ЕДНАКЪВ
    `updated_at` (типично за каталог, зареден с ЕДИН Excel импорт —
    секундна точност) се свеждаше до „последно вмъкнатият“, тоест напълно
    произволен избор. Тук последно вмъкнатият е БЕЗ тегло, а трябва да
    оцелее редът, който реално носи тегло — то отива на митническия
    опаковъчен лист."""
    same_ts = "2026-08-01 12:00:00"
    db_mod = _legacy_materials_db(tmp_db_path, monkeypatch, [
        ("kg-5", "с тегло", "7.500", same_ts),
        ("KG-5", "без тегло", "", same_ts),      # вмъкнат ПОСЛЕДЕН
    ])
    con = db_mod.get_db()
    try:
        row = con.execute("SELECT code, net_weight FROM materials").fetchone()
        assert row["net_weight"] == "7.500", (
            "при равен updated_at трябва да оцелее редът с реално тегло, "
            "не просто последно вмъкнатият")
        assert row["code"] == "kg-5"
        # Само единият вариант носи непразно тегло → това НЕ е конфликт.
        conflicts = con.execute(
            "SELECT DISTINCT weight_conflict FROM materials_merged_backup").fetchall()
        assert [r["weight_conflict"] for r in conflicts] == [0]
    finally:
        con.close()


def test_most_recently_updated_row_still_wins_when_timestamps_differ(
        tmp_db_path, monkeypatch):
    """Пазач да не сме счупили обещанието от находка №28б: когато
    `updated_at` РЕАЛНО се различава, той пак е първият критерий."""
    db_mod = _legacy_materials_db(tmp_db_path, monkeypatch, [
        ("zz-3", "старо", "1.000", "2026-01-01 10:00:00"),
        ("ZZ-3", "ново", "9.999", "2026-08-01 10:00:00"),
    ])
    con = db_mod.get_db()
    try:
        assert con.execute(
            "SELECT net_weight FROM materials").fetchone()["net_weight"] == "9.999"
    finally:
        con.close()


def _seed_merge_notice(db_module, weight_conflict=1):
    con = db_module.get_db()
    con.execute(
        "INSERT INTO materials_merged_backup (code_upper, code, description,"
        " net_weight, updated_at, kept, weight_conflict)"
        " VALUES ('WIDGET-7', 'widget-7', 'останал', '3.100', '', 1, ?)",
        (weight_conflict,))
    con.execute(
        "INSERT INTO materials_merged_backup (code_upper, code, description,"
        " net_weight, updated_at, kept, weight_conflict)"
        " VALUES ('WIDGET-7', 'WIDGET-7', 'изтрит', '8.400', '', 0, ?)",
        (weight_conflict,))
    db_module.save_settings(
        con, {db_module.MATERIALS_MERGE_NOTICE_KEY: "2026-08-22 09:00:00"})
    con.commit()
    con.close()


def test_materials_page_shows_the_merge_warning_with_codes_and_weights(
        admin_client, db_module):
    """Находка №6б: предупреждението стига до ЕКРАНА, не само до лога — със
    списък кои кодове са слети и кое тегло е останало."""
    _seed_merge_notice(db_module)
    body = admin_client.get("/materials").data.decode("utf-8")
    assert "WIDGET-7" in body
    assert "widget-7" in body
    assert "3.100" in body and "8.400" in body, "и оцелялото, и премахнатото тегло"
    assert "РАЗЛИЧНО нето тегло" in body, "по-опасният случай трябва да е отбелязан"


def test_merge_warning_marks_weight_conflicts_only_when_they_exist(
        admin_client, db_module):
    _seed_merge_notice(db_module, weight_conflict=0)
    body = admin_client.get("/materials").data.decode("utf-8")
    assert "WIDGET-7" in body
    assert "РАЗЛИЧНО нето тегло" not in body


def test_materials_page_has_no_warning_when_nothing_was_merged(admin_client):
    body = admin_client.get("/materials").data.decode("utf-8")
    assert "слети кодове" not in body
    assert "WIDGET-7" not in body


def test_dismissing_the_warning_keeps_the_backup_rows(admin_client, db_module):
    """Скрива се СЪОБЩЕНИЕТО, не доказателството — копията на изтритите
    редове остават в базата завинаги."""
    _seed_merge_notice(db_module)
    resp = post_with_csrf(admin_client, "/materials/merge-notice/dismiss", {},
                          csrf_source_url="/materials", follow_redirects=True)
    assert resp.status_code == 200
    assert "WIDGET-7" not in resp.data.decode("utf-8")

    con = db_module.get_db()
    try:
        assert db_module.MATERIALS_MERGE_NOTICE_KEY not in db_module.get_settings(con)
        assert con.execute(
            "SELECT COUNT(*) c FROM materials_merged_backup").fetchone()["c"] == 2
        assert db_module.merged_materials_notice(con) == []
    finally:
        con.close()


def test_dismissing_the_warning_requires_admin(employee_client, db_module):
    _seed_merge_notice(db_module)
    resp = post_with_csrf(employee_client, "/materials/merge-notice/dismiss", {},
                          csrf_source_url="/materials")
    assert resp.status_code in (302, 403)
    con = db_module.get_db()
    try:
        assert db_module.MATERIALS_MERGE_NOTICE_KEY in db_module.get_settings(con)
    finally:
        con.close()


# ==================================================================== №8
# Публичният токен: видим срок, подновяване, отнемане и смислена страница
# при изтекъл линк.

def _issue_cmr(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач ЕООД", "consignee_name": "Одит 22.08 ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def _token_and_expiry(db_module, doc_id):
    con = db_module.get_db()
    try:
        row = con.execute(
            "SELECT public_token, public_token_expires_at FROM documents WHERE id = ?",
            (doc_id,)).fetchone()
        return row["public_token"], row["public_token_expires_at"]
    finally:
        con.close()


def _set_expiry(db_module, doc_id, value):
    con = db_module.get_db()
    con.execute("UPDATE documents SET public_token_expires_at = ? WHERE id = ?",
                (value, doc_id))
    con.commit()
    con.close()


def test_document_view_shows_when_the_public_access_expires(admin_client, db_module):
    """Находка №8(1): дотук колоната се попълваше при издаване и НИКОЙ не
    я четеше — операторът нямаше как да разбере, че QR кодът на бланката
    ще спре да работи, нито кога."""
    doc_id = _issue_cmr(admin_client)
    _token, expires = _token_and_expiry(db_module, doc_id)
    assert expires, "новоиздаден документ трябва да има срок"

    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    assert "Публичен достъп през QR кода" in body
    # Датата се показва в български формат (ДД.ММ.ГГГГ), както навсякъде.
    day, month, year = expires[8:10], expires[5:7], expires[0:4]
    assert "%s.%s.%s" % (day, month, year) in body
    assert "Поднови за още" in body
    assert "Отнеми достъпа сега" in body


def test_expired_public_link_explains_itself_instead_of_a_bare_404(
        client, admin_client, db_module):
    """Ядрото на находка №8(3): архивна бланка, сканирана шест месеца
    по-късно при спор или митническа проверка, даваше гол 404 — все едно
    документът никога не е съществувал."""
    doc_id = _issue_cmr(admin_client)
    token, _expires = _token_and_expiry(db_module, doc_id)
    assert client.get("/p/%s" % token).status_code == 200

    _set_expiry(db_module, doc_id, "2020-01-01 00:00:00")
    resp = client.get("/p/%s" % token)
    assert resp.status_code == 410, "410 Gone — ресурсът Е СЪЩЕСТВУВАЛ тук"
    body = resp.data.decode("utf-8")
    assert "изтекъл" in body
    assert "издателя" in body


def test_the_expired_page_leaks_nothing_from_the_document(
        client, admin_client, db_module):
    """Токенът е изтекъл — носителят му вече няма право на съдържанието."""
    doc_id = _issue_cmr(admin_client)
    token, _expires = _token_and_expiry(db_module, doc_id)
    _set_expiry(db_module, doc_id, "2020-01-01 00:00:00")

    body = client.get("/p/%s" % token).data.decode("utf-8")
    assert "Одит 22.08 ЕООД" not in body
    assert "Изпращач ЕООД" not in body


def test_unknown_token_is_still_a_plain_404(client):
    """Непознат адрес не бива да се различава от несъществуващ — иначе
    „изтекъл“ би издало, че такъв документ изобщо е бил издаван."""
    assert client.get("/p/deadbeefdeadbeefdeadbeefdeadbeef").status_code == 404


def test_renew_brings_an_expired_public_link_back_to_life(
        client, admin_client, db_module):
    """Находка №8(2): преди това единственият изход беше преиздаване на
    документа — тоест НОВ номер на вече подписан транспортен документ."""
    import appcore

    doc_id = _issue_cmr(admin_client)
    token, _expires = _token_and_expiry(db_module, doc_id)
    _set_expiry(db_module, doc_id, "2020-01-01 00:00:00")
    assert client.get("/p/%s" % token).status_code == 410

    resp = post_with_csrf(admin_client, "/doc/%d/public-link/renew" % doc_id, {},
                          csrf_source_url="/doc/%d" % doc_id, follow_redirects=False)
    assert resp.status_code == 302

    _t, new_expiry = _token_and_expiry(db_module, doc_id)
    expected = datetime.now() + timedelta(days=appcore.PUBLIC_TOKEN_TTL_DAYS)
    assert abs((datetime.strptime(new_expiry, "%Y-%m-%d %H:%M:%S")
                - expected).total_seconds()) < 120
    assert client.get("/p/%s" % token).status_code == 200


def test_revoke_stops_the_public_link_immediately(client, admin_client, db_module):
    """Находка №8(2): бланка, попаднала у когото не трябва — достъпът
    трябва да спре ВЕДНАГА, без да се трие самият документ."""
    doc_id = _issue_cmr(admin_client)
    token, _expires = _token_and_expiry(db_module, doc_id)
    assert client.get("/p/%s" % token).status_code == 200

    resp = post_with_csrf(admin_client, "/doc/%d/public-link/revoke" % doc_id, {},
                          csrf_source_url="/doc/%d" % doc_id, follow_redirects=False)
    assert resp.status_code == 302
    assert client.get("/p/%s" % token).status_code == 410
    # Самият документ остава непокътнат за логнатия оператор.
    assert admin_client.get("/doc/%d" % doc_id).status_code == 200


def test_revoking_never_makes_the_link_eternal(admin_client, db_module):
    """NULL в тази колона означава „безсрочен“ — отнемането ТРЯБВА да
    записва минал момент, а не да зануляви колоната."""
    doc_id = _issue_cmr(admin_client)
    post_with_csrf(admin_client, "/doc/%d/public-link/revoke" % doc_id, {},
                   csrf_source_url="/doc/%d" % doc_id)
    _t, expiry = _token_and_expiry(db_module, doc_id)
    assert expiry is not None
    assert expiry < datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.parametrize("action", ["renew", "revoke"])
def test_public_link_actions_require_login(client, admin_client, db_module, action):
    doc_id = _issue_cmr(admin_client)
    _t, before = _token_and_expiry(db_module, doc_id)

    resp = client.post("/doc/%d/public-link/%s" % (doc_id, action),
                       data={"csrf_token": "x"})
    assert resp.status_code in (302, 400, 403)
    _t2, after = _token_and_expiry(db_module, doc_id)
    assert after == before, "нелогнат посетител не бива да променя срока"


@pytest.mark.parametrize("action", ["renew", "revoke"])
def test_public_link_actions_require_csrf(admin_client, db_module, action):
    doc_id = _issue_cmr(admin_client)
    _t, before = _token_and_expiry(db_module, doc_id)
    resp = admin_client.post("/doc/%d/public-link/%s" % (doc_id, action), data={})
    assert resp.status_code == 400
    _t2, after = _token_and_expiry(db_module, doc_id)
    assert after == before


@pytest.mark.parametrize("action", ["renew", "revoke"])
def test_public_link_actions_are_written_to_the_audit_trail(
        admin_client, monkeypatch, action):
    """Находка №8: и двете действия менят кой вижда документа отвън —
    точно класът действия, заради който съществува applog.log_audit."""
    recorded = []
    monkeypatch.setattr(applog, "log_audit",
                        lambda a, d="": recorded.append((a, d)))
    doc_id = _issue_cmr(admin_client)
    post_with_csrf(admin_client, "/doc/%d/public-link/%s" % (doc_id, action), {},
                   csrf_source_url="/doc/%d" % doc_id)
    assert any("публичен достъп" in a for a, _d in recorded), recorded


@pytest.mark.parametrize("action", ["renew", "revoke"])
def test_public_link_actions_404_for_a_missing_document(admin_client, action):
    resp = post_with_csrf(admin_client, "/doc/999999/public-link/%s" % action, {},
                          csrf_source_url="/docs")
    assert resp.status_code == 404


def test_documents_issued_before_the_ttl_stay_eternal(admin_client, db_module):
    """NULL = безсрочен (находка №20) — не бива изведнъж да спрем QR кодове
    върху бланки, които са в движение при клиенти."""
    doc_id = _issue_cmr(admin_client)
    _set_expiry(db_module, doc_id, None)
    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    assert "БЕЗСРОЧЕН" in body


def test_public_token_status_separates_missing_from_expired(con, db_module):
    """Двата случая вече са РАЗЛИЧНИ на ниво db — дотук и двата връщаха
    None и извикващият нямаше как да ги различи."""
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
        " public_token_expires_at, data) VALUES ('cmr', '1', 2026, 1, 'B', 'tok-1',"
        " '2020-01-01 00:00:00', '{}')")
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
        " public_token_expires_at, data) VALUES ('cmr', '2', 2026, 2, 'C', 'tok-2',"
        " NULL, '{}')")
    con.commit()

    status, doc_id = db_module.get_public_token_status(con, "tok-1")
    assert status == db_module.PUBLIC_TOKEN_EXPIRED and doc_id is not None
    assert db_module.get_document_id_by_public_token(con, "tok-1") is None

    status, doc_id = db_module.get_public_token_status(con, "tok-2")
    assert status == db_module.PUBLIC_TOKEN_OK

    status, doc_id = db_module.get_public_token_status(con, "няма-такъв")
    assert status == db_module.PUBLIC_TOKEN_MISSING and doc_id is None


# =================================================================== №10
# Дебаунсът на GitHub синхронизацията.

@pytest.fixture(autouse=True)
def _reset_backup_state():
    """Същата изолация като в test_audit_2026_08_16 — `backup._sync_state`
    е модулно глобално състояние и не се нулира между тестовете."""
    with backup._sync_lock:
        snapshot = dict(backup._sync_state)
    yield
    with backup._sync_lock:
        for key in ("debounce_timer", "retry_timer"):
            timer = backup._sync_state.get(key)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
        backup._sync_state.clear()
        backup._sync_state.update(snapshot)
    if backup._upload_lock.locked():
        try:
            backup._upload_lock.release()
        except RuntimeError:
            pass


def _cfg():
    return {"gh_auto_sync": True, "gh_owner": "o", "gh_repo": "r", "gh_token": "t",
            "gh_branch": "main", "gh_path": "pacho_logistic.db"}


def test_reschedule_debounce_also_cancels_the_retry_timer(monkeypatch):
    """Находка №10 (първа половина): `mark_dirty` отменя И ДВАТА таймера, а
    общата `_reschedule_debounce` отменяше само дебаунса — оставаше жив
    retry таймер за СЪЩОТО действие, тоест `_attempt_sync` тръгваше два
    пъти за едно пренасрочване."""
    import threading

    monkeypatch.setattr(backup, "DEBOUNCE_SECONDS", 999)
    stale = threading.Timer(999, lambda: None)
    stale.daemon = True
    stale.start()
    with backup._sync_lock:
        backup._sync_state["retry_timer"] = stale

    backup._reschedule_debounce(lambda: _cfg())

    with backup._sync_lock:
        assert backup._sync_state["retry_timer"] is None, "забравен retry таймер"
        assert backup._sync_state["debounce_timer"] is not None
        backup._sync_state["debounce_timer"].cancel()
    assert not stale.is_alive() or stale.finished.is_set(), "старият retry не е отменен"


def test_busy_collision_delay_grows_and_is_capped():
    """Находка №10 (втора половина): докато ръчното „Качи сега“ държи
    `_upload_lock`, автоматичният път се пренасрочваше на всеки 8 секунди
    БЕЗКРАЙНО (busy-poll). Делтата вече расте до таван."""
    with backup._sync_lock:
        backup._sync_state["busy_delay"] = 0
    seen = [backup._busy_delay() for _ in range(8)]
    assert seen[0] == backup.DEBOUNCE_SECONDS
    assert seen[1] == backup.DEBOUNCE_SECONDS * 2
    assert seen == sorted(seen), "делтата не бива да намалява"
    assert max(seen) == backup.BUSY_MAX_SECONDS
    assert seen[-1] == backup.BUSY_MAX_SECONDS


def test_normal_reschedule_resets_the_busy_backoff(monkeypatch):
    """Щом сблъсъкът приключи, следващият започва пак от 8 сек — иначе
    първата колизия би направила приложението мързеливо завинаги."""
    monkeypatch.setattr(backup, "DEBOUNCE_SECONDS", 999)
    backup._busy_delay()
    backup._busy_delay()
    with backup._sync_lock:
        assert backup._sync_state["busy_delay"] > 0

    backup._reschedule_debounce(lambda: _cfg())
    with backup._sync_lock:
        assert backup._sync_state["busy_delay"] == 0
        backup._sync_state["debounce_timer"].cancel()


def test_attempt_sync_uses_the_growing_delay_when_the_upload_lock_is_busy(monkeypatch):
    """Пълният път: при зает катинар `_attempt_sync` НЕ качва нищо, а
    пренасрочва с НАРАСТВАЩА (не фиксирана) делта."""
    delays = []
    monkeypatch.setattr(backup, "github_backup",
                        lambda *a, **k: pytest.fail("не бива да се качва при зает катинар"))
    monkeypatch.setattr(backup, "_reschedule_debounce",
                        lambda cfg, delay=None: delays.append(delay))

    backup._upload_lock.acquire()
    try:
        with backup._sync_lock:
            backup._sync_state["dirty"] = True
            backup._sync_state["busy_delay"] = 0
        backup._attempt_sync(lambda: _cfg())
        backup._attempt_sync(lambda: _cfg())
    finally:
        backup._upload_lock.release()

    assert delays == [backup.DEBOUNCE_SECONDS, backup.DEBOUNCE_SECONDS * 2], delays


def test_still_dirty_reschedule_after_a_successful_upload_stays_immediate(monkeypatch):
    """Пазач за находки №11/№17/№49: промяна, дошла ПО ВРЕМЕ на успешно
    качване, трябва да се пренасрочи НЕЗАБАВНО (обичайния дебаунс), а не с
    нарасналата „заето“ делта."""
    delays = []

    def fake_backup(*args, **kwargs):
        with backup._sync_lock:
            backup._sync_state["dirty_gen"] += 1

    monkeypatch.setattr(backup, "github_backup", fake_backup)
    monkeypatch.setattr(backup, "_reschedule_debounce",
                        lambda cfg, delay=None: delays.append(delay))

    with backup._sync_lock:
        backup._sync_state["dirty"] = True
        backup._sync_state["dirty_gen"] = 1

    backup._attempt_sync(lambda: _cfg())

    with backup._sync_lock:
        assert backup._sync_state["dirty"] is True   # находка №11 — не е загубена
    assert delays == [None], "находка №17/№49: пренасрочване с нормалния дебаунс"


def test_sync_status_does_not_leak_the_internal_backoff():
    """`sync_status()` се показва в „Настройки“ — вътрешната механика няма
    работа там (както dirty_gen/таймерите)."""
    status = backup.sync_status()
    for hidden in ("busy_delay", "dirty_gen", "debounce_timer", "retry_timer"):
        assert hidden not in status


# =================================================================== №11
# Преводен низ в innerHTML.

def _app_js():
    with open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8") as fh:
        return fh.read()


def test_invoice_totals_no_longer_writes_translations_into_innerhtml():
    """Находка №11: `box.innerHTML = parts.join(" · ")`, където частите са
    `t("summary_rows", …) + ": <b>" + …`. Числата са безопасни, но самите
    ПРЕВОДИ влизаха като HTML — бъдещ турски превод със „<“ би счупил
    изгледа мълчаливо."""
    source = _app_js()
    # Само истински код — коментарът над поправката цитира стария ред.
    code = [line for line in source.splitlines()
            if not line.lstrip().startswith(("//", "*", "/*"))]
    assert not [line for line in code if re.search(r"box\.innerHTML\s*=", line)]
    assert 'box.textContent = ""' in source


def test_no_translated_string_is_concatenated_into_any_innerhtml():
    """Обобщената проверка (за да не се върне дефектът другаде): нито един
    ред, който присвоява `innerHTML`, не бива да съдържа извикване на
    t()/tf() — единственият такъв ред в целия файл беше този от находката."""
    offenders = [line.strip() for line in _app_js().splitlines()
                 if re.search(r"\.innerHTML\s*=", line) and re.search(r"\bt f?\(|\btf?\(", line)]
    assert not offenders, offenders


def test_totals_are_still_built_with_bold_value_elements():
    """Форматът на изхода не се променя (e2e тестът чете
    „Общо нето тегло: <b>6…“ от innerHTML) — само начинът на изграждане."""
    source = _app_js()
    assert 'document.createElement("b")' in source
    assert 'strong.textContent = part[1]' in source
