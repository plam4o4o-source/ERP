# -*- coding: utf-8 -*-
"""Административен панел: системни настройки (мрежа/локален архив), отдалечен
достъп (Cloudflare тунел), управление на служители, и проверка/инсталиране на
обновления. Извлечено от app.py (Фаза 3).

Бележка (25.08.2026): синхронизацията с GitHub беше премахната по заявка на
потребителя — заедно с нея отпаднаха и системните ѝ настройки/бутони тук
(качване/изтегляне от GitHub). Локалният архив (папка/мрежов диск) остана."""
from flask import abort, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from werkzeug.security import generate_password_hash

import applog
import appcore
import backup
import config as appconfig
import db
import legacy_migration
import remote_tunnel
import updater
from version import __version__
from appcore import (admin_required, get_db, get_runtime_port,
                     invalidate_pending_restore_banner, password_policy_error)
from routes_auth import MAX_USERNAME_LENGTH

import ipaddress
import os
import re as _re
import tempfile
from urllib.parse import urlsplit

#: Одит (25.08.2026, предложение Д): груба, но достатъчна проверка за
#: „прилича ли на хост“ — букви/цифри/тире в етикети, разделени с точки
#: (домейн), или чист IPv4. Целта не е RFC-пълнота, а да отсече очевидно
#: невалидните адреси (без домейн, с интервал, с „?“/път), които иначе биха
#: влезли в печатния QR код като траен неработещ линк.
_HOSTNAME_RE = _re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _public_base_url_error(raw):
    """Връща съобщение за грешка, ако `raw` не е годен постоянен публичен
    адрес (предложение Д), или None, ако е наред. Очаква вече добавена схема
    (http/https) от извикващия."""
    # Интервалът се проверява ПЪРВИ и с първоначалното съобщение (пази
    # съвместимост с регресионния тест от находка №2/gaps) — той и обърква
    # разбора по-долу.
    if " " in raw:
        return _("Адресът не изглежда валиден — не трябва да съдържа интервали.")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return _("Адресът не изглежда валиден.")
    if parts.scheme not in ("http", "https"):
        return _("Адресът трябва да започва с http:// или https://.")
    # netloc може да включва порт (и по изключение потребител@) — за проверката
    # ни трябва само хостът.
    host = parts.hostname or ""
    if not host:
        return _("Адресът трябва да съдържа домейн (напр. https://firma.example.com).")
    # Одит (04.10.2026, S4): „потребител@“ в адрес, отпечатан на бланка, е
    # или грешка, или опит за подвеждане („https://banka.bg@zlo.example“).
    if "@" in parts.netloc:
        return _("Адресът не трябва да съдържа потребител или парола (част с „@“).")
    # Одит (04.10.2026, S4): портът досега изобщо не се проверяваше —
    # „https://example.com:abc“ минаваше (urlsplit гърми чак при достъп до
    # .port), а „javascript:alert(1)“ ставаше „https://javascript:alert(1)“.
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port is not None and not 1 <= port <= 65535:
        return _("Портът в адреса трябва да е число между 1 и 65535.")
    # Път/заявка/фрагмент нямат място в базов адрес — те се долепят по-късно
    # при строенето на конкретния линк към документа.
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return _("Въведете само адреса на сайта, без път или параметри след домейна.")
    if not _host_is_valid(host):
        return _("Домейнът в адреса не изглежда валиден.")
    return None


def _host_is_valid(host):
    """Одит (04.10.2026, S4): IP адрес (v4/v6) или име на хост; домейн на
    кирилица/с диакритика (IDNA, напр. „фирма.бг“) се проверява в ASCII вида
    си (punycode), както го праща браузърът."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return bool(_HOSTNAME_RE.match(ascii_host))


#: Одит (04.10.2026, S4): „схема:“ в началото, която НЕ е „хост:порт“ —
#: напр. „javascript:alert(1)“, „ftp://…“, „mailto:…“. Такъв вход не бива да
#: получава долепено „https://“ (ставаше „https://javascript:alert(1)“).
_FOREIGN_SCHEME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9+-]*:(?!\d+(?:/|$))")


def _folder_setting_error(raw):
    """Одит (04.10.2026, R8): папката за архив/клиентски копия се проверява
    ПРИ ЗАПИС, не чак при първия архив. Досега се приемаше всичко (вкл.
    „/etc/passwd“ — файл) и грешката излизаше часове по-късно, в нощния
    автоматичен архив. Празно = изключено (позволено). Иначе: пълен път,
    папка (не файл), съществуваща или създаваема, с право на запис
    (проба с временен файл). Връща (съобщение или None, нормализиран път)."""
    folder = (raw or "").strip()
    if not folder:
        return None, ""
    folder = os.path.expanduser(folder)
    if not os.path.isabs(folder):
        return _("Въведете пълен път до папката (напр. D:\\Архив или "
                 "\\\\СЪРВЪР\\споделена\\архив), не относителен."), folder
    if os.path.exists(folder) and not os.path.isdir(folder):
        return _("Пътят „%(path)s“ сочи към файл, а не към папка.") % {"path": folder}, folder
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            return (_("Папката „%(path)s“ не съществува и не може да бъде създадена "
                      "(%(reason)s).") % {"path": folder, "reason": exc.strerror or exc},
                    folder)
    try:
        fd, probe = tempfile.mkstemp(prefix="ph_logistics_probe_", suffix=".tmp",
                                     dir=folder)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        return (_("Няма право на запис в папката „%(path)s“ (%(reason)s) — изберете "
                  "друга папка.") % {"path": folder, "reason": exc.strerror or exc},
                folder)
    return None, folder


def _render_with_typed(con, overrides):
    """Одит (04.10.2026, R8): при отказ страницата се показва наново СЪС
    ВЪВЕДЕНОТО (не пренасочване към записаното) — за да се поправи на място."""
    import routes_settings
    ctx = routes_settings.system_context(con)
    ctx["s"] = dict(ctx["s"], **overrides)
    if _came_from_system():
        return render_template("my_settings.html", system_view=True, **ctx)
    ctx.update(routes_settings.personal_context(con))
    return render_template("my_settings.html", **ctx)


def register(app):
    app.add_url_rule("/admin/system", "system_settings", system_settings, methods=["GET", "POST"])
    app.add_url_rule("/admin/system/backup-now", "system_backup_now",
                     system_backup_now, methods=["POST"])
    app.add_url_rule("/admin/system/restore", "system_restore_request",
                     system_restore_request, methods=["POST"])
    app.add_url_rule("/admin/system/restore/cancel", "system_restore_cancel",
                     system_restore_cancel, methods=["POST"])
    app.add_url_rule("/admin/system/shared-rename", "system_shared_rename",
                     system_shared_rename, methods=["POST"])
    app.before_request(_flash_restore_result)
    app.before_request(_flash_migration_report)
    # Бележка (25.08.2026): маршрутите /admin/system/backup-github-now и
    # /admin/system/pull-now (качване/изтегляне от GitHub) отпаднаха заедно с
    # премахнатата синхронизация с GitHub. Локалният архив остана.
    app.add_url_rule("/admin/system/remote-start", "system_remote_start",
                     system_remote_start, methods=["POST"])
    app.add_url_rule("/admin/system/remote-stop", "system_remote_stop",
                     system_remote_stop, methods=["POST"])
    app.add_url_rule("/admin/system/remote-status", "system_remote_status",
                     system_remote_status)

    app.add_url_rule("/admin/users", "admin_users", admin_users)
    app.add_url_rule("/admin/users/new", "admin_user_new", admin_user_new, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/toggle", "admin_user_toggle",
                     admin_user_toggle, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/password", "admin_user_password",
                     admin_user_password, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/delete", "admin_user_delete",
                     admin_user_delete, methods=["POST"])

    app.add_url_rule("/update/check", "update_check", update_check)
    app.add_url_rule("/update/install", "update_install", update_install, methods=["POST"])
    app.add_url_rule("/update/complete-rename", "update_complete_rename",
                     update_complete_rename, methods=["POST"])


# ---------------------------------------------------------------- системни настройки (админ,
# показвани вградени в „Настройки“ — вижте routes_settings.my_settings)

def _came_from_system():
    try:
        came_from = urlsplit(request.referrer or "").path
    except ValueError:
        came_from = ""
    return came_from == url_for("system_settings")


def _back(**kwargs):
    """Одит (01.10.2026, P12): след запис — обратно на страницата, от която
    е изпратена формата („Система“ или „Настройки“)."""
    if _came_from_system():
        return url_for("system_settings", **kwargs)
    return url_for("my_settings", **kwargs)


@admin_required
def system_settings():
    con = get_db()
    if request.method == "GET":
        # Одит (01.10.2026, P12): самостоятелна страница „Система“ (досега —
        # пренасочване към личните настройки, където системните са най-долу).
        import routes_settings
        return render_template("my_settings.html", system_view=True,
                               **routes_settings.system_context(con))
    form = request.form.get("form")
    if form == "network":
        # Дребни (одит): int(request.form.get("network_port")) гърмеше с
        # необработен ValueError (500 грешка) при НЕЧИСЛОВ или празен след
        # strip() вход (напр. случайно вмъкнат текст в полето) — вместо
        # ясно съобщение „невалиден порт“. Проверяваме и допустимия
        # диапазон на TCP порт (1-65535), не само че е число.
        port_raw = request.form.get("network_port", "").strip()
        try:
            port = int(port_raw) if port_raw else 5000
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            flash(_("Невалиден мрежов порт — въведете число между 1 и 65535."), "error")
            return redirect(_back())
        # Одит (31.08.2026, находка №11): пътят до базата се валидира също
        # толкова строго, колкото порта над него. Печатна грешка тук е
        # най-скъпата в цялата програма — виж config.validate_db_path.
        db_error, db_path_value = appconfig.validate_db_path(
            request.form.get("db_path", ""),
            allow_new=request.form.get("db_path_new") == "on")
        if db_error:
            flash(db_error, "error")
            return redirect(_back())
        appconfig.save_config({
            "db_path": db_path_value,
            "network_mode": request.form.get("network_mode") == "on",
            "network_port": port,
        })
        applog.log_audit("променени мрежови настройки",
                        "db_path=%r, network_mode=%s, port=%s" % (
                            db_path_value,
                            request.form.get("network_mode") == "on", port))  # находка №51
        flash(_("Мрежовите настройки са запазени. Рестартирайте програмата, "
             "за да влязат в сила."), "success")
    elif form == "login_scene":
        # Изглед на входния екран (заявка: „запази този [класическия] и
        # добави опция да може да се сменя в настройките“) — обща
        # настройка за инсталацията, виж db.LOGIN_SCENES/get_login_scene.
        scene = request.form.get("login_scene", "")
        if scene not in db.LOGIN_SCENES:
            scene = db.DEFAULT_LOGIN_SCENE
        db.save_settings(con, {"login_scene": scene})
        con.commit()
        flash(_("Изгледът на входния екран е запазен."), "success")
    elif form == "backup_folder":
        backup_values = {
            "backup_folder": request.form.get("backup_folder", "").strip(),
            "backup_auto": "on" if request.form.get("backup_auto") == "on" else "",
        }
        folder_error, _folder = _folder_setting_error(backup_values["backup_folder"])
        if folder_error:
            flash(folder_error, "error")
            return _render_with_typed(con, backup_values)
        db.save_settings(con, backup_values)
        con.commit()
        # Одит (26.09.2026, находка №6): къде отиват архивите (с копие на
        # цялата база) е чувствителна настройка — досега без запис в дневника.
        applog.log_audit("променени настройки за архив",
                         "папка=%r, автоматично=%s" % (
                             backup_values["backup_folder"],
                             bool(backup_values["backup_auto"])))
        flash(_("Настройките за локален/мрежов архив са запазени."), "success")
    elif form == "public_base_url":
        # Одит (22.08.2026, находка №2): постоянният публичен адрес, който
        # влиза в QR кода на ПЕЧАТНАТА бланка. Виж routes_documents.
        # _public_doc_url: тунелният адрес е ефимерен (Cloudflare преизползва
        # поддомейните), затова върху хартия има работа само стабилен адрес.
        raw = request.form.get("public_base_url", "").strip()
        if raw and not raw.lower().startswith(("http://", "https://")):
            if _FOREIGN_SCHEME_RE.match(raw):
                flash(_("Адресът трябва да започва с http:// или https://."), "error")
                return redirect(_back(public_base_url_retry=raw))
            raw = "https://" + raw
        # Одит (25.08.2026, предложение Д): валидираме ХОСТА, не само за
        # интервали. Този адрес влиза буквално в QR кода на ПЕЧАТНАТА бланка;
        # невалиден хост (напр. „https://“ без домейн, „https://???“, или адрес
        # с път/интервал) означаваше траен неработещ QR върху официален
        # документ — открива се чак когато насрещната страна не може да отвори
        # линка. По-добре ясна грешка при запис, отколкото мълчаливо счупен
        # печат. ВАЖНО: валидацията е ПРЕДИ rstrip("/") — иначе „https://“ се
        # окастряше до „https:“ и после минаваше като (безсмислен) хост.
        if raw:
            invalid = _public_base_url_error(raw)
            if invalid:
                flash(invalid, "error")
                # Одит (05.09.2026, подобрение): сгрешеното ОСТАВА в полето,
                # за да се поправи, вместо да се пише отначало. Съседните
                # форми (редакция на документ, дублиран номер на фактура)
                # точно в такъв случай пазят въведеното — тази не.
                return redirect(_back(public_base_url_retry=raw))
        # Съхраняваме без завършващ „/“ (конкретният линк го долепя сам).
        raw = raw.rstrip("/")
        db.save_settings(con, {"public_base_url": raw})
        con.commit()
        applog.log_audit("променен постоянен публичен адрес", "url=%s" % (raw or "(изчистен)"))
        flash(_("Постоянният публичен адрес е запазен. Новоотпечатаните QR кодове "
                "ще го ползват.") if raw else
              _("Постоянният публичен адрес е изчистен — QR кодовете отново ще "
                "ползват локалния адрес."), "success")
    elif form == "client_export":
        export_values = {
            "client_export_dir": request.form.get("client_export_dir", "").strip(),
            "client_export_auto": "on" if request.form.get("client_export_auto") == "on" else "",
        }
        folder_error, _folder = _folder_setting_error(export_values["client_export_dir"])
        if folder_error:
            flash(folder_error, "error")
            return _render_with_typed(con, export_values)
        db.save_settings(con, export_values)
        con.commit()
        # Одит (26.09.2026, находка №6): същото като при папката за архив.
        applog.log_audit("променени настройки за клиентски папки",
                         "папка=%r, автоматично=%s" % (
                             export_values["client_export_dir"],
                             bool(export_values["client_export_auto"])))
        flash(_("Настройките за клиентски папки са запазени."), "success")
    # Бележка (25.08.2026): формата „backup_github“ (настройки за GitHub
    # синхронизация) отпадна заедно с премахнатата функция. Остана само
    # локалният архив (формата „backup_folder“ по-горе).
    return redirect(_back())


@admin_required
def system_backup_now():
    con = get_db()
    folder = db.get_settings(con).get("backup_folder", "").strip()
    try:
        path = backup.local_backup(folder)
        flash(_("Резервно копие е записано: %s") % path, "success")
    except Exception as exc:
        # Одит (04.10.2026, I3): db.error_text — преведеното съобщение на
        # backup.BackupError (досега суров български текст в преведена рамка).
        flash(_("Архивирането е неуспешно: %s") % db.error_text(exc), "error")
    return redirect(_back())


@admin_required
def system_restore_request():
    """Одит (01.10.2026, O1): насрочва възстановяване от архив в настроената
    папка — самото възстановяване става при следващото стартиране."""
    con = get_db()
    folder = db.get_settings(con).get("backup_folder", "").strip()
    try:
        path = backup.request_restore(folder, request.form.get("backup_name", ""),
                                      session.get("username", ""))
    except (ValueError, OSError) as exc:
        flash(_("Възстановяването не е насрочено: %s") % db.error_text(exc), "error")
        return redirect(_back())
    invalidate_pending_restore_banner()  # банерът да се появи веднага
    flash(_("Възстановяването от %(name)s е насрочено. Затворете програмата на "
            "ВСИЧКИ компютри и я стартирайте отново — архивът ще бъде възстановен "
            "при стартирането, преди някой да отвори базата. Текущата база ще бъде "
            "запазена в папка pre_restore_… до нея.") % {"name": os.path.basename(path)},
          "warning")
    return redirect(_back())


@admin_required
def system_restore_cancel():
    backup.cancel_restore()
    invalidate_pending_restore_banner()
    applog.log_audit("отменено насрочено възстановяване от архив")
    flash(_("Насроченото възстановяване е отменено."), "info")
    return redirect(_back())


#: Одит (01.10.2026, O1): за кои бази този процес вече е проверил резултата
#: от възстановяване (файлът се чете веднъж и се изтрива).
_restore_result_checked = set()


def _flash_restore_result():
    """Показва резултата от възстановяването на първия администратор след старта."""
    if session.get("role") != "admin" or db.DB_PATH in _restore_result_checked:
        return
    _restore_result_checked.add(db.DB_PATH)
    result = backup.take_restore_result()
    if not result:
        return
    name = os.path.basename(str(result.get("backup") or "")) or "?"
    if result.get("ok"):
        flash(_("Базата е възстановена от архива %(name)s. Предишната база е запазена "
                "в папка %(aside)s.") % {"name": name, "aside": result.get("aside", "")},
              "success")
        if result.get("files_error"):
            flash(_("Прикачените файлове и логото от архива НЕ бяха възстановени: %s")
                  % (db.record_text(result.get("files_error_record"))
                     or result["files_error"]), "error")
    else:
        # Одит (04.10.2026, I9): причината е записана и като msgid
        # (error_record) — показва се преведена; стар файл с резултат има само
        # българския текст в "error".
        flash(_("Възстановяването от архива %(name)s НЕ е извършено — текущата база е "
                "непроменена. Причина: %(reason)s")
              % {"name": name, "reason": db.record_text(result.get("error_record"))
                 or result.get("error", "")}, "error")


# ---------------------------------------------------------------- преход към новите имена
# Одит (06.10.2026): резултатът от прехода (legacy_migration, ph_migration.json)
# се показва ВЕДНЪЖ на първия администратор след старта.
_migration_report_checked = set()


def migration_reason_text(code):
    """Причината, поради която данните са оставени на старото място."""
    texts = {
        "shared": _("старата папка е споделена в мрежата"),
        "network": _("старата папка е на мрежов диск"),
        "registry": _("не може да се провери дали старата папка е споделена в мрежата"),
        "running": _("старата версия на програмата още работеше"),
        "move_failed": _("файл в старата папка беше зает"),
        "db_path_inside": _("пътят до базата в настройките сочи изрично към старата папка"),
        "backup_inside": _("папката за архив е в старата папка"),
        "restore_inside": _("насроченото възстановяване е от архив в старата папка"),
        "db_unreadable": _("базата не можа да бъде проверена"),
        "config_unreadable": _("конфигурационният файл е повреден"),
        "conflict": _("в новата папка вече има файлове със същите имена"),
        # Одит (07.10.2026): преименуването на място (преносима инсталация).
        "used_by_others": _("базата се ползва и от други компютри"),
    }
    return texts.get(code) or str(code or "?")


def _flash_migration_report():
    if session.get("role") != "admin":
        return
    install_dir = os.path.dirname(appconfig.CONFIG_PATH) or "."
    if install_dir in _migration_report_checked:
        return
    _migration_report_checked.add(install_dir)
    current = legacy_migration.last_result() or {}
    if current.get("status") in ("incomplete", "error"):
        report = current  # не е записан като показан — дневникът трябва
    else:
        report = legacy_migration.take_report_for_admin(install_dir)
    if not report:
        return
    status = report.get("status")
    old, new = report.get("legacy_dir", ""), report.get("new_dir", "")
    if status == "moved":
        flash(_("PH Logistics: данните са преместени от %(old)s в %(new)s с новите "
                "имена на файловете. Нищо не е изтрито.") % {"old": old, "new": new},
              "success")
    elif status in ("pointer", "pointer_runtime"):
        flash(_("PH Logistics: %(reason)s — данните остават в %(folder)s и програмата "
                "сочи към тях.") % {"reason": migration_reason_text(report.get("reason")),
                                    "folder": old}, "info")
    elif status == "renamed":
        flash(_("PH Logistics: файловете с данни в %(folder)s са преименувани на новите "
                "имена (ph_…). Нищо не е изтрито.") % {"folder": old}, "success")
    elif status == "kept":
        flash(_("PH Logistics: файловете с данни в %(folder)s запазват старите си имена "
                "(%(reason)s) — програмата работи с тях нормално.")
              % {"folder": old, "reason": migration_reason_text(report.get("reason"))},
              "info")
    elif status == "both":
        flash(_("PH Logistics: и старата папка %(old)s съдържа данни — те не са "
                "пипани. Програмата работи с данните в %(new)s.") % {"old": old, "new": new},
              "info")
    else:
        flash(_("PH Logistics: преместването на данните не завърши (%(error)s). "
                "Нищо не е изтрито — данните са в %(old)s и %(new)s.")
              % {"error": report.get("error", ""), "old": old, "new": new}, "error")


@admin_required
def system_shared_rename():
    """Одит (07.10.2026): „Преименувай споделената база на новото име“
    (pacho_logistic.db → ph_logistics.db в същата папка) — виж db.
    rename_shared_database. Версиите преди 3.80 не се отчитат в базата,
    затова е нужно и изрично потвърждение от администратора."""
    back = url_for("system_settings") + "#instances"
    if request.form.get("confirm_all_updated") != "on":
        flash(_("Отбележете, че всички компютри са обновени до 3.80 или по-нова — "
                "иначе базата не се преименува."), "error")
        return redirect(back)
    old = db.DB_PATH
    # Връзката на тази заявка се затваря — под Windows отворен файл не може
    # да бъде преименуван.
    appcore._close_db()
    try:
        new = db.rename_shared_database()
    except db.TranslatableError as exc:
        applog.log_audit("неуспешно преименуване на споделената база",
                         "%s: %s" % (old, exc.message_bg))
        flash(_("Споделената база НЕ е преименувана: %s") % exc, "error")
        return redirect(back)
    applog.log_audit("преименувана споделената база", "%s → %s" % (old, new))
    flash(_("Споделената база вече е %(name)s. Другите компютри (версия 3.80 или "
            "по-нова) я намират сами при следващото отваряне.")
          % {"name": new}, "success")
    return redirect(back)


# Бележка (25.08.2026): функциите system_backup_github_now (качване в GitHub)
# и system_pull_now (изтегляне от GitHub) отпаднаха заедно с премахнатата
# синхронизация с GitHub. Локалният архив остана (system_backup_now по-горе).


# ---------------------------------------------------------------- отдалечен достъп (сканиране с телефон)

@admin_required
def system_remote_start():
    # Одит (12.08.2026, находка №10): реално използваният порт (може да е
    # различен от конфигурирания, ако е бил зает при стартиране — виж
    # appcore.set_runtime_port/app.py) вместо сляпо да се чете
    # конфигурацията, която може да сочи към вече незает от тази сесия
    # порт.
    configured_port = appconfig.get_network_port(appconfig.load_config())
    port = get_runtime_port(configured_port)
    remote_tunnel.start(port)
    flash(_("Стартира се отдалечен достъп… изчакайте няколко секунди, статусът "
         "по-долу ще се обнови автоматично."), "info")
    return redirect(_back())


@admin_required
def system_remote_stop():
    remote_tunnel.stop()
    flash(_("Отдалеченият достъп е спрян."), "success")
    return redirect(_back())


@admin_required
def system_remote_status():
    return remote_tunnel.status()


# ---------------------------------------------------------------- админ панел

@admin_required
def admin_users():
    con = get_db()
    users = con.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("admin_users.html", users=users)


@admin_required
def admin_user_new():
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    password = request.form.get("password", "")
    role = "admin" if request.form.get("role") == "admin" else "employee"
    if not username or not password:
        flash(_("Потребителско име и парола са задължителни."), "error")
        return redirect(url_for("admin_users"))
    # Одит (26.09.2026, находка №1): /login не приема по-дълги имена (виж
    # routes_auth.MAX_USERNAME_LENGTH) — такъв акаунт не би могъл да влезе.
    if len(username) > MAX_USERNAME_LENGTH:
        flash(_("Потребителското име трябва да е най-много %d символа.")
              % MAX_USERNAME_LENGTH, "error")
        return redirect(url_for("admin_users"))
    # Одит (04.10.2026, S2): единната проверка (дължина, често срещани,
    # поредици, съдържа името) — виж appcore.password_policy_error.
    err = password_policy_error(password, username)
    if err:
        flash(err, "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    # Одит (04.10.2026, F11): проверката е БЕЗ регистър (ci_lower — Unicode,
    # вкл. кирилица) — „ivan“ и „IVAN“ ставаха два отделни акаунта, а хората
    # не различават имената по главни букви. Входът (/login) остава с точно
    # съвпадение: след тази проверка нови такива двойки не могат да
    # възникнат, а при вече съществуващи (стари бази) входът без регистър би
    # бил нееднозначен — кой от двата акаунта да отвори. BEGIN IMMEDIATE:
    # две едновременни създавания („ivan“/„IVAN“) не минават и двете.
    con.execute("BEGIN IMMEDIATE")
    exists = con.execute("SELECT username FROM users WHERE ci_lower(username) = ci_lower(?)",
                         (username,)).fetchone()
    if exists:
        con.rollback()
        flash(_("Вече има служител с потребителско име „%s“.") % exists["username"], "error")
    else:
        # must_change_password=1: администраторът вече знае тази парола
        # (той я е въвел тук), затова не е лична тайна на служителя —
        # задължаваме смяна при първия му вход.
        con.execute(
            "INSERT INTO users"
            " (username, password_hash, full_name, role, active, must_change_password)"
            " VALUES (?, ?, ?, ?, 1, 1)",
            (username, generate_password_hash(password), full_name, role),
        )
        con.commit()
        # Одит (26.09.2026, находка №6): създаването на акаунт (особено
        # администраторски) досега не оставяше следа в дневника.
        applog.log_audit("създаден служител", "потребител=%s, роля=%s" % (username, role))
        flash(_("Служителят „%s“ е добавен. Ще трябва да смени паролата при първия вход.") % username, "success")
    return redirect(url_for("admin_users"))


#: Одит (31.08.2026, находка №1, ВИСОКА): съобщението е едно и също за
#: деактивиране и за изтриване — и в двата случая проблемът е един: това е
#: последният администратор, който още може да влезе.
_LAST_ADMIN_MSG = _(
    "Това е последният активен администратор — ако го деактивирате или изтриете, "
    "никой няма да може да влезе в администрацията (управление на служители, "
    "системни настройки, архивиране, обновяване). Първо направете друг "
    "потребител администратор.")


def _would_leave_no_active_admin(con, user_id):
    """Одит (31.08.2026, находка №1, ВИСОКА): вярно, ако след деактивиране/
    изтриване на `user_id` НЕ би останал нито един активен администратор.

    Досега единствената защита беше „не можеш да пипнеш СЕБЕ СИ“. Тя пази
    инварианта последователно (последният админ не може да пипне себе си),
    но НЕ и конкурентно: адмиН A изпълнява `UPDATE … WHERE id=B`, докато в
    друга нишка на waitress адмиН B изпълнява огледалното за A. И двете
    заявки минават проверката „не съм аз“ и двете commit-ват.

    Проверено с изпълнение (2 админа, 2 тестови клиента, threading.Barrier):
    и двата POST-а върнаха 302, крайно състояние — НУЛА активни
    администратора. Рестартът не помага: db.init_db засява „admin“ само при
    ПРАЗНА таблица users, а тук потребители има. Оттам нататък управлението
    на служители, системните настройки (вкл. пътя до базата), архивирането и
    обновяването са недостъпни завинаги; изходът е ръчна редакция на .db
    файла — нещо, което тази потребителска група не може да направи.

    Извиква се ВИНАГИ вътре в отворена `BEGIN IMMEDIATE` транзакция (виж
    двата извикващи маршрута) — само така проверката и самата промяна са
    едно неделимо цяло и второто едновременно деактивиране вижда вече
    намаленото множество."""
    return con.execute(
        "SELECT COUNT(*) AS c FROM users"
        " WHERE role = 'admin' AND active = 1 AND id <> ?",
        (user_id,)).fetchone()["c"] == 0


@admin_required
def admin_user_toggle(user_id):
    if user_id == session["user_id"]:
        flash(_("Не можете да деактивирате собствения си акаунт."), "error")
        return redirect(url_for("admin_users"))
    # Одит (31.08.2026, находка №13): очакваното състояние идва от реда,
    # който администраторът е ВИЖДАЛ (скрито поле в admin_users.html).
    # Липсва (стара отворена страница) → третираме го като „не знам“ и
    # пропускаме проверката, за да не счупим работещ поток.
    expected_raw = request.form.get("expected_active", "")
    expected = expected_raw if expected_raw in ("0", "1") else None

    con = get_db()
    # Одит (26.09.2026, находка №3, средна): превключването вдига и
    # session_epoch (виж db._m007_session_epoch), както смяната на парола.
    # Деактивирането само блокираше старите бисквитки, докато active=0 —
    # след повторно активиране ВСИЧКИ стари (и евентуално откраднати)
    # сесии на служителя оживяваха.
    # BEGIN IMMEDIATE: проверката за „последен администратор“ и самата
    # промяна трябва да са неделими (находка №1) — иначе две едновременни
    # деактивирания и двете виждат „има още един активен“.
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT active, role FROM users WHERE id = ?",
                          (user_id,)).fetchone()
        if row is None:
            con.rollback()
            abort(404)
        deactivating = bool(row["active"])
        if deactivating and row["role"] == "admin" and _would_leave_no_active_admin(con, user_id):
            con.rollback()
            flash(_LAST_ADMIN_MSG, "error")
            return redirect(url_for("admin_users"))
        if expected is not None:
            # Находка №13: условен UPDATE + проверка на rowcount, същият
            # оптимистичен модел като при документите.
            cur = con.execute(
                "UPDATE users SET active = 1 - active, session_epoch = session_epoch + 1"
                " WHERE id = ? AND active = ?",
                (user_id, int(expected)))
            if cur.rowcount == 0:
                con.rollback()
                flash(_("Състоянието на този акаунт е било променено междувременно "
                        "— страницата е презаредена, проверете и опитайте пак."),
                      "warning")
                return redirect(url_for("admin_users"))
        else:
            con.execute("UPDATE users SET active = 1 - active, session_epoch = session_epoch + 1"
                        " WHERE id = ?", (user_id,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    applog.log_audit("променено състояние на служител",
                     "user_id=%s активен=%s" % (user_id, 0 if deactivating else 1))
    return redirect(url_for("admin_users"))


@admin_required
def admin_user_password(user_id):
    password = request.form.get("password", "")
    if not password:
        flash(_("Въведете нова парола."), "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    # Одит (01.09.2026, девети одит, находка №1): проверка, че служителят
    # изобщо СЪЩЕСТВУВА — огледално на admin_user_toggle/admin_user_delete
    # точно над/под този маршрут. Досега UPDATE-ът стреляше сляпо: админ Б
    # със стар отворен таб натиска „Нулирай парола“ на служител, когото
    # админ А междувременно е изтрил → UPDATE засяга 0 реда → зелено
    # „Паролата е сменена“ + ред в одитния лог за НЕСЪЩЕСТВУВАЩ потребител.
    # Същият клас (0 rowcount → подвеждащо „готово“) е поправян вече три
    # пъти: находка №22 (delete_document) и №33 (client_delete/
    # invoice_client_delete) — това беше останалата непокрита половина.
    row = con.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        abort(404)
    err = password_policy_error(password, row["username"])  # Одит (04.10.2026, S2)
    if err:
        flash(err, "error")
        return redirect(url_for("admin_users"))
    # must_change_password=1 по същата причина, както при admin_user_new —
    # администраторът, не служителят, е избрал тази парола.
    # session_epoch = session_epoch + 1 (одит 16.08.2026, находка №5): виж
    # db._m007_session_epoch — прекратява ВСЯКА вече отворена сесия на този
    # потребител (напр. служителят е забравил да излезе на споделен
    # компютър — администраторът сменя паролата именно, за да го изкара).
    con.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1,"
        " session_epoch = session_epoch + 1 WHERE id = ?",
        (generate_password_hash(password), user_id))
    con.commit()
    applog.log_audit("нулирана парола на служител", "user_id=%s" % user_id)  # находка №51
    flash(_("Паролата е сменена. Служителят ще трябва да я смени при следващия си вход."), "success")
    return redirect(url_for("admin_users"))


@admin_required
def admin_user_delete(user_id):
    if user_id == session["user_id"]:
        flash(_("Не можете да изтриете собствения си акаунт."), "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    # Одит (31.08.2026, находка №1): същата неделима проверка като при
    # деактивирането — изтриването на последния активен администратор
    # заключва фирмата извън собствената ѝ програма необратимо.
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT id, role, active FROM users WHERE id = ?",
                      (user_id,)).fetchone()
    if row is None:
        con.rollback()
        abort(404)
    if row["role"] == "admin" and row["active"] and _would_leave_no_active_admin(con, user_id):
        con.rollback()
        flash(_LAST_ADMIN_MSG, "error")
        return redirect(url_for("admin_users"))
    con.execute("UPDATE documents SET created_by = NULL WHERE created_by = ?", (user_id,))
    # Одит (19.08.2026, находка №16): и прикачените файлове. `document_
    # attachments.uploaded_by` е външен ключ към users БЕЗ ON DELETE
    # правило — преди тази поправка изтриването на служител, който някога е
    # качвал прикачен файл (сканирана подписана бланка и т.н.), гърмеше с
    # „FOREIGN KEY constraint failed“, а потребителят виждаше генеричното
    # „Възникна неочаквана грешка“. Проверено с изпълнение: такъв служител
    # оставаше неизтриваем ЗАВИНАГИ, без никакво обяснение защо. NULL е
    # правилното поведение и тук — самият прикачен файл остава при
    # документа, губи се само авторството (както при documents.created_by).
    con.execute("UPDATE document_attachments SET uploaded_by = NULL WHERE uploaded_by = ?",
                (user_id,))
    con.execute("DELETE FROM users WHERE id = ?", (user_id,))
    con.commit()
    applog.log_audit("изтрит служител", "user_id=%s" % user_id)  # находка №51
    flash(_("Служителят е изтрит."), "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- обновяване

@admin_required
def update_check():
    """Ръчна проверка за нова версия в GitHub Releases.

    Одит (находка В5, висок риск): само @login_required преди поправката
    — всеки служител можеше да задейства инсталиране на нова версия
    (update_install по-долу РЕСТАРТИРА цялата програма за всички
    едновременно работещи потребители, viz. находка В6), без изобщо да е
    администратор."""
    try:
        info = updater.check_for_update()
    except Exception as exc:
        flash(_("Проверката за обновяване е неуспешна: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    updater.set_cache(info)  # М5: под заключване (виж updater._cache_lock), не директно
    if info["available"]:
        flash(_("Налична е нова версия %s (текущата е %s).") % (info["latest"], info["current"]), "info")
    else:
        flash(_("Използвате най-новата версия (%s).") % info["current"], "info")
    return redirect(url_for("dashboard"))


@admin_required
def update_install():
    """Изтегля новата версия и рестартира програмата."""
    try:
        info = updater.check_for_update()
    except Exception as exc:
        flash(_("Проверката за обновяване е неуспешна: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    if not info["available"]:
        flash(_("Вече използвате най-новата версия (%s).") % info["current"], "info")
        return redirect(url_for("dashboard"))
    try:
        # Одит (31.08.2026, находка №6): ръчният бутон СЪЗНАТЕЛНО пренебрегва
        # маркера за провалена подмяна — админът натиска „Обнови сега“ именно
        # след като е отстранил причината (затворил е програмата на другите
        # компютри, изключил е антивирусната блокировка). Автоматичният път
        # уважава маркера и така не се върти безкрайно.
        updater.clear_failed_install_marker()
        # Одит (06.10.2026): старата локална инсталация се обновява през
        # инсталатора — с обновяването завършва и преходът към новите имена.
        # Ако преходът за тази версия вече се е провалил — обновяване на място.
        setup = updater.setup_of(info)
        if setup and updater.use_setup_for_update(info.get("latest")):
            updater.install_via_setup(setup[0], setup[1], version=info.get("latest"))
        else:
            updater.install_update(info["download"], info.get("expected_sha256"),
                                   version=info.get("latest"), ignore_failed_marker=True)
    except Exception as exc:
        flash(_("Обновяването е неуспешно: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    # Дребни (одит): бланката updating.html показваше ТВЪРДО закодиран
    # http://127.0.0.1:5000 — ако администраторът е сменил мрежовия порт в
    # „Системни настройки“ (виж system_settings по-горе), този адрес е
    # ПОГРЕШЕН след рестарт, а операторът остава без работещ адрес.
    # Одит (12.08.2026, находка №10): реално използваният порт (виж
    # system_remote_start по-горе за същото разсъждение) вместо сляпо
    # четене на конфигурацията — важно при fallback на зает порт.
    configured_port = appconfig.get_network_port(appconfig.load_config())
    port = get_runtime_port(configured_port)
    return render_template("updating.html", latest=info["latest"], local_port=port)


@admin_required
def update_complete_rename():
    """Одит (06.10.2026): бутонът на таблото за старата локална инсталация —
    инсталаторът на ТЕКУЩАТА версия в папка PHLogistics; новото .exe
    премества данните при първия си старт (legacy_migration)."""
    if not updater.can_complete_rename():
        flash(_("Преминаването към новите имена не е възможно от тази инсталация."),
              "error")
        return redirect(url_for("dashboard"))
    try:
        updater.complete_rename()
    except Exception as exc:
        flash(_("Преминаването към новите имена не можа да започне: %s")
              % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    applog.log_audit("преход към новите имена", "версия %s" % __version__)
    configured_port = appconfig.get_network_port(appconfig.load_config())
    return render_template("updating.html", latest=__version__,
                           local_port=get_runtime_port(configured_port))
