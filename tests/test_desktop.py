# -*- coding: utf-8 -*-
"""Регресионни тестове за desktop.py (стартиране като настолно Windows
приложение) — заявка: „направи всичките“ (одит 11.08.2026, ERP_ОДИТ.md,
Дребни находки)."""
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

    # pywebview не е инсталиран в тестовата среда (истински ModuleNotFoundError,
    # не мокнат) — точно сценарият от одита.
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
