"""CLI entry point for animescale."""
import argparse
import logging
import os
import sys
from pathlib import Path

from .colors import ColorFormatter
from .config import Config
from .pipeline import Pipeline

_PRESETS = {
    "libx264":   ["ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow", "placebo"],
    "libx265":   ["ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow", "placebo"],
    "libsvtav1": [str(n) for n in range(14)],  # SVT-AV1 uses numeric presets 0-13
}


def setup_logging(log_file: Path) -> None:
    plain_fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    color_fmt = ColorFormatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(plain_fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(color_fmt)

    logger = logging.getLogger("animescale")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="animescale",
        description="AI anime upscaling pipeline using Real-ESRGAN + FFmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  animescale input.mkv /output/                    # 2x upscale with defaults\n"
            "  animescale /shows/Aria/ /output/ --scale 4       # 4x upscale a whole season\n"
            "  animescale input.mkv /output/ --crf 18 --preset fast  # faster encode, slightly larger\n"
            "  animescale input.mkv /output/ --jellyfin-key YOUR_KEY # skip AI on intros/credits\n"
        ),
    )
    parser.add_argument("input",  help="Input video file or directory of video files")
    parser.add_argument("output", help="Output directory (created if it doesn't exist)")

    vid = parser.add_argument_group("video quality")
    vid.add_argument("--scale",  type=int, choices=[2, 4], default=2,
                     help="Upscale factor: 2x or 4x (default: 2)")
    vid.add_argument("--model",  default="realesr-animevideov3",
                     metavar="NAME",
                     help="Real-ESRGAN model name (default: realesr-animevideov3; "
                          "for 4x use realesrgan-x4plus-anime)")
    vid.add_argument("--codec",  choices=["libx264", "libx265", "libsvtav1"],
                     default="libx265",
                     help="Output video codec (default: libx265)")
    vid.add_argument("--crf",    type=int, default=14,
                     metavar="0-51",
                     help="Quality: lower = better, larger file. 14 is near-transparent (default: 14)")
    vid.add_argument("--preset", default="medium",
                     help="Encoder speed preset — slower = better compression (default: medium)")

    perf = parser.add_argument_group("performance")
    perf.add_argument("--gpu",           default="0",  metavar="INDEX",
                      help="Vulkan GPU device index (default: 0; use 1 for second GPU)")
    perf.add_argument("--dup-threshold", type=float,   default=1.0, metavar="FLOAT",
                      help="Duplicate frame sensitivity: lower = stricter, "
                           "0.5 catches only obvious dupes, 2.0 is aggressive (default: 1.0)")

    jf = parser.add_argument_group("jellyfin intro skipper")
    jf.add_argument("--jellyfin-url", default="http://localhost:8096", metavar="URL",
                    help="Jellyfin server URL (default: http://localhost:8096)")
    jf.add_argument("--jellyfin-key", default="", metavar="KEY",
                    help="Jellyfin API key — enables fast-scaling of intros/credits instead of AI")

    args = parser.parse_args()

    # --- Validate flags early so the user finds out immediately, not hours later ---
    errors = []

    if not (0 <= args.crf <= 51):
        errors.append(f"--crf must be 0–51, got {args.crf}")

    if args.dup_threshold <= 0:
        errors.append(f"--dup-threshold must be > 0, got {args.dup_threshold}")

    try:
        gpu_idx = int(args.gpu)
        if gpu_idx < 0:
            raise ValueError
    except ValueError:
        errors.append(f"--gpu must be a non-negative integer, got {args.gpu!r}")

    valid_presets = _PRESETS.get(args.codec, [])
    if valid_presets and args.preset not in valid_presets:
        errors.append(
            f"--preset {args.preset!r} is not valid for {args.codec}. "
            f"Valid presets: {', '.join(valid_presets)}"
        )

    if errors:
        for e in errors:
            parser.error(e)

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if not input_path.exists():
        parser.error(f"Input not found: {input_path}")

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
                log.error(
                    f"Already running (PID {old_pid}). "
                    f"If that process is gone, remove: {lock_file}"
                )
                sys.exit(1)
            except (OSError, ValueError):
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
