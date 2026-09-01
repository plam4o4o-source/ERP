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


@pytest.fixture(autouse=True)
def _reset_update_cache_refresh_flag():
    """С7: _refresh_in_progress е споделено module-ниво състояние —
    нулираме го преди/след всеки тест (иначе тест, прекъснат по средата
    на фоново опресняване, би оставил флага "залепнал" на True и всички
    следващи тестове в тоя файл биха мислели, че вече тече опресняване)."""
    updater._refresh_in_progress = False
    yield
    updater._refresh_in_progress = False


class _SyncThread:
    """Замества threading.Thread в тестовете за С7 — стартира target()
    СИНХРОННО (в СЪЩАТА нишка), за да е детерминирано кога фоновото
    опресняване на кеша (updater._refresh_cache_in_background) реално е
    приключило, вместо тестът да проследява/чака истинска фонова нишка."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass


@pytest.fixture(autouse=True)
def _reset_pending_restart():
    """В6: _pending_restart е споделено module-ниво състояние — нулираме
    го преди/след всеки тест, за да не изтича между тестове (напр. след
    тест, който реално задейства _schedule_auto_install)."""
    with updater._pending_restart_lock:
        updater._pending_restart["scheduled_at"] = None
        updater._pending_restart["version"] = None
    yield
    with updater._pending_restart_lock:
        updater._pending_restart["scheduled_at"] = None
        updater._pending_restart["version"] = None


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
    """Находка M5 + одит С7: две "едновременни" зареждания на таблото не
    бива да пускат по отделна GitHub заявка — при остарял кеш check_cached
    вече ВИНАГИ връща веднага текущото съдържание на кеша (тук: старото,
    None) и само СТАРТИРА фоново опресняване (виж С7 по-долу защо вече не
    е синхронно); втора заявка, пристигнала докато опресняването вече
    тече (или е приключило), не бива да пуска ВТОРА GitHub заявка."""
    updater._cache["time"] = 0.0
    updater._cache["info"] = None
    calls = []

    def _fake_check():
        calls.append(1)
        return {"available": False, "latest": "1.0.0", "current": "1.0.0"}

    monkeypatch.setattr(updater, "check_for_update", _fake_check)
    monkeypatch.setattr(updater.threading, "Thread", _SyncThread)

    # Първото извикване вижда СТАРИЯ (празен) кеш веднага, но синхронно
    # (само в теста, чрез _SyncThread) стартира фоновото опресняване.
    first = updater.check_cached(max_age=3600)
    assert first is None
    # Второто вижда вече ОПРЕСНЕНИЯ кеш — не стартира втора заявка.
    second = updater.check_cached(max_age=3600)
    assert second == {"available": False, "latest": "1.0.0", "current": "1.0.0"}
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
    monkeypatch.setattr(updater.threading, "Thread", _SyncThread)

    # Първото извикване вижда СТАРИЯ (все още успешен) кеш веднага —
    # одит С7: неуспешната проверка НЕ бива да бави тази заявка, дори тя
    # да е тази, която (синхронно, само в теста) стартира опресняването.
    first = updater.check_cached(max_age=0)  # 0 -> винаги "остарял", пробва пак
    assert first == {"available": True, "latest": "5.0.0", "current": "1.0.0"}
    # Второто вижда резултата от вече приключилото (неуспешно) опресняване.
    second = updater.check_cached(max_age=0)
    assert second is None
    assert updater._cache["last_error"]


# ---------------------------------------------------------------- С7: таблото не бива да чака GitHub

def test_check_cached_never_blocks_on_the_network_call(monkeypatch):
    """Одит (находка С7, среден риск): преди поправката check_for_update()
    (до 8 сек. по подразбиране) течеше СИНХРОННО вътре в check_cached() —
    заявката, обслужваща /  (таблото), блокираше цялото това време при
    недостъпен GitHub. Тук `check_for_update` изкуствено се бави, но
    check_cached() трябва да върне резултат ПРАКТИЧЕСКИ мигновено —
    реалната (бавна) проверка минава в НАСТОЯЩА фонова нишка (не
    _SyncThread тук — нарочно, за да измерим реално време), докато
    заявката просто вижда текущия (стар) кеш."""
    updater._cache["time"] = 0.0
    updater._cache["info"] = None

    def _slow_check():
        time.sleep(1.0)
        return {"available": False, "latest": "1.0.0", "current": "1.0.0"}

    monkeypatch.setattr(updater, "check_for_update", _slow_check)

    started = time.time()
    result = updater.check_cached(max_age=3600)
    elapsed = time.time() - started
    assert result is None  # още няма готов резултат — но не сме чакали за него
    assert elapsed < 0.5, (
        "check_cached() блокира на реалната мрежова проверка вместо да я "
        "прати във фонова нишка (отне %.2f сек.)" % elapsed
    )
    # Изчакваме РЕАЛНАТА фонова нишка да приключи, преди следващия тест —
    # иначе би могла да презапише _cache междувременно (без значение за
    # проверката по-горе, но пази следващите тестове в файла чисти).
    time.sleep(1.2)


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
    # В6: _schedule_auto_install (обвивката, викана от цикъла) изчаква
    # AUTO_RESTART_WARNING_SECONDS преди истинския install_update — за
    # този тест интересува само че той СЕ извиква, не колко се чака.
    monkeypatch.setattr(updater, "AUTO_RESTART_WARNING_SECONDS", 0)
    monkeypatch.setattr(updater, "check_for_update", lambda: {
        "available": True,
        "download": "http://example.invalid/x.exe",
        "expected_sha256": "deadbeef",
        "latest": "9.9.9",
    })
    done = threading.Event()
    calls = []

    def _fake_install(url, sha256=None, **kwargs):
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
    monkeypatch.setattr(updater, "AUTO_RESTART_WARNING_SECONDS", 0)
    attempts = {"n": 0}

    def _flaky_check():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("временно няма връзка")
        return {"available": True, "download": "http://example.invalid/y.exe",
                "expected_sha256": None, "latest": "9.9.9"}

    monkeypatch.setattr(updater, "check_for_update", _flaky_check)
    done = threading.Event()
    monkeypatch.setattr(updater, "install_update",
                        lambda url, sha256=None, **kwargs: done.set())
    updater.start_auto_update_loop(lambda: False, first_delay=0, interval=0.05)
    assert done.wait(timeout=2), "цикълът не се възстанови след грешката"
    assert attempts["n"] >= 2


# ---------------------------------------------------------------- „автоматично обновяване не работи“
# Заявка: „автоматично обновяване след старт на програмата неработи,
# поправи го“. Два отделни, реални проблема в самия АВТОМАТИЧЕН (не
# ръчния през бутона) цикъл — виж пълните обяснения в updater.py при
# _clear_pending_restart и в start_auto_update_loop.

def test_schedule_auto_install_clears_pending_restart_when_install_fails(monkeypatch):
    """Ако install_update() се провали СЛЕД като вече е показал банера
    „Ще се рестартира след X сек“, банерът не бива да остане закован на
    „0 сек“ завинаги — за потребителя това изглежда като „автоматичното
    обновяване не работи“, а всъщност е лъжлив банер за рестарт, който
    никога не идва."""
    def _fail_install(url, sha256=None, **kwargs):
        raise RuntimeError("файлът е повреден при изтеглянето")

    monkeypatch.setattr(updater, "install_update", _fail_install)
    with pytest.raises(RuntimeError):
        updater._schedule_auto_install(
            "http://example.invalid/x.exe", "abc123", "9.9.9", warning_seconds=0)
    assert updater.get_pending_restart() is None


def test_auto_update_loop_clears_stuck_banner_after_failed_install(monkeypatch):
    """Същото, но през реалния фонов цикъл (start_auto_update_loop), не
    само директно през _schedule_auto_install."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater, "AUTO_RESTART_WARNING_SECONDS", 0)
    monkeypatch.setattr(updater, "check_for_update", lambda: {
        "available": True,
        "download": "http://example.invalid/x.exe",
        "expected_sha256": "deadbeef",
        "latest": "9.9.9",
    })
    failed = threading.Event()

    def _fail_install(url, sha256=None, **kwargs):
        failed.set()
        raise RuntimeError("контролната сума не съвпада")

    monkeypatch.setattr(updater, "install_update", _fail_install)
    updater.start_auto_update_loop(lambda: False, first_delay=0, interval=999)
    assert failed.wait(timeout=2), "install_update() не беше извикан"
    deadline = time.time() + 2
    while time.time() < deadline and updater.get_pending_restart() is not None:
        time.sleep(0.02)
    assert updater.get_pending_restart() is None, (
        "банерът остана закован на „предстои рестарт“ след неуспешна инсталация")


def test_auto_update_loop_retries_soon_after_a_failed_check_not_after_full_interval(monkeypatch):
    """При грешка (напр. временна липса на връзка точно при старта на
    програмата) цикълът трябва да пробва пак СКОРО, не чак след целия
    (двучасов по подразбиране) `interval` — огледално на
    check_cached()/_FAIL_RETRY_SECONDS за таблото."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater, "_FAIL_RETRY_SECONDS", 0.05)
    attempts = {"n": 0}

    def _always_fails():
        attempts["n"] += 1
        raise RuntimeError("временно няма връзка")

    monkeypatch.setattr(updater, "check_for_update", _always_fails)
    # `interval` умишлено ОГРОМЕН — ако поправката не работи, вторият опит
    # никога не би дошъл в рамките на таймаута на теста.
    updater.start_auto_update_loop(lambda: False, first_delay=0, interval=999)
    deadline = time.time() + 2
    while time.time() < deadline and attempts["n"] < 2:
        time.sleep(0.02)
    assert attempts["n"] >= 2, "цикълът изчака целия `interval`, вместо да пробва пак скоро"


def test_auto_update_loop_checks_almost_immediately_on_start_by_default(monkeypatch):
    """Заявка (22.08.2026): „направи веднага като се стартира да проверява
    за нова версия автоматично“ — първата проверка (по подразбиране, без
    изрично подаден `first_delay`) трябва да стане само секунди след
    старта на програмата, не 20 сек. по-късно."""
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    checked = threading.Event()
    monkeypatch.setattr(updater, "check_for_update",
                        lambda: checked.set() or {"available": False})
    started_at = time.time()
    # Без изричен first_delay — тества подразбиращата се стойност.
    updater.start_auto_update_loop(lambda: False, interval=999)
    assert checked.wait(timeout=5), (
        "първата проверка трябва да стане в рамките на няколко секунди от "
        "старта, не да чака 20 сек. по подразбиране")
    assert time.time() - started_at < 5


def test_auto_restart_warning_is_zero_by_default(monkeypatch):
    """Заявка (26.08.2026): „...и ВЕДНАГА да се инсталира“ — потребителят
    избра изрично да махне 90-секундния банер „Ще се рестартира след X
    сек“ пред отворените в момента прозорци (риска от находка В6 —
    изчезнала незапазена работа — е приет съзнателно за тази инсталация).
    Пази подразбиращата се стойност; ако някой я вдигне обратно (напр.
    „връщане“ на В6 без да съобрази новата заявка), тестът пада."""
    assert updater.AUTO_RESTART_WARNING_SECONDS == 0


def test_schedule_auto_install_installs_without_waiting_by_default(monkeypatch):
    """`_schedule_auto_install`, извикана БЕЗ изричен `warning_seconds`
    (точно както прави `start_auto_update_loop`), трябва да стигне до
    `install_update()` практически веднага — не да чака 90 сек."""
    calls = []
    monkeypatch.setattr(updater, "install_update",
                        lambda url, sha, **kwargs: calls.append((url, sha)))
    started_at = time.time()
    updater._schedule_auto_install("http://example.invalid/x.exe", "abc123", "9.9.9")
    elapsed = time.time() - started_at
    assert calls == [("http://example.invalid/x.exe", "abc123")]
    assert elapsed < 1, (
        "install_update трябва да се извика веднага (AUTO_RESTART_WARNING_SECONDS=0), "
        "не след изчакване — отне %.2f сек" % elapsed)
