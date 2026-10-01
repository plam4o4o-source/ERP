# -*- coding: utf-8 -*-
"""Одит (01.10.2026, F2/R1): страничната таблица за търсене document_search
(search_index.py) — поддържана от тригери, без регистър за кирилица,
повреден JSON не чупи търсенето, броенето при много съвпадения е с таван."""
import json
import re
import sqlite3

import pytest

from conftest import post_with_csrf


def _issue(admin_client, url, fields):
    resp = post_with_csrf(admin_client, url, fields)
    assert resp.status_code == 302, resp.status_code
    return int(re.search(r"/doc/(\d+)", resp.headers["Location"]).group(1))


def _found(admin_client, query, url="/docs"):
    body = admin_client.get(url, query_string={"q": query}).get_data(as_text=True)
    return {int(x) for x in re.findall(r'href="/doc/(\d+)"', body)}


def _insert_raw(db_module, doc_type, number, data_text, barcode):
    con = db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, 2026, 1, ?, ?, ?, 1)", (doc_type, number, barcode, "t" + barcode, data_text))
    con.commit()
    doc_id = cur.lastrowid
    con.close()
    return doc_id


def _body(db_module, doc_id):
    con = db_module.get_db()
    row = con.execute("SELECT body FROM document_search WHERE id = ?", (doc_id,)).fetchone()
    con.close()
    return None if row is None else row["body"]


def test_search_table_is_maintained_by_triggers_for_every_write(admin_client, db_module):
    """Нов документ през формата, редакция, директен UPDATE и изтриване —
    всяко обновява тялото, без пътят за запис да знае за таблицата."""
    doc_id = _issue(admin_client, "/cmr/new", {"consignee_name": "Първа Фирма ООД"})
    assert "първа фирма ООД".lower() in _body(db_module, doc_id)

    resp = post_with_csrf(admin_client, "/doc/%d/edit" % doc_id,
                          {"consignee_name": "Втора Фирма ЕАД"}, follow_redirects=True)
    assert resp.status_code == 200
    assert "втора фирма еад" in _body(db_module, doc_id)
    assert "първа" not in _body(db_module, doc_id)
    assert _found(admin_client, "ВТОРА фирма") == {doc_id}
    assert _found(admin_client, "Първа") == set()

    # Връзка БЕЗ Python функциите на приложението (напр. DB Browser, по-стара
    # версия): записът минава, а тригерът пак обновява тялото.
    raw = sqlite3.connect(db_module.DB_PATH)
    raw.execute("UPDATE documents SET data = ? WHERE id = ?",
                (json.dumps({"consignee_name": "Трета ЕООД", "items": [{"qty": 12.5}]},
                            ensure_ascii=False), doc_id))
    raw.commit()
    raw.close()
    body = _body(db_module, doc_id)
    assert "трета еоод" in body and "12.5" in body

    resp = post_with_csrf(admin_client, "/doc/%d/delete" % doc_id, {}, follow_redirects=False)
    assert resp.status_code == 302
    assert _body(db_module, doc_id) is None


def test_bulk_pallet_issue_is_searchable(admin_client, db_module):
    items = [{"order_no": "1", "pos": "10", "reference": "R", "reference_desc": "Уникална стока", "qty": "5"}]
    resp = post_with_csrf(admin_client, "/pallet/bulk-issue", {
        "client_name": "Палетен Клиент", "groups": "g1", "items_format_g1": "orders",
        "items_json_g1": json.dumps(items, ensure_ascii=False), "gross_g1": "100",
    }, csrf_source_url="/pallet/new", follow_redirects=True)
    assert resp.status_code == 200
    con = db_module.get_db()
    ids = {r["id"] for r in con.execute("SELECT id FROM documents WHERE doc_type = 'pallet'")}
    con.close()
    assert ids and _found(admin_client, "УНИКАЛНА стока") == ids


def test_search_semantics_match_the_value_search(admin_client, db_module):
    """Паритет с досегашното търсене по стойност: без регистър (кирилица и
    латиница), без имената на ключовете, числа, кавички; съвпадение не
    прескача между две стойности."""
    a = _issue(admin_client, "/cmr/new", {"consignee_name": 'Фирма "Ромашка" ЕООД',
                                          "goods": "Прекъсвачи ABB", "weight": "1250"})
    b = _issue(admin_client, "/cmr/new", {"consignee_name": "Şişli Lojistik", "goods": "Straße"})
    assert _found(admin_client, "прекъсвачи abb") == {a}
    assert _found(admin_client, "ПРЕКЪСВАЧИ") == {a}
    assert _found(admin_client, '"ромашка"') == {a}
    assert _found(admin_client, "consignee") == set()
    assert _found(admin_client, "1250") == {a}
    assert _found(admin_client, "ШИШЛИ") == set()
    assert _found(admin_client, "ŞIŞLI LOJISTIK") == {b}
    # Знаци, които тригерите не сгъват („ß“), минават през ci_contains.
    assert _found(admin_client, "STRASSE") == set()
    assert _found(admin_client, "straße") == {b}
    # „ЕООД“ + следващата стойност не бива да съвпада като едно цяло.
    assert _found(admin_client, "ЕООД\x1fПрекъсвачи") == set()


@pytest.mark.parametrize("url,doc_type,field", [("/docs", "cmr", "consignee_name"),
                                                ("/invoices", "invoice_br", "consignee_name")])
def test_corrupt_json_row_does_not_break_search(admin_client, db_module, url, doc_type, field):
    """R1 (регресия от v3.75.0): един отрязан `data` ред правеше търсенето в
    целия списък „malformed JSON“ → 503. Сега повреденият ред просто не съвпада."""
    good = _insert_raw(db_module, doc_type, "N-1",
                       json.dumps({field: "Спиди АД"}, ensure_ascii=False), "BC-001")
    _insert_raw(db_module, doc_type, "N-2", '{"%s": "Спиди ЕООД", "items": [{"desc' % field, "BC-002")
    resp = admin_client.get(url, query_string={"q": "спиди"})
    assert resp.status_code == 200, resp.status_code
    assert _found(admin_client, "спиди", url) == {good}


def test_corrupt_json_row_does_not_break_the_fallback_search(admin_client, db_module, monkeypatch):
    """Резервният път (индексът не е готов) също пази json_tree с json_valid."""
    import search_index
    monkeypatch.setattr(search_index, "_ready_path", None)
    good = _insert_raw(db_module, "cmr", "N-1", json.dumps({"consignee_name": "Спиди АД"},
                                                          ensure_ascii=False), "BC-001")
    _insert_raw(db_module, "cmr", "N-2", '{"consignee_name": "Спиди', "BC-002")
    resp = admin_client.get("/docs", query_string={"q": "Спиди"})
    assert resp.status_code == 200
    assert _found(admin_client, "Спиди") == {good}


def test_missing_or_stale_index_is_backfilled_on_start(db_module):
    """Таблицата липсва (база от по-стара версия) → създава се и се попълва;
    разминаване в броя → пълно преизграждане."""
    import search_index
    doc_id = _insert_raw(db_module, "cmr", "N-1", json.dumps({"consignee_name": "Стара База"},
                                                            ensure_ascii=False), "BC-001")
    con = db_module.get_db()
    for name in ("trg_document_search_insert", "trg_document_search_update", "trg_document_search_delete"):
        con.execute("DROP TRIGGER IF EXISTS %s" % name)
    con.execute("DROP TABLE IF EXISTS document_search")
    con.commit()
    search_index.ensure_schema(con)
    assert con.execute("SELECT body FROM document_search WHERE id = ?", (doc_id,)).fetchone()[0] == "стара база"
    assert search_index.is_ready()

    con.execute("DELETE FROM document_search")
    con.commit()
    search_index.ensure_schema(con)
    assert con.execute("SELECT COUNT(*) FROM document_search").fetchone()[0] == 1
    con.close()


def test_search_count_is_capped(admin_client, db_module, monkeypatch):
    """При много съвпадения броенето спира след тавана → „(100+)“, а
    следващата страница остава достъпна."""
    import appcore
    monkeypatch.setattr(appcore, "SEARCH_COUNT_CAP", 100)
    con = db_module.get_db()
    con.executemany(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES ('cmr', ?, 2026, ?, ?, ?, ?, 1)",
        [("%04d/2026" % n, n, "CMR-X-%d" % n, "t%d" % n,
          json.dumps({"consignee_name": "Общ Клиент"}, ensure_ascii=False)) for n in range(1, 251)])
    con.commit()
    con.close()
    body = admin_client.get("/docs", query_string={"q": "общ клиент"}).get_data(as_text=True)
    assert "(100+)" in body
    body = admin_client.get("/docs", query_string={"q": "общ клиент", "page": 2}).get_data(as_text=True)
    assert "(200+)" in body
    body = admin_client.get("/docs", query_string={"q": "общ клиент", "page": 3}).get_data(as_text=True)
    assert "(250)" in body
    # Без търсене броят е точен.
    assert "(250)" in admin_client.get("/docs").get_data(as_text=True)
