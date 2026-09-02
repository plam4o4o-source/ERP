# -*- coding: utf-8 -*-
"""Регресионни тестове за седмия дълбочинен одит (29.08.2026), v3.67.1.

Находките тук са от одита ERP_ОДИТ_2026_08_29.md. Всяка е проверена с
реално изпълнение ПРЕДИ поправката (виж коментарите в самия код) и всеки
тест по-долу пази конкретния механизъм на поправката, не просто общото ѝ
намерение.
"""
import hashlib
import os
import re
import types

import pytest

import updater


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ==================================================================== №1
# Авто-обновяването беше МЪРТВО при кирилски път на инсталация.


class _FakeDownload:
    """Минимален отговор за net.urlopen — колкото install_update ползва."""

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


def _run_install_into(tmp_path, monkeypatch, subdir):
    """Изпълнява install_update с „инсталация“ в подадената подпапка и
    връща (пътя до .bat, прихванатите Popen аргументи)."""
    install_dir = tmp_path / subdir
    install_dir.mkdir(parents=True)
    fake_exe = install_dir / "PachoLogistic.exe"
    fake_exe.write_bytes(b"MZ" + b"\x00" * 10)

    monkeypatch.setattr(updater.sys, "executable", str(fake_exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Timer", lambda *a, **k: types.SimpleNamespace(
        start=lambda: None))

    captured = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda args, **kw: captured.setdefault("args", args))

    payload = b"MZ" + b"\x00" * 1_100_000
    monkeypatch.setattr(updater.net, "urlopen",
                        lambda req, timeout=120: _FakeDownload(payload))

    updater.install_update("http://example.invalid/x.exe",
                           expected_sha256=hashlib.sha256(payload).hexdigest())
    return (str(install_dir / ("pacho_update_%s.bat" % updater._machine_suffix())),
            captured.get("args"), str(fake_exe))


def test_install_update_works_when_install_path_has_cyrillic(tmp_path, monkeypatch):
    """Находка №1 (ВИСОКА): по подразбиране програмата се инсталира в
    %localappdata%\\Programs\\PachoLogistic, тоест C:\\Users\\<потребител>\\… —
    а за българските потребители потребителското име в Windows е КИРИЛСКО.

    Скриптът за рестарт се записваше с encoding="ascii", докато вътре в него
    стоеше sys.executable → UnicodeEncodeError СЛЕД успешно изтегляне и
    проверка на ~20 MB. Изключението се логваше тихо, версията влизаше в
    _failed_install_versions и обновяването не се пробваше повече: авто-
    обновяването беше мъртво за мнозинството реални инсталации.

    Старият тест не го хващаше, защото ползва ASCII tmp_path — затова тук
    пътят е изрично кирилски."""
    bat_path, args, fake_exe = _run_install_into(
        tmp_path, monkeypatch, os.path.join("Потребители", "Пламен", "ПачоЛогистик"))

    assert os.path.exists(bat_path), (
        "скриптът за рестарт не бе записан — обновяването пада при кирилски път")
    assert args and args[0] == "cmd.exe"


def test_restart_script_content_is_pure_ascii(tmp_path, monkeypatch):
    """Механизмът на поправката: пътищата НЕ се вграждат в текста на .bat —
    подават се като аргументи. Затова съдържанието е чисто ASCII, независимо
    къде е инсталирана програмата (и кодировката на файла спира да има
    значение изобщо)."""
    bat_path, _args, _exe = _run_install_into(
        tmp_path, monkeypatch, os.path.join("Потребители", "Пламен", "ПачоЛогистик"))

    raw = open(bat_path, "rb").read()
    raw.decode("ascii")  # хвърля UnicodeDecodeError, ако някой пак вгради път
    assert b"\xd0" not in raw, "в скрипта е попаднал кирилски байт"


def test_restart_script_receives_both_paths_as_arguments(tmp_path, monkeypatch):
    """Новото и текущото .exe пътуват като аргументи на процеса (Unicode
    през CreateProcessW), а не като текст във файла — това е причината
    кирилицата да минава непокътната."""
    bat_path, args, fake_exe = _run_install_into(
        tmp_path, monkeypatch, os.path.join("Потребители", "Пламен", "ПачоЛогистик"))

    assert list(args[:3]) == ["cmd.exe", "/c", bat_path]
    assert args[3] == fake_exe + "." + updater._machine_suffix() + ".new", \
        "първи аргумент трябва да е новото .exe"
    assert args[4] == fake_exe, "втори аргумент трябва да е текущото .exe"


def test_restart_script_keeps_the_retry_replace_logic(tmp_path, monkeypatch):
    """Поправката сменя САМО откъде идват пътищата — самият цикъл за
    подмяна (ping-изчакване, до 20 опита, лог, старт на новата версия,
    самоизтриване) трябва да остане непокътнат."""
    bat_path, _args, _exe = _run_install_into(
        tmp_path, monkeypatch, os.path.join("Потребители", "Пламен", "ПачоЛогистик"))
    bat = open(bat_path, encoding="ascii").read()

    assert 'move /y "%~1" "%~2"' in bat
    # Одит (02.09.2026, находка №9): бюджетът е вдигнат от 20 на 60 опита,
    # защото изходът на стария процес може да отнеме до ~16.5 сек. Тестът
    # проверява, че БРОЯЧЪТ съществува, а не точното число (то си има
    # собствен тест: test_batch_retry_budget_outlasts_the_slowest_shutdown).
    assert re.search(r"if %TRIES% LSS \d+ goto retry", bat), "броячът на опитите е счупен"
    assert "ping -n 2 127.0.0.1 >nul" in bat, "изчакването преди подмяна липсва"
    assert 'start "" "%~2"' in bat, "новата версия не се стартира"
    assert 'del "%~f0"' in bat, "скриптът не се самоизтрива"
    assert '"%~dp0pacho_update.log"' in bat, "логът вече не се пише до програмата"


def test_restart_script_is_not_written_as_ascii(tmp_path, monkeypatch):
    """Втора защита: дори ако някой ден в скрипта попадне не-ASCII литерал,
    записът не бива да гърми — файлът се пише като UTF-8, не като ASCII."""
    # Търсим само РЕАЛНИЯ ред за запис (не обясненията в коментарите, които
    # нарочно цитират старото кодиране).
    source = _read("updater.py")
    write_lines = [ln.strip() for ln in source.splitlines()
                   if "open(bat_path" in ln and not ln.strip().startswith("#")]
    assert write_lines, "редът, който записва скрипта, изчезна"
    for line in write_lines:
        assert 'encoding="ascii"' not in line, (
            "записът на скрипта пак е с ascii — кирилски път ще гърми отново: %s" % line)
        assert 'encoding="utf-8"' in line, line


# ==================================================================== №3
# Развален (не-речников) ред правеше износа на документа НЕВЪЗМОЖЕН.

def test_parse_items_drops_non_dict_rows(flask_app):
    """Единствената точка, през която редовете влизат от формите, вече
    отсява и не-речниковите елементи, не само не-списък на горното ниво."""
    import appcore
    with flask_app.test_request_context(
            "/", method="POST",
            data={"items_json": '[{"qty": "1"}, "развален", 42, null]'}):
        items = appcore.parse_items()
    assert items == [{"qty": "1"}], "не-речниковите редове трябва да отпаднат"


def test_export_survives_a_non_dict_item_already_stored(admin_client, db_module):
    """Втора защита за ВЕЧЕ ЗАПИСАНИ документи (записани преди филтъра или
    от ръчна намеса): износът не бива да пада с 500 — преди поправката
    `it.get(...)` върху низ даваше AttributeError и износът на документа
    оставаше невъзможен завинаги."""
    import json as _json
    from conftest import post_with_csrf

    resp = post_with_csrf(admin_client, "/packing/new", {
        "receiver_name": "Получател",
        "items_json": _json.dumps([{"description": "стока", "qty": "2"}]),
    }, csrf_source_url="/packing/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    # Вкарваме развален ред ДИРЕКТНО в базата (симулира стар/повреден запис).
    con = db_module.get_db()
    row = con.execute("SELECT data FROM documents WHERE id = ?", (doc_id,)).fetchone()
    data = _json.loads(row["data"])
    data["items"] = [{"description": "стока", "qty": "2"}, "развален-ред"]
    con.execute("UPDATE documents SET data = ? WHERE id = ?",
                (_json.dumps(data, ensure_ascii=False), doc_id))
    con.commit()
    con.close()

    assert admin_client.get("/doc/%d/export.xlsx" % doc_id).status_code == 200, \
        "Excel износът пада при развален ред (находка №3)"
    assert admin_client.get("/doc/%d/export.pdf" % doc_id).status_code == 200, \
        "PDF износът пада при развален ред (находка №3)"


# ==================================================================== №4
# Сблъсък на имена на архиви между процеси на споделена папка.

def test_two_backups_in_the_same_second_get_different_files(tmp_path, db_module,
                                                            monkeypatch):
    """`_local_backup_lock` е катинар на ниво ПРОЦЕС — не достига втори
    компютър/второ копие на .exe, пишещо в СЪЩАТА споделена папка. Досега
    името беше само със секундна резолюция, значи съвпадение в една секунда
    даваше един и същ файл: двата процеса пишеха в него, а error-пътят на
    изгубилия триеше валидния архив на спечелилия."""
    import backup
    from datetime import datetime

    dest = tmp_path / "arhiv"
    dest.mkdir()

    class _FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 29, 12, 0, 0)

    monkeypatch.setattr(backup, "datetime", _FrozenTime)
    first = backup.local_backup(str(dest))
    second = backup.local_backup(str(dest))

    assert first != second, "два архива в една и съща секунда получиха едно име"
    assert os.path.exists(first) and os.path.exists(second), \
        "единият архив е бил изтрит от другия"


def test_rotation_still_recognizes_old_and_new_backup_names(tmp_path):
    """Суфиксът е НЕЗАДЪЛЖИТЕЛЕН в израза на ротацията — иначе тя щеше да
    спре да чисти вече съществуващите архиви без суфикс и те щяха да се
    трупат неограничено (проблемът, който находка В12 затвори)."""
    import backup

    old_name = "pacho_logistic_20200101_010101.db"
    new_name = "pacho_logistic_20200102_010101_abc123.db"
    assert backup._BACKUP_NAME_RE.match(old_name), "старият формат вече не се разпознава"
    assert backup._BACKUP_NAME_RE.match(new_name), "новият формат не се разпознава"
    # Чужди файлове в папката пак не се пипат.
    assert not backup._BACKUP_NAME_RE.match("ръчно_копие.db")
    assert not backup._BACKUP_NAME_RE.match("pacho_logistic_20200101_010101_ZZZZZZ.db")


# ==================================================================== №5
# Живата сума на екрана трябва да съвпада с бланката.

def test_js_column_sum_uses_the_same_order_of_operations_as_python():
    """Механизмът: колонните суми минават през sumRawDecimals (пълна
    точност, ЕДНО закръгляне накрая) — точно както appcore трупа
    decimal.Decimal и квантува веднъж. Преди това JS закръгляше всяка
    стойност поотделно и два реда по „1.2345“ даваха 2.470 на екрана срещу
    2.469 на издадения документ."""
    js = _read("static", "app.js")
    assert "function sumRawDecimals(" in js, "помощната функция за точна сума липсва"
    # И трите колонни суми я ползват (палет, опаковъчен лист, фактура).
    assert js.count("sumRawDecimals(") >= 4, \
        "не всички колонни суми минават през точното сумиране"
    # Редовите ПРОИЗВЕДЕНИЯ съзнателно си остават закръглени на ред.
    assert "multiplyDecimalScaled(it.qty, it.unit_price, 2)" in js
    assert "multiplyDecimalScaled(it.qty, it.net_weight, 3)" in js


@pytest.mark.parametrize("qtys, expected", [
    (["1.2345", "1.2345"], "2.469"),   # разминаваше се: екранът даваше 2.470
    (["0.0005", "0.0005"], "0.001"),
    (["0.0625", "0.0625"], "0.125"),
    (["0.1", "0.2"], "0.3"),
    (["10", "-3", "5"], "15"),         # отрицателните се пропускат и от двете страни
])
def test_python_column_sum_reference_values(qtys, expected):
    """Стойностите, срещу които се равнява JS страната (виж теста по-горе и
    e2e паритета) — фиксирани тук, за да не се променят незабелязано."""
    import appcore
    assert appcore.pallet_total_qty([{"qty": q} for q in qtys]) == expected


# ==================================================================== №6
# Подсказката „Сбор от редовете“ след ПРОГРАМНО добавен ред.

def test_adding_a_row_notifies_the_packing_totals_listener():
    """initPackingTotals слушаше за „items-row-added“, но то никъде не се
    излъчваше — мъртъв слушател. Затова след „Добави от палета“ подсказките
    оставаха стари, докато операторът не пипне някоя клетка."""
    js = _read("static", "app.js")
    assert 'document.addEventListener("items-row-added"' in js, "слушателят изчезна"
    assert 'new CustomEvent("items-row-added")' in js, \
        "събитието пак не се излъчва — подсказката ще застоява"
