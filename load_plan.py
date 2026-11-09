# -*- coding: utf-8 -*-
"""Заявка (09.11.2026): „подредба на палети в камион/тир — и визуално да има“.

Подредба на палетите по пода на камиона/ремаркето според размерите им,
теглото и дали могат да се стифират, с чертеж отгоре и отстрани (SVG от
сървъра — същият чертеж и на екрана, и на печат). Нищо не се записва в базата:
страницата /load-plan е калкулатор с параметри в адреса (за печат/връзка), а
от опаковъчен лист, палетна карта или ЧМР бутонът „Подредба в камиона“ отваря
калкулатора с попълнените палети на пратката.

Правила на подредбата (алгоритъм „по редове“ — както се товари тир):
1. Стифиране: палет се слага върху друг само ако и двата позволяват още етаж
   (стифиране 2/3), отпечатъкът им е един и същ, височината на купа не
   надхвърля вътрешната височина и горният е НЕ по-тежък от долния.
2. Купове (стекове) се подреждат от челото (кабината) към вратите — първо
   най-големите и най-тежките; всеки нов ред взема ориентацията с най-малко
   изразходвана дължина на палет (EUR 1200×800 → 3 напряко → 33 в 13,6 м).
   Празното място в края на ред се запълва с по-малки купове, ако се събират.
3. Палет, който не се побира (по-висок/по-широк от каросерията или няма
   място), се показва в „Не се побират“.
Резултатът: товарни метри (заета дължина), използване на пода/обема/товара,
център на тежестта по дължина, ред на товарене (№ 1 се товари първи)."""
import math
from urllib.parse import urlencode

from flask import abort, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_babel import gettext as _dyn  # превод на готов msgid (не се извлича от pybabel)

import applog
import db
import shipment
from appcore import N_, _parse_decimal, fetch_document, get_db, login_required, safe_json_data

#: Вътрешни размери (мм) и полезен товар (кг) — типични стойности; при
#: нужда се избира „Друг (въведи размери)“.
VEHICLES = (
    ("tent", N_("Тир 13,6 м (тент)"), 13600, 2480, 2700, 24000),
    ("mega", N_("Мега ремарке 13,6 м"), 13600, 2480, 3000, 24000),
    ("solo", N_("Соло камион 7,2 м"), 7200, 2450, 2500, 7000),
    ("van", N_("Бус 4,2 м (до 3,5 т)"), 4200, 2100, 2200, 1100),
    ("c20", N_("Контейнер 20'"), 5898, 2352, 2393, 28000),
    ("c40", N_("Контейнер 40'"), 12032, 2352, 2393, 26700),
    ("c40hc", N_("Контейнер 40' HC"), 12032, 2352, 2698, 26500),
)
VEHICLE_DEFAULT = "tent"
CUSTOM = "custom"
MAX_LINES = 60
MAX_PALLETS = 300
MAX_STACK = 3
#: Граници за въведените размери (мм) и тегло (кг) — пазят от безсмислици.
DIM_MAX = 20000
WEIGHT_MAX = 100000
#: Цветове на редовете (светли пълнежи с тъмен контур — четат се и на
#: тъмна тема, защото чертежът е върху бял „лист“, и на черно-бял печат).
COLORS = ("#cfe3ff", "#ffe1b8", "#d4f2d2", "#f6d0e4", "#e3dcff", "#fff3a8", "#c9efef", "#f4d6c8")
STROKES = ("#2f5f9e", "#9a5a10", "#2f7a2b", "#9b2f66", "#5b47a8", "#8a7400", "#22777a", "#8f4a2c")
PRESETS = (("eur", "EUR 1200×800", 1200, 800), ("ind", N_("Индустриален 1200×1000"), 1200, 1000),
           ("half", N_("Половин EUR 800×600"), 800, 600))


def _num(value):
    """Неотрицателно число от текст („1 200“, „4,5“) или None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        parsed = _parse_decimal(value)
        if parsed is None:
            return None
        number = float(parsed)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def vehicle_choices():
    return [(key, _dyn(label)) for key, label, *_rest in VEHICLES] + [(CUSTOM, _("Друг (въведи размери)"))]


def resolve_vehicle(args):
    """Избраното превозно средство → речник с размерите (мм) и товара (кг)."""
    key = (args.get("v") or VEHICLE_DEFAULT).strip()
    for k, label, length, width, height, payload in VEHICLES:
        if k == key:
            return {"key": k, "label": _dyn(label), "length": length, "width": width,
                    "height": height, "payload": payload, "custom": False}
    base = next(v for v in VEHICLES if v[0] == VEHICLE_DEFAULT)

    def dim(name, default, top):
        number = _num(args.get(name))
        return int(number) if number and number <= top else default
    return {"key": CUSTOM, "label": _("Друг (въведи размери)"),
            "length": dim("vl", base[2], DIM_MAX), "width": dim("vw", base[3], DIM_MAX),
            "height": dim("vh", base[4], DIM_MAX), "payload": dim("vkg", base[5], WEIGHT_MAX),
            "custom": True}


def parse_lines(args):
    """Редовете на формата (параметри d, q, l, w, h, kg, st — по един на ред)
    → списък с речници; ред без размери или брой се пропуска, но се пази за
    формата (поле `ok`)."""
    def col(name):
        return args.getlist(name) if hasattr(args, "getlist") else list(args.get(name) or [])
    names = ("d", "q", "l", "w", "h", "kg", "st")
    cols = {n: col(n) for n in names}
    count = min(max((len(v) for v in cols.values()), default=0), MAX_LINES)
    lines = []
    for i in range(count):
        raw = {n: (cols[n][i] if i < len(cols[n]) else "") for n in names}
        qty = _num(raw["q"])
        length, width = _num(raw["l"]), _num(raw["w"])
        height, weight = _num(raw["h"]), _num(raw["kg"])
        stack = _num(raw["st"]) or 1
        line = {
            "desc": (raw["d"] or "").strip()[:80],
            "qty": int(qty) if qty is not None and qty == int(qty) else (qty or 0),
            "length": int(length) if length and length <= DIM_MAX else 0,
            "width": int(width) if width and width <= DIM_MAX else 0,
            "height": int(height) if height and height <= DIM_MAX else 0,
            "weight": round(weight, 1) if weight is not None and weight <= WEIGHT_MAX else 0,
            "stack": int(min(max(stack, 1), MAX_STACK)),
            "raw": raw,
        }
        line["ok"] = bool(line["length"] and line["width"] and isinstance(line["qty"], int)
                          and line["qty"] > 0)
        if not any((raw[n] or "").strip() for n in names if n != "st"):
            continue  # напълно празен ред на формата
        lines.append(line)
    return lines


def _expand(lines):
    """Редовете → отделни палети (с номер на реда за цвета/легендата)."""
    pallets = []
    for group, line in enumerate(lines):
        if not line["ok"]:
            continue
        for n in range(line["qty"]):
            if len(pallets) >= MAX_PALLETS:
                return pallets, True
            pallets.append({"group": group, "n": n + 1, "desc": line["desc"],
                            "l": line["length"], "w": line["width"], "h": line["height"],
                            "kg": line["weight"], "stack": line["stack"]})
    return pallets, False


def _footprint(p):
    return (max(p["l"], p["w"]), min(p["l"], p["w"]))


def build_stacks(pallets, max_height):
    """Купове от палетите (правило 1 от докстринга на модула)."""
    order = sorted(pallets, key=lambda p: (-p["l"] * p["w"], -p["kg"], p["group"], p["n"]))
    stacks = []
    for p in order:
        target = None
        if p["stack"] > 1 and p["h"]:
            for s in stacks:
                top = s[-1]
                levels = min(x["stack"] for x in s + [p])
                if (_footprint(s[0]) == _footprint(p) and len(s) < levels
                        and all(x["h"] for x in s)
                        and sum(x["h"] for x in s) + p["h"] <= max_height
                        and p["kg"] <= top["kg"]):
                    target = s
                    break
        if target is not None:
            target.append(p)
        else:
            stacks.append([p])
    return stacks


def _orientations(stack, vehicle):
    """(по дължина, напряко) — двете ориентации, които се събират в каросерията."""
    base = stack[0]
    options = {(base["l"], base["w"]), (base["w"], base["l"])}
    return [o for o in options if o[0] <= vehicle["length"] and o[1] <= vehicle["width"]]


def _row_choice(options, width):
    """Ориентацията за нов ред: най-малко дължина на куп; при равенство —
    повече напряко (EUR: 3 напряко, не 2)."""
    def cost(o):
        across = max(width // o[1], 1)
        return (o[0] / across, -across)
    return min(options, key=cost)


def place(stacks, vehicle):
    """Подрежда куповете по редове (правило 2). Връща (поставени, непоставени)."""
    order = sorted(stacks, key=lambda s: (-_footprint(s[0])[0] * _footprint(s[0])[1],
                                          -sum(p["kg"] for p in s), s[0]["group"], s[0]["n"]))
    rows = []
    placed, unplaced = [], []
    for stack in order:
        height = sum(p["h"] for p in stack)
        options = _orientations(stack, vehicle)
        if not options or height > vehicle["height"]:
            unplaced.append((stack, "size"))
            continue
        spot = None
        for row in rows:
            fitting = [o for o in options if o[0] <= row["depth"] and row["used"] + o[1] <= vehicle["width"]]
            if fitting:
                o = max(fitting, key=lambda o: (o[0], -o[1]))  # най-малко празно по дължина
                spot = (row, o)
                break
        if spot is None:
            start = sum(r["depth"] for r in rows)
            o = _row_choice(options, vehicle["width"])
            if start + o[0] > vehicle["length"]:
                other = [x for x in options if start + x[0] <= vehicle["length"]]
                if not other:
                    unplaced.append((stack, "space"))
                    continue
                o = _row_choice(other, vehicle["width"])
            row = {"x": start, "depth": o[0], "used": 0}
            rows.append(row)
            spot = (row, o)
        row, o = spot
        placed.append({"stack": stack, "x": row["x"], "y": row["used"], "along": o[0], "across": o[1],
                       "height": height, "kg": sum(p["kg"] for p in stack)})
        row["used"] += o[1]
    placed.sort(key=lambda it: (it["x"], it["y"]))
    for i, item in enumerate(placed, 1):
        item["no"] = i
    return placed, unplaced


def plan(lines, vehicle):
    """Пълният резултат за шаблона."""
    pallets, capped = _expand(lines)
    stacks = build_stacks(pallets, vehicle["height"])
    placed, unplaced = place(stacks, vehicle)
    total_kg = sum(p["kg"] for p in pallets)
    placed_kg = sum(it["kg"] for it in placed)
    used_len = max((it["x"] + it["along"] for it in placed), default=0)
    floor = sum(it["along"] * it["across"] for it in placed)
    volume = sum(p["l"] * p["w"] * p["h"] for it in placed for p in it["stack"])
    cog = (sum(it["kg"] * (it["x"] + it["along"] / 2) for it in placed) / placed_kg) if placed_kg else None
    v_floor = vehicle["length"] * vehicle["width"]
    v_volume = v_floor * vehicle["height"]
    warnings = []
    if capped:
        warnings.append(_("Показани са първите %(n)d палета.") % {"n": MAX_PALLETS})
    if unplaced:
        warnings.append(_("%(n)d палета не се побират — виж „Не се побират“.")
                        % {"n": sum(len(s) for s, _r in unplaced)})
    if placed_kg > vehicle["payload"]:
        warnings.append(_("Товарът (%(kg)s кг) надвишава полезния товар на превозното средство (%(max)s кг).")
                        % {"kg": fmt(placed_kg, 0), "max": fmt(vehicle["payload"], 0)})
    if pallets and any(not p["h"] for p in pallets):
        warnings.append(_("Палети без височина не се стифират и не влизат в обема."))
    if pallets and any(not p["kg"] for p in pallets):
        warnings.append(_("Палети без тегло не влизат в сбора и в центъра на тежестта."))
    if cog is not None and used_len and placed_kg > vehicle["payload"] * 0.5:
        share = cog / vehicle["length"]
        if share < 0.3 or share > 0.7:
            warnings.append(_("Центърът на тежестта е далеч от средата на каросерията — "
                              "разпределете тежките палети по дължината (натоварване на осите)."))
    legend = []
    for group, line in enumerate(lines):
        if not line["ok"]:
            continue
        legend.append({"group": group, "desc": line["desc"], "qty": line["qty"],
                       "dims": "%d×%d×%s" % (line["length"], line["width"], line["height"] or "—"),
                       "kg": line["weight"], "stack": line["stack"],
                       "fill": COLORS[group % len(COLORS)], "stroke": STROKES[group % len(STROKES)]})
    return {
        "vehicle": vehicle, "pallets": len(pallets), "stacks": len(placed), "placed": placed,
        "unplaced": [{"stack": s, "reason": r} for s, r in unplaced],
        "placed_pallets": sum(len(it["stack"]) for it in placed),
        "total_kg": total_kg, "placed_kg": placed_kg,
        "used_length": used_len, "ldm": used_len / 1000.0 if used_len else 0.0,
        "floor_pct": round(floor * 100.0 / v_floor) if v_floor else 0,
        "length_pct": round(used_len * 100.0 / vehicle["length"]) if vehicle["length"] else 0,
        "volume_pct": round(volume * 100.0 / v_volume) if v_volume else 0,
        "weight_pct": round(placed_kg * 100.0 / vehicle["payload"]) if vehicle["payload"] else 0,
        "cog": cog, "warnings": warnings, "legend": legend,
    }


def fmt(value, places=2):
    """Число за екрана с десетична запетая и интервал за хилядите."""
    if value is None:
        return ""
    text = "{:,.{p}f}".format(float(value), p=places).replace(",", " ").replace(".", ",")
    return text


def svg_views(result):
    """Данни за чертежите (в мм — мащабът е във viewBox на шаблона)."""
    v = result["vehicle"]
    boxes = []
    for it in result["placed"]:
        base = it["stack"][0]
        levels = []
        z = 0
        for p in it["stack"]:
            levels.append({"z": z, "h": p["h"] or 0, "group": p["group"],
                           "fill": COLORS[p["group"] % len(COLORS)],
                           "stroke": STROKES[p["group"] % len(STROKES)]})
            z += p["h"] or 0
        boxes.append({"no": it["no"], "x": it["x"], "y": it["y"], "l": it["along"], "w": it["across"],
                      "height": it["height"], "levels": levels, "count": len(it["stack"]),
                      "kg": it["kg"], "fill": COLORS[base["group"] % len(COLORS)],
                      "stroke": STROKES[base["group"] % len(STROKES)],
                      "desc": " / ".join(dict.fromkeys(p["desc"] for p in it["stack"] if p["desc"]))})
    return {"boxes": boxes, "L": v["length"], "W": v["width"], "H": v["height"],
            "font": max(min(v["width"], v["length"]) / 14.0, 90)}


# ---------------------------------------------------------------- от документ

def _pallet_dims(data):
    import routes_pallet_extra  # pylint: disable=import-outside-toplevel
    return routes_pallet_extra._pallet_dims_mm(data)  # pylint: disable=protected-access


def _packing_lines(data):
    """Редовете на опаковъчния лист с размери — всеки ред е един колет/палет
    (както при изтеглянето от палетни карти); еднаквите редове се сливат."""
    merged = {}
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        length, width = _num(it.get("length")), _num(it.get("width"))
        if not length or not width:
            continue
        desc = str(it.get("packing") or it.get("description") or "").strip()[:40]
        key = (desc, int(length), int(width), int(_num(it.get("height")) or 0), _num(it.get("gross")) or 0)
        merged[key] = merged.get(key, 0) + 1
    return [{"d": k[0], "q": n, "l": k[1], "w": k[2], "h": k[3] or "", "kg": _plain(k[4]), "st": 1}
            for k, n in merged.items()]


def _pallet_card_lines(datas):
    merged = {}
    for data in datas:
        dims = _pallet_dims(data)
        if not dims.get("length"):
            continue
        desc = str(data.get("pallet_type") or "").strip()[:40]
        key = (desc, int(dims["length"]), int(dims["width"]), int(dims.get("height") or 0),
               _num(data.get("gross")) or 0)
        merged[key] = merged.get(key, 0) + 1
    return [{"d": k[0], "q": n, "l": k[1], "w": k[2], "h": k[3] or "", "kg": _plain(k[4]), "st": 1}
            for k, n in merged.items()]


def _cmr_ldm_lines(data):
    """Редовете на калкулатора LDM в ЧМР (CUSTOMS) — без височина и тегло."""
    import customs  # pylint: disable=import-outside-toplevel
    out = []
    for line in customs.parse_ldm_lines(data.get("ldm_lines")):
        kind = str(line.get("type") or "custom").lower()
        if kind in customs.PALLET_TYPES:
            length, width = customs.PALLET_TYPES[kind]
        else:
            length, width = _num(line.get("length")), _num(line.get("width"))
        qty = _num(line.get("qty"))
        if length and width and qty:
            out.append({"d": kind.upper() if kind != "custom" else "", "q": int(qty), "l": int(length),
                        "w": int(width), "h": "", "kg": "", "st": int(_num(line.get("stack")) or 1)})
    return out


def _plain(number):
    if not number:
        return ""
    return ("%g" % number).replace(".", ",")


def lines_for_document(con, row):
    """Палетите на пратката на документа: опаковъчен лист → палетни карти →
    редовете LDM в ЧМР (първото, което има размери)."""
    data = safe_json_data(row["data"])
    if row["doc_type"] == "packing":
        return _packing_lines(data)
    if row["doc_type"] == "pallet":
        # Картите със същото PO и клиент (както „Опаковъчен лист от тази поръчка“).
        import routes_pallet_extra  # pylint: disable=import-outside-toplevel
        same, _capped = routes_pallet_extra.same_po_pallet_docs(con, row, data)
        lines = _pallet_card_lines(d for _r, d in same) if len(same) > 1 else []
        if lines:
            return lines
    related = []
    try:
        related = shipment.related_documents(con, row, data)
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("load_plan: свързаните документи не се прочетоха")
    strong = [r for r in related if r.get("checked")]
    packing = next((r for r in strong if r["doc_type"] == "packing"), None)
    if packing:
        prow = con.execute("SELECT data FROM documents WHERE id = ?", (packing["id"],)).fetchone()
        lines = _packing_lines(safe_json_data(prow["data"])) if prow else []
        if lines:
            return lines
    ids = [i for r in strong if r["doc_type"] == "pallet" for i in r["ids"]]
    if row["doc_type"] == "pallet" and row["id"] not in ids:
        ids.insert(0, row["id"])
    if ids:
        marks = ",".join("?" * len(ids))
        rows = con.execute("SELECT data FROM documents WHERE id IN (%s)" % marks, ids).fetchall()  # nosec B608 -- само ? плейсхолдъри
        lines = _pallet_card_lines(safe_json_data(r["data"]) for r in rows)
        if lines:
            return lines
    if row["doc_type"] == "cmr":
        return _cmr_ldm_lines(data)
    return []


def lines_to_args(lines):
    pairs = []
    for line in lines[:MAX_LINES]:
        for key in ("d", "q", "l", "w", "h", "kg", "st"):
            pairs.append((key, line.get(key, "")))
    return pairs


# ---------------------------------------------------------------- маршрути

@login_required
def load_plan_page():
    vehicle = resolve_vehicle(request.args)
    lines = parse_lines(request.args)
    result = plan(lines, vehicle) if any(line["ok"] for line in lines) else None
    src = None
    src_id = request.args.get("src", type=int)
    if src_id:
        row = get_db().execute("SELECT id, doc_type, number FROM documents WHERE id = ?", (src_id,)).fetchone()
        if row:
            src = {"id": row["id"], "number": row["number"],
                   "title": _(db.DOC_TYPES.get(row["doc_type"], {}).get("title", ""))}  # noqa
    form_lines = [line["raw"] for line in lines] or [
        {"d": "", "q": "", "l": "1200", "w": "800", "h": "", "kg": "", "st": "1"}]
    return render_template("load_plan.html", vehicle=vehicle, vehicles=vehicle_choices(),
                           lines=lines, form_lines=form_lines, result=result,
                           views=svg_views(result) if result else None, presets=PRESETS,
                           src=src, fmt=fmt, max_lines=MAX_LINES, max_stack=MAX_STACK)


@login_required
def load_plan_from_document(doc_id):
    row, _data = fetch_document(get_db(), doc_id)
    if row["doc_type"] not in ("packing", "pallet", "cmr"):
        abort(404)
    lines = lines_for_document(get_db(), row)
    args = [("src", row["id"])]
    if row["doc_type"] == "cmr":
        args.append(("v", VEHICLE_DEFAULT))
    args += lines_to_args(lines)
    return redirect(url_for("load_plan") + "?" + urlencode(args))


def register(app):
    app.add_url_rule("/load-plan", "load_plan", load_plan_page)
    app.add_url_rule("/doc/<int:doc_id>/load-plan", "load_plan_from_document", load_plan_from_document)
    app.add_template_global(lambda doc_type: doc_type in ("packing", "pallet", "cmr"),
                            name="load_plan_supported")
