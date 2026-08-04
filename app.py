# -*- coding: utf-8 -*-
"""ПачоЛогистик — логистична програма за издаване на ЧМР товарителници,
опаковъчни листи и палетни карти с баркодове.

Стартиране:  python app.py  →  http://127.0.0.1:5000
Първоначален вход: потребител "admin", парола "admin123" (сменете я!).
"""
import io
import json
from datetime import date, datetime
from functools import wraps

from flask import (Flask, abort, flash, redirect, render_template, request,
                   Response, send_file, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

import db
from barcode128 import code128_svg

APP_NAME = "ПачоЛогистик"

app = Flask(__name__)
app.secret_key = db.get_secret_key()
app.json.ensure_ascii = False

db.init_db()


# ---------------------------------------------------------------- помощни

@app.context_processor
def inject_globals():
    return {
        "APP_NAME": APP_NAME,
        "current_year": date.today().year,
        "today": date.today().isoformat(),
    }


@app.template_filter("barcode")
def barcode_filter(code, height=55):
    return Markup(code128_svg(code, height=height))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def form_data(exclude=("csrf_token", "items_json")):
    """Всички полета от формата като речник (за съхранение в JSON)."""
    return {k: v.strip() for k, v in request.form.items() if k not in exclude}


def load_clients(con):
    return con.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()


def clients_json(clients):
    return json.dumps([dict(c) for c in clients], ensure_ascii=False)


def parse_items():
    """Редовете от таблицата с артикули, подадени като JSON от формата."""
    raw = request.form.get("items_json", "[]")
    try:
        items = json.loads(raw)
    except ValueError:
        items = []
    return items if isinstance(items, list) else []


def save_document(con, doc_type, data):
    number, year, seq, barcode = db.next_number(con, doc_type)
    data["number"] = number
    data["barcode"] = barcode
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_type, number, year, seq, barcode,
         json.dumps(data, ensure_ascii=False), session["user_id"]),
    )
    con.commit()
    return cur.lastrowid


def fetch_document(con, doc_id):
    row = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by WHERE d.id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row, json.loads(row["data"])


# ---------------------------------------------------------------- вход/изход

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        con = db.get_db()
        user = con.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        con.close()
        if user and user["active"] and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            target = request.args.get("next") or url_for("dashboard")
            if not target.startswith("/"):
                target = url_for("dashboard")
            return redirect(target)
        error = "Грешно потребителско име или парола, или акаунтът е деактивиран."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    flash("Излязохте от системата.")
    return redirect(url_for("login"))


# ---------------------------------------------------------------- табло и сканиране

@app.route("/")
@login_required
def dashboard():
    con = db.get_db()
    recent = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " ORDER BY d.id DESC LIMIT 10"
    ).fetchall()
    counts = {t: con.execute(
        "SELECT COUNT(*) AS c FROM documents WHERE doc_type = ? AND year = ?",
        (t, date.today().year),
    ).fetchone()["c"] for t in db.DOC_TYPES}
    con.close()
    return render_template("dashboard.html", recent=recent, counts=counts,
                           doc_types=db.DOC_TYPES,
                           recent_docs_meta=[json.loads(r["data"]) for r in recent])


@app.route("/scan", methods=["POST"])
@login_required
def scan():
    """Зареждане на документ чрез сканиран баркод (или въведен номер)."""
    code = request.form.get("code", "").strip()
    con = db.get_db()
    doc = con.execute("SELECT id FROM documents WHERE barcode = ?", (code,)).fetchone()
    if doc is None:
        # опит и по номер, напр. "0001/2026"
        doc = con.execute(
            "SELECT id FROM documents WHERE number = ? ORDER BY id DESC", (code,)
        ).fetchone()
    con.close()
    if doc is None:
        flash("Няма документ с баркод „%s“." % code)
        return redirect(url_for("dashboard"))
    return redirect(url_for("view_document", doc_id=doc["id"]))


# ---------------------------------------------------------------- документи (списък/преглед)

@app.route("/docs")
@login_required
def documents():
    doc_type = request.args.get("type", "")
    query = request.args.get("q", "").strip()
    sql = ("SELECT d.*, u.full_name AS author FROM documents d"
           " LEFT JOIN users u ON u.id = d.created_by WHERE 1=1")
    params = []
    if doc_type in db.DOC_TYPES:
        sql += " AND d.doc_type = ?"
        params.append(doc_type)
    if query:
        sql += " AND (d.number LIKE ? OR d.barcode LIKE ? OR d.data LIKE ?)"
        like = "%" + query + "%"
        params += [like, like, like]
    sql += " ORDER BY d.id DESC LIMIT 300"
    con = db.get_db()
    docs = con.execute(sql, params).fetchall()
    con.close()
    metas = [json.loads(d["data"]) for d in docs]
    return render_template("documents.html", docs=docs, metas=metas,
                           doc_types=db.DOC_TYPES, sel_type=doc_type, q=query)


@app.route("/doc/<int:doc_id>")
@login_required
def view_document(doc_id):
    con = db.get_db()
    row, data = fetch_document(con, doc_id)
    con.close()
    copies = request.args.get("copies", type=int) or 1
    template = {"cmr": "cmr_print.html",
                "packing": "packing_print.html",
                "pallet": "pallet_print.html"}[row["doc_type"]]
    return render_template(template, doc=row, d=data, copies=min(copies, 4))


@app.route("/doc/<int:doc_id>/delete", methods=["POST"])
@admin_required
def delete_document(doc_id):
    con = db.get_db()
    con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    con.commit()
    con.close()
    flash("Документът е изтрит.")
    return redirect(url_for("documents"))


@app.route("/barcode/<code>.svg")
@login_required
def barcode_svg(code):
    return Response(code128_svg(code), mimetype="image/svg+xml")


# ---------------------------------------------------------------- ЧМР

@app.route("/cmr/new", methods=["GET", "POST"])
@login_required
def cmr_new():
    con = db.get_db()
    if request.method == "POST":
        data = form_data()
        doc_id = save_document(con, "cmr", data)
        con.close()
        flash("ЧМР № %s е издадено и запазено в базата данни." % data["number"])
        return redirect(url_for("view_document", doc_id=doc_id))
    clients = load_clients(con)
    settings = db.get_settings(con)
    con.close()
    return render_template("cmr_form.html", clients=clients,
                           clients_json=clients_json(clients), s=settings)


# ---------------------------------------------------------------- Опаковъчен лист

@app.route("/packing/new", methods=["GET", "POST"])
@login_required
def packing_new():
    con = db.get_db()
    if request.method == "POST":
        data = form_data()
        data["items"] = parse_items()
        doc_id = save_document(con, "packing", data)
        con.close()
        flash("Опаковъчен лист № %s е издаден и запазен." % data["number"])
        return redirect(url_for("view_document", doc_id=doc_id))
    clients = load_clients(con)
    settings = db.get_settings(con)
    con.close()
    return render_template("packing_form.html", clients=clients,
                           clients_json=clients_json(clients), s=settings,
                           items=[])


# ---------------------------------------------------------------- Палетна карта

@app.route("/pallet/new", methods=["GET", "POST"])
@login_required
def pallet_new():
    con = db.get_db()
    if request.method == "POST":
        data = form_data()
        data["items"] = parse_items()
        doc_id = save_document(con, "pallet", data)
        con.close()
        flash("Палетна карта № %s е издадена и запазена." % data["number"])
        return redirect(url_for("view_document", doc_id=doc_id))
    clients = load_clients(con)
    settings = db.get_settings(con)
    con.close()
    return render_template("pallet_form.html", clients=clients,
                           clients_json=clients_json(clients), s=settings,
                           items=[])


@app.route("/pallet/import", methods=["POST"])
@login_required
def pallet_import():
    """Импорт на редове за палетна карта от Excel файл (.xlsx).

    Очаквани колони: Артикул/код | Описание | Количество | Тегло (кг).
    Първият ред се пропуска, ако изглежда като заглавен.
    """
    from openpyxl import load_workbook

    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash("Моля, изберете Excel файл (.xlsx).")
        return redirect(url_for("pallet_new"))
    try:
        wb = load_workbook(io.BytesIO(file.read()), data_only=True)
    except Exception:
        flash("Файлът не може да бъде прочетен. Уверете се, че е валиден .xlsx файл.")
        return redirect(url_for("pallet_new"))

    ws = wb.worksheets[0]
    items = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        cells = ["" if c is None else str(c).strip() for c in row[:4]]
        cells += [""] * (4 - len(cells))
        if not any(cells):
            continue
        if i == 0 and _looks_like_header(cells):
            continue
        items.append({"code": cells[0], "description": cells[1],
                      "qty": cells[2], "weight": cells[3]})

    if not items:
        flash("Във файла не бяха намерени редове с данни.")
        return redirect(url_for("pallet_new"))

    con = db.get_db()
    clients = load_clients(con)
    settings = db.get_settings(con)
    con.close()
    flash("Заредени са %d реда от „%s“. Прегледайте и издайте картата." %
          (len(items), file.filename))
    return render_template("pallet_form.html", clients=clients,
                           clients_json=clients_json(clients), s=settings,
                           items=items)


def _looks_like_header(cells):
    joined = " ".join(cells).lower()
    keywords = ("артикул", "код", "описание", "колич", "тегло",
                "code", "item", "description", "qty", "quantity", "weight")
    return any(k in joined for k in keywords)


@app.route("/pallet/sample.xlsx")
@login_required
def pallet_sample():
    """Примерен Excel файл за импорт на палетна карта."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Палетна карта"
    ws.append(["Артикул/код", "Описание", "Количество", "Тегло (кг)"])
    ws.append(["ART-001", "Кашон резервни части", 10, 125.5])
    ws.append(["ART-002", "Кутия крепежни елементи", 4, 38])
    for col, width in zip("ABCD", (16, 40, 14, 14)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="primeren_palet_import.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


# ---------------------------------------------------------------- адресна книга

@app.route("/clients")
@login_required
def clients_list():
    con = db.get_db()
    clients = load_clients(con)
    con.close()
    return render_template("clients.html", clients=clients)


@app.route("/clients/new", methods=["GET", "POST"])
@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def client_edit(client_id=None):
    con = db.get_db()
    client = None
    if client_id is not None:
        client = con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if client is None:
            con.close()
            abort(404)
    if request.method == "POST":
        fields = ("name", "address", "city", "postcode", "country", "eik",
                  "vat", "phone", "email", "contact")
        values = [request.form.get(f, "").strip() for f in fields]
        if not values[0]:
            flash("Името на фирмата е задължително.")
        else:
            if client is None:
                con.execute(
                    "INSERT INTO clients (%s) VALUES (%s)"
                    % (", ".join(fields), ", ".join("?" * len(fields))),
                    values,
                )
            else:
                con.execute(
                    "UPDATE clients SET %s WHERE id = ?"
                    % ", ".join(f + " = ?" for f in fields),
                    values + [client_id],
                )
            con.commit()
            con.close()
            flash("Клиентът е запазен в адресната книга.")
            return redirect(url_for("clients_list"))
    con.close()
    return render_template("client_form.html", client=client)


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def client_delete(client_id):
    con = db.get_db()
    con.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    con.commit()
    con.close()
    flash("Клиентът е изтрит от адресната книга.")
    return redirect(url_for("clients_list"))


# ---------------------------------------------------------------- фирма изпращач

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    con = db.get_db()
    if request.method == "POST":
        keys = ("sender_name", "sender_address", "sender_city", "sender_postcode",
                "sender_country", "sender_eik", "sender_vat", "sender_phone",
                "sender_email", "sender_person")
        db.save_settings(con, {k: request.form.get(k, "").strip() for k in keys})
        con.commit()
        con.close()
        flash("Данните на фирмата изпращач са запазени.")
        return redirect(url_for("settings_page"))
    s = db.get_settings(con)
    con.close()
    return render_template("settings.html", s=s)


# ---------------------------------------------------------------- админ панел

@app.route("/admin/users")
@admin_required
def admin_users():
    con = db.get_db()
    users = con.execute("SELECT * FROM users ORDER BY username").fetchall()
    con.close()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/new", methods=["POST"])
@admin_required
def admin_user_new():
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    password = request.form.get("password", "")
    role = "admin" if request.form.get("role") == "admin" else "employee"
    if not username or not password:
        flash("Потребителско име и парола са задължителни.")
        return redirect(url_for("admin_users"))
    con = db.get_db()
    exists = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    if exists:
        flash("Вече има служител с потребителско име „%s“." % username)
    else:
        con.execute(
            "INSERT INTO users (username, password_hash, full_name, role, active)"
            " VALUES (?, ?, ?, ?, 1)",
            (username, generate_password_hash(password), full_name, role),
        )
        con.commit()
        flash("Служителят „%s“ е добавен." % username)
    con.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_user_toggle(user_id):
    if user_id == session["user_id"]:
        flash("Не можете да деактивирате собствения си акаунт.")
        return redirect(url_for("admin_users"))
    con = db.get_db()
    con.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
    con.commit()
    con.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_user_password(user_id):
    password = request.form.get("password", "")
    if not password:
        flash("Въведете нова парола.")
        return redirect(url_for("admin_users"))
    con = db.get_db()
    con.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user_id))
    con.commit()
    con.close()
    flash("Паролата е сменена.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    if user_id == session["user_id"]:
        flash("Не можете да изтриете собствения си акаунт.")
        return redirect(url_for("admin_users"))
    con = db.get_db()
    con.execute("UPDATE documents SET created_by = NULL WHERE created_by = ?", (user_id,))
    con.execute("DELETE FROM users WHERE id = ?", (user_id,))
    con.commit()
    con.close()
    flash("Служителят е изтрит.")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- смяна на парола

@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        repeat = request.form.get("repeat", "")
        con = db.get_db()
        user = con.execute("SELECT * FROM users WHERE id = ?",
                           (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current):
            flash("Текущата парола е грешна.")
        elif len(new) < 4:
            flash("Новата парола трябва да е поне 4 символа.")
        elif new != repeat:
            flash("Двете нови пароли не съвпадат.")
        else:
            con.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new), session["user_id"]))
            con.commit()
            con.close()
            flash("Паролата е сменена успешно.")
            return redirect(url_for("dashboard"))
        con.close()
    return render_template("change_password.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
