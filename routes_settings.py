# -*- coding: utf-8 -*-
"""Настройки на фирмата изпращач (лого, данни) и лични настройки (тема),
плюс системните настройки вградени в „Моите настройки“ за администратори.
Извлечено от app.py (Фаза 3) без промяна в поведението."""
from flask import abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_babel import gettext as _

import branding
import config as appconfig
import db
from appcore import _select_locale, admin_required, get_db, login_required


def register(app):
    app.add_url_rule("/settings", "settings_page", settings_page, methods=["GET", "POST"])
    app.add_url_rule("/settings/logo", "settings_logo_upload",
                     settings_logo_upload, methods=["POST"])
    app.add_url_rule("/settings/logo/remove", "settings_logo_remove",
                     settings_logo_remove, methods=["POST"])
    app.add_url_rule("/logo.img", "company_logo_image", company_logo_image)
    app.add_url_rule("/my-settings", "my_settings", my_settings, methods=["GET", "POST"])


@admin_required
def settings_page():
    # Одит (находка В4, висок риск): преди поправката ВСЕКИ логнат
    # служител (не само администратор) можеше да POST-не тук и да смени
    # фирмените данни на изпращача — включително банковите (sender_iban/
    # sender_swift), които после излизат директно на банковия ред на
    # всяка издадена фактура. Лично проверено при одита: служителски
    # акаунт успешно смени sender_iban през тази заявка.
    con = get_db()
    if request.method == "POST":
        keys = ("sender_name", "sender_address", "sender_city", "sender_postcode",
                "sender_country", "sender_eik", "sender_vat", "sender_phone",
                "sender_email", "sender_person",
                # Лице за контакт — отделно от МОЛ (заявка по образеца
                # PL.xlsx: „добави лице за контакти... да се вмъква
                # автоматично както за клиент и за изпращач“). Зарежда се
                # автоматично в блока Изпращач на опаковъчния лист.
                "sender_contact",
                # Банкови данни — излизат на банковия ред на фактурите
                # (заявка: „във фирма изпращач добави IBAN-а на фирмата; да
                # се зарежда във фактурите“). Три отделни полета, защото
                # редът в приложените образци съдържа и трите:
                # „IBAN : … SWIFT : … / Postbank Gabrovo-Bulgaria /“.
                "sender_iban", "sender_swift", "sender_bank",
                # Английска версия — по избор, за БГ/EN превключвателя при
                # попълване на нов документ (виж routes_documents.py).
                "sender_name_en", "sender_address_en", "sender_city_en", "sender_country_en")
        db.save_settings(con, {k: request.form.get(k, "").strip() for k in keys})
        con.commit()
        flash(_("Данните на фирмата изпращач са запазени."), "success")
        return redirect(url_for("settings_page"))
    s = db.get_settings(con)
    return render_template("settings.html", s=s)


@admin_required
def settings_logo_upload():
    file = request.files.get("logo_file")
    if not file or not file.filename:
        flash(_("Моля, изберете файл с изображение."), "error")
        return redirect(url_for("settings_page"))
    try:
        branding.save_logo(file)
        flash(_("Логото на фирмата е качено успешно."), "success")
    except ValueError as exc:
        flash(_("Логото не бе прието: %s") % exc, "error")
    return redirect(url_for("settings_page"))


@admin_required
def settings_logo_remove():
    branding.remove_logo()
    flash(_("Логото на фирмата е премахнато."), "success")
    return redirect(url_for("settings_page"))


@login_required
def company_logo_image():
    path = branding.logo_path()
    if path is None:
        abort(404)
    return send_file(path, mimetype=branding.logo_mimetype(path))


@login_required
def my_settings():
    con = get_db()
    if request.method == "POST":
        # Темата и езикът се подават от ДВЕ отделни форми на страницата
        # (темата автоизпраща при избор на радио бутон, езикът — при
        # натискане на "Запази") — POST заявката съдържа само полето на
        # формата, която реално е изпратена, затова липсващото поле пада
        # към ТЕКУЩАТА вече запазена стойност, не към подразбиращата се
        # (иначе смяната на темата би нулирала избрания преди това език,
        # и обратно).
        current_theme = db.get_user_theme(con, session["user_id"])
        current_lang = db.get_user_language(con, session["user_id"]) or _select_locale()
        theme = request.form.get("theme", current_theme)
        if theme not in db.THEMES:
            theme = db.DEFAULT_THEME
        lang = request.form.get("language", current_lang)
        if lang not in db.LANGUAGES:
            lang = db.DEFAULT_LANGUAGE
        db.save_user_settings(con, session["user_id"], {"theme": theme, "language": lang})
        con.commit()
        session["theme"] = theme
        session["lang"] = lang
        flash(_("Настройките са запазени."), "success")
        return redirect(url_for("my_settings"))
    current_theme = db.get_user_theme(con, session["user_id"])
    current_lang = db.get_user_language(con, session["user_id"]) or _select_locale()
    ctx = {"themes": db.THEMES, "current_theme": current_theme,
           "languages": db.LANGUAGES, "current_user_lang": current_lang}
    if session.get("role") == "admin":
        # Системните настройки (мрежа/локален архив) се показват на същата
        # страница, видими само за администратори. Бележка (25.08.2026):
        # синхронизацията с GitHub (и статусът ѝ `sync`) отпадна.
        ctx.update(s=db.get_settings(con), cfg=appconfig.load_config(),
                  db_path=db.DB_PATH)
        # Одит (19.08.2026, находка №46, втора половина): докато уникалният
        # индекс (вид, година, номер) липсва заради ИСТОРИЧЕСКИ дубликати,
        # инсталацията работи без защитата от състезание при записване —
        # двама служители могат да издадат два документа с един и същ
        # номер. Досега единствената следа беше ред в лог файла, който
        # потребител на .exe никога не отваря. Тук опитваме създаването
        # още веднъж (ако админът току-що е почистил дубликатите,
        # предупреждението изчезва веднага, без да се чака рестарт) и,
        # ако пак не стане, показваме ВИДИМО предупреждение със списък
        # какво точно да се почисти.
        if db.unique_number_index_missing(con) and not db.ensure_unique_number_index(con):
            ctx["number_index_dupes"] = db.duplicate_number_rows(con)
            ctx["doc_types"] = db.DOC_TYPES
    return render_template("my_settings.html", **ctx)
