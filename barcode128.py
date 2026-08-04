# -*- coding: utf-8 -*-
"""Генератор на Code128 (набор B) баркодове като SVG — без външни зависимости."""

# Таблица с ширини на модулите за Code128 (стойности 0-105 + STOP)
_WIDTHS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232",
]
_STOP = "2331112"
_START_B = 104


def code128_svg(text, module_width=2, height=55, font_size=13, show_text=True,
                responsive=False):
    """Връща SVG низ с Code128-B баркод за подадения текст (ASCII 32-126).

    При responsive=True SVG елементът получава width="100%" вместо
    фиксирана стойност в пиксели (viewBox се запазва) — така баркодът се
    смалява пропорционално, за да се събере в контейнера си, вместо да
    прелива извън тесни полета в бланките, независимо от дължината на текста.
    """
    values = [_START_B]
    for ch in text:
        code = ord(ch)
        if not 32 <= code <= 126:
            raise ValueError("Code128-B поддържа само ASCII 32-126: %r" % ch)
        values.append(code - 32)

    checksum = values[0]
    for i, v in enumerate(values[1:], start=1):
        checksum += i * v
    values.append(checksum % 103)

    pattern = "".join(_WIDTHS[v] for v in values) + _STOP

    quiet = 10 * module_width
    x = quiet
    bars = []
    is_bar = True
    for w in pattern:
        w_px = int(w) * module_width
        if is_bar:
            bars.append('<rect x="%d" y="0" width="%d" height="%d" fill="#000"/>'
                        % (x, w_px, height))
        x += w_px
        is_bar = not is_bar

    total_width = x + quiet
    text_h = font_size + 6 if show_text else 0
    total_height = height + text_h

    width_attr = 'width="100%%"' if responsive else 'width="%d"' % total_width
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" %s height="%d" '
        'viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="%s">'
        % (width_attr, total_height, total_width, total_height, text),
        '<rect width="%d" height="%d" fill="#fff"/>' % (total_width, total_height),
    ]
    parts.extend(bars)
    if show_text:
        parts.append(
            '<text x="%d" y="%d" text-anchor="middle" '
            'font-family="monospace" font-size="%d" fill="#000">%s</text>'
            % (total_width // 2, height + font_size + 2, font_size, text)
        )
    parts.append("</svg>")
    return "".join(parts)
