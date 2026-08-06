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
