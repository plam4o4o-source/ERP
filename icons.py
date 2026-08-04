# -*- coding: utf-8 -*-
"""Малък вграден набор от линийни SVG икони (без външни зависимости/CDN).

Всяка икона е поредица от прости фигури (правоъгълници, кръгове, линии) в
viewBox 0 0 24 24, начертани с currentColor, за да следват цвета на текста
и избраната тема автоматично.
"""
from markupsafe import Markup

_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/>'
                 '<rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/>'
                 '<rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/>'
                 '<rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/>',
    "truck": '<rect x="1.5" y="8" width="12" height="8.5" rx="1"/>'
             '<path d="M13.5 11h4l3 3v2.5h-7z"/>'
             '<circle cx="6" cy="19" r="2"/><circle cx="17" cy="19" r="2"/>'
             '<path d="M1.5 19h2.5M19 19h2.7"/>',
    "box": '<path d="M12 3 3 7.5v9L12 21l9-4.5v-9z"/>'
           '<path d="M3 7.5 12 12l9-4.5M12 12v9"/>',
    "layers": '<rect x="3" y="4" width="18" height="4.2" rx="1"/>'
              '<rect x="3" y="9.9" width="18" height="4.2" rx="1"/>'
              '<rect x="3" y="15.8" width="18" height="4.2" rx="1"/>',
    "shield": '<path d="M12 2.5 4.5 5.5v6c0 5 3.2 8.3 7.5 10 4.3-1.7 7.5-5 7.5-10v-6z"/>'
              '<path d="M8.3 12.2l2.4 2.4 4.8-4.9"/>',
    "globe": '<circle cx="12" cy="12" r="9"/>'
             '<ellipse cx="12" cy="12" rx="4" ry="9"/>'
             '<path d="M3 12h18M4.3 7.5h15.4M4.3 16.5h15.4"/>',
    "folder": '<path d="M3 6.5c0-.8.7-1.5 1.5-1.5H9l2 2.2h8.5c.8 0 1.5.7 1.5 1.5v9'
              'c0 .8-.7 1.5-1.5 1.5h-15C3.7 19.2 3 18.5 3 17.7z"/>',
    "book": '<path d="M4 4.5c0-.6.5-1 1.2-1H12v17H5.2c-.7 0-1.2-.4-1.2-1z"/>'
            '<path d="M20 4.5c0-.6-.5-1-1.2-1H12v17h6.8c.7 0 1.2-.4 1.2-1z"/>'
            '<path d="M8 7h1.5M8 10h1.5M14.5 7H16M14.5 10H16"/>',
    "building": '<rect x="4" y="3" width="16" height="18" rx="1"/>'
                '<path d="M8 7h2M14 7h2M8 11h2M14 11h2M8 15h2M14 15h2"/>'
                '<rect x="10" y="17" width="4" height="4"/>',
    "users": '<circle cx="9" cy="8" r="3.3"/><path d="M3 20c0-3.5 2.7-6 6-6s6 2.5 6 6"/>'
             '<circle cx="17.5" cy="9" r="2.6"/><path d="M15.5 12.7c2.6.3 4.6 2.5 4.6 5.3"/>',
    "sliders": '<path d="M4 6h9M17 6h3M4 12h3M9 12h11M4 18h13M21 18h-2"/>'
               '<circle cx="12" cy="6" r="2"/><circle cx="7" cy="12" r="2"/>'
               '<circle cx="18" cy="18" r="2"/>',
    "server": '<rect x="3" y="4" width="18" height="6" rx="1.3"/>'
              '<rect x="3" y="14" width="18" height="6" rx="1.3"/>'
              '<circle cx="7" cy="7" r="0.9" fill="currentColor" stroke="none"/>'
              '<circle cx="7" cy="17" r="0.9" fill="currentColor" stroke="none"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="1.6"/>'
            '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5"/>',
    "logout": '<path d="M9 4H5.5a1.5 1.5 0 0 0-1.5 1.5v13A1.5 1.5 0 0 0 5.5 20H9"/>'
              '<path d="M13 12h8M17.5 8l3.5 4-3.5 4"/>',
    "scan": '<path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8"/>'
            '<path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20H8M16 20h2.5a1.5 1.5 0 0 0 1.5-1.5V16"/>'
            '<path d="M6.5 9v6M9.5 9v6M12 9v6M15 9v6M17.5 9v6" stroke-width="1.6"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20 20l-4.8-4.8"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
           '<circle cx="12" cy="12" r="3"/>',
    "print": '<path d="M6.5 8.5V4h11v4.5"/>'
             '<rect x="3.5" y="8.5" width="17" height="8" rx="1.4"/>'
             '<path d="M6.5 15h11v5h-11z"/>',
    "trash": '<path d="M4 7h16M9.5 7V4.8c0-.5.4-.8.9-.8h3.2c.5 0 .9.3.9.8V7"/>'
             '<path d="M6.5 7 7.3 19.3c0 .8.7 1.4 1.5 1.4h6.4c.8 0 1.5-.6 1.5-1.4L17.5 7"/>',
    "check": '<path d="M4.5 12.5 9.5 17.5 19.5 6.5"/>',
    "download": '<path d="M12 3v12.5M7 11l5 5 5-5"/><path d="M4.5 18.5h15"/>',
    "chevron-down": '<path d="M5.5 8.5 12 15l6.5-6.5"/>',
    "menu": '<path d="M3.5 6.5h17M3.5 12h17M3.5 17.5h17"/>',
    "close": '<path d="M5.5 5.5 18.5 18.5M18.5 5.5 5.5 18.5"/>',
}


def render_icon(name, size=18, cls=""):
    body = _ICONS.get(name, "")
    class_attr = ' class="%s"' % cls if cls else ""
    return Markup(
        '<svg%s width="%d" height="%d" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">%s</svg>'
        % (class_attr, size, size, body)
    )
