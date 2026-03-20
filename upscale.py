#!/usr/bin/env python3
"""
Anime Upscaling Pipeline — Real-ESRGAN + FFmpeg

Usage:
    ./upscale.py <input_dir|file> <output_dir>
"""
import logging
import os
import sys
from pathlib import Path

from animescale.config import Config
from animescale.pipeline import Pipeline


def setup_logging(log_file: Path) -> None:
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    handler_file = logging.FileHandler(log_file)
    handler_file.setFormatter(fmt)
    handler_console = logging.StreamHandler()
    handler_console.setFormatter(fmt)
    logging.getLogger("animescale").setLevel(logging.INFO)
    logging.getLogger("animescale").addHandler(handler_file)
    logging.getLogger("animescale").addHandler(handler_console)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: upscale.py <input_dir|file> <output_dir>")
        print()
        print("  Upscales anime using Real-ESRGAN.")
        print("  Auto-detects interlacing and duplicate frames.")
        print("  Queries Jellyfin for intro/outro segments (fast-scale instead of AI).")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not input_path.exists():
        sys.exit(f"Input not found: {input_path}")

    script_dir = Path(__file__).parent
    log_file = script_dir / "upscale.log"
    lock_file = script_dir / ".upscale.lock"

    setup_logging(log_file)
    log = logging.getLogger("animescale")

    # Lock file — prevent concurrent runs
    if lock_file.exists():
        old_pid = lock_file.read_text().strip()
        if old_pid:
            try:
                os.kill(int(old_pid), 0)
                log.info(f"Already running (PID {old_pid}). Remove {lock_file} if stale.")
                sys.exit(1)
            except ProcessLookupError:
                pass
        lock_file.unlink(missing_ok=True)

    lock_file.write_text(str(os.getpid()))
    try:
        cfg = Config()
        pipeline = Pipeline(cfg, output_dir, log_file, lock_file)
        pipeline.run(input_path)
    finally:
        lock_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
