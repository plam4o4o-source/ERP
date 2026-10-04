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
import re
import sys
import tempfile
import threading
import weakref

from flask import render_template

import applog
from barcode128 import code128_png_data_uri

# Одит (01.10.2026, Q8/F7): xhtml2pdf се внася МЪРЗЕЛИВО (_pisa() по-долу) —
# самият му import е 0.5–0.8 сек от всеки старт на програмата, а PDF износът
# е рядко действие. Кръпката за временните файлове се закача при първия внос.
_pisa_module = None
_pisa_files = None
_pisa_import_lock = threading.Lock()


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

#: Одит (22.08.2026, находка №3): най-дългото изчакване на реда за PDF —
#: не позволява на опашката да блокира всички работни нишки на waitress.
#:
#: Одит (05.09.2026, находка №9): вдигнато от 20 на 90 сек., защото
#: обосновката „300 реда ≈ 2 сек“ беше сгрешена с цял порядък. ИЗМЕРЕНО на
#: тази машина след поправката на находка №2: 100 реда = 1.8 сек, 300 реда =
#: 5.8 сек, 500 реда = 10.1 сек (преди нея съответно 2.3 / 7.8 / 13+ сек, а
#: при по-широка таблица и 26 сек). Тоест втори служител, натиснал бутона
#: секунда след първия при 500-редов документ, опираше в стария таван и
#: получаваше „опашката е заета“ вместо своя файл — при напълно изправна
#: програма. 90 сек. побират двама души подред и на най-големия реалистичен
#: документ, а горната граница остава, за да не увисне цялата опашка.
_RENDER_LOCK_TIMEOUT = 90


class PdfBusyError(RuntimeError):
    """Одит (25.08.2026, находка №5): PDF опашката е препълнена в момента —
    ВРЕМЕННО състояние, не срив.

    Отделен клас (подклас на RuntimeError, за да го хване и всеки стар
    `except RuntimeError`), защото „заета опашка“ и „генерирането се провали“
    искат различно съобщение към оператора: първото е „изчакайте няколко
    секунди и опитайте пак“ (нищо не е счупено, никого не безпокойте),
    второто е „нещо се обърка, кажете на администратор“. Преди това и двете
    бяха гол RuntimeError с еднакъв текст, а всъщност заетата опашка се и
    уплиташе в общата обвивка за reportlab грешки (виж по-долу) — логваше се
    като срив с пълен traceback и текстът ѝ се преобличаше в „PDF
    генерирането е неуспешно“."""


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


def _pisa():
    """xhtml2pdf.pisa, внесен при първа нужда, с вече закачена кръпка на
    BaseFile.get_named_tmp_file (виж _windows_safe_get_named_tmp_file)."""
    global _pisa_module, _pisa_files
    if _pisa_module is None:
        with _pisa_import_lock:
            if _pisa_module is None:
                import xhtml2pdf.files as pisa_files
                from xhtml2pdf import pisa
                _pisa_files = pisa_files
                pisa_files.BaseFile.get_named_tmp_file = _windows_safe_get_named_tmp_file
                _pisa_module = pisa
    return _pisa_module


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


def _resource_policy():
    """Позволените локални файлове за xhtml2pdf — само папката на шрифтовете
    (баркодът е data: URI). None за версии без правила за достъп (< 0.2.20),
    където ограничение няма."""
    try:
        from pathlib import Path

        from xhtml2pdf.config.resources import ResourceAccessPolicy
    except ImportError:
        return None
    return ResourceAccessPolicy(base_dir=Path(_font_dir()), allow_remote=False)


#: Одит (05.09.2026, находка №2): колони със СВОБОДЕН ТЕКСТ. Само те получават
#: остатъка от ширината; кодовете, номерата, количествата, теглата и цените
#: се държат цели (CJK пренасянето чупеше „3750.0“ + „0“, HS „842139“ + „90“).
_PDF_TEXT_COLUMN_KEYS = frozenset((
    "description", "reference_desc", "marks", "packing", "notes",
))

#: Одит (04.10.2026, X1): числови колони — те получават ширината си ПРЕДИ
#: кодовете и текста, когато страницата не стига за всичко.
_PDF_NUMERIC_COLUMN_KEYS = frozenset((
    "pos", "qty", "weight", "net", "gross", "volume", "length", "width", "height",
    "net_weight", "unit_price", "__row_total__", "__row_weight__",
))

#: Одит (04.10.2026, X1): ширините на колоните вече се смятат от РЕАЛНОТО
#: съдържание (най-дългата стойност и най-дългата дума от заглавието),
#: измерено с метриките на самия шрифт (DejaVu Sans), а не от фиксирани
#: „подсказки“ в знаци. С подсказките 9-колонната фактура за Норвегия
#: даваше ~9 % на P.O NO и кода на материала: „4500000007A2“ и
#: „C0000012345-01“ се застъпваха („4500000007A2C0000…“), HS кодът излизаше
#: вляво от рамката, а заглавията се режеха („Колич“, „Дълж“). xhtml2pdf
#: не умее auto-layout, затова: (1) всяка колона получава поне ширината на
#: най-дългата си непрекъсваема стойност/дума, ако страницата я побира;
#: (2) при много колони шрифтът на таблицата намалява (9 → 8 → 7 pt);
#: (3) ако и това не стига, твърде дългата дума се пренася ПО МЯРКА
#: (wrap_cell_lines) — никога застъпване. CJK пренасянето (чупеше думи по
#: средата: „мате/риала“, „р/едуктор“) вече не се ползва.
#: Ширината на рамката: A4 (21 cm) минус страничните полета на @page (2 × 1.2 cm).
_PDF_FRAME_WIDTH_PT = (21.0 - 2 * 1.2) * 72 / 2.54
#: Вътрешен отстъп на клетка вляво/вдясно (pdf_export.html ползва същото число).
_PDF_CELL_PAD_PT = 2.0
#: Колко по-тясно от колоната е мястото за текст. ИЗМЕРЕНО (xhtml2pdf 0.2.x,
#: 7 и 9 pt): текстът се пренася, ако е по-широк от колоната минус
#: 4 × отстъпа + ~0.7 pt (xhtml2pdf отнема отстъпа два пъти — веднъж за
#: клетката и веднъж за параграфа в нея; видимият отстъп е 2 × 2 pt).
_PDF_CELL_EXTRA_PT = 4 * _PDF_CELL_PAD_PT + 1.5
_PDF_TABLE_FONT_SIZES = (9.0, 8.0, 7.0)
_PDF_FIELD_FONT_PT = 10.0
#: Дума в текстова колона, по-дълга от това, не „изяжда“ страницата —
#: пренася се по мярка.
_PDF_TEXT_TOKEN_CAP_PT = 90.0
#: Ред със стойност (с интервали) в нетекстова колона над това се пренася
#: по интервалите вместо да разширява колоната безкрайно.
_PDF_LINE_CAP_PT = 160.0
#: Най-тясна разумна колона при недостиг на място.
_PDF_MIN_COL_PT = 24.0
#: Желана ширина на текстова колона, преди да се намали шрифтът.
_PDF_TEXT_COMFORT_PT = 110.0
_PDF_FIELD_LABEL_PCT = 32.0

_PDF_METRIC_FONT = "PachoDejaVuSans"
_PDF_METRIC_FONT_BOLD = "PachoDejaVuSans-Bold"
_metric_font_lock = threading.Lock()


def _metric_font(bold):
    """Регистрира (веднъж) DejaVu Sans за измерване на текста — същият шрифт,
    с който pdf_export.html рисува таблицата."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    name = _PDF_METRIC_FONT_BOLD if bold else _PDF_METRIC_FONT
    with _metric_font_lock:
        if name not in pdfmetrics.getRegisteredFontNames():
            fname = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            pdfmetrics.registerFont(TTFont(name, os.path.join(_font_dir(), fname)))
    return name


def _text_width(text, bold=False, size=1.0):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    return stringWidth(text, _metric_font(bold), size)


def _cell_text_lines(value):
    """Редовете на стойността — новите редове от въведеното се ПАЗЯТ
    (X5: адресите губеха разделянето си на редове)."""
    if value is None:
        return [""]
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return [" ".join(line.split()) for line in text.split("\n")]


def _break_token(token, max_width, bold, size):
    """Реже дума, по-широка от колоната, на части, всяка от които се побира."""
    pieces, current = [], ""
    for ch in token:
        if current and _text_width(current + ch, bold, size) > max_width + 0.5:
            pieces.append(current)
            current = ch
        else:
            current += ch
    if current:
        pieces.append(current)
    return pieces or [""]


def wrap_cell_lines(value, width_pt, bold=False, size=_PDF_FIELD_FONT_PT):
    """Одит (04.10.2026, X1/X5): редовете, които клетка с ширина `width_pt`
    трябва да покаже. Пази новите редове от въведеното, а дума, по-широка от
    колоната, реже по мярка на шрифта (reportlab пренася само по интервали —
    без това дългият код излизаше извън клетката върху съседната). Думите,
    които се побират, остават цели; по интервалите пренася самият reportlab."""
    inner = max(width_pt - _PDF_CELL_EXTRA_PT, size)
    out = []
    for line in _cell_text_lines(value):
        words = line.split(" ") if line else []
        if not words:
            out.append("")
            continue
        current = []
        for word in words:
            # 0.5 pt допуск за закръгляне — запасът е вече в _PDF_CELL_EXTRA_PT.
            if _text_width(word, bold, size) <= inner + 0.5:
                current.append(word)
                continue
            pieces = _break_token(word, inner, bold, size)
            if current:
                out.append(" ".join(current))
            out.extend(pieces[:-1])
            current = [pieces[-1]]
        out.append(" ".join(current))
    while len(out) > 1 and not out[-1]:
        out.pop()
    while len(out) > 1 and not out[0]:
        out.pop(0)
    return out


def _column_measures(key, label, values, bold_values=()):
    """(мин. ширина на заглавието, пълна ширина на заглавието, най-дълга
    дума, най-дълъг ред) при размер 1 pt — ширината е линейна по размера."""
    head = " ".join(str(label or "").split())
    head_min = max([_text_width(w, True) for w in head.split(" ") if w] or [0.0])
    head_full = _text_width(head, True)
    token = line_w = 0.0
    seen = set()
    # Редът TOTAL е удебелен — мери се с удебеления шрифт.
    for bold, group in ((False, values), (True, bold_values)):
        for value in group:
            if value is None:
                continue
            text = str(value)
            if (bold, text) in seen:
                continue
            seen.add((bold, text))
            for line in _cell_text_lines(text):
                if not line:
                    continue
                w = _text_width(line, bold)
                if w > line_w:
                    line_w = w
                if w > token:  # дума не е по-широка от реда си
                    for word in line.split(" "):
                        token = max(token, _text_width(word, bold))
    return head_min, head_full, token, line_w


def _fill(budget, mins, idx):
    """Водно пълнене: колоните с малка нужда получават нуждата си, останалите
    делят поравно остатъка (поне _PDF_MIN_COL_PT)."""
    widths = {}
    rest = list(idx)
    while rest:
        share = budget / len(rest)
        small = [i for i in rest if mins[i] <= share]
        if not small:
            for i in rest:
                widths[i] = max(share, 0.0)
            break
        for i in small:
            widths[i] = mins[i]
            budget -= mins[i]
        rest = [i for i in rest if i not in small]
    return widths


def pdf_table_plan(item_columns, items=None, totals_row=None):
    """Одит (04.10.2026, X1): (ширини в pt, размер на шрифта) за таблицата с
    редовете — от реалното съдържание. Виж коментара при _PDF_FRAME_WIDTH_PT."""
    cols = list(item_columns or [])
    if not cols:
        return [], _PDF_TABLE_FONT_SIZES[0]
    avail = _PDF_FRAME_WIDTH_PT
    rows = [it for it in (items or []) if isinstance(it, dict)]
    measures = []
    for i, (key, label) in enumerate(cols):
        values = [it.get(key) for it in rows]
        bold_values = ([totals_row[i]] if totals_row is not None and i < len(totals_row)
                       else [])
        measures.append(_column_measures(key, label, values, bold_values))
    text_idx = [i for i, (key, _l) in enumerate(cols) if key in _PDF_TEXT_COLUMN_KEYS]
    extra = _PDF_CELL_EXTRA_PT
    plans = []
    for size in _PDF_TABLE_FONT_SIZES:
        mins, prefs = [], []
        for i, (key, _label) in enumerate(cols):
            head_min, head_full, token, line_w = (m * size for m in measures[i])
            if i in text_idx:
                need = max(head_min, min(token, _PDF_TEXT_TOKEN_CAP_PT))
            else:
                need = max(head_min, min(line_w, _PDF_LINE_CAP_PT))
            need = max(need + extra, _PDF_MIN_COL_PT)
            mins.append(need)
            prefs.append(max(need, max(head_full, line_w) + extra))
        plans.append((size, mins, prefs))
        # Текстовите колони трябва да получат поне _PDF_TEXT_COMFORT_PT (или
        # колкото им трябва, ако е по-малко) — иначе описанието се разлива в
        # тясна ивица по 6 реда; тогава се пробва по-малък шрифт.
        comfort = sum(max(0.0, min(prefs[i], _PDF_TEXT_COMFORT_PT) - mins[i]) for i in text_idx)
        if sum(mins) + comfort <= avail:
            break
    else:
        fitting = [p for p in plans if sum(p[1]) <= avail]
        # Никой размер не дава удобна ширина на текста → най-малкият, който
        # поне побира минимумите (така текстът получава най-много място).
        size, mins, prefs = fitting[-1] if fitting else plans[-1]
    if sum(prefs) <= avail:
        widths = list(prefs)
        receivers = text_idx or list(range(len(cols)))
        weight = sum(prefs[i] for i in receivers) or 1.0
        surplus = avail - sum(prefs)
        for i in receivers:
            widths[i] += surplus * prefs[i] / weight
    elif sum(mins) <= avail:
        widths = list(mins)
        free = avail - sum(mins)
        # Първо текстът до „удобната“ ширина, после остатъкът — пропорционално
        # на това, което на всяка колона още ѝ липсва до цял ред.
        gives = {i: max(0.0, min(prefs[i], _PDF_TEXT_COMFORT_PT) - mins[i]) for i in text_idx}
        need = sum(gives.values())
        if need > 0:
            ratio = min(1.0, free / need)
            for i, g in gives.items():
                widths[i] += g * ratio
            free -= need * ratio
        slack = [p - w for p, w in zip(prefs, widths)]
        total_slack = sum(slack) or 1.0
        widths = [w + free * sl / total_slack for w, sl in zip(widths, slack)]
    else:
        # Не стига дори минимумът: числата първи (те не бива да се режат),
        # после кодовете и текстът делят остатъка; дългите думи там се
        # пренасят по мярка (wrap_cell_lines).
        num_idx = [i for i, (key, _l) in enumerate(cols) if key in _PDF_NUMERIC_COLUMN_KEYS]
        other_idx = [i for i in range(len(cols)) if i not in num_idx]
        reserve = _PDF_MIN_COL_PT * len(other_idx)
        num_need = sum(mins[i] for i in num_idx)
        widths = [0.0] * len(cols)
        if num_need <= avail - reserve:
            for i in num_idx:
                widths[i] = mins[i]
            filled = _fill(avail - num_need, mins, other_idx)
        else:
            filled = _fill(avail, mins, range(len(cols)))
        for i, w in filled.items():
            widths[i] = w
    return widths, size


def pdf_column_layout(item_columns, items=None, totals_row=None):
    """(ключ, етикет, ширина в %, текстова ли е) за всяка колона — виж
    pdf_table_plan. Без `items` ширините идват само от заглавията, а
    текстовите колони поемат остатъка."""
    widths, _size = pdf_table_plan(item_columns, items, totals_row)
    total = sum(widths) or 1.0
    return [(key, label, round(100.0 * w / total, 3), key in _PDF_TEXT_COLUMN_KEYS)
            for (key, label), w in zip(item_columns or [], widths)]


#: Одит (26.09.2026, находка №10): ред на таблица, по-висок от една
#: страница, не може да се раздели от reportlab („LayoutError … too
#: large“) и целият PDF износ пропада — при опаковъчен лист още от ~780
#: знака описание в една клетка (най-тясната текстова колона). Дългият
#: текст се разделя по думи на части с тази дължина, всяка в свой ред-
#: продължение; 250 знака е под една трета страница и в най-тясната колона.
_PDF_CELL_CHUNK = 250

#: Одит (01.10.2026, R4 — регресия от v3.75.0): 250 знака важат за колона
#: от ~25 % ширина. В тясна колона (4–8 %) същите 250 знака са ~740 pt —
#: с повторения заглавен ред над рамката от 751 pt → LayoutError. Затова
#: частта се мащабира по ширината на колоната, с долна граница.
_PDF_CELL_CHUNK_REF_PCT = 25.0
_PDF_CELL_CHUNK_MIN = 20


def _chunk_for_width(width_pct):
    scaled = int(_PDF_CELL_CHUNK * float(width_pct) / _PDF_CELL_CHUNK_REF_PCT)
    return max(_PDF_CELL_CHUNK_MIN, min(_PDF_CELL_CHUNK, scaled))


def _split_long_text(value, limit=_PDF_CELL_CHUNK):
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return [value]
    chunks, current = [], ""
    for word in text.split():
        rest = word
        while len(rest) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(rest[:limit])
            rest = rest[limit:]
        if current and len(current) + 1 + len(rest) > limit:
            chunks.append(current)
            current = rest
        else:
            current = (current + " " + rest) if current else rest
    if current:
        chunks.append(current)
    return chunks or [""]


def _split_tall_items(items, item_columns, layout=None):
    keys = [key for key, _label in item_columns or []]
    if layout is None:
        layout = pdf_column_layout(item_columns or [])
    limits = {key: _chunk_for_width(width) for key, _label, width, _is_text in layout}
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            out.append(it)
            continue
        parts = {key: _split_long_text(it.get(key, ""), limits.get(key, _PDF_CELL_CHUNK))
                 for key in keys}
        height = max([len(p) for p in parts.values()] or [1])
        if height == 1:
            out.append(it)
            continue
        first = dict(it)
        for key in keys:
            first[key] = parts[key][0]
        out.append(first)
        for i in range(1, height):
            out.append({key: (parts[key][i] if i < len(parts[key]) else "") for key in keys})
    return out


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
        артикули; None пропуска реда изцяло. Одит (04.10.2026, X5): вече
        и за опаковъчния лист и палетната карта (виж
        routes_documents._export_totals_row).
    """
    barcode_uri = code128_png_data_uri(barcode) if barcode else None
    item_columns = list(item_columns or [])
    # Одит (04.10.2026, X1): ширините — от реалното съдържание (pdf_table_plan),
    # а всяка клетка идва като готови редове (wrap_cell_lines), за да не може
    # стойност да излезе извън колоната си върху съседната.
    widths, font_pt = pdf_table_plan(item_columns, items, totals_row)
    total_w = sum(widths) or 1.0
    layout = [(key, label, round(100.0 * w / total_w, 3), key in _PDF_TEXT_COLUMN_KEYS)
              for (key, label), w in zip(item_columns, widths)]
    head_cells = [wrap_cell_lines(label, w, bold=True, size=font_pt)
                  for (_key, label), w in zip(item_columns, widths)]
    rows = []
    if item_columns:
        for it in _split_tall_items(items, item_columns, layout):
            if not isinstance(it, dict):
                continue
            rows.append([wrap_cell_lines(it.get(key, ""), w, size=font_pt)
                         for (key, _label), w in zip(item_columns, widths)])
    totals_cells = None
    if totals_row is not None and item_columns:
        totals_cells = [wrap_cell_lines(totals_row[i] if i < len(totals_row) else "", w,
                                        bold=True, size=font_pt)
                        for i, w in enumerate(widths)]
    # Одит (04.10.2026, X5): празните полета не се печатат (бланката също
    # не ги показва), а новите редове в адресите се пазят.
    label_w = _PDF_FRAME_WIDTH_PT * _PDF_FIELD_LABEL_PCT / 100.0
    value_w = _PDF_FRAME_WIDTH_PT - label_w
    field_rows = []
    for label, value in fields or []:
        if value is None or not str(value).strip():
            continue
        field_rows.append((wrap_cell_lines(label, label_w, bold=True),
                           wrap_cell_lines(value, value_w)))
    html = render_template(
        "pdf_export.html",
        title=title,
        number=number,
        barcode_uri=barcode_uri,
        field_rows=field_rows,
        field_label_pct=_PDF_FIELD_LABEL_PCT,
        head_cells=head_cells,
        rows=rows,
        totals_cells=totals_cells,
        column_layout=layout,
        # Числата — вдясно; кодовете и текстът — вляво.
        col_classes=[("num" if key in _PDF_NUMERIC_COLUMN_KEYS else
                      "txt" if key in _PDF_TEXT_COLUMN_KEYS else "code")
                     for key, _label in item_columns],
        table_font_pt=font_pt,
        cell_pad_pt=_PDF_CELL_PAD_PT,
        footer_right_pad_pt=_FOOTER_RIGHT_PAD_PT,
        font_dir=_font_dir(),
    )
    pisa = _pisa()
    out = io.BytesIO()
    # Одит (19.08.2026, находка №4): вижте _render_lock по-горе — паралелни
    # PDF заявки се саботират взаимно през споделения списък с временни
    # файлове на xhtml2pdf.
    # Одит (22.08.2026, находка №3): катинарът вече е С ТАВАН.
    #
    # Обосновката „едно PDF ≈ 0.2 сек“ (находка №4 от 19.08) беше измерена
    # върху миниатюрен документ. Реално: 100 реда → 0.65 сек, 300 реда → 2.1
    # сек. waitress работи с точно 8 нишки, а БЕЗ таван осем едновременни
    # износа на голяма фактура държат ВСИЧКИТЕ осем нишки блокирани ~17 сек —
    # през това време нито вход, нито табло, нито запис на документ минава.
    # По-добре ясна грешка на един потребител, отколкото замразено приложение
    # за целия офис.
    #
    # Одит (25.08.2026, находка №5): изчакването на реда е ИЗВЪН try-а за
    # reportlab грешки по-долу — иначе неговият `except Exception` хващаше
    # PdfBusyError, логваше го като срив и преобличаше текста му. Сега
    # „заета опашка“ излита чиста, с отделния си клас.
    if not _render_lock.acquire(timeout=_RENDER_LOCK_TIMEOUT):
        raise PdfBusyError(
            "В момента се генерират други PDF файлове и изчакването беше "
            "твърде дълго. Опитайте отново след няколко секунди.")
    try:
        try:
            # Одит (04.10.2026): xhtml2pdf ≥ 0.2.20 чете локални файлове само
            # под папката на документа — за HTML от низ това е текущата папка
            # на процеса. В .exe шрифтовете са в sys._MEIPASS (%TEMP%), не
            # там, откъдето е стартирана програмата → DejaVu се блокираше и
            # кирилицата излизаше като квадратчета. Изрично правило за
            # папката на шрифтовете (`path=` не върши работа на Windows:
            # „D:\…“ се чете като URL със схема „d“ и пак остава cwd).
            kwargs = {}
            policy = _resource_policy()
            if policy is not None:
                kwargs["resource_policy"] = policy
            result = pisa.CreatePDF(src=html, dest=out, encoding="utf-8", **kwargs)
        finally:
            _render_lock.release()
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
    return _stamp_page_total(out.getvalue())


#: Одит (01.10.2026, F4): „X / Y“ в колонтитула. `<pdf:pagecount>` кара
#: xhtml2pdf да подреди целия документ ДВА пъти (−28…−43 % време без него).
#: Шаблонът печата „… · X“ подравнено вдясно с този отстъп, а общият брой
#: „ / Y“ се дорисува в празното място след него с pypdf, в един пас.
_FOOTER_RIGHT_PAD_PT = 28.0
_FOOTER_FONT_SIZE = 7.5
#: Отстояния на рамката на колонтитула (pdf_export.html, @frame footer) и
#: отместването на базовата линия на текста спрямо горния ѝ край — измерено.
_FOOTER_SIDE_PT = 1.2 * 72 / 2.54
_FOOTER_TOP_PT = (0.9 + 0.7) * 72 / 2.54
_FOOTER_BASELINE_DROP_PT = 6.70
_FOOTER_FONT_NAME = "PachoDejaVuSans"
_footer_font_lock = threading.Lock()


def _footer_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    with _footer_font_lock:
        if _FOOTER_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_FOOTER_FONT_NAME,
                                           os.path.join(_font_dir(), "DejaVuSans.ttf")))
    return _FOOTER_FONT_NAME


def _footer_label_end(page):
    """(x на края, y на базовата линия) на номера в колонтитула или None."""
    from reportlab.pdfbase import pdfmetrics
    spans = []

    def visit(text, cm, tm, _font, _size):
        if text.strip():
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            spans.append((x, y, text.strip()))

    page.extract_text(visitor_text=visit)
    footer = [s for s in spans if s[1] < _FOOTER_TOP_PT + 2]
    if not footer:
        return None
    x, y, text = min(footer, key=lambda s: s[1])
    return x + pdfmetrics.stringWidth(text, _footer_font(), _FOOTER_FONT_SIZE), y


def _stamp_page_total(pdf_bytes):
    """Дорисува „ / <общо страници>“ след номера на страницата на всеки лист.
    Надписът е еднакъв на всички листове — една и съща малка добавка към
    съдържанието (шрифтът се вгражда веднъж), без разбор на самите страници.
    При неуспех връща PDF-а без общия брой — износът не бива да пада заради
    колонтитула."""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
        from reportlab.pdfgen import canvas

        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = len(reader.pages)
        box = reader.pages[0].mediabox
        width, height = float(box.width), float(box.height)
        x = width - _FOOTER_SIDE_PT - 1.0 - _FOOTER_RIGHT_PAD_PT
        y = _FOOTER_TOP_PT - _FOOTER_BASELINE_DROP_PT
        # Одит (01.10.2026): точната позиция на номера се взима от самия лист —
        # различните версии на xhtml2pdf слагат базовата линия с ~1 pt разлика.
        found = _footer_label_end(reader.pages[0])
        if found is not None:
            x, y = found
        overlay_buf = io.BytesIO()
        c = canvas.Canvas(overlay_buf, pagesize=(width, height))
        c.setFont(_footer_font(), _FOOTER_FONT_SIZE)
        c.setFillColorRGB(0x55 / 255.0, 0x55 / 255.0, 0x55 / 255.0)
        c.drawString(x, y, " / %d" % total)
        c.save()
        overlay = PdfReader(io.BytesIO(overlay_buf.getvalue())).pages[0]
        content = overlay.get_contents().get_data()

        writer = PdfWriter(clone_from=reader)
        fonts = {}
        for index, (name, ref) in enumerate(
                overlay["/Resources"].get_object()["/Font"].get_object().items()):
            new_name = "/PachoPageTotal%d" % index
            fonts[NameObject(new_name)] = ref.get_object().clone(writer).indirect_reference
            content = re.sub(re.escape(name.encode("latin-1")) + rb"(?=\s)",
                             new_name.encode("latin-1"), content)
        head = DecodedStreamObject()
        head.set_data(b"q\n")
        tail = DecodedStreamObject()
        tail.set_data(b"\nQ\n" + content)
        head_ref = writer._add_object(head)
        tail_ref = writer._add_object(tail.flate_encode())
        for page in writer.pages:
            resources = page["/Resources"].get_object()
            if "/Font" not in resources:
                resources[NameObject("/Font")] = DictionaryObject()
            resources["/Font"].get_object().update(fonts)
            old = page.raw_get("/Contents")  # препратката, не самият поток (иначе се дублира)
            parts = ArrayObject([head_ref])
            if isinstance(old.get_object(), ArrayObject):
                parts.extend(old.get_object())
            else:
                parts.append(old)
            parts.append(tail_ref)
            page[NameObject("/Contents")] = parts
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        applog.log_exception("pdf_export._stamp_page_total: общият брой страници не е дорисуван")
        return pdf_bytes
