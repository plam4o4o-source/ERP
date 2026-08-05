# -*- coding: utf-8 -*-
"""Тестове за login_guard (заключване след повторни неуспешни опити, H5)."""
import pytest

import login_guard


@pytest.fixture(autouse=True)
def _reset():
    login_guard.reset_all()
    yield
    login_guard.reset_all()


def test_not_locked_initially():
    locked, remaining = login_guard.is_locked_out("ivan")
    assert locked is False
    assert remaining == 0


def test_locks_after_max_attempts():
    for _ in range(login_guard.MAX_ATTEMPTS - 1):
        login_guard.register_failure("ivan")
    locked, _ = login_guard.is_locked_out("ivan")
    assert locked is False  # още не е стигнал лимита

    login_guard.register_failure("ivan")  # последният опит достига лимита
    locked, remaining = login_guard.is_locked_out("ivan")
    assert locked is True
    assert remaining > 0


def test_lockout_expires(monkeypatch):
    now = [1000.0]
    for _ in range(login_guard.MAX_ATTEMPTS):
        login_guard.register_failure("ivan", now=now[0])
    locked, _ = login_guard.is_locked_out("ivan", now=now[0])
    assert locked is True

    # След изтичане на LOCKOUT_SECONDS заключването отпада.
    later = now[0] + login_guard.LOCKOUT_SECONDS + 1
    locked, remaining = login_guard.is_locked_out("ivan", now=later)
    assert locked is False
    assert remaining == 0


def test_successful_clear_resets_counter():
    for _ in range(login_guard.MAX_ATTEMPTS - 1):
        login_guard.register_failure("ivan")
    login_guard.clear("ivan")
    locked, _ = login_guard.is_locked_out("ivan")
    assert locked is False
    # Броячът е нулиран — трябва отново пълния лимит от неуспехи, за да заключи.
    for _ in range(login_guard.MAX_ATTEMPTS - 1):
        login_guard.register_failure("ivan")
    locked, _ = login_guard.is_locked_out("ivan")
    assert locked is False


def test_usernames_are_independent():
    for _ in range(login_guard.MAX_ATTEMPTS):
        login_guard.register_failure("ivan")
    locked_ivan, _ = login_guard.is_locked_out("ivan")
    locked_petar, _ = login_guard.is_locked_out("petar")
    assert locked_ivan is True
    assert locked_petar is False


def test_username_normalization_case_insensitive():
    for _ in range(login_guard.MAX_ATTEMPTS):
        login_guard.register_failure("Admin")
    locked, _ = login_guard.is_locked_out("admin")
    assert locked is True


def test_old_attempts_outside_window_do_not_accumulate():
    now = 1000.0
    login_guard.register_failure("ivan", now=now)
    # Следващ неуспех идва много по-късно, извън WINDOW_SECONDS — брояча
    # трябва да се рестартира от 1, не да продължи натрупването.
    later = now + login_guard.WINDOW_SECONDS + 1
    login_guard.register_failure("ivan", now=later)
    entry = login_guard._attempts["ivan"]
    assert entry[0] == 1
