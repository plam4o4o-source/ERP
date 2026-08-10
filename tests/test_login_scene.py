# -*- coding: utf-8 -*-
"""Тестове за анимираната логистична сцена на входния екран.

Заявка 1 (v3.53.0): „синия фон на началния екран при входа... анимиран,
да има движещ камион, летящ самолет, кораб който плава“.
Заявка 2 (v3.54.0): „подобри анимациите, и фонът също, направи го
реалистично, запази този и добави опция да може да се сменя в
настройките“ — оттук ДВА изгледа (db.LOGIN_SCENES):

  - „realistic“ (подразбиране) — здрачно небе със звезди/облаци, море с
    два пласта вълни, самолет със следа и мигаща светлина, кораб с пушек,
    камион с въртящи се колела по асфалтов път;
  - „classic“ — първоначалните бели силуети (запазени по заявка).

Тук е сървърната/статичната част (маркъп + CSS правила + настройката);
самото ДВИЖЕНИЕ (изчислените CSS анимации в реален браузър) е покрито с
e2e тест в tests/test_e2e_smoke.py."""
import os

from conftest import post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _set_scene(db_module, value):
    con = db_module.get_db()
    db_module.save_settings(con, {"login_scene": value})
    con.commit()
    con.close()


def _login_body(client):
    return client.get("/login").data.decode()


# ---------------------------------------------------------------- реалистичната (подразбиране)

def test_login_page_defaults_to_the_realistic_scene(client):
    body = _login_body(client)
    assert 'class="login-scene rs"' in body
    for cls in ("rs-truck", "rs-plane", "rs-ship", "rs-road", "rs-sun",
                "rs-stars", "rs-cloud", "rs-wave-back", "rs-wave-front",
                "rs-smoke", "rs-beacon"):
        assert cls in body, cls
    # Класическите силуети ги НЯМА едновременно с реалистичната сцена.
    assert "scene-truck" not in body


def test_realistic_truck_has_spinning_wheels_markup(client):
    """Колелата са отделни SVG групи (.rs-wheel) — точно тях върти
    rs-wheel-spin анимацията (transform-box: fill-box в style.css)."""
    body = _login_body(client)
    assert body.count('class="rs-wheel"') == 4


def test_invalid_scene_value_falls_back_to_realistic(client, db_module):
    _set_scene(db_module, "няма-такава-сцена")
    assert 'class="login-scene rs"' in _login_body(client)


# ---------------------------------------------------------------- класическата (по избор)

def test_classic_scene_still_available_when_selected(client, db_module):
    """Заявка: „запази този“ — класическите силуети остават избираеми."""
    _set_scene(db_module, "classic")
    body = _login_body(client)
    for cls in ("scene-truck", "scene-plane", "scene-ship", "scene-waves", "scene-road"):
        assert cls in body, cls
    assert 'class="login-scene rs"' not in body


def test_both_scenes_are_hidden_from_screen_readers(client, db_module):
    """Сцените са чисто декоративни — не бива екранен четец да ги обявява
    преди формата за вход."""
    assert '<div class="login-scene rs" aria-hidden="true">' in _login_body(client)
    _set_scene(db_module, "classic")
    assert '<div class="login-scene" aria-hidden="true">' in _login_body(client)


def test_scene_sits_behind_the_login_card(client):
    """Сцената идва ПРЕДИ картата в HTML-а, а картата е с по-висок z-index
    (в style.css) — формата за вход остава използваема върху анимацията."""
    body = _login_body(client)
    assert body.index('class="login-scene') < body.index('class="login-card"')


# ---------------------------------------------------------------- настройката

def test_settings_page_offers_the_scene_choice(admin_client):
    body = admin_client.get("/my-settings").data.decode()
    assert 'name="login_scene"' in body
    assert 'value="realistic"' in body
    assert 'value="classic"' in body
    # Подразбирането е отбелязано, докато нищо не е запазвано.
    block = body.split('value="realistic"')[1].split(">")[0]
    assert "checked" in block


def test_admin_can_switch_the_scene_and_login_page_follows(admin_client, client):
    resp = post_with_csrf(admin_client, "/admin/system",
                          {"form": "login_scene", "login_scene": "classic"},
                          csrf_source_url="/my-settings", follow_redirects=True)
    assert "Изгледът на входния екран е запазен." in resp.data.decode()
    assert "scene-truck" in client.get("/login").data.decode()

    post_with_csrf(admin_client, "/admin/system",
                   {"form": "login_scene", "login_scene": "realistic"},
                   csrf_source_url="/my-settings", follow_redirects=False)
    assert 'class="login-scene rs"' in client.get("/login").data.decode()


def test_saving_an_invalid_scene_value_stores_the_default(admin_client, client, db_module):
    post_with_csrf(admin_client, "/admin/system",
                   {"form": "login_scene", "login_scene": "измислена"},
                   csrf_source_url="/my-settings", follow_redirects=False)
    con = db_module.get_db()
    stored = db_module.get_settings(con).get("login_scene")
    con.close()
    assert stored == db_module.DEFAULT_LOGIN_SCENE
    assert 'class="login-scene rs"' in client.get("/login").data.decode()


def test_scene_setting_requires_admin(employee_client):
    resp = post_with_csrf(employee_client, "/admin/system",
                          {"form": "login_scene", "login_scene": "classic"},
                          csrf_source_url="/", follow_redirects=False)
    assert resp.status_code in (302, 403)


# ---------------------------------------------------------------- CSS правилата

def test_scene_css_animates_and_respects_reduced_motion():
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    # Пътуването през екрана съществува и за двете сцени.
    assert "@keyframes scene-cross-right" in css
    assert "@keyframes scene-cross-left" in css
    # Специфичните реалистични анимации: въртящи се колела, мигаща
    # светлина, пушек, трепкащи звезди.
    for kf in ("@keyframes rs-wheel-spin", "@keyframes rs-beacon",
               "@keyframes rs-smoke", "@keyframes rs-twinkle"):
        assert kf in css, kf
    assert "transform-box: fill-box" in css, "въртенето е около центъра на колелото"
    # При изключени анимации в операционната система И ДВЕТЕ сцени
    # застиват — изброени са в prefers-reduced-motion блока.
    reduced_block = css.split("prefers-reduced-motion: reduce)")[1].split("html {")[0]
    for cls in (".scene-truck", ".scene-plane", ".scene-ship",
                ".rs-truck", ".rs-plane", ".rs-ship", ".rs-wheel"):
        assert cls in reduced_block, cls
