# -*- coding: utf-8 -*-
"""Регресионни тестове за заявка на потребителя (12.08.2026, извън одита):

- Палетна карта: „Палет №“ и „Вид опаковка“ по-тесни, „Тип палет“ и
  „Височина“ обединени в едно поле „Размери“ (Д×Ш×В), „Бруто тегло“ след
  „Размери“, всичко на ЕДИН ред (pallet_form.html + pallet_print.html и
  „сестрите“ му — pallet_bulk_print/preview/result).
- Никой печатен документ вече няма баркод в долния край на бланката —
  баркод/QR остават само в горния край, където си бяха (ЧМР, товарителница,
  палетна карта, групов печат на палетни карти).
- Товарителницата излиза ДВА пъти на ЕДИН лист А4 (вместо едно копие с
  голямо празно място под него), разделени с линия за рязане.

Проверено ръчно и с Playwright (виж ПЛАН_ЗА_РАЗРАБОТКА.md): реалистично
попълнена товарителница (3 реда стоки, всички незадължителни полета) отнема
~242мм от наличните 281мм на един физически лист А4 — двете копия се
събират комфортно на един лист (PDF от 1 страница, потвърдено с pypdf)."""
from conftest import post_with_csrf


def _issue(admin_client, path, extra_fields=None):
    data = {"sender_name": "Тест"}
    if extra_fields:
        data.update(extra_fields)
    resp = post_with_csrf(admin_client, path, data, csrf_source_url=path,
                          follow_redirects=False)
    return admin_client.get(resp.headers["Location"])


# ---------------------------------------------------------------- палетна карта: форма

def test_pallet_form_has_one_row_with_pallet_no_and_packaging_narrow_and_merged_dims(admin_client):
    resp = admin_client.get("/pallet/new")
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    # Общият ред-контейнер съществува и групира всичките 5 полета.
    assert 'class="grid-pallet-row"' in body
    # „Размери“ обединява ДВЕТЕ полета (pallet_type + height) в общ блок.
    assert 'class="pallet-dims"' in body
    assert 'name="pallet_type"' in body
    assert 'name="height"' in body
    # Редът на полетата в HTML-а: Палет № → Вид опаковка → Размери → Бруто → Общ брой.
    row_start = body.index('class="grid-pallet-row"')
    row_html = body[row_start:body.index("</fieldset>", row_start)]
    idx_no = row_html.index('name="pallet_no"')
    idx_pack = row_html.index('name="packaging_type"')
    idx_dims = row_html.index('class="pallet-dims"')
    idx_gross = row_html.index('name="gross"')
    idx_total = row_html.index("Общ брой")
    assert idx_no < idx_pack < idx_dims < idx_gross < idx_total
    # Старите отделни надписи „Тип палет“/„Височина, см“ като САМОСТОЯТЕЛНИ
    # полета вече не съществуват — заменени от общия етикет „Размери“.
    assert "Размери (Д×Ш×В)" in body


def test_pallet_card_template_for_extra_cards_mirrors_the_same_row_layout(admin_client):
    """„+ Добави следваща палетна карта“ клонира <template
    id="pallet-card-template"> — трябва да НЕ се разминава с първата карта
    (иначе втората+ картите изглеждат различно/счупено)."""
    resp = admin_client.get("/pallet/new")
    body = resp.data.decode("utf-8")
    template_start = body.index('id="pallet-card-template"')
    template_html = body[template_start:body.index("</template>", template_start)]
    assert 'class="grid-pallet-row"' in template_html
    assert 'class="pallet-dims"' in template_html
    assert 'data-field="pallet_type"' in template_html
    assert 'data-field="height"' in template_html


# ---------------------------------------------------------------- палетна карта: печат

def test_pallet_print_merges_type_and_height_into_one_dimensions_box(admin_client):
    resp = _issue(admin_client, "/pallet/new", {
        "pallet_no": "1 от 1", "pallet_type": "120×80", "height": "15", "gross": "350",
    })
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "РАЗМЕРИ, СМ / DIMENSIONS" in body
    assert "120×80 × 15" in body
    # Старите отделни надписи вече не излизат на бланката.
    assert "ТИП ПАЛЕТ / PALLET TYPE" not in body
    assert "ВИСОЧИНА, СМ / HEIGHT" not in body


def test_pallet_print_shows_no_and_packaging_and_dims_and_gross_on_one_row_no_total_qty(admin_client):
    """Второ уточнение на заявката (12.08.2026): Палет №, Вид опаковка,
    Размери и Бруто — всичките на ЕДИН ред (един .plt-stats с 4 кутии,
    .plt-grid вече не се ползва); „Общ брой“ отпада изцяло от печатната
    бланка (остава само изчислим на екрана, във формата, и в списъка
    pallet_bulk_result.html)."""
    resp = _issue(admin_client, "/pallet/new", {
        "pallet_no": "1 от 1", "pallet_type": "120×80", "height": "15", "gross": "350",
    })
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "plt-grid" not in body
    assert body.count('class="plt-stats"') == 1
    stats_start = body.index('class="plt-stats"')
    stats_html = body[stats_start:stats_start + 1500]
    assert stats_html.count('class="pbox"') == 4
    idx_no = stats_html.index("ПАЛЕТ № / PALLET No")
    idx_pack = stats_html.index("ВИД ОПАКОВКА / PACKAGING")
    idx_dims = stats_html.index("РАЗМЕРИ, СМ / DIMENSIONS")
    idx_gross = stats_html.index("БРУТО, КГ / GROSS")
    assert idx_no < idx_pack < idx_dims < idx_gross
    assert "ОБЩ БРОЙ / TOTAL QTY" not in body


def test_pallet_print_has_no_barcode_at_the_bottom_of_the_card(admin_client):
    resp = _issue(admin_client, "/pallet/new", {"pallet_no": "1 от 1"})
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "plt-big-barcode" not in body
    # Баркодът (SVG с aria-label = стойността на баркода) излиза ТОЧНО
    # веднъж — само в горния край на картата.
    doc_barcode = _extract_barcode(body)
    assert _count_barcode_svgs(body, doc_barcode) == 1


def _extract_barcode(html):
    # Точно баркод-SVG-то (не който да е aria-label на страницата, напр.
    # полето за сканиране в страничната лента) — code128_svg винаги слага
    # role="img" точно преди aria-label.
    marker = 'role="img" aria-label="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]


def _count_barcode_svgs(html, barcode_value):
    return html.count('role="img" aria-label="%s"' % barcode_value)


# ---------------------------------------------------------------- ЧМР: печат

def test_cmr_print_has_no_barcode_at_the_bottom_of_the_document(admin_client):
    resp = _issue(admin_client, "/cmr/new", {"consignee_name": "Получател АД"})
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "cmr-footer" in body  # футърът остава (само текстът, без картинката)
    doc_barcode = _extract_barcode(body)
    assert _count_barcode_svgs(body, doc_barcode) == 1


# ---------------------------------------------------------------- товарителница: 2 копия на лист + без долен баркод

def test_waybill_print_shows_two_copies_on_one_a4_page(admin_client):
    resp = _issue(admin_client, "/waybill/new", {"consignee_name": "Получател АД"})
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    # Едно .print-page (един физически лист), с класа за компактния 2-up изглед.
    assert body.count('class="print-page twb-2up"') == 1
    # Съдържанието на бланката (.twb) се появява ДВА пъти вътре в него.
    assert body.count('<div class="twb">') == 2
    # Разделителна линия за рязане между двете копия.
    assert 'class="twb-cut"' in body


def test_waybill_print_has_barcode_only_at_the_top_of_each_copy_not_the_bottom(admin_client):
    resp = _issue(admin_client, "/waybill/new", {"consignee_name": "Получател АД"})
    body = resp.data.decode("utf-8")
    doc_barcode = _extract_barcode(body)
    # Точно 2 баркод-изображения общо — по едно на всяко от двете горни
    # заглавия (.twb-head-no), нито едно в долния край (.twb-footer).
    assert _count_barcode_svgs(body, doc_barcode) == 2
    for footer_start in _all_indexes(body, 'class="twb-footer"'):
        footer_html = body[footer_start:body.index("</div>", footer_start)]
        assert "role=\"img\"" not in footer_html


def _all_indexes(haystack, needle):
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            return
        yield i
        start = i + 1


# ---------------------------------------------------------------- одит 12.08.2026, "оправи всичките": находки №11/23/24/25

def test_pallet_bulk_review_uses_merged_dimensions_field(admin_client):
    """Одит (находка №11): pallet_bulk_review.html не беше обновена към
    обединеното поле „Размери“ от v3.60.0 — все още показваше „Тип
    палет“/„Височина“ като два отделни реда, визуално разминаване спрямо
    pallet_form.html/печатните бланки."""
    from openpyxl import Workbook
    import io as _io

    wb = Workbook()
    ws = wb.active
    ws.append(["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", ""])
    ws.append(["ORD-1", "10", "REF-1", "Ред едно", 5, "1"])
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = post_with_csrf(
        admin_client, "/pallet/bulk-import",
        {"excel_file": (buf, "poръчки.xlsx")},
        csrf_source_url="/pallet/new",
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Размери (Д×Ш×В), см" in body
    assert 'class="pallet-dims"' in body
    # Старите отделни надписи като самостоятелни полета вече не излизат.
    assert "Височина, см</label><input" not in body


def test_pallet_print_and_bulk_print_have_dash_fallback_for_empty_pallet_type(admin_client):
    """Одит (находка №23): pallet_print.html/pallet_bulk_print.html нямаха
    `or '—'` fallback за празно d.pallet_type (докато preview вариантът го
    имаше) — водеше до увиснало „× 15“ без нищо преди тирето."""
    resp = _issue(admin_client, "/pallet/new", {
        "pallet_no": "1 от 1", "height": "15",  # pallet_type НАРОЧНО празен
    })
    body = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "—  × 15" not in body  # старият счупен изглед
    assert "— × 15" in body or "—</div>" in body  # тирето присъства някъде в стойността


def test_pbox_val_has_overflow_wrap_for_long_combined_dimensions():
    """Одит (находка №24): .pbox .val нямаше overflow-wrap/word-break —
    по-тясната 4-колонна .plt-stats подредба (v3.61.0) може да събере
    дълга комбинирана стойност „Размери“ по-трудно от преди."""
    css = open("static/style.css", encoding="utf-8").read()
    idx = css.index(".pbox .val {")
    rule = css[idx:css.index("}", idx)]
    assert "overflow-wrap" in rule
    assert "word-break" in rule


def test_height_input_has_accessible_label_in_pallet_form():
    """Одит (находка №25): полето за височина изгуби собствения си
    <label for="f-height"> след обединяването в „Размери“ — остана само
    title атрибут (недостатъчно за screen readers)."""
    html = open("templates/pallet_form.html", encoding="utf-8").read()
    assert html.count('name="height"') >= 1
    # И живата карта, и клонираният template имат aria-label.
    assert html.count('data-field="height"') >= 2
    for idx in _all_indexes(html, 'data-field="height"'):
        tag_end = html.index(">", idx)
        tag = html[max(0, idx - 200):tag_end]
        assert "aria-label=" in tag


def test_twb_has_page_break_inside_avoid():
    """Одит (находка №27): предпазна мрежа за необичайно дълга
    товарителница при 2-копия-на-лист изгледа."""
    css = open("static/style.css", encoding="utf-8").read()
    idx = css.index(".twb {")
    rule = css[idx:css.index("}", idx)]
    assert "page-break-inside: avoid" in rule
