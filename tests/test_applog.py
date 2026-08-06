# -*- coding: utf-8 -*-
"""Тестове за applog.py (находка L1 — замяна на тихите
`except Exception: pass` с минимално структурирано логване, виж
ПЛАН_ЗА_РАЗРАБОТКА.md, допълнителен поправъчен цикъл след Фаза 5).

Проверяваме самия helper, плюс представителна извадка от местата, където
вече се вика — за да не се „изроди" тихо обратно в `pass` при бъдеща
промяна, без да се усети от тестовете."""
import backup


def test_log_exception_writes_context_and_traceback(capsys):
    import applog

    try:
        raise ValueError("тестова грешка")
    except ValueError:
        applog.log_exception("test-context")

    out = capsys.readouterr().out
    assert "test-context" in out
    assert "ValueError" in out
    assert "тестова грешка" in out


def test_log_exception_never_raises_without_active_exception(capsys):
    import applog

    applog.log_exception("no-active-exception")  # не трябва да гърми
    out = capsys.readouterr().out
    assert "no-active-exception" in out


def test_log_warning_writes_context_and_message(capsys):
    import applog

    applog.log_warning("ctx", "нещо си струва да се знае")
    out = capsys.readouterr().out
    assert "ctx" in out
    assert "нещо си струва да се знае" in out


def test_mark_dirty_logs_when_config_load_fails(capsys):
    def broken_config():
        raise RuntimeError("конфигурацията е повредена")

    backup.mark_dirty(broken_config)  # не трябва да хвърли изключение навън

    out = capsys.readouterr().out
    assert "backup.mark_dirty" in out
    assert "RuntimeError" in out


def test_trigger_sync_now_logs_when_config_load_fails(capsys):
    def broken_config():
        raise RuntimeError("конфигурацията е повредена")

    backup.trigger_sync_now(broken_config)  # не трябва да хвърли изключение навън

    out = capsys.readouterr().out
    assert "backup.trigger_sync_now" in out
