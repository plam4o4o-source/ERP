# -*- coding: utf-8 -*-
"""Клиентски папки за автоматичен запис на износи (PDF/Excel) — заявка:
„всеки клиент да се запазват в отделни папки във всички документи“.

Когато е включено (виж настройка client_export_auto/client_export_dir в
„Моите настройки“ → системни настройки, routes_admin.system_settings),
всеки износ на документ (Excel .xlsx, скоро и PDF) СЪЩО се записва
автоматично в <базова папка>/<име на клиента>/, ОСВЕН да се сваля през
браузъра. Записът е best-effort: грешка тук (непозволен път, недостъпен
мрежов диск и т.н.) НЕ бива да проваля самото сваляне на файла за
потребителя — затова save_client_export_copy() никога не хвърля грешка,
само я логва (виж applog)."""
import os
import re

import applog

# Символи, забранени в имена на файлове/папки на Windows (най-строгата от
# трите платформи, на които може да работи програмата — виж version.py/
# desktop.py) — филтрираме спрямо тях независимо от текущата ОС, за да е
# едно и също поведение навсякъде и да няма изненади при преместване на
# базата между Windows/Linux/Mac (мрежов режим).
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED_WIN_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *("COM%d" % i for i in range(1, 10)),
    *("LPT%d" % i for i in range(1, 10)),
}


def sanitize_client_folder_name(name):
    """Превръща свободно въведено име на клиент в безопасно име на папка.
    Празно/само служебни символи → „Без_име“ (никога не връща празен низ,
    иначе client_export_path би създал/писал направо в базовата папка)."""
    cleaned = _FORBIDDEN.sub("_", (name or "").strip())
    cleaned = cleaned.strip(" .")  # Windows не позволява папки, свършващи на точка/интервал
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or cleaned.upper() in _RESERVED_WIN_NAMES:
        cleaned = "Без_име"
    return cleaned[:120]  # разумна горна граница за дължина на име на папка


def client_export_path(base_dir, client_name, filename):
    """Пълен път до файла в клиентската папка, СЪЗДАВАЙКИ папката (mkdir -p),
    ако липсва. base_dir трябва да е вече зададен (проверка на извикващия
    код) — тук само join + mkdir."""
    folder = os.path.join(base_dir, sanitize_client_folder_name(client_name))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, filename)


def resolve_client_name(data):
    """Името на клиента/получателя от данните на документ — СЪЩИЯТ
    приоритет като показвания в „Издадени документи“ (виж
    templates/documents.html: m.consignee_name or m.receiver_name or
    m.client_name), само в обратен ред при търсене по dict — тук пазим
    идентична логика, за да сочи към СЪЩАТА папка, която потребителят
    вижда в списъка."""
    return (data.get("consignee_name") or data.get("receiver_name")
           or data.get("client_name") or "").strip()


_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename_stub(text):
    """Прави низ безопасен за директна употреба В ИМЕТО на файл (не на
    папка — за това вижте sanitize_client_folder_name по-горе): същите
    забранени Windows знаци се заменят с интервал (не директно с долна
    черта — иначе „ACME Ltd / Co.“ би дало тройна долна черта, вижте по-
    долу), после ВСЯКА поредица от интервали И/ИЛИ долни черти се събира в
    ЕДНА долна черта (по-удобно/кликаемо кратко име на файл, напр.
    „ACME_Ltd_Co“, не „ACME_Ltd___Co“ или „ACME Ltd Co“)."""
    cleaned = _FILENAME_UNSAFE.sub(" ", (text or "").strip())
    cleaned = re.sub(r"[\s_]+", "_", cleaned)
    cleaned = cleaned.strip("_.")
    return cleaned[:80]


def sanitize_number_stub(number):
    """Одит (находка В18, висок риск): routes_documents._export_filename
    строеше файловото име от `row["number"]` с `.replace("/", "-")` —
    заменяше САМО наклонена черта, не и обратна наклонена черта (`\\`) или
    останалите Windows-забранени знаци (`:`, `*`, `?`, `"`, `<`, `>`, `|`).
    За автоматично генерираните номера (ЧМР/опаковъчен лист/палетна
    карта/...) това е безопасно — форматът им е фиксиран. Но фактурите
    имат РЪЧНО въвеждан номер (DOCUMENT_FLOWS[...]['manual_number_field'])
    — низ, изцяло контролиран от потребителя. Номер като
    "..\\..\\..\\нещо" преди поправката минаваше НЕПРОМЕНЕН (нямаше „/“ за
    replace) в save_client_export_copy по-долу, което строи пътя на диска
    чрез директно свързване на базовата папка с това име — path traversal
    риск (запис извън предвидената клиентска папка).

    Заменя ВСИЧКИ забранени знаци (СЪЩИЯТ _FILENAME_UNSAFE регекс, ползван
    от sanitize_filename_stub) с тире — нарочно тире, не долна черта, за да
    остане СЪЩОТО поведение за нормални номера като преди ("0001/2026" →
    "0001-2026", идентично на стария `.replace("/", "-")`), само реално
    покрива всички опасни знаци, не само единия."""
    return _FILENAME_UNSAFE.sub("-", (number or "").strip())


def resolve_client_alias(con, data):
    """Псевдонимът (db.clients.alias) на клиента на документа, ако адресната
    книга съдържа запис със СЪЩОТО име — заявка: „наименованието на файла
    палетната карта ... да е псевдонима на клиента“. Точното име идва от
    resolve_client_name() (същия приоритет consignee/receiver/client_name);
    съвпадението с адресната книга е регистронезависимо.

    ВАЖНО: SQLite-ското COLLATE NOCASE сгъва малки/големи букви САМО за
    ASCII (a-z/A-Z) — кирилицата (по-голямата част от имената на клиенти в
    тази програма) минава през него непроменена, затова SQL сравнение с
    COLLATE NOCASE НЕ намира „регистър тест ООД“ при запис „Регистър Тест
    ООД“. Python-ското str.lower() сгъва Юникод правилно (включително
    кирилица), затова сравнението се прави тук, а не в SQL заявката —
    адресната книга е малка (десетки/стотици записи), така че извличането
    на всички имена и сравнението в Python е достатъчно бързо.

    Връща "" (не None) при липса на съвпадение или незададен псевдоним —
    извикващият код тогава пада обратно към досегашното име на файла,
    вместо да гърми/да остави файла без име."""
    name = resolve_client_name(data)
    if not name or con is None:
        return ""
    target = name.strip().lower()
    for row in con.execute("SELECT name, alias FROM clients"):
        if (row["name"] or "").strip().lower() == target:
            return row["alias"] or ""
    return ""


def save_client_export_copy(settings, doc_type, data, filename, file_bytes):
    """Best-effort запис на копие от износа (PDF/Excel) в клиентската папка,
    ако е включено в системните настройки. Никога не хвърля грешка —
    връща True/False само информативно (използва се от тестовете)."""
    if not settings.get("client_export_auto"):
        return False
    base_dir = (settings.get("client_export_dir") or "").strip()
    if not base_dir:
        return False
    client_name = resolve_client_name(data)
    if not client_name:
        return False
    try:
        path = client_export_path(base_dir, client_name, filename)
        with open(path, "wb") as f:
            f.write(file_bytes)
        return True
    except OSError:
        applog.log_exception(
            "client_export: неуспешен запис на копие за клиент „%s“ (%s)" % (client_name, doc_type))
        return False
