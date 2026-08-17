# -*- coding: utf-8 -*-
"""Тестове за remote_tunnel.ensure_binary() — находка L5 (проверка на
изтегления `cloudflared` изпълним файл).

Cloudflare не публикува контролна сума за `cloudflared` (виж коментара в
remote_tunnel._expected_magic() за подробности/източник), затова тук се
проверява само защитата срещу прекъснато/повредено изтегляне — точен
размер (Content-Length) + магически байтове на изпълним файл — по
аналогия с проверката за собствения ни .exe в updater.py (H3)."""
import os

import pytest

import remote_tunnel


class _FakeResponse:
    """Минимален заместител на http.client.HTTPResponse — поддържа
    context manager + четене на парчета + .headers.get(...), колкото
    ensure_binary() реално ползва."""

    def __init__(self, data, content_length=None):
        self._data = data
        self._pos = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n] if n and n > 0 else self._data[self._pos:]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def isolated_binary_path(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "bin", "cloudflared")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    monkeypatch.setattr(remote_tunnel, "_binary_path", lambda: path)
    return path


def _valid_elf_payload(size=200000):
    return b"\x7fELF" + b"\x00" * (size - 4)


def test_ensure_binary_accepts_valid_download(isolated_binary_path, monkeypatch):
    payload = _valid_elf_payload()
    monkeypatch.setattr(remote_tunnel.net, "urlopen",
                        lambda req, timeout=60: _FakeResponse(payload, content_length=len(payload)))

    path = remote_tunnel.ensure_binary()

    assert path == isolated_binary_path
    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read(4) == b"\x7fELF"


def test_ensure_binary_rejects_truncated_download(isolated_binary_path, monkeypatch, capsys):
    payload = _valid_elf_payload(size=200000)
    # Content-Length казва 300000, но реално получаваме само 200000 —
    # симулира прекъсната връзка по средата на преноса.
    monkeypatch.setattr(remote_tunnel.net, "urlopen",
                        lambda req, timeout=60: _FakeResponse(payload, content_length=300000))

    with pytest.raises(RuntimeError, match="непълно изтегляне"):
        remote_tunnel.ensure_binary()

    assert not os.path.exists(isolated_binary_path)
    assert not os.path.exists(isolated_binary_path + ".download")
    assert "remote_tunnel.ensure_binary" in capsys.readouterr().out


def test_ensure_binary_rejects_wrong_magic_bytes(isolated_binary_path, monkeypatch):
    # Симулира сървър, върнал HTML грешка (или друг неочакван отговор)
    # вместо реалния изпълним файл, но с достатъчно голям размер да мине
    # старата (само size>100000) проверка.
    payload = b"<html>Error page</html>" + b" " * 200000
    monkeypatch.setattr(remote_tunnel.net, "urlopen",
                        lambda req, timeout=60: _FakeResponse(payload, content_length=len(payload)))

    with pytest.raises(RuntimeError, match="не е валиден изпълним файл"):
        remote_tunnel.ensure_binary()

    assert not os.path.exists(isolated_binary_path)


def test_ensure_binary_rejects_too_small_file(isolated_binary_path, monkeypatch):
    payload = b"\x7fELF" + b"\x00" * 10  # много под 100000 байта
    monkeypatch.setattr(remote_tunnel.net, "urlopen",
                        lambda req, timeout=60: _FakeResponse(payload, content_length=len(payload)))

    with pytest.raises(RuntimeError, match="твърде малък"):
        remote_tunnel.ensure_binary()

    assert not os.path.exists(isolated_binary_path)


def test_ensure_binary_reuses_existing_cached_file(isolated_binary_path, monkeypatch):
    os.makedirs(os.path.dirname(isolated_binary_path), exist_ok=True)
    with open(isolated_binary_path, "wb") as f:
        f.write(b"\x7fELF" + b"\x00" * 200000)

    def _boom(req, timeout=60):
        raise AssertionError("не трябва да прави мрежова заявка, ако вече има кеширан файл")
    monkeypatch.setattr(remote_tunnel.net, "urlopen", _boom)

    path = remote_tunnel.ensure_binary()
    assert path == isolated_binary_path


# ---------------------------------------------------------------- В16: заклещено "error" състояние

class _FakeDeadProc:
    """Симулира cloudflared, който умира РАНО (напр. мрежов проблем),
    БЕЗ изобщо да отпечата публичния адрес — точно сценарият от находка
    В16."""

    def __init__(self, lines=("нещо тръгна на зле\n",)):
        self._lines = list(lines)
        self.stdout = self

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return ""

    def wait(self):
        return 1

    def terminate(self):
        pass


@pytest.fixture(autouse=True)
def _reset_remote_tunnel_state():
    """Модулно състояние (_state) трябва да е чисто преди/след всеки
    тест в този файл — иначе тестовете си пречат един на друг."""
    remote_tunnel._state.update(process=None, status="stopped", url=None, error=None)
    yield
    remote_tunnel._state.update(process=None, status="stopped", url=None, error=None)


def test_tunnel_dying_without_finding_url_resets_process_to_none():
    """Одит (находка В16, висок риск): преди поправката _consume_output
    оставяше _state["process"] сочещ към вече мъртвия процес, ако той
    приключеше БЕЗ да отпечата публичния адрес — статусът минаваше на
    "error", но process оставаше "зает". Тук директно викаме
    _consume_output с фалшив, рано умрял процес и проверяваме, че
    process СЕ нулира до None заедно със status="error"."""
    proc = _FakeDeadProc()
    with remote_tunnel._lock:
        remote_tunnel._state["process"] = proc
    remote_tunnel._consume_output(proc)

    status = remote_tunnel.status()
    assert status["status"] == "error"
    assert remote_tunnel._state["process"] is None


def test_start_is_not_a_silent_no_op_after_a_failed_tunnel(monkeypatch):
    """Пълният регресионен сценарий: след неуспешен тунел (умрял рано),
    следващо натискане на „Стартирай“ НЕ бива тихо да не прави нищо —
    трябва реално да пусне нов опит (нов subprocess.Popen), а не да се
    връща рано заради stale _state["process"]."""
    monkeypatch.setattr(remote_tunnel, "ensure_binary", lambda: "/fake/cloudflared")
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(args)
        return _FakeDeadProc()

    monkeypatch.setattr(remote_tunnel.subprocess, "Popen", fake_popen)

    remote_tunnel.start(8000)
    for _ in range(100):
        if remote_tunnel.status()["status"] == "error":
            break
        __import__("time").sleep(0.02)
    assert remote_tunnel.status()["status"] == "error"
    assert len(popen_calls) == 1

    # Второто "Стартирай" — преди поправката щеше тихо да е no-op тук,
    # защото _state["process"] още сочеше stale обекта.
    remote_tunnel.start(8000)
    for _ in range(100):
        if len(popen_calls) >= 2:
            break
        __import__("time").sleep(0.02)
    assert len(popen_calls) == 2, "второто натискане на „Стартирай“ не биваше да е тихо no-op"


# ---------------------------------------------------------------- находка №33: happy path на start()
# Одит (12.08.2026, находка №33): досегашните тестове покриваха добре
# граничните/regression сценарии (В16 — заклещено "error" състояние), но
# не и самия ОСНОВЕН поток на start() — спавване на нишка, стартиране на
# cloudflared, четене на URL от изхода до успешен "running" статус.

class _FakeRunningProc:
    """Симулира cloudflared, който УСПЕШНО отпечатва публичния адрес на
    изхода си и продължава да "работи" (wait() блокира, докато не бъде
    изрично спрян)."""

    def __init__(self, url_line="2026-08-12T10:00:00Z INF |  https://random-words-1234.trycloudflare.com  |\n"):
        self._lines = [url_line]
        self.stdout = self
        self._waited = __import__("threading").Event()
        self.terminated = False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        # След първия ред симулира "все още работи" — readline() блокира
        # (в реалния cloudflared процес) до terminate()/EOF; тук просто
        # изчакваме, докато тестът не спре тунела.
        self._waited.wait(timeout=5)
        return ""

    def wait(self):
        self._waited.wait(timeout=5)
        return 0

    def terminate(self):
        self.terminated = True
        self._waited.set()


def test_start_happy_path_reaches_running_status_with_parsed_url(monkeypatch):
    """Пълният щастлив път: ensure_binary() → subprocess.Popen() →
    _consume_output() открива URL-а в изхода → status() минава "running"
    с точния адрес, без грешка."""
    monkeypatch.setattr(remote_tunnel, "ensure_binary", lambda: "/fake/cloudflared")
    popen_calls = []
    fake_proc = _FakeRunningProc()

    def fake_popen(args, **kwargs):
        popen_calls.append(args)
        return fake_proc

    monkeypatch.setattr(remote_tunnel.subprocess, "Popen", fake_popen)

    remote_tunnel.start(8000)
    for _ in range(100):
        if remote_tunnel.status()["status"] == "running":
            break
        __import__("time").sleep(0.02)

    status = remote_tunnel.status()
    assert status["status"] == "running"
    assert status["url"] == "https://random-words-1234.trycloudflare.com"
    assert status["error"] is None
    # Правилните аргументи бяха подадени на Popen (порт/URL, без shell).
    assert popen_calls[0][0] == "/fake/cloudflared"
    assert "tunnel" in popen_calls[0]
    assert "http://127.0.0.1:8000" in popen_calls[0]

    remote_tunnel.stop()


def test_start_is_a_no_op_while_already_starting_or_running(monkeypatch):
    """start() не бива да спавва ВТОРИ паралелен опит, докато вече има
    активен/стартиращ тунел — вижте guard-а `if _state["process"] is not
    None or _state["status"] == "starting"` в самата start()."""
    monkeypatch.setattr(remote_tunnel, "ensure_binary", lambda: "/fake/cloudflared")
    popen_calls = []
    fake_proc = _FakeRunningProc()

    def fake_popen(args, **kwargs):
        popen_calls.append(args)
        return fake_proc

    monkeypatch.setattr(remote_tunnel.subprocess, "Popen", fake_popen)

    remote_tunnel.start(8000)
    for _ in range(100):
        if remote_tunnel.status()["status"] == "running":
            break
        __import__("time").sleep(0.02)
    assert len(popen_calls) == 1

    remote_tunnel.start(8000)  # вече "running" — трябва да е no-op
    assert len(popen_calls) == 1

    remote_tunnel.stop()
