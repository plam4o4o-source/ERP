# -*- coding: utf-8 -*-
"""Странична таблица за търсене в стойностите на документите и индексите,
които приложението поддържа само (без миграция в db.py).

Одит (01.10.2026, F2): търсенето в /docs и /invoices минаваше през
json_tree() + Python функция върху ЦЕЛИЯ JSON на всеки документ (~7 KB/ред)
при всяко натискане на клавиш. Тук текстовите стойности на `data` се пазят
предварително в `document_search.body`, вече сгънати по регистър за
латиница и кирилица.

Таблицата се поддържа от ТРИГЕРИ с вграден SQL (json_tree/group_concat/
lower/replace) — никой път за запис (нов документ, редакция, групово
издаване, ръчен UPDATE, по-стара версия на програмата върху същата база)
не може да я пропусне, а връзка без Python функциите на приложението не
може да провали запис. Повреден JSON просто дава празно тяло (не съвпада).
"""
import re
import sqlite3

import applog
import db

#: Разделител между отделните стойности — съвпадение не може да „прескочи“
#: от една стойност в друга (както при досегашната проверка по стойност).
SEP = "\x1f"

#: Пътят на базата, за която ensure_schema е минал успешно. За друга база
#: (или при неуспех) appcore.json_value_search ползва стария бавен път.
_ready_path = None


def is_ready():
    return _ready_path is not None and _ready_path == db.DB_PATH


#: Двойки (главна → малка), които тригерите сгъват с replace(): А–Я, Ё, Ѝ и
#: турските/немските главни с различна малка буква. ASCII сгъва lower().
#: „İ“ → „i“ огледално на ci_contains (там „i“ съвпада и с „İ“).
#:
#: Одит (04.10.2026, F12): и безточковото „ı“ → „i“, и „i“ + съчетаваща точка
#: (U+0307, разложено „İ“; „I“ + U+0307 вече е „i“ + U+0307 след lower()) →
#: „i“ — същото сгъване като db.search_fold, т.е. „istanbul“ намира и
#: „ıstanbul“. Промяната сменя SQL-а на тригерите, а ensure_schema при
#: несъвпадащ тригер пресъздава тригерите И преизгражда цялата таблица —
#: съществуващите бази се преиндексират сами при първото стартиране.
_FOLD_PAIRS = ([(chr(c), chr(c + 0x20)) for c in range(0x0410, 0x0430)]
               + [("Ё", "ё"), ("Ѝ", "ѝ"), ("Ç", "ç"), ("Ğ", "ğ"), ("İ", "i"),
                  ("Ö", "ö"), ("Ş", "ş"), ("Ü", "ü"), ("Ä", "ä"),
                  ("\u0131", "i"), ("i\u0307", "i")])
#: Одит (21.10.2026, APPFIX-10): без диакритика на латинските букви — главна и
#: малка буква с диакритика → основната (db.ACCENT_MAP: „Ü“/„ü“ → „u“, „ß“/„ẞ“
#: → „ss“); кирилицата („й“, „ё“, „ѝ“) не е в списъка. Смяната на списъка сменя
#: SQL-а на тригерите → ensure_schema преизгражда тялото и индекса веднъж.
_BASE_PAIR_COUNT = len(_FOLD_PAIRS)
_FOLD_PAIRS += [(up, base) for lo, base in sorted(db.ACCENT_MAP.items())
                for up in (lo.upper(),) if len(up) == 1 and up != lo
                and up not in dict(_FOLD_PAIRS)] + [("\u1e9e", "ss")]
_FOLD_PAIRS += sorted(db.ACCENT_MAP.items())
#: …и главните букви без разлагане от същите блокове („Æ“, „Ø“, „Ł“, „Đ“) → малки
#: (SQLite lower() сгъва само ASCII).
_FOLD_PAIRS += [(ch, ch.lower()) for ch in map(chr, db.ACCENT_CODES)
                if ch.isupper() and len(ch.lower()) == 1 and ch.lower() not in db.ACCENT_MAP
                and ch not in dict(_FOLD_PAIRS)]
#: Буквите от двойките за диакритика — GLOB клас (виж _fold_views).
_ACCENT_GLOB = "*[%s]*" % db.ACCENT_GLOB_CLASS
#: Над ~30 вложени replace() препълват стека на SQL парсера — затова
#: сгъването е на стъпки (вложени подзаявки).
#: Одит (21.10.2026, APPFIX-10): ~240 двойки не се побират в една заявка (общо
#: ~60 вложени replace() на израз) — стъпките са ИЗГЛЕДИ (всеки се разбира
#: отделно), а тригерът чете последния изглед за NEW.id (SQLite ги слива в
#: една заявка по първичния ключ). Първата стъпка носи и json_tree — по-къса.
_FOLD_STEPS = [_FOLD_PAIRS[:14]] + [_FOLD_PAIRS[i:min(i + 24, _BASE_PAIR_COUNT)]
                                    for i in range(14, _BASE_PAIR_COUNT, 24)]
_ACCENT_STEPS = [_FOLD_PAIRS[i:i + 24] for i in range(_BASE_PAIR_COUNT, len(_FOLD_PAIRS), 24)]

#: Знаци без регистър, които сгъването все пак ПРОМЕНЯ (F12) — заявка с тях
#: минава през ci_contains (db.search_fold), не през instr(body, lower()).
_NOT_FOLDABLE = frozenset(("\u0131", "\u0307"))
#: Одит (21.10.2026, APPFIX-10): заявката се сгъва с fold_query — ТОЧНО като
#: тригерите (ASCII lower() + същите replace() подред), затова всеки знак от
#: списъка е „безопасен“, не само тези, чийто str.lower() съвпада.
_SAFE_CHARS = (frozenset(lo for _up, lo in _FOLD_PAIRS if len(lo) == 1)
               | frozenset(up for up, _lo in _FOLD_PAIRS if len(up) == 1)) - _NOT_FOLDABLE


def fold_query(text):
    """Одит (21.10.2026, APPFIX-10): заявка, сгъната както тялото в тригерите
    (за instr()/MATCH) — само за is_foldable(text)."""
    folded = "".join(ch.lower() if ch < "\x80" else ch for ch in text)
    for upper, lower in _FOLD_PAIRS:
        if upper in folded:
            folded = folded.replace(upper, lower)
    return folded


def query_terms(text):
    """Одит (21.10.2026, APPFIX-10): думите на заявката („енерго про“ → две) —
    документът трябва да съдържа ВСЯКА от тях (в която и да е стойност)."""
    if SEP in (text or ""):
        return [text]  # разделителят никога не съвпада (str.split() го брои за интервал)
    terms = []
    for term in (text or "").split():
        if term not in terms:
            terms.append(term)
    return terms

#: Одит (13.10.2026, PERF-1): номерът и баркодът са в началото на тялото —
#: търсенето в списъците ги намира през индекса document_fts, без да чете
#: всеки ред на documents заради ci_contains(d.number/d.barcode).
_RAW_BODY_SQL = (
    "COALESCE({src}.number, '') || char(31) || COALESCE({src}.barcode, '') || char(31) || "
    "COALESCE((SELECT group_concat(CAST(jt.value AS TEXT), char(31))"
    " FROM json_tree(CASE WHEN json_valid({src}.data) THEN {src}.data ELSE '{{}}' END) AS jt"
    " WHERE jt.type IN ('text', 'integer', 'real')), '')")


def _replace_chain(expr, pairs):
    for upper, lower in pairs:
        expr = "replace(%s, '%s', '%s')" % (expr, upper, lower)
    return expr


def _fold_views():
    """{име: CREATE VIEW} — веригата изгледи на сгъването (виж _FOLD_STEPS).

    Одит (21.10.2026, APPFIX-10): стъпките за диакритика (_ACCENT_STEPS) се
    прилагат само на документ, в чийто JSON има латинска буква с диакритика
    (или \\u-ескейп) — флаг `a` от суровите колони (instr() по байт);
    иначе тялото е това след основните стъпки (`p`). Така кирилица/ASCII
    документите не плащат ~170-те replace(). Измерено при 20 000 документа
    (~6.6 KB, 32% с диакритика): тялото 5.2 → 8.7 s (с FTS ~11 s) еднократно; запис — без разлика."""
    views = {}
    prev = None
    # В JSON-а — по водещия байт в UTF-8 (instr върху BLOB е бързо търсене на
    # байт; GLOB с клас върху ~7 KB беше ~2.8 s при 20 000 документа):
    # C3 = U+00C0–U+00FF, C4/C5 = U+0100–U+017F, C8 = U+0200–U+023F, ẞ.
    blob = "CAST(d.data AS BLOB)"
    flag = "(%s OR instr(d.data, '\\u') > 0 OR (COALESCE(d.number, '') || COALESCE(d.barcode, '')) GLOB '%s')" % (
        " OR ".join("instr(%s, x'%s') > 0" % (blob, b) for b in ("C3", "C4", "C5", "C8", "E1BA9E")),
        _ACCENT_GLOB)
    steps = [(pairs, False) for pairs in _FOLD_STEPS] + [(pairs, True) for pairs in _ACCENT_STEPS]
    first_accent = True
    for n, (pairs, accent) in enumerate(steps):
        name = "document_search_fold%d" % n
        if prev is None:
            sql = "SELECT d.id AS id, %s AS a, %s AS s FROM documents d" % (flag, _replace_chain(  # nosec B608 -- само литерали от този модул
                "lower(%s)" % _RAW_BODY_SQL.format(src="d"), pairs))
        elif accent and first_accent:
            sql = "SELECT id, a, s AS p, %s AS s FROM %s" % (_replace_chain("s", pairs), prev)  # nosec B608 -- само литерали от този модул
            first_accent = False
        else:
            sql = "SELECT id, a, %s%s AS s FROM %s" % (
                "" if first_accent else "p, ", _replace_chain("s", pairs), prev)  # nosec B608 -- само литерали от този модул
        views[name] = "CREATE VIEW %s AS %s" % (name, sql)
        prev = name
    name = "document_search_fold%d" % len(steps)
    views[name] = ("CREATE VIEW %s AS SELECT id, CASE WHEN a THEN s ELSE p END AS s FROM %s"
                   % (name, prev))  # nosec B608 -- само литерали от този модул
    return views


_FOLD_VIEWS = _fold_views()
_FOLD_LAST = list(_FOLD_VIEWS)[-1]
_UPSERT = ("INSERT OR REPLACE INTO document_search (id, body) SELECT id, s FROM %s"  # nosec B608 -- име на изглед от този модул
           " WHERE id = NEW.id" % _FOLD_LAST)
_REBUILD = "INSERT INTO document_search (id, body) SELECT id, s FROM %s" % _FOLD_LAST  # nosec B608 -- име на изглед от този модул

_TRIGGERS = {
    "trg_document_search_insert": (
        "CREATE TRIGGER trg_document_search_insert AFTER INSERT ON documents BEGIN %s; END"
        % _UPSERT),
    "trg_document_search_update": (
        "CREATE TRIGGER trg_document_search_update AFTER UPDATE OF data, number, barcode ON documents"
        " BEGIN %s; END"
        % _UPSERT),
    "trg_document_search_delete": (
        "CREATE TRIGGER trg_document_search_delete AFTER DELETE ON documents BEGIN"
        " DELETE FROM document_search WHERE id = OLD.id; END"),
}

#: Одит (13.10.2026, PERF-1): триграмен FTS5 индекс върху document_search.body.
#: Без съдържание (content='') и с contentless_delete — текстът не се пази втори
#: път (detail=none: +~10% от базата), а изтриването е по rowid: разминаване
#: между индекса и тялото никога не проваля запис (при външно съдържание
#: 'delete' с други стойности дава „database disk image is malformed“).
#: Индексът дава само КАНДИДАТИ (AND на триграмите); съвпадението се проверява
#: с instr() върху истинското тяло — резултатът е точно досегашният.
_FTS_CREATE = ("CREATE VIRTUAL TABLE document_fts USING fts5(body, content='',"
               " contentless_delete=1, tokenize='trigram', detail='none')")
_FTS_UPSERT_NEW = "INSERT OR REPLACE INTO document_fts (rowid, body) VALUES (NEW.id, NEW.body)"
_FTS_TRIGGERS = {
    "trg_document_fts_insert": (
        "CREATE TRIGGER trg_document_fts_insert AFTER INSERT ON document_search BEGIN %s; END"
        % _FTS_UPSERT_NEW),
    "trg_document_fts_update": (
        "CREATE TRIGGER trg_document_fts_update AFTER UPDATE ON document_search BEGIN"  # nosec B608 -- само литерали от този модул
        " DELETE FROM document_fts WHERE rowid = OLD.id; %s; END" % _FTS_UPSERT_NEW),
    "trg_document_fts_delete": (
        "CREATE TRIGGER trg_document_fts_delete AFTER DELETE ON document_search BEGIN"
        " DELETE FROM document_fts WHERE rowid = OLD.id; END"),
}
#: Под толкова знака триграмният индекс не помага — старият път.
FTS_MIN_CHARS = 3
_fts_ready = False


def is_fts_ready():
    return _fts_ready and is_ready()


def fts_match_query(text):
    """(MATCH израз, игла за instr) за document_fts или None, ако заявката
    трябва да мине по стария път: индексът липсва, знаци извън SQL сгъването,
    разделител или под FTS_MIN_CHARS знака. MATCH е AND на триграмите на
    сгънатата заявка (detail=none не поддържа фрази)."""
    if not is_fts_ready() or not text or SEP in text or not is_foldable(text):
        return None
    folded = fold_query(text)
    if len(folded) < FTS_MIN_CHARS:
        return None
    return _grams_match(folded), folded


def _grams_match(folded, grams=None):
    grams = [] if grams is None else grams
    for i in range(len(folded) - 2):
        gram = '"%s"' % folded[i:i + 3].replace('"', '""')
        if gram not in grams:
            grams.append(gram)
    return " AND ".join(grams)


def fts_match_terms(terms):
    """Одит (21.10.2026, APPFIX-10): като fts_match_query, но за няколко думи —
    (MATCH израз от триграмите на ВСИЧКИ думи с поне FTS_MIN_CHARS знака,
    [сгънатите думи]) или None (стар път за цялата заявка). Най-дългата дума е
    първа (тя се проверява в индекса); по-късите — с instr() отделно."""
    if not is_fts_ready() or not terms:
        return None
    if any(SEP in t or not is_foldable(t) for t in terms):
        return None
    folded = sorted({fold_query(t) for t in terms}, key=lambda t: (-len(t), t))
    if len(folded[0]) < FTS_MIN_CHARS:
        return None
    grams = []
    for term in folded:
        if len(term) >= FTS_MIN_CHARS:
            _grams_match(term, grams)
    return " AND ".join(grams), folded


#: Одит (01.10.2026, F1c/F8): индекси, които приложението създава само.
#: (client_name, doc_type) покрива групирането по клиент и историята на
#: клиента без четене на редовете; заменя едноколонния idx_documents_client_name.
_INDEXES = {
    "idx_documents_client_type":
        "CREATE INDEX IF NOT EXISTS idx_documents_client_type ON documents (client_name, doc_type)",
    "idx_documents_created_type_client_number":
        "CREATE INDEX IF NOT EXISTS idx_documents_created_type_client_number"
        " ON documents (created_at, doc_type, client_name, number)",
}
_OBSOLETE_INDEXES = ("idx_documents_client_name", "idx_documents_created_at",
                     "idx_documents_created_type_client")


def _norm_sql(sql):
    return re.sub(r"\s+", " ", sql or "").strip()


def ensure_schema(con):
    """Идемпотентно: таблица, тригери, индекси; еднократно попълване, ако
    таблицата липсва, тригерите са от друга версия или броят не съвпада.
    Грешка тук не спира стартирането — търсенето и списъците остават верни,
    само по-бавни (виж json_value_search за резервния път)."""
    global _ready_path, _fts_ready
    try:
        fts = _ensure_schema(con)
        con.commit()
        _ready_path = db.DB_PATH
        _fts_ready = fts
    except Exception:  # nosec B110 -- оптимизация, не бива да спира старта
        _ready_path = None
        _fts_ready = False
        try:
            con.rollback()
        except Exception:  # nosec B110
            pass
        applog.log_exception("search_index.ensure_schema: неуспешно създаване на индекса за търсене")


def _ensure_schema(con):
    if not con.in_transaction:
        # Два компютъра, стартирали едновременно, не преизграждат заедно.
        con.execute("BEGIN IMMEDIATE")
    con.execute("CREATE TABLE IF NOT EXISTS document_search ("
                "id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT '')")
    stale = _ensure_fold_views(con)
    existing = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'documents'")}
    for name, sql in _TRIGGERS.items():
        if _norm_sql(existing.get(name)) != _norm_sql(sql):
            con.execute("DROP TRIGGER IF EXISTS %s" % name)
            con.execute(sql)
            stale = True
    if not stale:
        stale = con.execute(
            "SELECT (SELECT COUNT(*) FROM documents) <> (SELECT COUNT(*) FROM document_search)"
        ).fetchone()[0]
    fts = _ensure_fts(con)
    if stale:
        rebuild(con)
    elif fts == "stale":
        _rebuild_fts(con)
    for sql in _INDEXES.values():
        con.execute(sql)
    for name in _OBSOLETE_INDEXES:
        con.execute("DROP INDEX IF EXISTS %s" % name)
    return bool(fts)


def _ensure_fold_views(con):
    """Одит (21.10.2026, APPFIX-10): изгледите на сгъването (виж _FOLD_STEPS);
    True, ако е сменен/създаден поне един (тогава тялото се преизгражда)."""
    existing = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view'"
        " AND name LIKE 'document\\_search\\_fold%' ESCAPE '\\'")}
    if all(_norm_sql(existing.get(name)) == _norm_sql(sql) for name, sql in _FOLD_VIEWS.items()):
        return False
    for name in existing:
        con.execute("DROP VIEW IF EXISTS %s" % name)
    for sql in _FOLD_VIEWS.values():
        con.execute(sql)
    return True


def _fts5_trigram_available(con):
    try:
        con.execute("CREATE VIRTUAL TABLE temp.ph_fts_probe USING fts5(x, content='',"
                    " contentless_delete=1, tokenize='trigram', detail='none')")
        con.execute("DROP TABLE temp.ph_fts_probe")
        return True
    except sqlite3.Error:
        return False


def _ensure_fts(con):
    """Одит (13.10.2026, PERF-1): document_fts и тригерите му. Връща "ok",
    "stale" (трябва попълване — нова таблица, сменени тригери, разминат брой)
    или "" (няма FTS5/trigram/contentless_delete в тази SQLite — търсенето
    по стария път)."""
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                         " AND name = 'document_fts'").fetchone() is not None
    if not _fts5_trigram_available(con):
        if exists:
            # Таблица от друг компютър, а тук модулът липсва: без тригерите
            # записът на документи не гърми; другият компютър ги връща и
            # попълва индекса наново при следващия си старт.
            for name in _FTS_TRIGGERS:
                con.execute("DROP TRIGGER IF EXISTS %s" % name)
        return ""
    state = "ok"
    if not exists:
        con.execute(_FTS_CREATE)
        state = "stale"
    existing = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'document_search'")}
    for name, sql in _FTS_TRIGGERS.items():
        if _norm_sql(existing.get(name)) != _norm_sql(sql):
            con.execute("DROP TRIGGER IF EXISTS %s" % name)
            con.execute(sql)
            state = "stale"
    if state == "ok" and con.execute(
            "SELECT (SELECT COUNT(*) FROM document_fts_docsize)"
            " <> (SELECT COUNT(*) FROM document_search)").fetchone()[0]:
        state = "stale"
    return state


def _has_fts(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger'"
                       " AND name = 'trg_document_fts_insert'").fetchone() is not None


def _rebuild_fts(con):
    con.execute("INSERT INTO document_fts (document_fts) VALUES ('delete-all')")
    con.execute("INSERT INTO document_fts (rowid, body) SELECT id, body FROM document_search")


def rebuild(con):
    """Пълно преизграждане на тялото за търсене (~3 s при 20 000 документа).
    Одит (13.10.2026, PERF-1): тригерите на FTS се махат за масовия запис
    (иначе по два FTS записа на ред), после индексът се попълва наведнъж."""
    fts = _has_fts(con)
    if fts:
        for name in _FTS_TRIGGERS:
            con.execute("DROP TRIGGER IF EXISTS %s" % name)
    con.execute("DELETE FROM document_search")
    con.execute(_REBUILD)
    if fts:
        _rebuild_fts(con)
        for sql in _FTS_TRIGGERS.values():
            con.execute(sql)


def is_foldable(text):
    """True, ако SQL сгъването в тригерите покрива всички знаци на заявката
    (ASCII, _FOLD_PAIRS и знаци без регистър — цифри, препинателни). Тогава
    instr(body, text.lower()) е точно търсене без оглед на регистъра; иначе
    (напр. „ı“, „ß“, гръцки) търсенето минава през ci_contains."""
    for ch in text:
        if ch < "\x80" or ch in _SAFE_CHARS:
            continue
        if ch in _NOT_FOLDABLE:
            return False
        if ch.lower() != ch or ch.upper() != ch:
            return False
    return True
