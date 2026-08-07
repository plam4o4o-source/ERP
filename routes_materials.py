# -*- coding: utf-8 -*-
"""Раздел „Материали“ — справочникът ABB part ID → описание → нето тегло.

Заявка: „да не се зарежда всеки път файла с материалите; като се зареди
веднъж, да си остава зареден в програмата“. Затова тук има само екран за
ЕДНОКРАТНО качване (с възможност за повторно качване при нов ценоразпис)
и търсене в вече заредените — самото автоматично попълване във фактурите
става в routes_invoices.py през materials.lookup/lookup_many.

Качването изисква администраторски права (както изтриването на клиент,
виж routes_clients — справочникът е обща фирмена база, не лични данни на
конкретен служител), а търсенето е достъпно за всеки логнат служител.
"""
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext as _

import applog
import materials
from appcore import admin_required, get_db, login_required


def register(app):
    app.add_url_rule("/materials", "materials_list", materials_list)
    app.add_url_rule("/materials/import", "materials_import", materials_import,
                     methods=["POST"])
    app.add_url_rule("/materials/lookup", "materials_lookup", materials_lookup)


@login_required
def materials_list():
    con = get_db()
    query = request.args.get("q", "")
    return render_template("materials.html", rows=materials.search(con, query),
                           q=query, total=materials.count(con))


@admin_required
def materials_import():
    """Зарежда/обновява справочника от качен Excel файл. Обновява вече
    съществуващите редове по код и добавя новите — НЕ трие останалите (виж
    materials.replace_catalog за причината)."""
    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash(_("Моля, изберете Excel файл (.xlsx) със справочника."))
        return redirect(url_for("materials_list"))
    try:
        entries = materials.parse_catalog_xlsx(file.read())
    except Exception:
        applog.log_exception("routes_materials: неуспешно четене на качен .xlsx справочник")
        flash(_("Файлът не може да бъде прочетен. Уверете се, че е валиден .xlsx файл."))
        return redirect(url_for("materials_list"))

    if not entries:
        flash(_("Файлът не съдържа разпознаваеми колони. Нужни са колона с код на "
                "материала (напр. „ABB part ID“) и поне една от „Description“ / "
                "„Net weight [KG/pc]“."))
        return redirect(url_for("materials_list"))

    added, updated = materials.replace_catalog(get_db(), entries)
    flash(_("Справочникът е зареден от „%(file)s“: %(added)d нови и %(updated)d "
            "обновени материала. Остава зареден — не е нужно да го качвате "
            "отново при всяка фактура.")
          % {"file": file.filename, "added": added, "updated": updated})
    return redirect(url_for("materials_list"))


@login_required
def materials_lookup():
    """JSON справка за един материал по код — ползва се от формите на
    фактурите за автоматично попълване на теглото/описанието, докато
    операторът въвежда кода (виж initInvoiceMaterialLookup в app.js)."""
    row = materials.lookup(get_db(), request.args.get("code", ""))
    if row is None:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "code": row["code"],
                    "description": row["description"], "net_weight": row["net_weight"]})
