# -*- coding: utf-8 -*-
"""Одит (01.10.2026, Q9): .exe-то пакетира само CLDR данните на babel за
езиците на интерфейса (pyinstaller_hooks/hook-babel.py), не всичките ~1080
локала. Тук проверяваме, че приложението наистина работи на БГ/EN/TR само с
тези файлове, и че release.yml ползва hook-а."""
import os
import re
import shutil
import subprocess
import sys
import types

import babel

from conftest import ROOT, read_source

_HOOK = os.path.join(ROOT, "pyinstaller_hooks", "hook-babel.py")


def _hook_includes(monkeypatch):
    """Изпълнява hook-а със заместител на PyInstaller и връща `includes`."""
    calls = []
    hooks_mod = types.ModuleType("PyInstaller.utils.hooks")
    hooks_mod.collect_data_files = lambda package, **kw: calls.append((package, kw)) or []
    for name, mod in (("PyInstaller", types.ModuleType("PyInstaller")),
                      ("PyInstaller.utils", types.ModuleType("PyInstaller.utils")),
                      ("PyInstaller.utils.hooks", hooks_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    exec(compile(read_source("pyinstaller_hooks", "hook-babel.py"), _HOOK, "exec"), {})
    assert [package for package, _ in calls] == ["babel"]
    return calls[0][1]["includes"]


def _minimal_babel_copy(dest, includes):
    """Копие на пакета babel само с кода и файловете от hook-а — като в .exe-то."""
    src = os.path.dirname(babel.__file__)
    target = os.path.join(dest, "babel")
    shutil.copytree(src, target, ignore=shutil.ignore_patterns("*.dat", "__pycache__"))
    for rel in includes:
        shutil.copy(os.path.join(src, rel), os.path.join(target, rel))
    return target


_PROBE = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import babel, db, config
assert os.path.dirname(babel.__file__) == sys.argv[2], babel.__file__
config.CONFIG_PATH = os.path.join(sys.argv[3], "cfg.json")
db.DB_PATH = os.path.join(sys.argv[3], "t.db")
db.SECRET_PATH = db.DB_PATH + ".secret"
import appcore, routes_auth
app = appcore.create_app(run_boot_tasks=False)
routes_auth.register(app)
for lang in db.LANGUAGES:
    c = app.test_client()
    body = c.get("/login?lang=" + lang).get_data(as_text=True)
    print(lang, "label", body.split('for="username">', 1)[1].split("<", 1)[0])
    import flask_babel
    with app.test_request_context():
        from flask import session
        session["lang"] = lang
        print(lang, "locale", flask_babel.get_locale(), flask_babel.gettext("Вход"))
        # Пълно зареждане на CLDR данните (наследени от root) — за бъдещо форматиране.
        print(lang, "number", flask_babel.format_decimal(1234.5))
'''


def test_app_runs_in_every_ui_language_with_only_the_bundled_babel_data(tmp_path, monkeypatch):
    import db

    includes = _hook_includes(monkeypatch)
    for lang in db.LANGUAGES:
        assert "locale-data/%s.dat" % lang in includes, "%s липсва в hook-babel.py" % lang
    babel_dir = _minimal_babel_copy(str(tmp_path / "site"), includes)
    dat = [n for n in os.listdir(os.path.join(babel_dir, "locale-data")) if n.endswith(".dat")]
    assert sorted(dat) == sorted(os.path.basename(i) for i in includes if i.startswith("locale-data/"))

    env = dict(os.environ, PYTHONPATH=str(tmp_path / "site"))
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, ROOT, babel_dir, str(tmp_path)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    lines = dict((tuple(line.split(" ", 2)[:2]), line) for line in out.stdout.splitlines())
    expected = {"bg": ("Вход", "Потребителско име"), "en": ("Login", "Username"),
                "tr": ("Giriş", "Kullanıcı adı")}
    for lang, (word, label) in expected.items():
        assert lines[(lang, "locale")] == "%s locale %s %s" % (lang, lang, word), out.stdout
        assert lines[(lang, "label")] == "%s label %s" % (lang, label), out.stdout
    assert lines[("en", "number")] == "en number 1,234.5", out.stdout
    assert lines[("tr", "number")] == "tr number 1.234,5", out.stdout


def test_release_build_uses_the_babel_hook_instead_of_collecting_every_locale():
    workflow = read_source(".github", "workflows", "release.yml")
    build = re.search(r"pyinstaller --onefile[^\n]*(?:\n +[^\n]+)*", workflow).group(0)
    assert "--collect-data babel" not in build and "--collect-all babel" not in build
    assert "--additional-hooks-dir pyinstaller_hooks" in build
