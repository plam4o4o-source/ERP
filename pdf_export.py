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
import tempfile
import threading
import weakref

from flask import render_template
from xhtml2pdf import pisa
import xhtml2pdf.files as _pisa_files

import applog
from barcode128 import code128_png_data_uri


# Одит (19.08.2026, находка №4): xhtml2pdf НЕ е безопасен за паралелна
# употреба, въпреки вида си. Класът `xhtml2pdf.files.TmpFiles` наследява
# `threading.local`, но държи списъка с временни файлове като ClassVar с
# изменяема стойност по подразбиране (`files: ClassVar[list] = []`) —
# `self.files.append(...)` намира АТРИБУТА НА КЛАСА и пише в един общ
# списък, споделен от всички нишки. Проверено с изпълнение: нишка №2
# вижда файла, регистриран от нишка №1.
#
# Последствието у нас: `pisaDocument()` вика `cleanFiles()` в самия си
# край, а тя затваря и трие ВСИЧКО в общия списък. Двама служители,
# натиснали „Изтегли PDF“ едновременно (waitress работи с 8 нишки),
# си трият взаимно временното копие на DejaVu — reportlab получава
# „TTFError: Can't open file …“ и потребителят вижда „PDF файлът не можа
# да се генерира“ вместо документ. Възпроизведено: 1 провал на 210
# заявки при 16 паралелни нишки.
#
# Сериализираме самото рендиране. Едно PDF отнема ~0.2 сек — за офисен
# обем (единични натискания на бутон) чакането е практически незабележимо,
# а алтернативите (кръпка върху чужд клас, за да стане наистина
# thread-local) са чувствително по-чупливи при следващо обновяване на
# xhtml2pdf.
_render_lock = threading.Lock()


def _silent_remove(path):
    """Изтрива файл, без да вдига шум — ползва се и от обвития close(), и
    от предпазния weakref.finalize (виж находка №5), затова двойното
    извикване трябва да е безопасно."""
    try:
        os.remove(path)
    except OSError:
        pass  # вече изтрит/заключен — не пречи на самото PDF генериране


def _windows_safe_get_named_tmp_file(self):
    """Замества xhtml2pdf.files.BaseFile.get_named_tmp_file (вижте
    monkeypatch-а веднага след дефиницията) — поправка на РЕАЛНО счупения
    PDF износ в Windows .exe версията.

    Одит (17.08.2026, открито от първото изпълнение на pytest портала на
    Windows runner в release.yml — ~20 PDF теста гърмяха там с
    „TTFError: Can't open file …\\Temp\\tmpXXXX.ttf“, при зелени същите
    тестове на Linux): оригиналът създава `tempfile.NamedTemporaryFile(
    suffix=...)` с ПОДРАЗБИРАЩОТО СЕ `delete=True`. На Windows това отваря
    файла с флага O_TEMPORARY, който ЗАБРАНЯВА на когото и да е друг да
    отвори същия файл ПО ИМЕ, докато оригиналната дръжка е отворена — а
    точно това прави веригата на зареждане на @font-face шрифта ни
    (templates/pdf_export.html → xhtml2pdf context.loadFont →
    reportlab TTFont(name, filename) → open(filename)): xhtml2pdf копира
    DejaVuSans*.ttf във временния файл, ДЪРЖИ дръжката отворена (виж
    files_tmp — „to prevent file close“) и подава ИМЕТО на reportlab,
    който на Windows получава PermissionError → TTFError → нашият
    generate_document_pdf вдига RuntimeError → бутонът „Изтегли PDF“ в
    реалното Windows приложение връща грешка ВИНАГИ. На Linux няма такова
    ограничение за споделяне, затова разработката/CI никога не го видяха.

    Поправката: `delete=False` (без O_TEMPORARY — файлът е отваряем по
    име от reportlab), а изтриването поемаме ние — обвитият `close()`
    трие файла след затваряне. xhtml2pdf вика `close()` на всичко в
    `files_tmp` чрез `cleanFiles()` в края на всеки `pisaDocument()`
    (вижте xhtml2pdf/files.py), значи временните файлове се чистят в
    СЪЩИЯ момент, в който и оригиналът ги чистеше — без изтичане.

    Прилага се БЕЗУСЛОВНО (не само на Windows), за да тества Linux CI
    точно същия код път, който реално се доставя в .exe-то."""
    data = self.get_data()
    tmp_file = tempfile.NamedTemporaryFile(suffix=self.suffix, delete=False)
    _orig_close = tmp_file.close

    def _close_and_remove():
        _orig_close()
        _silent_remove(tmp_file.name)

    tmp_file.close = _close_and_remove
    # Одит (19.08.2026, находка №5): предпазна мрежа срещу ИЗТИЧАНЕ.
    # С оригиналния `delete=True` всеки временен файл, изпуснат без явен
    # close() (изоставен при изключение по средата на рендирането, или
    # изхвърлен от общия списък от чужд cleanFiles()), се триеше от
    # финализатора на _TemporaryFileWrapper при събиране на боклука. С
    # `delete=False` този финализатор само ЗАТВАРЯ файла: обвитият close()
    # по-горе е ИНСТАНЦИОНЕН атрибут и изобщо не се вика оттам, така че
    # копие от ~740 KB на шрифта оставаше в %TEMP% завинаги (Windows няма
    # автоматично чистене на тази папка). weakref.finalize връща
    # изгубената гаранция: когато обектът бъде събран, файлът се трие,
    # независимо по кой път сме стигнали дотам. Двойното изтриване е
    # безопасно — _silent_remove поглъща OSError.
    weakref.finalize(tmp_file, _silent_remove, tmp_file.name)
    if data:
        tmp_file.write(data)
        tmp_file.flush()
    # Оригиналът добавя към files_tmp само при непразно съдържание; тук
    # добавяме ВИНАГИ — с delete=False неследен празен файл иначе би
    # останал на диска завинаги (оригиналът разчиташе на delete=True).
    _pisa_files.files_tmp.append(tmp_file)
    if self.path is None:
        self.path = tmp_file.name
    return tmp_file


_pisa_files.BaseFile.get_named_tmp_file = _windows_safe_get_named_tmp_file


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
        # Одит (19.08.2026, находка №4): вижте _render_lock по-горе —
        # паралелни PDF заявки се саботират взаимно през споделения
        # списък с временни файлове на xhtml2pdf.
        with _render_lock:
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
