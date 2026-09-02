# -*- coding: utf-8 -*-
"""Десети одит (02.09.2026) — по един заключващ тест на находка.

Разделението е като при предишните одити: за находките в Python се
проверява самото ПОВЕДЕНИЕ (изпълнява се истинската функция), а за
находките в `static/app.js` — механизмът в самия файл, защото JS кодът не
може да бъде изпълнен от pytest, а e2e тестовете покриват само сценария,
не и точната числена стойност.
"""
import os
import re
import sqlite3

import pytest

import appcore
import materials
import routes_invoices
import routes_pallet_extra
import updater


APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "static", "app.js")


def _app_js():
    with open(APP_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def _updater_source_without_comments():
    """Изходният код на updater.py БЕЗ редовете-коментари — самите коментари
    цитират дословно сгрешения ред („echo %~3>“), за да остане разбираемо
    какво е било, и иначе биха давали лъжливо съвпадение."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "updater.py")
    with open(path, "r", encoding="utf-8") as fh:
        return "\n".join(line for line in fh.read().splitlines()
                         if not line.lstrip().startswith("#"))


# --------------------------------------------------------------- находка №1
def test_scaled_bigint_pads_beyond_ten_decimal_places():
    """Находка №1 (ВИСОКА): `_scaledBigInt` допълваше с ФИКСИРАН литерал от
    10 нули, затова при колона с повече от 10 знака всяка ДРУГА стойност
    излизаше мащабирана с 10^10 вместо с 10^maxScale. Един ред с
    „2.9000000000000004“ (какъвто Excel клетка с формула редовно държи)
    правеше екранната сума 2.900015 при сървърни 17.9."""
    src = _app_js()
    match = re.search(r"function _scaledBigInt\(text, scale\) \{(.*?)\n\}", src, re.S)
    assert match, "функцията _scaledBigInt не е намерена в static/app.js"
    body = match.group(1)
    assert '"0000000000"' not in body, (
        "находка №1: върнато е фиксираното допълване с 10 нули — при колона "
        "с повече от 10 знака сумата на екрана се разминава със сървъра")
    assert "scale - fracPart.length" in body, (
        "находка №1: допълването трябва да е с толкова нули, колкото искА "
        "`scale`, а не с постоянен брой")


def test_python_side_of_finding_1_is_the_reference_value():
    """Другата половина на находка №1: стойността, с която JS трябва да
    съвпадне. Ако този резултат някога се промени, JS поправката трябва да
    се преразгледа заедно с него."""
    items = [{"qty": "2.9000000000000004"}, {"qty": "10"}, {"qty": "5"}]
    assert appcore.pallet_total_qty(items) == "17.9"
    items2 = [{"qty": "0.30000000000000004"}, {"qty": "2"}, {"qty": "5"}]
    assert appcore.pallet_total_qty(items2) == "7.3"


# --------------------------------------------------------------- находка №2
def _catalog_con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE materials (code TEXT PRIMARY KEY, description TEXT,"
                " net_weight TEXT, updated_at TEXT)")
    return con


def _row(con):
    return dict(con.execute("SELECT code, description, net_weight FROM materials").fetchone())


def test_partial_import_does_not_wipe_existing_net_weight():
    """Находка №2 (ВИСОКА): файл с код + описание, но БЕЗ разпозната колона
    за тегло, се приема от парсера и дава `weight = ""` за всеки ред, а
    upsert-ът презаписваше безусловно — тоест допълнителен списък „само
    описанията“ тихо зануляваше нетното тегло на всеки засегнат материал.
    Оттам „Общо нето тегло“ на опаковъчния лист, който придружава ЧМР при
    митницата, излиза занижено, а `materials_merged_backup` не покрива
    този път."""
    con = _catalog_con()
    materials.replace_catalog(con, [("1TGB110025P1204", "Profile", "0.0875")])
    materials.replace_catalog(con, [("1TGB110025P1204", "Profile v2", "")])
    assert _row(con)["net_weight"] == "0.0875", (
        "находка №2: непълен импорт (само описания) изтри нетното тегло")
    assert _row(con)["description"] == "Profile v2", (
        "непразна нова стойност трябва да продължи да обновява")


def test_partial_import_does_not_wipe_existing_description():
    """Огледалният случай на находка №2: файл с код + тегло изтриваше
    описанията."""
    con = _catalog_con()
    materials.replace_catalog(con, [("ABC-1", "Profile", "0.0875")])
    materials.replace_catalog(con, [("ABC-1", "", "0.0910")])
    assert _row(con)["description"] == "Profile", (
        "находка №2: непълен импорт (само тегла) изтри описанието")
    assert _row(con)["net_weight"] == "0.0910"


def test_full_import_still_overwrites_both_fields():
    """Пазач срещу свръхпоправка: пълен файл трябва да продължи да
    презаписва — иначе поправката на находка №2 би направила справочника
    неактуализируем."""
    con = _catalog_con()
    materials.replace_catalog(con, [("ABC-1", "Profile", "0.0875")])
    materials.replace_catalog(con, [("ABC-1", "Profile v3", "0.1000")])
    assert _row(con) == {"code": "ABC-1", "description": "Profile v3",
                         "net_weight": "0.1000"}


# --------------------------------------------------------------- находка №3
def test_unreadable_packing_total_is_reported_as_mismatch():
    """Находка №3: `typed is None` (въведено, но нечетимо) се третираше
    наравно с „не е въведено нищо“ и НЕ даваше предупреждение — тоест
    най-очевидно сбърканата форма беше единствената непокрита. Стойността
    се отпечатваше буквално в реда ОБЩО/TOTAL."""
    data = {"items": [{"net": "1.5"}, {"net": "1.375"}], "total_net": "1.234,56"}
    out = appcore.packing_total_mismatches(data)
    assert out, ("находка №3: нечетимо въведено общо („1.234,56“) мина без "
                 "нито едно предупреждение")
    label, typed, computed = out[0]
    assert typed == "1.234,56"
    assert computed == "2.875"


@pytest.mark.parametrize("typed,expect_warning", [
    ("2.875", False),   # вярно — без предупреждение
    ("", False),        # непопълнено обобщение не е грешка
    ("1.11", True),     # грешно число — предупреждение (както и преди)
    ("nan", True),      # нечетимо — новото покритие
])
def test_packing_total_check_matrix(typed, expect_warning):
    data = {"items": [{"net": "1.5"}, {"net": "1.375"}], "total_net": typed}
    assert bool(appcore.packing_total_mismatches(data)) is expect_warning


# --------------------------------------------------------------- находка №4
@pytest.mark.parametrize("cellstr", [
    routes_pallet_extra._cellstr,
    routes_invoices._cellstr,
    materials._cellstr,
])
def test_excel_float_noise_is_normalised(cellstr):
    """Находка №4: дробният float се връщаше СУРОВ, тоест клетка с формула
    („Open Qty“ = разлика) даваше „2.9000000000000004“, а `fmt_num` с
    decimals=None нарочно пази въведената точност — този запис с 16 знака
    се печаташе буквално в колоната за количество на документ за клиента и
    за митницата. Беше и спусъкът на находка №1."""
    assert cellstr(2.9000000000000004) == "2.9"
    assert cellstr(0.30000000000000004) == "0.3"
    # реалната точност НЕ се губи
    assert cellstr(0.0125) == "0.0125"
    # целите числа остават както преди
    assert cellstr(10.0) == "10"
    # текстът не се пипа
    assert cellstr(" ABC-1 ") == "ABC-1"


@pytest.mark.parametrize("cellstr", [
    routes_pallet_extra._cellstr,
    routes_invoices._cellstr,
    materials._cellstr,
])
def test_tiny_value_stays_raw_so_the_operator_is_warned(cellstr):
    """Предпазната клауза на находка №4: закръгляне до 6 знака би
    превърнало 8.7135e-09 в „0“ — тихо количество нула на бланка. Такава
    стойност остава сурова, за да я хване `unparsable_item_rows` и
    операторът да получи предупреждение."""
    assert cellstr(8.7135e-09) == "8.7135e-09"
    assert appcore._parse_decimal("8.7135e-09") is None, (
        "предпазната клауза разчита, че такъв запис се отхвърля от "
        "числовата валидация и затова стига до предупреждението")


# --------------------------------------------------------------- находка №5
def test_failed_marker_is_written_with_a_space_before_the_redirect():
    """Находка №5 (ВИСОКА): `echo %~3> "…"` — cmd.exe разширява %~3 първо и
    чак после разбира пренасочването, а цифра, ЗАЛЕПЕНА вляво до „>“, е
    номер на дескриптор. Версията винаги завършва на цифра, затова редът се
    изпълняваше като `echo 3.69.` с `2> "…"`: маркерът се създаваше ПРАЗЕН,
    `read_failed_install_version()` връщаше None и пазачът срещу безкрайния
    цикъл сваляне↔рестарт никога не се задействаше."""
    src = _updater_source_without_comments()
    assert "echo %~3>" not in src, (
        "находка №5: цифрата на версията отново е залепена до „>“ — cmd.exe "
        "я чете като номер на дескриптор и маркерът остава празен")
    assert "echo %~3 >" in src, (
        "находка №5: между %~3 и „>“ трябва да има интервал")


def test_batch_retry_budget_outlasts_the_slowest_shutdown():
    """Находка №9: изходът на стария процес може да отнеме до ~16.5 сек.
    (Timer(1.5) + remote_tunnel.stop(): wait 10 сек. + kill/wait 5 сек.), а
    бюджетът на скрипта беше 20 опита × ~1 сек. — под 4 секунди запас, който
    `move` през SMB плюс антивирус изяжда. Тогава bat-ът стартира СТАРОТО
    .exe и (заедно с находка №5) се влиза в безкраен цикъл."""
    src = _updater_source_without_comments()
    match = re.search(r"if %TRIES% LSS (\d+) goto retry", src)
    assert match, "циклите за повторен опит в bat скрипта не са намерени"
    assert int(match.group(1)) >= 40, (
        "находка №9: бюджетът за повторни опити (%s × ~1 сек.) е под ~16.5 "
        "сек. най-бавен изход плюс запас" % match.group(1))


# --------------------------------------------------------------- находка №6
def test_update_staging_files_are_unique_per_machine():
    """Находка №6 (ВИСОКА): целият междинен запас на обновяването
    (`<exe>.new`, скриптът, маркерът) се пишеше до .exe-то — тоест в
    СПОДЕЛЕНАТА мрежова папка, а единственото взаимно изключване е нишков
    lock в рамките на ЕДИН процес. Машина B триеше вече проверения файл на
    машина A, а cmd.exe на A местеше половин свалeния файл на B върху
    общото .exe — всяка станция в офиса стартира отрязано .exe."""
    suffix = updater._machine_suffix()
    assert suffix and re.fullmatch(r"[0-9a-f]{8}", suffix), (
        "отпечатъкът на машината трябва да е къс шестнайсетичен низ (чист "
        "ASCII — името на компютъра под Windows може да е на кирилица)")
    assert suffix in updater._failed_marker_name()
    assert updater._failed_marker_name().endswith(".txt")


def test_machine_suffix_differs_between_machines(monkeypatch):
    """Смисълът на находка №6: две различни машини трябва да получат
    РАЗЛИЧНИ имена — иначе поправката не решава нищо."""
    import platform
    monkeypatch.setattr(platform, "node", lambda: "STANCIA-A")
    a = updater._machine_suffix()
    monkeypatch.setattr(platform, "node", lambda: "STANCIA-B")
    b = updater._machine_suffix()
    assert a != b, "находка №6: две станции получават едно и също име"


# --------------------------------------------------------------- находка №8
def test_checksum_fetch_failure_aborts_instead_of_skipping_verification(monkeypatch):
    """Находка №8: всяка грешка при изтеглянето на SHA256SUMS.txt се
    поглъщаше и връщаше СЪЩОТО None като „този релийз няма манифест“, а
    install_update проверява `elif expected_sha256:` — тоест ~20 MB .exe се
    инсталираше само срещу размер и „MZ“. Сега провалът отлага
    обновяването."""
    def _boom(*a, **kw):
        raise OSError("временно недостъпен")
    monkeypatch.setattr(updater.net, "urlopen", _boom)
    assets = [{"name": updater.CHECKSUMS_ASSET_NAME,
               "browser_download_url": "https://example.invalid/SHA256SUMS.txt"}]
    with pytest.raises(RuntimeError):
        updater._fetch_expected_checksum(assets, timeout=1)


def test_release_without_checksums_manifest_still_works(monkeypatch):
    """Пазач срещу свръхпоправка на находка №8: стар релийз, който изобщо
    НЯМА такъв файл, трябва да продължи да минава — там не се стига до
    мрежова заявка."""
    def _boom(*a, **kw):  # pragma: no cover - не бива да бъде извикано
        raise AssertionError("не бива да има мрежова заявка")
    monkeypatch.setattr(updater.net, "urlopen", _boom)
    assert updater._fetch_expected_checksum([{"name": "PachoLogistic.exe"}], timeout=1) is None


# -------------------------------------------------------------- находка №10
def test_auto_update_loop_can_be_disabled_by_env(monkeypatch):
    """Находка №10: smoke тестът в release.yml пуска прясно компилираното
    .exe на windows-latest, тоест is_frozen_windows() е True и цикълът
    тръгва наистина — при определени номера на версии подменя самото
    `dist\\PachoLogistic.exe`, файла, който следващите стъпки хешират и
    публикуват. `kill` не помага: подмяната я прави откачен cmd.exe."""
    started = []
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Thread",
                        lambda *a, **kw: started.append(kw) or _NoThread())
    monkeypatch.setenv("PACHO_DISABLE_AUTO_UPDATE", "1")
    updater.start_auto_update_loop(lambda: False)
    assert not started, ("находка №10: цикълът тръгна въпреки "
                         "PACHO_DISABLE_AUTO_UPDATE")


class _NoThread(object):
    def start(self):  # pragma: no cover - не бива да бъде достигнато
        raise AssertionError("нишката не бива да стартира")


def test_release_workflow_sets_the_disable_flag():
    """Другата половина на находка №10 — самата стъпка в CI."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "release.yml")
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert "PACHO_DISABLE_AUTO_UPDATE" in src, (
        "находка №10: smoke тестът пак пуска живия updater срещу истинските "
        "релийзи, докато билдва този")


# -------------------------------------------------------------- находка №11
def test_startup_log_name_is_per_machine():
    """Находка №11: логът беше ЕДИН файл до .exe-то, тоест в споделената
    папка всяка станция пишеше в него в режим „добавяне“. Ротацията
    (`os.replace`) не може да успее под Windows, докато друга машина държи
    файла отворен — таванът от 20 MB не важеше точно там, където логът расте
    най-бързо; а append през SMB не е атомарен между клиенти, тоест редовете
    се преплитат."""
    import platform

    import app
    name = app._startup_log_name()
    assert re.fullmatch(r"pacho_startup_[0-9a-f]{8}\.log", name), name
    # Същината: две станции трябва да получат РАЗЛИЧНИ файлове — иначе
    # поправката не решава нищо. (monkeypatch не се ползва, защото app вече
    # е импортиран и функцията чете platform.node() при всяко извикване.)
    real = platform.node
    try:
        platform.node = lambda: "STANCIA-A"
        a = app._startup_log_name()
        platform.node = lambda: "STANCIA-B"
        b = app._startup_log_name()
    finally:
        platform.node = real
    assert a != b, ("находка №11: две станции пишат в ЕДИН файл — ротацията "
                    "не може да успее, а редовете се преплитат през SMB")


def test_ci_still_finds_the_startup_log():
    """Находка №11 счупи буквалното име, което release.yml изкарваше при
    провал на smoke теста — без този пазач диагностиката в CI мълчи."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "release.yml")
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert "pacho_startup*.log" in src, (
        "находка №11: release.yml още търси точното старо име на лога")


# -------------------------------------------------------------- находка №12
def test_cloudflared_download_uses_a_unique_temp_name():
    """Находка №12: временното име беше фиксирано (`<път>.download`), а при
    компилиран .exe папката е споделената — две станции пишеха в един файл и
    всяка правеше `os.replace` върху частично изтегления файл на другата,
    тоест проверката по размер/магически байтове се е случила върху ДРУГ
    файл."""
    import remote_tunnel
    src = open(remote_tunnel.__file__.replace(".pyc", ".py"), "r", encoding="utf-8").read()
    assert 'tmp_path = path + ".download"' not in src, (
        "находка №12: върнато е фиксираното временно име")
    assert "os.getpid()" in src, (
        "находка №12: временното име трябва да е уникално за процеса/машината")


def test_cloudflared_replace_failure_falls_back_to_the_valid_existing_binary(monkeypatch, tmp_path):
    """Втората половина на находка №12: `os.replace` върху cloudflared.exe,
    който друга машина изпълнява, се проваля под Windows (sharing violation)
    и грешката излизаше като „Неуспешно изтегляне на компонента за
    отдалечен достъп“ — заблуждаващо, защото изтеглянето е успяло."""
    import remote_tunnel
    good = tmp_path / "cloudflared"
    good.write_bytes(remote_tunnel._expected_magic() + b"\0" * 200000)
    assert remote_tunnel._binary_looks_valid(str(good))
    small = tmp_path / "malko"
    small.write_bytes(b"x" * 10)
    assert not remote_tunnel._binary_looks_valid(str(small))
    assert not remote_tunnel._binary_looks_valid(str(tmp_path / "nyama"))
