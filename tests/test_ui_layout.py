# -*- coding: utf-8 -*-
"""Одит (04.10.2026) — графични подобрения (група UI, P1–P10, B2, A1–A8, F3).

Маркъпът на списъците/изгледа/таблото и токените на темите. Истинското
разположение в браузър (преливане, лепкави ленти, карти на телефон) е в
tests/test_e2e_ui_layout.py."""
import json
import re

import pytest

from conftest import issue_cmr, post_with_csrf, read_source

_ONE_INVOICE_ITEM = json.dumps([{"material_code": "TEST-ITEM", "qty": "1", "unit_price": "1"}])


def _css():
    return read_source("static", "style.css")


def _issue_invoice(client, consignee="Получател Фактура"):
    resp = post_with_csrf(client, "/invoice-br/new", {
        "consignee_name": consignee, "items_json": _ONE_INVOICE_ITEM,
    }, csrf_source_url="/invoice-br/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def _row_actions(body):
    return re.findall(r'<td class="row-actions">(.*?)</td>', body, re.S)


# ---------------------------------------------------------------- P2/Б7: действия в редовете
def test_documents_list_row_actions_are_calm_and_named(admin_client):
    doc_id = issue_cmr(admin_client, consignee_name="Ред Действия ЕООД")
    body = admin_client.get("/docs").get_data(as_text=True)
    cell = _row_actions(body)[0]
    # „Преглед“ — контурен; останалите — иконни с достъпно име и подсказка
    assert 'class="btn btn-small btn-outline btn-view"' in cell
    for label in ("Редактирай", "Копирай като нов", "Изтрий"):
        assert 'aria-label="%s"' % label in cell and 'title="%s"' % label in cell
        assert '<span class="btn-label">%s</span>' % label in cell
    # Б7: „Копирай като нов“ сочи съществуващия маршрут copy_document
    assert 'href="/doc/%d/copy"' % doc_id in cell
    # изтриването не е плътен червен бутон
    assert "btn-danger" not in cell and "icon-btn--danger" in cell


def test_invoices_list_has_copy_as_new_and_icon_buttons(admin_client):
    doc_id = _issue_invoice(admin_client)
    body = admin_client.get("/invoices").get_data(as_text=True)
    cell = _row_actions(body)[0]
    assert 'href="/doc/%d/copy"' % doc_id in cell
    assert "btn-view" in cell and cell.count("icon-btn") >= 3
    assert "btn-danger" not in cell
    # копирането реално отваря формата за нова фактура
    resp = admin_client.get("/doc/%d/copy" % doc_id)
    assert resp.status_code == 302 and "/invoice-br/new" in resp.headers["Location"]


@pytest.mark.parametrize("url", ["/clients", "/materials", "/invoices/clients"])
def test_address_books_and_materials_use_one_word_edit(admin_client, db_module, url):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, city) VALUES ('Клиент Ред', 'София')")
    con.execute("INSERT INTO materials (code, description, net_weight) VALUES ('MAT-1', 'Описание', '1.5')")
    con.execute("INSERT INTO invoice_clients (name, delivery_name, billing_name) VALUES ('Запис', 'Д', 'Ф')")
    con.commit()
    con.close()
    body = admin_client.get(url).get_data(as_text=True)
    cell = _row_actions(body)[0]
    assert '<span class="btn-label">Редактирай</span>' in cell
    assert "Редакция<" not in body
    assert "icon-btn--danger" in cell and "btn-danger" not in cell


# ---------------------------------------------------------------- P10/A7: карти на телефон
def test_list_cells_carry_data_labels_and_action_header_is_named(admin_client):
    issue_cmr(admin_client, consignee_name="Етикети ЕООД")
    body = admin_client.get("/docs").get_data(as_text=True)
    assert '<table class="list list-cards">' in body
    for label in ("Тип", "Номер", "Баркод", "Получател/Клиент", "Издаден от", "Дата"):
        assert 'data-label="%s"' % label in body
    assert "<th></th>" not in body
    assert '<th><span class="visually-hidden">Действия</span></th>' in body


def test_no_empty_header_cells_in_owned_list_templates():
    for name in ("documents", "invoices", "clients", "materials", "invoice_clients",
                 "dashboard", "scan_choose", "_macros", "my_settings"):
        assert "<th></th>" not in read_source("templates", name + ".html"), name


# ---------------------------------------------------------------- P3/B3: филтърната лента
def test_group_by_client_is_a_toggle_inside_the_filter_bar(admin_client):
    issue_cmr(admin_client, consignee_name="Група ЕООД")
    body = admin_client.get("/docs").get_data(as_text=True)
    actions = re.search(r'<div class="searchbar-actions">(.*?)</div>', body, re.S).group(1)
    toggle = re.search(r'<input type="checkbox" name="group" value="client"[^>]*>', actions)
    assert toggle and "checked" not in toggle.group(0) and "data-live-toggle" in toggle.group(0)
    # отделната карта само за бутона я няма
    assert "Без групиране" not in body
    grouped = admin_client.get("/docs?group=client").get_data(as_text=True)
    toggle = re.search(r'<input type="checkbox" name="group" value="client"[^>]*>', grouped)
    assert "checked" in toggle.group(0)
    assert 'type="hidden" name="group"' not in grouped


def test_filter_toggle_js_reruns_the_search():
    js = read_source("static", "app.js")
    assert "function initFilterToggles" in js
    assert 'querySelectorAll("input[data-live-toggle]")' in js


# ---------------------------------------------------------------- P9: табло
def test_dashboard_is_colour_coded_by_document_type(admin_client):
    issue_cmr(admin_client, consignee_name="Табло ЕООД")
    body = admin_client.get("/").get_data(as_text=True)
    for t in ("cmr", "packing", "pallet", "waybill", "dualuse", "export_it"):
        assert 'class="qa doc-type--%s"' % t in body
        assert 'class="tile doc-type--%s"' % t in body
    recent = body[body.index("Последни документи"):]
    assert '<span class="doc-type doc-type--cmr">' in recent
    assert 'class="btn btn-small btn-outline btn-view"' in recent
    assert re.search(r'<td class="nowrap" data-label="Баркод">CMR-', recent)
    assert re.search(r'<td class="nowrap" data-label="Дата">', recent)


def test_materials_weight_column_is_numeric(admin_client, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO materials (code, description, net_weight) VALUES ('W-1', 'Тегло', '12.5')")
    con.commit()
    con.close()
    body = admin_client.get("/materials").get_data(as_text=True)
    assert '<th class="num">Нето тегло, кг/бр</th>' in body
    assert '<td class="num" data-label="Нето тегло, кг/бр">12.5</td>' in body


# ---------------------------------------------------------------- P8: изглед на документ
def test_document_view_toolbar_segmented_copies_and_heading(admin_client):
    doc_id = issue_cmr(admin_client)
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    seg = re.search(r'<span class="seg-control" role="group" aria-label="Брой екземпляри">(.*?)</span>', body, re.S)
    assert seg and seg.group(1).count("btn-copies") == 4
    assert '<h1 class="doc-toolbar-meta">' in body          # A8
    assert body.count("doc-extra-card") == 2                 # прикачени + публичен достъп
    # A3: полето за файл има етикет; A2 — бутонът за изтриване на файл има име (макросът)
    assert '<label for="attachment-file-%d" class="visually-hidden">' % doc_id in body
    assert 'id="attachment-file-%d"' % doc_id in body
    macros = read_source("templates", "_macros.html")
    assert re.search(r'icon-btn--danger" title="\{\{ _\(\'Изтрий прикачения файл\'\) \}\}"\s+aria-label=', macros)


def test_pagination_disabled_state_is_readable():
    macros = read_source("templates", "_macros.html")
    assert "opacity:.4" not in macros
    assert macros.count('class="btn btn-secondary btn-small is-disabled" aria-disabled="true"') == 2
    assert re.search(r"\.pagination \.is-disabled \{[^}]*color: var\(--muted\)", _css())


# ---------------------------------------------------------------- A8: заглавия
def test_login_has_h1_and_settings_has_no_heading_skip(client, admin_client):
    assert re.search(r'<h1 class="t">', client.get("/login").get_data(as_text=True))
    body = admin_client.get("/settings").get_data(as_text=True)
    assert "<h3" not in body
    assert '<h2 class="form-section-title">' in body


# ---------------------------------------------------------------- B17: системни настройки
def test_system_settings_section_nav_targets_exist(admin_client):
    body = admin_client.get("/admin/system").get_data(as_text=True)
    nav = re.search(r'<nav class="settings-nav no-print"[^>]*>(.*?)</nav>', body, re.S).group(1)
    anchors = re.findall(r'href="#([\w-]+)"', nav)
    assert len(anchors) == 6
    for a in anchors:
        assert 'id="%s"' % a in body, a
    # „Архивирай сега“ е в реда на „Запази“ и е свързан със своята форма
    assert re.search(r'<button type="submit" class="btn-secondary" form="backup-now-form"', body)
    assert 'id="backup-now-form"' in body


# ---------------------------------------------------------------- F3: насрочено възстановяване
def _with_pending_restore(flask_app, value):
    # Тестов контекстен процесор (вместо този на BACKEND-CORE). Добавя се
    # направо в списъка — admin_client вече е обслужил заявка (входа), а
    # Flask забранява @context_processor след първата заявка. Добавеният
    # последен процесор печели при еднакъв ключ.
    def _test_pending_restore():
        return {"pending_restore": value}
    flask_app.template_context_processors[None].append(_test_pending_restore)


def test_pending_restore_banner_on_every_page(flask_app, admin_client):
    _with_pending_restore(flask_app, {"requested_at": "2026-10-04 12:30:00"})
    for url in ("/", "/docs", "/cmr/new", "/clients"):
        body = admin_client.get(url).get_data(as_text=True)
        assert "pending-restore-banner" in body, url
        assert "2026-10-04 12:30:00" in body
    assert 'href="/admin/system#backup"' in admin_client.get("/").get_data(as_text=True)


def test_pending_restore_banner_for_employee_without_admin_link(flask_app, employee_client):
    _with_pending_restore(flask_app, {"requested_at": "2026-10-04 12:30:00"})
    body = employee_client.get("/").get_data(as_text=True)
    assert "pending-restore-banner" in body
    assert "/admin/system#backup" not in body


@pytest.mark.parametrize("value", [None, {}, ""])
def test_no_pending_restore_banner_when_nothing_is_scheduled(flask_app, admin_client, value):
    _with_pending_restore(flask_app, value)
    assert "pending-restore-banner" not in admin_client.get("/").get_data(as_text=True)


def test_pending_restore_banner_tolerates_missing_requested_at(flask_app, admin_client):
    _with_pending_restore(flask_app, {"path": "x.db"})
    body = admin_client.get("/").get_data(as_text=True)
    assert "pending-restore-banner" in body and "(заявено на —)" in body


def test_without_context_processor_no_banner(admin_client):
    # Докато BACKEND-CORE не е добавил процесора, шаблонът не гърми.
    assert admin_client.get("/").status_code == 200


# ---------------------------------------------------------------- CSS: токени и контраст
def _hex_lum(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a, b):
    la, lb = _hex_lum(a), _hex_lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _themes():
    css = _css()
    out = {}
    for name in ("light", "dark", "blue", "green", "contrast", "sepia"):
        sel = (r':root\[data-theme="light"\], :root:not\(\[data-theme\]\)' if name == "light"
               else r':root\[data-theme="%s"\]' % name)
        block = re.search(sel + r" \{(.*?)\n\}", css, re.S).group(1)
        out[name] = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{3,6})\b", block))
    return out


@pytest.mark.parametrize("theme", ["light", "dark", "blue", "green", "contrast", "sepia"])
def test_theme_tokens_meet_contrast(theme):
    t = _themes()[theme]
    for tok in ("--accent-text", "--danger-text", "--input-border"):
        assert tok in t, (theme, tok)
    # P7: рамка на полето ≥3:1 спрямо фона на полето и картата (WCAG 1.4.11)
    assert _ratio(t["--input-border"], t["--input-bg"]) >= 3, theme
    assert _ratio(t["--input-border"], t["--card-bg"]) >= 3, theme
    # A1/A5: акцент и опасност КАТО ТЕКСТ ≥4.5:1
    for bg in ("--bg", "--card-bg"):
        assert _ratio(t["--accent-text"], t[bg]) >= 4.5, (theme, bg)
        assert _ratio(t["--danger-text"], t[bg]) >= 4.5, (theme, bg)
    assert _ratio(t["--danger-text"], t["--danger-soft"]) >= 4.5, theme


def test_new_tokens_defined_for_every_theme():
    css = _css()
    for tok in ("--ro-bg", "--sticky-bg"):
        assert css.count(tok + ":") >= 6, tok


def test_text_uses_text_tokens_not_button_tokens():
    css = _css()
    assert "a { color: var(--accent-text);" in css
    assert re.search(r"\.btn-outline \{[^}]*color: var\(--accent-text\)", css)
    for name in ("my_settings", "materials", "_macros"):
        src = read_source("templates", name + ".html")
        assert not re.search(r"(?<!border-)color:var\(--danger\)", src), name


def test_layout_rules():
    css = _css()
    # P1: по-широк контейнер и компактни отстъпи без горна граница
    assert ".container { max-width: 1480px;" in css
    assert "(max-width: 1599px)" not in css
    # B2: полето в лентата побеждава общото input[type=text]
    m = re.search(r"\.sidebar-scan input\[type=text\] \{([^}]*)\}", css)
    assert m and "background: transparent" in m.group(1) and "border: 0" in m.group(1)
    assert ".sidebar-scan:focus-within" in css
    # P10/B16: затворената лента няма сянка
    block = css[css.index("@media (max-width: 980px) {\n  .app-shell"):]
    sidebar = re.search(r"  \.sidebar \{([^}]*)\}", block).group(1)
    assert "box-shadow" not in sidebar
    assert re.search(r"\.sidebar\.open \{[^}]*box-shadow", block)
    # P8: sticky лентата на документа и overflow, който не я убива
    assert re.search(r"@media screen and \(min-width: 1100px\) \{\s*\.container-print \{ overflow-x: visible; \}", css)
    # P4: лепкава лента на формите с токена на темата
    assert re.search(r"form \.card > \.actions:last-child \{[^}]*position: sticky;[^}]*var\(--sticky-bg\)", css, re.S)
    # P6: само за четене
    assert re.search(r"input\[readonly\][^{]*\{[^}]*var\(--ro-bg\)[^}]*border-style: dashed", css, re.S)
    assert ".status-dot--warn { background: #b7791f; }" in css
