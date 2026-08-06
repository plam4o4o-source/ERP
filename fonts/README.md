# DejaVu Sans (PDF износ на документи)

`DejaVuSans.ttf` / `DejaVuSans-Bold.ttf` — свободен, разрешаващ лиценз шрифт
(Bitstream Vera License + принос от Arev Fonts, обществено достояние),
пълен текст: https://dejavu-fonts.github.io/License.html

Ползва се САМО от `pdf_export.py` (бутон „Изтегли PDF“ в документите), за да
рисува кирилица правилно през xhtml2pdf/reportlab — вграденият шрифт по
подразбиране (Helvetica) няма кирилски глифи и показва плътни черни
правоъгълници вместо текст. DejaVu Sans поддържа кирилица (и латиница),
затова е избран пред стандартните PDF шрифтове.

При компилиране на .exe (виж .github/workflows/release.yml) тази папка се
включва изрично с `--add-data "fonts;fonts"`, точно както `templates`/
`static`/`translations` — иначе бутонът „Изтегли PDF“ би работил само при
стартиране от изходния код, не и от компилираната програма (виж
pdf_export._font_dir() за логиката frozen/не-frozen, огледална на
config.py._BASE_DIR).
