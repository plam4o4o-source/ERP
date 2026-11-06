# -*- coding: utf-8 -*-
"""Регистър на превозвачите — фирма, ДДС/EORI, адрес, контакт, камиони и
ремаркета (рег. номера) и шофьори.

Заявка (06.11.2026, PARTIES): „Регистър на превозвачите с PDF поръчка за
превоз. По желание и запомнен превозвач и шофьор по клиент“.

Както при адресната книга, данните се КОПИРАТ в документа при издаване
(полета 16–18 на ЧМР + скритото `carrier_id`) — по-късна промяна в регистъра
не пренаписва вече издадени ЧМР.

`last_for_client(con, client_key)` е общата точка за „превозвачът от
последното ЧМР за този клиент“ — ползват я формата на ЧМР (static/parties.js,
/carriers/last-for-client) и шаблоните по клиент на група SHIP."""
import jsonutil

#: Полетата на превозвача в реда на формата (carrier_form.html).
FIELDS = ("name", "vat", "eori", "address", "postcode", "city", "country",
          "contact", "phone", "email", "notes")

#: Колони на камионите/ремаркетата и шофьорите в динамичните таблици.
VEHICLE_FIELDS = ("plate", "note")
DRIVER_FIELDS = ("name", "phone", "id_doc", "note")

#: Видове превозни средства — влекач (камион) и ремарке.
VEHICLE_KINDS = ("truck", "trailer")

PAGE_SIZE = 100
#: Колко превозвача се вграждат във формата на ЧМР (като адресната книга —
#: appcore.CLIENT_EMBED_LIMIT); останалите се намират през /carriers/lookup.
EMBED_LIMIT = 300
LOOKUP_LIMIT = 50
#: Таван на редовете камиони/шофьори на един превозвач (защита от грешен вход).
MAX_CHILD_ROWS = 200
_TEXT_LIMIT = 500


def _clean(value, limit=_TEXT_LIMIT):
    return " ".join(str(value if value is not None else "").split())[:limit]


def _clean_multiline(value, limit=4000):
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(ln.rstrip() for ln in text.split("\n")).strip()[:limit]


def clean_values(form):
    """Полетата на превозвача от формата/JSON — изчистени (бележките
    многоредови, останалите на един ред)."""
    return {f: (_clean_multiline(form.get(f)) if f == "notes" else _clean(form.get(f)))
            for f in FIELDS}


def clean_rows(rows, fields):
    """Редовете на динамичната таблица (камиони/шофьори): само речници, само
    известните колони, без изцяло празните, до MAX_CHILD_ROWS."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        row = {f: _clean(r.get(f), 200) for f in fields}
        if any(row.values()):
            out.append(row)
        if len(out) >= MAX_CHILD_ROWS:
            break
    return out


def _search_sql(query):
    query = (query or "").strip()
    if not query:
        return "", []
    fields = ("c.name", "c.vat", "c.eori", "c.city", "c.country", "c.phone", "c.email",
              "c.contact")
    where = " OR ".join("ci_contains(%s, ?)" % f for f in fields)  # nosec B608 -- имената са само от константата `fields`
    # Рег. номер на камион/ремарке или име на шофьор също намира превозвача.
    where += (" OR EXISTS (SELECT 1 FROM carrier_vehicles v WHERE v.carrier_id = c.id"
              " AND ci_contains(v.plate, ?))"
              " OR EXISTS (SELECT 1 FROM carrier_drivers dr WHERE dr.carrier_id = c.id"
              " AND ci_contains(dr.name, ?))")
    return " WHERE " + where, [query] * (len(fields) + 2)


def paginate(con, query, page, page_size=PAGE_SIZE):
    """(превозвачи, page, total_pages, total_count) за списъка — с
    обобщение на камионите/ремаркетата/шофьорите само за показаната страница."""
    where, params = _search_sql(query)
    total = con.execute("SELECT COUNT(*) FROM carriers c" + where, params).fetchone()[0]  # nosec B608 -- where е само от „?“ плейсхолдъри
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    rows = [dict(r) for r in con.execute(
        "SELECT c.* FROM carriers c" + where +  # nosec B608 -- виж по-горе
        " ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()]
    _attach_children(con, rows)
    return rows, page, total_pages, total


def _attach_children(con, carriers):
    """Добавя `trucks`, `trailers`, `drivers` към всеки речник (две заявки
    общо, не по две на превозвач)."""
    ids = [c["id"] for c in carriers]
    by_id = {c["id"]: c for c in carriers}
    for c in carriers:
        c["trucks"], c["trailers"], c["drivers"] = [], [], []
    if not ids:
        return carriers
    marks = ",".join("?" * len(ids))
    for v in con.execute(
            "SELECT carrier_id, kind, plate, note FROM carrier_vehicles"
            " WHERE carrier_id IN (%s) ORDER BY carrier_id, position, id" % marks, ids):  # nosec B608 -- само „?“
        key = "trailers" if v["kind"] == "trailer" else "trucks"
        by_id[v["carrier_id"]][key].append({"plate": v["plate"], "note": v["note"]})
    for d in con.execute(
            "SELECT carrier_id, name, phone, id_doc, note FROM carrier_drivers"
            " WHERE carrier_id IN (%s) ORDER BY carrier_id, position, id" % marks, ids):  # nosec B608 -- само „?“
        by_id[d["carrier_id"]]["drivers"].append(
            {"name": d["name"], "phone": d["phone"], "id_doc": d["id_doc"], "note": d["note"]})
    return carriers


def get(con, carrier_id):
    """Превозвачът с камионите/ремаркетата/шофьорите, или None."""
    if not isinstance(carrier_id, int) or not 1 <= carrier_id <= 2 ** 63 - 1:
        return None
    row = con.execute("SELECT * FROM carriers WHERE id = ?", (carrier_id,)).fetchone()
    if row is None:
        return None
    return _attach_children(con, [dict(row)])[0]


def load_for_form(con, limit=EMBED_LIMIT):
    """Превозвачите за падащото меню на ЧМР (с децата им)."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, name, vat, eori, address, postcode, city, country, contact, phone, email"
        " FROM carriers ORDER BY name COLLATE NOCASE LIMIT ?", (limit,)).fetchall()]
    return _attach_children(con, rows)


def count_all(con):
    return con.execute("SELECT COUNT(*) FROM carriers").fetchone()[0]


def search(con, query, limit=LOOKUP_LIMIT):
    """Сървърното търсене за формата (огледално на /clients/lookup)."""
    where, params = _search_sql(query)
    rows = [dict(r) for r in con.execute(
        "SELECT c.id, c.name, c.vat, c.eori, c.address, c.postcode, c.city, c.country,"
        " c.contact, c.phone, c.email FROM carriers c" + where +  # nosec B608 -- само „?“
        " ORDER BY c.name COLLATE NOCASE LIMIT ?", params + [limit + 1]).fetchall()]
    truncated = len(rows) > limit
    return _attach_children(con, rows[:limit]), truncated


def as_json(carriers):
    """За вграждане в HTML атрибут (апострофът се екранира — виж jsonutil)."""
    return jsonutil.dumps_for_inline_script(carriers)


def carrier_text(c):
    """Текстът за кутия 16 на ЧМР „Превозвач (име, адрес, държава)“ — същото
    сглобяване като static/parties.js (carrierBoxText)."""
    if not c:
        return ""
    place = " ".join(x for x in (c.get("postcode") or "", c.get("city") or "") if x)
    parts = [c.get("name") or "", c.get("address") or "", place, c.get("country") or ""]
    text = ", ".join(p.strip() for p in parts if p and p.strip())
    if c.get("vat"):
        text += ", VAT: " + c["vat"]
    return text


def _save_children(con, carrier_id, trucks, trailers, drivers):
    con.execute("DELETE FROM carrier_vehicles WHERE carrier_id = ?", (carrier_id,))
    con.execute("DELETE FROM carrier_drivers WHERE carrier_id = ?", (carrier_id,))
    pos = 0
    for kind, rows in (("truck", trucks), ("trailer", trailers)):
        for r in rows:
            pos += 1
            con.execute("INSERT INTO carrier_vehicles (carrier_id, kind, plate, note, position)"
                        " VALUES (?, ?, ?, ?, ?)", (carrier_id, kind, r["plate"], r["note"], pos))
    for i, r in enumerate(drivers, start=1):
        con.execute("INSERT INTO carrier_drivers (carrier_id, name, phone, id_doc, note, position)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (carrier_id, r["name"], r["phone"], r["id_doc"], r["note"], i))


class EditConflict(Exception):
    """Записът е променен (или изтрит) от друг потребител, докато формата е
    била отворена — нищо не е записано. `current` е текущият запис или None."""

    def __init__(self, current):
        super().__init__("carrier edit conflict")
        self.current = current


def save(con, values, trucks, trailers, drivers, carrier_id=None, version=None):
    """Създава/обновява превозвач (без commit). При редакция `version` е
    версията от отварянето на формата — различна от записаната → EditConflict
    (оптимистично заключване, както при документите). Връща id-то."""
    if carrier_id is None:
        cur = con.execute(
            "INSERT INTO carriers (%s) VALUES (%s)" % (", ".join(FIELDS), ", ".join("?" * len(FIELDS))),  # nosec B608 -- имената са само от FIELDS
            [values[f] for f in FIELDS])
        carrier_id = cur.lastrowid
    else:
        row = con.execute("SELECT version FROM carriers WHERE id = ?", (carrier_id,)).fetchone()
        if row is None:
            raise EditConflict(None)
        if version is not None and str(version).strip() != str(row["version"]):
            raise EditConflict(get(con, carrier_id))
        cur = con.execute(
            "UPDATE carriers SET %s, version = version + 1,"  # nosec B608 -- имената са само от FIELDS
            " updated_at = datetime('now','localtime') WHERE id = ? AND version = ?"
            % ", ".join(f + " = ?" for f in FIELDS),
            [values[f] for f in FIELDS] + [carrier_id, row["version"]])
        if cur.rowcount == 0:
            raise EditConflict(get(con, carrier_id))
    _save_children(con, carrier_id, trucks, trailers, drivers)
    return carrier_id


def delete(con, carrier_id):
    """Изтрива превозвача (камионите/шофьорите — каскадно; палетните
    движения остават с името си, carrier_id = NULL). Без commit."""
    con.execute("DELETE FROM carriers WHERE id = ?", (carrier_id,))


def find_by_name(con, name):
    """id на превозвач с това име (без регистър/интервали) или None."""
    name = _clean(name)
    if not name:
        return None
    row = con.execute("SELECT id FROM carriers WHERE ci_lower(TRIM(name)) = ci_lower(?)"
                      " ORDER BY id LIMIT 1", (name,)).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------- запомнен превозвач по клиент

#: Полетата на ЧМР, които „последният превозвач“ предлага.
MEMORY_FIELDS = ("carrier", "truck_reg", "trailer_reg", "driver")

#: Колко последни ЧМР на клиента се преглеждат за попълнен превозвач.
_MEMORY_SCAN = 20


def _client_name(con, client_key):
    """client_key: id от адресната книга (int или „12“) или име на клиент
    (получателя в ЧМР). Връща името, по което се търсят документите."""
    if isinstance(client_key, int) or (isinstance(client_key, str) and client_key.strip().isdigit()):
        try:
            cid = int(client_key)
        except (TypeError, ValueError):
            return ""
        if not 1 <= cid <= 2 ** 63 - 1:
            return ""
        row = con.execute("SELECT name FROM clients WHERE id = ?", (cid,)).fetchone()
        return (row["name"] or "").strip() if row else ""
    return _clean(client_key)


def last_for_client(con, client_key):
    """Превозвачът, камионът/ремаркето и шофьорът от ПОСЛЕДНОТО ЧМР за този
    клиент (получател), или None.

    Източникът са самите ЧМР (индексираната колона documents.client_name,
    db._m011 — поддържа се и от по-стари версии на програмата), затова
    работи и за документи, издадени преди регистъра. Ако ЧМР-то носи
    `carrier_id` от регистъра и превозвачът още съществува, връща и записа
    му (`carrier_record`) — формата показва избора в падащото меню.

    Връща речник: carrier, truck_reg, trailer_reg, driver, carrier_id,
    carrier_record, doc_id, doc_number, doc_date."""
    import json
    name = _client_name(con, client_key)
    if not name:
        return None
    rows = con.execute(
        "SELECT id, number, created_at, data FROM documents"
        " WHERE doc_type = 'cmr' AND ci_lower(client_name) = ci_lower(?)"
        " ORDER BY id DESC LIMIT ?", (name, _MEMORY_SCAN)).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        found = {f: _clean(data.get(f)) for f in MEMORY_FIELDS}
        if not any(found.values()):
            continue
        carrier_id = None
        raw_id = str(data.get("carrier_id") or "").strip()
        if raw_id.isdigit():
            carrier_id = int(raw_id)
        record = get(con, carrier_id) if carrier_id else None
        found.update({
            "carrier_id": record["id"] if record else None,
            "carrier_record": record,
            "doc_id": row["id"],
            "doc_number": row["number"],
            "doc_date": (data.get("date_loading") or data.get("established_date")
                         or (row["created_at"] or "")[:10]),
        })
        return found
    return None


def driver_details(carrier, driver_text):
    """Телефонът/документът на шофьора от регистъра по името в ЧМР (за
    поръчката за превоз) — речник или None."""
    if not carrier or not driver_text:
        return None
    needle = " ".join(str(driver_text).split()).lower()
    for d in carrier.get("drivers") or []:
        name = " ".join((d.get("name") or "").split()).lower()
        if name and (needle == name or needle.startswith(name)):
            return d
    return None
