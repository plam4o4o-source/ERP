# -*- coding: utf-8 -*-
"""Тестове за сравнението на версии в updater.parse_version.

Тази логика решава дали да се предложи автоматично обновяване, затова
коректното семантично сравнение е критично (напр. 3.10.0 > 3.9.0).
"""
import io
import os
import threading
import time

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


# ---------------------------------------------------------------- находка M5: _cache под заключване

def test_set_cache_updates_all_fields_under_lock():
    info = {"available": True, "latest": "9.9.9", "current": "1.0.0"}
    updater.set_cache(info)
    assert updater._cache["info"] == info
    assert updater._cache["last_error"] is None
    assert updater._cache["time"] > 0


def test_check_cached_serializes_concurrent_callers(monkeypatch):
    """Находка M5: две "едновременни" зареждания на таблото не бива да
    пускат по отделна GitHub заявка, ако кешът вече е бил обновен от
    първата, докато втората е чакала заключването."""
    updater._cache["time"] = 0.0
    updater._cache["info"] = None
    calls = []

    def _fake_check():
        calls.append(1)
        return {"available": False, "latest": "1.0.0", "current": "1.0.0"}

    monkeypatch.setattr(updater, "check_for_update", _fake_check)
    # Първото извикване прави реалната (фалшива) проверка и пълни кеша.
    first = updater.check_cached(max_age=3600)
    # Второто, веднага след това, трябва да ползва вече пресния кеш —
    # без нова заявка (симулира втора заявка, пристигнала точно след
    # първата е освободила заключването).
    second = updater.check_cached(max_age=3600)
    assert first == second
    assert len(calls) == 1


def test_check_cached_lock_prevents_lost_update(monkeypatch):
    """При грешка в проверката _cache["info"] коректно се нулира и
    last_error се записва — под заключване, без прекъсване от паралелен
    set_cache() (симулиран тук последователно, тъй като истинска
    многонишковост е недетерминирана за unit тест)."""
    updater._cache["time"] = 0.0
    updater._cache["info"] = {"available": True, "latest": "5.0.0", "current": "1.0.0"}

    def _boom():
        raise RuntimeError("няма връзка")

    monkeypatch.setattr(updater, "check_for_update", _boom)
    result = updater.check_cached(max_age=0)  # 0 -> винаги "остарял", пробва пак
    assert result is None
    assert updater._cache["last_error"]


def test_update_check_route_uses_set_cache(admin_client, monkeypatch):
    """/update/check (routes_admin.update_check) вече минава през
    updater.set_cache(), не пряко updater._cache[...] = ... (М5)."""
    info = {"available": True, "latest": "99.0.0", "current": "1.0.0"}
    monkeypatch.setattr(updater, "check_for_update", lambda: info)
    resp = admin_client.get("/update/check", follow_redirects=True)
    assert resp.status_code == 200
    assert "99.0.0".encode() in resp.data
    assert updater._cache["info"] == info
    assert updater._cache["last_error"] is None


# ---------------------------------------------------------------- рестарт след обновяване
# Заявка: „Failed to load Python DLL ..._MEIxxxxxx\python312.dll — грешката
# продължава след всяко автоматично обновяване, но програмата се обновява“.
# Причината НЕ е повреден файл (той се проверява по размер/MZ/SHA-256 при
# изтеглянето), а наследената среда: PyInstaller onefile bootloader-ът
# подава на детето си служебни променливи (_MEIPASS2 / _PYI_*), новото .exe
# ги наследява през cmd.exe → start и решава, че е вече разопаковано в
# _MEIxxxxxx папката на СТАРИЯ процес — а тя е изтрита при неговия изход.

def test_env_scrubber_removes_pyinstaller_bootloader_vars():
    dirty = {
        "PATH": r"C:\Windows;C:\Windows\System32",
        "SYSTEMROOT": r"C:\Windows",
        "_MEIPASS2": r"C:\Users\plam4\AppData\Local\Temp\_MEI243922",
        "_PYI_APPLICATION_HOME_DIR": r"C:\Users\plam4\AppData\Local\Temp\_MEI243922",
        "_PYI_ARCHIVE_FILE": r"C:\Program Files\PachoLogistic\PachoLogistic.exe",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "_PYI_SPLASH_IPC": "123",  # бъдеща/друга _PYI_ променлива — също пада
    }
    clean = updater._env_without_pyinstaller_vars(dirty)
    assert "_MEIPASS2" not in clean
    assert not any(k.startswith("_PYI_") for k in clean)
    # Нормалните променливи ОСТАВАТ — cmd.exe/новият процес имат нужда от тях.
    assert clean["PATH"] == dirty["PATH"]
    assert clean["SYSTEMROOT"] == dirty["SYSTEMROOT"]


def test_env_scrubber_defaults_to_current_process_environment(monkeypatch):
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEI_fake")
    monkeypatch.setenv("PACHO_SENTINEL", "тук-съм")
    clean = updater._env_without_pyinstaller_vars()
    assert "_PYI_APPLICATION_HOME_DIR" not in clean
    assert clean["PACHO_SENTINEL"] == "тук-съм"


def test_install_update_starts_bat_with_scrubbed_environment(tmp_path, monkeypatch):
    """Самата install_update трябва да подава ПОЧИСТЕНАТА среда на Popen —
    точно това е поправката на диалога след всяко автоматично обновяване.
    Проверено чрез прихващане на реалното Popen извикване."""
    import hashlib
    fake_exe = tmp_path / "PachoLogistic.exe"
    monkeypatch.setattr(updater.sys, "executable", str(fake_exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Timer", lambda *a, **k: type(
        "T", (), {"start": lambda self: None})())
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEI_stale")
    monkeypatch.setenv("_MEIPASS2", "/tmp/_MEI_stale")

    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    payload = b"MZ" + b"\x00" * 1_100_000
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _FakeResp(payload))
    updater.install_update("http://example.invalid/x.exe",
                           expected_sha256=hashlib.sha256(payload).hexdigest())

    assert "env" in captured, "Popen трябва да получава изрична, почистена среда"
    assert "_PYI_APPLICATION_HOME_DIR" not in captured["env"]
    assert "_MEIPASS2" not in captured["env"]
    assert captured["args"][0] == "cmd.exe"


# ---------------------------------------------------------------- start_auto_update_loop
# Заявка: „Прави автоматична проверка за нова версия и автоматичното ѝ
# инсталиране при всяко влизане в програмата.“ Функцията вече прави точно
# това при ВСЯКО стартиране на компилираното .exe (app.py вика я
# безусловно при старт, вж. app.py:137/updater.py) — но досега бяха
# тествани само помощните ѝ функции (кеш, checksum, env-чистене), НЕ и
# самата логика на цикъла (дали наистина проверява и инсталира БЕЗ
# потвърждение, дали спазва изключението за мрежов режим, дали не спира
# завинаги при временна грешка). Тестовете тук покриват директно цикъла.

def test_auto_update_loop_installs_automatically_without_confirmation(monkeypatch):
    """При налична нова версия install_update() се вика АВТОМАТИЧНО от
    фоновия цикъл — без потребителят да отваря таблото или да натиска
    бутон — точно поведението, което е поискано."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater, "check_for_update", lambda: {
        "available": True,
        "download": "http://example.invalid/x.exe",
        "expected_sha256": "deadbeef",
    })
    done = threading.Event()
    calls = []

    def _fake_install(url, sha256=None):
        calls.append((url, sha256))
        done.set()

    monkeypatch.setattr(updater, "install_update", _fake_install)
    updater.start_auto_update_loop(lambda: False, first_delay=0, interval=999)
    assert done.wait(timeout=2), "install_update() не беше извикан навреме"
    assert calls == [("http://example.invalid/x.exe", "deadbeef")]


def test_auto_update_loop_skips_entirely_in_network_server_mode(monkeypatch):
    """Мрежов режим (тази инсталация служи като централен сървър за други
    компютри в офиса) — автоматичен рестарт би прекъснал работата на
    всички останали служители неочаквано, затова обновяването там остава
    само ръчно (бутон в таблото). Цикълът не бива дори да пита GitHub."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    calls = []
    monkeypatch.setattr(updater, "check_for_update",
                        lambda: calls.append(1) or {"available": False})
    updater.start_auto_update_loop(lambda: True, first_delay=0, interval=0.01)
    time.sleep(0.2)
    assert calls == []


def test_auto_update_loop_does_nothing_outside_compiled_windows_exe(monkeypatch):
    """При стартиране от изходния код (не компилирано .exe) автоматичното
    обновяване изобщо не пуска фонова нишка — работи само в реалната
    настолна .exe версия за Windows."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: False)
    calls = []
    monkeypatch.setattr(updater, "check_for_update",
                        lambda: calls.append(1) or {"available": False})
    updater.start_auto_update_loop(lambda: False, first_delay=0, interval=0.01)
    time.sleep(0.2)
    assert calls == []


def test_auto_update_loop_recovers_after_a_failed_check_and_retries(monkeypatch):
    """При грешка (напр. временно няма връзка при старта на програмата)
    цикълът не бива да спира завинаги — пробва пак на следващата
    итерация, вместо да изисква нов рестарт на цялата програма, за да се
    провери отново за обновление."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    attempts = {"n": 0}

    def _flaky_check():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("временно няма връзка")
        return {"available": True, "download": "http://example.invalid/y.exe",
                "expected_sha256": None}

    monkeypatch.setattr(updater, "check_for_update", _flaky_check)
    done = threading.Event()
    monkeypatch.setattr(updater, "install_update",
                        lambda url, sha256=None: done.set())
    updater.start_auto_update_loop(lambda: False, first_delay=0, interval=0.05)
    assert done.wait(timeout=2), "цикълът не се възстанови след грешката"
    assert attempts["n"] >= 2
