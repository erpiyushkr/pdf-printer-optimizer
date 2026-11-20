"""
Interactive start-up menu for PDF Printer Optimizer.

Shown when the tool is started from a terminal (or by double-clicking
run.bat / run.command). It asks for the darkness preset, scan resolution,
colour mode and page handling, so nobody has to edit config.json by hand.
Pressing Enter on every question keeps the current settings.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LINE = "=" * 64
THIN = "-" * 64

# (value, label, explanation)
Option = Tuple[Any, str, str]

DARKNESS: List[Option] = [
    ("normal", "Normal",
     "Balanced. Clean background, text moderately darker."),
    ("strong", "Strong",
     "Grey text and thin strokes noticeably darker."),
    ("maximum_readable", "Maximum readable",
     "All text near black, faint ink strongly boosted."),
    ("low_toner", "Low toner (darkest)",
     "Everything solid black and slightly bolder. Best when the\n"
     "        cartridge is running low and light content disappears."),
]

DPI: List[Option] = [
    (300, "300 DPI", "Fastest, smallest files. Fine for simple text."),
    (400, "400 DPI", "Fast, small files."),
    (600, "600 DPI", "Recommended balance: 2-3x faster than 800, almost the same print."),
    (800, "800 DPI", "Very sharp edges. About 2.5-3.5 MB per scanned page."),
    (1200, "1200 DPI", "Maximum. Slow and large; needs plenty of memory."),
]

MODE: List[Option] = [
    ("grayscale", "Grayscale",
     "Keeps photos and grey shading. Recommended for most documents."),
    ("monochrome", "Black & white only",
     "Pure black and white, no grey at all. Darkest on a weak\n"
     "        cartridge and much smaller files. Photos become dotted."),
]

PAGES: List[Option] = [
    ("auto", "Automatic (recommended)",
     "Digital pages keep sharp, selectable text; scans are enhanced."),
    ("rasterize", "Treat every page as a scan",
     "Most uniform darkening, but text is no longer selectable."),
]


def _ask(title: str, options: List[Option], current: Any) -> Any:
    """Show a numbered list and return the chosen value (Enter = current)."""
    values = [o[0] for o in options]
    default_idx = values.index(current) if current in values else 0
    print()
    print(title)
    print(THIN)
    for i, (_, label, info) in enumerate(options, 1):
        mark = "  <- current" if i - 1 == default_idx else ""
        print(f"  {i}) {label}{mark}")
        print(f"        {info}")
    while True:
        raw = input(f"Choose 1-{len(options)} [Enter = {default_idx + 1}]: ").strip()
        if raw == "":
            return values[default_idx]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return values[int(raw) - 1]
        print(f"  Please type a number from 1 to {len(options)}, or just press Enter.")


def _yes_no(question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{question} [{hint}]: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please type y or n.")


def _label(options: List[Option], value: Any) -> str:
    for v, label, _ in options:
        if v == value:
            return label
    return str(value)


def _save_defaults(config_path: Path, choices: Dict[str, Any]) -> bool:
    """Write the chosen values into config.json, keeping its layout and comments."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for key, value in choices.items():
        literal = "true" if value is True else "false" if value is False else (
            str(value) if isinstance(value, int) else f'"{value}"')
        pattern = re.compile(r'("%s"\s*:\s*)("[^"]*"|-?\d+(?:\.\d+)?|true|false)' % re.escape(key))
        text, n = pattern.subn(lambda m: m.group(1) + literal, text, count=1)
        if n == 0:
            return False
    try:
        config_path.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def run_menu(cfg: Dict[str, Any], config_path: Path, n_pdfs: int, input_dir: Path) -> Optional[Dict[str, Any]]:
    """Ask the user for the main settings.

    Returns a dict of config overrides, or None if the user chose to quit.
    """
    print(LINE)
    print("                    PDF PRINTER OPTIMIZER")
    print(LINE)
    print(f"Files waiting in the input folder (PDFs and images): {n_pdfs}")
    print(f"  {input_dir}")
    if n_pdfs == 0:
        print("\nCopy your PDFs or photos (JPG, PNG, TIFF...) into the folder above, then run the tool again.")
        return None
    print("\nAnswer 4 quick questions. Press Enter to keep the current choice.")

    original = dict(cfg)
    current_pages = "rasterize" if cfg.get("force_rasterize_all_pages") else "auto"
    while True:
        darkness = _ask("1. How dark should the content be?", DARKNESS, cfg.get("darkness_level"))
        dpi = _ask("2. Resolution for scanned pages", DPI, int(cfg.get("target_dpi", 800)))
        mode = _ask("3. Colour mode", MODE, cfg.get("output_mode"))
        pages = _ask("4. Page handling", PAGES, current_pages)

        print()
        print(LINE)
        print("Your choices")
        print(THIN)
        print(f"  Darkness   : {_label(DARKNESS, darkness)}")
        print(f"  Resolution : {_label(DPI, dpi)}")
        print(f"  Colour     : {_label(MODE, mode)}")
        print(f"  Pages      : {_label(PAGES, pages)}")
        print(LINE)
        print("  1) Start processing")
        print("  2) Change the choices")
        print("  3) Quit without processing")
        while True:
            raw = input("Choose 1-3 [Enter = 1]: ").strip()
            if raw in ("", "1", "2", "3"):
                break
            print("  Please type 1, 2 or 3.")
        if raw == "3":
            return None
        if raw == "2":
            cfg = dict(cfg, darkness_level=darkness, target_dpi=dpi, output_mode=mode,
                       force_rasterize_all_pages=(pages == "rasterize"))
            current_pages = pages
            continue
        break

    choices = {
        "darkness_level": darkness,
        "target_dpi": dpi,
        "output_mode": mode,
        "force_rasterize_all_pages": pages == "rasterize",
    }
    changed = any(original.get(k) != v for k, v in choices.items())
    if changed and _yes_no("Remember these choices for next time?", False):
        if _save_defaults(config_path, choices):
            print("  Saved to config.json.")
        else:
            print("  Could not update config.json; the choices apply to this run only.")
    print()
    return choices
