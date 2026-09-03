# -*- coding: utf-8 -*-
"""Генератор на Code128 (набор B) баркодове — SVG (за печатните HTML/print
шаблони, вижда се директно в браузъра) и PNG (за PDF износа, pdf_export.py —
xhtml2pdf/reportlab НЕ рисува вложен <svg>/<rect> маркъп като истинска
векторна графика, само случайно "изтичащ" вложен <text>, затова там баркодът
трябва да е растерово изображение, вградено като data: URI, виж
code128_png_data_uri по-долу)."""

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

# Замени за XML/HTML специалните знаци — Code128-B е ASCII 32-126 (виж
# _pattern по-долу), който включва &, <, >, " и ' — всичките значими в
# XML/SVG контекст. code128_svg вгражда `text` директно в SVG атрибут
# (aria-label) и текстов елемент, а резултатът се маркира като safe Markup
# при употреба в шаблоните (appcore.barcode_filter) — БЕЗ това екраниране,
# баркод текст като `"><script>...` (напр. през /barcode/<code>.svg, където
# code идва направо от URL адреса) би се вмъкнал като истински HTML/SVG в
# страницата (XSS), не просто показан като текст. Открито при затягане на
# bandit бариерата в CI (B704: markupsafe.Markup на невалидирани данни).
_XML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
               ('"', "&quot;"), ("'", "&apos;"))


def _xml_escape(text):
    for ch, esc in _XML_ESCAPES:
        text = text.replace(ch, esc)
    return text


def _pattern(text):
    """Изчислява поредицата от ширини на модулите (низ от цифри 1-4,
    редуващи се лента/празнина, вкл. checksum и STOP) за Code128-B на
    подадения текст. Споделено от code128_svg и code128_png_data_uri, за да
    няма две копия на checksum/pattern алгоритъма."""
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

    return "".join(_WIDTHS[v] for v in values) + _STOP


def code128_svg(text, module_width=2, height=55, font_size=13, show_text=True,
                responsive=False):
    """Връща SVG низ с Code128-B баркод за подадения текст (ASCII 32-126).

    При responsive=True SVG елементът получава width="100%" вместо
    фиксирана стойност в пиксели (viewBox се запазва) — така баркодът се
    смалява пропорционално, за да се събере в контейнера си, вместо да
    прелива извън тесни полета в бланките, независимо от дължината на текста.
    """
    pattern = _pattern(text)
    safe_text = _xml_escape(text)

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

    width_attr = 'width="100%"' if responsive else 'width="%d"' % total_width
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" %s height="%d" '
        'viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="%s">'
        % (width_attr, total_height, total_width, total_height, safe_text),
        '<rect width="%d" height="%d" fill="#fff"/>' % (total_width, total_height),
    ]
    parts.extend(bars)
    if show_text:
        parts.append(
            '<text x="%d" y="%d" text-anchor="middle" '
            'font-family="monospace" font-size="%d" fill="#000">%s</text>'
            % (total_width // 2, height + font_size + 2, font_size, safe_text)
        )
    parts.append("</svg>")
    return "".join(parts)


def code128_png_data_uri(text, module_width=2, height=50):
    """Връща Code128-B баркод като PNG, base64-кодиран в data: URI (готов за
    директно вграждане в <img src="...">). Ползва СЪЩАТА pattern-таблица
    като code128_svg (_pattern по-горе), но рисува с Pillow вместо SVG —
    xhtml2pdf (PDF износ, pdf_export.py) не поддържа истинско рендиране на
    вложен <svg>/<rect> маркъп, само PNG/JPEG изображения."""
    import base64
    import io

    from PIL import Image, ImageDraw

    pattern = _pattern(text)
    quiet = 10 * module_width
    total_width = quiet + sum(int(w) * module_width for w in pattern) + quiet

    img = Image.new("RGB", (total_width, height), "white")
    draw = ImageDraw.Draw(img)
    x = quiet
    is_bar = True
    for w in pattern:
        w_px = int(w) * module_width
        if is_bar:
            # Одит (03.09.2026, находка №2): `-1` по двете оси. PIL рисува
            # правоъгълник ВКЛЮЧИТЕЛНО крайната координата, затова всяка
            # лента излизаше с 1 пиксел по-широка от модула си, а следващата
            # празнина — с 1 пиксел по-тясна (следващият елемент започва на
            # `x + w_px`, който вече е черен). При module_width=2 това е
            # систематична грешка от ±0.5 модула на ВСЕКИ елемент — далеч
            # извън толеранса на Code128, тоест баркодът в PDF износа не се
            # разчиташе от скенер. Проверено с изпълнение: ширините в PNG-а
            # бяха 5,1,3,3,3,7,7,… (нито една кратна на 2), докато в SVG-а
            # на печатната бланка са 4,2,2,6,6,4,2 — затова хартиената
            # бланка се сканираше, а PDF-ът не.
            draw.rectangle([x, 0, x + w_px - 1, height - 1], fill="black")
        x += w_px
        is_bar = not is_bar

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
