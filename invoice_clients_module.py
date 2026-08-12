# -*- coding: utf-8 -*-
"""Адресна книга САМО за фактури — данни за фактуриране (Bill To) и адрес
за доставка (Consignee) на всеки клиент.

Заявка: „в раздел Фактури добави адресна книга; да съдържа данните за
фактуриране на клиентите и също да има адрес за доставка“.

Нарочно е ОТДЕЛНА от общата адресна книга (таблица `clients`, ползвана от
ЧМР/палетни карти/опаковъчни листи) — потвърдено и от потребителя. Двете
решават различни задачи: общата пази един адрес на фирма, раздробен на
град/пощенски код/държава за попълване на ЧМР кутии, докато фактурата има
нужда от ДВА различни адреса едновременно (стоката отива на едно място,
фактурата — на друго, при ABB редовно в различни държави) и всеки от тях
се показва като един многоредов блок, точно както е въведен.

Самите данни се КОПИРАТ в документа при издаване (както навсякъде другаде
в програмата) — по-късна промяна в адресната книга не пренаписва вече
издадени фактури.
"""
import jsonutil


_FIELDS = ("name", "delivery_name", "delivery_address", "delivery_phone",
           "billing_name", "billing_address", "billing_phone", "notes")


def load_all(con):
    """Всички записи, подредени по име — за списъка и падащите менюта."""
    return con.execute("SELECT * FROM invoice_clients ORDER BY name").fetchall()


def get(con, entry_id):
    return con.execute("SELECT * FROM invoice_clients WHERE id = ?", (entry_id,)).fetchone()


def as_json(con):
    """Записите като JSON за вграждане във формата — оттам JavaScript-ът
    попълва двата адресни блока при избор от менюто (виж
    bindInvoiceClientSelect в app.js). Същият модел като clients_json за
    общата адресна книга.

    Одит (находка К4, критична — XSS): преди тази поправка тук стоеше
    обикновен `json.dumps` — резултатът се вгражда директно в HTML атрибут
    в единични кавички (`data-entries='...'`, виж _invoice_macros.html) с
    `|safe`. `json.dumps` НЕ екранира апострофа, затова име на клиент като
    `ACME' onmouseover='alert(1)` прекъсваше атрибута и изпълняваше
    произволен JS за ВСЕКИ потребител (вкл. администратор), отворил форма
    за фактура — стандартен запис в тази адресна книга, достъпен за всеки
    служител. jsonutil.dumps_for_inline_script екранира точно тези опасни
    знаци (включително апострофа) към \\uXXXX escape поредици."""
    return jsonutil.dumps_for_inline_script([dict(r) for r in load_all(con)])


def save(con, form, entry_id=None):
    """Създава или обновява запис от подадената форма. Връща id-то."""
    values = {k: (form.get(k) or "").strip() for k in _FIELDS}
    if entry_id:
        con.execute(
            "UPDATE invoice_clients SET %s WHERE id = ?"
            % ", ".join("%s = ?" % f for f in _FIELDS),  # nosec B608 -- имената на колоните идват само от константата _FIELDS, не от потребителски вход
            [values[f] for f in _FIELDS] + [entry_id],
        )
        con.commit()
        return entry_id
    cur = con.execute(
        "INSERT INTO invoice_clients (%s) VALUES (%s)"
        % (", ".join(_FIELDS), ", ".join("?" for _ in _FIELDS)),  # nosec B608 -- същото: имената идват от константата _FIELDS
        [values[f] for f in _FIELDS],
    )
    con.commit()
    return cur.lastrowid


def delete(con, entry_id):
    con.execute("DELETE FROM invoice_clients WHERE id = ?", (entry_id,))
    con.commit()
