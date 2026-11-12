#!/usr/bin/env python3
"""
PDF Printer Optimizer - batch entry point.

    python main.py                      # asks for darkness, DPI and mode, then runs
    python main.py --no-menu            # run straight away with config.json settings
    python main.py --preset strong      # override settings from config.json
    python main.py --dpi 600 --workers 1

All folders are resolved relative to this file, so the tool works the same
when started by double-clicking run.bat / run.command.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent
LINE = "=" * 64

try:
    import processor
    from processor import ConfigError, human_size, load_config, validate_config
except ImportError as exc:  # pragma: no cover - friendly message for missing deps
    print(f"Missing dependency: {exc}\nRun:  pip install -r requirements.txt")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Darken and optimise PDFs for laser printing.")
    ap.add_argument("--config", default=str(BASE_DIR / "config.json"), help="path to config.json")
    ap.add_argument("--input", help="input folder (overrides config)")
    ap.add_argument("--output", help="output folder (overrides config)")
    ap.add_argument("--dpi", type=int, help="target DPI: 300, 400, 600, 800 or 1200")
    ap.add_argument("--preset", help="normal | strong | maximum_readable | low_toner")
    ap.add_argument("--mode", help="grayscale | monochrome")
    ap.add_argument("--workers", help="number of PDFs processed in parallel, or 'auto'")
    ap.add_argument("--no-preview", action="store_true", help="do not write preview PNGs")
    ap.add_argument("--rasterize-all", action="store_true", help="treat every page like a scan")
    ap.add_argument("--menu", action="store_true", help="always show the settings menu")
    ap.add_argument("--no-menu", action="store_true",
                    help="skip the settings menu and use config.json / command-line values")
    return ap.parse_args()


def resolve(folder: str) -> Path:
    p = Path(os.path.expanduser(folder))
    return p if p.is_absolute() else (BASE_DIR / p)


def total_memory_bytes() -> int:
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return int(st.ullTotalPhys)
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return 8 * 1024 ** 3


def choose_workers(setting: Any, n_files: int, dpi: int) -> int:
    if n_files <= 1:
        return 1
    if setting != "auto":
        return max(1, min(int(setting), n_files))
    cpus = os.cpu_count() or 2
    # ~18 bytes per pixel of an A4 page at the target DPI (several work buffers)
    per_worker = 8.27 * 11.69 * dpi * dpi * 18 + 300 * 1024 ** 2
    by_mem = int(total_memory_bytes() * 0.55 // per_worker)
    return max(1, min(max(1, cpus // 2), by_mem, n_files, 6))


def find_pdfs(input_dir: Path, output_dir: Path, recursive: bool) -> List[Path]:
    pattern = "**/*" if recursive else "*"
    out_resolved = output_dir.resolve()
    found = []
    for p in input_dir.glob(pattern):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        rel = p.relative_to(input_dir)
        if any(part.startswith(".") or part.startswith("~$") for part in rel.parts):
            continue  # hidden / temp / macOS "._" resource files
        try:
            p.resolve().relative_to(out_resolved)
            continue  # never re-process our own output if it lives inside input
        except ValueError:
            pass
        found.append(p)
    return sorted(found, key=lambda x: str(x.relative_to(input_dir)).lower())


def setup_logging(log_dir: Path, enabled: bool) -> logging.Logger:
    log = logging.getLogger("pdf_optimizer")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    if enabled:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "processing.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
    else:
        log.addHandler(logging.NullHandler())
    return log


def clean_stale_temp(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for p in output_dir.rglob(".*.tmp-*.pdf"):
        try:
            p.unlink()
        except OSError:
            pass


def short(name: str, width: int) -> str:
    return name if len(name) <= width else name[: width - 3] + "..."


def print_summary(results: List[Dict[str, Any]], total: int, elapsed: float, output_dir: Path) -> None:
    ok = sum(r["status"] == "SUCCESS" for r in results)
    failed = sum(r["status"] == "FAILED" for r in results)
    skipped = sum(r["status"] == "SKIPPED" for r in results)
    name_w = max(20, min(44, max((len(r["file"]) for r in results), default=20) + 2))
    print()
    print(LINE)
    print("                    PDF PRINTER OPTIMIZER")
    print(LINE)
    print()
    print(f"Total PDFs found:        {total}")
    print(f"Successfully processed:  {ok}")
    print(f"Failed:                  {failed}")
    print(f"Skipped:                 {skipped}")
    print(f"Total time:              {elapsed:.1f} s")
    print()
    if results:
        print(f"{'Filename':<{name_w}}{'Status':<10}{'DPI':<9}{'Pages':>5}{'Time':>9}{'Size':>11}")
        print("-" * (name_w + 44))
        for r in results:
            size = human_size(r["output_size"]) if r["output_size"] else "-"
            print(f"{short(r['file'], name_w - 1):<{name_w}}{r['status']:<10}{str(r['dpi']):<9}"
                  f"{r['pages'] or '-':>5}{r['seconds']:>8.1f}s{size:>11}")
        problems = [r for r in results if r["status"] != "SUCCESS" or r["warnings"]]
        if problems:
            print()
            print("Notes:")
            for r in problems:
                if r["error"]:
                    print(f"  - {r['file']}: {r['status']} - {r['error']}")
                for w in r["warnings"][:3]:
                    print(f"  - {r['file']}: warning - {w}")
                if len(r["warnings"]) > 3:
                    print(f"  - {r['file']}: ... {len(r['warnings']) - 3} more warnings in the log")
    print()
    print(LINE)
    print("Processing completed!")
    print(f"Check the OUTPUT folder: {output_dir}")
    print(LINE)


def log_result(log: logging.Logger, r: Dict[str, Any]) -> None:
    lvl = logging.INFO if r["status"] == "SUCCESS" else (logging.WARNING if r["status"] == "SKIPPED" else logging.ERROR)
    types = ", ".join(f"{k}={v}" for k, v in sorted(r.get("page_types", {}).items())) or "-"
    log.log(lvl, f"{r['status']:<8} {r['file']} | pages={r['pages']} ({types}) | dpi={r['dpi']} | "
                 f"time={r['seconds']:.2f}s | in={human_size(r['input_size'])} | "
                 f"out={human_size(r['output_size']) if r['output_size'] else '-'} | "
                 f"verified={r.get('verified')}")
    if r["error"]:
        log.log(lvl, f"         error: {r['error']}")
    for w in r["warnings"]:
        log.warning(f"         {r['file']}: {w}")
    for d in r.get("page_details", []):
        log.info(f"         page {d}")
    if r.get("traceback"):
        log.error(r["traceback"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    try:
        cfg, cfg_warnings = load_config(Path(args.config))
        if args.input:
            cfg["input_folder"] = args.input
        if args.output:
            cfg["output_folder"] = args.output
        if args.dpi:
            cfg["target_dpi"] = args.dpi
        if args.preset:
            cfg["darkness_level"] = args.preset
        if args.mode:
            cfg["output_mode"] = args.mode
        if args.workers:
            cfg["workers"] = args.workers
        if args.no_preview:
            cfg["generate_preview"] = False
        if args.rasterize_all:
            cfg["force_rasterize_all_pages"] = True
        cfg = validate_config(cfg, cfg_warnings)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        return 2

    input_dir = resolve(cfg["input_folder"])
    output_dir = resolve(cfg["output_folder"])

    # Interactive menu: shown when started from a terminal / launcher without
    # explicit setting flags. Scripts and pipes run straight through.
    scripted = any([args.preset, args.dpi, args.mode, args.rasterize_all])
    interactive = sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()
    if args.menu or (interactive and not scripted and not args.no_menu):
        from menu import run_menu
        n_found = len(find_pdfs(input_dir, output_dir, cfg["include_subfolders"])) if input_dir.exists() else 0
        try:
            choices = run_menu(cfg, Path(args.config), n_found, input_dir)
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled - nothing was processed.")
            return 0
        if choices is None:
            print("Nothing was processed.")
            return 0
        cfg.update(choices)
        try:
            cfg = validate_config(cfg, [])
        except ConfigError as exc:
            print(f"CONFIGURATION ERROR: {exc}")
            return 2

    preview_dir = resolve(cfg["preview_folder"])
    log = setup_logging(resolve(cfg["log_folder"]), cfg["generate_logs"])

    print(LINE)
    print("                    PDF PRINTER OPTIMIZER")
    print(LINE)
    print(f"Input folder : {input_dir}")
    print(f"Output folder: {output_dir}")
    params = processor.Params(cfg)
    print(f"Settings     : {params.describe()}")
    for w in cfg_warnings:
        print(f"Config note  : {w}")

    if input_dir.resolve() == output_dir.resolve():
        print("CONFIGURATION ERROR: input and output folders must be different "
              "(originals are never overwritten).")
        return 2
    if not input_dir.exists():
        input_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nThe input folder did not exist and has been created:\n  {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_stale_temp(output_dir)

    log.info(LINE)
    log.info(f"Run started | python {platform.python_version()} | {platform.platform()} | "
             f"PyMuPDF {processor.fitz.VersionBind}")
    log.info(f"Input={input_dir} Output={output_dir}")
    log.info(f"Settings: {params.describe()}")
    for w in cfg_warnings:
        log.warning(f"Config: {w}")

    pdfs = find_pdfs(input_dir, output_dir, cfg["include_subfolders"])
    if not pdfs:
        print("\nNo PDF files were found in the input folder.")
        print("Copy your PDF files into:\n  " + str(input_dir))
        log.info("No PDF files found - nothing to do.")
        print_summary([], 0, 0.0, output_dir)
        return 0

    results: List[Dict[str, Any]] = []
    jobs: List[Dict[str, Any]] = []
    for src in pdfs:
        rel = src.relative_to(input_dir)
        dst = output_dir / rel
        if dst.exists() and not cfg["overwrite_output"]:
            results.append({"file": str(rel), "status": "SKIPPED", "pages": 0, "dpi": "-", "seconds": 0.0,
                            "input_size": src.stat().st_size, "output_size": dst.stat().st_size,
                            "warnings": [], "error": "output already exists (overwrite_output is false)"})
            continue
        preview = None
        if cfg["generate_preview"]:
            preview = str(preview_dir / rel.with_suffix(".png"))
        jobs.append({"src": str(src), "dst": str(dst), "rel": str(rel), "config": cfg, "preview": preview})

    workers = choose_workers(cfg["workers"], len(jobs), cfg["target_dpi"])
    threads = max(1, (os.cpu_count() or 2) // workers)
    for j in jobs:
        j["threads"] = threads
    print(f"\nFound {len(pdfs)} PDF file(s); processing {len(jobs)} with {workers} parallel worker(s)...\n")
    log.info(f"{len(pdfs)} PDF(s) found, {len(jobs)} to process, workers={workers}")

    t0 = time.time()
    done = 0

    def report(r: Dict[str, Any]) -> None:
        nonlocal done
        done += 1
        results.append(r)
        log_result(log, r)
        extra = f" - {r['error']}" if r["error"] else ""
        print(f"  [{done:>3}/{len(jobs)}] {r['status']:<8} {r['file']}  "
              f"({r['pages']} page{'s' if r['pages'] != 1 else ''}, {r['seconds']:.1f}s){extra}", flush=True)

    try:
        if workers == 1:
            for j in jobs:
                report(processor.process_document(j))
        else:
            ctx = mp.get_context("spawn")
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                futures = {pool.submit(processor.process_document, j): j for j in jobs}
                for fut in as_completed(futures):
                    j = futures[fut]
                    try:
                        r = fut.result()
                    except Exception as exc:  # worker crashed (e.g. killed for memory)
                        r = {"file": j["rel"], "status": "FAILED", "pages": 0, "dpi": "-", "seconds": 0.0,
                             "input_size": 0, "output_size": 0, "warnings": [],
                             "error": f"worker crashed ({exc.__class__.__name__}: {exc}); "
                                      "try workers=1 or a lower target_dpi"}
                    report(r)
    except KeyboardInterrupt:
        print("\nInterrupted by user - partial results below.")
        log.warning("Interrupted by user")
        clean_stale_temp(output_dir)

    order = {str(p.relative_to(input_dir)): i for i, p in enumerate(pdfs)}
    results.sort(key=lambda r: order.get(r["file"], 1 << 30))
    elapsed = time.time() - t0
    print_summary(results, len(pdfs), elapsed, output_dir)
    ok = sum(r["status"] == "SUCCESS" for r in results)
    failed = sum(r["status"] == "FAILED" for r in results)
    log.info(f"Run finished: {ok} succeeded, {failed} failed, "
             f"{sum(r['status'] == 'SKIPPED' for r in results)} skipped in {elapsed:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
