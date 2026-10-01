# -*- coding: utf-8 -*-
"""Общият модул на трите Excel импорта (xlsx_import.py) и обратната връзка
при неразпознати колони (одит 01.10.2026: Q5, U10 а/б)."""
import ast
import io
import os

import pytest
from openpyxl import Workbook

import materials
import routes_invoices
import routes_materials
import routes_pallet_extra
import xlsx_import
from conftest import ROOT, post_with_csrf

_SHARED = ("_read_limited_rows", "_xlsx_has_formulas", "_xlsx_has_merged_cells",
           "_parse_sheets", "_header_row_hint", "_formula_hint", "_cellstr",
           "xlsx_has_merged_cells", "reset_sheet_dimensions", "row_has_data")


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _post_file(client, url, blob, src):
    return post_with_csrf(client, url, {"excel_file": (io.BytesIO(blob), "a.xlsx")},
                          csrf_source_url=src, content_type="multipart/form-data",
                          follow_redirects=False)


def _tree(module):
    with open(os.path.join(ROOT, module + ".py"), encoding="utf-8") as fh:
        return ast.parse(fh.read())


# ---------------------------------------------------------------- Q5

@pytest.mark.parametrize("module", ["routes_invoices", "routes_pallet_extra", "materials"])
def test_import_helpers_are_defined_only_in_xlsx_import(module):
    """Q5: седемте помощни функции бяха копирани дословно в 2–3 модула."""
    defined = {n.name for n in ast.walk(_tree(module)) if isinstance(n, ast.FunctionDef)}
    assert not defined & set(_SHARED), sorted(defined & set(_SHARED))
    assert "find_col_in" not in defined


def test_cellstr_aliases_are_the_shared_function():
    for mod in (routes_invoices, routes_pallet_extra, materials):
        assert mod._cellstr is xlsx_import.cellstr


def test_invoices_no_longer_import_private_helpers_from_pallet_routes():
    for node in ast.walk(_tree("routes_invoices")):
        if isinstance(node, ast.ImportFrom) and node.module == "routes_pallet_extra":
            assert all(not a.name.startswith("_") for a in node.names), \
                [a.name for a in node.names]


def test_shared_messages_are_word_for_word_the_same(flask_app):
    """Едни и същи съобщения пред оператора от трите импорта."""
    with flask_app.test_request_context():
        assert xlsx_import.header_found_warning(3) == (
            "Заглавният ред е открит на ред 3 от файла (пропуснати са 2 реда над "
            "него) — проверете дали разпознатите данни са правилни.")
        assert xlsx_import.header_row_hint() == \
            "Заглавният ред трябва да е в първите 10 реда на листа."


def test_routes_materials_uses_the_shared_header_warning():
    src = open(routes_materials.__file__, encoding="utf-8").read()
    assert "xlsx_import.header_found_warning" in src
    assert "xlsx_import.merged_cells_warning" in src


# ---------------------------------------------------------------- U10 (а)

def test_invoice_import_accepts_a_material_column(admin_client, db_module):
    materials.replace_catalog(db_module.get_db(), [("3AUA0000123", "Breaker", "0.39")])
    blob = _xlsx([["Order No", "Pos", "Material", "Description", "Qty"],
                  ["PO-77", 10, "3AUA0000123", None, 10]])
    res = _post_file(admin_client, "/invoice/import-items", blob, "/invoice-br/new").get_json()
    assert res["ok"] and res["matched"] == 1, res
    assert res["rows"][0]["net_weight"] == "0.39"


def test_invoice_import_without_code_column_says_which_columns_it_saw(admin_client):
    blob = _xlsx([["Order No", "Pos", "Artikel", "Qty"], ["PO-1", 10, "X-1", 2]])
    res = _post_file(admin_client, "/invoice/import-items", blob, "/invoice-br/new").get_json()
    assert res["ok"], res
    warning = " ".join(res.get("warnings", []))
    assert "не е намерена; открити колони:" in warning
    assert "„Artikel“" in warning and "„Order No“" in warning


def test_pallet_import_without_code_column_warns(admin_client):
    blob = _xlsx([["Order No", "Pos", "Artikel", "Open Qty", "Pallet"], ["PO-1", 10, "X", 2, 1]])
    body = _post_file(admin_client, "/pallet/bulk-import", blob,
                      "/pallet/new").get_data(as_text=True)
    assert "не е намерена; открити колони:" in body


def test_pallet_import_with_reference_column_does_not_warn(admin_client):
    blob = _xlsx([["Order No", "Pos", "Reference", "Open Qty", "Pallet"], ["PO-1", 10, "X", 2, 1]])
    body = _post_file(admin_client, "/pallet/bulk-import", blob,
                      "/pallet/new").get_data(as_text=True)
    assert "не е намерена" not in body


# ---------------------------------------------------------------- U10 (б)

@pytest.mark.parametrize("header", [
    ("Material", "Description", "Net weight"),
    ("Material", "Description", "Net weight (kg)"),
    ("Part No", "Material Description", "Net Weight [kg]"),
    ("Код", "Описание", "Тегло"),                  # българските остават
    ("Материал", "Описание", "Нето тегло, кг"),
    ("ABB part ID", "Description", "Net weight\n[KG/pc]"),
])
def test_catalog_import_recognises_common_headers(db_module, header):
    entries = materials.parse_catalog_xlsx(_xlsx([header, ["NEW-001", "New part", "1,5"]]))
    assert entries == [("NEW-001", "New part", "1.5")]


def test_catalog_route_accepts_material_header(admin_client, db_module):
    blob = _xlsx([["Material", "Description", "Net weight"], ["NEW-001", "New part", "1,5"]])
    resp = _post_file(admin_client, "/materials/import", blob, "/materials")
    assert resp.status_code == 302
    row = materials.lookup(db_module.get_db(), "NEW-001")
    assert row is not None and row["net_weight"] == "1.5"
