# -*- coding: utf-8 -*-
"""Тестове за номерирането на документи (db.next_number).

Това е носеща бизнес логика: форматът на номера и баркода се печата върху
официални документи, а поредността трябва да е коректна и уникална.
"""
import datetime

import pytest


def test_first_number_starts_at_one(con, db_module):
    number, year, seq, barcode = db_module.next_number(con, "cmr")
    this_year = datetime.date.today().year
    assert seq == 1
    assert year == this_year
    assert number == "0001/%d" % this_year
    assert barcode == "CMR-%d-0001" % this_year


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
    this_year = datetime.date.today().year
    con.execute(
        "INSERT INTO counters (doc_type, year, last) VALUES ('cmr', ?, 9)",
        (this_year,),
    )
    number, _, seq, barcode = db_module.next_number(con, "cmr")
    assert seq == 10
    assert number == "0010/%d" % this_year
    assert barcode == "CMR-%d-0010" % this_year


def test_barcode_is_unique_per_sequence(con, db_module):
    seen = set()
    for _ in range(20):
        _, _, _, barcode = db_module.next_number(con, "dualuse")
        assert barcode not in seen
        seen.add(barcode)
