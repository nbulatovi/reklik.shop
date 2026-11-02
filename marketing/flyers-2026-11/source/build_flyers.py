#!/usr/bin/env python3
"""Compose ReKlik A6 flyers from unchanged original raster artwork.

Native PDF text, rectangles and QR modules; no raster retouching.
Install: pip install -r requirements.txt
Run: python source/build_flyers.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import pymupdf as fitz
import numpy as np
import qrcode
from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
MM = 72 / 25.4
TRIM_W, TRIM_H = 105.0, 148.0
CREAM = "#FFF8F1"
ORANGE = "#FF681D"
INK = "#202321"
URLS = {
    "a": "https://reklik.gcchelp.com/r/a6-a",
    "b": "https://reklik.gcchelp.com/r/a6-b",
}


def register_fonts() -> None:
    for name, filename in (
        ("Ubuntu", "Ubuntu-R.ttf"),
        ("UbuntuMedium", "Ubuntu-M.ttf"),
        ("UbuntuBold", "Ubuntu-B.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(ROOT / "source/fonts" / filename)))


def compose(variant: str, bleed: float, art: Path, output: Path) -> dict:
    """All layout coordinates below use mm from the trimmed top left."""
    page_w, page_h = TRIM_W + 2 * bleed, TRIM_H + 2 * bleed
    c = canvas.Canvas(
        str(output),
        pagesize=(page_w * MM, page_h * MM),
        pageCompression=1,
        invariant=1,
    )
    c.setTitle(f"ReKlik.shop — A6 {variant.upper()}")
    c.setAuthor("ReKlik.shop")
    c.setSubject("Laptopovi i telefoni — A6 letak")
    trim = (bleed * MM, bleed * MM, (bleed + TRIM_W) * MM, (bleed + TRIM_H) * MM)
    c.setTrimBox(trim)
    c.setBleedBox((0, 0, page_w * MM, page_h * MM))
    c.setCropBox((0, 0, page_w * MM, page_h * MM))

    def rect(x, y, w, h, color):
        c.setFillColor(HexColor(color))
        c.rect((x + bleed) * MM, (TRIM_H + bleed - y - h) * MM, w * MM, h * MM, stroke=0, fill=1)

    def text(x, y, s, size, font="Ubuntu", color=INK, align="left"):
        c.setFillColor(HexColor(color))
        c.setFont(font, size)
        x_pt, y_pt = (x + bleed) * MM, (TRIM_H + bleed - y) * MM
        if align == "right":
            c.drawRightString(x_pt, y_pt, s)
        else:
            c.drawString(x_pt, y_pt, s)

    # Bleed contains only continuous background and footer color.
    rect(-bleed, -bleed, page_w, page_h, CREAM)
    rect(6.5, 16.0, 92.0, 0.35, INK)
    text(6.5, 12.5, "ReKlik.shop", 15.7, "UbuntuBold")
    text(98.5, 11.7, "LAPTOPOVI I TELEFONI", 7.8, "UbuntuMedium", align="right")

    if variant == "a":
        text(6.5, 29.0, "Skoro novo.", 31.0, "UbuntuBold")
        text(6.5, 41.0, "Skroz pametno.", 31.0, "UbuntuBold", ORANGE)
        text(6.5, 49.2, "Zašto novo, kad ima i skoro novo?", 10.7, "UbuntuMedium")
        image_box = (7.0, 53.5, 91.0, 52.5)
    else:
        text(6.5, 27.0, "Zašto novo,", 29.0, "UbuntuBold")
        text(6.5, 38.2, "kad ima i", 29.0, "UbuntuBold")
        text(6.5, 49.4, "skoro novo?", 29.0, "UbuntuBold", ORANGE)
        text(6.5, 56.3, "Testirani uređaji. Pametan izbor.", 10.4, "UbuntuMedium")
        image_box = (7.0, 58.7, 91.0, 47.5)

    # Place the original file directly. Preserve its aspect ratio; no raster
    # cropping, recoloring, compositing, resampling or post-processing.
    with Image.open(art) as original:
        iw, ih = original.size
    bx, by, bw, bh = image_box
    scale = min(bw / iw, bh / ih)
    dw, dh = iw * scale, ih * scale
    dx, dy = bx + (bw - dw) / 2, by + (bh - dh) / 2
    c.drawImage(
        ImageReader(str(art)),
        (dx + bleed) * MM,
        (TRIM_H + bleed - dy - dh) * MM,
        width=dw * MM,
        height=dh * MM,
        preserveAspectRatio=True,
        mask="auto",
    )

    # Reassurance and contact details are real; there is no fabricated price,
    # warranty, discount or claim that all products share one condition grade.
    rect(-bleed, 110.0, page_w, 38.0 + bleed, ORANGE)
    text(6.5, 118.2, "Pogledaj ponudu", 8.6, "UbuntuMedium")
    text(6.5, 127.3, "ReKlik.shop", 21.0, "UbuntuBold")
    text(6.5, 135.0, "+381 63 697 007", 12.6, "UbuntuBold")
    text(6.5, 142.0, "Pouzećem bez doplate", 8.3, "UbuntuMedium")

    # 32 mm includes a four-module white quiet zone on every side.
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    qr.add_data(URLS[variant])
    qr.make(fit=True)
    matrix = qr.get_matrix()
    modules = len(matrix)
    qx, qy, qs = 67.0, 112.0, 32.0
    rect(qx, qy, qs, qs, "#FFFFFF")
    module_mm = qs / modules
    # One PDF path for all modules, no seams from separate antialiased strokes.
    c.setFillColor(HexColor("#000000"))
    path = c.beginPath()
    for row, values in enumerate(matrix):
        for col, is_dark in enumerate(values):
            if is_dark:
                path.rect(
                    (qx + bleed + col * module_mm) * MM,
                    (TRIM_H + bleed - qy - (row + 1) * module_mm) * MM,
                    module_mm * MM,
                    module_mm * MM,
                )
    c.drawPath(path, stroke=0, fill=1)
    c.linkURL(URLS[variant], ((qx + bleed) * MM, (TRIM_H + bleed - qy - qs) * MM, (qx + qs + bleed) * MM, (TRIM_H + bleed - qy) * MM), relative=0, thickness=0)
    c.linkURL("https://ReKlik.shop", ((6.5 + bleed) * MM, (TRIM_H + bleed - 129) * MM, (57.5 + bleed) * MM, (TRIM_H + bleed - 120) * MM), relative=0, thickness=0)
    c.showPage()
    c.save()

    return {
        "image_width_mm": round(dw, 3),
        "image_height_mm": round(dh, 3),
        "image_effective_dpi": round(iw / (dw / 25.4), 1),
        "qr_size_mm_including_quiet_zone": qs,
        "qr_modules_including_quiet_zone": modules,
        "qr_module_size_mm": round(module_mm, 4),
        "qr_quiet_zone_modules_each_side": 4,
    }


def verify_and_render(pdf: Path, variant: str, bleed: float, preview: bool) -> dict:
    doc = fitz.open(pdf)
    assert len(doc) == 1
    page = doc[0]
    expected_media = (TRIM_W + 2 * bleed, TRIM_H + 2 * bleed)
    actual_media = (page.mediabox.width / MM, page.mediabox.height / MM)
    assert all(abs(a - e) < 0.01 for a, e in zip(actual_media, expected_media)), actual_media
    expected_trim = (bleed, bleed, bleed + TRIM_W, bleed + TRIM_H)
    actual_trim = tuple(value / MM for value in page.trimbox)
    assert all(abs(a - e) < 0.01 for a, e in zip(actual_trim, expected_trim)), actual_trim
    txt = page.get_text()
    required = ["ReKlik.shop", "+381 63 697 007", "Pouzećem bez doplate"]
    if variant == "a":
        required += ["Skoro novo.", "Skroz pametno.", "Zašto novo, kad ima i skoro novo?"]
    else:
        required += ["Zašto novo,", "kad ima i", "skoro novo?", "Testirani uređaji. Pametan izbor."]
    for phrase in required:
        assert phrase in txt, (pdf.name, phrase, txt)
    for font in page.get_fonts(full=True):
        if "Ubuntu" in font[3]:
            font_data = doc.extract_font(font[0])
            assert len(font_data[3]) > 1000, ("font not embedded", font)

    # Confirm every native text span stays within the safe area.
    bounds = []
    safe = fitz.Rect((bleed + 5) * MM, (bleed + 5) * MM, (bleed + 100) * MM, (bleed + 145) * MM)
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                box = fitz.Rect(span["bbox"])
                assert safe.contains(box), (pdf.name, span["text"], tuple(box), tuple(safe))
                bounds.append({"text": span["text"], "font_pt": span["size"], "bbox_pt": list(box)})

    pix = page.get_pixmap(dpi=300, alpha=False)
    if preview:
        pix.save(str(pdf.with_name(f"reklik-a6-{variant}-preview-300dpi.png")))
    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
    assert decoded == URLS[variant], (pdf.name, "QR decode mismatch", decoded)
    return {
        "file": pdf.name,
        "media_mm": [round(v, 4) for v in actual_media],
        "trim_box_mm_from_top_left": [round(v, 4) for v in actual_trim],
        "qr_decoded_from_exported_pdf_at_300dpi": decoded,
        "text_and_serbian_diacritics_verified": True,
        "native_text_safe_bounds_verified": True,
        "fonts_embedded": True,
        "text_spans": bounds,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--art", type=Path, default=ROOT / "device-art-original.png")
    args = parser.parse_args()
    art = args.art.resolve()
    assert art.is_file(), f"Original artwork missing: {art}"
    register_fonts()
    before = hashlib.sha256(art.read_bytes()).hexdigest()
    report = {"original_art_sha256": before, "files": []}
    for variant in ("a", "b"):
        for mode, bleed in (("bleed", 3.0), ("clean", 0.0)):
            pdf = ROOT / f"reklik-a6-{variant}-{mode}.pdf"
            composition = compose(variant, bleed, art, pdf)
            verification = verify_and_render(pdf, variant, bleed, preview=mode == "clean")
            report["files"].append(composition | verification)
            print(f"Verified {pdf.name}: A6 trim, embedded fonts, native text bounds, 32 mm QR decoded")
    assert hashlib.sha256(art.read_bytes()).hexdigest() == before
    (ROOT / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
