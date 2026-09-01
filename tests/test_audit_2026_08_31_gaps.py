# -*- coding: utf-8 -*-
"""Собствено допълнително покритие за v3.69.0 (осми одит, 31.08.2026).

Проверих независимо (делегиран задълбочен преглед на целия diff + лична
проверка на резултата): архитектският патч затваря 20 находки коректно, но
самата поправка на находка №12 ("защита срещу второ копие") оставяше ЕДНА
непокрита половина, без собствен тест никъде в repo-то:

Второто копие (отказано от single_instance.acquire()) отваряше прозорец/
браузър към КОНФИГУРИРАНИЯ мрежов порт (appconfig.get_network_port), не към
РЕАЛНО използвания. Ако конфигурираният порт е бил зает от нещо друго при
старта на първото копие, net.find_available_port() го е преместила на друг,
свободен порт (appcore.set_runtime_port() пази точно тази разлика — виж
находка №10 от 12.08.2026, за system_remote_start()/updating.html). Тази
стойност е чисто in-process (модулен dict в appcore) — второто, отделен
процес, копие няма достъп до нея и сляпо отваря конфигурирания порт: адрес,
на който никой не слуша, вместо истинския прозорец на работещото копие.
Точно обратното на целта на находка №12.

Затворено с: single_instance.set_running_port()/read_running_port() —
реалният порт се пази в самия катинарен файл (текстов ред до pid-а),
четим от втория процес без да му е нужен самият катинар."""
import io
import os
import zipfile

import single_instance


def test_running_port_round_trips_through_the_lock_file(tmp_path, monkeypatch):
    """Механизмът: процесът, който държи катинара, записва реалния порт;
    друг процес (без катинара) го чете обратно от същия файл."""
    d = str(tmp_path)
    monkeypatch.setattr(single_instance, "_lock_file", None)
    assert single_instance.acquire(directory=d) is True
    try:
        single_instance.set_running_port(5123)
        # Четенето не изисква катинара — симулираме „втори процес“, като
        # просто извикваме read_running_port(); той чете само СЪДЪРЖАНИЕТО
        # на файла, не се опитва да го заключи.
        assert single_instance.read_running_port(directory=d) == 5123
    finally:
        single_instance.release()


def test_read_running_port_falls_back_to_none_without_a_port_written(tmp_path, monkeypatch):
    """Стар формат на катинарния файл (само pid, записан от версия отпреди
    тази поправка) или липсващ файл изобщо — извикващият пада обратно на
    конфигурирания порт, не гърми."""
    d = str(tmp_path)
    assert single_instance.read_running_port(directory=d) is None  # файлът още не съществува

    monkeypatch.setattr(single_instance, "_lock_file", None)
    assert single_instance.acquire(directory=d) is True
    try:
        # acquire() сам по себе си пише само " pid=N" — БЕЗ порт.
        assert single_instance.read_running_port(directory=d) is None
    finally:
        single_instance.release()


def test_second_launch_prefers_the_lock_files_port_over_the_configured_one(tmp_path, monkeypatch):
    """Реалният сценарий от одита: конфигурираният порт (5000) е зает от
    нещо друго, работещото копие реално слуша на 5001 (записан в катинара).
    Второто стартиране трябва да предпочете 5001, не сляпо да прочете 5000
    от конфигурацията."""
    import config as appconfig

    cfg_path = os.path.join(str(tmp_path), "pacho_config.json")
    monkeypatch.setattr(appconfig, "CONFIG_PATH", cfg_path)
    appconfig.save_config({"network_port": 5000})

    lock_dir = str(tmp_path)
    monkeypatch.setattr(single_instance, "_lock_file", None)
    assert single_instance.acquire(directory=lock_dir) is True
    try:
        single_instance.set_running_port(5001)

        # Точно логиката от app.py при отказано второ стартиране.
        cfg = appconfig.load_config()
        running_port = (single_instance.read_running_port(directory=lock_dir)
                        or appconfig.get_network_port(cfg))
        assert running_port == 5001, (
            "второто копие би отворило конфигурирания (свободен, никой не "
            "слуша) порт вместо реалния работещ адрес")
    finally:
        single_instance.release()


# ------------------------------------------------------------------- №7-Б
def _zip_bomb(member_size=300 * 1024 * 1024):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * member_size)
    return buf.getvalue()


def test_materials_import_shows_the_specific_zip_bomb_message(admin_client):
    """Находка №7 (доуточнение): `materials.parse_catalog_xlsx` вика
    `ensure_xlsx_within_limits` вътре в себе си (коментарът там изрично
    твърди „извикващият маршрут я показва като обикновено съобщение за
    грешка“), но `routes_materials.materials_import` хващаше само общия
    `except Exception` — операторът виждаше подвеждащото „файлът не може
    да бъде прочетен... уверете се, че е валиден“, макар файлът да Е
    валиден, просто твърде голям. `routes_invoices.py`/
    `routes_pallet_extra.py` вече показват КОНКРЕТНОТО съобщение (лимитите
    в MB + съвета да се раздели файлът) — справочникът с материали беше
    единственият от трите Excel импорта без тази конкретика."""
    from conftest import post_with_csrf

    bomb = _zip_bomb()
    resp = post_with_csrf(
        admin_client, "/materials/import", {"excel_file": (io.BytesIO(bomb), "spravochnik.xlsx")},
        csrf_source_url="/materials", follow_redirects=True, content_type="multipart/form-data",
    )
    body = resp.get_data(as_text=True)
    assert "MB" in body, (
        "справочникът с материали не показва конкретното съобщение за "
        "прекалено голям файл (само общото „не може да бъде прочетен“)")
    assert "не може да бъде прочетен" not in body
