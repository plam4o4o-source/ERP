# -*- coding: utf-8 -*-
"""Заявка (09.11.2026): „подредба на палети в камион/тир — и визуално да има“.
Заявка (11.11.2026): „подобри…, анализирай, провери информация, не си измисля,
и графично подобри“ — стойностите са проверени (docs/ПОДРЕДБА_В_КАМИОНА_ИЗТОЧНИЦИ.md).

Подредба на палетите по пода на камиона/ремаркето/контейнера според размерите,
теглото и стифирането, с 3D изглед, изглед отгоре и тегло по дължината (SVG
от сървъра — същият чертеж на екрана и на печат). Нищо не се записва в базата:
страницата /load-plan е калкулатор с параметри в адреса, а от опаковъчен лист,
палетна карта или ЧМР бутонът „Подредба в камиона“ отваря калкулатора с
палетите на пратката.

Правила:
1. Стифиране: палет върху друг само ако и двата позволяват още етаж, размерът
   е еднакъв, купът се побира на височина, горният НЕ е по-тежък и (за EPAL
   размерите) товарът върху долния палет е в границата на EPAL.
2. Подредба: пробват се две стратегии — „по редове“ (както се товари тир) и
   „свободно място“ (MaxRects, най-близо до челото, смесва надлъжни и
   напречни палети — нужно в контейнер 2 352 мм) — с няколко подредби на
   куповете; избира се най-много палети, после най-малка заета дължина.
3. Контейнер: всеки палет трябва да мине през вратата.
4. Предупреждения/бележки само по източник: полезен товар, безопасен товар на
   EPAL палета, „на ръба“ (под 50 мм свободна дължина), над 60 % от товара в
   едната половина на контейнер (EU насоки), празно напряко над 8 см (DVSA).
Резултатът: товарни метри (LDM = площ / 2,4 м) и заета дължина, обемно тегло,
зает под/обем, тегло, център на тежестта, ред на товарене (№ 1 — първи)."""
import math
from urllib.parse import urlencode

from flask import abort, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_babel import gettext as _dyn  # превод на готов msgid (не се извлича от pybabel)

import applog
import db
import shipment
from appcore import N_, _parse_decimal, fetch_document, get_db, login_required, safe_json_data

#: Вътрешни размери (мм), полезен товар (кг), отвор на вратата (мм, само при
#: контейнерите — палетът трябва да мине през нея) и типичният брой палети по
#: източник. Заявка (11.11.2026, „провери информацията, не си измисляй“) —
#: източниците са в docs/ПОДРЕДБА_В_КАМИОНА_ИЗТОЧНИЦИ.md:
#: тир/мега — LKW Walter (13 620 × 2 480 × 2 700 / 3 000), ~24 т при 40 т
#: композиция; C 7.45 — Schmitz Cargobull W.BO (7 290 × 2 470 × 2 517), ~12 т;
#: бус 3,5 т — 4 200 × 2 100 × 2 230, 900–1 200 кг (дилърски данни);
#: контейнери — Hapag-Lloyd; брой палети в контейнер — GDV/TIS.
VEHICLES = (
    ("tent", N_("Тир 13,6 м (тент)"), 13620, 2480, 2700, 24000, None, None,
     N_("Типично 33 EUR или 26 индустриални палета; 34 EUR напречно — само с 20 мм луфт.")),
    ("mega", N_("Мега ремарке 13,6 м"), 13620, 2480, 3000, 24000, None, None,
     N_("Като тира, но с 3 м вътрешна височина.")),
    ("swap745", N_("Сменяема каросерия C 7.45 (~7,3 м)"), 7290, 2470, 2517, 12000, None, None,
     N_("Около 18 EUR палета; полезният товар зависи от камиона — проверете в талона.")),
    ("van", N_("Бус до 3,5 т (8 EUR)"), 4200, 2100, 2230, 1000, None, None,
     N_("Полезният товар на бусовете е 900–1 200 кг — проверете в талона.")),
    ("c20", N_("Контейнер 20'"), 5900, 2352, 2395, 28130, 2340, 2292,
     N_("Типично 11 EUR или 10 индустриални палета (GDV); врата 2 340 × 2 292 мм.")),
    ("c40", N_("Контейнер 40'"), 12032, 2352, 2395, 28750, 2340, 2292,
     N_("Типично 23 EUR или 20 индустриални палета (GDV), до 25 EUR без луфт; врата 2 340 × 2 292 мм.")),
    ("c40hc", N_("Контейнер 40' HC"), 12032, 2352, 2700, 28600, 2340, 2597,
     N_("Като 40', но с 2,70 м височина; врата 2 340 × 2 597 мм.")),
)
VEHICLE_DEFAULT = "tent"
CUSTOM = "custom"
MAX_LINES = 60
MAX_PALLETS = 300
MAX_STACK = 3
#: EPAL (продуктови листове): размер → (безопасен товар на палета, най-много
#: товар ОТГОРЕ върху долния палет при стифиране), кг. EPAL 1: 1 500 / 4 000
#: (долният ≤ 5 500 кг общо); EPAL 3 (1 200 × 1 000): 1 500 / 3 000 (≤ 4 500);
#: EPAL 6 (800 × 600): 500 / 1 500 (≤ 2 000).
EPAL_LIMITS = {(1200, 800): (1500, 4000), (1200, 1000): (1500, 3000), (800, 600): (500, 1500)}
#: Максимално празно място напряко без допълнително укрепване — UK DVSA
#: „positive fit“ (XL ремарке): до 8 см отстрани.
SIDE_GAP_MM = 80
#: Под тази свободна дължина подредбата е „на ръба“ (напр. 34 EUR напречно в
#: 13 620 мм оставят 20 мм) — само предупреждение, не забрана.
TIGHT_SLACK_MM = 50
#: EU Best Practice Guidelines (2014): в контейнер не повече от 60 % от
#: товара в едната половина по дължина.
CONTAINER_HALF_MAX = 0.6
#: Ширината, към която се свеждат товарните метри (LDM = площ / 2,4 м).
LDM_WIDTH_M = 2.4
#: Граници за въведените размери (мм) и тегло (кг) — пазят от безсмислици.
DIM_MAX = 20000
WEIGHT_MAX = 100000
#: Цветове на редовете — проверената категорийна палитра (дальтонизъм,
#: контраст; skill dataviz / validate_palette.js), в постоянен ред. Ред 9+ е
#: „други“ (сиво) — цветът никога не се повтаря; номерът и легендата носят
#: идентичността. Чертежът е върху бял „лист“ (и на печат).
PALETTE = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
OTHER_COLOR = "#8a8f98"
#: Дървената основа на палета на чертежите (височина на EUR палета — 144 мм).
PALLET_BASE_MM = 144
WOOD, WOOD_DARK = "#d9b98f", "#9a7650"


def group_color(group):
    return PALETTE[group] if 0 <= group < len(PALETTE) else OTHER_COLOR


def _mix(color, other, t):
    """Смесва два #rrggbb цвята (t=0 → color, t=1 → other)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


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
    for k, label, length, width, height, payload, door_w, door_h, note in VEHICLES:
        if k == key:
            return {"key": k, "label": _dyn(label), "length": length, "width": width,
                    "height": height, "payload": payload, "custom": False,
                    "door_w": door_w, "door_h": door_h, "note": _dyn(note),
                    "container": door_w is not None}
    base = next(v for v in VEHICLES if v[0] == VEHICLE_DEFAULT)

    def dim(name, default, top):
        number = _num(args.get(name))
        return int(number) if number and number <= top else default
    return {"key": CUSTOM, "label": _("Друг (въведи размери)"),
            "length": dim("vl", base[2], DIM_MAX), "width": dim("vw", base[3], DIM_MAX),
            "height": dim("vh", base[4], DIM_MAX), "payload": dim("vkg", base[5], WEIGHT_MAX),
            "custom": True, "door_w": None, "door_h": None, "note": "", "container": False}


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


def _stack_load_ok(stack, extra):
    """EPAL: товарът върху долния палет не надвишава допустимия при
    стифиране (EPAL_LIMITS); за други размери няма данни — не се ограничава."""
    limits = EPAL_LIMITS.get(_footprint(stack[0]))
    if not limits:
        return True
    return sum(x["kg"] for x in stack[1:]) + extra["kg"] <= limits[1]


def _through_door(stack, vehicle):
    """Контейнер: всеки палет минава през вратата (по-тясната страна напряко,
    височината на палета под отвора)."""
    if not vehicle.get("door_w"):
        return True
    return all(min(p["l"], p["w"]) <= vehicle["door_w"] and (p["h"] or 0) <= vehicle["door_h"]
               for p in stack)


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
                        and p["kg"] <= top["kg"]
                        and _stack_load_ok(s, p)):
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
    return sorted(o for o in options if o[0] <= vehicle["length"] and o[1] <= vehicle["width"])


def _row_choice(options, width):
    """Ориентацията за нов ред: най-малко дължина на куп; при равенство —
    повече напряко (EUR: 3 напряко, не 2)."""
    def cost(o):
        across = max(width // o[1], 1)
        return (o[0] / across, -across)
    return min(options, key=cost)


def _pack_rows(order, vehicle):
    """Стратегия „по редове“ — всеки ред е с една дълбочина; празното място в
    края на реда се запълва с по-малки купове. Връща [(куп, x, y, по дълж., напряко) | None]."""
    rows, out = [], []
    for stack in order:
        options = _orientations(stack, vehicle)
        spot = None
        for row in rows:
            fitting = [o for o in options if o[0] <= row["depth"] and row["used"] + o[1] <= vehicle["width"]]
            if fitting:
                spot = (row, max(fitting, key=lambda o: (o[0], -o[1])))
                break
        if spot is None:
            start = sum(r["depth"] for r in rows)
            room = [o for o in options if start + o[0] <= vehicle["length"]]
            if not room:
                out.append(None)
                continue
            o = _row_choice(room, vehicle["width"])
            row = {"x": start, "depth": o[0], "used": 0}
            rows.append(row)
            spot = (row, o)
        row, o = spot
        out.append((row["x"], row["used"], o[0], o[1]))
        row["used"] += o[1]
    return out


def _split_free(free, px, py, pa, pb):
    """MaxRects: свободните правоъгълници след поставяне на (px, py, pa, pb)."""
    out = set()
    for fx, fy, fa, fb in free:
        if px >= fx + fa or px + pa <= fx or py >= fy + fb or py + pb <= fy:
            out.add((fx, fy, fa, fb))
            continue
        if px > fx:
            out.add((fx, fy, px - fx, fb))
        if px + pa < fx + fa:
            out.add((px + pa, fy, fx + fa - px - pa, fb))
        if py > fy:
            out.add((fx, fy, fa, py - fy))
        if py + pb < fy + fb:
            out.add((fx, py + pb, fa, fy + fb - py - pb))
    rects = sorted(out, key=lambda r: -(r[2] * r[3]))
    keep = []
    for r in rects:
        if not any(k[0] <= r[0] and k[1] <= r[1] and r[0] + r[2] <= k[0] + k[2]
                   and r[1] + r[3] <= k[1] + k[3] for k in keep):
            keep.append(r)
    return keep


def _pack_free(order, vehicle):
    """Стратегия „свободно място“ (MaxRects, най-близо до челото, после отляво):
    смесва ориентациите в една лента — нужно, когато 3 EUR не влизат напряко
    (контейнер 2352 мм: EUR надлъжно + EUR напречно = 2000 мм)."""
    free = [(0, 0, vehicle["length"], vehicle["width"])]
    out = []
    for stack in order:
        best = None
        for a, b in _orientations(stack, vehicle):
            for fx, fy, fa, fb in free:
                if a <= fa and b <= fb:
                    score = (fx + a, fy, -a)
                    if best is None or score < best[0]:
                        best = (score, fx, fy, a, b)
        if best is None:
            out.append(None)
            continue
        _score, x, y, a, b = best
        out.append((x, y, a, b))
        free = _split_free(free, x, y, a, b)
    return out


def _stack_kg(stack):
    return sum(p["kg"] for p in stack)


#: Подредби на куповете, които се пробват (най-доброто се избира в place()).
ORDERS = (
    ("area", lambda s: (-_footprint(s[0])[0] * _footprint(s[0])[1], -_stack_kg(s), s[0]["group"], s[0]["n"])),
    ("long", lambda s: (-_footprint(s[0])[0], -_footprint(s[0])[1], -_stack_kg(s), s[0]["group"], s[0]["n"])),
    ("lines", lambda s: (s[0]["group"], s[0]["n"])),
)


def place(stacks, vehicle):
    """Подрежда куповете (правило 2): пробва „по редове“ и „свободно място“ с
    няколко подредби и взема най-добрата — най-много палети, после най-малко
    заета дължина, после „по редове“ (по-лесна за товарене)."""
    fits = [s for s in stacks if _orientations(s, vehicle)
            and sum(p["h"] for p in s) <= vehicle["height"] and _through_door(s, vehicle)]
    too_big = [s for s in stacks if s not in fits]
    best = None
    for rank, (name, packer) in enumerate((("rows", _pack_rows), ("free", _pack_free))):
        for _oname, key in ORDERS:
            order = sorted(fits, key=key)
            spots = packer(order, vehicle)
            done = [(st, sp) for st, sp in zip(order, spots) if sp]
            count = sum(len(st) for st, _sp in done)
            used = max((sp[0] + sp[2] for _st, sp in done), default=0)
            score = (-count, used, rank)
            if best is None or score < best[0]:
                best = (score, name, done, [st for st, sp in zip(order, spots) if not sp])
    _score, strategy, done, rest = best if best else (None, "rows", [], [])
    placed = [{"stack": st, "x": x, "y": y, "along": a, "across": b,
               "height": sum(p["h"] for p in st), "kg": _stack_kg(st)}
              for st, (x, y, a, b) in done]
    placed.sort(key=lambda it: (it["x"], it["y"]))
    for i, item in enumerate(placed, 1):
        item["no"] = i
    unplaced = [(st, "size" if (_orientations(st, vehicle) == [] or sum(p["h"] for p in st) > vehicle["height"])
                 else "door") for st in too_big] + [(st, "space") for st in rest]
    return placed, unplaced, strategy


def side_gap_length(placed, width, used_len):
    """Обща дължина (мм) в заетата част, където празното място напряко е над
    SIDE_GAP_MM — там плътното нареждане не стига и товарът се укрепва."""
    cuts = sorted({0, used_len} | {it["x"] for it in placed} | {it["x"] + it["along"] for it in placed})
    total = 0
    for a, b in zip(cuts, cuts[1:]):
        if b <= a or a >= used_len:
            continue
        mid = (a + b) / 2.0
        occupied = sum(it["across"] for it in placed if it["x"] <= mid < it["x"] + it["along"])
        if width - occupied > SIDE_GAP_MM:
            total += b - a
    return total


def plan(lines, vehicle):
    """Пълният резултат за шаблона."""
    pallets, capped = _expand(lines)
    stacks = build_stacks(pallets, vehicle["height"])
    placed, unplaced, strategy = place(stacks, vehicle)
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
    # Разпределение по дължината: дял на товара в предната/задната половина.
    front_kg = sum(b["kg"] for b in weight_bins(placed, vehicle["length"], step=vehicle["length"] / 2.0)[:1])
    front_share = front_kg / placed_kg if placed_kg else None
    if front_share is not None and vehicle.get("container") and max(front_share, 1 - front_share) > CONTAINER_HALF_MAX:
        warnings.append(_("Над %(pct)d%% от товара е в едната половина на контейнера — по EU насоките за "
                          "укрепване на товари трябва да е най-много %(pct)d%%. Разпределете палетите по дължината.")
                        % {"pct": round(CONTAINER_HALF_MAX * 100)})
    overloaded = [p for p in pallets if EPAL_LIMITS.get(_footprint(p)) and p["kg"] > EPAL_LIMITS[_footprint(p)][0]]
    if overloaded:
        warnings.append(_("%(n)d палета са по-тежки от безопасния товар на EPAL палета (%(kg)s кг) — "
                          "проверете палета и теглото.")
                        % {"n": len(overloaded), "kg": fmt(EPAL_LIMITS[_footprint(overloaded[0])][0], 0)})
    slack = vehicle["length"] - used_len
    if placed and slack < TIGHT_SLACK_MM:
        warnings.append(_("Остават само %(mm)d мм свободна дължина — подредбата е на ръба. Проверете точната "
                          "вътрешна дължина на каросерията или махнете един палет.") % {"mm": slack})
    gap_len = side_gap_length(placed, vehicle["width"], used_len)
    notes = []
    if gap_len:
        notes.append(_("На %(m)s м от дължината празното място напряко е над 8 см — там палетите трябва да се "
                       "укрепят допълнително (ремъци, подпори), а не само да се наредят плътно (UK DVSA).")
                     % {"m": fmt(gap_len / 1000.0, 1)})
    legend = []
    for group, line in enumerate(lines):
        if not line["ok"]:
            continue
        legend.append({"group": group, "desc": line["desc"], "qty": line["qty"],
                       "dims": "%d×%d×%s" % (line["length"], line["width"], line["height"] or "—"),
                       "kg": line["weight"], "stack": line["stack"],
                       "color": group_color(group), "tint": _mix(group_color(group), "#ffffff", 0.55)})
    return {
        "vehicle": vehicle, "pallets": len(pallets), "stacks": len(placed), "placed": placed,
        "unplaced": [{"stack": s, "reason": r} for s, r in unplaced],
        "placed_pallets": sum(len(it["stack"]) for it in placed),
        "total_kg": total_kg, "placed_kg": placed_kg,
        "used_length": used_len, "ldm": used_len / 1000.0 if used_len else 0.0,
        # Товарни метри по площ (LDM = Д × Ш / 2,4 м на място на пода; купът се брои веднъж).
        "ldm_area": floor / 1e6 / LDM_WIDTH_M, "strategy": strategy,
        "floor_pct": round(floor * 100.0 / v_floor) if v_floor else 0,
        "length_pct": round(used_len * 100.0 / vehicle["length"]) if vehicle["length"] else 0,
        "volume_pct": round(volume * 100.0 / v_volume) if v_volume else 0,
        "weight_pct": round(placed_kg * 100.0 / vehicle["payload"]) if vehicle["payload"] else 0,
        "cog": cog, "warnings": warnings, "legend": legend, "notes": notes,
        "front_share": front_share, "slack": slack,
    }


def fmt(value, places=2):
    """Число за екрана с десетична запетая и интервал за хилядите."""
    if value is None:
        return ""
    text = "{:,.{p}f}".format(float(value), p=places).replace(",", " ").replace(".", ",")
    return text


#: Наклонена (кабинетна) проекция за 3D изгледа: дълбочината (напряко)
#: отива нагоре-надясно — виждат се предната, горната и дясната (към
#: вратите) стена на всеки куп.
ISO_KX, ISO_KY = 0.42, 0.30


def _iso(x, y, z):
    return (x + y * ISO_KX, -z - y * ISO_KY)


def _poly(points):
    return " ".join("%.0f,%.0f" % _iso(*pt) for pt in points)


def _boards(x, y, a, b, long_side_along):
    """Линиите между 5-те горни дъски на палета (успоредни на дългата му
    страна) — показват как е обърнат палетът на чертежа отгоре."""
    lines = []
    for k in range(1, 5):
        if long_side_along:
            yy = y + b * k / 5.0
            lines.append((x + 40, yy, x + a - 40, yy))
        else:
            xx = x + a * k / 5.0
            lines.append((xx, y + 40, xx, y + b - 40))
    return lines


def weight_bins(placed, length, step=1000):
    """Товар (кг) по участъци от `step` мм по дължината — теглото на всеки
    куп се разпределя пропорционално на дължината, която заема."""
    n = max(int(math.ceil(length / float(step))), 1)
    bins = [0.0] * n
    for it in placed:
        if not it["kg"]:
            continue
        x0, x1 = it["x"], it["x"] + it["along"]
        for i in range(int(x0 // step), min(int(math.ceil(x1 / float(step))), n)):
            lo, hi = max(x0, i * step), min(x1, (i + 1) * step)
            if hi > lo:
                bins[i] += it["kg"] * (hi - lo) / float(x1 - x0)
    return [{"x": i * step, "w": min(step, length - i * step), "kg": kg} for i, kg in enumerate(bins)]


def svg_views(result):
    """Данни за чертежите (в мм — мащабът е във viewBox на шаблона)."""
    v = result["vehicle"]
    L, W, H = v["length"], v["width"], v["height"]
    boxes, solids = [], []
    for it in result["placed"]:
        base = it["stack"][0]
        color = group_color(base["group"])
        long_along = it["along"] >= it["across"]
        levels, z = [], 0
        for p in it["stack"]:
            h = p["h"] or 0
            c = group_color(p["group"])
            levels.append({"z": z, "h": h, "group": p["group"], "color": c,
                           "tint": _mix(c, "#ffffff", 0.45),
                           "base": min(PALLET_BASE_MM, h * 0.3) if h else 0})
            # 3D: дървена основа + товар (по два „слоя“ на палет).
            if h:
                pb = min(PALLET_BASE_MM, h * 0.3)
                for z0, z1, top, front, side, stroke in (
                        (z, z + pb, _mix(WOOD, "#ffffff", 0.2), WOOD, _mix(WOOD, "#000000", 0.18), WOOD_DARK),
                        (z + pb, z + h, _mix(c, "#ffffff", 0.62), _mix(c, "#ffffff", 0.38),
                         _mix(c, "#000000", 0.08), _mix(c, "#000000", 0.35))):
                    x0, x1, y0, y1 = it["x"] + 12, it["x"] + it["along"] - 12, it["y"] + 12, it["y"] + it["across"] - 12
                    solids.append({
                        "key": (-y0, x0, z0), "no": it["no"], "stroke": stroke,
                        "front": _poly(((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))), "front_fill": front,
                        "top": _poly(((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))), "top_fill": top,
                        "side": _poly(((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))), "side_fill": side,
                    })
            z += h
        boxes.append({"no": it["no"], "x": it["x"], "y": it["y"], "l": it["along"], "w": it["across"],
                      "height": it["height"], "levels": levels, "count": len(it["stack"]),
                      "kg": it["kg"], "color": color, "tint": _mix(color, "#ffffff", 0.55),
                      "boards": _boards(it["x"], it["y"], it["along"], it["across"], long_along),
                      "desc": " / ".join(dict.fromkeys(p["desc"] for p in it["stack"] if p["desc"]))})
    solids.sort(key=lambda s: s["key"])
    bins = weight_bins(result["placed"], L)
    peak = max((b["kg"] for b in bins), default=0)
    peak_index = next((i for i, b in enumerate(bins) if peak and b["kg"] == peak), -1)
    iso_w, iso_h = _iso(L, W, 0)[0], H + W * ISO_KY
    return {"boxes": boxes, "L": L, "W": W, "H": H, "font": max(min(W, L) / 14.0, 90),
            "solids": solids, "bins": bins, "peak": peak, "peak_index": peak_index,
            "iso": {"w": iso_w, "h": iso_h,
                    "floor": _poly(((0, 0, 0), (L, 0, 0), (L, W, 0), (0, W, 0))),
                    "back": _poly(((0, W, 0), (L, W, 0), (L, W, H), (0, W, H))),
                    "front_wall": _poly(((0, 0, 0), (0, W, 0), (0, W, H), (0, 0, H))),
                    "roof": _poly(((0, 0, H), (L, 0, H), (L, W, H), (0, W, H))),
                    "cab_label": _iso(0, 0, H / 2.0), "door_label": _iso(L, 0, -260)}}


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
    if result:
        # Обемно тегло = LDM × коефициент кг/LDM от „Митнически настройки“ —
        # коефициентът е по договор с превозвача (1 750 / 1 850 / 2 000).
        import customs  # pylint: disable=import-outside-toplevel
        factor = customs.ldm_factor(db.get_settings(get_db()).get("customs_ldm_factor"))
        result["ldm_factor"] = factor
        result["volumetric_kg"] = result["ldm_area"] * factor
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
