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
import hashlib
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
#: Одит (19.08.2026, находка №27): сравнението се прави по casefold()
#: (Юникод-коректно сгъване на регистър), а не по .upper() — вижте
#: sanitize_client_folder_name.
_RESERVED_WIN_NAMES_FOLDED = {n.casefold() for n in _RESERVED_WIN_NAMES}


#: Одит (19.08.2026, находка №27): дължината си остава същата (120 знака —
#: разумна горна граница за име на папка), но вече е ЯВНА константа,
#: защото при съкращаване се долепя кратък хеш — виж по-долу.
_MAX_FOLDER_NAME = 120
_HASH_LEN = 8


def sanitize_client_folder_name(name):
    """Превръща свободно въведено име на клиент в безопасно име на папка.
    Празно/само служебни символи → „Без_име“ (никога не връща празен низ,
    иначе client_export_path би създал/писал направо в базовата папка).

    Одит (19.08.2026, находка №27, средна) — две отделни поправки:

    (1) **Съкращаването сливаше два различни клиента в една папка.** Преди
    това `cleaned[:120]` режеше сляпо: два реални клиента с дълги,
    официално изписани имена, различаващи се чак НАКРАЯ (напр. „… клон
    Пловдив“/„… клон Варна“ след общо начало от 120 знака), получаваха
    ИДЕНТИЧНА папка и износите им се смесваха. Сега при реално рязане се
    долепя кратък хеш на ПЪЛНОТО име — папките остават четими, но два
    различни клиента вече не могат да съвпаднат.

    (2) **Резервираните Windows имена се проверяваха по целия низ.** Windows
    не позволява устройствени имена като CON/PRN/NUL нито САМИ, нито с
    разширение: `PRN.txt` е също толкова невъзможен като `PRN`. Проверката
    сравняваше целия низ, така че клиент на име „PRN.txt“ (или „AUX.2“)
    минаваше, а `os.makedirs` после гърмеше на Windows — тихо изгубено
    копие в клиентската папка (виж находка №26 за невидимостта на този
    провал). Сега се сравнява частта ПРЕДИ първата точка, и то по
    casefold() (Юникод-коректното сгъване на регистър, за разлика от
    .upper() само за ASCII)."""
    cleaned = _FORBIDDEN.sub("_", (name or "").strip())
    cleaned = cleaned.strip(" .")  # Windows не позволява папки, свършващи на точка/интервал
    cleaned = re.sub(r"\s+", " ", cleaned)
    stem = cleaned.split(".", 1)[0].strip()
    if not cleaned or stem.casefold() in _RESERVED_WIN_NAMES_FOLDED:
        return "Без_име"
    if len(cleaned) > _MAX_FOLDER_NAME:
        digest = hashlib.sha256(cleaned.casefold().encode("utf-8")).hexdigest()[:_HASH_LEN]
        cleaned = cleaned[:_MAX_FOLDER_NAME - _HASH_LEN - 1].strip(" .") + "_" + digest
    return cleaned


def client_export_path(base_dir, client_name, filename):
    """Пълен път до файла в клиентската папка, СЪЗДАВАЙКИ папката (mkdir -p),
    ако липсва. base_dir трябва да е вече зададен (проверка на извикващия
    код) — тук само join + mkdir.

    Одит (19.08.2026, находка №27, средна): имената на папки на Windows са
    регистро-НЕЗАВИСИМИ, а на Linux/Mac — зависими. „фирма ООД“ и „ФИРМА
    ООД“ (един и същ клиент, въведен два пъти с различен регистър — нещо,
    което ОСТАНАЛАТА програма вече третира като един и същ клиент, виж
    resolve_client_alias/routes_clients._client_recent_documents) даваха
    ДВЕ папки на Linux и ЕДНА на Windows. Тоест износите на едно и също
    име се озовават на различни места в зависимост от машината, от която е
    свален файлът — а офисът ползва обща мрежова папка.

    Затова преди създаването на нова папка се търси вече съществуваща
    съседна, чието име съвпада след casefold() — намери ли се, пише се в
    НЕЯ. Резултатът е едно и също поведение на трите платформи."""
    folder_name = sanitize_client_folder_name(client_name)
    folder = os.path.join(base_dir, folder_name)
    if not os.path.isdir(folder):
        existing = _existing_folder_ignoring_case(base_dir, folder_name)
        if existing is not None:
            folder = existing
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, filename)


def _existing_folder_ignoring_case(base_dir, folder_name):
    """Вече съществуваща поддиректория на base_dir, чието име се различава
    от `folder_name` САМО по регистър — или None. Виж client_export_path
    по-горе (одит 19.08.2026, находка №27)."""
    target = folder_name.casefold()
    try:
        for entry in os.listdir(base_dir):
            if entry.casefold() == target and os.path.isdir(os.path.join(base_dir, entry)):
                return os.path.join(base_dir, entry)
    except OSError:
        # Базовата папка още не съществува/не е четима — makedirs по-долу
        # ще я създаде или ще гръмне с по-конкретната си грешка.
        return None
    return None


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


#: Одит (19.08.2026, находка №26): трите изхода на save_client_export_status
#: — „изключено/неприложимо“, „записано“ и „ОПИТА СЕ, но се провали“. Точно
#: третият случай досега беше неразличим от първия (и двата връщаха False),
#: затова извикващият нямаше как да покаже предупреждение само когато има
#: за какво.
EXPORT_SKIPPED = "skipped"
EXPORT_OK = "ok"
EXPORT_FAILED = "failed"


def save_client_export_status(settings, doc_type, data, filename, file_bytes):
    """Best-effort запис на копие от износа (PDF/Excel) в клиентската папка,
    ако е включено в системните настройки. Никога не хвърля грешка — връща
    EXPORT_SKIPPED / EXPORT_OK / EXPORT_FAILED.

    Одит (19.08.2026, находка №26, средна): досега функцията връщаше само
    True/False, а ДВЕТЕ места, които я викат (routes_documents.
    export_document_xlsx и export_document_pdf), игнорираха върнатата
    стойност изцяло. Единствената следа от провален запис беше ред в лог
    файла — който потребител на .exe никога не отваря. Свалянето през
    браузъра при това УСПЯВА, така че операторът остава убеден, че копието
    е и на общия диск (недостъпен мрежов път, пълен диск, твърде дълъг път
    на Windows, име на папка, което Windows отказва — виж находка №27).
    Разграничаването на „не се и опитахме“ от „опитахме и не стана“ е
    цялата причина за тази функция; save_client_export_copy по-долу остава
    като тънка обвивка за вече написания код и тестовете."""
    if not settings.get("client_export_auto"):
        return EXPORT_SKIPPED
    base_dir = (settings.get("client_export_dir") or "").strip()
    if not base_dir:
        return EXPORT_SKIPPED
    client_name = resolve_client_name(data)
    if not client_name:
        return EXPORT_SKIPPED
    try:
        path = client_export_path(base_dir, client_name, filename)
        with open(path, "wb") as f:
            f.write(file_bytes)
        return EXPORT_OK
    except OSError:
        applog.log_exception(
            "client_export: неуспешен запис на копие за клиент „%s“ (%s)" % (client_name, doc_type))
        return EXPORT_FAILED


def save_client_export_copy(settings, doc_type, data, filename, file_bytes):
    """Както save_client_export_status, но с булев резултат (True само при
    реално записан файл) — запазена за вече написания код и тестовете."""
    return save_client_export_status(
        settings, doc_type, data, filename, file_bytes) == EXPORT_OK
