# -*- coding: utf-8 -*-
"""Фактури — отделен тип за Бразилия (invoice_br) и за Норвегия
(invoice_no), плюс зареждането на редове от вече издадена палетна карта.

Издаването/прегледът минават през СЪЩИТЕ generic функции като останалите
документи (appcore.DOCUMENT_FLOWS → routes_documents._document_new/
_document_preview) — тук са само тънките wrapper-и с точните endpoint
имена, и специфичното за фактурите: изтеглянето на редове от палетна
карта с автоматично попълване на теглото/описанието от справочника
материали.

Защо два отделни типа, а не един с поле „държава“: самите бланки се
различават по заглавие И по колони на таблицата със стоките (Бразилия:
Net weight, без описание; Норвегия: Material Description + Pallet Number,
без тегло — виж приложените образци). Един тип би означавал разклонения
във всяка бланка, всеки износ и всяка форма; два типа следват вече
установения в програмата модел „един тип = една бланка“.
"""
import json

from flask import request
from flask_babel import gettext as _

import db
import materials
from appcore import get_db, login_required
from routes_documents import _document_new, _document_preview


def register(app):
    app.add_url_rule("/invoice-br/new", "invoice_br_new", invoice_br_new,
                     methods=["GET", "POST"])
    app.add_url_rule("/invoice-br/preview", "invoice_br_preview", invoice_br_preview,
                     methods=["POST"])
    app.add_url_rule("/invoice-no/new", "invoice_no_new", invoice_no_new,
                     methods=["GET", "POST"])
    app.add_url_rule("/invoice-no/preview", "invoice_no_preview", invoice_no_preview,
                     methods=["POST"])
    app.add_url_rule("/invoice/pull-pallet", "invoice_pull_pallet", invoice_pull_pallet,
                     methods=["POST"])


@login_required
def invoice_br_new():
    return _document_new("invoice_br")


@login_required
def invoice_br_preview():
    return _document_preview("invoice_br")


@login_required
def invoice_no_new():
    return _document_new("invoice_no")


@login_required
def invoice_no_preview():
    return _document_preview("invoice_no")


#: Тарифният код е един и същ за всички редове в приложените образци и
#: рядко се сменя — попълва се по подразбиране на всеки изтеглен ред, за
#: да не се преписва на ръка, но си остава редактируем във формата.
DEFAULT_HS_CODE = "85389099"


@login_required
def invoice_pull_pallet():
    """Издърпва ВСИЧКИ редове на вече издадена палетна карта, преобразувани
    в редове за фактура — заявка: „фактурата да може да се зарежда, както
    се зареждат палетните карти в опаковъчния лист, но само със
    съответните данни, които са необходими за фактура“.

    За разлика от packing_pull_pallet (издърпва ЕДИН обобщен ред, защото
    опаковъчният лист описва колети, не отделни артикули), фактурата има
    нужда от всеки материал поотделно — колоните на палетната карта във
    формат „поръчки“ съвпадат почти едно към едно с тези на фактурата:

        Order No       → P.O NO
        Pos            → Pos
        Reference      → Material code
        Reference Desc → Material Description
        Open Qty       → Quantity

    Липсващото нето тегло (и описанието, ако картата няма попълнено) се
    допълва от справочника материали по кода — това е самото „автоматично
    извличане на килограмите“ от заявката. Кодове, които ги няма в
    справочника, просто остават с празно тегло за ръчно попълване; редът
    НЕ се пропуска. Единичната цена винаги остава празна — тя се въвежда
    ръчно (виж отговора на въпроса при заданието).
    """
    code = (request.form.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": _("Въведете номер или баркод на палетна карта.")}

    con = get_db()
    row = con.execute(
        "SELECT * FROM documents WHERE doc_type = 'pallet' AND (barcode = ? OR number = ?)"
        " ORDER BY id DESC LIMIT 1",
        (code, code),
    ).fetchone()
    if row is None:
        other = con.execute(
            "SELECT doc_type FROM documents WHERE barcode = ? OR number = ?"
            " ORDER BY id DESC LIMIT 1",
            (code, code),
        ).fetchone()
        if other is not None:
            title = db.DOC_TYPES.get(other["doc_type"], {}).get("title", other["doc_type"])
            return {"ok": False,
                    "error": _("Намереният документ не е палетна карта (%s).") % title}
        return {"ok": False, "error": _("Няма документ с номер/баркод „%s“.") % code}

    d = json.loads(row["data"])
    items = d.get("items") or []
    if not items:
        return {"ok": False,
                "error": _("Палетна карта № %s няма редове за прехвърляне.") % row["number"]}

    orders_format = d.get("items_format") == "orders"
    # Кодът на материала е "reference" при формат „поръчки“ и "code" при
    # обикновения формат — и в двата случая това е ключът към справочника.
    codes = [(it.get("reference") if orders_format else it.get("code")) or "" for it in items]
    found = materials.lookup_many(con, codes)

    pallet_no = d.get("pallet_no") or ""
    rows = []
    matched = 0
    for it, material_code in zip(items, codes):
        entry = found.get(material_code)
        if entry is not None:
            matched += 1
        if orders_format:
            description = it.get("reference_desc") or ""
            po_no, pos, qty = it.get("order_no") or "", it.get("pos") or "", it.get("qty") or ""
        else:
            description = it.get("description") or ""
            po_no, pos, qty = "", "", it.get("qty") or ""
        rows.append({
            "hs_code": DEFAULT_HS_CODE,
            "po_no": po_no,
            "pos": pos,
            "material_code": material_code,
            # Описанието от палетната карта има предимство пред това от
            # справочника (операторът може да го е уточнил за конкретната
            # пратка); справочникът е резервният източник.
            "description": description or (entry["description"] if entry else ""),
            "pallet_no": pallet_no,
            "qty": qty,
            "net_weight": entry["net_weight"] if entry else "",
            "unit_price": "",
        })

    return {
        "ok": True,
        "number": row["number"],
        "count": len(rows),
        "matched": matched,
        "rows": rows,
    }
