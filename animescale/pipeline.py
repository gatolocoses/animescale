"""Main upscaling pipeline."""
import logging
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .config import Config
from .detect import detect_interlace, detect_duplicates, get_fps, get_resolution
from .encode import build_vcodec_flags, fast_scale_frame, stream_frames
from .jellyfin import Segments, get_segments

log = logging.getLogger("animescale")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts"}


def find_videos(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(
        f for f in path.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    )


def free_gb(directory: Path) -> int:
    stat = os.statvfs(directory)
    return (stat.f_bavail * stat.f_frsize) // (1024 ** 3)


def check_deps() -> None:
    missing = [
        cmd for cmd in ("realesrgan-ncnn-vulkan", "ffmpeg", "ffprobe")
        if not shutil.which(cmd)
    ]
    if missing:
        hint = {
            "realesrgan-ncnn-vulkan": "  Install from: https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan",
            "ffmpeg":  "  Install with: sudo pacman -S ffmpeg   (or your distro's equivalent)",
            "ffprobe": "  Install with: sudo pacman -S ffmpeg   (ffprobe is bundled with ffmpeg)",
        }
        msg = "Missing required programs:\n" + "\n".join(
            f"  - {cmd}\n{hint.get(cmd, '')}" for cmd in missing
        )
        sys.exit(msg)


class Pipeline:
    def __init__(self, cfg: Config, output_dir: Path, log_file: Path, lock_file: Path):
        self.cfg = cfg
        self.output_dir = output_dir
        self.log_file = log_file
        self.lock_file = lock_file
        self._upscaler: subprocess.Popen | None = None

        signal.signal(signal.SIGINT, self._on_interrupt)
        signal.signal(signal.SIGTERM, self._on_interrupt)

    def _on_interrupt(self, signum, frame):
        log.info("Interrupted — stopping child processes...")
        if self._upscaler:
            try:
                self._upscaler.terminate()
            except Exception:
                pass
        subprocess.run(["pkill", "-P", str(os.getpid()), "ffmpeg"],
                       capture_output=True)
        self.lock_file.unlink(missing_ok=True)
        log.info(f"Cleaned up. Work directory preserved: {self.cfg.temp_dir}")
        sys.exit(130)

    def run(self, input_path: Path) -> None:
        check_deps()

        files = find_videos(input_path)
        if not files:
            sys.exit(f"No video files found in {input_path}")

        temp_dir = Path(self.cfg.temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        vcodec_flags = build_vcodec_flags(self.cfg)
        total = len(files)

        log.info("")
        log.info("==========================================")
        log.info("Anime Upscale Pipeline")
        log.info(f"Input:  {input_path} ({total} files)")
        log.info(f"Output: {self.output_dir} (.{self.cfg.output_ext})")
        log.info(f"Config: {self.cfg.scale}x {self.cfg.model} | {self.cfg.codec} CRF {self.cfg.crf} {self.cfg.preset} 10-bit")
        jellyfin_status = "enabled" if self.cfg.jellyfin_api_key else "disabled"
        log.info(f"GPU:    {self.cfg.gpu} | Dedup: {self.cfg.dup_threshold} | Jellyfin: {jellyfin_status}")
        log.info("==========================================")

        done = skipped = failed = 0

        for i, input_file in enumerate(files, 1):
            prefix = f"[{i}/{total}]"
            name = input_file.stem
            output_file = self.output_dir / f"{name}.{self.cfg.output_ext}"
            work = temp_dir / name

            if output_file.exists():
                log.info(f"{prefix} {input_file.name} — done, skipping")
                skipped += 1
                continue

            gb = free_gb(temp_dir)
            if gb < self.cfg.min_free_gb:
                log.warning(
                    f"{prefix} {input_file.name} — only {gb}GB free in {temp_dir}, "
                    f"need {self.cfg.min_free_gb}GB. Stopping to avoid filling disk."
                )
                failed += 1
                break

            if work.exists():
                shutil.rmtree(work)
            for sub in ("frames", "unique", "upscaled_unique", "scaled"):
                (work / sub).mkdir(parents=True)

            log.info("")
            log.info(f"{prefix} ===== {input_file.name} =====")

            success = self._process_file(input_file, output_file, work, vcodec_flags, prefix)
            if success:
                done += 1
            else:
                failed += 1

        log.info("")
        log.info("==========================================")
        log.info(f"COMPLETE: {done} done, {skipped} skipped, {failed} failed")
        log.info(f"Output: {self.output_dir}")
        log.info("==========================================")

    def _process_file(
        self,
        input_file: Path,
        output_file: Path,
        work: Path,
        vcodec_flags: list[str],
        prefix: str,
    ) -> bool:
        cfg = self.cfg

        # 1. Detect interlacing
        log.info(f"{prefix} Detecting interlacing...")
        deinterlace = detect_interlace(str(input_file))
        if deinterlace:
            log.info(f"{prefix} Interlaced/telecined — will deinterlace")
            vf_extract = ["-vf", "yadif=mode=0:parity=0:deint=0"]
        else:
            log.info(f"{prefix} Progressive — no deinterlace needed")
            vf_extract = []

        # 2. Query Jellyfin for intro/outro
        segments = Segments()
        if cfg.jellyfin_api_key:
            log.info(f"{prefix} Querying Jellyfin for intro/outro...")
            result = get_segments(str(input_file), cfg.jellyfin_url, cfg.jellyfin_api_key)
            if result:
                segments = result
                desc = segments.description()
                if desc:
                    log.info(f"{prefix} Segments: {desc} (fast-scale)")
            else:
                log.info(f"{prefix} No Jellyfin segments found — full AI upscale")

        # 3. Detect duplicates + mark segments
        log.info(f"{prefix} Analyzing frames...")
        map_file = work / "frame_map.txt"
        stats = detect_duplicates(
            str(input_file), str(map_file), cfg.dup_threshold, deinterlace,
            segments.intro_start, segments.intro_end,
            segments.credits_start, segments.credits_end,
        )
        log.info(f"{prefix} {stats.summary()}")

        # 4. Extract frames
        log.info(f"{prefix} Extracting frames...")
        extract_log = work / "extract.log"
        with open(extract_log, "w") as extract_log_fh:
            result = subprocess.run(
                ["ffmpeg", "-i", str(input_file), *vf_extract,
                 "-q:v", "1", str(work / "frames" / "f_%06d.png"), "-y"],
                stderr=extract_log_fh,
            )
        if result.returncode != 0:
            log.error(f"{prefix} FAILED: could not extract frames from {input_file.name}")
            last_lines = extract_log.read_text().splitlines()
            for line in last_lines[-5:]:
                if line.strip():
                    log.error(f"  ffmpeg: {line.strip()}")
            return False

        frame_files = list((work / "frames").glob("*.png"))
        frame_count = len(frame_files)
        if frame_count == 0:
            log.error(f"{prefix} FAILED: ffmpeg ran but produced no frames — is the file a valid video?")
            shutil.rmtree(work)
            return False

        (work / ".total_frames").write_text(str(frame_count))
        fps = get_fps(str(input_file))
        src_w, src_h = get_resolution(str(input_file))
        target_w, target_h = cfg.target_resolution(src_w, src_h)

        # 5. Sanity-check frame count vs map; parse map once for all downstream use
        with open(map_file) as fh:
            map_entries = [line.split() for line in fh if line.strip()]

        # Discard malformed lines (shouldn't happen, but be defensive)
        map_entries = [p for p in map_entries if len(p) == 4]

        if len(map_entries) != frame_count:
            log.warning(f"{prefix} Frame count mismatch (analysis={len(map_entries)}, extracted={frame_count}) — dedup disabled for this file")
            map_entries = [[str(n), str(n), "1", "ai"] for n in range(1, frame_count + 1)]
            with open(map_file, "w") as fh:
                for p in map_entries:
                    fh.write(" ".join(p) + "\n")

        # 6. Link unique AI frames and fast-scale skip frames
        unique_entries = [p for p in map_entries if p[3] == "ai" and p[0] == p[1]]
        skip_frames = [p[0] for p in map_entries if p[3] == "skip"]
        unique_count = len(unique_entries)

        log.info(f"{prefix} Linking {unique_count} unique frames...")
        for p in unique_entries:
            src = work / "frames" / f"f_{int(p[0]):06d}.png"
            dst = work / "unique" / src.name
            os.link(src, dst)

        if skip_frames:
            log.info(f"{prefix} Fast-scaling {len(skip_frames)} skip frames (parallel)...")
            args = [
                (work / "frames" / f"f_{int(fnum):06d}.png",
                 work / "scaled" / f"f_{int(fnum):06d}.png",
                 target_w, target_h)
                for fnum in skip_frames
            ]
            with multiprocessing.Pool() as pool:
                pool.starmap(fast_scale_frame, args)

        log.info(f"{prefix} {frame_count} frames @ {fps} fps — AI: {unique_count}, fast-scale: {len(skip_frames)} ({free_gb(work)}GB free)")

        # 7. Upscale unique content frames in background
        log.info(f"{prefix} Upscaling {unique_count} frames ({cfg.scale}x)...")
        upscale_log = work / "upscale.log"
        upscale_log_fh = open(upscale_log, "w")
        self._upscaler = subprocess.Popen(
            ["realesrgan-ncnn-vulkan",
             "-i", str(work / "unique"),
             "-o", str(work / "upscaled_unique"),
             "-n", cfg.model, "-s", str(cfg.scale), "-f", "png",
             "-g", cfg.gpu],
            stdout=upscale_log_fh, stderr=subprocess.STDOUT,
        )

        # 8. Stream frames to encoder in parallel
        log.info(f"{prefix} Encoding ({cfg.codec} CRF {cfg.crf} {cfg.preset} 10-bit)...")
        audio_file = work / "audio.mkv"
        subprocess.run(
            ["ffmpeg", "-i", str(input_file), "-vn",
             "-c:a", "copy", "-c:s", "copy", str(audio_file), "-y"],
            capture_output=True,
        )

        video_file = work / "video.mkv"
        encode_log = work / "encode.log"
        encode_log_fh = open(encode_log, "w")
        encoder = subprocess.Popen(
            ["ffmpeg",
             "-framerate", str(fps), "-f", "image2pipe", "-vcodec", "png", "-i", "-",
             *vcodec_flags,
             str(video_file), "-y"],
            stdin=subprocess.PIPE,
            stderr=encode_log_fh,
        )

        pipe_ok = True
        try:
            stream_frames(map_file, work, self._upscaler.pid, encoder.stdin)
        except Exception as e:
            log.error(f"{prefix} Streaming error: {e}")
            pipe_ok = False
        finally:
            try:
                encoder.stdin.close()
            except OSError:
                pass
            upscale_log_fh.close()
            encode_log_fh.close()

        encoder.wait()
        pipe_exit = encoder.returncode

        # Mux video + audio
        out_file = work / f"out.{cfg.output_ext}"
        if pipe_ok and pipe_exit == 0:
            subprocess.run(
                ["ffmpeg",
                 "-i", str(video_file), "-i", str(audio_file),
                 "-map", "0:v", "-map", "1:a?", "-map", "1:s?",
                 "-c:v", "copy", "-c:a", "copy", "-c:s", "copy",
                 "-movflags", "+faststart",
                 str(out_file), "-y"],
                capture_output=True,
            )

        self._upscaler.wait()
        upscale_exit = self._upscaler.returncode
        self._upscaler = None

        # 9. Validate and move output
        if not pipe_ok or pipe_exit != 0 or not out_file.exists():
            log.error(f"{prefix} FAILED encoding {input_file.name}")
            upscale_tail = [l.strip() for l in upscale_log.read_text().splitlines() if l.strip()][-3:]
            encode_tail  = [l.strip() for l in encode_log.read_text().splitlines()  if l.strip()][-3:]
            if upscale_exit != 0 and upscale_tail:
                log.error(f"  Upscaler (exit {upscale_exit}) — check model name ({cfg.model!r}):")
                for line in upscale_tail:
                    log.error(f"    {line}")
            if pipe_exit != 0 and encode_tail:
                log.error(f"  Encoder (exit {pipe_exit}):")
                for line in encode_tail:
                    log.error(f"    {line}")
            return False

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out_file)],
            capture_output=True,
        )
        if probe.returncode != 0:
            log.error(f"{prefix} FAILED: Output file appears corrupt — check disk space and encoder logs at {encode_log}")
            return False

        size_mb = out_file.stat().st_size // (1024 * 1024)
        shutil.move(str(out_file), output_file)
        shutil.rmtree(work)
        log.info(f"{prefix} DONE — {size_mb}MB")
        return True
