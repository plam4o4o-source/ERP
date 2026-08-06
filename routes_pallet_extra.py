# -*- coding: utf-8 -*-
"""Допълнителни хендлъри около палетни карти и опаковъчни листи: издърпване
на обобщен ред от палетна карта в опаковъчен лист, импорт от Excel (единична
карта и bulk импорт от справка за поръчки), плюс предварителен преглед и
масово издаване на bulk-внесените карти. Извлечено от app.py (Фаза 3) без
промяна в поведението."""
import io
import json

from flask import flash, redirect, render_template, request, send_file, url_for
from flask_babel import gettext as _

import applog
import db
from appcore import (_get_preview, _store_preview, clients_json, load_clients,
                     login_required, save_document)


def register(app):
    app.add_url_rule("/packing/pull-pallet", "packing_pull_pallet",
                     packing_pull_pallet, methods=["POST"])
    app.add_url_rule("/pallet/import", "pallet_import", pallet_import, methods=["POST"])
    app.add_url_rule("/pallet/sample.xlsx", "pallet_sample", pallet_sample)
    app.add_url_rule("/pallet/bulk-import", "pallet_bulk_import",
                     pallet_bulk_import, methods=["POST"])
    app.add_url_rule("/pallet/bulk-preview", "pallet_bulk_preview",
                     pallet_bulk_preview, methods=["POST"])
    app.add_url_rule("/pallet/bulk-preview/<token>", "pallet_bulk_preview_view",
                     pallet_bulk_preview_view)
    app.add_url_rule("/pallet/bulk-issue", "pallet_bulk_issue",
                     pallet_bulk_issue, methods=["POST"])
    app.add_url_rule("/pallet/bulk-result", "pallet_bulk_result", pallet_bulk_result)


@login_required
def packing_pull_pallet():
    """Издърпва обобщен ред (съдържание + нето/бруто тегло) от вече
    издадена палетна карта по нейния номер или баркод, за добавяне в
    опаковъчния лист — без ръчно преписване на данните."""
    code = request.form.get("code", "").strip()
    if not code:
        return {"ok": False, "error": "Въведете номер или баркод на палетна карта."}
    con = db.get_db()
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
        con.close()
        if other is not None:
            title = db.DOC_TYPES.get(other["doc_type"], {}).get("title", other["doc_type"])
            return {"ok": False, "error": "Намереният документ не е палетна карта (%s)." % title}
        return {"ok": False, "error": "Няма документ с номер/баркод „%s“." % code}
    con.close()

    d = json.loads(row["data"])
    items = d.get("items") or []
    if d.get("items_format") == "orders":
        labels = [it.get("reference_desc") or it.get("reference") or it.get("order_no") or ""
                 for it in items]
    else:
        labels = [it.get("description") or it.get("code") or "" for it in items]
    labels = [l for l in labels if l]
    summary = ", ".join(labels[:3])
    if len(labels) > 3:
        summary += " и още %d" % (len(labels) - 3)
    description = "Палет %s" % (d.get("pallet_no") or row["number"])
    if summary:
        description += " — " + summary

    return {
        "ok": True,
        "number": row["number"],
        "row": {
            "description": description,
            "qty": d.get("boxes") or str(len(items)) or "1",
            "packing": "Палет",
            "net": d.get("net", ""),
            "gross": d.get("gross", ""),
        },
    }


@login_required
def pallet_import():
    """Импорт на редове за палетна карта от Excel файл (.xlsx).

    Очаквани колони: Артикул/код | Описание | Количество | Тегло (кг).
    Първият ред се пропуска, ако изглежда като заглавен.
    """
    from openpyxl import load_workbook

    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash(_("Моля, изберете Excel файл (.xlsx)."))
        return redirect(url_for("pallet_new"))
    try:
        wb = load_workbook(io.BytesIO(file.read()), data_only=True)
    except Exception:
        applog.log_exception("routes_pallet_extra: неуспешно четене на качен .xlsx файл")
        flash(_("Файлът не може да бъде прочетен. Уверете се, че е валиден .xlsx файл."))
        return redirect(url_for("pallet_new"))

    ws = wb.worksheets[0]
    items = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        cells = ["" if c is None else str(c).strip() for c in row[:4]]
        cells += [""] * (4 - len(cells))
        if not any(cells):
            continue
        if i == 0 and _looks_like_header(cells):
            continue
        items.append({"code": cells[0], "description": cells[1],
                      "qty": cells[2], "weight": cells[3]})

    if not items:
        flash(_("Във файла не бяха намерени редове с данни."))
        return redirect(url_for("pallet_new"))

    con = db.get_db()
    clients = load_clients(con)
    settings = db.get_settings(con)
    con.close()
    flash(_("Заредени са %d реда от „%s“. Прегледайте и издайте картата.") %
          (len(items), file.filename))
    return render_template("pallet_form.html", clients=clients,
                           clients_json=clients_json(clients), s=settings,
                           items=items)


def _looks_like_header(cells):
    joined = " ".join(cells).lower()
    keywords = ("артикул", "код", "описание", "колич", "тегло",
                "code", "item", "description", "qty", "quantity", "weight")
    return any(k in joined for k in keywords)


def _cellstr(v):
    """Клетка към низ, без излишно „.0“ за цели числа, записани като float."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _parse_order_export(ws):
    """Разпознава експортен файл на поръчки (колони Due Date, Order No, Pos,
    Project, Reference, Reference Desc, Open Qty, Unit, Stock, <номер на
    палетна карта>) и групира редовете по последната колона — всеки различен
    номер там става отделна палетна карта. Reference и Reference Desc се
    оставят празни за конкретен ред, ако липсват там (или изобщо няма такива
    колони във файла) — не се попълват с друга стойност. Връща
    {номер: [items]} подредени по реда на поява, или None ако форматът не е
    разпознат."""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return None
    header = [_cellstr(c) for c in rows[0]]
    header_lower = [h.lower() for h in header]

    def find_col(*names):
        for name in names:
            for i, h in enumerate(header_lower):
                if h == name:
                    return i
        return None

    col_order = find_col("order no", "order number", "orderno")
    col_pos = find_col("pos", "position")
    col_ref = find_col("reference")
    col_ref_desc = find_col("reference desc", "reference description", "ref desc")
    col_qty = find_col("open qty", "qty", "quantity")
    if col_order is None or col_qty is None:
        return None

    # Групиращата колона е последната без заглавие (примерният файл я оставя
    # безименна) — резервно, ако всички колони имат заглавие, вземаме
    # последната изобщо.
    group_col = None
    for i in range(len(header) - 1, -1, -1):
        if header[i] == "":
            group_col = i
            break
    if group_col is None:
        group_col = len(header) - 1

    def cell(row, i):
        if i is None or i >= len(row):
            return ""
        return _cellstr(row[i])

    groups = {}
    for row in rows[1:]:
        if row is None or all(c is None for c in row):
            continue
        order_no = cell(row, col_order)
        if not order_no:
            continue
        group_raw = row[group_col] if group_col < len(row) else None
        try:
            group = int(group_raw)
        except (TypeError, ValueError):
            group = 1
        groups.setdefault(group, []).append({
            "order_no": order_no,
            "pos": cell(row, col_pos),
            "reference": cell(row, col_ref),
            "reference_desc": cell(row, col_ref_desc),
            "qty": cell(row, col_qty),
        })
    return groups if groups else None


@login_required
def pallet_sample():
    """Примерен Excel файл за импорт на палетна карта."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Палетна карта"
    ws.append(["Артикул/код", "Описание", "Количество", "Тегло (кг)"])
    ws.append(["ART-001", "Кашон резервни части", 10, 125.5])
    ws.append(["ART-002", "Кутия крепежни елементи", 4, 38])
    for col, width in zip("ABCD", (16, 40, 14, 14)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="primeren_palet_import.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


@login_required
def pallet_bulk_import():
    """Импорт от справка за поръчки (Order No, Pos, Reference, Open Qty) —
    редовете се разделят автоматично в отделни палетни карти по последната
    колона на файла (номер на палет)."""
    from openpyxl import load_workbook

    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash(_("Моля, изберете Excel файл (.xlsx)."))
        return redirect(url_for("pallet_new"))
    try:
        wb = load_workbook(io.BytesIO(file.read()), data_only=True)
    except Exception:
        applog.log_exception("routes_pallet_extra: неуспешно четене на качен .xlsx файл")
        flash(_("Файлът не може да бъде прочетен. Уверете се, че е валиден .xlsx файл."))
        return redirect(url_for("pallet_new"))

    groups = _parse_order_export(wb.worksheets[0])
    if not groups:
        flash(_("Файлът не съдържа разпознаваеми колони (Order No, Pos, Reference, "
             "Open Qty) или редове за импорт."))
        return redirect(url_for("pallet_new"))

    con = db.get_db()
    clients = load_clients(con)
    settings = db.get_settings(con)
    con.close()
    ordered = sorted(groups.items())
    flash(_("Открити са %d палетни карти (%d реда общо) от „%s“. Прегледайте и издайте.") %
          (len(ordered), sum(len(v) for _, v in ordered), file.filename))
    return render_template("pallet_bulk_review.html", clients=clients,
                           clients_json=clients_json(clients), s=settings,
                           groups=ordered)


def _collect_bulk_pallet_drafts():
    """Чете подадените от прегледа за bulk импорт полета (общи за
    партидата + поотделно за всеки палет — тип, кашони, нето, бруто,
    височина) и връща списък от речници с данните за всяка карта, БЕЗ да
    ги записва в базата. Ползва се и от прегледа (без запис), и от
    реалното издаване."""
    shared_fields = ("sender_name", "sender_city", "client_name", "client_address",
                     "client_city", "client_country", "doc_date", "ref_cmr", "notes")
    shared = {k: request.form.get(k, "").strip() for k in shared_fields}
    per_card_fields = ("pallet_type", "boxes", "net", "gross", "height")
    group_ids = [g for g in request.form.get("groups", "").split(",") if g.strip()]

    drafts = []
    for g in group_ids:
        raw = request.form.get("items_json_%s" % g, "[]")
        try:
            items = json.loads(raw)
        except ValueError:
            items = []
        items = [it for it in items if isinstance(it, dict) and
                 any((it.get(k) or "").strip() if isinstance(it.get(k), str) else it.get(k)
                     for k in ("order_no", "pos", "reference", "reference_desc", "qty"))]
        if not items:
            continue
        data = dict(shared)
        for f in per_card_fields:
            data[f] = request.form.get("%s_%s" % (f, g), "").strip()
        data["items"] = items
        data["items_format"] = "orders"
        data["pallet_no"] = "%s от %s" % (g, len(group_ids))
        drafts.append(data)
    return drafts


@login_required
def pallet_bulk_preview():
    """Предварителен преглед на всички палетни карти от прегледа за bulk
    импорт, точно както ще изглеждат при печат — БЕЗ да се записват в
    базата и БЕЗ да се изразходват номера. POST → съхрани → пренасочи →
    GET, за да е безопасно презареждане/връщане назад на страницата (виж
    _store_preview в appcore.py)."""
    drafts = _collect_bulk_pallet_drafts()
    if not drafts:
        flash(_("Няма палетни карти за преглед (всички редове са празни)."))
        return redirect(url_for("pallet_new"))
    token = _store_preview("bulk_pallet", drafts)
    return redirect(url_for("pallet_bulk_preview_view", token=token))


@login_required
def pallet_bulk_preview_view(token):
    drafts = _get_preview(token, "bulk_pallet")
    if drafts is None:
        flash(_("Прегледът е изтекъл или вече е използван — заредете файла отново."))
        return redirect(url_for("pallet_new"))
    return render_template("pallet_bulk_preview.html", drafts=drafts)


@login_required
def pallet_bulk_issue():
    """Издава наведнъж всички палетни карти от прегледа за импорт от
    справка за поръчки. Изпращач/клиент/дата/бележки са общи за цялата
    партида, но размерите и теглото на всеки палет (тип, кашони, нето,
    бруто, височина) се задават и записват отделно за всяка карта."""
    drafts = _collect_bulk_pallet_drafts()
    if not drafts:
        flash(_("Няма палетни карти за издаване (всички редове са празни)."))
        return redirect(url_for("pallet_new"))

    con = db.get_db()
    created = []
    for data in drafts:
        doc_id = save_document(con, "pallet", data)
        created.append((data["number"], doc_id))
    con.close()

    flash(_("Издадени и запазени %d палетни карти: %s") %
         (len(created), ", ".join(num for num, _ in created)))
    return redirect(url_for("pallet_bulk_result",
                            ids=",".join(str(doc_id) for _, doc_id in created)))


@login_required
def pallet_bulk_result():
    """Преглед на току-що издадените палетни карти преди печат — списък с
    бърз линк към всяка, за да се провери всяка карта, преди да се
    разпечата."""
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()]
    con = db.get_db()
    docs = []
    for doc_id in ids:
        row = con.execute(
            "SELECT d.*, u.full_name AS author FROM documents d"
            " LEFT JOIN users u ON u.id = d.created_by WHERE d.id = ?",
            (doc_id,),
        ).fetchone()
        if row is not None:
            docs.append((row, json.loads(row["data"])))
    con.close()
    return render_template("pallet_bulk_result.html", docs=docs)
