# -*- coding: utf-8 -*-
"""Проверка на ДДС номер във VIES (системата на ЕС за обмен на информация
по ДДС).

Заявка (06.11.2026, PARTIES): „Бутон за проверка на ДДС номер във VIES в
адресника (с кеширан резултат и дата на проверката)“.

* Публичната REST услуга на Европейската комисия, без ключ:
  POST https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
  с JSON {"countryCode": "DE", "vatNumber": "123456789"}. Отговорът носи
  `valid`, `name`, `address`, `requestDate`, `requestIdentifier`; при грешка
  — {"actionSucceed": false, "errorWrappers": [{"error": "MS_UNAVAILABLE"}]}
  (или `userError` в GET варианта /ms/{cc}/vat/{num}) — и двата вида се
  разпознават.
* Програмата е офлайн десктоп: заявка се прави САМО при натиснат бутон
  (никога автоматично), с таймаут и ясна грешка. Минава през net.urlopen
  (системните прокси настройки + резервния certifi пакет при SSL грешка).
* Тестовете подменят `_http_post_json` — без мрежа.
"""
import json
import re
import socket
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime

import net

#: Официалният адрес на REST услугата (константа — никога от потребителски вход).
VIES_CHECK_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"

#: Секунди за цялата заявка — услугата понякога отговаря бавно.
VIES_TIMEOUT = 10

#: Кодовете на държавите във VIES. Гърция е „EL“ (не „GR“); „XI“ —
#: Северна Ирландия (стоки) след Brexit.
VIES_COUNTRY_CODES = frozenset((
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR",
    "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI",
    "SK", "XI"))

#: ISO код → код във VIES, където се различават.
_ISO_TO_VIES = {"GR": "EL"}

#: Държавата от адреса на клиента (BG/EN/местно изписване) → код във VIES —
#: за номер, въведен без префикс („123456789“ при държава „България“).
_COUNTRY_NAMES = {
    "AT": ("австрия", "austria", "österreich", "osterreich"),
    "BE": ("белгия", "belgium", "belgique", "belgië", "belgie"),
    "BG": ("българия", "bulgaria", "republic of bulgaria", "република българия"),
    "CY": ("кипър", "cyprus"),
    "CZ": ("чехия", "czech republic", "czechia", "česko", "cesko"),
    "DE": ("германия", "germany", "deutschland"),
    "DK": ("дания", "denmark", "danmark"),
    "EE": ("естония", "estonia", "eesti"),
    "EL": ("гърция", "greece", "hellas", "ελλάδα", "ελλαδα"),
    "ES": ("испания", "spain", "españa", "espana"),
    "FI": ("финландия", "finland", "suomi"),
    "FR": ("франция", "france"),
    "HR": ("хърватия", "croatia", "hrvatska"),
    "HU": ("унгария", "hungary", "magyarország", "magyarorszag"),
    "IE": ("ирландия", "ireland"),
    "IT": ("италия", "italy", "italia"),
    "LT": ("литва", "lithuania", "lietuva"),
    "LU": ("люксембург", "luxembourg"),
    "LV": ("латвия", "latvia", "latvija"),
    "MT": ("малта", "malta"),
    "NL": ("нидерландия", "холандия", "netherlands", "the netherlands", "holland", "nederland"),
    "PL": ("полша", "poland", "polska"),
    "PT": ("португалия", "portugal"),
    "RO": ("румъния", "romania", "românia"),
    "SE": ("швеция", "sweden", "sverige"),
    "SI": ("словения", "slovenia", "slovenija"),
    "SK": ("словакия", "slovakia", "slovensko"),
    "XI": ("северна ирландия", "northern ireland"),
}
_NAME_TO_CODE = {name: code for code, names in _COUNTRY_NAMES.items() for name in names}

#: Номерът след префикса: букви/цифри (+ и * при Ирландия/Холандия), 2–12 знака.
_NUMBER_RE = re.compile(r"[0-9A-Z+*]{2,12}\Z")

#: Кодовете на грешките на VIES → вид на грешката (за текста в интерфейса).
_UNAVAILABLE_CODES = frozenset(("MS_UNAVAILABLE", "SERVICE_UNAVAILABLE", "TIMEOUT",
                                "GLOBAL_MAX_CONCURRENT_REQ", "MS_MAX_CONCURRENT_REQ",
                                "GLOBAL_MAX_CONCURRENT_REQ_TIME", "MS_MAX_CONCURRENT_REQ_TIME"))
_BLOCKED_CODES = frozenset(("VAT_BLOCKED", "IP_BLOCKED", "BLOCKED"))
_INPUT_CODES = frozenset(("INVALID_INPUT", "INVALID_REQUESTER_INFO"))


class ViesError(Exception):
    """Проверката не можа да се направи. `kind`: 'input' (номерът не е във
    вид за VIES), 'unavailable' (услугата/държавата не отговаря — опитайте
    по-късно), 'network' (няма връзка/таймаут), 'blocked', 'unknown'."""

    def __init__(self, kind, code="", detail=""):
        super().__init__("%s %s %s" % (kind, code, detail))
        self.kind = kind
        self.code = code
        self.detail = detail


def country_code_for(country):
    """Кодът във VIES за държавата от адреса („България“ → „BG“, „GR“ →
    „EL“) или "" ако не е държава от ЕС/не е разпозната."""
    text = " ".join(str(country or "").replace(".", " ").split()).lower()
    if not text:
        return ""
    upper = text.upper()
    if upper in _ISO_TO_VIES:
        return _ISO_TO_VIES[upper]
    if upper in VIES_COUNTRY_CODES:
        return upper
    return _NAME_TO_CODE.get(text, "")


def normalize_vat(vat, country_hint=""):
    """(код на държавата, номер) за VIES от свободно въведен ДДС номер.

    „BG 123 456 789“, „bg123456789“, „BG-123.456.789“ → ("BG", "123456789");
    „GR094...“ → ("EL", "094..."); номер без префикс взема държавата от
    `country_hint` (адреса на клиента). Хвърля ViesError('input'), ако
    номерът не е във вид за VIES."""
    raw = str(vat or "").upper()
    compact = re.sub(r"[\s.\-/_,:;  ]+", "", raw)
    if not compact:
        raise ViesError("input", "EMPTY")
    prefix = compact[:2]
    if prefix.isalpha():
        code = _ISO_TO_VIES.get(prefix, prefix)
        if code not in VIES_COUNTRY_CODES:
            raise ViesError("input", "COUNTRY", prefix)
        number = compact[2:]
    else:
        code = country_code_for(country_hint)
        if not code:
            raise ViesError("input", "NO_PREFIX")
        number = compact
    if not _NUMBER_RE.match(number):
        raise ViesError("input", "FORMAT", number)
    return code, number


def _http_post_json(url, payload, timeout):
    """POST с JSON тяло → (HTTP статус, речник от отговора). Отделна функция,
    за да я подменят тестовете (без мрежа). Грешка с HTTP статус (4xx/5xx)
    връща тялото ѝ — VIES описва причината вътре (errorWrappers)."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "PH-Logistics"})
    try:
        with net.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            data = {}
        return exc.code, data if isinstance(data, dict) else {}


def _clean_text(value):
    """„---“ (данните не се разкриват — напр. Германия) → празно; редовете
    на адреса — по един интервал, новите редове се пазят."""
    text = str(value or "").strip()
    if text.strip("-– ") == "":
        return ""
    lines = [" ".join(ln.split()) for ln in text.replace("\r", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _error_code(data):
    wrappers = data.get("errorWrappers") if isinstance(data, dict) else None
    if isinstance(wrappers, list) and wrappers:
        first = wrappers[0] if isinstance(wrappers[0], dict) else {}
        return str(first.get("error") or "UNKNOWN").upper()
    user_error = str(data.get("userError") or "").upper() if isinstance(data, dict) else ""
    if user_error and user_error not in ("VALID", "INVALID"):
        return user_error
    return ""


def check_vat(vat, country_hint="", timeout=VIES_TIMEOUT):
    """Проверява номера във VIES. Връща речник: country_code, vat_number,
    vat (с префикса), valid (bool), name, address, request_date,
    request_id, checked_at (местно време „ГГГГ-ММ-ДД ЧЧ:ММ:СС“).
    Хвърля ViesError при невъзможна проверка."""
    code, number = normalize_vat(vat, country_hint)
    try:
        status, data = _http_post_json(VIES_CHECK_URL,
                                       {"countryCode": code, "vatNumber": number}, timeout)
    except (socket.timeout, TimeoutError) as exc:
        raise ViesError("network", "TIMEOUT", str(exc)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ViesError("network", type(exc).__name__, str(exc)) from exc
    if not isinstance(data, dict):
        raise ViesError("unknown", "BAD_RESPONSE")
    err = _error_code(data)
    if err:
        if err in _UNAVAILABLE_CODES:
            raise ViesError("unavailable", err)
        if err in _BLOCKED_CODES:
            raise ViesError("blocked", err)
        if err in _INPUT_CODES:
            raise ViesError("input", err)
        raise ViesError("unknown", err)
    if status >= 400:
        raise ViesError("unavailable" if status >= 500 else "unknown", "HTTP_%d" % status)
    valid = data.get("valid", data.get("isValid"))
    if not isinstance(valid, bool):
        raise ViesError("unknown", "BAD_RESPONSE")
    return {
        "country_code": code,
        "vat_number": number,
        "vat": code + number,
        "valid": valid,
        "name": _clean_text(data.get("name") or data.get("traderName")),
        "address": _clean_text(data.get("address") or data.get("traderAddress")),
        "request_date": str(data.get("requestDate") or ""),
        "request_id": str(data.get("requestIdentifier") or "")[:64],
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


#: Таблиците, в които се кешира резултатът (име → име на таблица в SQL).
CACHE_TABLES = {"client": "clients", "invoice_client": "invoice_clients", "carrier": "carriers"}


def save_result(con, target, target_id, result):
    """Кешира резултата на записа (колоните vies_* от db._m016). Връща True,
    ако записът съществува. Не прави commit."""
    table = CACHE_TABLES.get(target)
    if table is None or not target_id:
        return False
    cur = con.execute(
        "UPDATE %s SET vies_vat = ?, vies_valid = ?, vies_name = ?, vies_address = ?,"  # nosec B608 -- таблицата идва само от константата CACHE_TABLES
        " vies_checked_at = ?, vies_request_id = ? WHERE id = ?" % table,
        (result["vat"], 1 if result["valid"] else 0, result["name"], result["address"],
         result["checked_at"], result["request_id"], target_id))
    return cur.rowcount > 0


def cached_result(row):
    """Кешираният резултат от ред на клиент/превозвач (sqlite3.Row/речник) или
    None, ако номерът не е проверяван (или базата е отпреди миграцията)."""
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    if "vies_checked_at" not in keys or not row["vies_checked_at"]:
        return None
    return {"vat": row["vies_vat"] or "", "valid": bool(row["vies_valid"]),
            "name": row["vies_name"] or "", "address": row["vies_address"] or "",
            "checked_at": row["vies_checked_at"], "request_id": row["vies_request_id"] or ""}


def cached_status_text(con, vat):
    """Заявка (06.11.2026, интеграция CUSTOMS↔PARTIES): кешираният резултат от
    VIES за номера — „валиден (ДД.ММ.ГГГГ)“ / „невалиден (…)“ или "" — за
    колоната „VIES“ в Intrastat. Само чете кеша, никога не вика услугата."""
    try:
        want = normalize_vat(vat)
    except ViesError:
        return ""
    best = None
    for table in CACHE_TABLES.values():
        try:
            rows = con.execute(
                "SELECT vies_vat, vies_valid, vies_checked_at FROM %s"  # nosec B608 -- таблицата идва само от константата CACHE_TABLES
                " WHERE vies_checked_at IS NOT NULL AND vies_vat <> ''" % table).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            try:
                same = normalize_vat(row["vies_vat"]) == want
            except ViesError:
                same = False
            if same and (best is None or row["vies_checked_at"] > best[1]):
                best = (bool(row["vies_valid"]), row["vies_checked_at"])
    if best is None:
        return ""
    day = str(best[1])[:10]
    if len(day) == 10 and day[4] == "-":
        day = day[8:10] + "." + day[5:7] + "." + day[:4]
    return ("валиден" if best[0] else "невалиден") + " (" + day + ")"


def same_number(vat_a, vat_b, country_hint=""):
    """Дали двата записа са един и същ номер след нормализацията (за
    предупреждението „проверката е за друг номер“)."""
    try:
        return normalize_vat(vat_a, country_hint) == normalize_vat(vat_b, country_hint)
    except ViesError:
        return False
