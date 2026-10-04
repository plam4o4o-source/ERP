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
#: Над ~30 вложени replace() препълват стека на SQL парсера — затова
#: сгъването е на стъпки (вложени подзаявки).
_FOLD_STEPS = [_FOLD_PAIRS[i:i + 14] for i in range(0, len(_FOLD_PAIRS), 14)]
_SAFE_CHARS = frozenset(lo for _up, lo in _FOLD_PAIRS) | frozenset(
    up for up, lo in _FOLD_PAIRS if up.lower() == lo)

#: Знаци без регистър, които сгъването все пак ПРОМЕНЯ (F12) — заявка с тях
#: минава през ci_contains (db.search_fold), не през instr(body, lower()).
_NOT_FOLDABLE = frozenset(("\u0131", "\u0307"))

_RAW_BODY_SQL = (
    "COALESCE((SELECT group_concat(CAST(jt.value AS TEXT), char(31))"
    " FROM json_tree(CASE WHEN json_valid({src}.data) THEN {src}.data ELSE '{{}}' END) AS jt"
    " WHERE jt.type IN ('text', 'integer', 'real')), '')")


def _replace_chain(expr, pairs):
    for upper, lower in pairs:
        expr = "replace(%s, '%s', '%s')" % (expr, upper, lower)
    return expr


def _folded_select(src, from_sql):
    """SELECT id, s — сгънатото тяло на документите от `from_sql`."""
    sql = "SELECT %s.id AS id, %s AS s%s" % (
        src, _replace_chain("lower(%s)" % _RAW_BODY_SQL.format(src=src), _FOLD_STEPS[0]), from_sql)
    for pairs in _FOLD_STEPS[1:]:
        sql = "SELECT id, %s AS s FROM (%s)" % (_replace_chain("s", pairs), sql)  # nosec B608 -- само литерали от този модул
    return sql


_UPSERT = "INSERT OR REPLACE INTO document_search (id, body) %s" % _folded_select("NEW", "")
_REBUILD = "INSERT INTO document_search (id, body) %s" % _folded_select("d", " FROM documents d")

_TRIGGERS = {
    "trg_document_search_insert": (
        "CREATE TRIGGER trg_document_search_insert AFTER INSERT ON documents BEGIN %s; END"
        % _UPSERT),
    "trg_document_search_update": (
        "CREATE TRIGGER trg_document_search_update AFTER UPDATE OF data ON documents BEGIN %s; END"
        % _UPSERT),
    "trg_document_search_delete": (
        "CREATE TRIGGER trg_document_search_delete AFTER DELETE ON documents BEGIN"
        " DELETE FROM document_search WHERE id = OLD.id; END"),
}

#: Одит (01.10.2026, F1c/F8): индекси, които приложението създава само.
#: (client_name, doc_type) покрива групирането по клиент и историята на
#: клиента без четене на редовете; заменя едноколонния idx_documents_client_name.
_INDEXES = {
    "idx_documents_client_type":
        "CREATE INDEX IF NOT EXISTS idx_documents_client_type ON documents (client_name, doc_type)",
    "idx_documents_created_type_client":
        "CREATE INDEX IF NOT EXISTS idx_documents_created_type_client"
        " ON documents (created_at, doc_type, client_name)",
}
_OBSOLETE_INDEXES = ("idx_documents_client_name", "idx_documents_created_at")


def _norm_sql(sql):
    return re.sub(r"\s+", " ", sql or "").strip()


def ensure_schema(con):
    """Идемпотентно: таблица, тригери, индекси; еднократно попълване, ако
    таблицата липсва, тригерите са от друга версия или броят не съвпада.
    Грешка тук не спира стартирането — търсенето и списъците остават верни,
    само по-бавни (виж json_value_search за резервния път)."""
    global _ready_path
    try:
        _ensure_schema(con)
        con.commit()
        _ready_path = db.DB_PATH
    except Exception:  # nosec B110 -- оптимизация, не бива да спира старта
        _ready_path = None
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
    existing = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'documents'")}
    stale = False
    for name, sql in _TRIGGERS.items():
        if _norm_sql(existing.get(name)) != _norm_sql(sql):
            con.execute("DROP TRIGGER IF EXISTS %s" % name)
            con.execute(sql)
            stale = True
    if not stale:
        stale = con.execute(
            "SELECT (SELECT COUNT(*) FROM documents) <> (SELECT COUNT(*) FROM document_search)"
        ).fetchone()[0]
    if stale:
        rebuild(con)
    for sql in _INDEXES.values():
        con.execute(sql)
    for name in _OBSOLETE_INDEXES:
        con.execute("DROP INDEX IF EXISTS %s" % name)


def rebuild(con):
    """Пълно преизграждане на тялото за търсене (~5 s при 20 000 документа)."""
    con.execute("DELETE FROM document_search")
    con.execute(_REBUILD)


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
