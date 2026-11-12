#!/usr/bin/env python3
"""
Generate a realistic set of test PDFs for the PDF Printer Optimizer.

    python tools/make_test_pdfs.py            # writes into ./samples
    python tools/make_test_pdfs.py --out input

Covers: digital text with grey / faint / tiny text, hairline tables, white
text on dark bars, watermarks, degraded scans (faded ink, yellow paper,
uneven lighting, noise, bleed-through), photographs, QR codes, barcodes,
blue signatures, red stamps, OCR text layers, rotated and landscape pages,
CMYK content, multi-page files, an encrypted file and corrupt files.
"""
from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

import cv2
import numpy as np

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

A4 = fitz.paper_rect("a4")
LOREM = ("The applicant has completed the prescribed course of study and passed the "
         "examination held in March 2024. Marks obtained in each subject are listed below. "
         "This document is issued under the authority of the Board and is valid for all "
         "official purposes. Address: 42, Gandhi Nagar, Sector 7, New Delhi - 110001.")


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def synthetic_photo(w: int, h: int, seed: int = 1) -> np.ndarray:
    """Continuous-tone 'portrait' (RGB uint8)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    bg = 120 + 60 * np.sin(xx / w * 3.0) * np.cos(yy / h * 2.0)
    noise = cv2.GaussianBlur(rng.normal(0, 40, (h, w)).astype(np.float32), (0, 0), w / 25)
    img = np.dstack([bg + noise * 0.6 + 20, bg + noise * 0.5, bg + noise * 0.4 - 10])
    cx, cy = w / 2, h * 0.42
    face = ((xx - cx) / (w * 0.22)) ** 2 + ((yy - cy) / (h * 0.25)) ** 2
    shade = np.clip(1.0 - face, 0, 1)[..., None]
    skin = np.array([205, 160, 130], np.float32) - 50 * (xx[..., None] - cx) / w
    img = img * (1 - np.clip(shade * 3, 0, 1)) + skin * np.clip(shade * 3, 0, 1)
    body = (yy > h * 0.72) & (np.abs(xx - cx) < w * (0.2 + (yy / h - 0.72) * 1.2))
    img[body] = img[body] * 0.3 + np.array([40, 50, 90]) * 0.7
    for ex in (-0.08, 0.08):
        cv2.circle(img, (int(cx + ex * w), int(cy - 0.03 * h)), max(2, w // 40), (40, 30, 30), -1)
    img = cv2.GaussianBlur(img, (0, 0), 1.2)
    return np.clip(img, 0, 255).astype(np.uint8)


def qr_image(text: str, module_px: int) -> np.ndarray:
    enc = cv2.QRCodeEncoder.create()
    q = enc.encode(text)
    q = cv2.resize(q, None, fx=module_px, fy=module_px, interpolation=cv2.INTER_NEAREST)
    return cv2.copyMakeBorder(q, 4 * module_px, 4 * module_px, 4 * module_px, 4 * module_px,
                              cv2.BORDER_CONSTANT, value=255)


def barcode_image(w: int, h: int, seed: int = 3) -> np.ndarray:
    rng = random.Random(seed)
    img = np.full((h, w), 255, np.uint8)
    x = 10
    while x < w - 14:
        bw = rng.choice((2, 2, 3, 4, 6))
        img[6:h - 6, x:x + bw] = 0
        x += bw + rng.choice((2, 3, 4, 6))
    return img


def draw_signature(img: np.ndarray, x: int, y: int, scale: float, color) -> None:
    pts = []
    for t in np.linspace(0, 1, 260):
        px = x + scale * (t * 260 + 18 * math.sin(t * 22))
        py = y + scale * (24 * math.sin(t * 13) * (1 - t) + 10 * math.cos(t * 31))
        pts.append((int(px), int(py)))
    cv2.polylines(img, [np.array(pts, np.int32)], False, color, max(2, int(2.2 * scale)), cv2.LINE_AA)
    cv2.line(img, (x, y + int(40 * scale)), (x + int(250 * scale), y + int(34 * scale)), color,
             max(1, int(1.6 * scale)), cv2.LINE_AA)


def draw_stamp(img: np.ndarray, cx: int, cy: int, r: int, color) -> None:
    cv2.circle(img, (cx, cy), r, color, max(2, r // 25), cv2.LINE_AA)
    cv2.circle(img, (cx, cy), int(r * 0.78), color, max(1, r // 45), cv2.LINE_AA)
    cv2.putText(img, "VERIFIED", (cx - int(r * 0.62), cy + int(r * 0.12)), cv2.FONT_HERSHEY_DUPLEX,
                r / 110, color, max(1, r // 40), cv2.LINE_AA)
    for k in range(18):
        a = k / 18 * 2 * math.pi
        cv2.circle(img, (int(cx + 0.89 * r * math.cos(a)), int(cy + 0.89 * r * math.sin(a))),
                   max(1, r // 40), color, -1, cv2.LINE_AA)


def add_digital_content(page: "fitz.Page", title: str, seed: int = 0, with_watermark: bool = True,
                        tint: bool = True, images: bool = True) -> None:
    rng = random.Random(seed)
    W, H = page.rect.width, page.rect.height
    if tint:
        page.draw_rect(page.rect, color=None, fill=(0.975, 0.975, 0.97), width=0)
    # dark header bar with white and light-grey text
    page.draw_rect(fitz.Rect(0, 0, W, 70), color=None, fill=(0.12, 0.18, 0.32))
    page.insert_text((40, 38), title, fontsize=20, fontname="hebo", color=(1, 1, 1))
    page.insert_text((40, 58), "Government of India  |  Reference No. GOI/2024/00731", fontsize=9,
                     color=(0.78, 0.8, 0.85))
    y = 100
    page.insert_text((40, y), "Personal Details", fontsize=13, fontname="hebo", color=(0.2, 0.2, 0.2))
    y += 18
    rows = [("Name", "Aarav Kumar Sharma", (0, 0, 0)),
            ("Date of Birth", "14/08/2006", (0.45, 0.45, 0.45)),
            ("Father's Name", "Rajesh Kumar Sharma", (0.62, 0.62, 0.62)),
            ("Address", "42, Gandhi Nagar, New Delhi - 110001", (0.75, 0.75, 0.75)),
            ("Roll Number", "2024-DL-0098123", (0.15, 0.35, 0.8))]
    for label, val, col in rows:
        page.insert_text((40, y), f"{label}:", fontsize=10, color=(0.5, 0.5, 0.5))
        page.insert_text((150, y), val, fontsize=10, color=col)
        y += 15
    y += 8
    rect = fitz.Rect(40, y, W - 40, y + 70)
    page.insert_textbox(rect, LOREM, fontsize=8.5, color=(0.55, 0.55, 0.55), fontname="tiro")
    y += 78
    page.insert_textbox(fitz.Rect(40, y, W - 40, y + 30),
                        "Tiny print: Errors and omissions excepted. Verify at www.example.gov.in "
                        "using the QR code. Valid only with the seal of the issuing authority.",
                        fontsize=5.5, color=(0.6, 0.6, 0.6))
    y += 34
    # table with hairline borders, light header fill and zebra rows
    cols = [40, 200, 300, 400, W - 40]
    header = ["Subject", "Max Marks", "Obtained", "Grade"]
    data = [["English", "100", "88", "A"], ["Hindi", "100", "79", "B+"],
            ["Mathematics", "100", "95", "A+"], ["Science", "100", "91", "A+"],
            ["Social Science", "100", "84", "A"], ["Total", "500", "437", "A"]]
    rh = 17
    page.draw_rect(fitz.Rect(cols[0], y, cols[-1], y + rh), color=None, fill=(0.9, 0.9, 0.9))
    for r_i in range(len(data)):
        if r_i % 2 == 1:
            yy = y + rh * (r_i + 1)
            page.draw_rect(fitz.Rect(cols[0], yy, cols[-1], yy + rh), color=None, fill=(0.965, 0.965, 0.965))
    for r_i in range(len(data) + 2):
        page.draw_line((cols[0], y + r_i * rh), (cols[-1], y + r_i * rh), color=(0.7, 0.7, 0.7), width=0.25)
    for cx in cols:
        page.draw_line((cx, y), (cx, y + rh * (len(data) + 1)), color=(0.72, 0.72, 0.72), width=0)
    for c_i, txt in enumerate(header):
        page.insert_text((cols[c_i] + 5, y + 12), txt, fontsize=9.5, fontname="hebo", color=(0.3, 0.3, 0.3))
    for r_i, row in enumerate(data):
        for c_i, txt in enumerate(row):
            page.insert_text((cols[c_i] + 5, y + rh * (r_i + 1) + 12), txt, fontsize=9,
                             color=(0.5, 0.5, 0.5) if r_i % 2 else (0.25, 0.25, 0.25))
    y += rh * (len(data) + 1) + 20
    # separator hairline and light text
    page.draw_line((40, y), (W - 40, y), color=(0.8, 0.8, 0.8), width=0.1)
    y += 16
    page.insert_text((40, y), "Remarks: Passed with Distinction", fontsize=10, color=(0.85, 0.3, 0.3))
    y += 14
    page.insert_text((40, y), "Note: this line is very faint grey (0.85) and hard to print.", fontsize=9,
                     color=(0.85, 0.85, 0.85))
    if images:
        photo = synthetic_photo(300, 380, seed + 1)
        page.insert_image(fitz.Rect(W - 150, 85, W - 40, 225), stream=_jpeg(photo, 85))
        q = qr_image(f"https://verify.example.gov.in/{seed:05d}", 6)
        page.insert_image(fitz.Rect(W - 140, H - 160, W - 50, H - 70), stream=_png(q))
    # footer: light-grey text on white and a light signature line
    page.draw_line((40, H - 90), (220, H - 90), color=(0.7, 0.7, 0.7), width=0.3)
    page.insert_text((40, H - 78), "Signature of Controller of Examinations", fontsize=8, color=(0.6, 0.6, 0.6))
    page.insert_text((40, H - 40), f"Page generated for testing - seed {seed} - {rng.randint(1000, 9999)}",
                     fontsize=7, color=(0.7, 0.7, 0.7))
    if with_watermark:
        page.insert_text((150, 560), "CONFIDENTIAL", fontsize=64, color=(0.85, 0.85, 0.85),
                         fill_opacity=0.35, morph=(fitz.Point(150, 560), fitz.Matrix(35)))


def _jpeg(arr: np.ndarray, q: int) -> bytes:
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()


def _png(arr: np.ndarray) -> bytes:
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return cv2.imencode(".png", arr)[1].tobytes()


def render_rgb(page: "fitz.Page", dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, annots=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, 3).copy()


def degrade_scan(img: np.ndarray, dpi: int, seed: int, fade: float = 0.45, extras: bool = True,
                 back: np.ndarray = None) -> np.ndarray:
    """Turn a clean RGB rendering into a realistic poor scan."""
    rng = np.random.default_rng(seed)
    h, w = img.shape[:2]
    f = img.astype(np.float32)
    if extras:
        s = dpi / 300
        draw_stamp(img, int(w * 0.72), int(h * 0.78), int(110 * s), (200, 40, 50))
        draw_signature(img, int(w * 0.1), int(h * 0.83), 1.1 * s, (30, 50, 170))
        bc = barcode_image(int(2.2 * dpi), int(0.45 * dpi), seed)
        bx, by = int(w * 0.08), int(h * 0.9)
        img[by:by + bc.shape[0], bx:bx + bc.shape[1]] = np.minimum(
            img[by:by + bc.shape[0], bx:bx + bc.shape[1]], bc[..., None])
        f = img.astype(np.float32)
    # faded ink: compress darkness
    f = 255 - (255 - f) * fade
    # bleed-through from the back side (mirrored, blurred, faint)
    if back is not None:
        bt = cv2.GaussianBlur(cv2.flip(back, 1).astype(np.float32), (0, 0), 2.0 * dpi / 300)
        f = f - (255 - bt) * 0.10
    # yellowish paper and uneven illumination
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    light = 1.0 - 0.18 * ((xx / w - 0.2) ** 2 + (yy / h - 0.3) ** 2)
    f = f * light[..., None] * np.array([0.98, 0.95, 0.85], np.float32)
    f = cv2.GaussianBlur(f, (0, 0), 0.7 * dpi / 300)
    f += rng.normal(0, 5.0, f.shape).astype(np.float32)
    return np.clip(f, 0, 255).astype(np.uint8)


def scanned_page(doc: "fitz.Document", img: np.ndarray, rect: "fitz.Rect", ocr_text: str = "",
                 q: int = 70) -> "fitz.Page":
    page = doc.new_page(width=rect.width, height=rect.height)
    page.insert_image(page.rect, stream=_jpeg(img, q))
    if ocr_text:
        tw = fitz.TextWriter(page.rect)
        tw.fill_textbox(fitz.Rect(40, 100, rect.width - 40, rect.height - 100), ocr_text, fontsize=10)
        tw.write_text(page, render_mode=3)
    return page


def make_scan_from_digital(title: str, seed: int, dpi: int = 300, fade: float = 0.45,
                           landscape: bool = False, ocr: bool = False) -> "fitz.Document":
    src = fitz.open()
    rect = fitz.paper_rect("a4-l") if landscape else A4
    p = src.new_page(width=rect.width, height=rect.height)
    add_digital_content(p, title, seed, with_watermark=False, tint=False)
    img = render_rgb(p, dpi)
    back_doc = fitz.open()
    bp = back_doc.new_page(width=rect.width, height=rect.height)
    add_digital_content(bp, "BACK SIDE TEXT", seed + 7, with_watermark=False, tint=False, images=False)
    back = render_rgb(bp, dpi)
    img = degrade_scan(img, dpi, seed, fade=fade, back=back)
    out = fitz.open()
    scanned_page(out, img, rect, ocr_text=(title + "\n" + LOREM) if ocr else "")
    return out


# ---------------------------------------------------------------------------
# Test files
# ---------------------------------------------------------------------------

def build(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []

    def save(doc: "fitz.Document", rel: str, **kw) -> None:
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(p), garbage=3, deflate=True, **kw)
        files.append(rel)

    # 1. Digital documents
    for name, seed in (("Resume.pdf", 1), ("Class 10th.pdf", 2), ("Offer Letter.pdf", 3)):
        d = fitz.open()
        add_digital_content(d.new_page(), name[:-4].upper(), seed)
        save(d, name)

    # 2. Scanned documents (faded, noisy, bleed-through, stamp, signature, barcode, photo, QR)
    save(make_scan_from_digital("AADHAAR CARD", 11, fade=0.40), "Aadhaar Card.pdf")
    save(make_scan_from_digital("CERTIFICATE OF MERIT", 12, fade=0.35, ocr=True), "Certificate.pdf")
    save(make_scan_from_digital("MIGRATION CERTIFICATE", 13, dpi=200, fade=0.30), "Migration Certificate (200dpi).pdf")
    save(make_scan_from_digital("TRANSFER CERTIFICATE", 14, landscape=True, fade=0.5), "Landscape Scan.pdf")
    save(make_scan_from_digital("VERY FADED RECEIPT", 15, fade=0.18), "Very Faded Receipt.pdf")

    # 3. Rotated pages (digital + scanned)
    d = fitz.open()
    p = d.new_page()
    add_digital_content(p, "ROTATED DIGITAL", 21)
    p.set_rotation(90)
    s = make_scan_from_digital("ROTATED SCAN", 22, ocr=True)
    s[0].set_rotation(270)
    d.insert_pdf(s)
    save(d, "Rotated Pages.pdf")

    # 4. Mixed page: digital text + a scanned certificate image + photo
    d = fitz.open()
    p = d.new_page()
    p.insert_text((40, 50), "Attached: scanned copy of the original mark sheet", fontsize=12,
                  color=(0.4, 0.4, 0.4))
    scan = make_scan_from_digital("MARK SHEET (ATTACHED SCAN)", 31, dpi=200, fade=0.4)
    img_pix = scan[0].get_pixmap(dpi=200)
    p.insert_image(fitz.Rect(40, 70, 555, 760), pixmap=img_pix)
    p.insert_text((40, 800), "Verified true copy - Notary Public, Delhi", fontsize=10, color=(0.55, 0.55, 0.55))
    save(d, "Mixed Attachment.pdf")

    # 5. Multi-page document mixing everything
    d = fitz.open()
    for k in range(4):
        add_digital_content(d.new_page(), f"ANNUAL REPORT - PAGE {k + 1}", 40 + k, with_watermark=(k % 2 == 0))
    for k in range(3):
        d.insert_pdf(make_scan_from_digital(f"ANNEXURE {k + 1}", 50 + k, dpi=300, fade=0.35 + 0.1 * k,
                                            ocr=(k == 1)))
    save(d, "Annual Report (7 pages).pdf")

    # 6. Form XObjects (content placed via show_pdf_page, 2-up)
    inner = fitz.open()
    add_digital_content(inner.new_page(), "FORM XOBJECT SOURCE", 60)
    d = fitz.open()
    p = d.new_page()
    p.show_pdf_page(fitz.Rect(20, 20, 300, 420), inner, 0)
    p.show_pdf_page(fitz.Rect(300, 420, 580, 820), inner, 0)
    save(d, "Two-Up Layout.pdf")

    # 7. CMYK and RGB operators written directly into the content stream
    d = fitz.open()
    p = d.new_page()
    p.insert_text((50, 50), "CMYK test page", fontsize=14, fontname="helv")
    cs = (b"q BT /helv 12 Tf 0 0 0 0.45 k 50 700 Td (CMYK 45% black text - should get darker) Tj ET "
          b"BT /helv 12 Tf 0 0 0 0.20 k 50 680 Td (CMYK 20% black faint text) Tj ET "
          b"BT /helv 12 Tf 0.6 0.2 0 0.1 k 50 660 Td (CMYK light blue text) Tj ET "
          b"BT /helv 12 Tf /DeviceRGB cs 0.6 0.6 0.6 sc 50 640 Td (RGB via cs/sc grey text) Tj ET "
          b"0 0 0 0.3 K 0.1 w 50 620 m 500 620 l S "
          b"0.05 0.03 0 0 k 50 400 300 150 re f "
          b"BT /helv 11 Tf 0 0 0 0.5 k 60 470 Td (Grey text on a very light CMYK tint) Tj ET Q")
    x = d.get_new_xref()
    d.update_object(x, "<<>>")
    d.update_stream(x, cs)
    conts = p.get_contents()
    d.xref_set_key(p.xref, "Contents", "[" + " ".join(f"{c} 0 R" for c in conts) + f" {x} 0 R]")
    save(d, "CMYK Colours.pdf")

    # 8. Nested folders
    d = fitz.open()
    add_digital_content(d.new_page(), "SEMESTER MARKSHEET", 70)
    save(d, "Certificates/2024/Marksheet.pdf")
    save(make_scan_from_digital("PAN CARD", 71, fade=0.4), "Certificates/PAN Card.pdf")

    # 9. Extra files to exceed 20 documents
    for k in range(6):
        if k % 2:
            save(make_scan_from_digital(f"BILL {k + 1}", 80 + k, dpi=300, fade=0.3 + 0.05 * k), f"Bills/Bill {k + 1}.pdf")
        else:
            dd = fitz.open()
            add_digital_content(dd.new_page(), f"INVOICE {k + 1}", 80 + k, with_watermark=False)
            save(dd, f"Bills/Invoice {k + 1}.pdf")

    # 10. Error cases
    d = fitz.open()
    add_digital_content(d.new_page(), "SECRET", 90)
    save(d, "Password Protected.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret",
         owner_pw="owner")
    (out_dir / "Corrupt File.pdf").write_bytes(b"This is not really a PDF file\n" * 20)
    files.append("Corrupt File.pdf")
    good = (out_dir / "Resume.pdf").read_bytes()
    (out_dir / "Truncated Download.pdf").write_bytes(good[: len(good) // 3])
    files.append("Truncated Download.pdf")
    (out_dir / "._Resume.pdf").write_bytes(b"\x00\x05\x16\x07 AppleDouble junk")  # must be ignored

    print(f"Created {len(files)} test PDFs in {out_dir}")
    for f in files:
        print("  ", f)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "samples"))
    build(Path(ap.parse_args().out))
