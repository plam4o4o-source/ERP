# -*- coding: utf-8 -*-
"""PyInstaller hook за babel — заменя вградения (release.yml подава тази папка
с --additional-hooks-dir, който има предимство пред вградените hook-ове).

Одит (01.10.2026, Q9): вграденият hook събира CLDR данните за всичките ~1080
локала (~29 MB, ~9.7 MB в .exe-то). Интерфейсът е само БГ/EN/TR (db.LANGUAGES);
`root` е родителят на всеки локал, `global.dat` — общите таблици на babel.
Нов език в db.LANGUAGES → добави го тук (tests/test_release_babel_data.py пази това)."""
from PyInstaller.utils.hooks import collect_data_files

LOCALES = ("root", "bg", "en", "tr")
BABEL_FILES = ["global.dat"] + ["locale-data/%s.dat" % name for name in LOCALES]

datas = collect_data_files("babel", includes=BABEL_FILES)

# Същите като във вградения hook: разпикълването на root.dat ги изисква.
hiddenimports = [
    "babel.dates",
    "babel.localedata",
    "babel.plural",
    "babel.numbers",
]
