# -*- coding: utf-8 -*-
"""Регресионни тестове за desktop.py (стартиране като настолно Windows
приложение) — заявка: „направи всичките“ (одит 11.08.2026, ERP_ОДИТ.md,
Дребни находки)."""
import os
import sys

import desktop


def test_run_native_window_waits_for_server_even_when_pywebview_is_missing(monkeypatch):
    """Одит (Дребни): run_native_window() връщаше False ВЕДНАГА при
    ImportError (pywebview липсва), БЕЗ изобщо да извика wait_for_server()
    — извикващият код (app.py) веднага пада към отваряне на браузър
    прозорец към сървър, който може все още да не приема връзки (стартира
    в паралелна фонова нишка). wait_for_server() трябва да се извика
    БЕЗУСЛОВНО, дори когато pywebview не е инсталиран."""
    calls = []
    monkeypatch.setattr(desktop, "wait_for_server", lambda url, timeout=15: calls.append(url) or True)

    # „pywebview липсва“ трябва да е ГАРАНТИРАНО на всяка платформа, не
    # разчитано на средата: sys.modules["webview"] = None кара
    # `import webview` да гърми с ImportError детерминирано. На Linux CI
    # пакетът бездруго липсва (маркер sys_platform=="win32" в
    # requirements.txt), но на Windows release runner-а (pytest порталът в
    # release.yml, одит 16.08 находка №28) той Е инсталиран — без този ред
    # тестът там реално отваряше WebView2 прозорец и webview.start()
    # (главният цикъл на GUI-то) блокираше завинаги, окачвайки целия билд
    # (Build and Release run #79 за v3.63.0 виси >1 час именно така).
    monkeypatch.setitem(sys.modules, "webview", None)
    result = desktop.run_native_window("http://127.0.0.1:5000")

    assert result is False, "без pywebview функцията трябва да върне False, за да падне извикващият код към резервния вариант"
    assert calls == ["http://127.0.0.1:5000"], (
        "wait_for_server() не е извикана ПРЕДИ връщането — извикващият код би отворил "
        "браузър прозорец към сървър, който може още да не е готов"
    )


def test_wait_for_server_returns_true_once_the_server_answers():
    """Проверка по конструкция на самата wait_for_server: истински HTTP
    сървър, вдигнат във фонова нишка, трябва да бъде открит в рамките на
    timeout-а."""
    import http.server
    import socketserver
    import threading
    import time

    with socketserver.TCPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.time()
            ok = desktop.wait_for_server("http://127.0.0.1:%d" % port, timeout=5)
            assert ok is True
            assert time.time() - started < 5
        finally:
            httpd.shutdown()
            thread.join(timeout=2)


def test_wait_for_server_times_out_when_nothing_is_listening():
    """Безопасност по конструкция: порт, на който НИЩО не слуша, трябва да
    доведе до False след timeout-а, не безкрайно чакане."""
    ok = desktop.wait_for_server("http://127.0.0.1:1", timeout=0.5)
    assert ok is False


# ---------------------------------------------------------------- находка №32: ниво 2 (Chrome/Edge --app fallback)
# Одит (12.08.2026, находка №32): _find_windows_browser()/open_app_window()
# (ниво 2 от 3-степенната стратегия — виж модулния докстринг) нямаха НИКАКЪВ
# тест — ако този fallback се счупи (грешен --app= синтаксис, грешен
# profile_dir), десктоп потребител без pywebview/WebView2 би паднал направо
# до ниво 3 (обикновен браузър таб) без предупреждение — регресия, която CI
# не би хванал.

def test_find_windows_browser_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(desktop.os, "environ", {}, raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop._find_windows_browser() is None


def test_find_windows_browser_finds_chrome_via_program_files(monkeypatch, tmp_path):
    # _find_windows_browser() гради пътя с ЛИТЕРАЛНИ Windows-стилни
    # обратни наклонени черти (r"Google\Chrome\Application\chrome.exe") —
    # на Linux os.path.join НЕ ги третира като разделители на директории
    # (POSIX разделителят е "/"), затова резултатът е ЕДИН файл с обратни
    # наклонени черти В ИМЕТО, не вложени папки. Тестът пресъздава ТОЧНО
    # това, вместо реални вложени директории, за да съответства на
    # реалното поведение на кода на тази платформа.
    expected_path = os.path.join(str(tmp_path), r"Google\Chrome\Application\chrome.exe")
    with open(expected_path, "wb") as f:
        f.write(b"fake-exe")
    monkeypatch.setattr(desktop.os, "environ", {"PROGRAMFILES": str(tmp_path)}, raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    found = desktop._find_windows_browser()
    assert found == expected_path


def test_find_windows_browser_falls_back_to_path_lookup(monkeypatch):
    monkeypatch.setattr(desktop.os, "environ", {}, raising=False)
    monkeypatch.setattr(desktop.shutil, "which",
                        lambda name: "/usr/bin/msedge" if name == "msedge" else None)
    assert desktop._find_windows_browser() == "/usr/bin/msedge"


def test_open_app_window_returns_false_on_non_windows(monkeypatch):
    """os.name != "nt": ранен изход — вижте docstring на open_app_window."""
    monkeypatch.setattr(desktop.os, "name", "posix")
    assert desktop.open_app_window("http://127.0.0.1:5000") is False


def test_open_app_window_returns_false_when_no_browser_found(monkeypatch):
    monkeypatch.setattr(desktop.os, "name", "nt")
    monkeypatch.setattr(desktop, "_find_windows_browser", lambda: None)
    assert desktop.open_app_window("http://127.0.0.1:5000") is False


def test_open_app_window_launches_browser_with_expected_args(monkeypatch, tmp_path):
    """Одит (находка №32): проверява ТОЧНИТЕ аргументи, подадени на
    subprocess.Popen — грешен --app=/--window-size синтаксис би минал
    незабелязано без този тест (браузърът просто не отваря очаквания
    прозорец, трудно забележимо в реална употреба)."""
    monkeypatch.setattr(desktop.os, "name", "nt")
    monkeypatch.setattr(desktop, "_find_windows_browser", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(desktop.os, "environ", {"LOCALAPPDATA": str(tmp_path)}, raising=False)

    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(desktop.subprocess, "Popen", _FakePopen)

    result = desktop.open_app_window("http://127.0.0.1:5000", width=1200, height=800)

    assert result is True
    args = captured["args"]
    assert args[0] == r"C:\Chrome\chrome.exe"
    assert "--app=http://127.0.0.1:5000" in args
    assert "--window-size=1200,800" in args
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert captured["kwargs"].get("close_fds") is True


def test_open_app_window_returns_false_when_popen_raises_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.os, "name", "nt")
    monkeypatch.setattr(desktop, "_find_windows_browser", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(desktop.os, "environ", {"LOCALAPPDATA": str(tmp_path)}, raising=False)

    def _raise(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(desktop.subprocess, "Popen", _raise)
    assert desktop.open_app_window("http://127.0.0.1:5000") is False
