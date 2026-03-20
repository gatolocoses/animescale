"""CLI entry point for animescale."""
import argparse
import logging
import os
import sys
from pathlib import Path

from .config import Config
from .pipeline import Pipeline


def setup_logging(log_file: Path) -> None:
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    logger = logging.getLogger("animescale")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.FileHandler(log_file))
    logger.addHandler(logging.StreamHandler())
    for handler in logger.handlers:
        handler.setFormatter(fmt)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="animescale",
        description="AI anime upscaling pipeline using Real-ESRGAN + FFmpeg.",
    )
    parser.add_argument("input", help="Input file or directory")
    parser.add_argument("output", help="Output directory")
    parser.add_argument("--scale", type=int, choices=[2, 4], default=2,
                        help="Upscale factor (default: 2)")
    parser.add_argument("--model", default="realesr-animevideov3",
                        help="Real-ESRGAN model name")
    parser.add_argument("--codec", choices=["libx264", "libx265", "libsvtav1"],
                        default="libx265", help="Video codec (default: libx265)")
    parser.add_argument("--crf", type=int, default=14,
                        help="CRF quality value, lower = better (default: 14)")
    parser.add_argument("--preset", default="medium",
                        help="Encoder preset (default: medium)")
    parser.add_argument("--gpu", default="0",
                        help="Vulkan GPU device index (default: 0)")
    parser.add_argument("--dup-threshold", type=float, default=1.0,
                        help="Duplicate detection threshold, lower = stricter (default: 1.0)")
    parser.add_argument("--jellyfin-url", default="http://localhost:8096",
                        help="Jellyfin server URL")
    parser.add_argument("--jellyfin-key", default="",
                        help="Jellyfin API key (enables intro/credits fast-scaling)")

    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if not input_path.exists():
        sys.exit(f"Input not found: {input_path}")

    xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    data_dir = xdg_data / "animescale"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file = data_dir / "upscale.log"
    lock_file = data_dir / ".upscale.lock"

    setup_logging(log_file)
    log = logging.getLogger("animescale")

    if lock_file.exists():
        old_pid = lock_file.read_text().strip()
        if old_pid:
            try:
                os.kill(int(old_pid), 0)
                log.info(f"Already running (PID {old_pid}). Remove {lock_file} if stale.")
                sys.exit(1)
            except (ProcessLookupError, ValueError):
                pass
        lock_file.unlink(missing_ok=True)

    lock_file.write_text(str(os.getpid()))
    try:
        cfg = Config(
            scale=args.scale,
            model=args.model,
            codec=args.codec,
            crf=args.crf,
            preset=args.preset,
            gpu=args.gpu,
            dup_threshold=args.dup_threshold,
            jellyfin_url=args.jellyfin_url,
            jellyfin_api_key=args.jellyfin_key,
        )
        pipeline = Pipeline(cfg, output_dir, log_file, lock_file)
        pipeline.run(input_path)
    finally:
        lock_file.unlink(missing_ok=True)
