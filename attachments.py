# -*- coding: utf-8 -*-
"""Прикачване на снимка/скен към издаден документ — заявка: „направи
всичко което предлагаш“ (списък с предложения за подобрения). Например
снимка на подписана ЧМР бланка или скен на митническа декларация,
съхранени заедно с електронния документ в системата.

Файловете се пазят на диск до базата данни (СЪЩИЯТ подход като
company_logo в branding.py, за същата причина — при компилираната .exe
версия static/ се разопакова във временна папка на PyInstaller при всяко
стартиране и не е трайна за запис между пусканията), в
`<папка на базата>/attachments/<document_id>/<token>.<разширение>`.
Случаен token (не поредния ID) в името на файла, за да може файлът да се
запише на диск ПРЕДИ реда в базата да бъде потвърден с commit — при грешка
между двете стъпки просто остава неизползван файл на диск (безобиден), а
не ред в базата, сочещ към несъществуващ файл (счупена връзка в UI)."""
import os
import secrets
import shutil

import db

_ALLOWED_EXT = ("png", "jpg", "jpeg", "gif", "pdf")
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"%PDF-", "pdf"),
)
# По-щедро от логото (branding.MAX_SIZE = 3MB) — снимка от телефон на
# сканиран документ обичайно е няколко MB. Все пак значително под общия
# MAX_CONTENT_LENGTH на приложението (25MB, виж appcore.create_app), за да
# остане ясно, конкретно съобщение за грешка тук, вместо суровата грешка
# на Werkzeug при надвишен общ лимит на заявката.
MAX_SIZE = 15 * 1024 * 1024

_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "pdf": "application/pdf",
}


def _base_dir(document_id):
    return os.path.join(os.path.dirname(db.DB_PATH), "attachments", str(document_id))


def _detect_ext(head):
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    return None


def save_attachment(con, document_id, file_storage, uploaded_by=None):
    """Записва прикачен файл към документ, след проверка че реално е
    разпознат формат — по магическите байтове в началото на файла, НЕ
    само по разширението (както в branding.save_logo). Хвърля ValueError
    с ясно съобщение при проблем. Връща ID-то на новия ред в
    document_attachments."""
    data = file_storage.read()
    if not data:
        raise ValueError("Файлът е празен.")
    if len(data) > MAX_SIZE:
        raise ValueError("Файлът е твърде голям (макс. 15MB).")
    ext = _detect_ext(data[:8])
    if ext is None:
        raise ValueError(
            "Файлът не е разпознат формат (приемат се PNG, JPG, GIF или PDF)."
        )
    token = secrets.token_hex(16)
    base = _base_dir(document_id)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "%s.%s" % (token, ext))
    with open(path, "wb") as f:
        f.write(data)
    try:
        cur = con.execute(
            "INSERT INTO document_attachments"
            " (document_id, token, filename, ext, size, uploaded_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, token, (file_storage.filename or "файл")[:200],
             ext, len(data), uploaded_by),
        )
        con.commit()
    except Exception:
        os.remove(path)
        raise
    return cur.lastrowid


def list_attachments(con, document_id):
    return con.execute(
        "SELECT a.*, u.full_name AS uploaded_by_name FROM document_attachments a"
        " LEFT JOIN users u ON u.id = a.uploaded_by"
        " WHERE a.document_id = ? ORDER BY a.id DESC",
        (document_id,),
    ).fetchall()


def get_attachment(con, document_id, attachment_id):
    return con.execute(
        "SELECT * FROM document_attachments WHERE id = ? AND document_id = ?",
        (attachment_id, document_id),
    ).fetchone()


def attachment_path(document_id, row):
    return os.path.join(_base_dir(document_id), "%s.%s" % (row["token"], row["ext"]))


def delete_attachment(con, document_id, attachment_id):
    """Трие реда от базата и файла на диска; връща True ако е намерен и
    изтрит ред (False — вече не съществувал, напр. двоен клик)."""
    row = get_attachment(con, document_id, attachment_id)
    if row is None:
        return False
    con.execute("DELETE FROM document_attachments WHERE id = ?", (attachment_id,))
    con.commit()
    path = attachment_path(document_id, row)
    if os.path.exists(path):
        os.remove(path)
    return True


def delete_all_attachments_dir(document_id):
    """Одит (находка С9, среден риск): при изтриване на ЦЕЛИЯ документ
    `ON DELETE CASCADE` трие редовете в document_attachments, но НИКОЙ
    код не пипа файловете на диска — те остават осиротели завинаги
    (натрупване, и по-лошо: сканирана подписана бланка остава четима на
    споделения мрежов диск, макар документът да е „изтрит“ в приложението).
    Извиква се СЛЕД потвърденото (commit) изтриване на документа. Тихо не прави
    нищо, ако папката вече не съществува (документ без прикачени файлове —
    най-честият случай)."""
    shutil.rmtree(_base_dir(document_id), ignore_errors=True)


def mimetype(ext):
    return _MIME.get(ext, "application/octet-stream")
