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

import applog
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

# Одит (19.08.2026, информативна находка): дотук нямаше НИКАКЪВ таван на
# БРОЯ и на ОБЩИЯ обем прикачени файлове към един документ — само на
# размера на всеки поотделно (MAX_SIZE). Тоест един служител (или скрипт с
# неговата сесия) можеше да закачи стотици файлове по 15MB към един и същ
# документ: гигабайти на споделения диск/в архивите за GitHub
# синхронизация, страница на документа с безкраен списък и (най-неприятно)
# нарастващ размер на самата резервна копия. Реалната нужда е няколко
# снимки/скена на документ, затова таванът е щедър, но ясен, с конкретно
# съобщение вместо мълчаливо натрупване.
MAX_FILES = 20
MAX_TOTAL_SIZE = 60 * 1024 * 1024

_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "pdf": "application/pdf",
}


def _base_dir(document_id):
    return os.path.join(os.path.dirname(db.DB_PATH), "attachments", str(document_id))


#: Одит (19.08.2026, находка №18): символи, които нямат работа в име на
#: файл, подавано после като `download_name` на HTTP отговор. CR/LF са
#: най-важните: werkzeug (правилно) отказва да построи заглавието и връща
#: 500, тоест файл с такова име ставаше НЕСВАЛЯЕМ ЗАВИНАГИ — проверено с
#: изпълнение. Пътните разделители махаме, за да не изглежда името като
#: път при запис от страна на браузъра.
_UNSAFE_FILENAME_CHARS = "\r\n\t\x00/\\"


def _safe_display_filename(raw):
    """Изчистено име за показване/сваляне. Съдържанието на файла е
    независимо от това (пази се под случаен token — виж докстринга на
    модула), затова тук е достатъчно да махнем опасните символи, вместо да
    транслитерираме — кирилицата в имената е желана и остава."""
    name = (raw or "").strip()
    for ch in _UNSAFE_FILENAME_CHARS:
        name = name.replace(ch, "_")
    name = name.strip(". ") or "файл"
    return name[:200]


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
    # Одит (19.08.2026, информативна находка): таван на брой и общ обем на
    # прикачените към ЕДИН документ файлове — виж MAX_FILES по-горе.
    # Проверката е ТУК (не в маршрута), за да важи за всеки път, по който
    # се прикача файл, и се прави СЛЕД валидацията на самия файл, за да
    # получава потребителят първо по-конкретното съобщение.
    stats = con.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS total"
        " FROM document_attachments WHERE document_id = ?", (document_id,)
    ).fetchone()
    if stats["c"] >= MAX_FILES:
        raise ValueError(
            "Документът вече има %d прикачени файла (максимумът). Изтрийте "
            "ненужен файл, преди да прикачите нов." % MAX_FILES)
    if stats["total"] + len(data) > MAX_TOTAL_SIZE:
        raise ValueError(
            "Общият обем на прикачените към този документ файлове ще надхвърли "
            "%d MB (максимумът). Изтрийте ненужен файл или прикачете по-малък."
            % (MAX_TOTAL_SIZE // (1024 * 1024)))
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
            (document_id, token, _safe_display_filename(file_storage.filename),
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
    # Одит (16.08.2026, находка №36, дребна): редът в базата вече е
    # ИЗТРИТ И COMMIT-НАТ по-горе — оттук нататък самото изтриване вече е
    # успешно от гледна точка на потребителя/приложението; os.remove() на
    # файла е само чистене. os.path.exists() ПРЕДИ os.remove() е TOCTOU
    # проверка, не гаранция (особено на споделен мрежов диск — файлът може
    # да изчезне между двете, или изобщо да е недостъпен временно —
    # прекъсната мрежова връзка, заключен от антивирус и т.н.). Преди тази
    # поправка такъв OSError гърмеше НАГОРЕ и превръщаше УСПЕШНО изтриване
    # (базата вече е коректна) в гол 500 за потребителя. Логваме и
    # продължаваме — осиротял файл на диска е далеч по-малък проблем от
    # подвеждаща грешка при действие, което реално е успяло.
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        applog.log_exception(
            "attachments.delete_attachment: неуспешно изтриване на файла на диска "
            "(редът в базата вече е изтрит успешно)")
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
