# -*- coding: utf-8 -*-
"""PDF износ на документи (бутон „Изтегли PDF“ — задача 25 от заявката за
търсене/клиентски папки/PDF+Excel износ). Генерира PDF през xhtml2pdf (pisa)
от ЕДИН споделен, генеричен HTML/Jinja шаблон (templates/pdf_export.html),
който ПРЕИЗПОЛЗВА същите label/key речници като Excel износа
(routes_documents._XLSX_FIELDS / _XLSX_ITEM_COLUMNS, подадени тук отвън —
вижте export_document_pdf в routes_documents.py) — вместо 6 отделни
pixel-perfect PDF шаблона, огледални на печатните cmr_print.html и т.н.

Съзнателен компромис: xhtml2pdf има слаба поддръжка на CSS grid/flexbox, а
печатните шаблони масово ги ползват (static/style.css, десетки срещания) —
пренасянето им 1:1 в PDF би било голяма и рискова инвестиция. PDF-ът затова
прилича на „хубаво форматирано копие на Excel износа“, не на пиксел-копие на
печатната бланка — за случая „искам точно както изглежда на екран“ си остава
браузърният диалог за печат (бутон „Печат / PDF“, doc_toolbar).

Два рендиращи проблема на xhtml2pdf, заради които тук НЕ е просто
"render_template + pisa.CreatePDF":

1. Кирилица — вграденият шрифт по подразбиране (Helvetica, вграден в
   reportlab) няма кирилски глифи; xhtml2pdf/reportlab го рисува като
   плътни черни правоъгълници вместо букви. Решение: @font-face към DejaVu
   Sans (fonts/DejaVuSans*.ttf, вижте fonts/README.md за лиценза),
   регистриран директно в CSS на pdf_export.html чрез абсолютен път
   (_font_dir() по-долу).
2. Баркод — печатните шаблони вграждат баркода като вложен <svg>/<rect>
   (barcode128.code128_svg), но xhtml2pdf НЕ рисува вложен SVG маркъп като
   истинска векторна графика (само случайно "изтичащ" вложен <text> се
   вижда). Решение: растеров вариант (barcode128.code128_png_data_uri),
   вграден като base64 data: URI <img> — вижда се и не изисква никаква
   допълнителна нативна/бинарна зависимост извън вече наличния Pillow."""
import io
import os
import sys

from flask import render_template
from xhtml2pdf import pisa

import applog
from barcode128 import code128_png_data_uri


def _font_dir():
    """Папката с DejaVu Sans шрифтовете — огледално на config.py._BASE_DIR
    (frozen .exe: до временната PyInstaller папка sys._MEIPASS, точно както
    templates/static, виж appcore.create_app; иначе: до този файл в
    изходния код). Виж .github/workflows/release.yml (--add-data
    "fonts;fonts") и fonts/README.md."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "fonts")


def generate_document_pdf(title, number, barcode, fields, items, item_columns, totals_row=None):
    """Връща готовия PDF файл (bytes) за един документ.

    fields: списък от (label, value) двойки — точно каквото Excel износа
        показва за документа (виж routes_documents.export_document_xlsx).
    items: списък от dict-ове с редовете артикули на документа, или []
        ако документният тип няма редове (напр. декларациите).
    item_columns: списък от (key, label) двойки за таблицата с редовете —
        празен списък пропуска таблицата с редове изцяло.
    totals_row: одит (находка С2) — списък стойности, подравнени 1:1 по
        item_columns (виж routes_documents._invoice_export_totals_row),
        отпечатван като допълнителен удебелен ред TOTAL под редовете
        артикули (само за фактури); None пропуска реда изцяло, точно
        както при документи без такъв ред (напр. опаковъчен лист).
    """
    barcode_uri = code128_png_data_uri(barcode) if barcode else None
    html = render_template(
        "pdf_export.html",
        title=title,
        number=number,
        barcode_uri=barcode_uri,
        fields=fields,
        items=items or [],
        item_columns=item_columns or [],
        totals_row=totals_row,
        font_dir=_font_dir(),
    )
    out = io.BytesIO()
    try:
        result = pisa.CreatePDF(src=html, dest=out, encoding="utf-8")
    except Exception as exc:
        # xhtml2pdf/reportlab понякога хвърлят СУРОВО изключение дълбоко в
        # собствения си layout код (reportlab.platypus.tables), не просто
        # връщат ненулево .err — открито наживо: документ с много колони
        # в таблицата с редове (напр. фактура за Норвегия) И дълга
        # неразделима стойност (код на материал/описание без интервали) в
        # някоя от тях кара reportlab да пресметне отрицателна свободна
        # ширина за таблицата и да гръмне с ValueError/TypeError, което
        # преди стигаше НЕуловено до Flask → суров "Internal Server Error"
        # на потребителя, без никакъв следа в лог (виж CHANGELOG — оправено
        # с изрична ширина на колоните в pdf_export.html; тази защита тук е
        # ВТОРА линия за евентуален бъдещ подобен случай, не заместител на
        # истинската поправка). Логваме ПЪЛНИЯ traceback (стига до
        # pacho_startup.log в компилирания .exe, виж applog.py), за да е
        # диагностируемо следващия път, вместо отново да гадаем.
        applog.log_exception("pdf_export.generate_document_pdf: xhtml2pdf/reportlab гръмна")
        raise RuntimeError("PDF генерирането е неуспешно (%s: %s)" % (type(exc).__name__, exc)) from exc
    if result.err:
        # xhtml2pdf не хвърля изключение при "мека" грешка в рендирането,
        # само връща ненулево .err — превръщаме го в изключение, за да не
        # се свали "PDF" файл от 0 байта на потребителя без обяснение.
        raise RuntimeError("PDF генерирането е неуспешно (xhtml2pdf err=%r)" % result.err)
    return out.getvalue()
