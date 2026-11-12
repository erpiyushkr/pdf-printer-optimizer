"""
processor.py - Core engine of the PDF Printer Optimizer.

Pipeline per document
---------------------
1. Every page is classified as DIGITAL, SCANNED or MIXED from MuPDF's drawing
   log (image coverage vs. visible text).
2. DIGITAL / MIXED pages keep their vector content and selectable text:
     * embedded raster images are enhanced in place (native resolution),
     * all colours are converted to grayscale (MuPDF recolor),
     * the content streams are rewritten so that grey / faint text and thin
       lines are darkened, hairlines get a printable minimum width and
       near-white page tints are removed, while white-on-dark text,
       watermarks and large fills are left alone.
3. SCANNED pages are rendered at the configured DPI (800 by default) and run
   through the raster enhancement pipeline (illumination correction,
   edge-preserving denoise, unsharp masking, adaptive local-ink tone mapping,
   controlled stroke growth, table-line reinforcement).  Photographs, colour
   ink (signatures, stamps, seals) and QR / bar codes are detected and given
   conservative processing.  Any invisible OCR text layer is kept so the page
   stays searchable.
4. The result is written to a temporary file, verified (opens, page count,
   page sizes, raster resolution, independent pypdf parse) and only then
   moved to its final name.  The input file is never written to.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time
import traceback
import warnings
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

try:  # PyMuPDF >= 1.24 exposes "pymupdf"; older releases only "fitz"
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from PIL import Image

try:
    import pypdf
except ImportError:  # pragma: no cover - verification degrades gracefully
    pypdf = None

fitz.TOOLS.mupdf_display_errors(False)
try:
    fitz.TOOLS.mupdf_display_warnings(False)
except Exception:  # pragma: no cover
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "input_folder": "input",
    "output_folder": "output",
    "target_dpi": 600,
    "output_mode": "grayscale",
    "darkness_level": "low_toner",
    "contrast_enhancement": True,
    "adaptive_contrast": True,
    "background_cleanup": True,
    "denoise": True,
    "sharpening": True,
    "faint_text_enhancement": True,
    "thin_line_enhancement": True,
    "preserve_photographs": True,
    "preserve_signatures": True,
    "preserve_qr_codes": True,
    "preserve_barcodes": True,
    "preserve_stamps": True,
    "preserve_seals": True,
    "preserve_original_page_size": True,
    "preserve_page_orientation": True,
    "overwrite_output": True,
    "generate_logs": True,
    "sharpening_strength": "auto",
    "denoise_strength": "auto",
    "background_cleanup_strength": "auto",
    "min_line_width_pt": "auto",
    "mixed_page_mode": "hybrid",
    "force_rasterize_all_pages": False,
    "enhance_embedded_images": True,
    "raster_compression": "jpeg",
    "jpeg_quality": 92,
    "max_page_megapixels": 200,
    "include_subfolders": True,
    "workers": "auto",
    "verify_output": True,
    "generate_preview": True,
    "preview_folder": "previews",
    "log_folder": "logs",
}

RECOMMENDED_DPI = (300, 400, 600, 800, 1200)

PRESET_ALIASES = {
    "normal": "normal",
    "balanced": "normal",
    "strong": "strong",
    "maximum_readable": "maximum_readable",
    "maximum readable": "maximum_readable",
    "maximum-readable": "maximum_readable",
    "maximum": "maximum_readable",
    "max": "maximum_readable",
    "low_toner": "low_toner",
    "low toner": "low_toner",
    "low-toner": "low_toner",
    "lowtoner": "low_toner",
    "extreme": "low_toner",
    "darkest": "low_toner",
}

# Tuning of the presets.  Raster values act on the scanned-page pipeline,
# vector values on digital content.
#   r_black / gamma / r_knee : tone curve - ink reaching r_black of the
#                              strongest nearby ink becomes black; ink below
#                              r_knee of it (bleed-through) stays light
#   black_cut                : raster grey below this becomes solid black (0)
#   snap                     : vector grey darker than this becomes solid black
#   bold                     : extra outline stroke on vector text, as a
#                              fraction of the font size (faux bold)
#   gentle_extra             : how much lighter signatures/stamps/codes are
#                              mapped than normal ink
#   mono_cut                 : monochrome threshold
#   lm_cap                   : upper limit of the "strongest nearby ink"
#                              reference: lower = faint text next to dark
#                              text is also pushed to black
#   gate                     : strictness of the noise gate (lower keeps
#                              fainter strokes, but also more paper noise)
# A laser printer prints any grey below 100 % as a halftone dot pattern.
# With a weak cartridge those dots are the first thing to disappear, while
# solid black still prints - so the darker presets snap ink to pure black.
PRESETS: Dict[str, Dict[str, float]] = {
    "normal": dict(
        r_black=0.75, gamma=0.95, m_min=140, stroke=0.00, line_gain=1.35,
        sharpen=0.45, denoise=0.80, bg_cleanup=1.0,
        text_scale=0.60, line_scale=0.65, min_line_pt=0.30,
        photo_clip=1.6, photo_gamma=1.03,
        r_knee=0.0, black_cut=0, snap=0.0, bold=0.0, gentle_extra=0.22, mono_cut=172, gate=1.0, lm_cap=255,
    ),
    "strong": dict(
        r_black=0.65, gamma=0.88, m_min=115, stroke=0.35, line_gain=1.70,
        sharpen=0.60, denoise=1.00, bg_cleanup=1.0,
        text_scale=0.40, line_scale=0.45, min_line_pt=0.40,
        photo_clip=2.0, photo_gamma=1.08,
        r_knee=0.0, black_cut=30, snap=0.10, bold=0.0, gentle_extra=0.20, mono_cut=172, gate=1.0, lm_cap=255,
    ),
    "maximum_readable": dict(
        r_black=0.58, gamma=0.80, m_min=95, stroke=0.50, line_gain=2.00,
        sharpen=0.75, denoise=1.00, bg_cleanup=1.0,
        text_scale=0.20, line_scale=0.25, min_line_pt=0.50,
        photo_clip=2.2, photo_gamma=1.12,
        r_knee=0.0, black_cut=60, snap=0.20, bold=0.012, gentle_extra=0.16, mono_cut=180, gate=1.0, lm_cap=255,
    ),
    # For printers with a weak / nearly empty cartridge: all text and lines
    # become solid black (no grey halftones), text is slightly emboldened,
    # thin strokes are grown more and faint scanned ink is boosted hard.
    "low_toner": dict(
        r_black=0.45, gamma=0.70, m_min=32, stroke=0.70, line_gain=2.60,
        sharpen=0.80, denoise=1.00, bg_cleanup=1.0,
        text_scale=0.0, line_scale=0.0, min_line_pt=0.80,
        photo_clip=2.4, photo_gamma=1.22,
        r_knee=0.06, black_cut=110, snap=0.92, bold=0.032, gentle_extra=0.05, mono_cut=205, gate=0.8, lm_cap=70,
    ),
}


class ConfigError(ValueError):
    """Raised for configuration values that cannot be used."""


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"'{key}' must be true or false (got {value!r})")


def _auto_or_float(value: Any, key: str, lo: float, hi: float) -> Optional[float]:
    if value is None or (isinstance(value, str) and value.strip().lower() in ("auto", "")):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{key}' must be \"auto\" or a number (got {value!r})")
    if not lo <= v <= hi:
        raise ConfigError(f"'{key}' must be between {lo} and {hi} (got {v})")
    return v


def load_config(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """Load config.json, merge with defaults and validate.

    Returns (config, warnings).  Raises ConfigError for unusable values.
    """
    warnings: List[str] = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config.json must contain a JSON object")
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if k not in DEFAULT_CONFIG:
                warnings.append(f"Unknown config key ignored: '{k}'")
                continue
            cfg[k] = v
    else:
        warnings.append(f"{path.name} not found - using built-in defaults")
    return validate_config(cfg, warnings), warnings


def validate_config(cfg: Dict[str, Any], warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    bool_keys = [k for k, v in DEFAULT_CONFIG.items() if isinstance(v, bool)]
    for k in bool_keys:
        cfg[k] = _as_bool(cfg[k], k)

    try:
        dpi = int(cfg["target_dpi"])
    except (TypeError, ValueError):
        raise ConfigError(f"'target_dpi' must be a number (got {cfg['target_dpi']!r})")
    if not 72 <= dpi <= 2400:
        raise ConfigError("'target_dpi' must be between 72 and 2400")
    if dpi not in RECOMMENDED_DPI:
        warnings.append(f"target_dpi {dpi} is unusual; recommended values: {RECOMMENDED_DPI}")
    cfg["target_dpi"] = dpi

    mode = str(cfg["output_mode"]).strip().lower()
    if mode in ("gray", "greyscale"):
        mode = "grayscale"
    if mode in ("mono", "bw", "black_and_white", "black-and-white"):
        mode = "monochrome"
    if mode not in ("grayscale", "monochrome"):
        raise ConfigError("'output_mode' must be \"grayscale\" or \"monochrome\"")
    cfg["output_mode"] = mode

    level = PRESET_ALIASES.get(str(cfg["darkness_level"]).strip().lower())
    if level is None:
        raise ConfigError("'darkness_level' must be normal, strong, maximum_readable or low_toner")
    cfg["darkness_level"] = level

    for key in ("sharpening_strength", "denoise_strength", "background_cleanup_strength"):
        cfg[key] = _auto_or_float(cfg[key], key, 0.0, 2.0)
    cfg["min_line_width_pt"] = _auto_or_float(cfg["min_line_width_pt"], "min_line_width_pt", 0.0, 3.0)

    mpm = str(cfg["mixed_page_mode"]).strip().lower()
    if mpm not in ("hybrid", "rasterize"):
        raise ConfigError("'mixed_page_mode' must be \"hybrid\" or \"rasterize\"")
    cfg["mixed_page_mode"] = mpm

    comp = str(cfg["raster_compression"]).strip().lower()
    if comp in ("flate", "png", "zip"):
        comp = "lossless"
    if comp not in ("jpeg", "lossless"):
        raise ConfigError("'raster_compression' must be \"jpeg\" or \"lossless\"")
    cfg["raster_compression"] = comp

    q = int(cfg["jpeg_quality"])
    if not 50 <= q <= 100:
        raise ConfigError("'jpeg_quality' must be between 50 and 100")
    cfg["jpeg_quality"] = q

    mp = float(cfg["max_page_megapixels"])
    if mp < 5:
        raise ConfigError("'max_page_megapixels' must be at least 5")
    cfg["max_page_megapixels"] = mp

    w = cfg["workers"]
    if isinstance(w, str) and w.strip().lower() == "auto":
        cfg["workers"] = "auto"
    else:
        try:
            cfg["workers"] = max(1, int(w))
        except (TypeError, ValueError):
            raise ConfigError("'workers' must be \"auto\" or a whole number")
    return cfg


class Params:
    """Flattened processing parameters (config + preset), picklable."""

    def __init__(self, cfg: Dict[str, Any]):
        p = dict(PRESETS[cfg["darkness_level"]])
        self.preset: str = cfg["darkness_level"]
        self.dpi: int = cfg["target_dpi"]
        self.mono: bool = cfg["output_mode"] == "monochrome"
        self.contrast: bool = cfg["contrast_enhancement"]
        self.adaptive: bool = cfg["adaptive_contrast"]
        self.faint: bool = cfg["faint_text_enhancement"]
        self.lines: bool = cfg["thin_line_enhancement"]
        self.preserve_photos: bool = cfg["preserve_photographs"]
        self.preserve_ink: bool = (cfg["preserve_signatures"] or cfg["preserve_stamps"]
                                   or cfg["preserve_seals"])
        self.preserve_qr: bool = cfg["preserve_qr_codes"]
        self.preserve_bar: bool = cfg["preserve_barcodes"]
        self.mixed_mode: str = cfg["mixed_page_mode"]
        self.force_raster: bool = cfg["force_rasterize_all_pages"]
        self.enhance_images: bool = cfg["enhance_embedded_images"]
        self.compression: str = cfg["raster_compression"]
        self.jpeg_quality: int = cfg["jpeg_quality"]
        self.max_mp: float = cfg["max_page_megapixels"]

        def pick(user: Optional[float], preset_val: float, enabled: bool) -> float:
            if not enabled:
                return 0.0
            return preset_val if user is None else user

        self.sharpen = pick(cfg["sharpening_strength"], p["sharpen"], cfg["sharpening"])
        self.denoise = pick(cfg["denoise_strength"], p["denoise"], cfg["denoise"])
        self.bg_cleanup = pick(cfg["background_cleanup_strength"], p["bg_cleanup"],
                               cfg["background_cleanup"])

        self.r_black = p["r_black"] if self.contrast else 1.0
        self.gamma = p["gamma"] if self.contrast else 1.0
        self.m_min = p["m_min"] if self.faint else 235.0
        self.stroke = p["stroke"] if self.faint else 0.0
        self.line_gain = p["line_gain"] if self.lines else 1.0
        self.text_scale = p["text_scale"] if self.faint else min(1.0, p["text_scale"] + 0.4)
        self.line_scale = p["line_scale"] if self.lines else 1.0
        self.min_line_pt = 0.0 if not self.lines else (
            p["min_line_pt"] if cfg["min_line_width_pt"] is None else cfg["min_line_width_pt"])
        self.photo_clip = p["photo_clip"]
        self.photo_gamma = p["photo_gamma"]
        self.r_knee = p["r_knee"] if self.contrast else 0.0
        self.black_cut = int(p["black_cut"]) if self.contrast else 0
        self.snap = p["snap"] if self.faint else 0.0
        self.bold = p["bold"] if self.faint else 0.0
        self.gentle_extra = p["gentle_extra"]
        self.mono_cut = int(p["mono_cut"])
        self.gate = p["gate"]
        self.lm_cap = float(p["lm_cap"]) if self.adaptive else 255.0

    def describe(self) -> str:
        return (f"preset={self.preset} dpi={self.dpi} mode={'monochrome' if self.mono else 'grayscale'} "
                f"sharpen={self.sharpen:.2f} denoise={self.denoise:.2f} bg_cleanup={self.bg_cleanup:.2f} "
                f"min_line={self.min_line_pt:.2f}pt")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _odd(n: float, minimum: int = 1) -> int:
    n = max(minimum, int(round(n)))
    return n if n % 2 == 1 else n + 1


def _rect_kernel(w: int, h: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_RECT, (max(1, int(w)), max(1, int(h))))


def _ellipse_kernel(d: int) -> np.ndarray:
    d = max(1, int(d))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))


_CROSS3 = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))


def _resize(img: np.ndarray, size_wh: Tuple[int, int], interp: int) -> np.ndarray:
    w, h = max(1, int(size_wh[0])), max(1, int(size_wh[1]))
    if img.shape[1] == w and img.shape[0] == h:
        return img.copy()
    return cv2.resize(img, (w, h), interpolation=interp)


def _fmt(v: float) -> bytes:
    """Format a number for a PDF content stream (never exponent notation)."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s.encode()


def _lum_rgb(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def _lum_cmyk(c: float, m: float, y: float, k: float) -> float:
    return _lum_rgb((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))


def _pix_to_array(pix: "fitz.Pixmap") -> np.ndarray:
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.stride)
    a = a[:, : pix.w * pix.n]
    if pix.n == 1:
        return a.reshape(pix.h, pix.w).copy()
    return a.reshape(pix.h, pix.w, pix.n).copy()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# ---------------------------------------------------------------------------
# Raster enhancement
# ---------------------------------------------------------------------------

def _estimate_illumination(small: np.ndarray, a_dpi: float) -> Tuple[np.ndarray, float]:
    """Smooth paper-illumination surface (uneven lighting, yellowed paper,
    scanner gradients).  Built from ~0.4 inch blocks that are mostly bare
    paper; other blocks (grey bars, photos, dense text) are filled in from
    their neighbours, so they are *not* mistaken for background.
    Returns (surface uint8, paper level)."""
    H, W = small.shape
    f = min(1.0, 50.0 / a_dpi)
    tiny = _resize(small, (W * f, H * f), cv2.INTER_AREA) if f < 1.0 else small
    b = max(4, int(round(0.4 * a_dpi * f)))
    th, tw = tiny.shape
    nby, nbx = max(1, -(-th // b)), max(1, -(-tw // b))
    padded = cv2.copyMakeBorder(tiny, 0, nby * b - th, 0, nbx * b - tw, cv2.BORDER_REFLECT)
    blocks = padded.reshape(nby, b, nbx, b).transpose(0, 2, 1, 3).reshape(nby, nbx, -1)
    p95 = np.percentile(blocks, 95, axis=2).astype(np.float32)
    paperness = (blocks >= (p95[..., None] - 12)).mean(axis=2)
    paper = float(np.percentile(p95, 90))
    paper = max(paper, 40.0)
    cand = (paperness >= 0.5) & (p95 >= 0.75 * paper)
    if cand.sum() < 4:
        return np.full((H, W), np.uint8(round(paper)), np.uint8), paper
    # keep paper blocks, rebuild the others (photos, bars, dense text) from
    # their neighbours; local, so it also copes with a scan pasted on a white
    # page (two different paper levels)
    grid = np.clip(p95, 0, 255).astype(np.uint8)
    holes = (~cand).astype(np.uint8)
    if holes.any():
        grid = cv2.inpaint(grid, holes, 3, cv2.INPAINT_TELEA)
    grid = cv2.GaussianBlur(grid.astype(np.float32), (0, 0), 0.8)
    grid = np.maximum(grid, np.where(cand, p95 - 3, 0).astype(np.float32))
    # block centres -> padded tiny grid -> crop -> analysis resolution
    surf = _resize(grid, (nbx * b, nby * b), cv2.INTER_LINEAR)[:th, :tw]
    surf = _resize(surf, (W, H), cv2.INTER_LINEAR)
    surf = cv2.GaussianBlur(surf, (0, 0), max(1.0, 0.10 * a_dpi))
    return np.clip(surf + 0.5, max(1.0, 0.5 * paper), 255).astype(np.uint8), paper


def _estimate_background(small: np.ndarray, a_dpi: float, strength: float) -> Tuple[np.ndarray, float]:
    """Background used for normalisation = paper illumination, plus removal
    of *light* tints (near-paper shading, stains, highlighter) found with a
    stroke-erasing max filter.  Mid/dark fills (grey bars with white text,
    photos) keep their tone.  Returns (background uint8, paper level)."""
    illum, paper = _estimate_illumination(small, a_dpi)
    if strength <= 0:
        return np.full_like(small, 255), paper
    k = _odd(0.22 * a_dpi, 3)
    # closing (max then min filter) erases text strokes but, unlike a plain
    # max filter, does not drag a bright margin across a paper edge
    local = cv2.morphologyEx(small, cv2.MORPH_CLOSE, _rect_kernel(k, k))
    local = cv2.medianBlur(local, min(_odd(0.12 * a_dpi, 3), 255))
    local = cv2.GaussianBlur(local, (0, 0), max(0.8, 0.025 * a_dpi)).astype(np.float32)
    il = illum.astype(np.float32)
    local = np.minimum(local, il)
    ratio = local / np.maximum(il, 1.0)
    w = np.clip((ratio - 0.80) / 0.10, 0.0, 1.0) * min(1.0, strength)
    w = cv2.GaussianBlur(w, (0, 0), max(1.0, 0.05 * a_dpi))
    bg = w * local + (1.0 - w) * il
    return np.clip(bg + 0.5, 1, 255).astype(np.uint8), paper


def _noise_floor(n_full: np.ndarray, bg_strength: float) -> Tuple[float, float]:
    """Return (darkness floor d0, noise sigma) measured on paper pixels."""
    step = max(1, int(math.sqrt(n_full.size / 2_000_000)))
    sample = n_full[::step, ::step]
    paper = sample[sample > 200]
    if paper.size < 1000:
        return 12.0, 4.0
    med = float(np.median(paper))
    mad = float(np.median(np.abs(paper.astype(np.float32) - med))) * 1.4826
    sigma = max(1.0, mad)
    if bg_strength <= 0:
        return 3.0, sigma
    d0 = (255.0 - med) + (2.0 + 1.5 * min(bg_strength, 2.0)) * sigma
    return float(np.clip(d0, 6.0, 45.0)), sigma


def _guided_filter_self(img: np.ndarray, r: int, eps: float, rows: int = 768) -> np.ndarray:
    """Edge-preserving smoothing (self-guided filter), processed in strips."""
    H = img.shape[0]
    out = np.empty_like(img)
    pad = 2 * r + 2
    ksz = (2 * r + 1, 2 * r + 1)
    for y0 in range(0, H, rows):
        y1 = min(H, y0 + rows)
        a0, a1 = max(0, y0 - pad), min(H, y1 + pad)
        I = img[a0:a1].astype(np.float32)
        mI = cv2.boxFilter(I, -1, ksz, borderType=cv2.BORDER_REFLECT)
        mII = cv2.boxFilter(I * I, -1, ksz, borderType=cv2.BORDER_REFLECT)
        var = mII - mI * mI
        a = var / (var + eps)
        b = mI - a * mI
        del mII, var
        ma = cv2.boxFilter(a, -1, ksz, borderType=cv2.BORDER_REFLECT)
        mb = cv2.boxFilter(b, -1, ksz, borderType=cv2.BORDER_REFLECT)
        q = ma * I + mb
        out[y0:y1] = np.clip(q[y0 - a0: y0 - a0 + (y1 - y0)] + 0.5, 0, 255).astype(np.uint8)
    return out


def _unsharp(img: np.ndarray, sigma: float, amount: float) -> np.ndarray:
    if amount <= 0:
        return img
    blur = cv2.GaussianBlur(img, (0, 0), max(0.3, sigma))
    return cv2.addWeighted(img, 1.0 + amount, blur, -amount, 0)


def _local_ink_max(d: np.ndarray, dpi: float, m_min: float, adaptive: bool,
                   cap: float = 255.0) -> np.ndarray:
    """Smooth map of the strongest ink darkness in the neighbourhood of every
    pixel (computed at ~100 dpi, returned at full resolution, uint8)."""
    H, W = d.shape
    if not adaptive:
        sample = d[:: max(1, H // 600), :: max(1, W // 600)]
        g = float(np.percentile(sample[sample > 20], 99)) if np.any(sample > 20) else 255.0
        return np.full((H, W), np.uint8(np.clip(max(g, m_min), 1, 255)), dtype=np.uint8)
    b = max(1, int(round(dpi / 100.0)))
    pooled = cv2.dilate(d, _rect_kernel(b, b))[b // 2:: b, b // 2:: b] if b > 1 else d.copy()
    lm_dpi = dpi / b
    win = _odd(0.40 * lm_dpi, 3)
    lm = cv2.dilate(pooled, _rect_kernel(win, win)).astype(np.float32)
    lm = cv2.GaussianBlur(lm, (0, 0), max(1.0, 0.15 * lm_dpi))
    lm = np.clip(np.maximum(lm, m_min), 1, max(cap, m_min, 1.0)).astype(np.uint8)
    return _resize(lm, (W, H), cv2.INTER_LINEAR)


def _paper_stats(darkness: np.ndarray) -> Tuple[float, float]:
    """(median offset, robust sigma) of paper pixels in a darkness image."""
    step = max(1, int(math.sqrt(darkness.size / 2_000_000)))
    sample = darkness[::step, ::step]
    paper = sample[sample < 55].astype(np.float32)
    if paper.size < 1000:
        return 4.0, 2.0
    med = float(np.median(paper))
    sig = float(np.median(np.abs(paper - med))) * 1.4826
    # sigma clipping: faint ink (also darkness < 55 on faded pages) must not
    # be mistaken for paper grain, or the noise floor ends up above the ink
    for _ in range(3):
        kept = paper[paper <= med + 3.0 * max(sig, 0.5) + 1.0]
        if kept.size < 1000:
            break
        med = float(np.median(kept))
        sig = float(np.median(np.abs(kept - med))) * 1.4826
    return med, max(0.5, sig)


def _local_ink_floor(db: np.ndarray, dpi: float, k: float) -> np.ndarray:
    """Per-area noise floor (uint8 map at full resolution): paper-grain level
    + k * robust sigma, measured in ~0.5 inch blocks.  Documents that mix a
    noisy scan with clean white margins get the right threshold everywhere."""
    H, W = db.shape
    step = max(1, int(round(dpi / 75.0)))          # sample ~75 dpi, no averaging
    smp = db[::step, ::step]
    b = max(8, int(round(0.5 * dpi / step)))
    h, w = smp.shape
    nby, nbx = -(-h // b), -(-w // b)
    pad = cv2.copyMakeBorder(smp, 0, nby * b - h, 0, nbx * b - w, cv2.BORDER_REFLECT)
    blk = pad.reshape(nby, b, nbx, b).transpose(0, 2, 1, 3).reshape(nby, nbx, -1).astype(np.float32)
    paper = blk < 55
    masked = np.where(paper, blk, np.nan)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN blocks are expected
        med = np.nanmedian(masked, axis=2)
        mad = np.nanmedian(np.abs(masked - med[..., None]), axis=2) * 1.4826
        # sigma clipping (see _paper_stats): on faded pages faint text is
        # lighter than 55 too and would otherwise inflate the floor
        for _ in range(3):
            lim = med + 3.0 * np.maximum(mad, 0.5) + 1.0
            paper = blk <= np.nan_to_num(lim, nan=55.0)[..., None]
            masked = np.where(paper, blk, np.nan)
            med = np.nanmedian(masked, axis=2)
            mad = np.nanmedian(np.abs(masked - med[..., None]), axis=2) * 1.4826
    cnt = paper.sum(axis=2)
    floor = med + k * np.maximum(mad, 0.5)
    good = cnt >= 0.2 * b * b
    glob_med, glob_sig = _paper_stats(db)
    fallback = glob_med + k * glob_sig
    good &= np.isfinite(floor)
    floor = np.where(good, floor, fallback).astype(np.float32)
    # a block's floor never drops far below its neighbours' (protects against
    # blocks dominated by smooth ink) and smooth transitions.  Blocks of
    # clean, measurable paper keep their own value: otherwise a grey header
    # bar raises the floor of the paper around it and erases faint text there.
    reliable = good & (med <= glob_med + 6.0)
    spread = cv2.GaussianBlur(np.maximum(floor, 0.85 * cv2.dilate(floor, _rect_kernel(3, 3))),
                              (0, 0), 0.7)
    floor = np.where(reliable, floor, spread).astype(np.float32)
    floor = cv2.GaussianBlur(floor, (0, 0), 0.45)
    floor = np.clip(floor, 4.0, 45.0)
    up = _resize(floor, (nbx * b, nby * b), cv2.INTER_LINEAR)[:h, :w]
    return _resize(up, (W, H), cv2.INTER_LINEAR).round().astype(np.uint8)


_GATE_LUT: Optional[np.ndarray] = None


def _gate_lut() -> np.ndarray:
    """2-D table [floor * 256 + blurred darkness] -> gate weight (0..256)."""
    global _GATE_LUT
    if _GATE_LUT is None:
        T = np.maximum(np.arange(256, dtype=np.float32)[:, None], 1.0)
        x = np.arange(256, dtype=np.float32)[None, :]
        t = np.clip((x - 0.75 * T) / (0.6 * T), 0.0, 1.0)
        _GATE_LUT = np.round(256.0 * t * t * (3 - 2 * t)).astype(np.uint16).reshape(-1)
    return _GATE_LUT


def _apply_gate(out: np.ndarray, db: np.ndarray, floor_map: np.ndarray, rows: int = 1024) -> None:
    """out = 255 - (255 - out) * gate(db, floor)   (in place, strip-wise)."""
    lut = _gate_lut()
    for y0 in range(0, out.shape[0], rows):
        y1 = min(out.shape[0], y0 + rows)
        dark = 255 - out[y0:y1].astype(np.uint16)
        idx = (floor_map[y0:y1].astype(np.uint16) << 8) | db[y0:y1]
        g = lut[idx]
        out[y0:y1] = (255 - ((dark * g) >> 8)).astype(np.uint8)


def _tone_lut(d0: float, r_black: float, gamma: float, r_knee: float = 0.0) -> np.ndarray:
    """2-D lookup table [local_max * 256 + darkness] -> output gray.

    r = ink darkness relative to the strongest ink nearby.  r >= r_black is
    black; with r_knee > 0 a soft knee keeps marks far weaker than their
    neighbours (bleed-through, background patterns) light."""
    lm = np.arange(256, dtype=np.float32)[:, None]
    d = np.arange(256, dtype=np.float32)[None, :]
    den = np.maximum(lm - d0, 1.0)
    r = np.clip((d - d0) / den, 0.0, 1.0)
    if r_knee > 0:
        k = min(r_knee, 0.45 * r_black)
        # quadratic toe: below the knee the slope fades to zero, above it the
        # curve continues linearly so that r_black still maps to black
        r = np.where(r < 2 * k, r * r / (4 * k), r - k) * (r_black / max(r_black - k, 1e-3))
    t = np.clip(r / max(r_black, 1e-3), 0.0, 1.0) ** gamma
    out = np.round(255.0 * (1.0 - t))
    return np.clip(out, 0, 255).astype(np.uint8).reshape(-1)


def _apply_tone(d: np.ndarray, lm: np.ndarray, lut: np.ndarray, rows: int = 1024) -> np.ndarray:
    H = d.shape[0]
    out = np.empty_like(d)
    for y0 in range(0, H, rows):
        y1 = min(H, y0 + rows)
        idx = (lm[y0:y1].astype(np.uint16) << 8) | d[y0:y1]
        out[y0:y1] = lut[idx]
    return out


def _photo_enhance(g: np.ndarray, dpi: float, P: Params) -> np.ndarray:
    """Photographs: restore faded contrast with a gain-limited linear stretch,
    add mild local contrast (CLAHE) and a gentle darkening gamma.  No
    thresholding, no background removal, no morphology."""
    out = g
    if P.contrast:
        lo, hi = np.percentile(g[:: max(1, g.shape[0] // 400), :: max(1, g.shape[1] // 400)], [0.5, 99.5])
        gain = min(1.8, 235.0 / max(hi - lo, 1.0))
        if gain > 1.02:
            lut = np.clip(250.0 - (hi - np.arange(256)) * gain, 0, 255).astype(np.uint8)
            out = cv2.LUT(out, lut)
        h, w = g.shape
        tiles = (max(2, min(6, w // 96)), max(2, min(6, h // 96)))
        clahe = cv2.createCLAHE(clipLimit=max(1.0, P.photo_clip * 0.6), tileGridSize=tiles)
        out = cv2.addWeighted(out, 0.65, clahe.apply(out), 0.35, 0)
    if abs(P.photo_gamma - 1.0) > 1e-3:
        lut = np.clip(255.0 * (np.arange(256) / 255.0) ** P.photo_gamma + 0.5, 0, 255).astype(np.uint8)
        out = cv2.LUT(out, lut)
    if P.sharpen > 0:
        out = _unsharp(out, 0.7 * dpi / 300.0, 0.30 * P.sharpen)
    return out


def _fill_enhance(g: np.ndarray, P: Params) -> np.ndarray:
    """Flat tinted areas (grey bars, shaded boxes): keep the fill tone (no
    toner-heavy blackening), push darker text towards black and lighter
    (white) text towards white."""
    F = float(np.median(g))
    lo, hi = np.percentile(g, [0.5, 99.5])
    k_dn = min(3.0, F / max(F - lo, 1.0)) if P.contrast else 1.0
    k_up = min(3.0, (255.0 - F) / max(hi - F, 1.0)) if P.contrast else 1.0
    x = np.arange(256, dtype=np.float32)
    lut = np.where(x < F, F - (F - x) * k_dn, F + (x - F) * k_up)
    return cv2.LUT(g, np.clip(lut + 0.5, 0, 255).astype(np.uint8))


def _components(mask: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return [tuple(int(v) for v in stats[i]) for i in range(1, n)]  # x, y, w, h, area


def _detect_tone_regions(n_small: np.ndarray, a_dpi: float) -> Tuple[np.ndarray, np.ndarray]:
    """Find continuous-tone areas on the illumination-corrected analysis image.

    Candidates are areas dense in *smooth* non-paper pixels (text only has
    non-paper pixels on sharp edges).  Each region is then classified by the
    spread of its smooth tones: photographs cover a range of tones, flat
    fills (grey bars, shaded boxes) sit at one level.
    Returns (photo_mask, fill_mask) as uint8 0/255 images."""
    g = n_small
    nonpaper = (g > 12) & (g < 226)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    smooth = cv2.magnitude(gx, gy) < 110
    ms = nonpaper & smooth
    k = _odd(0.15 * a_dpi, 3)
    cnt = cv2.blur(ms.astype(np.float32), (k, k))
    cand = (cnt > 0.35).astype(np.uint8) * 255
    cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, _ellipse_kernel(_odd(0.10 * a_dpi, 3)))
    cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, _ellipse_kernel(_odd(0.08 * a_dpi, 3)))
    photo = np.zeros_like(cand)
    fill = np.zeros_like(cand)
    min_area = 0.30 * a_dpi * a_dpi
    min_side = 0.30 * a_dpi
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area or min(w, h) < min_side:
            continue
        comp = labels[y:y + h, x:x + w] == i
        vals = g[y:y + h, x:x + w][comp & ms[y:y + h, x:x + w]]
        if vals.size < 50:
            continue
        p10, p50, p90 = np.percentile(vals, [10, 50, 90])
        if (p90 - p10) >= 24:
            target = photo
        elif p50 <= 215:
            target = fill
        else:
            continue  # near-paper tint: handled by background cleanup
        if area >= 0.5 * w * h:
            target[y:y + h, x:x + w] = 255
        else:
            target[y:y + h, x:x + w][comp] = 255
    return photo, fill


def _detect_color_ink(rgb_small: np.ndarray, a_dpi: float, exclude: np.ndarray) -> np.ndarray:
    """Coloured ink: signatures, stamps, seals (saturated, darker than paper)."""
    hsv = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    ink = ((s > 60) & (v < 235) & (v > 25)).astype(np.uint8) * 255
    ink[exclude > 0] = 0
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, _rect_kernel(2, 2))
    if not np.any(ink):
        return ink
    ink = cv2.dilate(ink, _ellipse_kernel(_odd(0.05 * a_dpi, 3)))
    out = np.zeros_like(ink)
    min_area = 0.01 * a_dpi * a_dpi
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    for i in range(1, n):
        if stats[i][4] >= min_area:
            out[labels == i] = 255
    return out


def _detect_codes(gray: np.ndarray, dpi: float, bg_small: np.ndarray, want_qr: bool,
                  want_bar: bool) -> List[Tuple[float, float, float, float]]:
    """Locate QR codes / barcodes. Returns boxes as fractions (x0, y0, x1, y1)."""
    H, W = gray.shape
    c_dpi = min(dpi, 300.0)
    f = c_dpi / dpi
    g3 = _resize(gray, (W * f, H * f), cv2.INTER_AREA)
    bg3 = _resize(bg_small, (g3.shape[1], g3.shape[0]), cv2.INTER_LINEAR)
    n3 = cv2.divide(g3, bg3, scale=255)
    h3, w3 = n3.shape
    boxes: List[Tuple[float, float, float, float]] = []

    def add(x0, y0, x1, y1):
        boxes.append((max(0.0, x0 / w3), max(0.0, y0 / h3), min(1.0, x1 / w3), min(1.0, y1 / h3)))

    # 1) OpenCV detectors
    if want_qr:
        try:
            ok, pts = cv2.QRCodeDetector().detectMulti(n3)
            if ok and pts is not None:
                for quad in pts:
                    xs, ys = quad[:, 0], quad[:, 1]
                    add(xs.min(), ys.min(), xs.max(), ys.max())
        except Exception:
            pass
    if want_bar and hasattr(cv2, "barcode"):
        try:
            res = cv2.barcode.BarcodeDetector().detect(n3)
            ok, pts = res[0], res[1]
            if ok and pts is not None:
                for quad in pts:
                    xs, ys = quad[:, 0], quad[:, 1]
                    add(xs.min(), ys.min(), xs.max(), ys.max())
        except Exception:
            pass

    # 2) Texture heuristic (dense high-contrast module patterns)
    bw = (n3 < 128).astype(np.float32)
    k = _odd(0.12 * c_dpi, 3)
    p = cv2.blur(bw, (k, k))
    th = np.zeros_like(bw)
    tv = np.zeros_like(bw)
    th[:, 1:] = np.abs(bw[:, 1:] - bw[:, :-1])
    tv[1:, :] = np.abs(bw[1:, :] - bw[:-1, :])
    th = cv2.blur(th, (k, k))
    tv = cv2.blur(tv, (k, k))
    close_k = _ellipse_kernel(_odd(0.05 * c_dpi, 3))
    if want_qr:
        qr = ((p > 0.28) & (p < 0.75) & (th > 0.10) & (tv > 0.10)).astype(np.uint8) * 255
        qr = cv2.morphologyEx(qr, cv2.MORPH_CLOSE, close_k)
        for x, y, w, h, area in _components(qr):
            side = max(w, h)
            if area >= 0.12 * c_dpi * c_dpi and 0.6 <= w / max(h, 1) <= 1.6 and area >= 0.55 * w * h \
                    and side <= 4 * c_dpi:
                pad = 0.06 * c_dpi
                add(x - pad, y - pad, x + w + pad, y + h + pad)
    if want_bar:
        bar = ((p > 0.2) & (p < 0.8) & (th > 0.12) & (tv < 0.04)).astype(np.uint8) * 255
        bar = cv2.morphologyEx(bar, cv2.MORPH_CLOSE, close_k)
        for x, y, w, h, area in _components(bar):
            if area >= 0.08 * c_dpi * c_dpi and w >= 0.5 * c_dpi and h >= 0.12 * c_dpi:
                pad = 0.06 * c_dpi
                add(x - pad, y - pad, x + w + pad, y + h + pad)
    return boxes


def _detect_lines(out: np.ndarray, dpi: float) -> Optional[np.ndarray]:
    """Long thin horizontal / vertical lines (table borders, separators).
    Returns a full-resolution uint8 mask or None."""
    H, W = out.shape
    l_dpi = min(dpi, 200.0)
    f = l_dpi / dpi
    sm = _resize(out, (W * f, H * f), cv2.INTER_AREA)
    bw = (sm < 205).astype(np.uint8) * 255
    L = max(8, int(round(0.30 * l_dpi)))
    gap = _odd(0.02 * l_dpi, 3)
    thick = max(3, int(round(0.035 * l_dpi)) + 1)
    h = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, _rect_kernel(gap, 1))
    h = cv2.morphologyEx(h, cv2.MORPH_OPEN, _rect_kernel(L, 1))
    h_thick = cv2.morphologyEx(h, cv2.MORPH_OPEN, _rect_kernel(1, thick))
    h = cv2.subtract(h, h_thick)
    v = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, _rect_kernel(1, gap))
    v = cv2.morphologyEx(v, cv2.MORPH_OPEN, _rect_kernel(1, L))
    v_thick = cv2.morphologyEx(v, cv2.MORPH_OPEN, _rect_kernel(thick, 1))
    v = cv2.subtract(v, v_thick)
    lines = cv2.bitwise_or(h, v)
    if not np.any(lines):
        return None
    lines = cv2.dilate(lines, _CROSS3)
    return _resize(lines, (W, H), cv2.INTER_NEAREST)


def _feather(mask: np.ndarray, a_dpi: float) -> np.ndarray:
    m = cv2.dilate(mask, _ellipse_kernel(_odd(0.03 * a_dpi, 3)))
    return np.clip(cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), max(0.8, 0.02 * a_dpi)), 0, 1)


def _blend_regions(out: np.ndarray, alpha_small: np.ndarray, scale: float, producer) -> None:
    """For each region of `alpha_small` (analysis resolution) produce an
    alternative crop with `producer(y0, y1, x0, x1)` and blend it into `out`."""
    H, W = out.shape
    mask = (alpha_small > 0.01).astype(np.uint8)
    for x, y, w, h, _ in _components(mask):
        X0 = max(0, int(math.floor(x / scale)))
        Y0 = max(0, int(math.floor(y / scale)))
        X1 = min(W, int(math.ceil((x + w) / scale)))
        Y1 = min(H, int(math.ceil((y + h) / scale)))
        if X1 <= X0 or Y1 <= Y0:
            continue
        alt = producer(Y0, Y1, X0, X1)
        a = _resize(alpha_small[y:y + h, x:x + w], (X1 - X0, Y1 - Y0), cv2.INTER_LINEAR)
        cur = out[Y0:Y1, X0:X1].astype(np.float32)
        out[Y0:Y1, X0:X1] = np.clip(cur + a * (alt.astype(np.float32) - cur) + 0.5, 0, 255).astype(np.uint8)


def enhance_raster(gray: np.ndarray, dpi: float, P: Params, rgb_small: Optional[np.ndarray] = None,
                   role: str = "page", stats: Optional[Dict[str, Any]] = None) -> np.ndarray:
    """Enhance a grayscale document raster for laser printing.

    gray      : uint8 HxW image (any resolution)
    dpi       : resolution of `gray` in dots per inch
    rgb_small : optional low-resolution RGB rendering of the same area
                (used to find coloured signatures / stamps)
    role      : "page" (scanned page), "image" (embedded image) or
                "background" (page-size artwork under vector text: gentler)
    Returns a uint8 image (values 0/255 only in monochrome mode).
    """
    stats = stats if stats is not None else {}
    H, W = gray.shape
    if H < 8 or W < 8:
        return gray
    s = dpi / 300.0

    # --- analysis copy (~150 dpi) ---------------------------------------------
    a_dpi = min(float(dpi), 150.0)
    fa = a_dpi / dpi
    small = _resize(gray, (W * fa, H * fa), cv2.INTER_AREA)
    bg_small, paper = _estimate_background(small, a_dpi, P.bg_cleanup)
    n_small = cv2.divide(small, bg_small, scale=255)
    illum_small, _ = _estimate_illumination(small, a_dpi)
    n_illum = cv2.divide(small, illum_small, scale=255)

    # --- region detection -------------------------------------------------------
    photo = np.zeros_like(n_small)
    fill = np.zeros_like(n_small)
    photo_raw, fill_raw = _detect_tone_regions(n_illum, a_dpi)
    if P.preserve_photos:
        photo = photo_raw
    if role != "background":
        fill = fill_raw
    gentle = np.zeros_like(n_small)
    if P.preserve_ink and rgb_small is not None:
        rgb = _resize(rgb_small, (n_small.shape[1], n_small.shape[0]), cv2.INTER_AREA)
        gentle = cv2.bitwise_or(gentle, _detect_color_ink(rgb, a_dpi, photo))
    if (P.preserve_qr or P.preserve_bar) and min(H, W) / dpi >= 0.3:
        for x0, y0, x1, y1 in _detect_codes(gray, dpi, bg_small, P.preserve_qr, P.preserve_bar):
            hs, ws = gentle.shape
            gentle[int(y0 * hs):int(math.ceil(y1 * hs)), int(x0 * ws):int(math.ceil(x1 * ws))] = 255
            stats["codes"] = stats.get("codes", 0) + 1
    gentle[(photo > 0) | (fill > 0)] = 0
    fill[photo > 0] = 0
    stats["fill_regions"] = len(_components(fill)) if np.any(fill) else 0
    stats["photo_regions"] = len(_components(photo)) if np.any(photo) else 0
    stats["protected_regions"] = len(_components(gentle)) if np.any(gentle) else 0

    # --- illumination correction at full resolution ------------------------------
    bg_full = _resize(bg_small, (W, H), cv2.INTER_LINEAR)
    N = cv2.divide(gray, bg_full, scale=255)
    del bg_full
    if P.bg_cleanup <= 0 and P.contrast:
        # no flattening requested: still even out lighting softly with CLAHE
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        N = cv2.addWeighted(N, 0.6, clahe.apply(N), 0.4, 0)
    d0_raw, sigma = _noise_floor(N, P.bg_cleanup)
    stats["noise_sigma"] = round(sigma, 2)
    stats["paper_level"] = round(paper, 1)

    # keep un-denoised crops for the gentle (signature/stamp/code) regions
    gentle_alpha = _feather(gentle, a_dpi) if np.any(gentle) else None
    photo_alpha = _feather(photo, a_dpi) if np.any(photo) else None
    fill_alpha = _feather(fill, a_dpi) if np.any(fill) else None

    # --- edge-preserving denoise + unsharp mask ------------------------------------
    N2 = N
    if P.denoise > 0:
        r = max(1, int(round(1.6 * s)))
        eps = (max(2.0, 2.2 * sigma) * P.denoise) ** 2 + 4.0
        N2 = _guided_filter_self(N, r, eps)
    # noise floor measured after denoising: coherent faint strokes survive the
    # filter while paper grain does not, so faint text can be kept
    d0 = _noise_floor(N2, P.bg_cleanup)[0] if N2 is not N else d0_raw
    d0 = min(d0, d0_raw)
    if P.sharpen > 0:
        N2 = _unsharp(N2, 0.8 * s, P.sharpen)

    # --- adaptive local-ink tone mapping ---------------------------------------
    m_min = P.m_min
    r_black, gamma = P.r_black, P.gamma
    if role == "background":
        m_min, r_black, gamma = max(m_min, 220.0), max(r_black, 0.9), 1.0
    d = cv2.bitwise_not(N2)
    if N2 is not N:
        del N2
    lm = _local_ink_max(d, dpi, m_min, P.adaptive, P.lm_cap if role != "background" else 255.0)
    # coherence gate: a slightly blurred copy separates real (spatially
    # coherent) faint strokes from isolated paper-grain / JPEG noise pixels
    db = cv2.GaussianBlur(d, (0, 0), max(0.6, 0.9 * s))
    off_b, sig_b = _paper_stats(db)
    k_noise = (2.5 + 1.5 * min(P.bg_cleanup, 2.0)) * P.gate
    floor_map = _local_ink_floor(db, dpi, k_noise)
    d0_tone = float(min(d0, max(2.0, off_b + 1.0 * sig_b)))
    stats["ink_floor"] = round(float(np.median(floor_map[::16, ::16])), 1)
    out = _apply_tone(d, lm, _tone_lut(d0_tone, r_black, gamma, P.r_knee if role != "background" else 0.0))
    _apply_gate(out, db, floor_map)
    del d, db, floor_map

    # --- controlled stroke growth for thin letters -----------------------------
    if P.stroke > 0 and dpi >= 400 and role != "background":
        # whole part: full 1-pixel growth steps (scaled with resolution so
        # the physical growth is the same at 400 and 1200 dpi); fraction:
        # blended partial step
        amount = P.stroke * dpi / 800.0
        full, frac = int(amount), amount - int(amount)
        for _ in range(full):
            out = cv2.erode(out, _CROSS3)
        if frac > 0.05:
            er = cv2.erode(out, _CROSS3)
            out = cv2.addWeighted(out, 1.0 - frac, er, frac, 0)
            del er

    # --- table borders / separators ------------------------------------------
    if P.lines and P.line_gain > 1.0 and role != "background":
        lines = _detect_lines(out, dpi)
        if lines is not None:
            lut = np.clip(255.0 - (255.0 - np.arange(256)) * P.line_gain, 0, 255).astype(np.uint8)
            dark = cv2.LUT(out, lut)
            if dpi >= 400:
                dark = cv2.erode(dark, _CROSS3)
            np.copyto(out, dark, where=lines > 0)
            stats["line_pixels"] = int(np.count_nonzero(lines))
            del dark, lines

    # --- protected regions -----------------------------------------------------
    if gentle_alpha is not None:
        g_lut = _tone_lut(d0, min(0.92, r_black + P.gentle_extra), 1.0)

        def gentle_crop(y0, y1, x0, x1):
            dc = cv2.bitwise_not(N[y0:y1, x0:x1])
            return _apply_tone(dc, lm[y0:y1, x0:x1], g_lut)

        _blend_regions(out, gentle_alpha, fa, gentle_crop)
    if fill_alpha is not None:
        def fill_crop(y0, y1, x0, x1):
            il = _resize(illum_small[int(y0 * fa):int(math.ceil(y1 * fa)) or 1,
                                     int(x0 * fa):int(math.ceil(x1 * fa)) or 1], (x1 - x0, y1 - y0),
                         cv2.INTER_LINEAR)
            return _fill_enhance(cv2.divide(gray[y0:y1, x0:x1], il, scale=255), P)

        _blend_regions(out, fill_alpha, fa, fill_crop)
    if photo_alpha is not None:
        _blend_regions(out, photo_alpha, fa, lambda y0, y1, x0, x1: _photo_enhance(gray[y0:y1, x0:x1], dpi, P))
    # --- solid black ---------------------------------------------------------------
    # dark grey would be printed as a halftone dot pattern; on a weak cartridge
    # solid black prints far more reliably (photos are left untouched)
    if P.black_cut > 0 and role != "background":
        cut = np.where(np.arange(256) < P.black_cut, 0,
                       np.arange(256)).astype(np.uint8)
        if photo_alpha is None:
            cv2.LUT(out, cut, dst=out)
        else:
            keep = _resize((photo_alpha > 0.02).astype(np.uint8), (W, H), cv2.INTER_NEAREST)
            np.copyto(out, cv2.LUT(out, cut), where=keep == 0)
            del keep
    # --- monochrome conversion -------------------------------------------------
    if P.mono:
        # generous cut: anything visibly grey after enhancement is ink (the
        # noise gate has already cleaned the paper)
        mono = np.where(out < P.mono_cut, 0, 255).astype(np.uint8)
        if photo_alpha is not None:
            pm = (photo_alpha > 0.5).astype(np.uint8)
            for x, y, w, h, _ in _components(pm):
                X0, Y0 = int(x / fa), int(y / fa)
                X1, Y1 = min(W, int(math.ceil((x + w) / fa))), min(H, int(math.ceil((y + h) / fa)))
                crop = Image.fromarray(out[Y0:Y1, X0:X1]).convert("1")  # Floyd-Steinberg
                mono[Y0:Y1, X0:X1] = np.array(crop, dtype=np.uint8) * 255
        out = mono
    return out


# ---------------------------------------------------------------------------
# PDF content-stream rewriting (vector pages)
# ---------------------------------------------------------------------------

_DELIMS = b"()<>[]{}/%"
_TOKEN_RE = re.compile(
    rb"(?P<ws>[\x00\t\n\x0c\r ]+)"
    rb"|(?P<comment>%[^\r\n]*)"
    rb"|(?P<num>[+-]?(?:\d+\.\d*|\.\d+|\d+))(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
    rb"|(?P<name>/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)"
    rb"|(?P<dopen><<)|(?P<dclose>>>)"
    rb"|(?P<hex><[^<>]*>)"
    rb"|(?P<aopen>\[)|(?P<aclose>\])"
    rb"|(?P<bopen>\{)|(?P<bclose>\})"
    rb"|(?P<sopen>\()"
    rb"|(?P<kw>[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"
    rb"|(?P<other>.)",
    re.S,
)
_STR_SPECIAL = re.compile(rb"[()\\]")
_EI_RE = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r ]|$)")

_PATH_OPS = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}
_PAINT_OPS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"}
_FILL_PAINT = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_STROKE_PAINT = {b"S", b"s", b"B", b"B*", b"b", b"b*"}
_TEXT_SHOW = {b"Tj", b"TJ", b"'", b'"'}
# graphics-state operators some producers (incl. MuPDF) emit between path
# construction and painting; they must not terminate the current path
_STATE_IN_PATH = {b"w", b"J", b"j", b"M", b"d", b"ri", b"i", b"gs", b"g", b"G", b"rg", b"RG",
                  b"k", b"K", b"cs", b"CS", b"sc", b"scn", b"SC", b"SCN"}
_VALUE_KW = {b"true", b"false", b"null"}

_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m1, m2):
    """PDF matrix product m1 x m2 (row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _mat_scale(m) -> float:
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1e-6


class _GState:
    __slots__ = ("fill", "stroke", "afill", "astroke", "fcs", "scs", "fcs_dirty", "scs_dirty",
                 "ctm", "lw", "ca", "CA", "tf_size", "tr")

    def __init__(self, known: bool):
        z = 0.0 if known else None
        self.fill: Optional[float] = z       # original colour (luminance 0..1)
        self.stroke: Optional[float] = z
        self.afill: Optional[float] = z      # colour actually in effect in the output
        self.astroke: Optional[float] = z
        self.fcs: Tuple[str, bytes] = ("G", b"/DeviceGray")  # (kind, name as written)
        self.scs: Tuple[str, bytes] = ("G", b"/DeviceGray")
        self.fcs_dirty = False               # our injected 'g' replaced the colour space
        self.scs_dirty = False
        self.ctm = _IDENT
        self.lw: Optional[float] = 1.0 if known else None
        self.ca = 1.0
        self.CA = 1.0
        self.tf_size = 12.0
        self.tr = 0

    def copy(self) -> "_GState":
        n = _GState.__new__(_GState)
        for k in _GState.__slots__:
            setattr(n, k, getattr(self, k))
        return n


class _VectorContext:
    """Per-page information shared by the stream rewriters."""

    def __init__(self, doc, P: Params, lod_bins: Dict[int, List[int]]):
        self.doc = doc
        self.P = P
        self.lod_bins = lod_bins
        self.counts = {"text": 0, "lines": 0, "fills": 0, "widths": 0, "bold": 0}

    def light_on_dark(self, L: float) -> bool:
        key = int(round(L * 40))
        dark = light = 0
        for k in (key - 1, key, key + 1):
            v = self.lod_bins.get(k)
            if v:
                dark += v[0]
                light += v[1]
        return dark > light


class _StreamRewriter:
    """Rewrites one content stream: injects darker gray colours right before
    text-showing and path-painting operators, and minimum line widths before
    strokes.  The original operators stay untouched, so a parse problem can
    never corrupt the drawing - it can only result in less darkening."""

    def __init__(self, data: bytes, owner_xref: int, ctx: _VectorContext, known_state: bool,
                 matrix=_IDENT):
        self.data = data
        self.owner = owner_xref
        self.ctx = ctx
        self.P = ctx.P
        self.st = _GState(known_state)
        self.st.ctm = matrix
        self.stack: List[_GState] = []
        self.out: List[bytes] = []
        self.pos = 0
        self.changed = False
        self.tm = _IDENT
        self.path_idx: Optional[int] = None
        self.path_state_ops = False
        self.path_box = [math.inf, math.inf, -math.inf, -math.inf]
        self.forms: List[int] = []
        self._res_cache: Dict[Tuple[str, str], Any] = {}

    # -- resource helpers ---------------------------------------------------
    def _res_get(self, category: str, name: bytes, key: str) -> Tuple[str, str]:
        nm = name.decode("latin-1").lstrip("/")
        ck = (category, nm + "/" + key)
        if ck in self._res_cache:
            return self._res_cache[ck]
        doc = self.ctx.doc
        val = ("null", "null")
        xref = self.owner
        for _ in range(16):
            try:
                t, v = doc.xref_get_key(xref, f"Resources/{category}/{nm}/{key}" if key else
                                        f"Resources/{category}/{nm}")
            except Exception:
                t, v = "null", "null"
            if t != "null":
                val = (t, v)
                break
            try:
                rt, _ = doc.xref_get_key(xref, "Resources")
                pt, pv = doc.xref_get_key(xref, "Parent")
            except Exception:
                break
            if rt != "null" or pt != "xref":
                break
            xref = int(pv.split()[0])
        self._res_cache[ck] = val
        return val

    def _xobject_xref(self, name: bytes) -> int:
        t, v = self._res_get("XObject", name, "")
        if t == "xref":
            try:
                return int(v.split()[0])
            except ValueError:
                return 0
        return 0

    # -- output helpers -----------------------------------------------------
    def _flush(self, upto: int) -> None:
        if upto > self.pos:
            self.out.append(self.data[self.pos:upto])
            self.pos = upto

    def _inject(self, at: int, ops: List[bytes]) -> None:
        self._flush(at)
        self.out.append(b"\n" + b" ".join(ops) + b"\n")
        self.changed = True

    # -- colour mapping -----------------------------------------------------
    def _text_target(self, L: Optional[float], alpha: float) -> Optional[float]:
        if L is None or L >= 0.97:
            return L
        m = _mat_mul(self.tm, self.st.ctm)
        size = abs(self.st.tf_size) * _mat_scale(m)
        ang = abs(math.degrees(math.atan2(m[1], m[0]))) % 90.0
        diagonal = 8.0 < ang < 82.0
        if alpha < 0.85 or (size >= 28 and L >= 0.55) or (diagonal and size >= 14 and L >= 0.45):
            return L  # watermark-like: keep
        if self.ctx.light_on_dark(L):
            return L
        return self._darken(L, self.P.text_scale)

    def _darken(self, L: float, scale: float) -> float:
        """Map an ink grey to its print grey.  Greys darker than `snap` (and
        everything in monochrome mode) become solid black: a printer renders
        any grey as a halftone dot pattern, which fades first on a weak
        cartridge."""
        t = L * scale
        if self.P.mono and t < 0.85:
            return 0.0
        if L < self.P.snap or t < self.P.snap * 0.5:
            return 0.0
        return t

    def _line_target(self, L: Optional[float]) -> Optional[float]:
        if L is None or L >= 0.97 or self.st.CA < 0.85:
            return L
        return self._darken(L, self.P.line_scale)

    def _fill_target(self, L: Optional[float], w: float, h: float) -> Optional[float]:
        if L is None:
            return None
        lo, hi = min(w, h), max(w, h)
        if lo <= 1.6 and hi >= 3 * max(lo, 0.05):
            return self._line_target(L)                      # thin rule / table border
        if hi <= 12.0 and L < 0.97 and self.st.ca >= 0.85:   # glyph-sized shapes
            return self._darken(L, self.P.text_scale)
        if self.P.bg_cleanup > 0 and L >= 0.955 and L < 1.0:
            return 1.0                                        # near-white tint
        return L

    def _set_fill(self, at: int, target: Optional[float], ops: List[bytes]) -> None:
        st = self.st
        if target is None or (st.afill is not None and abs(target - st.afill) < 1e-4):
            return
        ops.append(_fmt(target) + b" g")
        st.afill = target
        if st.fcs[0] != "G" or st.fcs[1] != b"/DeviceGray":
            st.fcs_dirty = True

    def _set_stroke(self, at: int, target: Optional[float], ops: List[bytes]) -> None:
        st = self.st
        if target is None or (st.astroke is not None and abs(target - st.astroke) < 1e-4):
            return
        ops.append(_fmt(target) + b" G")
        st.astroke = target
        if st.scs[0] != "G" or st.scs[1] != b"/DeviceGray":
            st.scs_dirty = True

    # -- colour-space handling ------------------------------------------------
    def _cs_kind(self, name: bytes) -> str:
        if name in (b"/DeviceGray", b"/G", b"/CalGray"):
            return "G"
        if name in (b"/DeviceRGB", b"/RGB", b"/CalRGB"):
            return "RGB"
        if name in (b"/DeviceCMYK", b"/CMYK"):
            return "CMYK"
        if name == b"/Pattern":
            return "other"
        t, v = self._res_get("ColorSpace", name, "")
        txt = v if isinstance(v, str) else ""
        if t == "xref":
            try:
                txt = self.ctx.doc.xref_object(int(v.split()[0]), compressed=True)
            except Exception:
                txt = ""
        if "/ICCBased" in txt:
            m = re.search(r"\[\s*/ICCBased\s+(\d+)\s+0\s+R", txt)
            if m:
                try:
                    nt, nv = self.ctx.doc.xref_get_key(int(m.group(1)), "N")
                    return {"1": "G", "3": "RGB", "4": "CMYK"}.get(nv, "other")
                except Exception:
                    return "other"
        if txt.startswith("/DeviceGray") or "/CalGray" in txt:
            return "G"
        if txt.startswith("/DeviceRGB") or "/CalRGB" in txt:
            return "RGB"
        if txt.startswith("/DeviceCMYK"):
            return "CMYK"
        return "other"

    @staticmethod
    def _lum(kind: str, nums: List[float]) -> Optional[float]:
        try:
            if kind == "G" and len(nums) == 1:
                return max(0.0, min(1.0, nums[0]))
            if kind == "RGB" and len(nums) == 3:
                return max(0.0, min(1.0, _lum_rgb(*nums)))
            if kind == "CMYK" and len(nums) == 4:
                return max(0.0, min(1.0, _lum_cmyk(*nums)))
        except Exception:
            return None
        return None

    # -- main loop --------------------------------------------------------------
    def run(self) -> Optional[bytes]:
        data = self.data
        n = len(data)
        i = 0
        operands: List[Tuple[str, int, int]] = []
        depth_arr = depth_dict = 0
        st_ref = self
        while i < n:
            m = _TOKEN_RE.match(data, i)
            if m is None:
                i += 1
                continue
            kind = m.lastgroup
            start, end = m.start(), m.end()
            if kind == "ws" or kind == "comment":
                i = end
                continue
            if kind == "sopen":
                depth = 0
                j = start
                while True:
                    sm = _STR_SPECIAL.search(data, j)
                    if sm is None:
                        j = n
                        break
                    ch = data[sm.start()]
                    if ch == 0x5C:
                        j = sm.start() + 2
                        continue
                    if ch == 0x28:
                        depth += 1
                    else:
                        depth -= 1
                        if depth == 0:
                            j = sm.start() + 1
                            break
                    j = sm.start() + 1
                operands.append(("str", start, j))
                i = j
                continue
            i = end
            if kind == "aopen":
                depth_arr += 1
                operands.append(("arr", start, end))
                continue
            if kind == "aclose":
                depth_arr = max(0, depth_arr - 1)
                continue
            if kind == "dopen":
                depth_dict += 1
                operands.append(("dict", start, end))
                continue
            if kind == "dclose":
                depth_dict = max(0, depth_dict - 1)
                continue
            if kind in ("num", "name", "hex", "bopen", "bclose", "other"):
                operands.append((kind, start, end))
                continue
            # keyword
            tok = data[start:end]
            if depth_arr or depth_dict or tok in _VALUE_KW:
                operands.append(("kw", start, end))
                continue
            if tok == b"BI":
                idm = re.compile(rb"[\x00\t\n\x0c\r ]ID[\x00\t\n\x0c\r ]").search(data, end)
                if idm is None:
                    i = n
                else:
                    em = _EI_RE.search(data, idm.end())
                    i = n if em is None else em.end()
                operands = []
                self._end_path_if_stray()
                continue
            try:
                st_ref._operator(tok, operands, start, end)
            except (ValueError, IndexError, ZeroDivisionError, OverflowError):
                pass  # malformed operands: leave the drawing exactly as it was
            operands = []
        if not self.changed:
            return None
        self._flush(n)
        return b"".join(self.out)

    def _nums(self, operands, count: Optional[int] = None) -> List[float]:
        vals = [float(self.data[s:e]) for k, s, e in operands if k == "num"]
        if count is not None:
            if len(vals) < count:
                raise ValueError("operand count")
            vals = vals[-count:]
        return vals

    def _end_path_if_stray(self) -> None:
        self.path_idx = None
        self.path_box = [math.inf, math.inf, -math.inf, -math.inf]

    def _add_pt(self, x: float, y: float) -> None:
        a, b, c, d, e, f = self.st.ctm
        X, Y = a * x + c * y + e, b * x + d * y + f
        bx = self.path_box
        bx[0], bx[1] = min(bx[0], X), min(bx[1], Y)
        bx[2], bx[3] = max(bx[2], X), max(bx[3], Y)

    def _operator(self, op: bytes, operands, start: int, end: int) -> None:
        st = self.st
        first = operands[0][1] if operands else start

        if op in _PATH_OPS:
            if self.path_idx is None:
                self._flush(first)
                self.path_idx = len(self.out)
                self.path_state_ops = False
                self.path_box = [math.inf, math.inf, -math.inf, -math.inf]
            if op == b"re":
                x, y, w, h = self._nums(operands, 4)
                for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                    self._add_pt(px, py)
            elif op != b"h":
                v = self._nums(operands)
                for k in range(0, len(v) - 1, 2):
                    self._add_pt(v[k], v[k + 1])
            return
        if op in (b"W", b"W*"):
            return
        if op in _PAINT_OPS:
            self._paint(op, start, end)
            return
        if self.path_idx is not None:
            if op in _STATE_IN_PATH:
                self.path_state_ops = True
            else:
                self._end_path_if_stray()

        if op == b"q":
            self.stack.append(st.copy())
        elif op == b"Q":
            if self.stack:
                self.st = self.stack.pop()
        elif op == b"cm":
            st.ctm = _mat_mul(tuple(self._nums(operands, 6)), st.ctm)
        elif op == b"w":
            st.lw = self._nums(operands, 1)[0]
        elif op == b"gs":
            names = [self.data[s:e] for k, s, e in operands if k == "name"]
            if names:
                t, v = self._res_get("ExtGState", names[-1], "ca")
                if t in ("float", "int"):
                    st.ca = float(v)
                t, v = self._res_get("ExtGState", names[-1], "CA")
                if t in ("float", "int"):
                    st.CA = float(v)
                t, v = self._res_get("ExtGState", names[-1], "LW")
                if t in ("float", "int"):
                    st.lw = float(v)
        elif op in (b"g", b"rg", b"k"):
            kind = {b"g": "G", b"rg": "RGB", b"k": "CMYK"}[op]
            L = self._lum(kind, self._nums(operands, {"G": 1, "RGB": 3, "CMYK": 4}[kind]))
            st.fill = st.afill = L
            st.fcs = (kind, {"G": b"/DeviceGray", "RGB": b"/DeviceRGB", "CMYK": b"/DeviceCMYK"}[kind])
            st.fcs_dirty = False
        elif op in (b"G", b"RG", b"K"):
            kind = {b"G": "G", b"RG": "RGB", b"K": "CMYK"}[op]
            L = self._lum(kind, self._nums(operands, {"G": 1, "RGB": 3, "CMYK": 4}[kind]))
            st.stroke = st.astroke = L
            st.scs = (kind, {"G": b"/DeviceGray", "RGB": b"/DeviceRGB", "CMYK": b"/DeviceCMYK"}[kind])
            st.scs_dirty = False
        elif op in (b"cs", b"CS"):
            names = [self.data[s:e] for k, s, e in operands if k == "name"]
            name = names[-1] if names else b"/DeviceGray"
            kind = self._cs_kind(name)
            init = 0.0 if kind in ("G", "RGB", "CMYK") else None
            if op == b"cs":
                st.fcs, st.fcs_dirty, st.fill, st.afill = (kind, name), False, init, init
            else:
                st.scs, st.scs_dirty, st.stroke, st.astroke = (kind, name), False, init, init
        elif op in (b"sc", b"scn", b"SC", b"SCN"):
            is_fill = op in (b"sc", b"scn")
            cs = st.fcs if is_fill else st.scs
            dirty = st.fcs_dirty if is_fill else st.scs_dirty
            if dirty:  # restore the colour space our injection replaced
                self._inject(first, [cs[1] + (b" cs" if is_fill else b" CS")])
                if is_fill:
                    st.fcs_dirty = False
                else:
                    st.scs_dirty = False
            has_name = any(k == "name" for k, _, _ in operands)
            L = None if has_name else self._lum(cs[0], self._nums(operands))
            if is_fill:
                st.fill = st.afill = L
            else:
                st.stroke = st.astroke = L
        elif op == b"BT":
            self.tm = _IDENT
        elif op == b"Tf":
            st.tf_size = self._nums(operands, 1)[0]
        elif op == b"Tr":
            st.tr = int(self._nums(operands, 1)[0])
        elif op == b"Tm":
            self.tm = tuple(self._nums(operands, 6))
        elif op in _TEXT_SHOW:
            self._text(first, end)
        elif op == b"Do":
            names = [self.data[s:e] for k, s, e in operands if k == "name"]
            if names:
                x = self._xobject_xref(names[-1])
                if x:
                    sub = self.ctx.doc.xref_get_key(x, "Subtype")[1]
                    if sub == "/Form":
                        self.forms.append(x)
                    elif sub == "/Image" and self.ctx.doc.xref_get_key(x, "ImageMask")[1] == "true":
                        ops: List[bytes] = []
                        self._set_fill(first, self._text_target(st.fill, st.ca), ops)
                        if ops:
                            self._inject(first, ops)
                            self.ctx.counts["text"] += 1

    def _text(self, at: int, end: int) -> None:
        st = self.st
        if st.tr in (3, 7):
            return
        ops: List[bytes] = []
        target = None
        if st.tr in (0, 2, 4, 6):
            target = self._text_target(st.fill, st.ca)
            self._set_fill(at, target, ops)
        if st.tr in (1, 2, 5, 6):
            self._set_stroke(at, self._text_target(st.stroke, st.CA), ops)
        after: List[bytes] = []
        if (self.P.bold > 0 and st.tr == 0 and target is not None and target <= 0.35
                and st.ca >= 0.85 and st.lw is not None and st.stroke is not None):
            # faux bold: fill + stroke the glyph outlines with the same dark
            # colour, so thin letters print with more toner.  Text state is
            # restored right after the text-showing operator.
            m = _mat_mul(self.tm, st.ctm)
            size = abs(st.tf_size) * _mat_scale(m)
            scale = _mat_scale(st.ctm)
            if 3.0 <= size <= 40.0 and scale > 0:
                width_dev = min(0.45, max(0.08, self.P.bold * size))
                ops += [b"2 Tr", _fmt(width_dev / scale) + b" w"]
                self._set_stroke(at, target, ops)
                after = [b"0 Tr", _fmt(st.lw) + b" w", _fmt(st.stroke) + b" G"]
                st.astroke = st.stroke
                self.ctx.counts["bold"] += 1
        if ops:
            self._inject(at, ops)
            self.ctx.counts["text"] += 1
        if after:
            self._inject(end, after)

    def _paint(self, op: bytes, start: int, end: int) -> None:
        st = self.st
        idx = self.path_idx
        box = self.path_box
        self.path_idx = None
        if op == b"n" or idx is None:
            return
        w = max(0.0, box[2] - box[0]) if box[2] >= box[0] else 0.0
        h = max(0.0, box[3] - box[1]) if box[3] >= box[1] else 0.0
        ops: List[bytes] = []
        restore_lw = None
        if op in _FILL_PAINT:
            before = len(ops)
            self._set_fill(0, self._fill_target(st.fill, w, h), ops)
            if len(ops) > before:
                self.ctx.counts["fills"] += 1
        if op in _STROKE_PAINT:
            before = len(ops)
            self._set_stroke(0, self._line_target(st.stroke), ops)
            if len(ops) > before:
                self.ctx.counts["lines"] += 1
            if self.P.min_line_pt > 0 and st.lw is not None:
                scale = _mat_scale(st.ctm)
                if st.lw * scale < self.P.min_line_pt and (w > 0.5 or h > 0.5):
                    restore_lw = st.lw
                    ops.append(_fmt(self.P.min_line_pt / scale) + b" w")
                    self.ctx.counts["widths"] += 1
        if ops:
            if self.path_state_ops:
                # the producer already sets state inside the path: our values
                # must come after its own, i.e. right before the paint operator
                self._inject(start, ops)
            else:
                self.out.insert(idx, b"\n" + b" ".join(ops) + b"\n")
                self.changed = True
        if restore_lw is not None:
            self._inject(end, [_fmt(restore_lw) + b" w"])


def _text_background_profile(page) -> Dict[int, List[int]]:
    """For every text colour (luminance bin) count how often it sits on a
    dark vs. a light background, so light text on dark bars is never darkened."""
    bins: Dict[int, List[int]] = {}
    try:
        spans = page.get_texttrace()
    except Exception:
        return bins
    if not spans:
        return bins
    try:
        pix = page.get_pixmap(dpi=48, colorspace=fitz.csGRAY, annots=False)
    except Exception:
        return bins
    img = _pix_to_array(pix)
    sx = pix.w / max(page.rect.width, 1)
    sy = pix.h / max(page.rect.height, 1)
    rot = page.rotation_matrix if page.rotation else None
    for sp in spans:
        if sp.get("type") == 3 or sp.get("opacity", 1) == 0:
            continue
        col = sp.get("color") or (0,)
        if len(col) == 1:
            L = col[0]
        elif len(col) == 3:
            L = _lum_rgb(*col)
        elif len(col) == 4:
            L = _lum_cmyk(*col)
        else:
            continue
        r = fitz.Rect(sp["bbox"])
        if rot is not None:
            r = r * rot
        hgt = max(r.height, 2)
        x0 = int((r.x0 - 0.3 * hgt) * sx)
        x1 = int(math.ceil((r.x1 + 0.3 * hgt) * sx))
        y0 = int((r.y0 - 0.3 * hgt) * sy)
        y1 = int(math.ceil((r.y1 + 0.3 * hgt) * sy))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(img.shape[1], max(x1, x0 + 1)), min(img.shape[0], max(y1, y0 + 1))
        reg = img[y0:y1, x0:x1]
        if reg.size == 0:
            continue
        bgL = float(np.median(reg)) / 255.0
        dark_bg = bgL < 0.6 and bgL < L - 0.2
        b = bins.setdefault(int(round(L * 40)), [0, 0])
        b[0 if dark_bg else 1] += max(1, len(sp.get("chars", ())))
    return bins


def _rewrite_vectors(doc, page, P: Params, done_forms: set, warnings: List[str]) -> Dict[str, int]:
    ctx = _VectorContext(doc, P, _text_background_profile(page))
    data = page.read_contents()
    if data:
        rw = _StreamRewriter(data, page.xref, ctx, known_state=True)
        new = rw.run()
        forms = list(rw.forms)
        if new is not None:
            x = doc.get_new_xref()
            doc.update_object(x, "<<>>")
            doc.update_stream(x, new)
            page.set_contents(x)
    else:
        forms = []
    # annotation appearance streams (e.g. filled-in form fields, stamps)
    try:
        for annot in page.annots() or []:
            t, v = doc.xref_get_key(annot.xref, "AP/N")
            if t == "xref":
                forms.append(int(v.split()[0]))
    except Exception:
        pass
    # nested form XObjects (processed once per document)
    guard = 0
    while forms and guard < 5000:
        guard += 1
        fx = forms.pop()
        if fx in done_forms:
            continue
        done_forms.add(fx)
        try:
            if not doc.xref_is_stream(fx):
                continue
            mt, mv = doc.xref_get_key(fx, "Matrix")
            matrix = _IDENT
            if mt == "array":
                try:
                    vals = [float(t) for t in mv.strip("[]").split()]
                    if len(vals) == 6:
                        matrix = tuple(vals)
                except ValueError:
                    pass
            rw = _StreamRewriter(doc.xref_stream(fx), fx, ctx, known_state=False, matrix=matrix)
            new = rw.run()
            forms.extend(rw.forms)
            if new is not None:
                doc.update_stream(fx, new)
        except Exception as exc:  # leave this form untouched
            warnings.append(f"form xobject {fx} left unchanged ({exc.__class__.__name__})")
    return ctx.counts


# ---------------------------------------------------------------------------
# Embedded images (digital / mixed pages)
# ---------------------------------------------------------------------------

def _write_image_stream(doc, xref: int, arr: np.ndarray, P: Params, prefer_jpeg: bool) -> None:
    """Replace the pixels of image `xref` with gray (or 1-bit) data, keeping
    any soft mask and the object number (so every page using it is updated)."""
    h, w = arr.shape
    if P.mono:
        bits = np.packbits(arr > 127, axis=1)
        data = zlib.compress(bits.tobytes(), 6)
        filt, bpc = "/FlateDecode", 1
    elif prefer_jpeg:
        ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, P.jpeg_quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        data, filt, bpc = buf.tobytes(), "/DCTDecode", 8
    else:
        data, filt, bpc = zlib.compress(arr.tobytes(), 6), "/FlateDecode", 8
    doc.update_stream(xref, data, compress=False)
    doc.xref_set_key(xref, "Filter", filt)
    for key in ("DecodeParms", "Decode", "Mask"):
        if doc.xref_get_key(xref, key)[0] != "null":
            if key == "Mask" and doc.xref_get_key(xref, key)[0] == "xref":
                continue  # explicit stencil mask stays valid
            doc.xref_set_key(xref, key, "null")
    doc.xref_set_key(xref, "ColorSpace", "/DeviceGray")
    doc.xref_set_key(xref, "BitsPerComponent", str(bpc))
    doc.xref_set_key(xref, "Width", str(w))
    doc.xref_set_key(xref, "Height", str(h))


def _is_photograph(gray: np.ndarray, dpi: float) -> bool:
    """True when an embedded image is predominantly continuous-tone."""
    a_dpi = min(dpi, 100.0)
    f = a_dpi / dpi
    sm = _resize(gray, (gray.shape[1] * f, gray.shape[0] * f), cv2.INTER_AREA)
    paper = float(np.percentile(sm, 95))
    gx = cv2.Sobel(sm, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(sm, cv2.CV_32F, 0, 1, ksize=3)
    smooth = cv2.magnitude(gx, gy) < 110
    nonpaper = (sm > 12) & (sm < min(238.0, paper - 10))
    vals = sm[smooth & nonpaper]
    if vals.size < 0.4 * sm.size:
        return False
    p10, p90 = np.percentile(vals, [10, 90])
    return (p90 - p10) >= 24


def _enhance_page_images(doc, page, P: Params, done: set, page_class: str,
                         warnings: List[str]) -> int:
    count = 0
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return 0
    page_area = max(1.0, abs(page.rect))
    by_xref: Dict[int, Dict[str, float]] = {}
    for info in infos:
        x = info.get("xref", 0)
        if not x:
            continue
        t = info.get("transform") or (1, 0, 0, 1, 0, 0)
        pw = math.hypot(t[0], t[1]) / 72.0
        ph = math.hypot(t[2], t[3]) / 72.0
        dpi = max(info["width"] / max(pw, 1e-3), info["height"] / max(ph, 1e-3))
        cov = abs(fitz.Rect(info["bbox"]) & page.rect) / page_area
        e = by_xref.setdefault(x, {"dpi": 0.0, "cov": 0.0})
        e["dpi"] = max(e["dpi"], dpi)
        e["cov"] = max(e["cov"], cov)
    for xref, e in by_xref.items():
        if xref in done:
            continue
        done.add(xref)
        try:
            if doc.xref_get_key(xref, "ImageMask")[1] == "true":
                continue
            bpc = doc.xref_get_key(xref, "BitsPerComponent")[1]
            if bpc == "1":
                continue  # bilevel already has maximum contrast
            pix = fitz.Pixmap(doc, xref)
            if pix.w < 48 or pix.h < 48:
                continue
            if pix.alpha:
                pix = fitz.Pixmap(pix, 0)
            if pix.n not in (1, 3):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            arr = _pix_to_array(pix)
            rgb_small = None
            if arr.ndim == 3:
                gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
                f = min(1.0, 150.0 / max(e["dpi"], 1.0))
                rgb_small = _resize(arr, (arr.shape[1] * f, arr.shape[0] * f), cv2.INTER_AREA)
            else:
                gray = arr
            del arr, pix
            dpi = float(np.clip(e["dpi"], 50.0, 2400.0))
            role = "background" if (page_class == "mixed" and e["cov"] >= 0.5) else "image"
            if P.preserve_photos and _is_photograph(gray, dpi):
                enhanced = _photo_enhance(gray, dpi, P)
                if P.mono:
                    enhanced = np.array(Image.fromarray(enhanced).convert("1"), dtype=np.uint8) * 255
            else:
                enhanced = enhance_raster(gray, dpi, P, rgb_small, role=role)
            filt = doc.xref_get_key(xref, "Filter")[1]
            prefer_jpeg = ("DCT" in filt or "JPX" in filt) and P.compression == "jpeg"
            _write_image_stream(doc, xref, enhanced, P, prefer_jpeg)
            count += 1
        except Exception as exc:
            warnings.append(f"image xref {xref} left unchanged ({exc.__class__.__name__}: {exc})")
    return count


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

def classify_page(page) -> Tuple[str, Dict[str, float]]:
    """Return ("digital" | "scanned" | "mixed", metrics)."""
    rect = page.rect
    area = max(1.0, abs(rect))
    try:
        log = page.get_bboxlog()
    except Exception:
        log = []
    images: List[Tuple[int, fitz.Rect]] = []
    texts: List[Tuple[int, fitz.Rect]] = []
    hidden_text = 0
    for i, (kind, r) in enumerate(log):
        r = fitz.Rect(r)
        if kind in ("fill-image", "fill-imgmask"):
            images.append((i, r & rect))
        elif kind in ("fill-text", "stroke-text"):
            texts.append((i, r))
        elif kind == "ignore-text":
            hidden_text += 1
    grid = np.zeros((120, 120), dtype=bool)
    for _, r in images:
        if r.is_empty:
            continue
        x0 = int((r.x0 - rect.x0) / rect.width * 120)
        x1 = int(math.ceil((r.x1 - rect.x0) / rect.width * 120))
        y0 = int((r.y0 - rect.y0) / rect.height * 120)
        y1 = int(math.ceil((r.y1 - rect.y0) / rect.height * 120))
        grid[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    coverage = float(grid.mean())
    vis_text = 0.0
    for ti, tr in texts:
        c = tr.tl + (tr.br - tr.tl) * 0.5
        if any(ii > ti and ir.contains(c) for ii, ir in images):
            continue  # text painted under a later image is not visible
        vis_text += abs(tr & rect)
    text_frac = vis_text / area
    metrics = {"image_coverage": round(coverage, 3), "visible_text": round(text_frac, 4),
               "invisible_text_items": hidden_text}
    if coverage >= 0.45 and text_frac < 0.015:
        return "scanned", metrics
    if coverage < 0.08:
        return "digital", metrics
    return "mixed", metrics


# ---------------------------------------------------------------------------
# Raster page assembly
# ---------------------------------------------------------------------------

def _encode_page_image(doc, arr: np.ndarray, P: Params) -> Tuple[int, int, int, str]:
    """Create an image XObject for a processed page raster. Returns
    (xref, width, height, filter)."""
    h, w = arr.shape
    if P.mono:
        data = zlib.compress(np.packbits(arr > 127, axis=1).tobytes(), 6)
        filt, bpc = "/FlateDecode", 1
    elif P.compression == "jpeg":
        ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, P.jpeg_quality,
                                             cv2.IMWRITE_JPEG_OPTIMIZE, 1])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        data, filt, bpc = buf.tobytes(), "/DCTDecode", 8
    else:
        data, filt, bpc = zlib.compress(arr.tobytes(), 6), "/FlateDecode", 8
    x = doc.get_new_xref()
    doc.update_object(x, f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}"
                         f"/ColorSpace/DeviceGray/BitsPerComponent {bpc}/Interpolate false>>")
    doc.update_stream(x, data, new=True, compress=False)
    doc.xref_set_key(x, "Filter", filt)
    return x, w, h, filt


def _drop_image_resources(doc, page) -> None:
    """Remove image XObjects from the page resources so replaced pictures are
    not kept in the file (forms and fonts stay, the text layer needs them)."""
    t, v = doc.xref_get_key(page.xref, "Resources/XObject")
    if t == "null":
        return
    for name in re.findall(r"/([^\s/<>\[\]()]+)\s+\d+\s+0\s+R", v if t == "dict" else
                           doc.xref_object(int(v.split()[0])) if t == "xref" else ""):
        x = doc.xref_get_key(page.xref, f"Resources/XObject/{name}")
        if x[0] == "xref" and doc.xref_get_key(int(x[1].split()[0]), "Subtype")[1] == "/Image":
            doc.xref_set_key(page.xref, f"Resources/XObject/{name}", "null")


def _rasterize_page(doc, page, P: Params, page_info: Dict[str, Any]) -> None:
    rect = page.rect
    w_in, h_in = rect.width / 72.0, rect.height / 72.0
    dpi = int(P.dpi)
    max_px = P.max_mp * 1e6
    if w_in * h_in * dpi * dpi > max_px:
        dpi = int(math.floor(math.sqrt(max_px / (w_in * h_in))))
        page_info["note"] = f"page too large for {P.dpi} dpi, used {dpi:.0f} dpi"
    has_text = bool(page.get_texttrace())

    rgb_small = None
    if P.preserve_ink:
        cp = page.get_pixmap(dpi=min(dpi, 100), colorspace=fitz.csRGB, annots=False)
        rgb_small = _pix_to_array(cp)
        del cp
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    gray = _pix_to_array(pix)
    real_dpi = pix.w / w_in
    del pix
    stats: Dict[str, Any] = {}
    enhanced = enhance_raster(gray, real_dpi, P, rgb_small, role="page", stats=stats)
    del gray
    page_info.update({k: v for k, v in stats.items() if v})
    xref, iw, ih, filt = _encode_page_image(doc, enhanced, P)
    del enhanced

    # --- mutate the page (all heavy work is done) -----------------------------
    if page.rotation:
        page.remove_rotation()
    keep_text = has_text and hasattr(fitz, "PDF_REDACT_TEXT_NONE")
    if has_text and not keep_text:
        page_info["note"] = "PyMuPDF too old to keep the OCR text layer (upgrade PyMuPDF)"
    if keep_text:
        # drop the old pictures and vector art, keep the (OCR) text layer so the
        # page stays searchable; the new opaque raster is drawn on top of it.
        page.add_redact_annot(page.rect, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
        _drop_image_resources(doc, page)
    else:
        empty = doc.get_new_xref()
        doc.update_object(empty, "<<>>")
        doc.update_stream(empty, b" ")
        page.set_contents(empty)
        doc.xref_set_key(page.xref, "Resources", "<<>>")  # old pictures/fonts no longer used
    page.insert_image(page.rect, xref=xref, keep_proportion=False, overlay=True)
    page_info["raster"] = {"width": iw, "height": ih, "dpi": round(real_dpi, 1), "filter": filt}


# ---------------------------------------------------------------------------
# Verification & preview
# ---------------------------------------------------------------------------

def verify_output(out_path: Path, expected_sizes: List[Tuple[float, float]],
                  raster_pages: Dict[int, float]) -> List[str]:
    """Return a list of problems (empty list = verified OK)."""
    problems: List[str] = []
    try:
        doc = fitz.open(str(out_path))
    except Exception as exc:
        return [f"output does not open: {exc}"]
    try:
        if doc.page_count != len(expected_sizes):
            problems.append(f"page count {doc.page_count} != original {len(expected_sizes)}")
        for i, page in enumerate(doc):
            if i >= len(expected_sizes):
                break
            ew, eh = expected_sizes[i]
            r = page.rect
            if abs(r.width - ew) > 0.6 or abs(r.height - eh) > 0.6:
                problems.append(f"page {i + 1} size {r.width:.1f}x{r.height:.1f} != "
                                f"original {ew:.1f}x{eh:.1f}")
            if i in raster_pages:
                infos = [im for im in page.get_image_info() if im.get("width")]
                if not infos:
                    problems.append(f"page {i + 1}: raster image missing")
                else:
                    im = max(infos, key=lambda d: d["width"] * d["height"])
                    eff = im["width"] / max(page.rect.width / 72.0, 1e-6)
                    if abs(eff - raster_pages[i]) > max(2.0, 0.01 * raster_pages[i]):
                        problems.append(f"page {i + 1}: raster {eff:.0f} dpi != expected "
                                        f"{raster_pages[i]:.0f}")
        # render a page as smoke test that content streams parse
        if doc.page_count:
            doc[0].get_pixmap(dpi=20)
    finally:
        doc.close()
    if pypdf is not None:
        try:
            rdr = pypdf.PdfReader(str(out_path))
            if len(rdr.pages) != len(expected_sizes):
                problems.append(f"pypdf sees {len(rdr.pages)} pages")
        except Exception as exc:
            problems.append(f"pypdf could not parse output: {exc}")
    return problems


def make_preview(original: bytes, out_path: Path, preview_path: Path, page_no: int = 0,
                 dpi: int = 110) -> None:
    """Side-by-side ORIGINAL | OPTIMIZED PNG of one page."""
    a = fitz.open(stream=original, filetype="pdf")
    b = fitz.open(str(out_path))
    try:
        pa = _pix_to_array(a[page_no].get_pixmap(dpi=dpi, colorspace=fitz.csRGB))
        pb = _pix_to_array(b[page_no].get_pixmap(dpi=dpi, colorspace=fitz.csRGB))
    finally:
        a.close()
        b.close()
    h = max(pa.shape[0], pb.shape[0])
    bar = 44
    gap = 24
    canvas = np.full((h + bar, pa.shape[1] + pb.shape[1] + gap, 3), 255, np.uint8)
    canvas[bar:bar + pa.shape[0], :pa.shape[1]] = pa
    canvas[bar:bar + pb.shape[0], pa.shape[1] + gap:] = pb
    canvas[:, pa.shape[1]:pa.shape[1] + gap] = 200
    for text, x in (("ORIGINAL", 10), ("OPTIMIZED", pa.shape[1] + gap + 10)):
        cv2.putText(canvas, text, (x, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 160), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    if ok:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(buf.tobytes())


# ---------------------------------------------------------------------------
# Document processing (runs inside worker processes)
# ---------------------------------------------------------------------------

def process_document(job: Dict[str, Any]) -> Dict[str, Any]:
    """Process one PDF.  `job` keys: src, dst, rel, config, preview (path or None),
    threads.  Never raises - all problems are reported in the result dict."""
    t0 = time.time()
    src, dst = Path(job["src"]), Path(job["dst"])
    res: Dict[str, Any] = {
        "file": job["rel"], "status": "FAILED", "pages": 0, "dpi": "-", "seconds": 0.0,
        "input_size": 0, "output_size": 0, "page_types": {}, "warnings": [], "error": "",
        "verified": False,
    }
    warnings: List[str] = res["warnings"]
    tmp: Optional[Path] = None
    try:
        if job.get("threads"):
            cv2.setNumThreads(int(job["threads"]))
        P = Params(job["config"])
        sha_before = file_sha256(src)
        original = src.read_bytes()
        res["input_size"] = len(original)
        if not original.lstrip()[:5].startswith(b"%PDF") and b"%PDF" not in original[:1024]:
            raise _DocError("not a PDF file (missing %PDF header)")
        try:
            doc = fitz.open(stream=original, filetype="pdf")
        except Exception as exc:
            raise _DocError(f"invalid or corrupt PDF ({exc})")
        if doc.needs_pass and not doc.authenticate(""):
            doc.close()
            res["status"] = "SKIPPED"
            res["error"] = "password protected - remove the password and run again"
            return res
        if doc.page_count == 0:
            raise _DocError("PDF has no pages")
        reference = fitz.open(stream=original, filetype="pdf")
        if reference.needs_pass:
            reference.authenticate("")
        res["pages"] = doc.page_count

        sizes = [(p.rect.width, p.rect.height) for p in doc]
        raster_pages: Dict[int, float] = {}
        done_images: set = set()
        done_forms: set = set()
        types: Dict[str, int] = {}
        details: List[Dict[str, Any]] = []
        for i in range(doc.page_count):
            page = doc[i]
            info: Dict[str, Any] = {"page": i + 1}
            try:
                kind, metrics = classify_page(page)
            except Exception as exc:
                kind, metrics = "digital", {}
                warnings.append(f"page {i + 1}: classification failed ({exc}), treated as digital")
            info["type"] = kind
            info.update(metrics)
            types[kind] = types.get(kind, 0) + 1
            raster = P.force_raster or kind == "scanned" or (kind == "mixed" and P.mixed_mode == "rasterize")
            try:
                if raster:
                    _rasterize_page(doc, page, P, info)
                    raster_pages[i] = info["raster"]["dpi"]
                else:
                    if P.enhance_images:
                        info["images_enhanced"] = _enhance_page_images(doc, page, P, done_images, kind, warnings)
                    try:
                        page.recolor(1)
                    except Exception as exc:
                        warnings.append(f"page {i + 1}: grayscale conversion skipped ({exc})")
                    info["vector_changes"] = _rewrite_vectors(doc, page, P, done_forms, warnings)
            except Exception as exc:
                warnings.append(f"page {i + 1}: processing failed, original page kept "
                                f"({exc.__class__.__name__}: {exc})")
                info["error"] = str(exc)
                raster_pages.pop(i, None)
                try:
                    doc.delete_page(i)
                    doc.insert_pdf(reference, from_page=i, to_page=i, start_at=i)
                except Exception as exc2:
                    raise _DocError(f"page {i + 1} could not be restored: {exc2}")
            details.append(info)
        res["page_types"] = types
        res["page_details"] = details
        reference.close()

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.stem}.tmp-{os.getpid()}.pdf")
        doc.save(str(tmp), garbage=3, deflate=True)
        doc.close()

        if job["config"].get("verify_output", True):
            problems = verify_output(tmp, sizes, raster_pages)
            if problems:
                raise _DocError("verification failed: " + "; ".join(problems))
            res["verified"] = True
        os.replace(tmp, dst)
        tmp = None
        res["output_size"] = dst.stat().st_size
        if file_sha256(src) != sha_before:
            warnings.append("input file changed on disk during processing (not by this tool)")
        if raster_pages:
            vals = sorted(set(int(round(v)) for v in raster_pages.values()))
            res["dpi"] = "/".join(str(v) for v in vals)
        else:
            res["dpi"] = "vector"
        if job.get("preview"):
            try:
                make_preview(original, dst, Path(job["preview"]))
            except Exception as exc:
                warnings.append(f"preview not created ({exc})")
        res["status"] = "SUCCESS"
    except _DocError as exc:
        res["error"] = str(exc)
    except MemoryError:
        res["error"] = "out of memory - lower target_dpi or set workers to 1"
    except Exception as exc:
        res["error"] = f"{exc.__class__.__name__}: {exc}"
        res["traceback"] = traceback.format_exc()
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        res["seconds"] = round(time.time() - t0, 2)
    return res


class _DocError(Exception):
    """Document-level problem with a user-friendly message."""
