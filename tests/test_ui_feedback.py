# -*- coding: utf-8 -*-
"""Тестове за графичните подобрения на обратната връзка — заявка:
„съобщенията да излизат анимирано и да са по-забележими“ (+ одобрените
предложения от прегледа на интерфейса).

Обхванато тук (сървърната част; самите анимации/таймери са клиентско JS и
CSS, покрити с e2e тестове в tests/test_e2e_smoke.py):

  1. Toast съобщения с КАТЕГОРИЯ — всяко flash() в кода вече подава
     success/error/warning/info и бланката ги рендира с различен клас
     (цвят/икона по категория, вместо еднакъв жълт блок за всичко).
  2. Успех/инфо се маркират .toast-auto (скриват се сами, с
     прогрес-линийка); грешки/предупреждения — НЕ (остават до ръчно
     затваряне) и носят role="alert" за екранните четци.
  3. Модалният диалог за потвърждение замества браузърния confirm() —
     формите за изтриване носят data-confirm, а старият onsubmit го няма.
  4. Празните списъци са .empty-state с икона и подканващ бутон.
  5. Бавните/фонови операции (GitHub/архив/отдалечен достъп) носят
     data-busy — бутонът им получава въртящ се индикатор (JS).
"""
from conftest import post_with_csrf


# ---------------------------------------------------------------- категории

def test_success_flash_renders_as_success_toast(admin_client):
    resp = post_with_csrf(admin_client, "/settings", {"sender_name": "Фирма"},
                          csrf_source_url="/settings", follow_redirects=True)
    body = resp.data.decode()
    assert "toast-success" in body
    assert "Данните на фирмата изпращач са запазени." in body


def test_error_flash_renders_as_error_toast(admin_client):
    resp = post_with_csrf(admin_client, "/password", {
        "current": "грешна", "new": "x", "repeat": "x",
    }, csrf_source_url="/password", follow_redirects=True)
    body = resp.data.decode()
    assert "toast-error" in body
    assert "Текущата парола е грешна." in body


def test_warning_flash_renders_as_warning_toast(admin_client):
    """Дублиран номер на фактура е предупреждение (не грешка — документът
    все пак се издава), и излиза в жълтия warning стил."""
    for _i in range(2):
        resp = post_with_csrf(admin_client, "/invoice-br/new",
                              {"consignee_name": "ABB", "invoice_number": "ДУБЛЬОР-1"},
                              csrf_source_url="/invoice-br/new", follow_redirects=True)
    body = resp.data.decode()
    assert "toast-warning" in body
    assert "вече има издаден документ с номер ДУБЛЬОР-1" in body


def test_info_flash_renders_as_info_toast(admin_client):
    body = admin_client.get("/logout", follow_redirects=True).data.decode()
    assert "toast-info" in body
    assert "Излязохте от системата." in body


# ---------------------------------------------------------------- автоскриване и достъпност

def test_success_toast_auto_dismisses_but_error_does_not(admin_client):
    """Успехът се скрива сам (.toast-auto + прогрес-линийка .toast-bar);
    грешката НЯМА .toast-auto — стои, докато не бъде затворена ръчно,
    защото грешка не бива да изчезне, преди да е прочетена."""
    ok = post_with_csrf(admin_client, "/settings", {"sender_name": "Фирма"},
                        csrf_source_url="/settings", follow_redirects=True).data.decode()
    assert "toast-auto" in ok.split('class="toast ')[1].split('"')[0]
    assert "toast-bar" in ok

    err = post_with_csrf(admin_client, "/password", {
        "current": "грешна", "new": "x", "repeat": "x",
    }, csrf_source_url="/password", follow_redirects=True).data.decode()
    assert "toast-auto" not in err.split('class="toast ')[1].split('"')[0]
    assert "toast-bar" not in err


def test_error_toast_is_an_alert_for_screen_readers(admin_client):
    err = post_with_csrf(admin_client, "/password", {
        "current": "грешна", "new": "x", "repeat": "x",
    }, csrf_source_url="/password", follow_redirects=True).data.decode()
    toast = err.split('class="toast ')[1].split(">")[0]
    assert 'role="alert"' in toast


def test_success_toast_is_a_status_for_screen_readers(admin_client):
    ok = post_with_csrf(admin_client, "/settings", {"sender_name": "Фирма"},
                        csrf_source_url="/settings", follow_redirects=True).data.decode()
    toast = ok.split('class="toast ')[1].split(">")[0]
    assert 'role="status"' in toast


def test_every_toast_has_a_manual_close_button(admin_client):
    body = admin_client.get("/logout", follow_redirects=True).data.decode()
    toast = body.split('class="toast ')[1].split("</div>")[0]
    assert "toast-close" in toast


def test_toasts_appear_on_the_guest_login_page_too(client, admin_client):
    """Изходът от системата пренасочва към login (гост изглед) — toast
    контейнерът трябва да работи и там, не само в пълното приложение."""
    body = admin_client.get("/logout", follow_redirects=True).data.decode()
    assert 'class="toasts' in body


# ---------------------------------------------------------------- модал за потвърждение

def test_delete_forms_use_the_styled_confirm_modal_not_browser_confirm(admin_client):
    resp = post_with_csrf(admin_client, "/invoice-br/new",
                          {"consignee_name": "ABB", "invoice_number": "МОДАЛ-1"},
                          csrf_source_url="/invoice-br/new", follow_redirects=False)
    assert resp.status_code == 302
    body = admin_client.get("/invoices").data.decode()
    assert "data-confirm=" in body
    assert "return confirm(" not in body, "браузърният confirm() е заменен"


def test_confirm_modal_markup_is_present_on_every_logged_in_page(admin_client):
    body = admin_client.get("/").data.decode()
    assert 'id="confirm-modal"' in body
    assert 'id="confirm-modal-ok"' in body
    assert 'id="confirm-modal-cancel"' in body


def test_confirm_modal_is_absent_from_the_public_guest_view(client, admin_client, db_module):
    """Публичният преглед през QR код (гост изглед) няма форми за
    изтриване — модалът не се рендира там."""
    resp = post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "X"},
                          csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    con = db_module.get_db()
    token = con.execute("SELECT public_token FROM documents WHERE id = ?",
                        (doc_id,)).fetchone()["public_token"]
    con.close()
    body = client.get("/p/%s" % token).data.decode()
    assert 'id="confirm-modal"' not in body


def test_all_browser_confirms_are_gone_from_the_templates():
    """Никой шаблон не ползва вече onsubmit="return confirm(...)" — всички
    минават през модала (data-confirm)."""
    import glob
    import os
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates")
    for path in glob.glob(os.path.join(root, "*.html")):
        content = open(path, encoding="utf-8").read()
        assert "return confirm(" not in content, path


# ---------------------------------------------------------------- празни състояния

def test_empty_invoices_list_shows_empty_state_with_cta(admin_client):
    body = admin_client.get("/invoices").data.decode()
    assert 'class="empty-state"' in body
    assert "Издай първата фактура" in body


def test_empty_clients_list_shows_empty_state_with_cta(admin_client):
    body = admin_client.get("/clients").data.decode()
    assert 'class="empty-state"' in body
    assert "Добави първия клиент" in body


def test_empty_documents_list_shows_empty_state(admin_client):
    body = admin_client.get("/docs").data.decode()
    assert 'class="empty-state"' in body


def test_empty_state_disappears_once_there_is_data(admin_client):
    post_with_csrf(admin_client, "/clients/new", {"name": "Клиент ЕООД"},
                   csrf_source_url="/clients/new", follow_redirects=False)
    assert 'class="empty-state"' not in admin_client.get("/clients").data.decode()


# ---------------------------------------------------------------- заети бутони

def test_slow_github_and_backup_forms_are_marked_busy(admin_client):
    body = admin_client.get("/my-settings").data.decode()
    # 5-те бавни/фонови операции: архив, качване/изтегляне GitHub,
    # старт/стоп на отдалечен достъп (виж initBusyForms в app.js).
    assert body.count("data-busy") == 5
