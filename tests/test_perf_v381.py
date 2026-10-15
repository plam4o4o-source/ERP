# -*- coding: utf-8 -*-
"""Одит (13.10.2026, PERF): бързина при мрежова база и по-лек браузър —
триграмен FTS5 индекс за търсенето (PERF-1), търсене на живо без DOMParser
(PERF-2), gzip на статичните файлове (PERF-3), PDF без pyhanko (PERF-4),
SVG спрайт за иконите в списъците (PERF-8) и др."""
import json
import re
import sqlite3

import pytest

from conftest import post_with_csrf


class _RecordingConnection(sqlite3.Connection):
    statements = None

    def execute(self, sql, params=()):
        if _RecordingConnection.statements is not None:
            _RecordingConnection.statements.append((sql, tuple(params)))
        return super().execute(sql, params)


@pytest.fixture
def recorded(monkeypatch):
    """Всички (sql, params), изпратени през sqlite3.connect по време на теста."""
    real_connect = sqlite3.connect
    log = []
    _RecordingConnection.statements = log

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", _RecordingConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield log
    _RecordingConnection.statements = None


def _insert(db_module, doc_type, number, barcode, data, raw=False):
    con = sqlite3.connect(db_module.DB_PATH) if raw else db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, 2026, 1, ?, ?, ?, 1)",
        (doc_type, number, barcode, "t-" + barcode,
         data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)))
    con.commit()
    doc_id = cur.lastrowid
    con.close()
    return doc_id


def _found(client, query, url="/docs"):
    body = client.get(url, query_string={"q": query}).get_data(as_text=True)
    return {int(x) for x in re.findall(r'href="/doc/(\d+)"', body)}


def _fts_integrity(db_module):
    """FTS5 проверка на индекса + за всеки документ: тялото е сред кандидатите
    на собствените си триграми (индексът е надмножество на съвпаденията)."""
    import search_index
    con = sqlite3.connect(db_module.DB_PATH)
    try:
        con.execute("INSERT INTO document_fts (document_fts) VALUES ('integrity-check')")
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for doc_id, body in con.execute("SELECT id, body FROM document_search").fetchall():
            for part in body.split(search_index.SEP):
                if len(part) >= 3:
                    match = " AND ".join('"%s"' % part[i:i + 3].replace('"', '""') for i in range(len(part) - 2))
                    ids = {r[0] for r in con.execute(
                        "SELECT rowid FROM document_fts WHERE document_fts MATCH ?", (match,))}
                    assert doc_id in ids, (doc_id, part)
    finally:
        con.close()


_SAMPLE = [
    ("cmr", "0001/2026", "CMR-13102026-0001", {"consignee_name": "İstanbul Lojistik A.Ş.",
                                                "goods": "Кабели ЗА ВИСОКО напрежение"}),
    ("cmr", "0002/2026", "CMR-13102026-0002", {"consignee_name": "Ормантрейд ЕООД",
                                                "goods": "50% off_price [A*B?] x_y"}),
    ("packing", "0003/2026", "OPL-13102026-0003", {"consignee_name": "ŞİŞLİ TEKSTİL",
                                                    "items": [{"po": "4500015679", "qty": 12.5}]}),
    ("invoice_br", "INV-77", "INVBR-13102026-0001", {"consignee_name": "Çağdaş Ltd",
                                                      "items": [{"desc": "Ёлка", "qty": 3}]}),
    ("waybill", "0004/2026", "TOV-13102026-0004", {"consignee_name": "back\\slash ООД"}),
]
_QUERIES = ["istanbul", "İSTANBUL", "ıstanbul", "ормантрейд", "ОРМАНТРЕЙД", "еоод", "şişli", "sisli",
            "кабели за", "50%", "off_price", "[a*b?]", "a*b", "x_y", "_y", "4500015679", "12.5",
            "0002/2026", "cmr-13102026", "OPL-1310", "inv-77", "invbr-1310", "ёлка", "ЁЛКА", "çağdaş",
            "back\\slash", "zzz", "ab", "45", "0", "%", "_", "*", "?", "[", "a\x1fb", "lojistik a.ş."]


@pytest.fixture
def sample_docs(admin_client, db_module):
    return [_insert(db_module, t, n, b, d) for t, n, b, d in _SAMPLE]


# ---------------------------------------------------------------- PERF-1 търсене

def test_fts_index_is_created_and_search_uses_it(admin_client, db_module, sample_docs, recorded):
    import search_index
    assert search_index.is_fts_ready()
    del recorded[:]
    assert _found(admin_client, "ормантрейд") == {sample_docs[1]}
    sqls = [s for s, _p in recorded if "FROM documents d" in s or "document_fts" in s]
    assert any("document_fts" in s and "MATCH" in s for s in sqls), sqls
    # Номерът и баркодът — през индекса, не с ci_contains върху всеки ред.
    assert not any("ci_contains(d.number" in s or "ci_contains(d.barcode" in s for s in sqls), sqls
    _fts_integrity(db_module)


@pytest.mark.parametrize("url", ["/docs", "/invoices"])
def test_fts_search_gives_the_same_results_as_the_old_path(admin_client, db_module, sample_docs,
                                                           monkeypatch, url):
    """Еквивалентност с досегашния път (без индекса) — регистър на кирилица и
    турски, „istanbul“ ↔ „İstanbul“, %/_/*/?/[ като обикновени знаци, номер,
    баркод, къси заявки (старият път) и разделителят между стойностите."""
    import search_index
    new = {q: _found(admin_client, q, url) for q in _QUERIES}
    monkeypatch.setattr(search_index, "_fts_ready", False)
    old = {q: _found(admin_client, q, url) for q in _QUERIES}
    assert new == old
    assert new["istanbul"] == new["İSTANBUL"] == new["ıstanbul"] == ({sample_docs[0]} if url == "/docs" else set())
    assert new["ЁЛКА"] == ({sample_docs[3]} if url == "/invoices" else set())
    assert new["[a*b?]"] == new["a*b"] == new["x_y"] == ({sample_docs[1]} if url == "/docs" else set())
    assert new["a\x1fb"] == set() and new["zzz"] == set()


def test_fts_query_only_for_three_foldable_chars(flask_app):
    import search_index
    assert search_index.fts_match_query("WEG") == ('"weg"', "weg")
    assert search_index.fts_match_query('A"bcb"') == ('"a""b" AND """bc" AND "bcb" AND "cb"""', 'a"bcb"')
    assert search_index.fts_match_query("ab") is None
    assert search_index.fts_match_query("straße") is None        # „ß“ не се сгъва в SQL
    assert search_index.fts_match_query("ab\x1fcd") is None


def test_fts_follows_every_write(admin_client, db_module):
    """Форма, редакция, връзка без функциите на приложението, „INSERT OR
    REPLACE“ в document_search (тригерите на по-стара версия), изтриване —
    индексът остава точен (integrity-check) и търсенето намира новото."""
    resp = post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "Първа Фирма ООД"})
    doc_id = int(re.search(r"/doc/(\d+)", resp.headers["Location"]).group(1))
    assert _found(admin_client, "първа фирма") == {doc_id}
    post_with_csrf(admin_client, "/doc/%d/edit" % doc_id, {"consignee_name": "Втора Фирма ЕАД"},
                   follow_redirects=True)
    assert _found(admin_client, "втора фирма") == {doc_id}
    assert _found(admin_client, "първа фирма") == set()
    raw = sqlite3.connect(db_module.DB_PATH)
    raw.execute("UPDATE documents SET data = ? WHERE id = ?",
                (json.dumps({"consignee_name": "Трета ЕООД"}, ensure_ascii=False), doc_id))
    raw.execute("UPDATE documents SET number = 'ZX-991' WHERE id = ?", (doc_id,))
    # Тригер от по-стара версия: REPLACE не задейства тригерите за изтриване.
    raw.execute("INSERT OR REPLACE INTO document_search (id, body) VALUES (?, 'четвърта стойност')",
                (doc_id,))
    raw.commit()
    raw.close()
    _fts_integrity(db_module)
    assert _found(admin_client, "четвърта") == {doc_id}
    # Ред, изчезнал от индекса (разминаване), не проваля следващия запис.
    raw = sqlite3.connect(db_module.DB_PATH)
    raw.execute("DELETE FROM document_fts WHERE rowid = ?", (doc_id,))
    raw.commit()
    raw.close()
    assert _found(admin_client, "трета") == set()
    raw = sqlite3.connect(db_module.DB_PATH)
    raw.execute("UPDATE documents SET data = ? WHERE id = ?",
                (json.dumps({"consignee_name": "Пета ЕООД"}, ensure_ascii=False), doc_id))
    raw.commit()
    raw.close()
    assert _found(admin_client, "zx-991") == _found(admin_client, "пета") == {doc_id}
    post_with_csrf(admin_client, "/doc/%d/delete" % doc_id, {})
    _fts_integrity(db_module)
    assert _found(admin_client, "пета") == set()


def test_fts_is_built_for_an_old_database_and_rebuilt_when_stale(db_module):
    import search_index
    doc_id = _insert(db_module, "cmr", "N-1", "BC-001", {"consignee_name": "Стара База"})
    con = db_module.get_db()
    search_index.ensure_schema(con)
    con.execute("DROP TABLE document_fts")  # база от v3.80 — без индекса
    con.commit()
    search_index.ensure_schema(con)
    assert search_index.is_fts_ready()
    found = "SELECT rowid FROM document_fts WHERE document_fts MATCH '\"ста\" AND \"тар\"'"
    assert [r[0] for r in con.execute(found)] == [doc_id]
    # Разминат брой (напр. ръчно изтрит ред от индекса) → попълва се наново.
    con.execute("DELETE FROM document_fts WHERE rowid = ?", (doc_id,))
    con.commit()
    assert not con.execute(found).fetchall()
    search_index.ensure_schema(con)
    assert [r[0] for r in con.execute(found)] == [doc_id]
    # Сменен тригер → пресъздава се и индексът се преизгражда.
    con.execute("DROP TRIGGER trg_document_fts_insert")
    con.commit()
    search_index.ensure_schema(con)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'trg_document_fts_insert'").fetchone()
    con.close()
    _fts_integrity(db_module)


def test_fts_on_a_shared_database_and_in_a_backup_copy(admin_client, db_module, tmp_path):
    """Споделена база (DELETE журнал): записите поддържат индекса; архивното
    копие (backup._snapshot_db — VACUUM INTO) го носи, минава integrity_check
    и търсенето в него работи (възстановяване от архив)."""
    import time

    import backup
    # Одит (15.10.2026, PERF2-11): смяната на журнала иска да няма други
    # връзки — и свободните от склада на заявките.
    db_module.close_cached_connections()
    con = sqlite3.connect(db_module.DB_PATH)
    assert con.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    con.close()
    resp = post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "Споделена Фирма"})
    doc_id = int(re.search(r"/doc/(\d+)", resp.headers["Location"]).group(1))
    post_with_csrf(admin_client, "/doc/%d/edit" % doc_id, {"consignee_name": "Мрежова Фирма"},
                   follow_redirects=True)
    assert _found(admin_client, "мрежова") == {doc_id}
    _fts_integrity(db_module)
    copy = str(tmp_path / "snap.db")
    backup._snapshot_db(copy, time.monotonic() + 60)
    snap = sqlite3.connect(copy)
    assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [r[0] for r in snap.execute(
        "SELECT rowid FROM document_fts WHERE document_fts MATCH '\"мре\" AND \"ежо\" AND \"ова\"'")] == [doc_id]
    snap.close()


def test_without_fts5_search_and_writes_use_the_old_path(admin_client, db_module, monkeypatch, recorded):
    """SQLite без FTS5/trigram: индексът не се създава, търсенето е по стария
    път; ако таблицата е от друг компютър — тригерите се махат, за да не
    гърми записът на документ."""
    import search_index
    monkeypatch.setattr(search_index, "_fts5_trigram_available", lambda con: False)
    con = db_module.get_db()
    search_index.ensure_schema(con)
    con.close()
    assert search_index.is_ready() and not search_index.is_fts_ready()
    con = sqlite3.connect(db_module.DB_PATH)
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'trg_document_fts%'").fetchall()
    con.close()
    doc_id = _insert(db_module, "cmr", "N-1", "BC-001", {"consignee_name": "Без Индекс"})
    del recorded[:]
    assert _found(admin_client, "без индекс") == {doc_id}
    assert not any("document_fts" in s for s, _p in recorded)


def test_search_counts_in_one_pass(admin_client, db_module, recorded):
    """Без отделно COUNT … LIMIT 1001: една заявка за id-тата дава и броя;
    над една страница — „(100+)“ и следваща страница."""
    con = db_module.get_db()
    con.executemany(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES ('cmr', ?, 2026, ?, ?, ?, ?, 1)",
        [("%04d/2026" % n, n, "CMR-X-%d" % n, "t%d" % n,
          json.dumps({"consignee_name": "Общ Клиент"}, ensure_ascii=False)) for n in range(1, 131)])
    con.commit()
    con.close()
    del recorded[:]
    body = admin_client.get("/docs", query_string={"q": "общ клиент"}).get_data(as_text=True)
    assert "(100+)" in body
    assert not any("COUNT(" in s.upper() and "documents d" in s for s, _p in recorded)
    body = admin_client.get("/docs", query_string={"q": "общ клиент", "page": 2}).get_data(as_text=True)
    assert "(130)" in body and len(re.findall(r'href="/doc/(\d+)"', body)) >= 30


# ---------------------------------------------------------------- PERF-3 gzip

_LAN = {"REMOTE_ADDR": "192.168.1.20"}


@pytest.mark.parametrize("name", ["app.js", "style.css"])
def test_static_text_files_are_gzipped_for_network_clients(admin_client, name):
    import gzip as _gzip
    from version import __version__
    url = "/static/%s?v=%s" % (name, __version__)
    plain = admin_client.get(url, environ_base=_LAN)
    assert "Content-Encoding" not in plain.headers
    zipped = admin_client.get(url, headers={"Accept-Encoding": "gzip, br"}, environ_base=_LAN)
    assert zipped.status_code == 200
    assert zipped.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in zipped.headers["Vary"]
    assert _gzip.decompress(zipped.data) == plain.data
    assert len(zipped.data) < len(plain.data) / 3
    assert int(zipped.headers["Content-Length"]) == len(zipped.data)
    # Дългият кеш остава; ETag на компресирания вариант е различен и дава 304.
    assert zipped.cache_control.immutable and zipped.cache_control.max_age >= 30 * 24 * 3600
    etag = zipped.headers["ETag"]
    assert etag != plain.headers["ETag"] and etag.endswith('-gz"')
    again = admin_client.get(url, headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
                             environ_base=_LAN)
    assert again.status_code == 304 and again.data == b""
    # Обикновеният ETag пак дава 304 без компресия (send_file).
    same = admin_client.get(url, headers={"If-None-Match": plain.headers["ETag"]}, environ_base=_LAN)
    assert same.status_code == 304
    # Тунелът идва от 127.0.0.1, но с CF-Connecting-IP — компресира се.
    tunnel = admin_client.get(url, headers={"Accept-Encoding": "gzip", "CF-Connecting-IP": "203.0.113.5"})
    assert tunnel.headers.get("Content-Encoding") == "gzip"
    # От същия компютър — без компресия.
    local = admin_client.get(url, headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in local.headers and local.data == plain.data


def test_static_gzip_cache_follows_the_file(admin_client, flask_app, tmp_path, monkeypatch):
    import gzip as _gzip
    import os
    import shutil
    static = tmp_path / "static"
    shutil.copytree(flask_app.static_folder, static)
    monkeypatch.setattr(flask_app, "static_folder", str(static))
    target = static / "app.js"
    first = admin_client.get("/static/app.js", headers={"Accept-Encoding": "gzip"}, environ_base=_LAN)
    target.write_text("console.log('нова версия');\n" * 200, encoding="utf-8")
    st = os.stat(target)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))
    second = admin_client.get("/static/app.js", headers={"Accept-Encoding": "gzip"}, environ_base=_LAN)
    assert _gzip.decompress(second.data).decode("utf-8").startswith("console.log('нова версия')")
    assert first.headers["ETag"] != second.headers["ETag"]
    # Range заявка — без компресия (206 върху суровия файл).
    part = admin_client.get("/static/app.js", headers={"Accept-Encoding": "gzip", "Range": "bytes=0-9"},
                            environ_base=_LAN)
    assert part.status_code == 206 and "Content-Encoding" not in part.headers


# ---------------------------------------------------------------- PERF-4 PDF без pyhanko

#: Пакетите, които release.yml изключва от .exe-то (--exclude-module).
_EXCLUDED_FROM_EXE = ("pyhanko", "pyhanko_certvalidator", "aiohttp", "yarl", "multidict", "frozenlist",
                      "propcache", "aiosignal", "aiohappyeyeballs", "asn1crypto", "oscrypto", "tzlocal",
                      "uritools")


def test_release_build_excludes_the_pdf_signing_chain():
    from conftest import read_source
    yml = read_source(".github", "workflows", "release.yml")
    build = yml[yml.index("pyinstaller --onefile"):yml.index("app.py", yml.index("pyinstaller --onefile"))]
    excluded = set(re.findall(r"--exclude-module (\S+)", build))
    assert excluded == set(_EXCLUDED_FROM_EXE)
    assert "cryptography" not in excluded  # pypdf я внася


def test_release_build_checks_pdf_from_the_real_exe():
    """Изключените модули се проверяват и върху истинския билд: .exe-то
    прави PDF с кирилица (app.py --pdf-selftest) преди публикуване."""
    from conftest import read_source
    yml = read_source(".github", "workflows", "release.yml")
    step = yml[yml.index("Smoke test PDF and search index from the exe"):yml.index("Install Inno Setup")]
    assert "PHLogistics.exe --pdf-selftest" in step and "Кирилица ЙЪЬЯЮ" in step
    assert '"--pdf-selftest" in sys.argv' in read_source("app.py")


def test_first_pdf_does_not_import_the_signing_chain(flask_app):
    """В отделен процес: след PDF с кирилица нито един от изключените пакети
    не е зареден (дори без блокиране)."""
    import subprocess
    import sys
    code = r'''
import sys
sys.path.insert(0, %r)
import pdf_export
pisa = pdf_export._pisa()
import io
out = io.BytesIO()
pisa.CreatePDF("<html><body><p>Здравей</p></body></html>", dest=out)
assert out.getvalue().startswith(b"%%PDF")
bad = sorted(m for m in sys.modules if m.split(".")[0] in %r)
print("LOADED", bad)
''' % (str(__import__("pathlib").Path(__file__).resolve().parent.parent), _EXCLUDED_FROM_EXE)
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-2000:]
    assert "LOADED []" in res.stdout, res.stdout


def test_pdf_tests_pass_with_the_excluded_packages_blocked(tmp_path):
    """Доказателство за .exe-то без тези пакети: целият tests/test_pdf_export.py
    (вкл. кирилицата и шрифтовете) с sys.modules[име] = None за всеки от тях."""
    import os
    import subprocess
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (tmp_path / "ph_block_signing.py").write_text(
        "import sys\nfor _n in %r:\n    sys.modules[_n] = None\n" % (_EXCLUDED_FROM_EXE,), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "ph_block_signing",
                          "-m", "not e2e", os.path.join(repo, "tests", "test_pdf_export.py")],
                         capture_output=True, text=True, cwd=repo, env=env, timeout=600)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    assert re.search(r"^\.{20,}\s", res.stdout, re.M) and "F" not in res.stdout.split("[")[0], res.stdout


# ---------------------------------------------------------------- PERF-5 една четяща транзакция

@pytest.fixture
def recorded_connects(monkeypatch):
    """Като `recorded`, плюс ("CONNECT", път) при всяко отваряне на връзка."""
    real_connect = sqlite3.connect
    log = []
    _RecordingConnection.statements = log

    def connect(*args, **kwargs):
        log.append(("CONNECT", args[0] if args else kwargs.get("database")))
        kwargs.setdefault("factory", _RecordingConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield log
    _RecordingConnection.statements = None


_WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.I)


@pytest.mark.parametrize("url", ["/", "/docs", "/docs?q=клиент", "/docs?group=client", "/invoices",
                                 "/clients", "/materials", "/doc/{cmr}"])
def test_read_only_views_run_in_one_read_transaction(admin_client, db_module, recorded_connects, url):
    """Таблото, списъците и прегледът: BEGIN … ROLLBACK около целия изглед,
    без нито един запис и без втора връзка към базата междувременно (иначе
    при споделена база — взаимно чакане с пишещ от друг компютър)."""
    from conftest import issue_cmr
    issue_cmr(admin_client, consignee_name="Клиент Едно ЕООД")
    con = db_module.get_db()
    cmr = con.execute("SELECT id FROM documents ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.close()
    admin_client.get(url.format(cmr=cmr))  # загрявка (сесия, отчет на копието)
    del recorded_connects[:]
    resp = admin_client.get(url.format(cmr=cmr))
    assert resp.status_code == 200
    sqls = [s for s, _p in recorded_connects]
    assert "BEGIN" in sqls, sqls
    # Изгледът чете САМО вътре в транзакцията (преди нея — входът и отчетът).
    before = sqls[:sqls.index("BEGIN")]
    assert not any(re.search(r"\b(documents|clients|materials)\b", s) for s in before), before
    inside = recorded_connects[sqls.index("BEGIN") + 1:]
    assert inside and not any(s == "CONNECT" for s, _p in inside), inside
    assert not any(isinstance(s, str) and _WRITE_RE.match(s) for s, _p in inside), inside


def test_read_snapshot_releases_the_lock_when_the_view_fails(flask_app, db_module):
    import appcore

    @appcore.read_snapshot
    def broken():
        appcore.get_db().execute("SELECT 1")
        raise RuntimeError("x")

    with flask_app.test_request_context("/"):
        with pytest.raises(RuntimeError):
            broken()
        assert not appcore.get_db().in_transaction
        # Писащ от друга връзка не чака (споделеният катинар е пуснат).
        other = sqlite3.connect(db_module.DB_PATH, timeout=0.1)
        other.execute("UPDATE settings SET value = value WHERE key = 'x'")
        other.commit()
        other.close()


# ---------------------------------------------------------------- PERF-8 SVG спрайт

def _add_docs(db_module, n, doc_type="cmr"):
    con = db_module.get_db()
    con.executemany(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, 2026, ?, ?, ?, ?, 1)",
        [(doc_type, "%04d/2026" % i, i, "%s-S-%d" % (doc_type, i), "ts-%s-%d" % (doc_type, i),
          json.dumps({"consignee_name": "Клиент %d" % (i % 9), "invoice_number": "INV-%d" % i},
                     ensure_ascii=False)) for i in range(1, n + 1)])
    con.commit()
    con.close()


@pytest.mark.parametrize("url,container,names", [
    ("/docs", "docs-results", ("eye", "edit", "trash", "folder", "layers")),
    ("/docs?group=client", "docs-results", ("eye", "edit", "trash", "folder", "layers")),
    ("/invoices", "invoices-results", ("eye", "edit", "trash", "layers")),
    ("/clients", "clients-results", ("edit", "trash")),
    ("/materials", "materials-results", ("edit", "trash")),
    ("/invoices/clients", "invoice-clients-results", ("edit", "trash")),
])
def test_list_rows_use_one_sprite_per_icon(admin_client, db_module, url, container, names):
    import icons
    _add_docs(db_module, 30)
    _add_docs(db_module, 30, "invoice_br")
    con = db_module.get_db()
    for i in range(30):
        con.execute("INSERT INTO clients (name, city) VALUES (?, 'София')", ("Фирма %d" % i,))
        con.execute("INSERT INTO materials (code, description, net_weight) VALUES (?, 'Шина', '1.5')",
                    ("MAT-%03d" % i,))
        con.execute("INSERT INTO invoice_clients (name) VALUES (?)", ("Адрес %d" % i,))
    con.commit()
    con.close()
    body = admin_client.get(url).get_data(as_text=True)
    results = body[body.index('id="%s"' % container):]
    for name in names:
        assert body.count('<symbol id="i-%s"' % name) == 1, name
        # Символът е ВЪТРЕ в подменяемия от живото търсене контейнер.
        assert results.count('<symbol id="i-%s"' % name) == 1, name
        # Тялото на иконата е само в символа, не на всеки ред.
        assert results.count(icons._ICONS[name]) == 1, name
    uses = re.findall(r'<svg[^>]*><use href="#i-(\w+)"/></svg>', body)
    assert len(uses) >= 30 * min(2, len(names))
    for tag in re.findall(r'<svg[^>]*><use href="#i-\w+"/></svg>', body):
        assert 'aria-hidden="true"' in tag
    # Скритият спрайт не се вижда и не се чете от екранни четци.
    sprite = re.search(r'<svg class="icon-sprite"[^>]*>', body).group(0)
    assert 'aria-hidden="true"' in sprite and "position:absolute" in sprite


def test_live_search_response_carries_its_own_sprite(admin_client, db_module):
    _add_docs(db_module, 5)
    body = admin_client.get("/docs", query_string={"q": "клиент"},
                            headers={"X-Requested-With": "live-search"}).get_data(as_text=True)
    results = body[body.index('id="docs-results"'):]
    assert '<symbol id="i-eye"' in results and '<use href="#i-eye"/>' in results


def test_sprite_makes_the_document_list_lighter(admin_client, db_module, flask_app):
    import icons
    _add_docs(db_module, 100)
    with_sprite = admin_client.get("/docs").get_data(as_text=True)
    flask_app.jinja_env.globals["icon_ref"] = icons.render_icon  # старото: цяла икона на ред
    try:
        inline = admin_client.get("/docs").get_data(as_text=True)
    finally:
        flask_app.jinja_env.globals["icon_ref"] = icons.render_icon_ref
    assert len(with_sprite.encode("utf-8")) < 0.7 * len(inline.encode("utf-8"))


def test_icon_ref_falls_back_for_an_unknown_icon():
    import icons
    assert icons.render_icon_ref("няма-такава", 12) == icons.render_icon("няма-такава", 12)
    assert icons.render_icon_sprite("eye", "eye", "няма-такава").count("<symbol") == 1


# ---------------------------------------------------------------- PERF-2 живо търсене

def test_live_search_parses_with_one_reusable_template():
    from conftest import app_js_source
    js = app_js_source()
    body = js[js.index("function bindLiveSearch"):js.index("function initPdfExportBusy")]
    assert "DOMParser" not in body.replace("нов DOMParser документ", "")
    assert 'document.createElement("template")' in body
    assert "parser.innerHTML = html" in body and 'parser.innerHTML = ""' in body
    assert "results.innerHTML = fresh.innerHTML" not in body


@pytest.mark.e2e
def test_live_search_does_not_pile_up_documents_in_the_browser(page, live_server, db_module):
    """В Chromium: 12 живи търсения — броят документи/възли в паметта на
    страницата не расте (с DOMParser: +1 документ и ~6 000 възела на търсене)."""
    from conftest import e2e_login
    _add_docs(db_module, 60)
    e2e_login(page, live_server)
    page.goto(live_server + "/docs")
    cdp = page.context.new_cdp_session(page)

    def counters():
        cdp.send("HeapProfiler.collectGarbage")
        return cdp.send("Memory.getDOMCounters")

    box = page.locator(".searchbar-input").first

    def round_of_searches():
        for term in ["Клиент 1", "Клиент 2", "0001", "Клиент", "S-1", "zzz"]:
            with page.expect_response(lambda r: "/docs?" in r.url):
                box.fill(term)
            page.wait_for_timeout(100)
        page.wait_for_function("() => !document.querySelector('#docs-results').classList.contains('is-loading')")
        return counters()

    # Две еднакви серии, завършващи в едно и също състояние: втората не
    # бива да добави нищо (с DOMParser: +6 документа и ~36 000 възела).
    # Сравнява се серия със серия, а не с началото — колко отделени възела
    # чакат GC след ЕДНО търсене зависи от версията на Chromium.
    mid = round_of_searches()
    end = round_of_searches()
    assert end["documents"] <= mid["documents"], (mid, end)
    assert end["nodes"] <= mid["nodes"] + 1500, (mid, end)
    # Резултатът и иконите (спрайтът е в подменения контейнер) са налице.
    assert page.locator("#docs-results .empty-state, #docs-results table.list").count() >= 1
    box.fill("Клиент 3")
    page.wait_for_function("() => document.querySelectorAll('#docs-results tr[data-row-id], #docs-results tbody tr').length > 1")
    assert page.evaluate("() => !!document.querySelector('#docs-results symbol#i-eye')")
    w = page.evaluate("() => document.querySelector('#docs-results use').closest('svg').getBoundingClientRect().width")
    assert w > 0
