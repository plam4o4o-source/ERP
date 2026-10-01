# -*- coding: utf-8 -*-
"""Връщане към старата версия при неуспешно обновяване (одит 01.10.2026, O8).

Скриптът досега правеше `move /y` върху .exe-то и никога не проверяваше
дали новата версия изобщо тръгва. Сега старото .exe се пази като .old,
новото се стартира и скриптът чака знак „стартирах“ (updater.confirm_started
от app.py); без него в срок — връща .old и пише маркера за провал.

Windows не е наличен в тестовата среда: .bat-ът се генерира от истинския
install_update и се изпълнява от малък симулатор на точно онова подмножество
от cmd.exe, което скриптът ползва (labels/goto, if, set, move, copy, start…)."""
import os
import re


import updater

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeResp:
    def __init__(self, data):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _generate(tmp_path, monkeypatch):
    """Истинският install_update с фалшиви мрежа/Popen; връща (bat, popen kwargs)."""
    import hashlib
    folder = tmp_path / "R&D 100% !важно!"
    folder.mkdir()
    exe = folder / "PachoLogistic.exe"
    exe.write_bytes(b"OLD")
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Timer", lambda *a, **k: type(
        "T", (), {"start": lambda self: None})())
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    payload = b"MZ" + b"\x00" * 1_100_000
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _FakeResp(payload))
    updater.install_update("http://example.invalid/x.exe",
                           expected_sha256=hashlib.sha256(payload).hexdigest(),
                           version="3.76.0")
    bat = str(folder / ("pacho_update_%s.bat" % updater._machine_suffix()))
    return bat, captured, str(exe)


# ---------------------------------------------------------------- симулатор на cmd.exe

class _Batch:
    def __init__(self, path, args, env, on_start, locked_moves=0):
        self.path, self.args, self.env = path, args, dict(env)
        self.on_start = on_start
        self.locked_moves = locked_moves
        self.started = []
        self.killed = 0
        self.lines = open(path, encoding="ascii", newline="").read().split("\r\n")

    def _expand(self, line):
        line = line.replace("%~dp0", os.path.dirname(self.path) + os.sep)
        line = line.replace("%~f0", self.path)
        line = line.replace("%~nx2", os.path.basename(self.args[1]))
        for i, value in enumerate(self.args, start=1):
            line = line.replace("%%~%d" % i, value)
        return re.sub(r"%([A-Z_]+)%", lambda m: self.env.get(m.group(1), ""), line)

    def _cmd(self, cmd):
        """Връща (успех, goto или None)."""
        cmd = cmd.strip()
        cmd = re.sub(r"\s*(>nul 2>&1|2>nul|>nul)$", "", cmd)
        m = re.match(r'(?:copy|move) /y "([^"]+)" "([^"]+)"$', cmd)
        if m:
            if cmd.startswith("move") and self.locked_moves:
                self.locked_moves -= 1
                return False, None
            if not os.path.exists(m.group(1)):
                return False, None
            data = open(m.group(1), "rb").read()
            with open(m.group(2), "wb") as fh:
                fh.write(data)
            if cmd.startswith("move"):
                os.remove(m.group(1))
            return True, None
        m = re.match(r"goto (\w+)$", cmd)
        if m:
            return True, m.group(1)
        m = re.match(r"set /a (\w+)\+=1$", cmd)
        if m:
            self.env[m.group(1)] = str(int(self.env.get(m.group(1), "0")) + 1)
            return True, None
        m = re.match(r"set (\w+)=(.*)$", cmd)
        if m:
            if m.group(2):
                self.env[m.group(1)] = m.group(2)
            else:
                self.env.pop(m.group(1), None)
            return True, None
        m = re.match(r'echo (.*?)\s?> "([^"]+)"$', cmd)
        if m:
            with open(m.group(2), "w", encoding="ascii") as fh:
                fh.write(m.group(1) + "\n")
            return True, None
        m = re.match(r'del "([^"]+)"$', cmd)
        if m:
            if os.path.exists(m.group(1)):
                os.remove(m.group(1))
            return True, None
        m = re.match(r'start "" "([^"]+)"$', cmd)
        if m:
            self.started.append(open(m.group(1), "rb").read())
            self.on_start(m.group(1), dict(self.env))
            return True, None
        if cmd.startswith(("ping ", "@echo off", "setlocal")):
            return True, None
        if cmd.startswith("taskkill "):
            self.killed += 1
            return True, None
        raise AssertionError("непозната команда в скрипта: %r" % cmd)

    def _run(self, line):
        line = line.strip()
        m = re.match(r'if (not )?exist "([^"]+)" (.*)$', line)
        if m:
            if bool(m.group(1)) != os.path.exists(m.group(2)):
                return self._run(m.group(3))
            return None
        m = re.match(r"if defined (\w+) \((.*)\) else \((.*)\)$", line)
        if m:
            branch = m.group(2) if m.group(1) in self.env else m.group(3)
            for part in branch.split(" & "):
                target = self._run(part)
                if target:
                    return target
            return None
        m = re.match(r"if defined (\w+) (.*)$", line)
        if m:
            return self._run(m.group(2)) if m.group(1) in self.env else None
        m = re.match(r"if (\d+) LSS (\d+) (.*)$", line)
        if m:
            return self._run(m.group(3)) if int(m.group(1)) < int(m.group(2)) else None
        for op in (" && ", " || "):
            if op in line:
                first, rest = line.split(op, 1)
                ok, target = self._cmd(first)
                if ok == (op == " && "):
                    return self._run(rest)
                return target
        return self._cmd(line)[1]

    def run(self, max_steps=10000):
        labels = {l[1:]: i for i, l in enumerate(self.lines) if l.startswith(":")}
        pc = 0
        for _ in range(max_steps):
            if pc >= len(self.lines):
                return
            line = self.lines[pc]
            pc += 1
            if not line or line.startswith(":"):
                continue
            target = self._run(self._expand(line))
            if target:
                pc = labels[target] + 1
        raise AssertionError("скриптът не приключи")


def _simulate(tmp_path, monkeypatch, new_starts_ok, locked_moves=0):
    bat, popen, exe = _generate(tmp_path, monkeypatch)
    args = re.findall(r'"([^"]*)"', popen["args"][len('cmd.exe /d /s /c "'):-1])
    assert args[0] == bat and args[2] == exe and args[3] == "3.76.0"
    with open(args[1], "wb") as fh:  # „свалената“ версия
        fh.write(b"NEW")

    def on_start(path, env):
        # Новата версия при успешен старт вика updater.confirm_started().
        if open(path, "rb").read() == b"NEW" and new_starts_ok:
            monkeypatch.setattr(updater.os, "environ", env)
            assert updater.confirm_started()

    sim = _Batch(bat, args[1:], popen["env"], on_start, locked_moves=locked_moves)
    sim.run()
    folder = os.path.dirname(exe)
    log = open(os.path.join(folder, "pacho_update.log"), encoding="ascii").read()
    return sim, exe, folder, log


def test_successful_update_keeps_old_exe_and_waits_for_the_started_marker(tmp_path,
                                                                          monkeypatch):
    sim, exe, folder, log = _simulate(tmp_path, monkeypatch, new_starts_ok=True,
                                      locked_moves=3)
    assert open(exe, "rb").read() == b"NEW"
    assert open(exe + ".old", "rb").read() == b"OLD"
    assert "OK: updated successfully" in log
    assert not os.path.exists(os.path.join(folder, updater._failed_marker_name()))
    assert not [n for n in os.listdir(folder) if "_started_" in n]
    assert sim.started == [b"NEW"] and sim.killed == 0
    assert not os.path.exists(sim.path), "скриптът се самоизтрива"


def test_new_version_that_never_starts_is_rolled_back(tmp_path, monkeypatch):
    sim, exe, folder, log = _simulate(tmp_path, monkeypatch, new_starts_ok=False)
    assert open(exe, "rb").read() == b"OLD", "старото .exe трябва да е върнато"
    assert "old version restored" in log
    marker = os.path.join(folder, updater._failed_marker_name())
    assert open(marker, encoding="ascii").read().strip() == "3.76.0"
    assert sim.started == [b"NEW", b"OLD"] and sim.killed == 1
    assert "PACHO_UPDATE_STARTED_MARKER" not in sim.env, \
        "старата версия не бива да пише знак за успешен старт"


def test_restart_script_disables_delayed_expansion_and_passes_marker_path(tmp_path,
                                                                          monkeypatch):
    """`!` в пътя се губеше при включено DelayedExpansion в регистъра."""
    bat, popen, exe = _generate(tmp_path, monkeypatch)
    content = open(bat, encoding="ascii").read()
    assert content.splitlines()[1] == "setlocal DisableDelayedExpansion"
    assert content.index('copy /y "%~2" "%~2.old"') < content.index(":retry")
    marker = popen["env"][updater.STARTED_MARKER_ENV]
    assert os.path.dirname(marker) == os.path.dirname(exe)
    assert os.path.basename(marker) in content


def test_confirm_started_writes_only_when_launched_by_the_script(tmp_path, monkeypatch):
    marker = tmp_path / "started.txt"
    monkeypatch.delenv(updater.STARTED_MARKER_ENV, raising=False)
    assert updater.confirm_started() is False
    monkeypatch.setenv(updater.STARTED_MARKER_ENV, str(marker))
    assert updater.confirm_started() is True
    assert marker.read_text(encoding="utf-8") == updater.__version__
    assert updater.STARTED_MARKER_ENV not in os.environ


def test_app_confirms_start_in_main():
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    main = src[src.index('if __name__ == "__main__":\n    _cfg'):]
    assert "updater.confirm_started()" in main


def test_expired_preview_message_is_translatable():
    """Одит (01.10.2026): съобщението в app.preview_document не минаваше през _()."""
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert 'flash(_("Прегледът е изтекъл' in src
