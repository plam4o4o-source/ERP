# -*- coding: utf-8 -*-
"""Одит (13.10.2026, група GFX): движение и графика — v3.81.0.

Сървърната/статичната част на одобрения прототип (scratchpad/t/anim):
  Г9  жетони за движение + едно общо правило за prefers-reduced-motion;
  Г6  бутони без „подскачане“ при посочване, scale(.97) при натискане;
  Г2  лента за прогрес при навигация / живо търсене (JS — виж e2e);
  Г3  ?hl=<id> след запис → data-row-id на реда в списъка;
  Г4  модали: мек фон + „изскачане“ (само CSS);
  Г5  отметка на успеха, свиване на купчината toast-ове;
  Г1  графика „Документи по месеци“ (SVG от сървъра) + броячи на таблото;
  Г7  странична лента на телефон (opacity/visibility вместо display);
  Г8  вход: картата влиза веднъж, грешката „поклаща“;
  П11 Speculation Rules — сървърът не изразходва flash при prefetch;
  Г10 кратък етикет на типа в списъците.
Поведението в браузъра — tests/test_motion_v381_e2e.py (-m e2e)."""
import os
import re
from datetime import date

from conftest import issue_cmr, post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _css():
    with open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8") as f:
        return f.read()


def _js():
    with open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8") as f:
        return f.read()


def _css_no_comments():
    return re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)


def _reduce_blocks(css):
    """Съдържанието на всички @media (prefers-reduced-motion: reduce) блокове."""
    out = []
    for m in re.finditer(r"@media \(prefers-reduced-motion: reduce\) \{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[m.end():i - 1])
    return "\n".join(out)


# ---------------------------------------------------------------- Г9: жетони

def test_motion_tokens_and_transition_alias():
    css = _css_no_comments()
    root = re.search(r":root \{([^}]*)\}", css).group(1)
    for token in ("--dur-1: 90ms", "--dur-2: 160ms", "--dur-3: 240ms", "--dur-4: 420ms",
                  "--dur-flash: 1600ms", "--ease-out: cubic-bezier(.2, .8, .2, 1)",
                  "--ease-in: cubic-bezier(.4, 0, 1, 1)", "--ease-std: cubic-bezier(.4, 0, .2, 1)",
                  "--transition: var(--dur-2) var(--ease-std)"):
        assert token in root, token
    # Старите твърди времена на toast/реда/търсенето минават през жетоните.
    assert "toast-in 300ms" not in css and "toast-out 200ms" not in css
    assert "row-appear 250ms" not in css
    assert "opacity .12s ease" not in css and "color .15s ease" not in css


def test_css_comments_do_not_swallow_rules():
    """Регресия от самата работа: „--dur-*/“ в коментар затваря коментара и
    браузърът изхвърля следващото правило (#ph-progress остана без стил)."""
    css = _css()
    for m in re.finditer(r"/\*(.*?)\*/", css, re.S):
        assert "/*" not in m.group(1), m.group(0)[:80]
    stripped = _css_no_comments()
    assert stripped.count("{") == stripped.count("}")
    assert re.search(r"\n#ph-progress \{[^}]*position: fixed", stripped)


def test_reduced_motion_single_global_rule_with_informative_exceptions():
    css = _css_no_comments()
    block = _reduce_blocks(css)
    rule = re.search(r"\*, \*::before, \*::after \{([^}]*)\}", block)
    assert rule, "общото правило * за намалена анимация"
    body = rule.group(1)
    for decl in ("animation-duration: 1ms !important", "animation-iteration-count: 1 !important",
                 "transition-duration: 1ms !important", "scroll-behavior: auto !important"):
        assert decl in body, decl
    # Изключенията са информативни и неподвижни.
    assert re.search(r"tr\.ph-row-flash > td \{[^}]*animation: none !important;[^}]*--flash-bg", block)
    assert re.search(r"\.toast-success \.toast-icon path \{[^}]*stroke-dashoffset: 0", block)
    assert re.search(r"#ph-progress\.is-active \{[^}]*animation: none !important", block)
    assert re.search(r"\.ph-delta \{[^}]*opacity: 1", block)
    assert re.search(r"\.qa:hover[^{]*\{[^}]*transform: none !important", block)
    # Кръстосаните View Transitions НЕ са включени (измерено по-бавно).
    assert "@view-transition" not in css


# ---------------------------------------------------------------- Г6: бутони

def test_buttons_press_instead_of_hover_lift():
    css = _css_no_comments()
    hover = re.search(r"\nbutton:hover, \.btn:hover \{([^}]*)\}", css).group(1)
    assert "translateY" not in hover and "transform" not in hover
    assert re.search(r"button:active:not\(:disabled\), \.btn:active \{[^}]*scale\(\.97\)", css)
    base = re.search(r"\nbutton, \.btn \{([^}]*)\}", css).group(1)
    assert "outline-offset" in base, "пръстенът на фокуса „се прибира“ (преход на outline-offset)"
    assert "button:not(:focus-visible), .btn:not(:focus-visible) { outline-offset: 5px; }" in css


# ---------------------------------------------------------------- Г4/Г5/Г7/Г8

def test_modals_soft_backdrop_and_pop_contrast_keeps_dark():
    css = _css_no_comments()
    soft = css.index(".camera-modal, .pl-modal { background: rgba(8, 14, 22, .58);")
    # Правилото е СЛЕД основните .pl-modal/.camera-modal (иначе те го надвиват).
    assert soft > css.index(".pl-modal {\n") and soft > css.index(".camera-modal {\n")
    assert "animation: ph-fade var(--dur-2)" in css[soft:soft + 200]
    assert re.search(r":root\[data-theme=\"contrast\"\] \.camera-modal, :root\[data-theme=\"contrast\"\] "
                     r"\.pl-modal \{ background: rgba\(0, 0, 0, \.85\); \}", css)
    assert re.search(r"\.camera-modal-inner, \.pl-modal-inner \{ animation: ph-pop var\(--dur-3\)", css)
    assert "@keyframes ph-pop { from { opacity: 0; transform: translateY(10px) scale(.96); } }" in css


def test_toast_checkmark_draws_and_stack_collapses():
    css = _css_no_comments()
    assert re.search(r"\.toast-success \.toast-icon path \{[^}]*stroke-dasharray: 23", css)
    assert re.search(r"\.toast\.toast-collapse \{[^}]*height: 0 !important[^}]*margin-bottom: -10px", css)
    js = _js()
    body = js[js.index("function dismissToast"):js.index("function initToasts")]
    assert "toast-collapse" in body and "transitionend" in body


def test_phone_sidebar_backdrop_fades_with_visibility():
    css = _css_no_comments()
    block = css[css.index("@media (max-width: 980px) {\n  .app-shell"):]
    backdrop = re.search(r"  \.sidebar-backdrop \{([^}]*)\}", block).group(1)
    assert "display: block" in backdrop and "opacity: 0" in backdrop and "visibility: hidden" in backdrop
    assert re.search(r"\.sidebar-backdrop\.open \{[^}]*opacity: 1; visibility: visible", block)
    assert "transition: transform var(--dur-2) var(--ease-in)" in block
    assert re.search(r"\.sidebar\.open \{[^}]*transition: transform var\(--dur-3\) var\(--ease-out\)", block)


def test_login_card_enters_once_and_error_shakes():
    css = _css_no_comments()
    assert ".login-card { animation: ph-card-in 360ms var(--ease-out) both; }" in css
    assert ".login-card:has(.error) { animation: none; }" in css
    assert ".login-card .error { animation: ph-shake 360ms" in css
    assert "translateX(-5px)" in css and "translateX(5px)" in css


# ---------------------------------------------------------------- Г1: графика

def _insert_doc(con, doc_type, created_at, number):
    con.execute(
        "INSERT INTO documents (doc_type, number, seq, year, barcode, data, created_at, client_name)"
        " VALUES (?, ?, ?, ?, ?, '{}', ?, 'Графика ООД')",
        (doc_type, number, int(number.split("/")[0]), int(created_at[:4]),
         "%s-%s" % (doc_type.upper(), number), created_at))


def test_monthly_counts_last_12_months_without_invoices(flask_app, con):
    import routes_dashboard
    _insert_doc(con, "cmr", "2026-10-02 10:00:00", "9001/2026")
    _insert_doc(con, "pallet", "2026-10-12 23:59:59", "9002/2026")
    _insert_doc(con, "invoice_br", "2026-10-05 10:00:00", "9003/2026")   # фактура — не се брои
    _insert_doc(con, "waybill", "2026-03-31 08:00:00", "9004/2026")
    _insert_doc(con, "cmr", "2025-11-01 00:00:00", "9005/2025")         # първият от 12-те
    _insert_doc(con, "cmr", "2025-10-31 23:00:00", "9006/2025")         # 13-и месец — извън
    con.commit()
    out = routes_dashboard._monthly_counts(con, today=date(2026, 10, 13))
    months = out["months"]
    assert [m["key"] for m in months][0] == "2025-11" and months[-1]["key"] == "2026-10"
    assert len(months) == 12 and months[-1]["current"] and not months[0]["current"]
    values = {m["key"]: m["value"] for m in months}
    assert values["2026-10"] == 2 and values["2026-03"] == 1 and values["2025-11"] == 1
    assert out["total"] == 4 and out["max"] == 2
    # Етикет — само текущият и най-високият стълб.
    assert [m["key"] for m in months if m["labelled"]] == ["2026-10"]
    assert months[-1]["short"] == "окт" and months[-1]["long"] == "октомври 2026"


def test_monthly_counts_use_covering_index(flask_app, con, monkeypatch):
    import routes_dashboard
    statements = []
    real = con.execute

    class _Spy:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            return real(sql, params)

    routes_dashboard._monthly_counts(_Spy())
    assert len(statements) == 1
    sql, params = statements[0]
    plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "COVERING INDEX" in plan and "created_at>" in plan, plan


def test_dashboard_renders_accessible_svg_chart(admin_client):
    issue_cmr(admin_client, "Графика Клиент ЕООД")
    body = admin_client.get("/").data.decode()
    assert 'class="ph-chart-svg"' in body and 'role="img"' in body and 'height="150"' in body
    svg = body[body.index('<svg class="ph-chart-svg"'):body.index("</svg>", body.index('<svg class="ph-chart-svg"'))]
    assert svg.count('<g class="col') == 12
    assert svg.count('class="col is-current"') == 1
    assert 'aria-label="Документи по месеци за последните 12 месеца' in svg
    assert 'data-tip="' in svg
    # Скритата таблица с всички стойности за екранните четци.
    assert re.search(r'<table class="visually-hidden">\s*<caption>Документи по месеци</caption>', body)
    # Стълбът на текущия месец растe (поне 1 документ) и има етикет.
    assert 'class="vl"' in svg


def test_chart_month_names_follow_interface_language(admin_client):
    with admin_client.session_transaction() as s:
        s["lang"] = "en"
    body = admin_client.get("/").data.decode()
    svg = body[body.index('<svg class="ph-chart-svg"'):]
    month = date.today().strftime("%b")   # C локал → английски съкращения
    assert '<text class="xl"' in svg and ">%s</text>" % month in svg


# ---------------------------------------------------------------- Г3: ?hl=

def test_client_save_redirects_with_hl_and_row_has_id(admin_client, con):
    resp = post_with_csrf(admin_client, "/clients/new", {"name": "Подчертан Клиент ООД"},
                          csrf_source_url="/clients/new", follow_redirects=False)
    assert resp.status_code == 302
    new_id = con.execute("SELECT id FROM clients WHERE name = 'Подчертан Клиент ООД'").fetchone()["id"]
    assert resp.headers["Location"].endswith("/clients?hl=%d" % new_id)
    body = admin_client.get(resp.headers["Location"]).data.decode()
    assert '<tr data-row-id="%d">' % new_id in body


def test_material_save_redirects_with_hl_code(admin_client):
    resp = post_with_csrf(admin_client, "/materials/save",
                          {"original_code": "", "code": "HL-1", "description": "Тест", "net_weight": "1"},
                          csrf_source_url="/materials", follow_redirects=False)
    assert resp.status_code == 302 and "hl=HL-1" in resp.headers["Location"]
    body = admin_client.get(resp.headers["Location"]).data.decode()
    assert '<tr data-row-id="HL-1">' in body


def test_invoice_client_and_user_saves_redirect_with_hl(admin_client, con):
    resp = post_with_csrf(admin_client, "/invoices/clients/new", {"name": "HL Bill To"},
                          csrf_source_url="/invoices/clients/new", follow_redirects=False)
    entry = con.execute("SELECT id FROM invoice_clients WHERE name = 'HL Bill To'").fetchone()["id"]
    assert resp.headers["Location"].endswith("hl=%d" % entry)
    assert '<tr data-row-id="%d">' % entry in admin_client.get(resp.headers["Location"]).data.decode()

    resp = post_with_csrf(admin_client, "/admin/users/new",
                          {"username": "hl_user", "password": "hl-password-123", "full_name": "HL",
                           "role": "employee"}, csrf_source_url="/admin/users", follow_redirects=False)
    uid = con.execute("SELECT id FROM users WHERE username = 'hl_user'").fetchone()["id"]
    assert resp.headers["Location"].endswith("/admin/users?hl=%d" % uid)
    assert '<tr data-row-id="%d">' % uid in admin_client.get(resp.headers["Location"]).data.decode()


def test_document_and_invoice_lists_carry_row_ids(admin_client, con):
    issue_cmr(admin_client, "Ред С Номер ЕООД")
    doc_id = con.execute("SELECT MAX(id) AS m FROM documents").fetchone()["m"]
    assert '<tr data-row-id="%d">' % doc_id in admin_client.get("/docs").data.decode()
    _insert_doc(con, "invoice_no", date.today().isoformat() + " 08:00:00", "8001/%d" % date.today().year)
    con.commit()
    inv_id = con.execute("SELECT MAX(id) AS m FROM documents").fetchone()["m"]
    assert '<tr data-row-id="%d">' % inv_id in admin_client.get("/invoices").data.decode()


def test_row_highlight_js_uses_ids_not_text():
    js = _js()
    fn = js[js.index("function initRowHighlight"):js.index("var TILE_COUNT_MS")]
    assert 'params.get("hl")' in fn and "data-row-id" in fn and "replaceState" in fn
    assert "textContent" not in fn, "без познаване по текст"
    assert "scrollIntoView" in fn and "ph-row-flash" in fn


# ---------------------------------------------------------------- П11: prefetch

def test_prefetch_does_not_consume_flash_messages(admin_client):
    """Предварителното изтегляне (Sec-Purpose: prefetch) рендира страницата
    БЕЗ да изразходва flash съобщенията — иначе „Клиентът е запазен“ би
    изчезнало в невидим отговор, преди човекът да е отворил страницата."""
    resp = post_with_csrf(admin_client, "/clients/new", {"name": "Prefetch ООД"},
                          csrf_source_url="/clients/new", follow_redirects=False)
    assert resp.status_code == 302
    pre = admin_client.get("/clients", headers={"Sec-Purpose": "prefetch"}).data.decode()
    assert "Клиентът е запазен" not in pre and 'class="toasts' not in pre
    old = admin_client.get("/docs", headers={"Purpose": "prefetch"}).data.decode()
    assert "Клиентът е запазен" not in old
    real = admin_client.get("/clients").data.decode()
    assert "Клиентът е запазен в адресната книга." in real


def test_prefetch_skips_instance_heartbeat(admin_client, monkeypatch):
    import db as db_mod
    calls = []
    monkeypatch.setattr(db_mod, "record_instance", lambda *a, **k: calls.append(1))
    admin_client.get("/", headers={"Sec-Purpose": "prefetch;prerender"})
    assert calls == []
    admin_client.get("/")
    assert calls == [1]


def test_speculation_rules_only_for_sidebar_and_view_links():
    js = _js()
    fn = js[js.index("function initPrefetchRules"):]
    fn = fn[:fn.index("\n}\n")]
    assert 'selector_matches: "#sidebar a.nav-item, a.btn-view"' in fn
    assert 'eagerness: "moderate"' in fn
    for never in ('"/logout"', '"/*/pdf*"', '"/*.pdf"', '"/*.xlsx"', '"/*/export*"', '"/*copy*"',
                  '"/*delete*"'):
        assert never in fn, never
    assert 'type = "speculationrules"' in fn
    # Не е вграден <script> в шаблоните (CSP/SEC) — само от app.js.
    with open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8") as f:
        assert "speculationrules" not in f.read()


def test_nav_progress_skips_downloads_and_prevented_events():
    js = _js()
    fn = js[js.index("var NAV_PROGRESS_DELAY_MS"):js.index("function initRowHighlight")]
    assert "NAV_PROGRESS_DELAY_MS = 150" in fn
    for marker in ("e.defaultPrevented", '"download"', '"data-pdf-export"', "e.ctrlKey", "e.metaKey",
                   "pageshow", '"_self"'):
        assert marker in fn, marker
    regex = re.search(r"var NAV_DOWNLOAD_RE = (/.*/i);", fn).group(1)
    assert "export" in regex and "pdf" in regex and "xlsx" in regex


# ---------------------------------------------------------------- Г10: етикет

def test_short_type_labels_defined_for_long_titles():
    import db
    assert db.DOC_TYPES["waybill"]["short"] == "Товарителница (вътр.)"
    assert db.DOC_TYPES["dualuse"]["short"] == "Декл. двойна употреба"
    assert db.DOC_TYPES["export_it"]["short"] == "Декл. износ Италия"
    assert "short" not in db.DOC_TYPES["cmr"]


def test_short_type_label_rendered_for_existing_waybill(admin_client, con):
    _insert_doc(con, "waybill", date.today().isoformat() + " 09:00:00", "7001/%d" % date.today().year)
    con.commit()
    for url in ("/docs", "/"):
        body = admin_client.get(url).data.decode()
        assert ('<span class="doc-type doc-type--waybill" title="Товарителница (вътрешен превоз)">'
                'Товарителница (вътр.)</span>') in body, url
