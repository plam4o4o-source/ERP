# -*- coding: utf-8 -*-
"""Тест-ПАЗАЧ за пълнотата на преводите (одит 19.08.2026, находка №13).

ЗАЩО съществува този файл
-------------------------
Одитът установи, че от 693 низа, маркирани с ``_()`` в шаблоните и в
Python модулите, **242 изобщо липсваха** и в двата каталога: след като са
били добавени нови функции (целият раздел „Фактури“, „Материали“,
статистиките на таблото, модалът за потвърждение…), никой НЕ е пуснал
повторно ``pybabel extract``/``update``. Разминаването е ТИХО — нищо не
гърми, приложението просто показва български текст на английски и турски
потребител, и то на случайни места, така че интерфейсът изглежда наполовина
преведен.

Тестовете тук правят това разминаване ШУМНО. При провал съобщението казва
кои точно низове липсват/не са преведени и коя команда да се пусне.

КАКВО се проверява
------------------
1. Всеки ``_()`` низ от кода присъства като msgid и в двата каталога.
2. Няма празен превод (msgstr "") — msgid в каталога, но без превод, е
   същият дефект, само че по-късно в тръбопровода.
3. Форматиращите плейсхолдъри (``%s``, ``%d``, ``%(name)s``, ``{name}``)
   съвпадат между msgid и превода — иначе приложението гърми при
   форматиране ТОЧНО в момента, в който покаже съобщението.
4. Компилираните .mo файлове са в крак с .po (иначе преводът е налице в
   хранилището, но потребителят пак вижда български).
5. Речникът за JavaScript (``js_i18n`` в base.html) и ключовете, реално
   четени от ``static/app.js`` през ``t()``/``tf()``, съвпадат — иначе
   тихо се пада към българския fallback.

При провал: pybabel extract -F babel.cfg -o messages.pot .
            pybabel update -i messages.pot -d translations -l en
            pybabel update -i messages.pot -d translations -l tr
            (преведи новите низове)
            pybabel compile -d translations
"""
import os
import re

import pytest
from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.mofile import read_mo
from babel.messages.pofile import read_po

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGS = ("en", "tr")

FIXIT = (
    "\n\nКАК СЕ ПОПРАВЯ (от кореновата папка на проекта):\n"
    "    pybabel extract -F babel.cfg -o messages.pot .\n"
    "    pybabel update -i messages.pot -d translations -l en\n"
    "    pybabel update -i messages.pot -d translations -l tr\n"
    "    (преведи новопоявилите се низове в двата .po файла)\n"
    "    pybabel compile -d translations\n"
)


def _po_path(lang):
    return os.path.join(ROOT, "translations", lang, "LC_MESSAGES", "messages.po")


def _mo_path(lang):
    return os.path.join(ROOT, "translations", lang, "LC_MESSAGES", "messages.mo")


def _source_msgids():
    """Всички низове, маркирани за превод в изходния код — извлечени по
    ТОЧНО СЪЩИЯ начин, по който го прави `pybabel extract -F babel.cfg`
    (същият babel.cfg, същият екстрактор), за да няма как тестът да е
    „съгласен“ с каталога, а реалната команда — не."""
    with open(os.path.join(ROOT, "babel.cfg"), encoding="utf-8") as fh:
        method_map, options_map = parse_mapping_cfg(fh, filename="babel.cfg")
    ids = {}
    for filename, lineno, message, _comments, _ctx in extract_from_dir(
            ROOT, method_map=method_map, options_map=options_map):
        # ngettext дава кортеж (единствено, множествено) — интересува ни
        # всяка форма поотделно.
        forms = message if isinstance(message, tuple) else (message,)
        for form in forms:
            if form:
                ids.setdefault(form, "%s:%d" % (filename, lineno))
    return ids


def _catalog(lang):
    with open(_po_path(lang), encoding="utf-8") as fh:
        return read_po(fh, locale=lang)


def _fmt_list(items, limit=25):
    """Първите `limit` елемента, по един на ред, с указание колко още."""
    shown = ["    • %s" % s for s in items[:limit]]
    if len(items) > limit:
        shown.append("    … и още %d" % (len(items) - limit))
    return "\n".join(shown)


# ---------------------------------------------------------------- 1. нищо не липсва в каталозите

@pytest.mark.parametrize("lang", LANGS)
def test_every_marked_string_is_present_in_the_catalog(lang):
    """Точно регресията от одита: `_()` низ в кода, който изобщо го няма
    като msgid в каталога. Такъв низ НЕ може да бъде преведен — колкото и
    добър да е преводачът, той дори не го вижда."""
    source = _source_msgids()
    catalog = _catalog(lang)
    missing = sorted(msgid for msgid in source if catalog.get(msgid) is None)
    assert not missing, (
        "В translations/%s/LC_MESSAGES/messages.po ЛИПСВАТ %d низа, маркирани с _()\n"
        "в кода. Най-вероятно е добавена нова функционалност без повторен\n"
        "`pybabel extract`. Липсващите (първият им адрес в кода):\n%s%s"
        % (lang, len(missing),
           _fmt_list(["%s   [%s]" % (m, source[m]) for m in missing]), FIXIT))


@pytest.mark.parametrize("lang", LANGS)
def test_no_msgid_is_left_untranslated(lang):
    """msgid в каталога, но с празен превод — низът е бил извлечен, но
    после забравен. За потребителя резултатът е същият като при липсващ
    низ: вижда български текст в EN/TR интерфейс."""
    catalog = _catalog(lang)
    untranslated = sorted(m.id for m in catalog if m.id and not m.string)
    assert not untranslated, (
        "В translations/%s/LC_MESSAGES/messages.po има %d НЕПРЕВЕДЕНИ низа\n"
        "(msgid присъства, msgstr е празен):\n%s%s"
        % (lang, len(untranslated), _fmt_list(untranslated), FIXIT))


# ---------------------------------------------------------------- 2. плейсхолдърите не са счупени

#: %s / %d / %(name)s (Python `%` форматиране, ползва се в кода) и {name}
#: (ползва се от JS речника в base.html и от .format() в
#: routes_pallet_extra) — виж коментара при js_i18n в base.html защо
#: JS низовете НЕ могат да ползват %-синтаксис.
_PLACEHOLDER = re.compile(r"%\(\w+\)[sd]|%[sd]|\{\w+\}")


@pytest.mark.parametrize("lang", LANGS)
def test_format_placeholders_survive_the_translation(lang):
    """Ако преводът изгуби или добави плейсхолдър, приложението гърми при
    форматирането (TypeError/KeyError) — и то точно когато трябва да
    покаже съобщението, тоест обикновено при грешка, тоест на най-лошото
    възможно място. Именуваните (%(name)s / {name}) може да се повтарят
    или да отпадат в превода (напр. двуезичното „Карта {n} / Card {n}“ на
    EN е само „Card {n}“), затова за тях сравняваме МНОЖЕСТВА и искаме
    само преводът да не въвежда НОВИ имена. Позиционните (%s/%d) се
    сравняват по БРОЙ — при тях редът и количеството са задължителни."""
    broken = []
    for message in _catalog(lang):
        if not message.id or not message.string:
            continue
        src = _PLACEHOLDER.findall(message.id)
        dst = _PLACEHOLDER.findall(message.string)
        src_named = {p for p in src if p.startswith("%(") or p.startswith("{")}
        dst_named = {p for p in dst if p.startswith("%(") or p.startswith("{")}
        src_pos = [p for p in src if p in ("%s", "%d")]
        dst_pos = [p for p in dst if p in ("%s", "%d")]
        if dst_named - src_named:
            broken.append("НОВИ имена %s: %r → %r"
                          % (sorted(dst_named - src_named), message.id, message.string))
        elif src_pos != dst_pos:
            broken.append("позиционни %s ≠ %s: %r → %r"
                          % (src_pos, dst_pos, message.id, message.string))
    assert not broken, (
        "В translations/%s/LC_MESSAGES/messages.po има %d превода със СЧУПЕНИ\n"
        "форматиращи плейсхолдъри — приложението ще гръмне при показването им:\n%s"
        % (lang, len(broken), _fmt_list(broken)))


# ---------------------------------------------------------------- 3. .mo е в крак с .po

@pytest.mark.parametrize("lang", LANGS)
def test_compiled_mo_matches_the_po_file(lang):
    """Преведено в .po, но некомпилирано в .mo = потребителят пак вижда
    български. Flask-Babel чете САМО .mo файла."""
    with open(_mo_path(lang), "rb") as fh:
        compiled = read_mo(fh)
    catalog = _catalog(lang)
    stale = sorted(m.id for m in catalog
                   if m.id and m.string and (compiled.get(m.id) is None
                                             or compiled.get(m.id).string != m.string))
    assert not stale, (
        "translations/%s/LC_MESSAGES/messages.mo е ОСТАРЯЛ — %d превода от .po\n"
        "липсват или се различават в компилирания файл:\n%s\n\n"
        "Пусни: pybabel compile -d translations"
        % (lang, len(stale), _fmt_list(stale)))


# ---------------------------------------------------------------- 4. JS речникът (част „б“ от находката)

_JS_DICT_KEY = re.compile(r"^\s*'(\w+)':", re.M)
#: t("ключ", "…") / tf("ключ", "…", {…}) в static/app.js
#: `t("ключ", …)` и `tf("ключ", …)`, а от 05.09.2026 и `tfp("ключ_ед",
#: "…", "ключ_мн", "…", …)` — помощната за българското единствено число
#: (виж tfp в app.js). При tfp се ползват ДВА ключа, затова изразът хваща и
#: двата: първия по общото правило, втория — с шаблона по-долу.
_JS_USED_KEY = re.compile(r'\bt(?:f|fp)?\(\s*"(\w+)"\s*,')
#: Вторият ключ на tfp стои след низа-заместител на първия.
_JS_USED_KEY_SECOND = re.compile(
    r'\btfp\(\s*"\w+"\s*,\s*"(?:[^"\\]|\\.)*"\s*,\s*"(\w+)"\s*,')


def _base_html_keys():
    with open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8") as fh:
        body = fh.read()
    block = body.split("{% set js_i18n = {", 1)
    assert len(block) == 2, "js_i18n речникът изчезна от templates/base.html"
    return set(_JS_DICT_KEY.findall(block[1].split("} %}", 1)[0]))


def _app_js_keys():
    with open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8") as fh:
        source = fh.read()
    return set(_JS_USED_KEY.findall(source)) | set(_JS_USED_KEY_SECOND.findall(source))


def test_every_key_used_by_app_js_exists_in_the_server_dictionary():
    """Част „б“ от находката: app.js вече чете преводите си от речника,
    вграден в base.html. Ключ, който го няма там, НЕ гърми — просто тихо
    пада към българския fallback, тоест дефектът от одита се връща
    незабелязано точно за този низ."""
    missing = sorted(_app_js_keys() - _base_html_keys())
    assert not missing, (
        "static/app.js чете %d ключа, които ги НЯМА в js_i18n речника на\n"
        "templates/base.html — тези низове ще останат на български при EN/TR:\n%s\n\n"
        "Добави ги в речника (обвити с _()), после пусни pybabel extract/update/compile."
        % (len(missing), _fmt_list(missing)))


def test_server_dictionary_has_no_keys_nobody_reads():
    """Обратната посока — мъртъв ключ в речника означава или печатна
    грешка в името, или премахнат от app.js низ, който продължава да се
    праща по мрежата на всяка страница."""
    unused = sorted(_base_html_keys() - _app_js_keys())
    assert not unused, (
        "В js_i18n речника на templates/base.html има %d ключа, които никой в\n"
        "static/app.js не чете (печатна грешка в името или остатък от премахнат код):\n%s"
        % (len(unused), _fmt_list(unused)))


def test_js_dictionary_strings_are_all_translated_in_both_catalogs():
    """Изрична проверка точно за низовете на app.js — те са най-лесни за
    пропускане, защото не се виждат в нито един шаблон като текст."""
    with open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8") as fh:
        block = fh.read().split("{% set js_i18n = {", 1)[1].split("} %}", 1)[0]
    strings = re.findall(r"_\('((?:[^'\\]|\\.)*)'\)", block)
    assert len(strings) >= 30, "js_i18n речникът внезапно се смали — %d низа" % len(strings)
    for lang in LANGS:
        catalog = _catalog(lang)
        bad = [s for s in strings
               if catalog.get(s) is None or not catalog.get(s).string]
        assert not bad, (
            "Низове от JS речника липсват/не са преведени в %s:\n%s%s"
            % (lang, _fmt_list(bad), FIXIT))


# ---------------------------------------------------------------- 5. дребни санитарни проверки

def test_both_catalogs_cover_exactly_the_same_msgids():
    """EN и TR трябва да са огледални — иначе някой е обновил само единия
    каталог и разминаването пак ще е тихо."""
    en = {m.id for m in _catalog("en") if m.id}
    tr = {m.id for m in _catalog("tr") if m.id}
    assert en == tr, (
        "Каталозите се разминават: само в EN — %d, само в TR — %d\n%s"
        % (len(en - tr), len(tr - en), _fmt_list(sorted((en - tr) | (tr - en)))))


def test_extraction_finds_the_expected_order_of_magnitude():
    """Груб предпазител срещу „смълчаване“ на теста по-горе: ако някой
    счупи babel.cfg или подаде грешна папка, extract_from_dir би върнал
    шепа низове и всички проверки биха минали ТРИВИАЛНО зелени."""
    found = _source_msgids()
    assert len(found) > 600, (
        "Извличането намери само %d низа — очакват се над 600. Провери\n"
        "babel.cfg (методите за python/jinja2) вместо да „поправяш“ теста." % len(found))
