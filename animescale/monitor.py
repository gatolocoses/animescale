"""Live pipeline monitor dashboard."""
import os
import subprocess
import time
from pathlib import Path


TEMP_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "animescale"


def _data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
    return Path(xdg) / "animescale"


def format_time(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def bar(value: int, total: int, width: int = 30) -> str:
    if total <= 0:
        total = 1
    fill = max(0, min(width, int(value / total * width)))
    return "#" * fill + "-" * (width - fill)


def pct(value: int, total: int) -> str:
    if total <= 0:
        return "0.0"
    return f"{value / total * 100:.1f}"


def count_png(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.glob("*.png"))


def find_pid(pattern: str) -> str:
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""


def parse_frame_map(map_file: Path):
    ai_unique = dup_count = skip_count = map_total = 0
    if not map_file.exists():
        return ai_unique, dup_count, skip_count, map_total
    for line in map_file.read_text().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        map_total += 1
        fnum, src, _, mode = parts[0], parts[1], parts[2], parts[3]
        if mode == "skip":
            skip_count += 1
        elif fnum == src:
            ai_unique += 1
        else:
            dup_count += 1
    return ai_unique, dup_count, skip_count, map_total


def gpu_utilization() -> str:
    """Return GPU utilization string, trying AMD/Intel DRM sysfs then nvidia-smi."""
    cards = sorted(Path("/sys/class/drm").glob("card*/device/gpu_busy_percent"))
    if cards:
        parts = [f"GPU{i} {p.read_text().strip()}%" for i, p in enumerate(cards)]
        return "  ".join(parts)

    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,utilization.gpu",
         "--format=csv,noheader"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        parts = []
        for i, line in enumerate(result.stdout.strip().splitlines()):
            name, util = line.split(",", 1)
            parts.append(f"GPU{i} ({name.strip()}) {util.strip()}%")
        return "  ".join(parts)

    return "?"


def gpu_temps() -> tuple[str, str]:
    """Return (cpu_temp, gpu_temp) strings from lm_sensors, or '?' if unavailable."""
    sensors = subprocess.run(["sensors"], capture_output=True, text=True)
    cpu_temp = gpu_temp = "?"
    if sensors.returncode != 0:
        return cpu_temp, gpu_temp
    for line in sensors.stdout.splitlines():
        # CPU: look for common patterns across vendors
        if cpu_temp == "?" and any(k in line for k in ("Tctl:", "Package id 0:", "Core 0:")):
            parts = line.split()
            if len(parts) >= 2:
                cpu_temp = parts[1]
        # GPU: look for junction (AMD) or edge (AMD/Intel)
        if any(k in line for k in ("junction:", "edge:", "GPU Temperature:")):
            parts = line.split()
            if len(parts) >= 2:
                gpu_temp = parts[1]
    return cpu_temp, gpu_temp


def main() -> None:
    data_dir = _data_dir()
    log_file = data_dir / "upscale.log"
    lock_file = data_dir / ".upscale.lock"

    while True:
        os.system("clear")
        now = int(time.time())

        work_dir: Path | None = None
        if TEMP_DIR.exists():
            subdirs = [d for d in TEMP_DIR.iterdir() if d.is_dir()]
            if subdirs:
                work_dir = subdirs[0]

        pipeline_pid = lock_file.read_text().strip() if lock_file.exists() else ""
        pipeline_alive = False
        if pipeline_pid:
            try:
                os.kill(int(pipeline_pid), 0)
                pipeline_alive = True
            except (ProcessLookupError, ValueError):
                pass

        upscale_pid = find_pid("realesrgan-ncnn-vulkan")
        encode_pid = find_pid("ffmpeg.*image2pipe")
        extract_pid = find_pid("ffmpeg.*frames/f_")
        scale_pid = find_pid("ffmpeg.*lanczos")

        print("======================  ANIME UPSCALE PIPELINE  ======================")

        if log_file.exists():
            lines = log_file.read_text().splitlines()
            output_dir_line = next(
                (l for l in reversed(lines) if "Output:" in l and "/" in l), ""
            )
            output_dir_str = output_dir_line.split("Output:")[-1].strip().split()[0] if output_dir_line else ""
            output_dir = Path(output_dir_str) if output_dir_str else None
            if output_dir and output_dir.exists():
                completed = sum(1 for f in output_dir.iterdir() if f.suffix in (".mkv", ".mp4"))
                if completed > 0:
                    print(f"  Completed:   {completed} file(s) in {output_dir}")

        if work_dir and work_dir.exists():
            encoded = int((work_dir / ".encoded_frames").read_text().strip()) \
                if (work_dir / ".encoded_frames").exists() else 0

            map_file = work_dir / "frame_map.txt"
            ai_unique, dup_count, skip_count, map_total = parse_frame_map(map_file)
            gpu_saved = (dup_count + skip_count) * 100 // map_total if map_total else 0

            extracted = count_png(work_dir / "frames")
            unique_linked = count_png(work_dir / "unique")
            scaled_done = count_png(work_dir / "scaled")
            upscaled_done = count_png(work_dir / "upscaled_unique")

            print(f"  Episode:     {work_dir.name}")
            print()

            if extract_pid and extracted < map_total and map_total > 0:
                print("  Stage:       EXTRACTING FRAMES")
                print(f"  Extract:     [{bar(extracted, map_total)}] {pct(extracted, map_total)}%  ({extracted} / {map_total})")

            elif scale_pid or (unique_linked < ai_unique and not upscale_pid and not encode_pid):
                print("  Stage:       PREPARING (linking + fast-scaling)")
                if ai_unique > 0:
                    print(f"  Unique:      [{bar(unique_linked, ai_unique)}] {pct(unique_linked, ai_unique)}%  ({unique_linked} / {ai_unique} linked)")
                if skip_count > 0:
                    print(f"  Fast-scale:  [{bar(scaled_done, skip_count)}] {pct(scaled_done, skip_count)}%  ({scaled_done} / {skip_count} lanczos)")

            elif upscale_pid or encode_pid or encoded > 0:
                print("  Stage:       UPSCALING + ENCODING")

                if ai_unique > 0:
                    ai_consumed = 0
                    if encoded > 0 and map_total > 0 and map_file.exists():
                        lines = map_file.read_text().splitlines()
                        ai_consumed = sum(
                            1 for l in lines[:encoded]
                            if len(l.split()) >= 4 and l.split()[3] == "ai" and l.split()[0] == l.split()[1]
                        )
                    upscale_progress = min(upscaled_done + ai_consumed, ai_unique)
                    print(f"  Upscale:     [{bar(upscale_progress, ai_unique)}] {pct(upscale_progress, ai_unique)}%  ({upscale_progress} / {ai_unique} AI frames)  pending: {upscaled_done}")

                if map_total > 0:
                    out_files = list(work_dir.glob("out.*"))
                    out_size = f"{out_files[0].stat().st_size // (1024 * 1024)}MB" if out_files else "0MB"
                    print(f"  Encode:      [{bar(encoded, map_total)}] {pct(encoded, map_total)}%  ({encoded} / {map_total})  {out_size}")

                if encoded > 0 and map_total > 0:
                    encode_start_file = work_dir / ".encode_start"
                    if not encode_start_file.exists():
                        encode_start_file.write_text(str(now))
                    encode_start = int(encode_start_file.read_text().strip())
                    elapsed = now - encode_start
                    if elapsed > 0:
                        rate = encoded / elapsed
                        remaining = map_total - encoded
                        eta = int(remaining / rate) if rate > 0 else 0
                        print(f"               Elapsed: {format_time(elapsed)}  |  ETA: {format_time(eta)}  |  Rate: {rate:.2f} fps")

            else:
                status = "Running (detecting...)" if pipeline_alive else "Idle"
                print(f"  Stage:       {status}")

            if map_total > 0:
                print()
                print(f"  Dedup:       {ai_unique} AI  |  {dup_count} dedup  |  {skip_count} intro/outro skip  ({gpu_saved}% GPU saved)")

            print()
            pids_str = f"Pipeline: {pipeline_pid or '—'}"
            if upscale_pid:
                pids_str += f"  Upscaler: {upscale_pid}"
            if encode_pid:
                pids_str += f"  Encoder: {encode_pid}"
            if extract_pid:
                pids_str += f"  Extract: {extract_pid}"
            print(f"  PIDs:        {pids_str}")

        else:
            status = "Running (no work dir yet...)" if pipeline_alive else "Idle"
            print(f"  Status:      {status}")

        print()
        print("-------------------------  SYSTEM  -------------------------")
        print(f"  GPU:   {gpu_utilization()}")
        cpu_temp, gpu_temp = gpu_temps()
        print(f"  Temp:  CPU {cpu_temp}  |  GPU {gpu_temp}")

        free_result = subprocess.run(["df", "-h", str(TEMP_DIR)], capture_output=True, text=True)
        free_str = free_result.stdout.splitlines()[-1].split()[3] if free_result.returncode == 0 else "?"
        used_result = subprocess.run(["du", "-sh", str(TEMP_DIR)], capture_output=True, text=True)
        used_str = used_result.stdout.split()[0] if used_result.returncode == 0 else "0"
        print(f"  NVMe:  {used_str} used  |  {free_str} free")

        if log_file.exists() and log_file.stat().st_size:
            last_line = log_file.read_text().splitlines()[-1][:75]
            print(f"\n  Log:   {last_line}")

        print()
        print(f"=================================================  {time.strftime('%H:%M:%S')}  5s")
        time.sleep(5)
