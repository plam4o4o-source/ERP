# -*- coding: utf-8 -*-
"""Собствено допълнително тестово покритие за v3.66.0 (одит 25.08.2026).

Проверих независимо: CHANGELOG.md на този патч твърди „11 нови файла
(tests/test_audit_2026_08_25_*.py)“, но реално архитектският патч съдържа
само 8 (bulk_numbering, currency, fmtnum_print, livefilter, neg_excel,
pdf_busy, public_url, ui). От 13-те находки в доклада ДВЕ останаха без
собствен регресионен тест никъде в repo-то:

- №1 (ВИСОКА!) — конкурентен `save_config` (config.py): катинар + mkstemp
  срещу lost-update и FileNotFoundError при застъпващи се писачи. Именно
  тази находка носи най-драматичната собствена претенция в доклада
  („208 FileNotFoundError, 1169 повредени четения“) — точно затова заслужава
  собствена, независима проверка, не само доверие на думите на патча.
- №3 (средна) — видимост на филтърното „×“ в контрастната тема
  (`:root[data-theme="contrast"] .filter-chip .filter-chip-x`).

И двете са затворени тук, по модела на предишните `..._gaps.py` файлове от
тази сесия (v3.65.1). Проверени с revert-observe-restore: върнат стария
`config.save_config` (директен `open(CONFIG_PATH + ".tmp", "w")`, без
катинар) — тестът по-долу за конкурентност се провали с реални
`FileNotFoundError`/повредени четения; върната поправката — отново зелено.
"""
import json
import os
import threading

import config as appconfig


def test_concurrent_save_config_never_crashes_or_corrupts(tmp_path, monkeypatch):
    """Находка №1: няколко нишки викат save_config/load_config почти
    едновременно (симулира мрежов режим — backup.mark_dirty вика
    load_config при всеки запис на документ, докато админ едновременно
    пази системни настройки). Преди поправката споделеният
    `CONFIG_PATH + ".tmp"` водеше до FileNotFoundError от `os.replace`
    и до повредени/непълни прочити; лошия случай — крайното състояние
    изгубено. Тук очакваме нула грешки, нула повредени четения, и валидно
    финално състояние."""
    cfg_path = os.path.join(str(tmp_path), "pacho_config.json")
    monkeypatch.setattr(appconfig, "CONFIG_PATH", cfg_path)
    appconfig.save_config({"db_path": "", "network_mode": False})

    N_WRITES_PER_WRITER = 40
    N_READS_PER_READER = 60
    errors = []
    corrupted_reads = []

    def writer(tag):
        for i in range(N_WRITES_PER_WRITER):
            try:
                appconfig.save_config({"gh_branch": "%s-%d" % (tag, i)})
            except Exception as exc:  # искаме да видим ВСЯКО изключение, не само OSError
                errors.append((tag, i, repr(exc)))

    def reader():
        for _ in range(N_READS_PER_READER):
            try:
                cfg = appconfig.load_config()
                # load_config трябва ВИНАГИ да върне валиден речник с
                # известните ключове — никога частично/повредено съдържание.
                if "db_path" not in cfg or "gh_branch" not in cfg:
                    corrupted_reads.append(dict(cfg))
            except Exception as exc:
                corrupted_reads.append(repr(exc))

    threads = (
        [threading.Thread(target=writer, args=("a",)),
         threading.Thread(target=writer, args=("b",))]
        + [threading.Thread(target=reader) for _ in range(2)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], "конкурентен save_config гръмна: %r" % (errors,)
    assert corrupted_reads == [], "конкурентен load_config видя повредено четене: %r" % (corrupted_reads,)

    # Файлът съществува и е синтактично коректен JSON (не осакатен наполовина).
    with open(cfg_path, encoding="utf-8") as f:
        final = json.load(f)
    assert final["gh_branch"].startswith(("a-", "b-"))

    # Никакви осиротели временни файлове (mkstemp почиства при успех, а при
    # OSError на os.replace/fsync ги трие изрично).
    leftovers = [p for p in os.listdir(str(tmp_path)) if p != "pacho_config.json"]
    assert leftovers == [], "останали временни файлове след записите: %r" % (leftovers,)


def test_contrast_theme_filter_chip_x_is_visible():
    """Находка №3: в контрастната тема и --border, и --fg-soft са жълти —
    без специално правило „×“-ът на филтъра изчезва (жълто на жълто).
    Пазим конкретното CSS правило, не само че темата съществува."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "style.css"), encoding="utf-8") as fh:
        css = fh.read()
    idx = css.find(':root[data-theme="contrast"] .filter-chip .filter-chip-x')
    assert idx != -1, "липсва контрастно правило за filter-chip-x (находка №3)"
    block = css[idx: css.find("}", idx) + 1]
    assert "background: #000" in block
    assert "var(--accent)" in block
