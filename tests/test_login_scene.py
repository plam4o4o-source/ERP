# -*- coding: utf-8 -*-
"""Тестове за анимираната логистична сцена на входния екран — заявка:
„синия фон на началния екран при входа... анимиран, да има движещ камион,
летящ самолет, кораб който плава“.

Тук е сървърната/статичната част (маркъп + CSS правила); самото ДВИЖЕНИЕ
(изчислените CSS анимации в реален браузър) е покрито с e2e тест в
tests/test_e2e_smoke.py."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _login_body(client):
    return client.get("/login").data.decode()


def test_login_page_has_the_scene_with_truck_plane_and_ship(client):
    body = _login_body(client)
    assert 'class="login-scene"' in body
    for cls in ("scene-truck", "scene-plane", "scene-ship", "scene-waves", "scene-road"):
        assert cls in body, cls


def test_scene_is_hidden_from_screen_readers(client):
    """Сцената е чисто декоративна — не бива екранен четец да обявява
    четири SVG картинки преди формата за вход."""
    assert '<div class="login-scene" aria-hidden="true">' in _login_body(client)


def test_scene_sits_behind_the_login_card(client):
    """Сцената идва ПРЕДИ картата в HTML-а, а картата е с по-висок z-index
    (в style.css) — формата за вход остава използваема върху анимацията."""
    body = _login_body(client)
    assert body.index('class="login-scene"') < body.index('class="login-card"')


def test_scene_css_animates_and_respects_reduced_motion():
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    # Пътуването през екрана съществува и е закачено за трите елемента.
    assert "@keyframes scene-cross-right" in css
    assert "@keyframes scene-cross-left" in css
    for cls in (".scene-truck", ".scene-plane", ".scene-ship"):
        assert cls in css, cls
    # При изключени анимации в операционната система сцената застива —
    # изброена е в prefers-reduced-motion блока.
    reduced = css.split("prefers-reduced-motion")[1].split("}")[0] + "}"
    reduced_block = css.split("prefers-reduced-motion: reduce)")[1].split("html {")[0]
    assert ".scene-truck" in reduced_block
    assert ".scene-plane" in reduced_block
    assert ".scene-ship" in reduced_block
