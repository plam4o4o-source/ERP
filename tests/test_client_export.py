# -*- coding: utf-8 -*-
"""Регресионни тестове за клиентските папки — заявка: „всеки клиент да се
запазват в отделни папки във всички документи“ (client_export.py) + UI
групиране по клиент в „Издадени документи“ (routes_documents.documents,
templates/documents.html)."""
import io
import json
import os

import pytest

from conftest import post_with_csrf


# ---------------------------------------------------------------- client_export.py (unit)

def test_sanitize_client_folder_name_strips_forbidden_characters():
    from client_export import sanitize_client_folder_name
    assert sanitize_client_folder_name('А/Б\\В:Г*Д?Е"Ж<З>И|Й') == "А_Б_В_Г_Д_Е_Ж_З_И_Й"


def test_sanitize_client_folder_name_handles_empty_and_reserved():
    from client_export import sanitize_client_folder_name
    assert sanitize_client_folder_name("") == "Без_име"
    assert sanitize_client_folder_name("   ") == "Без_име"
    assert sanitize_client_folder_name(None) == "Без_име"
    assert sanitize_client_folder_name("con") == "Без_име"  # Windows резервирано име
    assert sanitize_client_folder_name("NUL") == "Без_име"


def test_sanitize_client_folder_name_trims_trailing_dot_and_spaces():
    from client_export import sanitize_client_folder_name
    assert sanitize_client_folder_name("Клиент ООД. ") == "Клиент ООД"


def test_client_export_path_creates_directory(tmp_path):
    from client_export import client_export_path
    path = client_export_path(str(tmp_path), "Тестов Клиент", "pallet_0001.xlsx")
    assert os.path.isdir(os.path.join(str(tmp_path), "Тестов Клиент"))
    assert path.endswith(os.path.join("Тестов Клиент", "pallet_0001.xlsx"))


def test_save_client_export_copy_disabled_by_default(tmp_path):
    from client_export import save_client_export_copy
    settings = {"client_export_dir": str(tmp_path)}  # client_export_auto липсва
    ok = save_client_export_copy(settings, "pallet", {"client_name": "X"}, "f.xlsx", b"data")
    assert ok is False
    assert not os.listdir(str(tmp_path))


def test_save_client_export_copy_writes_file_when_enabled(tmp_path):
    from client_export import save_client_export_copy
    settings = {"client_export_dir": str(tmp_path), "client_export_auto": "on"}
    payload = "хубави данни".encode()
    ok = save_client_export_copy(settings, "pallet", {"client_name": "Клиент Едно"},
                                 "pallet_0001.xlsx", payload)
    assert ok is True
    written = os.path.join(str(tmp_path), "Клиент Едно", "pallet_0001.xlsx")
    assert os.path.exists(written)
    with open(written, "rb") as f:
        assert f.read() == payload


def test_save_client_export_copy_no_client_name_is_noop(tmp_path):
    from client_export import save_client_export_copy
    settings = {"client_export_dir": str(tmp_path), "client_export_auto": "on"}
    ok = save_client_export_copy(settings, "cmr", {}, "f.xlsx", b"data")
    assert ok is False
    assert not os.listdir(str(tmp_path))


def test_save_client_export_copy_survives_unwritable_base_dir(tmp_path):
    """Best-effort: грешка при запис (напр. базовата „папка“ всъщност е
    файл, не директория — os.makedirs не може да създаде поддиректория
    там) НЕ бива да хвърля изключение — само се логва (виж applog)."""
    from client_export import save_client_export_copy
    blocking_file = tmp_path / "not_a_directory.txt"
    blocking_file.write_text("аз съм файл, не папка")
    settings = {"client_export_dir": str(blocking_file), "client_export_auto": "on"}
    ok = save_client_export_copy(settings, "cmr", {"client_name": "X"}, "f.xlsx", b"data")
    assert ok is False  # не хвърли грешка, просто не успя


# ---------------------------------------------------------------- системни настройки (admin UI)

def test_client_export_settings_save_via_system_settings(admin_client, tmp_path):
    resp = post_with_csrf(admin_client, "/admin/system", {
        "form": "client_export", "client_export_dir": str(tmp_path),
        "client_export_auto": "on",
    }, csrf_source_url="/my-settings", follow_redirects=False)
    assert resp.status_code == 302
    page = admin_client.get("/my-settings")
    body = page.data.decode()
    assert str(tmp_path) in body
    assert "Клиентски папки" in body


# ---------------------------------------------------------------- export xlsx хук

def test_xlsx_export_writes_client_folder_copy_when_enabled(admin_client, tmp_path):
    post_with_csrf(admin_client, "/admin/system", {
        "form": "client_export", "client_export_dir": str(tmp_path),
        "client_export_auto": "on",
    }, csrf_source_url="/my-settings", follow_redirects=False)

    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент За Папка ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    xlsx_resp = admin_client.get("/doc/%s/export.xlsx" % doc_id)
    assert xlsx_resp.status_code == 200  # свалянето работи независимо от копието

    expected_dir = os.path.join(str(tmp_path), "Клиент За Папка ЕООД")
    assert os.path.isdir(expected_dir)
    files = os.listdir(expected_dir)
    assert len(files) == 1
    assert files[0].startswith("cmr_")


def test_xlsx_export_does_not_write_copy_when_disabled(admin_client, tmp_path):
    # НЕ включваме client_export_auto
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент Изключен ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    xlsx_resp = admin_client.get("/doc/%s/export.xlsx" % doc_id)
    assert xlsx_resp.status_code == 200
    assert not os.path.isdir(os.path.join(str(tmp_path), "Клиент Изключен ЕООД"))


# ---------------------------------------------------------------- export pdf хук (същият
# механизъм като xlsx по-горе — заявка "И двете" важи за всички износи)

def test_pdf_export_writes_client_folder_copy_when_enabled(admin_client, tmp_path):
    post_with_csrf(admin_client, "/admin/system", {
        "form": "client_export", "client_export_dir": str(tmp_path),
        "client_export_auto": "on",
    }, csrf_source_url="/my-settings", follow_redirects=False)

    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент За PDF Папка ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    pdf_resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    assert pdf_resp.status_code == 200  # свалянето работи независимо от копието

    expected_dir = os.path.join(str(tmp_path), "Клиент За PDF Папка ЕООД")
    assert os.path.isdir(expected_dir)
    files = os.listdir(expected_dir)
    assert len(files) == 1
    assert files[0].startswith("cmr_") and files[0].endswith(".pdf")


def test_pdf_export_does_not_write_copy_when_disabled(admin_client, tmp_path):
    # НЕ включваме client_export_auto
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент PDF Изключен ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    pdf_resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    assert pdf_resp.status_code == 200
    assert not os.path.isdir(os.path.join(str(tmp_path), "Клиент PDF Изключен ЕООД"))


# ---------------------------------------------------------------- групиране по клиент (UI)

def test_documents_group_by_client_shows_group_headers(admin_client):
    for name in ("Алфа Клиент", "Бета Клиент", "Алфа Клиент"):
        post_with_csrf(admin_client, "/cmr/new", {
            "sender_name": "Изпращач", "consignee_name": name,
        }, csrf_source_url="/cmr/new", follow_redirects=False)

    resp = admin_client.get("/docs?group=client")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "list-group-row" in body
    assert "Алфа Клиент" in body
    assert "Бета Клиент" in body
    assert "Групирай по клиент" not in body  # групирано — трябва да предложи обратния линк
    assert "Без групиране" in body


def test_documents_without_group_param_has_no_group_headers(admin_client):
    post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Гама Клиент",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    resp = admin_client.get("/docs")
    body = resp.data.decode()
    assert "list-group-row" not in body
    assert "Групирай по клиент" in body
