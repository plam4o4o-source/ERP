# -*- coding: utf-8 -*-
"""Допълнителни хендлъри около палетни карти и опаковъчни листи: издърпване
на обобщен ред от палетна карта в опаковъчен лист, bulk импорт от справка за
поръчки, плюс предварителен преглед и масово издаване на bulk-внесените
карти. Извлечено от app.py (Фаза 3) без промяна в поведението."""
import io
import json

from flask import flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

import applog
import db
from appcore import (_get_preview, _store_preview, clients_json, get_db, load_clients,
                     login_required, pallet_total_qty, safe_json_data, save_document)


def register(app):
    app.add_url_rule("/packing/pull-pallet", "packing_pull_pallet",
                     packing_pull_pallet, methods=["POST"])
    app.add_url_rule("/pallet/bulk-import", "pallet_bulk_import",
                     pallet_bulk_import, methods=["POST"])
    app.add_url_rule("/pallet/bulk-preview", "pallet_bulk_preview",
                     pallet_bulk_preview, methods=["POST"])
    app.add_url_rule("/pallet/bulk-preview/<token>", "pallet_bulk_preview_view",
                     pallet_bulk_preview_view)
    app.add_url_rule("/pallet/bulk-issue", "pallet_bulk_issue",
                     pallet_bulk_issue, methods=["POST"])
    app.add_url_rule("/pallet/bulk-result", "pallet_bulk_result", pallet_bulk_result)
    app.add_url_rule("/pallet/bulk-print", "pallet_bulk_print", pallet_bulk_print)


@login_required
def packing_pull_pallet():
    """Издърпва обобщен ред (съдържание + нето/бруто тегло) от вече
    издадена палетна карта по нейния номер или баркод, за добавяне в
    опаковъчния лист — без ръчно преписване на данните."""
    code = request.form.get("code", "").strip()
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
            return {"ok": False, "error": _("Намереният документ не е палетна карта (%s).") % title}
        return {"ok": False, "error": _("Няма документ с номер/баркод „%s“.") % code}

    d = safe_json_data(row["data"])
    items = d.get("items") or []
    if d.get("items_format") == "orders":
        labels = [it.get("reference_desc") or it.get("reference") or it.get("order_no") or ""
                 for it in items]
    else:
        labels = [it.get("description") or it.get("code") or "" for it in items]
    labels = [l for l in labels if l]
    summary = ", ".join(labels[:3])
    if len(labels) > 3:
        # Дребни (одит): голи низове без _() — при интерфейс на EN/TR
        # излизаха на български независимо от избрания език.
        summary += _(" и още %d") % (len(labels) - 3)
    description = _("Палет %s") % (d.get("pallet_no") or row["number"])
    if summary:
        description += " — " + summary

    return {
        "ok": True,
        "number": row["number"],
        # Одит (находка С12, нисък риск): по-рано тук стоеше d.get("net", "")
        # — палетната карта отдавна НЯМА поле „net“ (заменено с „Общ брой“,
        # виж appcore.pallet_total_qty), затова полето беше ВИНАГИ празно,
        # без операторът да разбира защо. "note" обяснява изрично защо
        # нето теглото трябва да се въведе ръчно, вместо мълчаливо празно
        # поле да изглежда като грешка в самата програма.
        "note": _("Палетната карта не пази нето тегло — попълнете го ръчно."),
        "row": {
            "description": description,
            "qty": pallet_total_qty(items) or str(len(items)) or "1",
            "packing": _("Палет"),
            "gross": d.get("gross", ""),
        },
    }


def _cellstr(v):
    """Клетка към низ, без излишно „.0“ за цели числа, записани като float."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _parse_group_numbers(raw):
    """Парсва стойността на групиращата колона в _parse_order_export —
    обикновено едно цяло число, но може да съдържа няколко номера,
    разделени с „+“ (напр. „1+3+4“), ако редът принадлежи физически на
    няколко палетни карти едновременно (материалът е физически наличен и в
    двете/трите). Връща списък от int групи; непарсваемо/празно съдържание
    пада към [1] (по подразбиране всичко отива в карта № 1).

    Одит (12.08.2026, находка №7, high): дублирани номера в стойността
    (напр. „1+1“ — печатна грешка или copy-paste в изходния файл) преди
    тази поправка добавяха СЪЩИЯ артикул ДВА ПЪТИ в СЪЩАТА карта —
    удвоено количество, дублиран ред на бланката, без нищо да сигнализира
    проблема. Дубликатите се премахват тук, като се пази реда на първа
    поява (напр. „1+2+1“ → [1, 2], не [1, 2, 1])."""
    if raw is None:
        return [1]
    parts = str(raw).split("+")
    nums = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            n = int(float(p))
        except (TypeError, ValueError):
            continue
        if n not in nums:
            nums.append(n)
    return nums if nums else [1]


def _parse_order_export(ws):
    """Разпознава експортен файл на поръчки (колони Order No, Pos,
    Reference, Reference Desc, Open Qty — плюс произволни други,
    напр. Due Date/Project/Unit/Stock/Company, и номер на палетна карта в
    последната колона) и групира редовете по последната колона — всеки
    различен номер там става отделна палетна карта. Ако последната колона
    съдържа няколко номера, разделени с "+" (напр. "1+3+4"), редът се
    добавя КЪМ ВСЯКА от изброените карти (виж _parse_group_numbers) — за
    материал, физически наличен в повече от една карта.

    Пазят се САМО тези 5 колони (заявка: „палетна карта да зарежда само
    информацията от следните колони и да съдържа само тях... Order No,
    Pos, Reference, Reference Desc, Open Qty“) — всички останали колони на
    файла (Due Date, Project, Unit, Stock и т.н.) се игнорират нарочно,
    дори да присъстват. Липсваща/неразпозната от тези 5 колона просто
    остава празна за съответното поле, не се попълва с друга стойност.
    Връща {номер: [items]} подредени по реда на поява, или None ако
    форматът не е разпознат."""
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

    def cell_has_value(row, i):
        """Дали клетка i от този ред е реално попълнена (не None/празен
        низ след трим) — ползва се само за откриване на групиращата
        колона (находка №6), затова минималната проверка е достатъчна."""
        if row is None or i is None or i >= len(row):
            return False
        return _cellstr(row[i]) != ""

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
    #
    # Одит (12.08.2026, находка №6, high): преди тази поправка тук се
    # вземаше просто НАЙ-ДЯСНАТА колона с празно заглавие, по ПОЗИЦИЯ —
    # без изобщо да се провери дали тя реално съдържа данни. Чест остатък
    # при износ от Excel е ОЩЕ една напълно празна колона след истинската
    # групираща (напр. неизползван диапазон, включен в експорта) — в този
    # случай кодът избираше именно нея (истински празна навсякъде),
    # `cell(row, group_col)` връщаше "" за всеки ред, `_parse_group_numbers`
    # падаше към подразбиращото се [1] за всеки ред — ВСИЧКИ редове
    # погрешно попадаха в карта №1, вместо да се разпределят по палети.
    # Сега сред колоните с празно заглавие се търси (от дясно наляво,
    # запазвайки предпочитанието към последната) ПЪРВАТА, която реално
    # съдържа поне една непразна стойност в данните — истински празните
    # колони отдясно на нея се прескачат.
    candidates = [i for i in range(len(header) - 1, -1, -1) if header[i] == ""]
    group_col = None
    for i in candidates:
        if any(cell_has_value(row, i) for row in rows[1:]):
            group_col = i
            break
    if group_col is None and candidates:
        # Нито една от безименните колони не съдържа данни (напр. файл без
        # реална групираща колона, но с празен остатъчен диапазон) — пази
        # старото поведение (последната безименна) вместо да гърми.
        group_col = candidates[0]
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
        item = {
            "order_no": order_no,
            "pos": cell(row, col_pos),
            "reference": cell(row, col_ref),
            "reference_desc": cell(row, col_ref_desc),
            "qty": cell(row, col_qty),
        }
        # Един и същ ред може да принадлежи на няколко карти наведнъж
        # ("1+3" и т.н.) — добавяме СЪЩИЯ артикул към всяка от тях (не
        # копие — общите редакции по-нататък не мутират тези речници).
        for group in _parse_group_numbers(group_raw):
            groups.setdefault(group, []).append(item)
    return groups if groups else None


@login_required
def pallet_bulk_import():
    """Импорт от справка за поръчки (Order No, Pos, Reference, Reference
    Desc, Open Qty — само тези колони се зареждат, останалите от файла се
    игнорират, вижте _parse_order_export) — редовете се разделят
    автоматично в отделни палетни карти по последната колона на файла
    (номер на палет)."""
    from openpyxl import load_workbook

    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash(_("Моля, изберете Excel файл (.xlsx)."), "error")
        return redirect(url_for("pallet_new"))
    try:
        wb = load_workbook(io.BytesIO(file.read()), data_only=True)
    except Exception:
        applog.log_exception("routes_pallet_extra: неуспешно четене на качен .xlsx файл")
        flash(_("Файлът не може да бъде прочетен. Уверете се, че е валиден .xlsx файл."), "error")
        return redirect(url_for("pallet_new"))

    groups = _parse_order_export(wb.worksheets[0])
    if not groups:
        flash(_("Файлът не съдържа разпознаваеми колони (Order No, Pos, Reference, "
             "Reference Desc, Open Qty) или редове за импорт."), "error")
        return redirect(url_for("pallet_new"))

    con = get_db()
    clients = load_clients(con)
    settings = db.get_settings(con)
    ordered = sorted(groups.items())
    flash(_("Открити са %d палетни карти (%d реда общо) от „%s“. Прегледайте и издайте.") %
          (len(ordered), sum(len(v) for _, v in ordered), file.filename), "success")
    return render_template("pallet_bulk_review.html", clients=clients,
                           clients_json=clients_json(clients), s=settings,
                           groups=ordered)


def _collect_bulk_pallet_drafts():
    """Чете подадените от прегледа за bulk импорт полета (общи за
    партидата + поотделно за всеки палет — тип, вид опаковка, бруто,
    височина) и връща списък от речници с данните за всяка карта, БЕЗ да
    ги записва в базата. Ползва се от ТРИ различни екрана: (1) прегледа за
    bulk импорт от справка за поръчки (pallet_bulk_review.html — редовете
    са във формат „orders“), (2) неговия предварителен преглед (без
    запис), и (3) ръчния композитор за няколко карти наведнъж на самата
    pallet_form.html (виж initPalletMultiCard в static/app.js — от заявката
    „палетна карта да съдържа в съдържание на палета Order No, Pos,
    Reference, Reference Desc, Qty“ и той е с колоните на формат „orders“;
    старият формат код/описание/кол./тегло се среща само при редакция на
    вече издадени стари карти).
    „Общ брой“ НЕ е сред тях — изчислява се на момента от items (виж
    appcore.pallet_total_qty), не се пази като отделно подадено поле."""
    shared_fields = ("sender_name", "sender_city", "client_name", "client_address",
                     "client_city", "client_country", "doc_date", "ref_cmr", "notes")
    shared = {k: request.form.get(k, "").strip() for k in shared_fields}
    per_card_fields = ("pallet_type", "packaging_type", "gross", "height")
    group_ids = [g for g in request.form.get("groups", "").split(",") if g.strip()]

    item_check_fields = ("due_date", "order_no", "pos", "project", "reference",
                         "reference_desc", "qty", "unit", "stock", "code", "description", "weight")
    drafts = []
    for g in group_ids:
        raw = request.form.get("items_json_%s" % g, "[]")
        try:
            items = json.loads(raw)
        except ValueError:
            items = []
        items = [it for it in items if isinstance(it, dict) and
                 any((it.get(k) or "").strip() if isinstance(it.get(k), str) else it.get(k)
                     for k in item_check_fields)]
        if not items:
            continue
        data = dict(shared)
        # items_format идва от самия екран — и pallet_bulk_review.html, и
        # ръчният композитор на pallet_form.html вече подават "orders"
        # (виж заявката в docstring-а по-горе). Липсващо поле пада към
        # "orders" — поведението на bulk-review преглед/Excel импорт
        # остава напълно непроменено.
        fmt = request.form.get("items_format_%s" % g, "orders").strip()
        data["items_format"] = fmt or "orders"
        for f in per_card_fields:
            data[f] = request.form.get("%s_%s" % (f, g), "").strip()
        data["items"] = items
        drafts.append(data)

    # Одит (находка С3, среден риск): преди поправката номерът се пресмяташе
    # ТУК, вътре в цикъла, като "%s от %s" % (g, len(group_ids)) — g е
    # номерът на групата ОТ ФАЙЛА (напр. 3, 4, 5 при палети, продължаващи
    # номерацията от предишна пратка), а len(group_ids) броеше ВСИЧКИ
    # подадени групи, включително тези, филтрирани по-горе като напълно
    # празни (if not items: continue). Резултат: файл с групи 3/4/5 даваше
    # карти „3 от 3“/„4 от 3“/„5 от 3“ вместо очакваното „1 от 3“/„2 от
    # 3“/„3 от 3“; а изчистена (празна) група сред тях правеше знаменателя
    # погрешно голям — „1 от 3“ и „3 от 3“ за само 2 РЕАЛНО издадени карти.
    # Номерът се печата на самата карта и отива при клиента, затова трябва
    # да отразява РЕАЛНАТА позиция сред РЕАЛНО издадените карти — броим
    # последователно (1, 2, 3, ...) само върху `drafts` (вече филтрирания
    # списък), а не върху суровия `group_ids`/номера на групата от файла.
    total = len(drafts)
    for idx, data in enumerate(drafts, start=1):
        # Дребни (одит): гол низ без _() — при интерфейс на EN/TR
        # печатната карта показваше „от“ независимо от избрания език.
        data["pallet_no"] = _("%s от %s") % (idx, total)
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
        flash(_("Няма палетни карти за преглед (всички редове са празни)."), "warning")
        return redirect(url_for("pallet_new"))
    token = _store_preview("bulk_pallet", drafts)
    return redirect(url_for("pallet_bulk_preview_view", token=token))


@login_required
def pallet_bulk_preview_view(token):
    drafts = _get_preview(token, "bulk_pallet")
    if drafts is None:
        flash(_("Прегледът е изтекъл или вече е използван — заредете файла отново."), "warning")
        return redirect(url_for("pallet_new"))
    return render_template("pallet_bulk_preview.html", drafts=drafts)


@login_required
def pallet_bulk_issue():
    """Издава наведнъж всички палетни карти от прегледа за импорт от
    справка за поръчки (или от ръчния композитор на pallet_form.html).
    Изпращач/клиент/дата/бележки са общи за цялата партида, но размерите
    и теглото на всеки палет (тип, вид опаковка, бруто, височина) се
    задават и записват отделно за всяка карта."""
    drafts = _collect_bulk_pallet_drafts()
    if not drafts:
        flash(_("Няма палетни карти за издаване (всички редове са празни)."), "warning")
        return redirect(url_for("pallet_new"))

    con = get_db()
    # Одит (находка В14, висок риск): преди поправката всеки save_document
    # тук commit-ваше ОТДЕЛНО (подразбиращото се поведение) — грешка по
    # средата на партида (напр. db.next_number блокирана от друг
    # едновременен процес, или неочакван ValueError в данните на конкретна
    # карта) оставяше ЧАСТ от партидата трайно записана в базата, а
    # останалата — изгубена, без ясен начин операторът да разбере кои
    # номера реално са издадени. Тук цялата партида е ЕДНА транзакция
    # (commit=False на всеки save_document + един общ commit/rollback накрая)
    # — или всички карти от партидата се записват, или НИТО ЕДНА.
    # Одит (находка В14, висок риск): преди поправката всеки save_document
    # тук commit-ваше ОТДЕЛНО (подразбиращото се поведение) — грешка по
    # средата на партида (напр. db.next_number блокирана от друг
    # едновременен процес, или неочакван ValueError в данните на конкретна
    # карта) оставяше ЧАСТ от партидата трайно записана в базата, а
    # останалата — изгубена, без ясен начин операторът да разбере кои
    # номера реално са издадени. Тук цялата партида е ЕДНА транзакция
    # (commit=False на всеки save_document + един общ commit/rollback накрая)
    # — или всички карти от партидата се записват, или НИТО ЕДНА.
    created = []
    try:
        for data in drafts:
            doc_id = save_document(con, "pallet", data, commit=False)
            created.append((data["number"], doc_id))
        con.commit()
    except Exception:
        con.rollback()
        applog.log_exception(
            "routes_pallet_extra.pallet_bulk_issue: грешка по средата на партида — "
            "цялата партида е върната назад (rollback), нищо не е записано")
        flash(_("Възникна грешка при масовото издаване — нищо не бе записано "
               "(партидата е отменена изцяло, за да не останат частично издадени "
               "карти). Опитайте отново."), "error")
        return redirect(url_for("pallet_new"))

    flash(_("Издадени и запазени %d палетни карти: %s") %
         (len(created), ", ".join(num for num, _ in created)), "success")
    return redirect(url_for("pallet_bulk_result",
                            ids=",".join(str(doc_id) for _, doc_id in created)))


def _fetch_pallet_docs_by_ids(con, ids_param):
    """Общо за pallet_bulk_result/pallet_bulk_print — чете ?ids=1,2,3 и
    връща списък от (doc_row, data) двойки, СЪЩАТА заявка и на двете
    места, за да не се разминат при бъдеща промяна."""
    ids = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
    docs = []
    for doc_id in ids:
        row = con.execute(
            "SELECT d.*, u.full_name AS author FROM documents d"
            " LEFT JOIN users u ON u.id = d.created_by WHERE d.id = ?",
            (doc_id,),
        ).fetchone()
        if row is not None:
            docs.append((row, safe_json_data(row["data"])))
    return docs


@login_required
def pallet_bulk_result():
    """Преглед на току-що издадените палетни карти преди печат — списък с
    бърз линк към всяка (за проверка поотделно), плюс бутон за печат на
    всички наведнъж (pallet_bulk_print)."""
    ids_param = request.args.get("ids", "")
    docs = _fetch_pallet_docs_by_ids(get_db(), ids_param)
    return render_template("pallet_bulk_result.html", docs=docs, ids_param=ids_param)


@login_required
def pallet_bulk_print():
    """Печат на няколко вече издадени палетни карти наведнъж, в ЕДИН
    документ (browser print обхваща всички едновременно) — заявка:
    „запазят картите да може да се принтират директно всичкия брой
    карти“, вместо да се отваря и печата всяка карта поотделно."""
    ids_param = request.args.get("ids", "")
    docs = _fetch_pallet_docs_by_ids(get_db(), ids_param)
    if not docs:
        flash(_("Няма намерени документи за печат."), "warning")
        return redirect(url_for("pallet_bulk_result", ids=ids_param))
    return render_template("pallet_bulk_print.html", docs=docs, ids_str=ids_param)
