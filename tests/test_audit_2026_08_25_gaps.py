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

import pytest

import config as appconfig


def test_concurrent_save_config_never_crashes_or_corrupts(tmp_path, monkeypatch):
    """Находка №1: няколко нишки викат save_config/load_config почти
    едновременно (симулира мрежов режим, в който админ пази системни
    настройки, докато друга нишка чете конфигурацията). Преди поправката
    споделеният `CONFIG_PATH + ".tmp"` водеше до FileNotFoundError от
    `os.replace` и до повредени/непълни прочити; лошия случай — крайното
    състояние изгубено. Тук очакваме нула грешки, нула повредени четения, и
    валидно финално състояние.

    Бележка (25.08.2026): преди тук писателите мутираха `gh_branch`; след
    премахването на GitHub синхронизацията този ключ вече не съществува,
    затова тестът мутира реалния текстов ключ `db_path` — самата проверка
    (конкурентност на save/load) е абсолютно същата."""
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
                appconfig.save_config({"db_path": "%s-%d" % (tag, i)})
            except Exception as exc:  # искаме да видим ВСЯКО изключение, не само OSError
                errors.append((tag, i, repr(exc)))

    def reader():
        for _ in range(N_READS_PER_READER):
            try:
                cfg = appconfig.load_config()
                # load_config трябва ВИНАГИ да върне валиден речник с
                # известните ключове — никога частично/повредено съдържание.
                if "db_path" not in cfg or "network_mode" not in cfg:
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
    assert final["db_path"].startswith(("a-", "b-"))

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


# ---------------------------------------------------------------- №1-Б
# Одит (26.08.2026): доуточнение на находка №1 след истински Windows CI.
#
# Първата поправка сериализираше само ПИСАЧИТЕ. На POSIX това стига —
# rename() върху отворен файл минава. На Windows НЕ минава: `os.replace`
# гърми с PermissionError [WinError 5], докато който и да е държи целевия
# файл отворен, значи всеки конкурентен ЧЕТЕЦ проваляше записа на админа.
# Тестът по-горе (test_concurrent_save_config_never_crashes_or_corrupts) го
# улови на windows-latest runner; тези тук пазят самия механизъм и се
# изпълняват НА ВСЯКА платформа (без да зависят от Windows семантиката).

def test_load_config_reads_under_the_same_lock_as_save(tmp_path, monkeypatch):
    """Механизмът, който затваря Windows дупката: четенето ДЪРЖИ същия
    катинар като записа. Ако някой го премахне, писачът пак ще може да
    замени файла изпод отворен четец — невидимо на Linux, счупено на
    Windows."""
    cfg_path = os.path.join(str(tmp_path), "pacho_config.json")
    monkeypatch.setattr(appconfig, "CONFIG_PATH", cfg_path)
    appconfig.save_config({"db_path": "seed"})

    started = threading.Event()
    finished = threading.Event()

    def reader():
        started.set()
        appconfig.load_config()
        finished.set()

    # Държим катинара — четецът трябва да ЧАКА, не да мине покрай него.
    with appconfig._config_lock:
        t = threading.Thread(target=reader)
        t.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.5), (
            "load_config чете БЕЗ катинара — на Windows конкурентен запис "
            "гърми с PermissionError (находка №1-Б)")
    t.join(timeout=5)
    assert finished.is_set(), "четецът трябва да продължи след пускането на катинара"


def test_replace_retries_when_windows_denies_access(tmp_path, monkeypatch):
    """`os.replace` с „Access is denied“ (чужд процес държи файла отворен —
    друг компютър на мрежовия дял, антивирусна, индексатор) се повтаря
    накратко, вместо да се предаде на първия опит."""
    monkeypatch.setattr(appconfig, "_REPLACE_RETRY_SLEEP", 0)
    src_path = os.path.join(str(tmp_path), "src.tmp")
    dst_path = os.path.join(str(tmp_path), "dst.json")
    with open(src_path, "w", encoding="utf-8") as f:
        f.write("{}")

    calls = []
    real_replace = os.replace

    def flaky_replace(a, b):
        calls.append((a, b))
        if len(calls) < 3:            # първите два опита „забранени“
            raise PermissionError(13, "Access is denied")
        return real_replace(a, b)

    monkeypatch.setattr(appconfig.os, "replace", flaky_replace)
    appconfig._replace_with_retry(src_path, dst_path)

    assert len(calls) == 3, "заетият файл трябва да се опита повторно"
    assert os.path.exists(dst_path), "след успешния опит файлът трябва да е на място"


def test_replace_gives_up_and_reports_after_exhausting_retries(tmp_path, monkeypatch):
    """Трайно заключен файл НЕ се мълчи безкрайно — след опитите грешката
    излиза нормално (извикващият я логва, старите настройки остават)."""
    monkeypatch.setattr(appconfig, "_REPLACE_RETRY_SLEEP", 0)
    monkeypatch.setattr(appconfig, "_REPLACE_RETRIES", 3)

    calls = []

    def always_denied(a, b):
        calls.append(1)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(appconfig.os, "replace", always_denied)
    with pytest.raises(PermissionError):
        appconfig._replace_with_retry("a", "b")
    assert len(calls) == 3, "точно толкова опита, колкото са зададени"


def test_survives_windows_replace_semantics_simulated_on_any_platform(tmp_path, monkeypatch):
    """НАЙ-важният от тримата: възпроизвежда Windows поведението НА ВСЯКА
    платформа, за да не чакаме пак Build and Release, за да научим, че
    поправката е непълна.

    Правилото на Windows, което Linux няма: `os.replace` върху целеви файл,
    който КОЙТО И ДА Е държи отворен, гърми с `PermissionError [WinError 5]`.
    Тук го налагаме изкуствено — броим отворените четци на CONFIG_PATH и
    отказваме преименуването, докато има поне един.

    `_REPLACE_RETRIES = 1` нарочно: изключваме мрежата за безопасност на
    повторните опити, за да проверим точно КАТИНАРА (той е механизмът срещу
    вътрешнопроцесните четци; повторните опити пазят от ЧУЖДИ процеси и се
    проверяват отделно по-горе)."""
    import builtins

    cfg_path = os.path.join(str(tmp_path), "pacho_config.json")
    monkeypatch.setattr(appconfig, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(appconfig, "_REPLACE_RETRIES", 1)
    appconfig.save_config({"db_path": "seed"})

    open_readers = {"n": 0}
    counter_lock = threading.Lock()
    real_open, real_replace = builtins.open, os.replace

    class _CountedFile:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self._fh.__enter__()

        def __exit__(self, *exc):
            try:
                return self._fh.__exit__(*exc)
            finally:
                with counter_lock:
                    open_readers["n"] -= 1

    def counting_open(path, mode="r", *a, **kw):
        is_cfg_read = (os.path.abspath(str(path)) == os.path.abspath(cfg_path)
                       and "w" not in mode and "a" not in mode)
        fh = real_open(path, mode, *a, **kw)
        if not is_cfg_read:
            return fh
        with counter_lock:
            open_readers["n"] += 1
        return _CountedFile(fh)

    def windows_like_replace(a, b):
        with counter_lock:
            busy = open_readers["n"] > 0
        if busy:
            raise PermissionError(13, "Access is denied")
        return real_replace(a, b)

    # Инжектираме САМО в модула config (Python търси в globals преди builtins),
    # за да не пипаме отварянето на файлове никъде другаде в процеса.
    monkeypatch.setattr(appconfig, "open", counting_open, raising=False)
    monkeypatch.setattr(appconfig.os, "replace", windows_like_replace)

    errors, stop = [], threading.Event()

    def writer(tag):
        for i in range(25):
            try:
                appconfig.save_config({"db_path": "%s-%d" % (tag, i)})
            except Exception as exc:
                errors.append((tag, i, repr(exc)))

    def reader():
        while not stop.is_set():
            appconfig.load_config()

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for t in readers:
        t.start()
    writers = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join(timeout=5)

    assert errors == [], (
        "при Windows семантика записът гърми, защото четец държи файла "
        "отворен — катинарът в load_config е задължителен (находка №1-Б): %r"
        % (errors,))
