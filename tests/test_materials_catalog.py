# -*- coding: utf-8 -*-
"""Тестове за справочника материали (materials.py + раздел „Материали“).

Заявка: „Прикачвам ти файл със съответния материал, описанието на
материала и килограми на всеки материал“ + „да не се зарежда всеки път
файла с материалите; като се зареди веднъж, да си остава зареден в
програмата“ — тоест справочникът живее в БАЗАТА, не се качва повторно при
всяка фактура. Точно това постоянство се проверява тук.
"""
import io

from openpyxl import Workbook

import materials
from conftest import post_with_csrf


def _catalog_bytes(rows, headers=("ABB part ID", "Description", "Net weight\n[KG/pc]")):
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_SAMPLE = [
    ("GLBK400002P0012", "C-PROFILE 3   1150MM", 2.21),
    ("1TFL151621P0550", "transverse section 06  folded", 2.74259375),
    ("1SRY105253P0001", "Lock shackle for MNS", 0.0646176),
]


# ---------------------------------------------------------------- четене на файла

def test_parse_catalog_reads_code_description_and_weight():
    entries = materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE))
    assert entries is not None
    assert ("GLBK400002P0012", "C-PROFILE 3   1150MM", "2.21") in entries


def test_parse_catalog_trims_floating_point_tail_of_weight():
    """Excel връща теглата като float с дълга опашка (2.74259375,
    0.0646176) — записваме ги закръглени до 6 знака, вместо суровия
    repr, който би излязъл така и на самата фактура."""
    entries = dict((c, w) for c, _d, w in materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    assert entries["1TFL151621P0550"] == "2.742594"
    assert entries["1SRY105253P0001"] == "0.064618"


def test_parse_catalog_finds_header_row_below_decorative_rows():
    """Реалните файлове често имат празни/декоративни редове най-отгоре —
    заглавният ред се търси сред първите 10, не само на ред 1."""
    wb = Workbook()
    ws = wb.active
    ws.append([])
    ws.append(["Ценоразпис H2'26", None, None])
    ws.append(["ABB part ID", "Description", "Net weight\n[KG/pc]"])
    ws.append(["X-1", "Нещо", 1.5])
    buf = io.BytesIO()
    wb.save(buf)
    entries = materials.parse_catalog_xlsx(buf.getvalue())
    assert entries == [("X-1", "Нещо", "1.5")]


def test_parse_catalog_returns_none_for_unrecognised_file():
    assert materials.parse_catalog_xlsx(_catalog_bytes(
        [("а", "б", "в")], headers=("Колона1", "Колона2", "Колона3"))) is None


def test_parse_catalog_keeps_rows_without_weight_or_description():
    """В подадения реален файл има 845 материала без тегло и 105 без
    описание — те трябва да ОСТАНАТ в справочника (за тях просто няма
    какво да се попълни автоматично), а не да изчезнат от него."""
    entries = materials.parse_catalog_xlsx(_catalog_bytes([
        ("NOWEIGHT-1", "Без тегло", None),
        ("NODESC-1", None, 3.5),
    ]))
    assert ("NOWEIGHT-1", "Без тегло", "") in entries
    assert ("NODESC-1", "", "3.5") in entries


def test_parse_catalog_skips_rows_without_code():
    entries = materials.parse_catalog_xlsx(_catalog_bytes([
        ("", "Ред без код", 1.0),
        ("HAS-CODE", "Ред с код", 2.0),
    ]))
    assert entries == [("HAS-CODE", "Ред с код", "2")]


# ---------------------------------------------------------------- запис и търсене

def test_replace_catalog_inserts_then_updates_without_duplicating(con):
    added, updated = materials.replace_catalog(con, materials.parse_catalog_xlsx(
        _catalog_bytes(_SAMPLE)))
    assert (added, updated) == (3, 0)
    assert materials.count(con) == 3

    added, updated = materials.replace_catalog(con, materials.parse_catalog_xlsx(
        _catalog_bytes(_SAMPLE)))
    assert (added, updated) == (0, 3)
    assert materials.count(con) == 3, "повторното зареждане не бива да дублира редове"


def test_replace_catalog_keeps_materials_missing_from_the_new_file(con):
    """Нов ценоразпис обичайно покрива само част от материалите — вече
    издадени фактури сочат към старите кодове и те трябва да продължат да
    се разпознават, затова липсващите в новия файл НЕ се трият."""
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    materials.replace_catalog(con, materials.parse_catalog_xlsx(
        _catalog_bytes([("НОВ-1", "Само в новия файл", 1.0)])))
    assert materials.count(con) == 4
    assert materials.lookup(con, "GLBK400002P0012") is not None


def test_replace_catalog_updates_changed_weight(con):
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    materials.replace_catalog(con, materials.parse_catalog_xlsx(
        _catalog_bytes([("GLBK400002P0012", "C-PROFILE 3   1150MM", 9.99)])))
    assert materials.lookup(con, "GLBK400002P0012")["net_weight"] == "9.99"


def test_lookup_is_case_insensitive(con):
    """Операторите пишат кода на ръка и регистърът често се разминава."""
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    assert materials.lookup(con, "glbk400002p0012")["net_weight"] == "2.21"
    assert materials.lookup(con, "  GLBK400002P0012  ")["net_weight"] == "2.21"


def test_lookup_returns_none_for_unknown_or_empty_code(con):
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    assert materials.lookup(con, "НЯМА-ТАКЪВ") is None
    assert materials.lookup(con, "") is None
    assert materials.lookup(con, None) is None


def test_lookup_many_returns_only_found_codes_keyed_by_requested_code(con):
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    found = materials.lookup_many(con, ["glbk400002p0012", "НЯМА", "", "1SRY105253P0001"])
    assert set(found) == {"glbk400002p0012", "1SRY105253P0001"}
    assert found["glbk400002p0012"]["net_weight"] == "2.21"


def test_lookup_many_handles_more_codes_than_sqlite_parameter_limit(con):
    """Голяма палетна карта може да има повече редове от лимита за брой
    параметри на SQLite — заявката се разбива на партиди."""
    rows = [("CODE-%04d" % i, "Материал %d" % i, i * 0.5) for i in range(1200)]
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(rows)))
    found = materials.lookup_many(con, [c for c, _d, _w in rows])
    assert len(found) == 1200


def test_search_matches_code_and_description(con):
    materials.replace_catalog(con, materials.parse_catalog_xlsx(_catalog_bytes(_SAMPLE)))
    assert len(materials.search(con, "GLBK")) == 1
    assert len(materials.search(con, "Lock shackle")) == 1
    assert len(materials.search(con, "")) == 3


# ---------------------------------------------------------------- екранът „Материали“

def test_materials_page_shows_empty_state_before_import(admin_client):
    body = admin_client.get("/materials").data.decode()
    assert "Няма заредени материали" in body


def test_materials_import_loads_catalog_and_it_stays_loaded(admin_client):
    """Ядрото на заявката: качва се ВЕДНЪЖ и остава — следващо зареждане
    на страницата (нова заявка, нищо не се качва) пак показва материалите."""
    resp = post_with_csrf(
        admin_client, "/materials/import",
        {"excel_file": (io.BytesIO(_catalog_bytes(_SAMPLE)), "desk kg.xlsx")},
        csrf_source_url="/materials", follow_redirects=True,
        content_type="multipart/form-data")
    assert resp.status_code == 200
    assert "3 нови" in resp.data.decode()

    body = admin_client.get("/materials").data.decode()
    assert "GLBK400002P0012" in body
    assert "заредени" in body


def test_materials_import_rejects_unrecognised_file(admin_client):
    resp = post_with_csrf(
        admin_client, "/materials/import",
        {"excel_file": (io.BytesIO(_catalog_bytes(
            [("а", "б", "в")], headers=("К1", "К2", "К3"))), "грешен.xlsx")},
        csrf_source_url="/materials", follow_redirects=True,
        content_type="multipart/form-data")
    assert "не съдържа разпознаваеми колони" in resp.data.decode()


def test_materials_import_requires_admin(employee_client):
    resp = post_with_csrf(
        employee_client, "/materials/import",
        {"excel_file": (io.BytesIO(_catalog_bytes(_SAMPLE)), "k.xlsx")},
        csrf_source_url="/materials", follow_redirects=False,
        content_type="multipart/form-data")
    assert resp.status_code in (302, 403)


def test_materials_lookup_endpoint_returns_weight_and_description(admin_client):
    post_with_csrf(
        admin_client, "/materials/import",
        {"excel_file": (io.BytesIO(_catalog_bytes(_SAMPLE)), "k.xlsx")},
        csrf_source_url="/materials", content_type="multipart/form-data")
    payload = admin_client.get("/materials/lookup?code=GLBK400002P0012").get_json()
    assert payload["ok"] is True
    assert payload["net_weight"] == "2.21"
    assert payload["description"] == "C-PROFILE 3   1150MM"


def test_materials_lookup_endpoint_reports_unknown_code(admin_client):
    assert admin_client.get("/materials/lookup?code=НЯМА").get_json() == {"ok": False}


# ---------------------------------------------------------------- опашки след тире
# Заявка: „референция 1TGB110025P1204-RAS да се вмъкне като 1TGB110025P1204,
# за да може да се заредят автоматично килограмите“. В реалния ценоразпис
# обаче има 26 кода, които САМИ съдържат тире (напр. 3ACD5282AA842-1) —
# затова опашката се маха стъпка по стъпка отдясно и пълният код винаги
# има предимство (виж materials.code_candidates).

def test_code_candidates_full_code_first_then_progressively_stripped():
    assert materials.code_candidates("1TGB110025P1204-RAS") == \
        ["1TGB110025P1204-RAS", "1TGB110025P1204"]
    assert materials.code_candidates("3ACD5282AA842-1-RAS") == \
        ["3ACD5282AA842-1-RAS", "3ACD5282AA842-1", "3ACD5282AA842"]
    assert materials.code_candidates("БЕЗ-ТИРЕ") == ["БЕЗ-ТИРЕ", "БЕЗ"]
    assert materials.code_candidates("ЧИСТ") == ["ЧИСТ"]
    assert materials.code_candidates("") == []
    assert materials.code_candidates(None) == []


def test_lookup_strips_dash_suffix_when_full_code_is_not_in_catalog(con):
    materials.replace_catalog(con, [("1TGB110025P1204", "Профил", "1.5")])
    row = materials.lookup(con, "1TGB110025P1204-RAS")
    assert row is not None
    assert row["code"] == "1TGB110025P1204"
    assert row["net_weight"] == "1.5"


def test_lookup_prefers_the_full_dashed_code_over_its_own_base(con):
    """Кодове с тире СЪЩЕСТВУВАТ в ценоразписа (3ACD5282AA842-1 и т.н.) —
    пълният код винаги печели пред орязания си вариант."""
    materials.replace_catalog(con, [("3ACD5282AA842", "База", "1.0"),
                                    ("3ACD5282AA842-1", "Вариант 1", "2.0")])
    assert materials.lookup(con, "3ACD5282AA842-1")["net_weight"] == "2.0"
    # Суфикс върху кода С тире пада до НЕГО, не до голата база.
    assert materials.lookup(con, "3ACD5282AA842-1-RAS")["net_weight"] == "2.0"
    assert materials.lookup(con, "3ACD5282AA842")["net_weight"] == "1.0"


def test_lookup_suffix_fallback_is_case_insensitive(con):
    materials.replace_catalog(con, [("1TGB110025P1204", "Профил", "1.5")])
    assert materials.lookup(con, "1tgb110025p1204-ras")["code"] == "1TGB110025P1204"


def test_lookup_returns_none_when_neither_full_nor_stripped_exists(con):
    materials.replace_catalog(con, [("ДРУГ-КОД", "Нещо", "1.0")])
    assert materials.lookup(con, "НЕПОЗНАТ-RAS") is None


def test_lookup_many_strips_suffixes_and_keys_by_the_original_code(con):
    materials.replace_catalog(con, [("1TGB110025P1204", "Профил", "1.5"),
                                    ("GLBK400002P0012", "C-Profile", "2.21")])
    found = materials.lookup_many(
        con, ["1TGB110025P1204-RAS", "GLBK400002P0012", "НЕПОЗНАТ-XX"])
    assert set(found) == {"1TGB110025P1204-RAS", "GLBK400002P0012"}
    assert found["1TGB110025P1204-RAS"]["code"] == "1TGB110025P1204"
    assert found["1TGB110025P1204-RAS"]["net_weight"] == "1.5"


def test_materials_lookup_endpoint_strips_dash_suffix(admin_client):
    """Живото автопопълване във формата (fetch към /materials/lookup) също
    намира теглото при ръчно въведен код със суфикс."""
    post_with_csrf(
        admin_client, "/materials/import",
        {"excel_file": (io.BytesIO(_catalog_bytes(_SAMPLE)), "k.xlsx")},
        csrf_source_url="/materials", content_type="multipart/form-data")
    payload = admin_client.get("/materials/lookup?code=GLBK400002P0012-RAS").get_json()
    assert payload["ok"] is True
    assert payload["code"] == "GLBK400002P0012"
    assert payload["net_weight"] == "2.21"
