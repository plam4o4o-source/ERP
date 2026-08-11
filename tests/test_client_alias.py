# -*- coding: utf-8 -*-
"""Тестове за псевдонима на клиента в адресната книга — заявка: „добави за
всеки клиент псевдоним в адресната книга и да излиза при избор от
падащите менюта псевдонима“ + „наименованието на файла палетната карта,
която се запаметява като pdf или xlsx да е псевдонима на клиента [и]
номер палетна карта, на английски език“.

Обхватът беше уточнен изрично (AskUserQuestion): псевдонимът е поле в
ЦЯЛАТА адресна книга клиенти (важи за ЧМР/опаковъчен лист/палетна карта/
товарителница/декларациите — не само палетни карти), но е ползван за
ИМЕТО НА ФАЙЛА само при палетни карти (единственият тип, за който бе
поискано изрично — вижте routes_documents._export_filename)."""
import json

from conftest import post_with_csrf

import client_export


# ---------------------------------------------------------------- db.py: миграция/схема

def test_clients_table_has_alias_column(db_module):
    con = db_module.get_db()
    cols = [r["name"] for r in con.execute("PRAGMA table_info(clients)")]
    con.close()
    assert "alias" in cols


# ---------------------------------------------------------------- адресна книга: форма/списък

def test_client_alias_saved_and_shown_in_list(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/clients/new", {
        "name": "Ей Си Ем И Инженеринг ООД", "alias": "ACME", "city": "София",
    }, csrf_source_url="/clients/new", follow_redirects=False)
    assert resp.status_code == 302

    con = db_module.get_db()
    row = con.execute(
        "SELECT alias FROM clients WHERE name = ?", ("Ей Си Ем И Инженеринг ООД",)
    ).fetchone()
    con.close()
    assert row["alias"] == "ACME"

    body = admin_client.get("/clients").data.decode()
    assert "ACME" in body


def test_client_edit_form_prefills_alias(admin_client, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, alias) VALUES (?, ?)", ("Клиент Псевдоним", "CLNT"))
    con.commit()
    client_id = con.execute("SELECT id FROM clients WHERE name = ?", ("Клиент Псевдоним",)).fetchone()["id"]
    con.close()

    body = admin_client.get("/clients/%d/edit" % client_id).data.decode()
    assert 'name="alias" value="CLNT"' in body


def test_client_alias_is_optional(admin_client, db_module):
    """Псевдонимът е по избор — клиент БЕЗ псевдоним се запазва нормално."""
    resp = post_with_csrf(admin_client, "/clients/new", {
        "name": "Клиент Без Псевдоним",
    }, csrf_source_url="/clients/new", follow_redirects=False)
    assert resp.status_code == 302
    con = db_module.get_db()
    row = con.execute(
        "SELECT alias FROM clients WHERE name = ?", ("Клиент Без Псевдоним",)
    ).fetchone()
    con.close()
    assert row["alias"] == ""


# ---------------------------------------------------------------- падащи менюта (7 форми)

def _add_client(db_module, name, alias=""):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, alias, city) VALUES (?, ?, ?)", (name, alias, "Варна"))
    con.commit()
    con.close()


def test_dropdown_shows_alias_in_parentheses_when_set(admin_client, db_module):
    _add_client(db_module, "Дропдаун Клиент ЕООД", "DRP")
    for url in ("/cmr/new", "/packing/new", "/pallet/new", "/waybill/new",
               "/dualuse/new", "/export-it/new"):
        body = admin_client.get(url).data.decode()
        assert "Дропдаун Клиент ЕООД (DRP) —" in body, url


def test_dropdown_omits_parentheses_when_alias_not_set(admin_client, db_module):
    _add_client(db_module, "Клиент Без Псевдоним ООД")
    body = admin_client.get("/cmr/new").data.decode()
    assert "Клиент Без Псевдоним ООД —" in body
    assert "Клиент Без Псевдоним ООД ()" not in body


# ---------------------------------------------------------------- client_export.resolve_client_alias

def test_resolve_client_alias_matches_by_name_case_insensitively(db_module):
    _add_client(db_module, "Регистър Тест ООД", "REGT")
    con = db_module.get_db()
    alias = client_export.resolve_client_alias(con, {"client_name": "регистър тест ООД"})
    con.close()
    assert alias == "REGT"


def test_resolve_client_alias_empty_when_no_match(db_module):
    con = db_module.get_db()
    alias = client_export.resolve_client_alias(con, {"client_name": "Няма такъв клиент"})
    con.close()
    assert alias == ""


def test_resolve_client_alias_empty_when_client_has_no_alias(db_module):
    _add_client(db_module, "Без Псевдоним Директно ООД")
    con = db_module.get_db()
    alias = client_export.resolve_client_alias(con, {"client_name": "Без Псевдоним Директно ООД"})
    con.close()
    assert alias == ""


def test_resolve_client_alias_uses_consignee_before_client_name():
    """Същият приоритет като resolve_client_name (consignee > receiver >
    client_name) — тук само проверяваме, че se ползва точно него."""
    assert client_export.resolve_client_name(
        {"consignee_name": "А", "client_name": "Б"}) == "А"


# ---------------------------------------------------------------- sanitize_filename_stub

def test_sanitize_filename_stub_replaces_spaces_and_forbidden_chars():
    assert client_export.sanitize_filename_stub("ACME Ltd / Co.") == "ACME_Ltd_Co"


def test_sanitize_filename_stub_empty_for_blank_input():
    assert client_export.sanitize_filename_stub("   ") == ""


# ---------------------------------------------------------------- име на файла при износ (PDF/Excel)

def test_pallet_pdf_filename_uses_client_alias(admin_client, db_module):
    _add_client(db_module, "Файлов Клиент ЕООД", "FILEX")
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Файлов Клиент ЕООД",
        "items_json": json.dumps([{"code": "A1", "qty": "1"}]),
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    pdf_resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    assert "FILEX_" in pdf_resp.headers.get("Content-Disposition", "")

    xlsx_resp = admin_client.get("/doc/%s/export.xlsx" % doc_id)
    assert "FILEX_" in xlsx_resp.headers.get("Content-Disposition", "")


def test_pallet_pdf_filename_falls_back_when_no_alias(admin_client, db_module):
    """Клиент БЕЗ псевдоним (или изобщо не е в адресната книга) — файлът
    пази досегашното име (pallet_<номер>), не гърми и не оставя файла без
    име."""
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Съвсем Непознат Клиент",
        "items_json": json.dumps([{"code": "A1", "qty": "1"}]),
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    pdf_resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    disposition = pdf_resp.headers.get("Content-Disposition", "")
    assert "pallet_" in disposition


def test_other_doc_types_filename_unaffected_by_alias(admin_client, db_module):
    """Псевдонимът влияе на името на файла САМО за палетни карти (единствен
    тип, за който бе поискано) — ЧМР с идентично съвпадащ по име клиент
    пази досегашното си име на файл, дори клиентът да има псевдоним."""
    _add_client(db_module, "Общ Клиент За ЧМР ЕООД", "CMRX")
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "consignee_name": "Общ Клиент За ЧМР ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    pdf_resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    disposition = pdf_resp.headers.get("Content-Disposition", "")
    assert "cmr_" in disposition
    assert "CMRX" not in disposition
