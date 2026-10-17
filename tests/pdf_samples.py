"""Build small PDFs whose page shows correct Vietnamese text over a chosen text layer.

This is what a PDF with a legacy (TCVN3) font or a broken font encoding looks like to
MarkitdownTool: the page renders correctly, but the extracted text is wrong.
"""

import ctypes
import os
from pathlib import Path

VISIBLE_TEXT = "Ngày hiệu chuẩn: 15/03/2026. Thiết bị đạt yêu cầu kỹ thuật."
TEXT_LAYERS = {
    "good": VISIBLE_TEXT,
    "tcvn3": "Ngµy hiÖu chuÈn: 15/03/2026. ThiÕt bÞ ®¹t yªu cÇu kü thuËt.",
    # A standard font cannot encode Vietnamese letters; extraction then yields U+00FF for each.
    "broken": VISIBLE_TEXT,
}
ARIAL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"


def _add_page(pdf, layer: str, embed_font: bool) -> None:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (2480, 600), "white")
    ImageDraw.Draw(image).text((120, 260), VISIBLE_TEXT, fill="black", font=ImageFont.truetype(str(ARIAL), 64))
    width, height = 595, 144
    page = pdf.new_page(width, height)
    picture = pdfium.PdfImage.new(pdf)
    picture.set_bitmap(pdfium.PdfBitmap.from_pil(image))
    picture.set_matrix(pdfium.PdfMatrix().scale(width, height))
    page.insert_obj(picture)

    if embed_font:
        data = ARIAL.read_bytes()
        font_data = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        font = raw.FPDFText_LoadFont(pdf.raw, font_data, len(data), raw.FPDF_FONT_TRUETYPE, True)
    else:
        font = raw.FPDFText_LoadStandardFont(pdf.raw, b"Helvetica")
    text = raw.FPDFPageObj_CreateTextObj(pdf.raw, font, 10)
    buffer = ctypes.create_string_buffer((layer + "\0").encode("utf-16-le"))
    raw.FPDFText_SetText(text, ctypes.cast(buffer, ctypes.POINTER(raw.FPDF_WCHAR)))
    raw.FPDFTextObj_SetTextRenderMode(text, raw.FPDF_TEXTRENDERMODE_INVISIBLE)
    raw.FPDFPageObj_Transform(text, 1, 0, 0, 1, 20, 60)
    raw.FPDFPage_InsertObject(page.raw, text)
    page.gen_content()
    page.close()


def make_pdf(path: Path, *layers: str) -> Path:
    """One page per layer name from TEXT_LAYERS ("good", "tcvn3", "broken")."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for name in layers:
        _add_page(pdf, TEXT_LAYERS[name], embed_font=name == "good")
    pdf.save(path)
    pdf.close()
    return path
