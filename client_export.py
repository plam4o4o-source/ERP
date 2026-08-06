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
