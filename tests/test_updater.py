# -*- coding: utf-8 -*-
"""Тестове за сравнението на версии в updater.parse_version.

Тази логика решава дали да се предложи автоматично обновяване, затова
коректното семантично сравнение е критично (напр. 3.10.0 > 3.9.0).
"""
import io
import os

import pytest

import updater


def test_parses_simple_version():
    assert updater.parse_version("3.10.0") == (3, 10, 0)


def test_strips_v_prefix():
    assert updater.parse_version("v3.10.0") == (3, 10, 0)
    assert updater.parse_version("V2.1.3") == (2, 1, 3)


def test_numeric_not_lexical_ordering():
    # Класически капан: като низове "3.9.0" > "3.10.0", но семантично е обратното.
    assert updater.parse_version("3.10.0") > updater.parse_version("3.9.0")
    assert (updater.parse_version("3.2.0") > updater.parse_version("3.10.0")) is False


def test_invalid_version_is_lowest():
    assert updater.parse_version("не-е-версия") == (0,)
    assert updater.parse_version(None) == (0,)


def test_equal_versions_not_greater():
    assert (updater.parse_version("3.10.0") > updater.parse_version("3.10.0")) is False


def test_patch_bump_detected():
    assert updater.parse_version("1.0.1") > updater.parse_version("1.0.0")


# ---------------------------------------------------------------- H3: контролни суми

_HASH_A = "ab" * 32  # валиден на вид (64 hex символа) примерен SHA-256 hex
_HASH_D = "d2" * 32


def test_parse_sha256sums_finds_matching_file():
    text = (
        "%s  PachoLogistic-Setup.exe\n"
        "%s  PachoLogistic.exe\n" % (_HASH_D, _HASH_A)
    )
    assert updater.parse_sha256sums(text, "PachoLogistic.exe") == _HASH_A


def test_parse_sha256sums_missing_file_returns_none():
    text = "%s  Other.exe\n" % _HASH_A
    assert updater.parse_sha256sums(text, "PachoLogistic.exe") is None


def test_parse_sha256sums_ignores_blank_lines_and_comments():
    text = "# generated manifest\n\n%s  PachoLogistic.exe\n" % _HASH_A
    assert updater.parse_sha256sums(text, "PachoLogistic.exe") == _HASH_A


def test_parse_sha256sums_rejects_malformed_hash():
    text = "not-a-valid-hash  PachoLogistic.exe\n"
    assert updater.parse_sha256sums(text, "PachoLogistic.exe") is None


def test_parse_sha256sums_handles_star_prefix():
    # Някои sha256sum варианти маркират бинарен режим с "*" пред името.
    text = "%s *PachoLogistic.exe\n" % _HASH_A
    assert updater.parse_sha256sums(text, "PachoLogistic.exe") == _HASH_A


def test_sha256_of_file_matches_hashlib(tmp_path):
    import hashlib
    p = tmp_path / "sample.bin"
    p.write_bytes(b"MZ" + b"0" * 1000)
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert updater.sha256_of_file(str(p)) == expected


class _FakeResp:
    """Минимален заместител на urlopen() резултат — за разлика от простото
    връщане на цялото съдържание на всяко read(), тук ИЗЧЕРПВАМЕ буфер
    (io.BytesIO), точно както прави истински HTTP отговор. Без това,
    shutil.copyfileobj() (който вика read(n) на цикъл, докато не получи
    празен резултат) никога не вижда празен низ и презаписва едно и също
    съдържание безкрайно — запълвайки диска, вместо просто да гръмне тест."""
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        return self._buf.read(n) if n is not None and n >= 0 else self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_install_update_rejects_checksum_mismatch(tmp_path, monkeypatch):
    """install_update отхвърля файл, чиято контролна сума не съвпада с
    очакваната от SHA256SUMS.txt, дори ако размерът и MZ байтовете са наред
    (симулира подменен/повреден файл, но все пак валиден Windows binary)."""
    fake_exe = tmp_path / "PachoLogistic.exe"
    monkeypatch.setattr(updater.sys, "executable", str(fake_exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)

    payload = b"MZ" + b"\x00" * 1_100_000  # над 1MB, за да мине size проверката
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _FakeResp(payload))

    wrong_hash = "0" * 64
    try:
        with pytest.raises(RuntimeError, match="контролната сума"):
            updater.install_update("http://example.invalid/x.exe", expected_sha256=wrong_hash)
    finally:
        new_exe = str(fake_exe) + ".new"
        if os.path.exists(new_exe):
            os.remove(new_exe)


def test_install_update_accepts_matching_checksum(tmp_path, monkeypatch):
    import hashlib
    fake_exe = tmp_path / "PachoLogistic.exe"
    monkeypatch.setattr(updater.sys, "executable", str(fake_exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    # Изолираме страничните ефекти отвъд проверката, която тестваме тук —
    # не искаме тестът реално да пише .bat файлове или да спира процеса.
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(updater.threading, "Timer", lambda *a, **k: type(
        "T", (), {"start": lambda self: None})())

    payload = b"MZ" + b"\x00" * 1_100_000
    expected_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _FakeResp(payload))

    updater.install_update("http://example.invalid/x.exe", expected_sha256=expected_hash)
    # install_update приключва без RuntimeError -> проверката на контролната
    # сума е минала успешно (файлът е приет и обработен по-нататък).
