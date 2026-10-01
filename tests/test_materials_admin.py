# -*- coding: utf-8 -*-
"""Ръчно добавяне/редакция/изтриване на един материал в справочника
(одит 01.10.2026, P13) — само администратор, CSRF, код без оглед на регистъра."""
import pytest

import materials
from conftest import post_with_csrf


def _save(client, **fields):
    data = {"original_code": "", "code": "", "description": "", "net_weight": ""}
    data.update(fields)
    return post_with_csrf(client, "/materials/save", data, csrf_source_url="/materials",
                          follow_redirects=True)


def _row(db_module, code):
    return materials.get_exact(db_module.get_db(), code)


def test_admin_adds_a_material(admin_client, db_module):
    body = _save(admin_client, code="NEW-1", description="Част", net_weight="0,25").get_data(as_text=True)
    assert "Материалът „NEW-1“ е запазен." in body
    row = _row(db_module, "NEW-1")
    assert (row["description"], row["net_weight"]) == ("Част", "0.25")


def test_materials_page_has_the_add_form_and_row_actions_for_admin(admin_client, db_module):
    materials.replace_catalog(db_module.get_db(), [("ABC-1", "d", "1")])
    body = admin_client.get("/materials").get_data(as_text=True)
    assert 'action="/materials/save"' in body and 'action="/materials/delete"' in body
    body = admin_client.get("/materials?edit=abc-1").get_data(as_text=True)
    assert 'name="original_code" value="ABC-1"' in body
    assert 'name="code" value="ABC-1"' in body


def test_duplicate_code_is_rejected_case_insensitively_and_input_is_kept(admin_client, db_module):
    materials.replace_catalog(db_module.get_db(), [("ABC-1", "Стар", "1")])
    body = _save(admin_client, code="abc-1", description="Нов", net_weight="2").get_data(as_text=True)
    assert "Материал с код „ABC-1“ вече съществува" in body
    assert 'value="Нов"' in body  # въведеното не се губи
    row = _row(db_module, "ABC-1")
    assert (row["code"], row["description"]) == ("ABC-1", "Стар")
    assert materials.count(db_module.get_db()) == 1


@pytest.mark.parametrize("weight", ["abc", "-1", "nan"])
def test_bad_weight_is_rejected(admin_client, db_module, weight):
    body = _save(admin_client, code="W-1", net_weight=weight).get_data(as_text=True)
    assert "Нето теглото трябва да е неотрицателно число" in body
    assert _row(db_module, "W-1") is None


def test_empty_code_is_rejected(admin_client, db_module):
    body = _save(admin_client, code="  ", description="x").get_data(as_text=True)
    assert "Въведете код на материала." in body
    assert materials.count(db_module.get_db()) == 0


def test_edit_changes_fields_and_code(admin_client, db_module):
    materials.replace_catalog(db_module.get_db(), [("ABC-1", "Стар", "1"), ("XYZ-9", "", "")])
    _save(admin_client, original_code="ABC-1", code="ABC-2", description="Нов", net_weight="3")
    assert _row(db_module, "ABC-1") is None
    row = _row(db_module, "ABC-2")
    assert (row["description"], row["net_weight"]) == ("Нов", "3")
    # Смяна само на регистъра е позволена; сблъсък с ДРУГ код — не.
    _save(admin_client, original_code="ABC-2", code="abc-2", description="Нов", net_weight="3")
    assert _row(db_module, "ABC-2")["code"] == "abc-2"
    body = _save(admin_client, original_code="abc-2", code="xyz-9").get_data(as_text=True)
    assert "Материал с код „XYZ-9“ вече съществува" in body
    assert _row(db_module, "abc-2")["code"] == "abc-2"


def test_delete(admin_client, db_module):
    materials.replace_catalog(db_module.get_db(), [("ABC-1", "d", "1")])
    body = post_with_csrf(admin_client, "/materials/delete", {"code": "abc-1"},
                          csrf_source_url="/materials", follow_redirects=True).get_data(as_text=True)
    assert "Материалът „ABC-1“ е изтрит от справочника." in body
    assert materials.count(db_module.get_db()) == 0


@pytest.mark.parametrize("url,data", [
    ("/materials/save", {"code": "E-1"}),
    ("/materials/delete", {"code": "ABC-1"}),
])
def test_employee_cannot_change_the_catalog(employee_client, db_module, url, data):
    materials.replace_catalog(db_module.get_db(), [("ABC-1", "d", "1")])
    resp = post_with_csrf(employee_client, url, data, csrf_source_url="/materials")
    assert resp.status_code == 403
    assert materials.count(db_module.get_db()) == 1
    body = employee_client.get("/materials").get_data(as_text=True)
    assert 'action="/materials/save"' not in body and 'action="/materials/delete"' not in body


@pytest.mark.parametrize("url", ["/materials/save", "/materials/delete"])
def test_csrf_is_required(admin_client, db_module, url):
    materials.replace_catalog(db_module.get_db(), [("ABC-1", "d", "1")])
    resp = admin_client.post(url, data={"code": "ABC-1", "original_code": "ABC-1"})
    assert resp.status_code == 400
    assert _row(db_module, "ABC-1")["description"] == "d"
    assert materials.count(db_module.get_db()) == 1
