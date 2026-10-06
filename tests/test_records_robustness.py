# -*- coding: utf-8 -*-
"""Одит (04.10.2026, група DATA): записи, файлове, архив, настройки, търсене.

R2 — изгубена промяна в адресните книги при едновременна редакция;
R3 — неатомарен запис на логото; S5 — таваните на прикачените файлове при
паралелно качване; S4 — валидация на публичния адрес; R8 — папките за архив
и клиентски копия; UX-12 — порт в текста на „Система“; I3/I9 — преводими
съобщения за грешки; F7 — клиент на декларацията за двойна употреба;
F11 — потребителски имена без регистър; F12 — търсене „istanbul“/„İstanbul“;
UX-9 — броене при импорт на материали; F10 — изтриване на клиент с документи.
"""
import io
import json
import os
import re
import threading

import pytest

from conftest import get_csrf_token, post_with_csrf


# ---------------------------------------------------------------- R2

def _client_form(resp_data):
    """{име: стойност} на скритите и видимите input-и от формата на клиент."""
    html = resp_data.decode()
    out = {}
    for m in re.finditer(r'<input[^>]*\bname="([^"]+)"[^>]*\bvalue="([^"]*)"', html):
        out[m.group(1)] = (m.group(2).replace("&#34;", '"').replace("&quot;", '"')
                           .replace("&#39;", "'").replace("&amp;", "&"))
    return out


def _make_client(db_module, **kw):
    con = db_module.get_db()
    vals = {"name": "Акме ООД", "phone": "111", "email": "a@x.bg", "city": "София"}
    vals.update(kw)
    cur = con.execute("INSERT INTO clients (%s) VALUES (%s)"
                      % (", ".join(vals), ", ".join("?" * len(vals))), list(vals.values()))
    con.commit()
    cid = cur.lastrowid
    con.close()
    return cid


def _client_row(db_module, cid):
    con = db_module.get_db()
    try:
        return dict(con.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone())
    finally:
        con.close()


def test_r2_concurrent_client_edits_of_different_fields_both_survive(
        admin_client, employee_client, db_module):
    cid = _make_client(db_module)
    url = "/clients/%d/edit" % cid
    form_admin = _client_form(admin_client.get(url).data)
    form_emp = _client_form(employee_client.get(url).data)
    # служителят сменя имейла и записва първи
    form_emp["email"] = "new@x.bg"
    form_emp["unload_points_json"] = "[]"
    assert employee_client.post(url, data=form_emp).status_code == 302
    # админът (формата му е отворена ОТПРЕДИ това) сменя телефона
    form_admin["phone"] = "999"
    form_admin["unload_points_json"] = "[]"
    assert admin_client.post(url, data=form_admin).status_code == 302
    row = _client_row(db_module, cid)
    assert row["phone"] == "999"
    assert row["email"] == "new@x.bg"  # преди поправката: „a@x.bg“ (тихо върнато)


def test_r2_same_field_conflict_keeps_input_and_explains(
        admin_client, employee_client, db_module):
    cid = _make_client(db_module)
    url = "/clients/%d/edit" % cid
    form_admin = _client_form(admin_client.get(url).data)
    form_emp = _client_form(employee_client.get(url).data)
    form_emp["phone"] = "222"
    form_emp["unload_points_json"] = "[]"
    assert employee_client.post(url, data=form_emp).status_code == 302
    form_admin["phone"] = "333"
    form_admin["city"] = "Пловдив"
    form_admin["unload_points_json"] = "[]"
    resp = admin_client.post(url, data=form_admin)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "променен от друг потребител" in body
    assert "записано „222“, ваше „333“" in body
    row = _client_row(db_module, cid)
    assert row["phone"] == "222" and row["city"] == "София"  # нищо не е записано
    again = _client_form(resp.data)
    assert again["phone"] == "333" and again["city"] == "Пловдив"  # въведеното е запазено
    assert again["orig_phone"] == "222"  # новата база е текущото
    # второто „Запази“ е съзнателен избор — минава
    assert admin_client.post(url, data=again).status_code == 302
    row = _client_row(db_module, cid)
    assert row["phone"] == "333" and row["city"] == "Пловдив"


def test_r2_untouched_unload_points_are_not_overwritten(admin_client, employee_client, db_module):
    cid = _make_client(db_module)
    url = "/clients/%d/edit" % cid
    form_admin = _client_form(admin_client.get(url).data)
    form_emp = _client_form(employee_client.get(url).data)
    form_emp["unload_points_json"] = json.dumps([{"label": "Склад 1", "city": "Русе"}])
    assert employee_client.post(url, data=form_emp).status_code == 302
    form_admin["phone"] = "777"
    form_admin["unload_points_json"] = "[]"  # формата на админа е без пунктове
    assert admin_client.post(url, data=form_admin).status_code == 302
    con = db_module.get_db()
    points = con.execute("SELECT label FROM client_unload_points WHERE client_id = ?",
                         (cid,)).fetchall()
    con.close()
    assert [p["label"] for p in points] == ["Склад 1"]


def test_r2_client_post_without_originals_still_saves(admin_client, db_module):
    """Стара кеширана страница/скрипт без orig_ полета — записва се, както досега."""
    cid = _make_client(db_module)
    resp = post_with_csrf(admin_client, "/clients/%d/edit" % cid,
                          {"name": "Акме ООД", "phone": "555"})
    assert resp.status_code == 302
    assert _client_row(db_module, cid)["phone"] == "555"


def test_r2_invoice_client_merge_and_conflict(db_module):
    import invoice_clients_module as icm
    con = db_module.get_db()
    entry_id = icm.save(con, {"name": "ABB BR", "delivery_phone": "1",
                              "billing_address": "Line 1\nLine 2"})
    opened = dict(icm.get(con, entry_id))
    form_a = {k: opened[k] for k in icm._FIELDS}
    form_a.update({"orig_" + k: opened[k] for k in icm._FIELDS})
    # браузърът праща многоредовите стойности с \r\n
    form_a["billing_address"] = form_a["orig_billing_address"] = "Line 1\r\nLine 2"
    form_b = dict(form_a)
    form_b["delivery_phone"] = "2"
    icm.save(con, form_b, entry_id)
    form_a["notes"] = "VIP"
    icm.save(con, form_a, entry_id)
    row = icm.get(con, entry_id)
    assert row["delivery_phone"] == "2" and row["notes"] == "VIP"
    # едно и също поле, различно → EditConflict, нищо не е записано
    form_c = dict(form_a)
    form_c["delivery_phone"] = "3"
    with pytest.raises(icm.EditConflict) as info:
        icm.save(con, form_c, entry_id)
    assert info.value.conflicts == [("delivery_phone", "2", "3")]
    assert info.value.values["notes"] == "VIP"
    assert icm.get(con, entry_id)["delivery_phone"] == "2"
    assert not con.in_transaction
    con.close()


def test_r2_invoice_client_form_carries_originals(admin_client, db_module):
    import invoice_clients_module as icm
    con = db_module.get_db()
    entry_id = icm.save(con, {"name": "ABB BR", "delivery_phone": "1"})
    con.close()
    form = _client_form(admin_client.get("/invoices/clients/%d/edit" % entry_id).data)
    assert form["orig_name"] == "ABB BR" and form["orig_delivery_phone"] == "1"


# ---------------------------------------------------------------- R3

_VALID_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _Upload:
    def __init__(self, data, filename="logo.png"):
        self._data, self.filename = data, filename

    def read(self):
        return self._data


def _big_png(size=20000):
    """Валиден PNG, по-голям от 8192 байта (шум — не се компресира)."""
    from PIL import Image
    img = Image.frombytes("RGB", (90, 90), os.urandom(90 * 90 * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    assert len(data) > 8192
    return data


class _DiskFullFile:
    """Файл, който записва първите 8192 байта и после гърми с ENOSPC."""

    def __init__(self, real):
        self._real = real

    def write(self, data):
        self._real.write(data[:8192])
        self._real.flush()
        raise OSError(28, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False


def test_r3_disk_full_keeps_old_logo_and_leaves_no_truncated_file(db_module, monkeypatch):
    import branding
    base = os.path.dirname(db_module.DB_PATH)
    old = os.path.join(base, "company_logo.jpg")
    with open(old, "wb") as fh:
        fh.write(b"\xff\xd8\xff OLD LOGO")
    import builtins
    real_open, real_fdopen = builtins.open, os.fdopen
    data = _big_png()
    with monkeypatch.context() as mp:
        mp.setattr(branding, "open", lambda *a, **k: _DiskFullFile(real_open(*a, **k)),
                   raising=False)
        mp.setattr(os, "fdopen", lambda *a, **k: _DiskFullFile(real_fdopen(*a, **k)))
        with pytest.raises(OSError):
            branding.save_logo(_Upload(data))
    # старото лого е непокътнато и продължава да се сервира; няма орязан PNG
    assert branding.logo_path() == old
    assert open(old, "rb").read() == b"\xff\xd8\xff OLD LOGO"
    assert not os.path.exists(os.path.join(base, "company_logo.png"))
    assert not [n for n in os.listdir(base) if n.endswith(".tmp")]


def test_r3_corrupt_image_is_rejected_and_old_logo_kept(db_module):
    import branding
    branding.save_logo(_Upload(_VALID_PNG))
    good = branding.logo_path()
    truncated = _big_png()[:8192]
    with pytest.raises(ValueError):
        branding.save_logo(_Upload(truncated))
    assert branding.logo_path() == good
    assert open(good, "rb").read() == _VALID_PNG


def test_r3_new_logo_with_other_extension_replaces_old_only_after_success(db_module):
    import branding
    base = os.path.dirname(db_module.DB_PATH)
    with open(os.path.join(base, "company_logo.gif"), "wb") as fh:
        fh.write(b"GIF89a-old")
    path = branding.save_logo(_Upload(_VALID_PNG))
    assert path.endswith("company_logo.png") and branding.logo_path() == path
    assert not os.path.exists(os.path.join(base, "company_logo.gif"))


def test_r3_route_reports_disk_error_without_500(admin_client, monkeypatch):
    import branding

    def boom(_file):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(branding, "save_logo", boom)
    resp = post_with_csrf(admin_client, "/settings/logo",
                          {"logo_file": (io.BytesIO(_VALID_PNG), "logo.png")},
                          csrf_source_url="/settings", content_type="multipart/form-data",
                          follow_redirects=True)
    assert resp.status_code == 200
    assert "Логото НЕ е записано" in resp.get_data(as_text=True)


# ---------------------------------------------------------------- S5

def _make_document(db_module):
    con = db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
        " VALUES ('cmr', '0001/2026', 2026, 1, 'CMR-1', '{}')")
    con.commit()
    doc_id = cur.lastrowid
    con.close()
    return doc_id


def _parallel_uploads(db_module, doc_id, count, size):
    import attachments
    payload = b"%PDF-" + b"x" * (size - 5)
    barrier = threading.Barrier(count)
    errors, ok = [], []

    def worker(i):
        con = db_module.get_db()
        try:
            barrier.wait()
            attachments.save_attachment(con, doc_id, _Upload(payload, "f%d.pdf" % i))
            ok.append(i)
        except ValueError as exc:
            errors.append(exc)
        finally:
            con.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    con = db_module.get_db()
    stats = con.execute("SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS s"
                        " FROM document_attachments WHERE document_id = ?", (doc_id,)).fetchone()
    con.close()
    return stats["c"], stats["s"], ok, errors


def test_s5_parallel_uploads_respect_file_count_cap(db_module, monkeypatch):
    import attachments
    doc_id = _make_document(db_module)
    real_fsync = os.fsync

    def slow_fsync(fd):  # разширява прозореца между проверката и INSERT-а
        import time
        time.sleep(0.02)
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", slow_fsync)
    count, _total, ok, errors = _parallel_uploads(db_module, doc_id, 40, 100)
    assert count == attachments.MAX_FILES, count
    assert len(ok) == attachments.MAX_FILES and len(errors) == 40 - attachments.MAX_FILES
    # файловете на отказаните не остават на диска
    folder = attachments._base_dir(doc_id)
    assert len(os.listdir(folder)) == attachments.MAX_FILES


def test_s5_parallel_uploads_respect_total_size_cap(db_module, monkeypatch):
    import attachments
    doc_id = _make_document(db_module)
    monkeypatch.setattr(attachments, "MAX_TOTAL_SIZE", 10 * 1000)
    count, total, ok, _errors = _parallel_uploads(db_module, doc_id, 30, 1000)
    assert total <= 10 * 1000 and count == 10, (count, total)


# ---------------------------------------------------------------- S4

@pytest.mark.parametrize("raw", [
    "javascript:alert(1)", "https://example.com:abc", "https://example.com:0",
    "https://example.com:70000", "https://user:pass@example.com", "https://bank.bg@evil.example",
    "ftp://example.com", "mailto:x@example.com",
])
def test_s4_public_url_rejects_invalid_values(admin_client, db_module, raw):
    resp = post_with_csrf(admin_client, "/admin/system", {"form": "public_base_url",
                                                         "public_base_url": raw},
                          csrf_source_url="/admin/system", follow_redirects=True)
    assert resp.status_code == 200
    con = db_module.get_db()
    stored = db_module.get_settings(con).get("public_base_url", "")
    con.close()
    assert stored == "", "приет невалиден адрес %r → %r" % (raw, stored)


@pytest.mark.parametrize("raw,expected", [
    ("firma.example.com", "https://firma.example.com"),
    ("https://example.com:8443/", "https://example.com:8443"),
    ("example.com:8443", "https://example.com:8443"),
    ("http://192.168.1.10:5000", "http://192.168.1.10:5000"),
    ("https://[::1]:8443", "https://[::1]:8443"),
    ("https://фирма.бг", "https://фирма.бг"),
])
def test_s4_public_url_accepts_valid_values(admin_client, db_module, raw, expected):
    post_with_csrf(admin_client, "/admin/system", {"form": "public_base_url",
                                                  "public_base_url": raw},
                   csrf_source_url="/admin/system")
    con = db_module.get_db()
    stored = db_module.get_settings(con).get("public_base_url", "")
    con.close()
    assert stored == expected


# ---------------------------------------------------------------- R8

@pytest.mark.parametrize("form,key", [("backup_folder", "backup_folder"),
                                      ("client_export", "client_export_dir")])
def test_r8_folder_pointing_to_a_file_is_rejected_and_input_kept(
        admin_client, db_module, tmp_path, form, key):
    a_file = tmp_path / "passwd"
    a_file.write_text("root:x:0:0")
    resp = post_with_csrf(admin_client, "/admin/system", {"form": form, key: str(a_file)},
                          csrf_source_url="/admin/system")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "сочи към файл" in body
    assert 'value="%s"' % a_file in body  # въведеното остава в полето
    con = db_module.get_db()
    assert db_module.get_settings(con).get(key, "") == ""
    con.close()


@pytest.mark.parametrize("form,key", [("backup_folder", "backup_folder"),
                                      ("client_export", "client_export_dir")])
def test_r8_relative_folder_is_rejected(admin_client, db_module, form, key):
    resp = post_with_csrf(admin_client, "/admin/system", {"form": form, key: "arhiv"},
                          csrf_source_url="/admin/system")
    assert resp.status_code == 200 and "пълен път" in resp.get_data(as_text=True)
    con = db_module.get_db()
    assert db_module.get_settings(con).get(key, "") == ""
    con.close()


def test_r8_missing_folder_is_created_and_saved(admin_client, db_module, tmp_path):
    target = tmp_path / "new" / "arhiv"
    resp = post_with_csrf(admin_client, "/admin/system",
                          {"form": "backup_folder", "backup_folder": str(target)},
                          csrf_source_url="/admin/system")
    assert resp.status_code == 302
    assert target.is_dir()
    con = db_module.get_db()
    assert db_module.get_settings(con)["backup_folder"] == str(target)
    con.close()


def test_r8_unwritable_folder_is_rejected(admin_client, db_module, tmp_path, monkeypatch):
    import tempfile as _tempfile

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    csrf = get_csrf_token(admin_client, "/admin/system")
    with monkeypatch.context() as mp:
        mp.setattr(_tempfile, "mkstemp", denied)
        resp = admin_client.post("/admin/system", data={
            "form": "backup_folder", "backup_folder": str(tmp_path), "csrf_token": csrf})
    assert resp.status_code == 200 and "Няма право на запис" in resp.get_data(as_text=True)


def test_r8_empty_folder_clears_setting(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/admin/system",
                          {"form": "backup_folder", "backup_folder": ""},
                          csrf_source_url="/admin/system")
    assert resp.status_code == 302


# ---------------------------------------------------------------- UX-12

def test_ux12_system_context_reports_the_real_port(flask_app, db_module, monkeypatch):
    import appcore
    import routes_settings
    monkeypatch.setitem(appcore._RUNTIME_STATE, "port", None)
    con = db_module.get_db()
    with flask_app.test_request_context("/admin/system", base_url="http://127.0.0.1:5123"):
        assert routes_settings.system_context(con)["runtime_port"] == 5123
    monkeypatch.setitem(appcore._RUNTIME_STATE, "port", 5001)
    with flask_app.test_request_context("/admin/system", base_url="http://127.0.0.1:5123"):
        assert routes_settings.system_context(con)["runtime_port"] == 5001
    con.close()


# ---------------------------------------------------------------- I3 / I9

@pytest.fixture
def fake_english(monkeypatch):
    """Подменя превода: всеки msgid става „EN:<msgid>“ — така се вижда кой
    текст минава през gettext и кой е залепен суров."""
    import flask_babel
    monkeypatch.setattr(flask_babel, "gettext",
                        lambda s, **kw: ("EN:" + s) % kw if kw else "EN:" + s)


def test_i3_attachment_error_is_translated_in_route(admin_client, db_module, fake_english):
    doc_id = _make_document(db_module)
    resp = post_with_csrf(admin_client, "/doc/%d/attachments" % doc_id,
                          {"attachment": (io.BytesIO(b""), "empty.pdf")},
                          csrf_source_url="/doc/%d" % doc_id,
                          content_type="multipart/form-data", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "EN:Файлът е празен." in body


def test_i3_errors_carry_msgid_and_translate_on_display(flask_app, db_module, fake_english):
    import attachments
    import backup
    import branding
    con = db_module.get_db()
    with pytest.raises(ValueError) as a:
        attachments.save_attachment(con, 1, _Upload(b""))
    with pytest.raises(ValueError) as b:
        branding.save_logo(_Upload(b"not an image"))
    with pytest.raises(ValueError) as c:
        backup.local_backup("")
    con.close()
    assert str(a.value) == "Файлът е празен."  # извън заявка — оригиналът
    with flask_app.test_request_context("/"):
        assert str(a.value) == "EN:Файлът е празен."
        assert str(b.value).startswith("EN:Файлът не е разпознато изображение")
        assert db_module.error_text(c.value) == "EN:Не е зададена папка за архив."


def test_i3_numbering_errors_translate_but_busy_marker_stays_bulgarian(flask_app, db_module,
                                                                        fake_english):
    clock = db_module.ClockBehindError(db_module.N_(
        "Часовникът на този компютър показва %(today)s, а в базата вече "
        "има номера от %(year)d година. Документ с номер от минала година "
        "няма да бъде издаден — поправете датата и часа на компютъра "
        "(Настройки на Windows → Дата и час) и опитайте пак."), today="01.01.2025", year=2026)
    busy = db_module.DatabaseBusyError(db_module.N_(
        "Не успяхме да генерираме следващия номер — базата данни е заета от "
        "друг едновременен запис (опитайте отново): %(reason)s"), reason="database is locked")
    with flask_app.test_request_context("/"):
        assert str(clock).startswith("EN:Часовникът") and "2026" in str(clock)
        # appcore разпознава заетата база по „заета“ в str(exc) — остава БГ
        assert "заета" in str(busy) and not str(busy).startswith("EN:")
        assert db_module.error_text(busy).startswith("EN:Не успяхме")
        assert isinstance(busy, RuntimeError)


def test_i9_last_backup_error_is_stored_as_msgid_and_translated_on_display(
        flask_app, db_module, fake_english):
    import backup
    with pytest.raises(ValueError):
        backup.local_backup("")
    con = db_module.get_db()
    raw = db_module.get_settings(con)[backup.LAST_ERROR_KEY]
    assert json.loads(raw)["msgid"] == "Не е зададена папка за архив."
    with flask_app.test_request_context("/"):
        assert backup.status(con)["last_error"] == "EN:Не е зададена папка за архив."
    # стар запис (обикновен текст) — показва се както е
    db_module.save_settings(con, {backup.LAST_ERROR_KEY: "стара грешка {без json"})
    con.commit()
    with flask_app.test_request_context("/"):
        assert backup.status(con)["last_error"] == "стара грешка {без json"
    con.close()


def test_i9_restore_result_reason_is_translated(admin_client, db_module, fake_english):
    import backup
    import routes_admin
    os.makedirs(os.path.dirname(backup._restore_result_path()), exist_ok=True)
    err = backup.BackupError(db_module.N_("маркерът е повреден"))
    backup._write_json_atomic(backup._restore_result_path(), {
        "ok": False, "backup": "x.db", "error": str(err),
        "error_record": db_module.error_record(err)})
    routes_admin._restore_result_checked.discard(db_module.DB_PATH)
    body = admin_client.get("/admin/system").get_data(as_text=True)
    assert "EN:маркерът е повреден" in body


# ---------------------------------------------------------------- F7

def _issue_dualuse(admin_client, dest_name):
    resp = post_with_csrf(admin_client, "/dualuse/new", {
        "sender_name": "Износител ЕООД", "invoice_numbers": "11249",
        "dest_name": dest_name, "destination_country": "Турция",
        "declarant_name": "Иван Петров", "place": "Габрово", "doc_date": "2026-10-04",
    }, csrf_source_url="/dualuse/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data[:300]
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def test_f7_dualuse_declaration_has_a_client(admin_client, db_module):
    doc_id = _issue_dualuse(admin_client, "Дестинация АД")
    con = db_module.get_db()
    assert con.execute("SELECT client_name FROM documents WHERE id = ?",
                       (doc_id,)).fetchone()[0] == "Дестинация АД"
    con.close()
    cid = _make_client(db_module, name="Дестинация АД")
    card = admin_client.get("/clients/%d/edit" % cid).get_data(as_text=True)
    assert "/doc/%d" % doc_id in card  # в картата на клиента


def test_f7_migration_backfills_existing_declarations(db_module):
    con = db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
        " VALUES ('dualuse', '0001/2026', 2026, 1, 'DUD-1', ?)",
        (json.dumps({"dest_name": "Стар Клиент"}, ensure_ascii=False),))
    doc_id = cur.lastrowid
    # състояние „отпреди“: празна колона (старият тригер не знаеше dest_name)
    con.execute("UPDATE documents SET client_name = '' WHERE id = ?", (doc_id,))
    # Одит (07.10.2026): позицията на _m012 (след нея има и други стъпки).
    con.execute("PRAGMA user_version = %d" % db_module.MIGRATIONS.index(
        db_module._m012_documents_client_name_dest_name))
    con.commit()
    db_module._apply_migrations(con)
    assert con.execute("SELECT client_name FROM documents WHERE id = ?",
                       (doc_id,)).fetchone()[0] == "Стар Клиент"
    con.close()


# ---------------------------------------------------------------- F11

def test_f11_usernames_differing_only_by_case_are_rejected(admin_client, db_module):
    for name in ("ivan", "IVAN", "Ivan"):
        post_with_csrf(admin_client, "/admin/users/new",
                       {"username": name, "password": "parola-123456", "full_name": "Иван"},
                       csrf_source_url="/admin/users")
    for name in ("Мария", "МАРИЯ"):
        post_with_csrf(admin_client, "/admin/users/new",
                       {"username": name, "password": "parola-123456", "full_name": "Мария"},
                       csrf_source_url="/admin/users")
    con = db_module.get_db()
    names = sorted(r[0] for r in con.execute("SELECT username FROM users"))
    con.close()
    assert [n for n in names if n.lower() == "ivan"] == ["ivan"]
    assert [n for n in names if n.lower() == "мария"] == ["Мария"]


# ---------------------------------------------------------------- F12

def test_f12_ci_contains_folds_turkish_dotted_and_dotless_i(db_module):
    c = db_module._ci_contains
    assert c("İstanbul Lojistik", "istanbul")
    assert c("ISTANBUL", "İstanbul")
    assert c("ıstanbul", "istanbul") and c("istanbul", "ıstanbul")
    assert c("x" * 300 + "İstanbul", "istanbul")  # дългият път (регекс)
    assert c("x" * 300 + "i̇stanbul", "İSTANBUL")
    assert not c("Ankara", "istanbul")


def test_f12_client_search_finds_istanbul(admin_client, db_module):
    _make_client(db_module, name="İstanbul Lojistik A.Ş.", city="İSTANBUL")
    body = admin_client.get("/clients?q=istanbul").get_data(as_text=True)
    assert "İstanbul Lojistik" in body


def test_f12_document_search_index_folds_turkish_i(db_module):
    import search_index
    con = db_module.get_db()
    search_index.ensure_schema(con)
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
        " VALUES ('cmr', '0002/2026', 2026, 2, 'CMR-2', ?)",
        (json.dumps({"consignee_city": "IĞDIR ıstanbul"}, ensure_ascii=False),))
    con.commit()
    body = con.execute("SELECT body FROM document_search WHERE id = ?",
                       (cur.lastrowid,)).fetchone()[0]
    assert "istanbul" in body and "iğdir" in body
    assert search_index.is_foldable("istanbul")
    assert not search_index.is_foldable("ıstanbul")
    assert not search_index.is_foldable("i̇stanbul")
    con.close()


def test_f12_old_search_index_is_rebuilt_on_start(db_module):
    """Стара база: тригерите са от предишната версия → ensure_schema ги
    пресъздава и преизгражда тялото (така се преиндексира без ръчна стъпка)."""
    import search_index
    con = db_module.get_db()
    search_index.ensure_schema(con)
    con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
                " VALUES ('cmr', '0003/2026', 2026, 3, 'CMR-3', ?)",
                (json.dumps({"x": "ıstanbul"}, ensure_ascii=False),))
    con.execute("UPDATE document_search SET body = 'ıstanbul'")  # „старото“ сгъване
    con.execute("DROP TRIGGER trg_document_search_update")
    con.execute("CREATE TRIGGER trg_document_search_update AFTER UPDATE OF data ON documents"
                " BEGIN SELECT 1; END")
    con.commit()
    search_index.ensure_schema(con)
    assert con.execute("SELECT body FROM document_search").fetchone()[0] == "istanbul"
    con.close()


# ---------------------------------------------------------------- UX-9

def test_ux9_duplicate_rows_in_file_are_not_counted_as_updates(db_module):
    import materials
    con = db_module.get_db()
    stats = {}
    added, updated = materials.replace_catalog(con, [
        ("MAT-001", "Bolt", "0.025"), ("MAT-002", "Washer", "0.0035"),
        ("mat-001", "Dup lowercase", "0.03")], stats=stats)
    assert (added, updated) == (2, 0)
    assert stats["case_conflicts"] == 0
    stats = {}
    added, updated = materials.replace_catalog(con, [
        ("mat-001", "A", ""), ("MAT-001", "B", ""), ("MAT-003", "C", "")], stats=stats)
    assert (added, updated) == (1, 1)
    assert stats["case_conflicts"] == 1
    con.close()


# ---------------------------------------------------------------- F10

def test_f10_deleting_client_with_documents_asks_with_count(admin_client, db_module):
    from conftest import issue_cmr
    issue_cmr(admin_client, consignee_name="Клиент с документи ООД")
    issue_cmr(admin_client, consignee_name="Клиент с документи ООД")
    cid = _make_client(db_module, name="Клиент с документи ООД")
    url = "/clients/%d/delete" % cid
    resp = post_with_csrf(admin_client, url, {}, csrf_source_url="/clients")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Издадени документи с клиента „Клиент с документи ООД“: 2." in body
    con = db_module.get_db()
    assert con.execute("SELECT 1 FROM clients WHERE id = ?", (cid,)).fetchone()
    con.close()
    resp = post_with_csrf(admin_client, url, {"confirm_documents": "1"}, csrf_source_url="/clients")
    assert resp.status_code == 302
    con = db_module.get_db()
    assert con.execute("SELECT 1 FROM clients WHERE id = ?", (cid,)).fetchone() is None
    con.close()


def test_f10_client_without_documents_is_deleted_directly(admin_client, db_module):
    cid = _make_client(db_module, name="Без документи ЕООД")
    resp = post_with_csrf(admin_client, "/clients/%d/delete" % cid, {}, csrf_source_url="/clients")
    assert resp.status_code == 302
