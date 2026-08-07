# -*- coding: utf-8 -*-
"""Справочник материали (ABB part ID → описание → нето тегло кг/бр).

Заявка: „Прикачвам ти файл със съответния материал, описанието на
материала и килограми на всеки материал. От файла с килограмите
автоматично да се извличат съответните килограми във фактурата“ + „да не
се зарежда всеки път файла с материалите; като се зареди веднъж, да си
остава зареден в програмата“.

Затова справочникът НЕ се качва при всяка фактура, а се внася ВЕДНЪЖ в
таблица `materials` в базата (виж db.SCHEMA) и оттам нататък стои. При
издаване на фактура въведеният код на материала се търси в тази таблица и
теглото/описанието се попълват автоматично (виж routes_invoices.py).
Повторно качване на нов файл просто ОБНОВЯВА вече заредените редове по
код и добавя новите — старите редове, които ги няма в новия файл, се
запазват (частично обновяване на ценоразпис не бива да изтрива
исторически материали, ползвани от вече издадени фактури).

Теглото се пази като ТЕКСТ, а не като REAL — по същата причина, по която
и количествата/теглата в самите документи са текст: стойностите идват от
Excel файл на трета страна, могат да са с различен десетичен разделител
или с празни/нечислови клетки, а програмата само ги ПОКАЗВА обратно, не
смята с тях (общото тегло се смята на момента, толерантно, виж
parse_number по-долу).
"""
import io

#: Заглавия на колоните в подадения Excel файл, по които се разпознават
#: трите нужни колони. Търси се точно съвпадение след смъкване до малки
#: букви и изчистване на празните места/нови редове (заглавието на
#: колоната с теглото в оригиналния файл е на два реда: "Net weight\n[KG/pc]").
_CODE_HEADERS = ("abb part id", "part id", "material code", "code", "материал", "код")
_DESC_HEADERS = ("description", "material description", "описание")
_WEIGHT_HEADERS = ("net weight [kg/pc]", "net weight[kg/pc]", "net weight",
                   "weight [kg/pc]", "weight", "kg/pc", "тегло")


def _cellstr(v):
    """Клетка към низ, без излишно „.0“ за цели числа, записани като float
    (същата помощна функция като в routes_pallet_extra._cellstr — Excel
    връща всички числа като float, а кодовете на материали се ползват като
    текстови ключове)."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _norm_header(v):
    """Заглавие на колона към сравним вид: малки букви, без нови редове и
    без повтарящи се празни места."""
    return " ".join(_cellstr(v).lower().split())


def parse_number(value):
    """Толерантно четене на число от текстово поле (тегло, количество,
    цена) — приема и десетична запетая, и точка, и празно. Връща None при
    непарсваема/празна стойност, за да може извикващият да реши какво да
    показва (обичайно „—“ вместо 0)."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    try:
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return None


def _fmt_weight(value):
    """Тегло към текст за запис в справочника — реже безсмисленото
    „плаващо“ опашче на float-овете от Excel (0.087135000000001) до 6
    знака, но пази стойността, ако не е число."""
    number = parse_number(value)
    if number is None:
        return _cellstr(value)
    text = "%.6f" % number
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_catalog_xlsx(file_bytes):
    """Чете качения Excel файл със справочника и връща списък от
    (код, описание, тегло) тройки, ИЛИ None ако файлът не съдържа
    разпознаваеми колони.

    Разпознава колоните по ЗАГЛАВИЕ (не по позиция), за да продължи да
    работи, ако ABB разместят колоните в следваща версия на ценоразписа.
    Търси заглавния ред сред първите 10 реда — реалните файлове често имат
    празни/декоративни редове най-отгоре.

    Редове без код се пропускат. Редове с код, но без тегло или без
    описание, се ЗАПАЗВАТ (в подадения файл има 845 без тегло и 105 без
    описание) — за тях просто няма какво да се попълни автоматично във
    фактурата и полето остава за ръчно въвеждане, което е по-добре от това
    материалът изобщо да липсва от справочника.
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header_row = None
        cols = None
        for i, row in enumerate(rows):
            if i > 10:
                break
            headers = [_norm_header(c) for c in row]
            code_i = desc_i = weight_i = None
            for j, h in enumerate(headers):
                if code_i is None and h in _CODE_HEADERS:
                    code_i = j
                elif desc_i is None and h in _DESC_HEADERS:
                    desc_i = j
                elif weight_i is None and h in _WEIGHT_HEADERS:
                    weight_i = j
            if code_i is not None and (desc_i is not None or weight_i is not None):
                header_row = i
                cols = (code_i, desc_i, weight_i)
                break
        if header_row is None:
            continue

        code_i, desc_i, weight_i = cols
        out = []
        for row in rows:  # итераторът вече е СЛЕД заглавния ред
            code = _cellstr(row[code_i]) if code_i < len(row) else ""
            if not code:
                continue
            desc = _cellstr(row[desc_i]) if desc_i is not None and desc_i < len(row) else ""
            weight = _fmt_weight(row[weight_i]) if weight_i is not None and weight_i < len(row) else ""
            out.append((code, desc, weight))
        if out:
            return out
    return None


def replace_catalog(con, entries):
    """Записва прочетените редове в базата — обновява вече съществуващите
    по код и добавя новите, в ЕДНА транзакция.

    Нарочно НЕ трие таблицата преди това: нов ценоразпис обичайно покрива
    само част от материалите, а вече издадени фактури сочат към кодове,
    които трябва да продължат да се разпознават. Връща (нови, обновени).
    """
    existing = {r["code"] for r in con.execute("SELECT code FROM materials")}
    added = updated = 0
    for code, desc, weight in entries:
        if code in existing:
            updated += 1
        else:
            added += 1
            existing.add(code)
        con.execute(
            "INSERT INTO materials (code, description, net_weight, updated_at)"
            " VALUES (?, ?, ?, datetime('now','localtime'))"
            " ON CONFLICT(code) DO UPDATE SET"
            " description = excluded.description,"
            " net_weight = excluded.net_weight,"
            " updated_at = excluded.updated_at",
            (code, desc, weight),
        )
    con.commit()
    return added, updated


def lookup(con, code):
    """Един материал по точен код, или None. Кодът се търси както е
    въведен и в горен регистър — операторите често пишат кода на ръка и
    регистърът се разминава с този във файла."""
    code = (code or "").strip()
    if not code:
        return None
    row = con.execute("SELECT * FROM materials WHERE code = ?", (code,)).fetchone()
    if row is None:
        row = con.execute("SELECT * FROM materials WHERE UPPER(code) = UPPER(?)",
                          (code,)).fetchone()
    return row


def lookup_many(con, codes):
    """Няколко материала наведнъж → {търсен код: ред} за намерените.

    Ползва се при зареждане на цяла палетна карта във фактура (десетки
    реда) — една заявка вместо по една на ред. Заявката е с UPPER(), за да
    хване и разминат регистър, а резултатът се връща с ОРИГИНАЛНО
    подадения код като ключ, за да може извикващият да го съпостави с реда
    си без допълнително нормализиране.
    """
    wanted = [c for c in ((c or "").strip() for c in codes) if c]
    if not wanted:
        return {}
    found = {}
    # Разбива на партиди — SQLite има ограничение за брой параметри (999 по
    # подразбиране в по-старите билдове), а палетна карта може да е голяма.
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        # placeholders е само поредица от „?“, изчислена от БРОЯ елементи —
        # самите кодове никога не влизат в SQL текста, а се подават като
        # bound параметри на реда отдолу. Същият шаблон като
        # db.get_unload_points_map.
        placeholders = ",".join("?" for _ in chunk)
        rows = con.execute(
            "SELECT * FROM materials WHERE UPPER(code) IN (%s)" % placeholders,  # nosec B608 -- само „?“ плейсхолдъри по брой; стойностите са bound параметри (виж коментара по-горе)
            [c.upper() for c in chunk],
        ).fetchall()
        by_upper = {r["code"].upper(): r for r in rows}
        for code in chunk:
            row = by_upper.get(code.upper())
            if row is not None:
                found[code] = row
    return found


def count(con):
    """Брой заредени материали — показва се в раздел „Материали“."""
    return con.execute("SELECT COUNT(*) AS c FROM materials").fetchone()["c"]


def search(con, query, limit=200):
    """Търсене по код ИЛИ описание (за екрана на раздел „Материали“).
    Празна заявка връща първите `limit` реда по код."""
    query = (query or "").strip()
    if not query:
        return con.execute(
            "SELECT * FROM materials ORDER BY code LIMIT ?", (limit,)).fetchall()
    like = "%" + query + "%"
    return con.execute(
        "SELECT * FROM materials WHERE code LIKE ? OR description LIKE ?"
        " ORDER BY code LIMIT ?",
        (like, like, limit),
    ).fetchall()
