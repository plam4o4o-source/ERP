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
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from flask_babel import gettext as _

import applog
import db
import materials
import xlsx_import
from appcore import XlsxTooLargeError, admin_required, get_db, login_required


def register(app):
    app.add_url_rule("/materials", "materials_list", materials_list)
    app.add_url_rule("/materials/import", "materials_import", materials_import,
                     methods=["POST"])
    app.add_url_rule("/materials/lookup", "materials_lookup", materials_lookup)
    # Одит (01.10.2026, P13): ръчно добавяне/редакция/изтриване на ЕДИН ред.
    app.add_url_rule("/materials/save", "materials_save", materials_save, methods=["POST"])
    app.add_url_rule("/materials/delete", "materials_delete", materials_delete,
                     methods=["POST"])
    # Одит (22.08.2026, находка №6) — потвърждаване на предупреждението за
    # слети кодове. POST (променя състояние), само за администратор.
    app.add_url_rule("/materials/merge-notice/dismiss", "materials_merge_dismiss",
                     materials_merge_dismiss, methods=["POST"])


@login_required
def materials_list():
    return _render_list()


def _render_list(form_values=None):
    """Екранът „Материали“; `form_values` пази въведеното при отказ от
    ръчното добавяне/редакция (P13)."""
    con = get_db()
    query = request.args.get("q", "")
    if form_values is None and request.args.get("edit") and session.get("role") == "admin":
        edit_row = materials.get_exact(con, request.args.get("edit"))
        if edit_row is not None:
            form_values = {"original_code": edit_row["code"], "code": edit_row["code"],
                           "description": edit_row["description"],
                           "net_weight": edit_row["net_weight"]}
    # Одит (22.08.2026, находка №6, средна): миграцията _m010 слива кодове,
    # различаващи се само по регистър, и ИЗТРИВА излишните редове. Досега
    # единствената следа беше ред в `pacho_startup.log` — файл, който
    # потребител на .exe никога не отваря; операторът просто откриваше (или
    # НЕ откриваше) променено нето тегло на митническия опаковъчен лист.
    # Тук списъкът стига до самия екран „Материали“, при първото му
    # отваряне след обновяването, и стои, докато не бъде потвърден.
    return render_template("materials.html", rows=materials.search(con, query),
                           q=query, total=materials.count(con),
                           merged_notice=db.merged_materials_notice(con),
                           form_values=form_values or {})


@admin_required
def materials_save():
    """Одит (01.10.2026, P13): справочникът беше само Excel — една поправка
    на тегло изискваше целия файл наново. Празен original_code = нов ред."""
    values = {k: (request.form.get(k) or "").strip()
              for k in ("original_code", "code", "description", "net_weight")}
    try:
        code = materials.save_one(get_db(), values["code"], values["description"],
                                  values["net_weight"], values["original_code"] or None)
    except materials.MaterialError as exc:
        if exc.reason == "missing":
            flash(_("Материалът „%(code)s“ вече не съществува.") % {"code": exc.code}, "error")
            return redirect(url_for("materials_list"))
        flash({
            "code": _("Въведете код на материала."),
            "weight": _("Нето теглото трябва да е неотрицателно число (напр. 0.25) или празно."),
            "duplicate": _("Материал с код „%(code)s“ вече съществува (кодовете не се "
                           "различават по главни/малки букви).") % {"code": exc.code},
        }[exc.reason], "error")
        return _render_list(form_values=values)
    detail = "код=%s" % code
    if values["original_code"] and values["original_code"] != code:
        detail += ", беше=%s" % values["original_code"]
    applog.log_audit("ръчна промяна в справочника материали", detail)
    flash(_("Материалът „%(code)s“ е запазен.") % {"code": code}, "success")
    return redirect(url_for("materials_list", q=code))


@admin_required
def materials_delete():
    row = materials.delete_one(get_db(), request.form.get("code", ""))
    if row is None:
        flash(_("Материалът „%(code)s“ вече не съществува.")
              % {"code": request.form.get("code", "")}, "error")
    else:
        applog.log_audit("изтрит материал от справочника", "код=%s" % row["code"])
        flash(_("Материалът „%(code)s“ е изтрит от справочника.") % {"code": row["code"]},
              "success")
    return redirect(url_for("materials_list"))


@admin_required
def materials_merge_dismiss():
    """Скрива предупреждението за слети кодове (находка №6), след като
    администраторът потвърди, че го е видял. Самите копия на изтритите
    редове ОСТАВАТ в `materials_merged_backup` — скрива се съобщението, не
    доказателството."""
    con = get_db()
    groups = db.merged_materials_notice(con)
    db.dismiss_merged_materials_notice(con)
    con.commit()
    applog.log_audit(
        "потвърдено предупреждение за слети кодове в справочника материали",
        "групи=%d, с различно тегло=%d"
        % (len(groups), sum(1 for g in groups if g["weight_conflict"])))
    flash(_("Предупреждението е скрито. Копие на слетите редове остава запазено "
            "в базата."), "success")
    return redirect(url_for("materials_list"))


@admin_required
def materials_import():
    """Зарежда/обновява справочника от качен Excel файл. Обновява вече
    съществуващите редове по код и добавя новите — НЕ трие останалите (виж
    materials.replace_catalog за причината)."""
    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash(_("Моля, изберете Excel файл (.xlsx) със справочника."), "error")
        return redirect(url_for("materials_list"))
    # Одит (19.08.2026, находка №38, дребна): справочникът беше единственият
    # от трите Excel импорта БЕЗ защитите на находка №18 (16.08) — без таван
    # на редовете, без предупреждение за обединени клетки, без съобщение кой
    # ред е приет за заглавен и без брояч за дублирани кодове (дублиран код
    # тихо презаписваше първия с ДРУГО тегло, а съобщението рапортуваше „1
    # нови и 1 обновени“, все едно са два различни материала). `stats` носи
    # числата от разбора; самите съобщения се съставят ТУК, защото
    # materials.py се ползва и извън заявка (виж коментара там).
    stats = {}
    try:
        entries = materials.parse_catalog_xlsx(file.read(), stats=stats)
    except XlsxTooLargeError as exc:
        # Одит (01.09.2026, доуточнение на находка №7, установено при
        # преглед на v3.69.0): materials.parse_catalog_xlsx вика
        # ensure_xlsx_within_limits ВЪТРЕ в себе си (виж коментара там), но
        # тук изключението падаше в общия `except Exception` по-долу —
        # операторът виждаше подвеждащото „файлът не може да бъде
        # прочетен... уверете се, че е валиден“, макар файлът да Е валиден,
        # просто твърде голям. routes_invoices.py и routes_pallet_extra.py
        # вече хващат XlsxTooLargeError отделно и показват КОНКРЕТНОТО ѝ
        # съобщение (лимитите в MB + съветът да се раздели файлът) —
        # справочникът с материали беше единственият от трите Excel импорта
        # без тази конкретика, въпреки че коментарът долу изрично твърди
        # съобщенията да са „ДУМА ПО ДУМА същите като в другите два“.
        flash(str(exc), "error")
        return redirect(url_for("materials_list"))
    except Exception:
        applog.log_exception("routes_materials: неуспешно четене на качен .xlsx справочник")
        flash(_("Файлът не може да бъде прочетен. Уверете се, че е валиден .xlsx файл."), "error")
        return redirect(url_for("materials_list"))

    if not entries:
        flash(_("Файлът не съдържа разпознаваеми колони. Нужни са колона с код на "
                "материала (напр. „ABB part ID“) и поне една от „Description“ / "
                "„Net weight [KG/pc]“."), "error")
        return redirect(url_for("materials_list"))

    # Съобщенията нарочно са ДУМА ПО ДУМА същите като в другите два Excel
    # импорта (routes_pallet_extra/routes_invoices) — един и същ msgid
    # означава един превод и еднакъв текст пред оператора, независимо кой
    # от трите файла качва.
    if stats.get("header_row", 1) > 1:
        flash(xlsx_import.header_found_warning(stats["header_row"]), "warning")
    if stats.get("truncated"):
        max_rows = stats.get("max_rows", 0)
        # Одит (22.09.2026, находка №10): съобщението казваше КАКВО е
        # пропуснато, но не и какво да направи операторът. Реален
        # ценоразпис на голям доставчик минава 5000 артикула, а
        # справочникът е НАТРУПВАЩ (materials.replace_catalog обновява и
        # добавя, не трие останалите) — значи качване на части наистина
        # работи. Текстът е ДУМА ПО ДУМА същият като в другите два Excel
        # импорта (routes_invoices/routes_pallet_extra) — един msgid,
        # един превод, еднакъв текст пред оператора.
        flash(_("Файлът съдържа повече от %d реда данни — заредени са само "
                "първите %d, останалите са пропуснати. Разделете файла на "
                "части до %d реда и ги качете една след друга — всяко "
                "следващо качване се добавя към вече заредените.")
              % (max_rows, max_rows, max_rows), "warning")
    if stats.get("merged_cells"):
        flash(xlsx_import.merged_cells_warning(), "warning")
    if stats.get("bad_weights"):
        # Одит (19.08.2026, находка №28а): текстово „nan“/„N/A“/„—“ или
        # отрицателно тегло вече НЕ влиза сурово в справочника (оттам — на
        # официалната бланка на фактура), а се брои и се съобщава.
        flash(_("%(count)d реда с неразпознато тегло (напр. „nan“, „N/A“, „—“ или "
                "отрицателна стойност) — за тях теглото остава празно и се въвежда "
                "ръчно.") % {"count": stats["bad_weights"]}, "warning")
    if stats.get("duplicate_codes"):
        flash(_("%(count)d реда с код, който вече се среща по-горе в същия файл — "
                "за всеки такъв код важи ПОСЛЕДНИЯТ ред, по-горните се "
                "презаписват.") % {"count": stats["duplicate_codes"]}, "warning")

    save_stats = {}
    added, updated = materials.replace_catalog(get_db(), entries, stats=save_stats)
    if save_stats.get("case_conflicts"):
        # Одит (19.08.2026, находка №28б): кодове, различаващи се само по
        # регистър, вече обновяват СЪЩИЯ ред (иначе lookup и lookup_many
        # връщаха различни килограми за един и същ материал).
        flash(_("%(count)d кода се различават само по регистър от вече заредени — "
                "обновен е съществуващият материал, вместо да се създаде втори "
                "запис със същия код.") % {"count": save_stats["case_conflicts"]},
              "warning")
    flash(_("Справочникът е зареден от „%(file)s“: %(added)d нови и %(updated)d "
            "обновени материала. Остава зареден — не е нужно да го качвате "
            "отново при всяка фактура.")
          % {"file": file.filename, "added": added, "updated": updated}, "success")
    return redirect(url_for("materials_list"))


#: Одит (09.09.2026, подобрение №3): таван на предложенията при живо
#: търсене по код/описание (виж клона `q=` по-долу) — същият модел като
#: invoice_clients_module.LOOKUP_LIMIT, за да не се вгражда целият
#: справочник (може да е с хиляди артикули) в един отговор.
MATERIALS_SEARCH_LOOKUP_LIMIT = 20


@login_required
def materials_lookup():
    """JSON справка за материали.

    Две отделни употреби зад един и същ route:

    * `?code=` — ТОЧНО съвпадение по код (историческата употреба, ползва
      се от формите на фактурите за автоматично попълване на теглото/
      описанието при напускане на полето, виж initInvoiceMaterialLookup в
      app.js).
    * `?q=` — Одит (09.09.2026, подобрение №3): живо търсене по код ИЛИ
      описание, докато операторът пише — редовете на фактура/опаковъчен
      лист нямаха никакво автодовършване за код на материал, само тази
      проверка при напускане на полето; ако кодът не се помнеше наизуст,
      единственият път беше отделен таб към /materials и обратно. Връща
      списък (не едно съвпадение), захранва `<datalist>` в браузъра —
      виж attachMaterialCodeSearch в app.js."""
    q = request.args.get("q")
    if q is not None:
        rows = materials.search(get_db(), q, limit=MATERIALS_SEARCH_LOOKUP_LIMIT)
        return jsonify({"materials": [
            {"code": r["code"], "description": r["description"],
             "net_weight": r["net_weight"]} for r in rows]})
    row = materials.lookup(get_db(), request.args.get("code", ""))
    if row is None:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "code": row["code"],
                    "description": row["description"], "net_weight": row["net_weight"]})
