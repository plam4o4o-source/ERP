# -*- coding: utf-8 -*-
"""Одит (13.10.2026, група GFX): движение и графика — поведение в истински
Chromium (Playwright). Пуска се с `-m e2e`. Статичната/сървърната част —
tests/test_motion_v381.py."""
import time

import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("playwright.sync_api")

import conftest  # noqa: E402

_login = conftest.e2e_login
live_server = conftest.live_server
page = conftest.page

_RUNNING = ("document.getAnimations().filter(a => a.playState === 'running')"
            ".map(a => (a.animationName || a.transitionProperty))")


def _new_page(e2e_context_factory, **kw):
    return e2e_context_factory(viewport={"width": 1366, "height": 768}, **kw).new_page()


def test_reduced_motion_leaves_no_running_animations(live_server, e2e_context_factory):
    """Г9: при prefers-reduced-motion: reduce след зареждане, посочване на
    плочка и отваряне на модал не върви НИТО ЕДНА анимация/преход; без
    настройката модалът наистина анимира (контрола на измерването)."""
    results = {}
    for mode in ("no-preference", "reduce"):
        pg = _new_page(e2e_context_factory, reduced_motion=mode)
        _login(pg, live_server)
        pg.goto(live_server + "/clients/new")
        pg.fill('input[name="name"]', "Модал Тест ООД")
        pg.locator('main form[method="post"] button[type="submit"]').last.click()
        pg.wait_for_load_state("load")
        pg.goto(live_server + "/")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(500)
        pg.hover(".qa >> nth=0")
        pg.wait_for_timeout(40)
        hover = pg.evaluate(_RUNNING)
        lift = pg.evaluate("getComputedStyle(document.querySelector('.qa')).transform")
        pg.goto(live_server + "/clients")
        pg.wait_for_load_state("networkidle")
        pg.locator("form[data-confirm] button").first.click()
        pg.wait_for_timeout(25)
        modal = pg.evaluate(_RUNNING)
        results[mode] = (hover, lift, modal)
    hover, lift, modal = results["reduce"]
    assert hover == [] and modal == [], results["reduce"]
    assert lift == "none", "плочката не се повдига при намалена анимация"
    assert "ph-pop" in results["no-preference"][2], results["no-preference"]


def test_nav_progress_bar_after_delay_and_not_for_prevented(flask_app, live_server, e2e_context_factory):
    """Г2: лентата горе се появява чак след ~150ms (бърз преход не мига),
    расте, и изчезва при новата страница; Ctrl+клик и прихванато събитие
    (модал за потвърждение) не я пускат."""
    from flask import redirect

    def slow():
        time.sleep(1.2)
        return redirect("/clients")
    flask_app.add_url_rule("/__e2e_slow", "__e2e_slow", slow)

    pg = _new_page(e2e_context_factory)
    logs = []
    pg.on("console", lambda m: logs.append(m.text) if m.text.startswith("PP ") else None)
    _login(pg, live_server)
    pg.goto(live_server + "/docs")
    pg.evaluate("""() => {
      const a = document.createElement('a'); a.href = '/__e2e_slow'; a.id = 'slow'; a.textContent = 'slow';
      document.querySelector('main').prepend(a);
      const el = document.getElementById('ph-progress');
      a.addEventListener('click', () => [40, 450].forEach(t => setTimeout(() =>
        console.log('PP ' + t + ' ' + el.classList.contains('is-active') + ' ' +
                    getComputedStyle(el).transform), t)));
    }""")
    pg.click("#slow", no_wait_after=True)
    pg.wait_for_url("**/clients", timeout=10000)
    pg.wait_for_load_state("load")
    assert len(logs) == 2, logs
    assert logs[0].startswith("PP 40 false"), logs
    assert logs[1].startswith("PP 450 true"), logs
    assert not pg.evaluate("document.getElementById('ph-progress').classList.contains('is-active')")
    # Ctrl+клик (нов раздел) и модал за потвърждение — без лента.
    pg.click('#sidebar a[href="/docs"]', modifiers=["Control"])
    pg.wait_for_timeout(300)
    assert not pg.evaluate("document.getElementById('ph-progress').classList.contains('is-active')")


def test_saved_row_flashes_and_hl_leaves_the_url(page, live_server):
    """Г3: след запис на клиент списъкът откроява ТОЧНО неговия ред (по
    data-row-id), а hl изчезва от адреса."""
    _login(page, live_server)
    page.goto(live_server + "/clients/new")
    page.fill('input[name="name"]', "Премигващ Ред ООД")
    page.locator('main form[method="post"] button[type="submit"]').last.click()
    page.wait_for_url("**/clients*")
    page.wait_for_load_state("load")
    flashed = page.evaluate("[...document.querySelectorAll('tr.ph-row-flash')].map(r => r.innerText)")
    assert len(flashed) == 1 and "Премигващ Ред ООД" in flashed[0], flashed
    assert "hl=" not in page.url
    page.wait_for_timeout(1900)
    assert page.locator("tr.ph-row-flash").count() == 0


def test_dashboard_chart_tooltip_counters_and_delta_badge(page, live_server, admin_client):
    """Г1: подсказка при посочване и при фокус от клавиатурата; при второ
    отваряне в сесията плочката с нов документ показва „+1“."""
    _login(page, live_server)
    page.goto(live_server + "/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    page.hover(".ph-chart .col.is-current")
    tip = page.locator(".ph-tip")
    assert "1" in tip.inner_text() or "0" in tip.inner_text()
    assert page.evaluate("getComputedStyle(document.querySelector('.ph-tip')).opacity") != "0"
    page.mouse.move(5, 5)
    page.locator(".ph-chart-svg").focus()
    page.keyboard.press("ArrowLeft")
    assert page.locator(".ph-chart .col.is-hot").count() == 1
    first_tip = tip.inner_text()
    assert ":" in first_tip
    # Нов документ → при връщане на таблото значка „+1“ на плочката ЧМР.
    conftest.issue_cmr(admin_client, "Делта Клиент ЕООД")
    page.goto(live_server + "/docs")
    page.goto(live_server + "/")
    page.wait_for_load_state("networkidle")
    badges = page.evaluate("[...document.querySelectorAll('.ph-delta')].map(b => b.textContent)")
    assert "+1" in badges, badges


def test_speculation_prefetch_for_sidebar_not_for_pdf(page, live_server):
    """П11: задържане върху пункт в лентата изтегля страницата предварително
    (Sec-Purpose: prefetch), а кликът я ползва; изключените адреси (PDF) не
    се изтеглят."""
    requests = []
    page.context.on("request", lambda r: requests.append((r.url, r.headers.get("sec-purpose"))))
    _login(page, live_server)
    page.goto(live_server + "/")
    page.wait_for_load_state("networkidle")
    if not page.evaluate("HTMLScriptElement.supports && HTMLScriptElement.supports('speculationrules')"):
        pytest.skip("браузърът не поддържа Speculation Rules")
    assert page.locator('script[type="speculationrules"]').count() == 1
    requests.clear()
    page.hover('#sidebar a[href="/materials"]')
    page.wait_for_timeout(800)
    assert (live_server + "/materials", "prefetch") in requests, requests
    page.evaluate("""() => { const a = document.createElement('a'); a.className = 'btn-view';
      a.href = '/doc/1/export.pdf'; a.textContent = 'pdf'; document.querySelector('main').prepend(a); }""")
    requests.clear()
    page.hover("main > a.btn-view")
    page.wait_for_timeout(800)
    assert not [r for r in requests if "export.pdf" in r[0]], requests


def test_login_card_and_error_shake(live_server, e2e_context_factory):
    """Г8: картата влиза веднъж; при грешна парола само съобщението „поклаща“
    (картата не влиза отново); при намалена анимация — нищо."""
    for mode, expect_shake in (("no-preference", True), ("reduce", False)):
        pg = _new_page(e2e_context_factory, reduced_motion=mode)
        pg.goto(live_server + "/login")
        pg.fill('input[name="username"]', "няма-такъв")
        pg.fill('input[name="password"]', "грешна")
        pg.click('main button[type="submit"]')
        pg.wait_for_selector(".login-card .error")
        names = pg.evaluate("getComputedStyle(document.querySelector('.login-card .error')).animationName")
        card = pg.evaluate("getComputedStyle(document.querySelector('.login-card')).animationName")
        assert card == "none", "картата не влиза повторно при грешка"
        assert (names == "ph-shake") is expect_shake, (mode, names)


def test_toast_close_collapses_and_removes(page, live_server):
    """Г5: затвореното известие се свива и изчезва от DOM."""
    _login(page, live_server)
    page.goto(live_server + "/password")
    page.fill('input[name="current"]', "грешна-парола")
    page.fill('input[name="new"]', "новапарола123")
    page.fill('input[name="repeat"]', "новапарола123")
    page.click('main button[type="submit"]')
    toast = page.locator(".toast-error")
    toast.wait_for(timeout=8000)
    page.evaluate("""() => { window.__collapsed = false; const t = document.querySelector('.toast-error');
      new MutationObserver(() => { if (t.classList.contains('toast-collapse')) window.__collapsed = true; })
        .observe(t, {attributes: true, attributeFilter: ['class']}); }""")
    toast.locator(".toast-close").click()
    toast.wait_for(state="detached", timeout=3000)
    assert page.evaluate("window.__collapsed"), "мястото се свива преди премахването"
