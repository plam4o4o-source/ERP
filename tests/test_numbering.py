# -*- coding: utf-8 -*-
"""Тестове за номерирането на документи (db.next_number).

Това е носеща бизнес логика: форматът на номера и баркода се печата върху
официални документи, а поредността трябва да е коректна и уникална.
"""
import datetime

import pytest


def test_first_number_starts_at_one(con, db_module):
    number, year, seq, barcode = db_module.next_number(con, "cmr")
    today = datetime.date.today()
    this_year = today.year
    assert seq == 1
    assert year == this_year
    assert number == "0001/%d" % this_year
    # Заявка: „в баркодовете... да се съдържа и датата“ — пълна дата
    # (ДДММГГГГ) вместо само годината, вижте db.next_number.
    assert barcode == "CMR-%02d%02d%d-0001" % (today.day, today.month, today.year)


def test_sequence_increments_within_type(con, db_module):
    seqs = []
    for _ in range(5):
        _, _, seq, _ = db_module.next_number(con, "cmr")
        seqs.append(seq)
    assert seqs == [1, 2, 3, 4, 5]


def test_types_have_independent_counters(con, db_module):
    _, _, cmr_seq, cmr_bc = db_module.next_number(con, "cmr")
    _, _, pal_seq, pal_bc = db_module.next_number(con, "pallet")
    # Различните типове не си делят брояча — и двата започват от 1.
    assert cmr_seq == 1
    assert pal_seq == 1
    assert cmr_bc.startswith("CMR-")
    assert pal_bc.startswith("PAL-")


def test_all_doc_types_have_valid_prefix(con, db_module):
    for doc_type, meta in db_module.DOC_TYPES.items():
        _, _, _, barcode = db_module.next_number(con, doc_type)
        assert barcode.startswith(meta["prefix"] + "-")


def test_unknown_doc_type_raises(con, db_module):
    with pytest.raises(ValueError):
        db_module.next_number(con, "no_such_type")


def test_number_format_is_zero_padded_four_digits(con, db_module):
    # Прескачаме брояча до 9, за да проверим форматирането при преминаване към 4 цифри.
    today = datetime.date.today()
    this_year = today.year
    con.execute(
        "INSERT INTO counters (doc_type, year, last) VALUES ('cmr', ?, 9)",
        (this_year,),
    )
    number, _, seq, barcode = db_module.next_number(con, "cmr")
    assert seq == 10
    assert number == "0010/%d" % this_year
    assert barcode == "CMR-%02d%02d%d-0010" % (today.day, today.month, today.year)


def test_barcode_contains_full_issue_date_not_just_year(con, db_module):
    """Заявка: „в баркодовете... да се съдържа и датата“ — баркодът трябва
    да носи деня и месеца на издаване, не само годината."""
    today = datetime.date.today()
    _, _, _, barcode = db_module.next_number(con, "waybill")
    day_month = "%02d%02d" % (today.day, today.month)
    assert day_month in barcode
    assert str(today.year) in barcode


def test_barcode_is_unique_per_sequence(con, db_module):
    seen = set()
    for _ in range(20):
        _, _, _, barcode = db_module.next_number(con, "dualuse")
        assert barcode not in seen
        seen.add(barcode)


# ---------------------------------------------------------------- H6: едновременност

def test_concurrent_next_number_produces_no_duplicates(db_module):
    """Регресионен тест за H6: преди поправката, две ЕДНОВРЕМЕННИ връзки,
    всяка на своя нишка (симулира двама служители в мрежов режим), можеха
    да прочетат същия 'last' преди коя да е от двете да commit-не своя
    UPDATE — получавайки еднакъв seq/barcode, и вторият INSERT да гръмне
    (UNIQUE(barcode)) — губейки документа на потребителя. Тук всяка нишка
    отваря СВОЯ РЕАЛНА sqlite3 връзка (не споделя connection обект между
    нишки — това никога не е поддържано от sqlite3) към същия временен
    .db файл и извиква next_number + INSERT INTO documents + commit,
    точно както прави app.py:save_document() в реален заявков контекст."""
    import sqlite3
    import threading

    db_path = db_module.DB_PATH
    n_threads = 12
    per_thread = 5
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        con = sqlite3.connect(db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            for _ in range(per_thread):
                number, year, seq, barcode = db_module.next_number(con, "cmr")
                con.execute(
                    "INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
                    " VALUES ('cmr', ?, ?, ?, ?, '{}')",
                    (number, year, seq, barcode),
                )
                con.commit()
                with lock:
                    results.append(seq)
        except Exception as exc:  # искаме да видим ВСЯКА грешка в теста, не да я скрием
            with lock:
                errors.append(exc)
        finally:
            con.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, "неочаквани грешки при едновременни заявки: %r" % errors
    expected_total = n_threads * per_thread
    assert len(results) == expected_total
    # Най-важната проверка: НУЛА дублирани поредни номера, независимо от
    # реда, в който нишките реално са се изпълнили.
    assert len(set(results)) == expected_total
    assert sorted(results) == list(range(1, expected_total + 1))
