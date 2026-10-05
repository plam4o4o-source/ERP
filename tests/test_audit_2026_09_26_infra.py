# -*- coding: utf-8 -*-
"""Регресионни тестове за одита от 26.09.2026 — инфраструктура (обновяване,
конфигурация, архиви, таен ключ, стартиране, отдалечен достъп, тестова
изолация)."""
import hashlib
import http.server
import io
import os
import subprocess
import sys
import tarfile
import threading
import time
import types
import zipfile
from datetime import datetime, timedelta

import pytest

from conftest import ROOT, post_with_csrf


# ------------------------------------------------------------------ №1
class _Dl:
    def __init__(self, data):
        self._data = data
        self.headers = {}

    def read(self, n=-1):
        data, self._data = self._data, b""
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _cmd_executes(line):
    """Какво реално изпълнява cmd.exe за даден команден ред (CreateProcessW
    го подава непроменен) — по документираното в `cmd /?` правило: ако има
    /S или НЕ са точно две кавички, а първият знак след /C е кавичка, cmd
    маха нея и ПОСЛЕДНАТА кавичка на реда, а останалото пази дословно."""
    head, after = line.split(" /c ", 1)
    use_s = " /s" in head.lower()
    if after.startswith('"') and (use_s or after.count('"') != 2):
        last = after.rfind('"')
        after = after[1:last] + after[last + 1:]
    return after


def _split_cmd(text):
    """Разделя ред на думи като cmd/.bat (%1, %2…): интервалите в кавички не
    делят; %~N маха кавичките."""
    tokens, cur, quoted, started = [], "", False, False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            started = True
        elif ch == " " and not quoted:
            if started:
                tokens.append(cur)
            cur, started = "", False
        else:
            cur += ch
            started = True
    if started:
        tokens.append(cur)
    return tokens


def _install(tmp_path, monkeypatch, sub, version="3.75.0"):
    import updater
    d = tmp_path / sub
    d.mkdir(parents=True)
    exe = d / "PachoLogistic.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Timer",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    cap = {}
    monkeypatch.setattr(updater.subprocess, "Popen", lambda a, **k: cap.setdefault("a", a))
    payload = b"MZ" + b"\0" * 1_100_000
    monkeypatch.setattr(updater.net, "urlopen", lambda r, timeout=120: _Dl(payload))
    updater.install_update("http://x/x.exe", expected_sha256=hashlib.sha256(payload).hexdigest(),
                           version=version)
    bat = os.path.join(str(d), "pacho_update_%s.bat" % updater._machine_suffix())
    new_exe = str(exe) + "." + updater._machine_suffix() + ".new"
    return cap["a"], bat, new_exe, str(exe)


def test_restart_command_runs_the_bat_when_install_path_has_spaces(tmp_path, monkeypatch):
    """Находка №1 (ВИСОКА): при интервал в пътя cmd изпълняваше „…/Ivan“
    вместо .bat-а, а старият процес пак излизаше — сваляне при всяко пускане."""
    args, bat, new_exe, exe = _install(
        tmp_path, monkeypatch, os.path.join("Ivan Petrov", "Нова папка (2)"))
    line = args if isinstance(args, str) else subprocess.list2cmdline(args)
    tokens = _split_cmd(_cmd_executes(line))
    assert tokens[0] == bat, "cmd изпълнява %r вместо скрипта" % tokens[0]
    assert tokens[1:] == [new_exe, exe, "3.75.0"]


def test_restart_command_neutralises_unsafe_version_text(tmp_path, monkeypatch):
    """Версията идва от етикета на релийза — кавичка в нея не бива да може да
    разцепи командния ред."""
    import updater
    line = updater._restart_command_line(r"C:\A B\x.bat", r"C:\A B\n.exe", r"C:\A B\e.exe",
                                         'v1" & calc & "')
    tokens = _split_cmd(_cmd_executes(line))
    assert tokens == [r"C:\A B\x.bat", r"C:\A B\n.exe", r"C:\A B\e.exe", "?"]


# ------------------------------------------------------------------ №2
def test_config_saved_with_utf8_bom_is_read(tmp_path, monkeypatch):
    """Находка №2: Notepad записва „UTF-8 с BOM“ → файлът минаваше за
    повреден и db_path се губеше (нова празна база с admin/admin123)."""
    import config as appconfig
    path = tmp_path / "pacho_config.json"
    path.write_bytes(b"\xef\xbb\xbf" + '{"db_path": "\\\\\\\\SRV\\\\Дял\\\\p.db"}'.encode("utf-8"))
    monkeypatch.setattr(appconfig, "CONFIG_PATH", str(path))
    cfg = appconfig.load_config()
    assert cfg["db_path"] == "\\\\SRV\\Дял\\p.db"
    assert not os.path.exists(str(path) + ".corrupt")


# ------------------------------------------------------------------ №3
def _backup_env(db_module, tmp_path):
    base = os.path.dirname(db_module.DB_PATH)
    folder = tmp_path / "archive"
    folder.mkdir()
    return base, str(folder)


def test_backup_includes_attachments_and_logo(db_module, tmp_path):
    """Находка №3: архивът съдържаше само .db — сканираните ЧМР-и и логото
    на фирмата не се архивираха изобщо."""
    import backup
    base, folder = _backup_env(db_module, tmp_path)
    os.makedirs(os.path.join(base, "attachments", "7"))
    with open(os.path.join(base, "attachments", "7", "cmr.pdf"), "wb") as f:
        f.write(b"%PDF-signed")
    with open(os.path.join(base, "company_logo.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nlogo")

    dest = backup.local_backup(folder)

    companion = dest[:-3] + ".files.zip"
    assert os.path.exists(companion), "няма архив на прикачените файлове"
    with zipfile.ZipFile(companion) as zf:
        assert zf.read("attachments/7/cmr.pdf") == b"%PDF-signed"
        assert zf.read("company_logo.png") == b"\x89PNG\r\n\x1a\nlogo"
    assert not [n for n in os.listdir(folder) if n.endswith(".partial")]


def test_backup_without_attachments_writes_no_zip(db_module, tmp_path):
    import backup
    _base, folder = _backup_env(db_module, tmp_path)
    dest = backup.local_backup(folder)
    assert os.listdir(folder) == [os.path.basename(dest)]


def test_rotation_removes_the_companion_with_its_db(tmp_path):
    """Ротацията трие zip-а заедно със своя .db (иначе файловете растат без
    край в същата папка)."""
    import backup
    now = datetime(2026, 9, 26, 12, 0, 0)
    day = now - timedelta(days=5)
    older = "pacho_logistic_%s_aaaaaa" % day.replace(hour=8).strftime("%Y%m%d_%H%M%S")
    newer = "pacho_logistic_%s_bbbbbb" % day.replace(hour=9).strftime("%Y%m%d_%H%M%S")
    for stem in (older, newer):
        for ext in (".db", ".files.zip"):
            (tmp_path / (stem + ext)).write_bytes(b"x")
    backup._rotate_local_backups(str(tmp_path), now=now)
    left = sorted(os.listdir(str(tmp_path)))
    assert left == [older + ".db", older + ".files.zip"]


# ------------------------------------------------------------------ №4
@pytest.mark.parametrize("content", ["", "   \n", "abc"])
def test_empty_secret_key_file_is_repaired(tmp_path, monkeypatch, content):
    """Находка №4: празен (или твърде къс) .secret_key никога не се поправяше
    — всеки старт вземаше временен ключ и изхвърляше всички потребители."""
    import db
    path = tmp_path / ".secret_key"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(db, "SECRET_PATH", str(path))
    first = db.get_secret_key()
    second = db.get_secret_key()
    assert first == second, "ключът се сменя при всеки старт"
    assert path.read_text(encoding="utf-8").strip() == first
    assert len(first) >= 32


# ------------------------------------------------------------------ №5
class _Resp:
    def __init__(self, text):
        self._b = text.encode("utf-8")

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("text", [
    "%s  OtherFile.zip\n" % ("a" * 64),
    "не е манифест\n",
])
def test_checksum_manifest_without_exe_entry_fails_closed(monkeypatch, text):
    """Находка №5: SHA256SUMS.txt без ред за .exe-то → None → install_update
    пропускаше проверката. Щом релийзът има манифест, това е провал."""
    import updater
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=8: _Resp(text))
    assets = [{"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/s"}]
    with pytest.raises(RuntimeError, match="SHA256SUMS"):
        updater._fetch_expected_checksum(assets, timeout=1)


# ------------------------------------------------------------------ №6
def test_wait_for_server_accepts_an_http_error_page():
    """Находка №6: 503 от страницата „базата е недостъпна“ значи, че
    сървърът е горе — досега се чакаше целият таймаут."""
    import desktop

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"db unavailable")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t = time.time()
        ok = desktop.wait_for_server("http://127.0.0.1:%d" % srv.server_port, timeout=4)
        elapsed = time.time() - t
    finally:
        srv.shutdown()
        srv.server_close()
    assert ok is True
    assert elapsed < 2


# ------------------------------------------------------------------ №7
def _auto_settings(folder):
    return lambda: {"backup_folder": folder, "backup_auto": "on"}


def test_scheduled_backup_runs_once_per_interval_across_workstations(db_module, tmp_path):
    """Находка №7: при споделена база всяка станция пускаше свой часов архив
    — N копия на час. Втората „станция“ (същата база) трябва да пропусне."""
    import backup
    _base, folder = _backup_env(db_module, tmp_path)
    first = backup.run_scheduled_backup(_auto_settings(folder))
    second = backup.run_scheduled_backup(_auto_settings(folder))
    assert first and second is None
    assert len([n for n in os.listdir(folder) if n.endswith(".db")]) == 1


def test_scheduled_backup_slot_frees_after_the_interval(db_module):
    import backup
    now = time.time()
    assert backup._claim_auto_backup_slot(3600, now=now - 3600) is True
    assert backup._claim_auto_backup_slot(3600, now=now) is True
    assert backup._claim_auto_backup_slot(3600, now=now + 60) is False


def test_missing_backup_folder_is_logged_once(db_module, tmp_path, monkeypatch):
    """Недостъпна от този компютър папка — един запис в лога, не всеки час."""
    import applog
    import backup
    logged = []
    monkeypatch.setattr(applog, "log_warning", lambda *a, **k: logged.append(a))
    monkeypatch.setattr(applog, "log_exception", lambda *a, **k: logged.append(a))
    monkeypatch.setattr(backup, "_missing_folder_logged", set())
    missing = str(tmp_path / "Z_drive_only_on_server")
    for _ in range(3):
        assert backup.run_scheduled_backup(_auto_settings(missing)) is None
    assert len(logged) == 1


# ------------------------------------------------------------------ №8
def test_tests_never_see_the_real_config_path():
    """Находка №8: тест, пращащ формата „Мрежови настройки“, презаписваше
    истинския pacho_config.json в корена на проекта."""
    import config as appconfig
    assert os.path.dirname(os.path.abspath(appconfig.CONFIG_PATH)) != os.path.abspath(ROOT)


def test_import_app_test_uses_a_temporary_database():
    """`import app` пуска db.init_db() на модулно ниво — тестът трябва да е
    върху временна база (db_module), не върху истинската."""
    import inspect

    import test_audit_2026_09_02 as mod
    assert "db_module" in inspect.signature(mod.test_startup_log_name_is_per_machine).parameters


def test_single_instance_test_does_not_leak_temp_dirs():
    import inspect

    import test_audit_2026_08_31 as mod
    fn = mod.test_only_one_instance_can_hold_the_lock
    code = [ln for ln in inspect.getsource(fn).splitlines() if not ln.strip().startswith("#")]
    assert not any("mkdtemp(" in ln for ln in code)
    assert "tmp_path" in inspect.signature(fn).parameters


# ------------------------------------------------------------------ №9
def test_macos_cloudflared_is_extracted_from_the_tgz(tmp_path, monkeypatch):
    """Находка №9: под macOS .tgz се пускаше направо (exec format error).
    Вади се само `cloudflared`; член с „../“ не се пише никъде."""
    import remote_tunnel
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(remote_tunnel.os, "name", "posix")
    target = tmp_path / "bin" / "cloudflared"
    target.parent.mkdir()
    monkeypatch.setattr(remote_tunnel, "_binary_path", lambda: str(target))

    binary = b"\xcf\xfa\xed\xfe" + os.urandom(200000)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("../evil", os.urandom(150000)), ("cloudflared", binary)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    tgz = buf.getvalue()

    class R:
        headers = {"Content-Length": str(len(tgz))}

        def __init__(self):
            self._s = io.BytesIO(tgz)

        def read(self, n=-1):
            return self._s.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(remote_tunnel.net, "urlopen", lambda req, timeout=60: R())
    path = remote_tunnel.ensure_binary()
    assert path == str(target)
    assert target.read_bytes() == binary
    assert os.access(path, os.X_OK)
    assert not (tmp_path / "evil").exists() and not (tmp_path / "bin" / "evil").exists()
    assert sorted(os.listdir(str(target.parent))) == ["cloudflared"]
    assert remote_tunnel._binary_looks_valid(path)


# ------------------------------------------------------------------ №10
def test_numeric_unload_point_values_do_not_lose_the_client(admin_client, db_module):
    """Находка №10: {"postcode": 1000} гърмеше с AttributeError на .strip()
    и новият клиент се губеше."""
    resp = post_with_csrf(admin_client, "/clients/new", {
        "name": "Числов Пощенски Код ЕООД",
        "unload_points_json": '[{"postcode": 1000, "city": "София", "label": ["x"]}]',
    }, csrf_source_url="/clients/new", follow_redirects=False)
    assert resp.status_code == 302
    con = db_module.get_db()
    try:
        row = con.execute("SELECT id FROM clients WHERE name = ?",
                          ("Числов Пощенски Код ЕООД",)).fetchone()
        assert row is not None, "клиентът е загубен"
        points = con.execute("SELECT * FROM client_unload_points WHERE client_id = ?",
                             (row["id"],)).fetchall()
    finally:
        con.close()
    assert [(p["postcode"], p["city"], p["label"]) for p in points] == [("1000", "София", "")]
