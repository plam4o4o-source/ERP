# -*- coding: utf-8 -*-
"""QR код за публичен преглед на документ БЕЗ вход, чрез сканиране с
телефон на физическата бланка — заявка: „всеки, който сканира с телефон
баркода на някой от документите, да му се зареди директно документа, без
да има нужда от домейна, който е в програмата“ (+ уточнение: „само
документа, нищо друго да не вижда“).

За разлика от Code128 баркода (barcode128.py) — той е за ВЪТРЕШНО
повторно въвеждане в самата програма (USB лазерен скенер/global-scan-form,
routes_dashboard.scan) и остава непроменен — QR кодът тук носи ПЪЛЕН
интернет адрес (текущия домейн, от който в момента се преглежда/печата
документът — локален/LAN/Cloudflare тунел, каквото и да е, виж
routes_documents.public_document_view/view_document), не просто вътрешен
код. Телефонните камери разпознават QR далеч по-надеждно от Code128 за
толкова дълъг текст (URL адрес).

Винаги PNG data URI (не SVG) — за разлика от barcode128 (нужен е и SVG за
браузъра, и PNG за PDF износа, виж pdf_export.py), тук няма нужда от SVG
изобщо: PNG работи еднакво добре и на екран, и при печат — по-малко код
за поддръжка."""
import base64
import io

import qrcode


def qr_png_data_uri(text, box_size=5, border=2):
    """PNG data URI на QR код, кодиращ `text` (пълен URL) — вграждан
    директно в <img src="..."> в печатните шаблони (виж
    templates/_macros.html, макро doc_qr)."""
    img = qrcode.make(text, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64,%s" % base64.b64encode(buf.getvalue()).decode("ascii")
