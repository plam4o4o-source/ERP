# -*- coding: utf-8 -*-
"""Регресионни тестове за одита от 16.08.2026 (ERP_ОДИТ_2026_08_16.md) —
находка №46: липсващо тестово покритие за няколко реални, нетривиални
поведения, пипнати/добавени в тази поправка: dirty-flag lost-update
защитата и single-flight заключването в backup.py, автоматичния локален
архив (start_auto_backup), certifi резервния SSL опит в net.py, и
показването на страница „база данни недостъпна“ вместо гол 500/безкраен
redirect (appcore._is_db_unavailable_error). Плюс находка №1 (единствената
критична в този одит) — рестарт при обновяване вече спира отдалечения
тунел, но също нямаше нито един регресионен тест.

Стил: директни извиквания на функциите (без нужда от Flask клиент за
повечето), както в tests/test_applog.py — с monkeypatch/capsys вместо
пълен end-to-end сценарий, където е практично."""
import io
import sqlite3
import threading
import time

import pytest

import appcore
import backup
import remote_tunnel
import updater
import net


# Бележка (25.08.2026): секцията за backup.mark_dirty / _attempt_sync /
# trigger_sync_now (автоматичната GitHub синхронизация) отпадна заедно с
# премахнатата функция. Локалният архив (start_auto_backup по-долу) е
# отделен и остава напълно покрит.

# --------------------------------------------------------------------- #
# backup.start_auto_backup — насрочва фонов таймер, без да чакаме реален
# интервал (60 мин/секунди) в теста
# --------------------------------------------------------------------- #

def test_start_auto_backup_schedules_a_timer_without_running_it(monkeypatch):
    created_timers = []
    real_timer = threading.Timer

    class RecordingTimer(real_timer):
        def __init__(self, interval, function, args=None, kwargs=None):
            super().__init__(interval, function, args=args, kwargs=kwargs)
            created_timers.append((interval, self))

        def start(self):
            # НЕ стартираме реалния таймер (не искаме той да "гръмне" по
            # време на тестовия процес) — само проверяваме, че е бил
            # правилно конструиран и подаден на _auto_thread.
            pass

    monkeypatch.setattr(threading, "Timer", RecordingTimer)

    backup.start_auto_backup(lambda: {"backup_folder": "", "backup_auto": False})

    assert len(created_timers) == 1
    interval, timer_obj = created_timers[0]
    assert interval == 60  # първи опит — минута след стартиране
    assert backup._auto_thread["timer"] is timer_obj


def test_start_auto_backup_tick_skips_when_no_folder_configured(monkeypatch):
    """_tick() (вътрешната функция) не трябва да гърми и не трябва да
    вика local_backup, ако няма зададена папка/изключен е auto бекъп —
    проверяваме индиректно, като хващаме local_backup да не е викнат."""
    calls = []
    monkeypatch.setattr(backup, "local_backup", lambda folder: calls.append(folder))

    captured = {}
    real_timer = threading.Timer

    class CapturingTimer(real_timer):
        def __init__(self, interval, function, args=None, kwargs=None):
            captured["function"] = function
            super().__init__(interval, function, args=args, kwargs=kwargs)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", CapturingTimer)

    backup.start_auto_backup(lambda: {"backup_folder": "", "backup_auto": True})
    tick_fn = captured["function"]

    # Изпълняваме _tick() директно (синхронно) — вътре пак ще се опита да
    # насрочи СЛЕДВАЩ таймер чрез вече monkeypatch-натия Timer, който пак
    # не стартира реално.
    tick_fn()

    assert calls == []


def test_start_auto_backup_tick_runs_local_backup_when_folder_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(backup, "local_backup", lambda folder: calls.append(folder))

    captured = {}
    real_timer = threading.Timer

    class CapturingTimer(real_timer):
        def __init__(self, interval, function, args=None, kwargs=None):
            captured.setdefault("function", function)
            super().__init__(interval, function, args=args, kwargs=kwargs)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", CapturingTimer)

    backup.start_auto_backup(
        lambda: {"backup_folder": "/tmp/some-backup-dir", "backup_auto": True})
    captured["function"]()

    assert calls == ["/tmp/some-backup-dir"]


def test_start_auto_backup_tick_logs_and_reschedules_on_error(monkeypatch, capsys):
    def boom(folder):
        raise OSError("диска е недостъпен")

    monkeypatch.setattr(backup, "local_backup", boom)

    captured = {}
    real_timer = threading.Timer

    class CapturingTimer(real_timer):
        def __init__(self, interval, function, args=None, kwargs=None):
            captured.setdefault("function", function)
            super().__init__(interval, function, args=args, kwargs=kwargs)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", CapturingTimer)

    backup.start_auto_backup(
        lambda: {"backup_folder": "/tmp/some-backup-dir", "backup_auto": True})
    captured["function"]()  # не трябва да хвърли изключение навън

    out = capsys.readouterr().out
    assert "backup._tick" in out
    assert "OSError" in out


# --------------------------------------------------------------------- #
# net.urlopen — резервен опит през certifi при SSL грешка
# --------------------------------------------------------------------- #

def test_urlopen_returns_normally_when_first_attempt_succeeds(monkeypatch):
    sentinel = object()
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(context)
        return sentinel

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)

    result = net.urlopen("fake-request", timeout=5)

    assert result is sentinel
    assert calls == [None]  # само един опит, без context/резервен опит


def test_urlopen_falls_back_to_certifi_context_on_ssl_error(monkeypatch):
    import ssl
    import urllib.error

    sentinel = object()
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(context)
        if context is None:
            raise net.urllib.error.URLError(ssl.SSLError("certificate verify failed"))
        return sentinel

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(net, "_FALLBACK_CONTEXT", "fake-certifi-context")

    result = net.urlopen("fake-request", timeout=5)

    assert result is sentinel
    assert calls == [None, "fake-certifi-context"]


def test_urlopen_reraises_ssl_error_when_no_fallback_context_available(monkeypatch):
    import ssl

    def fake_urlopen(request, timeout=None, context=None):
        raise net.urllib.error.URLError(ssl.SSLError("certificate verify failed"))

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(net, "_FALLBACK_CONTEXT", None)  # напр. certifi липсва изобщо

    with pytest.raises(net.urllib.error.URLError):
        net.urlopen("fake-request", timeout=5)


def test_urlopen_reraises_non_ssl_url_errors_without_fallback_attempt(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(context)
        raise net.urllib.error.URLError("connection refused")  # НЕ SSL грешка

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(net, "_FALLBACK_CONTEXT", "fake-certifi-context")

    with pytest.raises(net.urllib.error.URLError):
        net.urlopen("fake-request", timeout=5)

    assert calls == [None]  # без втори опит — не беше SSL грешка


# --------------------------------------------------------------------- #
# appcore._is_db_unavailable_error / страница „база данни недостъпна“
# (находка №9)
# --------------------------------------------------------------------- #

def test_is_db_unavailable_error_true_for_missing_network_drive():
    exc = RuntimeError("мрежовият диск не е достъпен в момента")
    assert appcore._is_db_unavailable_error(exc) is True


def test_is_db_unavailable_error_true_for_unable_to_open_database_file():
    exc = sqlite3.OperationalError("unable to open database file")
    assert appcore._is_db_unavailable_error(exc) is True


def test_is_db_unavailable_error_true_for_disk_io_error():
    exc = sqlite3.OperationalError("disk I/O error")
    assert appcore._is_db_unavailable_error(exc) is True


def test_is_db_unavailable_error_false_for_transient_locked_error():
    """"database is locked" е ВРЕМЕННО състояние (различен клон в
    _handle_unexpected_error — flash + redirect, не самостоятелна
    страница) — не бива да се класифицира като трайна недостъпност,
    иначе потребителят вижда грешна, по-тревожна страница за нещо, което
    следващият опит съвсем скоро вероятно ще оправи."""
    exc = sqlite3.OperationalError("database is locked")
    assert appcore._is_db_unavailable_error(exc) is False


def test_is_db_unavailable_error_false_for_unrelated_exception():
    assert appcore._is_db_unavailable_error(ValueError("нещо съвсем друго")) is False


def test_db_unavailable_error_renders_dedicated_page_with_503(admin_client, monkeypatch):
    """Пълен end-to-end сценарий: заявка, при която обработката хвърля
    RuntimeError с текста за недостъпен мрежов диск, трябва да покаже
    db_unavailable.html със статус 503 (БЕЗ redirect — виж коментара в
    appcore._handle_unexpected_error за защо безкраен redirect цикъл беше
    реалният бъг преди тази поправка), вместо гол 500 Internal Server
    Error от Werkzeug."""
    import routes_dashboard

    def boom_get_db():
        raise RuntimeError("мрежовият диск не е достъпен в момента")

    # routes_dashboard прави `from appcore import get_db` — патчваме
    # собствената му вече обвързана препратка, не appcore.get_db (патч
    # там не би имал ефект, защото модулът вече държи собствено име,
    # сочещо към оригиналната функция, от момента на import-а).
    monkeypatch.setattr(routes_dashboard, "get_db", boom_get_db)

    resp = admin_client.get("/")

    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert "мрежовият диск" in body or "недостъпна" in body.lower()


# --------------------------------------------------------------------- #
# находка №1 (единствената критична в одита от 16.08.2026): рестартът
# след обновяване (ръчно и автоматично) трябва да спре отдалечения тунел
# ПРЕДИ os._exit(0) — иначе cloudflared остава „сирак“, точно както
# поправената критична находка №2 от одита на 12.08.2026.
# --------------------------------------------------------------------- #

class _FakeUpdateResp:
    """Минимален заместител на urlopen() резултат — виж _FakeResp в
    tests/test_updater.py за пълното обяснение защо е нужен истински
    буфер (io.BytesIO), не просто връщане на цялото съдържание наведнъж."""
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        return self._buf.read(n) if n is not None and n >= 0 else self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_install_update_stops_remote_tunnel_before_exiting(tmp_path, monkeypatch):
    """install_update() насрочва рестарта чрез threading.Timer(1.5, ...) —
    улавяме подадената функция БЕЗ да чакаме реалните 1.5 сек и БЕЗ
    таймерът да стартира истински (start() е no-op), после я извикваме
    директно в теста, с monkeypatch-нат os._exit (за да не убие реално
    тестовия процес) — проверяваме, че remote_tunnel.stop() е бил
    извикан ПРЕДИ os._exit(), не пропуснат."""
    import hashlib

    fake_exe = tmp_path / "PachoLogistic.exe"
    monkeypatch.setattr(updater.sys, "executable", str(fake_exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: None)

    captured = {}

    class CapturingTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            captured["interval"] = interval
            captured["function"] = function

        def start(self):
            pass  # НЕ чакаме реалните 1.5 сек в теста

    monkeypatch.setattr(updater.threading, "Timer", CapturingTimer)

    calls = []
    monkeypatch.setattr(updater.remote_tunnel, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(updater.os, "_exit", lambda code: calls.append(("_exit", code)))

    payload = b"MZ" + b"\x00" * 1_100_000
    expected_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater.net, "urlopen",
                         lambda req, timeout=120: _FakeUpdateResp(payload))

    updater.install_update("http://example.invalid/x.exe", expected_sha256=expected_hash)

    assert captured["interval"] == 1.5
    captured["function"]()  # директно извикване на _exit_and_stop_tunnel

    # ГЛАВНАТА проверка на находка №1: stop() ПРЕДИ _exit(), в този ред,
    # не пропуснат и не разменен местата.
    assert calls == ["stop", ("_exit", 0)]


def test_app_registers_remote_tunnel_stop_at_exit():
    """Одит (находка №1): нормалният изход/Ctrl+C минава през atexit, не
    през updater.py-я рестартов път по-горе — app.py трябва изрично да
    регистрира remote_tunnel.stop() там (виж app.py, коментар до
    `atexit.register`). Тук само проверяваме статично, ЧЕ регистрацията
    реално стои (АКТИВНА, не закоментирана) в изходния код на app.py (без
    да импортираме/изпълняваме целия app.py модул тук, което би дръпнало
    твърде много странични ефекти — стартиране на Flask/waitress — за
    unit тест). Проверката е по РЕД (не просто substring в целия файл),
    за да не мине, ако редът бъде случайно закоментиран — коментар,
    съдържащ същия низ, все пак би съдържал substring-а."""
    import os as os_module

    app_py_path = os_module.path.join(
        os_module.path.dirname(os_module.path.dirname(os_module.path.abspath(__file__))),
        "app.py")
    with open(app_py_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    matching = [ln for ln in lines
                if ln.strip() == "atexit.register(remote_tunnel.stop)"]
    assert matching, ("app.py трябва да съдържа АКТИВЕН (не закоментиран) "
                     "ред `atexit.register(remote_tunnel.stop)`")


def test_remote_tunnel_stop_during_starting_phase_prevents_orphaned_process(monkeypatch):
    """Находка №12 (свързана с №1): stop(), извикан ДОКАТО start() е още
    в ensure_binary() фазата (до 60 сек при първо изтегляне на
    cloudflared), не бива тихо да се игнорира. start() спавва СОБСТВЕНА
    фонова нишка (_run) и връща управлението веднага — тук извикваме
    start() директно (не в допълнителна нишка), изчакваме малко, докато
    сме сигурни, че _run() е все още вътре в (бавния, фалшив)
    ensure_binary(), извикваме stop() и накрая проверяваме, че cloudflared
    изобщо НЕ е бил стартиран (Popen никога не се вика — kодът проверява
    поколението веднага след ensure_binary() и излиза, преди Popen)."""
    original_generation = remote_tunnel._state["generation"]
    original_process = remote_tunnel._state["process"]
    original_status = remote_tunnel._state["status"]
    try:
        popen_calls = []
        ensure_binary_started = threading.Event()

        def fake_ensure_binary():
            # Симулира бавното първо изтегляне — по време на него тестът
            # ще извика stop(), точно както описва находка №12.
            ensure_binary_started.set()
            time.sleep(0.1)
            return "/fake/cloudflared"

        class FakePopen:
            def __init__(self, *a, **k):
                popen_calls.append(1)
                self.stdout = io.BytesIO(b"")
                self.pid = 12345

            def poll(self):
                return None

        monkeypatch.setattr(remote_tunnel, "ensure_binary", fake_ensure_binary)
        monkeypatch.setattr(remote_tunnel.subprocess, "Popen", FakePopen)

        remote_tunnel.start(5000)
        assert ensure_binary_started.wait(timeout=2), (
            "ensure_binary() не стартира навреме — тестът не може да "
            "провери сценария от находка №12")
        remote_tunnel.stop()
        time.sleep(0.3)  # изчакваме fake_ensure_binary() (0.1с) + _run() да приключи

        # Основната проверка на находка №12: cloudflared НИКОГА не е бил
        # реално стартиран (Popen), а _state е чист — не остава "сирак"
        # процес, регистриран след като вече сме поискали "спри".
        assert popen_calls == []
        assert remote_tunnel._state["process"] is None
    finally:
        remote_tunnel._state["generation"] = original_generation
        remote_tunnel._state["process"] = original_process
        remote_tunnel._state["status"] = original_status
