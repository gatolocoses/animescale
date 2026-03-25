"""FFmpeg codec configuration and frame streaming utilities."""
import os
import subprocess
import time
from pathlib import Path
from typing import IO

from .config import Config


_NVENC_PRESET = {"fast": "p3", "medium": "p5", "slow": "p7"}
_AMF_PRESET   = {"fast": "speed", "medium": "balanced", "slow": "quality"}
# VAAPI compression_level: lower = faster, higher = better compression (0-6)
_VAAPI_LEVEL  = {"fast": "1", "medium": "3", "slow": "5"}


def build_vcodec_flags(cfg: Config) -> tuple[list[str], list[str]]:
    """
    Return (pre_input_flags, post_input_flags) for the encoder ffmpeg command.

    pre_input_flags go before -i (e.g. -vaapi_device for VAAPI).
    post_input_flags go after -i (codec, quality, pix_fmt, etc).
    For most codecs pre_input_flags is empty.
    """
    if cfg.codec == "libx264":
        return [], [
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-profile:v", "high10", "-pix_fmt", cfg.pix_fmt,
            "-g", "240", "-x264-params", "keyint=240:scenecut=1",
        ]
    elif cfg.codec == "libx265":
        return [], [
            "-c:v", "libx265", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", cfg.pix_fmt, "-tag:v", "hvc1",
            "-x265-params", "keyint=240:min-keyint=24:scenecut=40",
        ]
    elif cfg.codec == "libsvtav1":
        return [], [
            "-c:v", "libsvtav1", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-b:v", "0", "-pix_fmt", cfg.pix_fmt, "-g", "240",
            "-svtav1-params", "keyint=240:scd=1",
        ]
    elif cfg.codec in ("h264_nvenc", "hevc_nvenc"):
        preset = _NVENC_PRESET.get(cfg.preset, "p5")
        pix_fmt = "p010le" if cfg.codec == "hevc_nvenc" else "yuv420p"
        return [], [
            "-c:v", cfg.codec, "-preset", preset, "-cq", str(cfg.crf),
            "-pix_fmt", pix_fmt, "-g", "240",
        ]
    elif cfg.codec in ("h264_vaapi", "hevc_vaapi"):
        level = _VAAPI_LEVEL.get(cfg.preset, "3")
        fmt = "p010" if cfg.codec == "hevc_vaapi" else "nv12"
        pre = ["-vaapi_device", cfg.vaapi_device]
        post = [
            "-vf", f"format=nv12,hwupload,scale_vaapi=format={fmt}",
            "-c:v", cfg.codec, "-qp", str(cfg.crf),
            "-compression_level", level, "-g", "240",
        ]
        return pre, post
    elif cfg.codec in ("h264_amf", "hevc_amf"):
        quality = _AMF_PRESET.get(cfg.preset, "balanced")
        pix_fmt = "p010le" if cfg.codec == "hevc_amf" else "yuv420p"
        return [], [
            "-c:v", cfg.codec, "-quality", quality,
            "-qp_i", str(cfg.crf), "-qp_p", str(cfg.crf),
            "-pix_fmt", pix_fmt, "-g", "240",
        ]
    else:
        raise ValueError(f"Unknown codec: {cfg.codec}")


def wait_frame(path: Path, upscaler_pid: int) -> bool:
    """Wait until a frame file exists and has finished being written."""
    while not path.exists():
        try:
            os.kill(upscaler_pid, 0)
        except OSError:
            # ProcessLookupError (no such process) or PermissionError — upscaler gone
            return False
        time.sleep(0.08)

    prev_size = -1
    while True:
        try:
            cur_size = path.stat().st_size
        except FileNotFoundError:
            return False
        if cur_size > 0 and cur_size == prev_size:
            return True
        prev_size = cur_size
        time.sleep(0.04)


def fast_scale_frame(src: Path, dst: Path, width: int, height: int) -> None:
    result = subprocess.run(
        ["ffmpeg", "-i", str(src),
         "-vf", f"scale={width}:{height}:flags=lanczos",
         str(dst), "-y"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fast_scale_frame failed for {src} → {dst}")


def stream_frames(map_file: Path, work: Path, upscaler_pid: int, out: IO[bytes]) -> int:
    """
    Read the frame map and stream PNG data to `out` for the encoder.
    Returns the number of frames written.
    """
    encoded = 0
    with open(map_file) as f:
        for line in f:
            fnum_s, src_s, is_last_s, mode = line.split()
            fnum = int(fnum_s)
            src = int(src_s)
            is_last = is_last_s == "1"

            fname = f"f_{fnum:06d}.png"
            src_fname = f"f_{src:06d}.png"

            if mode == "skip":
                scaled = work / "scaled" / fname
                data = scaled.read_bytes()
                out.write(data)
                scaled.unlink(missing_ok=True)
                (work / "frames" / fname).unlink(missing_ok=True)
            else:
                upscaled = work / "upscaled_unique" / src_fname
                if not wait_frame(upscaled, upscaler_pid):
                    raise RuntimeError(f"Upscaler died at frame {fnum}")
                data = upscaled.read_bytes()
                out.write(data)
                (work / "frames" / fname).unlink(missing_ok=True)
                if is_last:
                    upscaled.unlink(missing_ok=True)

            encoded += 1
            (work / ".encoded_frames").write_text(str(encoded))

    return encoded
