# -*- coding: utf-8 -*-
"""Логото на създателя на програмата (заявка 05.10.2026: „добави и логото на
създателя в програмата, премахни имената ми“) — знакът е на входа и в
страничната лента, без име; имената не се срещат в продукта."""
import os
import re

from PIL import Image

from conftest import ROOT

LOGO = os.path.join(ROOT, "static", "creator_logo.png")


def test_creator_logo_is_a_small_transparent_png():
    img = Image.open(LOGO)
    assert img.format == "PNG" and img.mode == "RGBA"
    # прозрачен фон — еднакво добре на тъмната лента и на светлата карта
    assert img.getpixel((0, 0))[3] == 0
    assert img.width <= 600 and os.path.getsize(LOGO) < 200 * 1024


def test_login_page_shows_the_creator_logo(client):
    body = client.get("/login").get_data(as_text=True)
    assert re.search(r'class="login-creator"[^>]*>\s*<img src="[^"]*creator_logo\.png', body)


def test_sidebar_shows_the_creator_logo(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    aside = body[body.index("<aside"):body.index("</aside>")]
    assert "creator_logo.png" in aside and 'class="sidebar-creator"' in aside


def test_creator_names_are_not_in_the_product():
    names = re.compile(r"пламен|plamen|христов|hristov", re.I)
    hits = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "bin", "dist", "build")]
        for name in files:
            if not name.endswith((".py", ".html", ".js", ".css", ".iss", ".txt", ".md", ".po", ".yml", ".bat", ".cfg", ".ini")) \
                    and name != "LICENSE":
                continue
            path = os.path.join(base, name)
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                if names.search(fh.read()):
                    hits.append(os.path.relpath(path, ROOT))
    assert hits == []
