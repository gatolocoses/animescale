"""Interlace detection and duplicate frame analysis."""
import re
import subprocess
from dataclasses import dataclass
from typing import Literal

Mode = Literal["ai", "skip"]


@dataclass
class FrameEntry:
    frame_num: int
    source_unique: int
    is_last: bool
    mode: Mode


@dataclass
class FrameMapStats:
    unique_ai: int
    dup_count: int
    skip_count: int
    total: int

    def summary(self) -> str:
        saved = (self.dup_count + self.skip_count) * 100 // self.total if self.total else 0
        return (
            f"{self.unique_ai} AI-upscale, {self.dup_count} dedup, "
            f"{self.skip_count} fast-scale ({saved}% GPU saved)"
        )


def get_fps(input_file: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", input_file],
        capture_output=True, text=True,
    )
    fps_str = result.stdout.strip()
    if "/" in fps_str:
        num, den = map(int, fps_str.split("/"))
        return num / den
    return float(fps_str)


def detect_interlace(input_file: str) -> bool:
    """Sample 3 points in the video and vote on whether it's interlaced."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_file],
        capture_output=True, text=True,
    )
    try:
        duration = int(float(result.stdout.strip()))
    except (ValueError, TypeError):
        duration = 0
    duration = max(duration, 30)

    quarter = duration // 4
    tff_total = prog_total = 0

    for offset in (quarter, quarter * 2, quarter * 3):
        proc = subprocess.run(
            ["ffmpeg", "-ss", str(offset), "-i", input_file,
             "-t", "3", "-vf", "idet", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        output = proc.stderr
        for line in reversed(output.splitlines()):
            if "Multi frame detection" in line:
                tff = int(m.group(1)) if (m := re.search(r"TFF:\s*(\d+)", line)) else 0
                prog = int(m.group(1)) if (m := re.search(r"Progressive:\s*(\d+)", line)) else 0
                tff_total += tff
                prog_total += prog
                break

    return tff_total > prog_total


def detect_duplicates(
    input_file: str,
    map_file: str,
    threshold: float,
    deinterlace: bool,
    intro_start: float = -1,
    intro_end: float = -1,
    credits_start: float = -1,
    credits_end: float = -1,
) -> FrameMapStats:
    """
    Scan all frames at low resolution, detect duplicates, mark intro/credits
    frames for fast-scaling, and write the frame map file.
    """
    vf = "yadif=mode=0:parity=0:deint=0," if deinterlace else ""
    vf += "scale=128:72,format=gray"

    fps = get_fps(input_file)

    def to_frame(t: float) -> int:
        return int(t * fps) if t >= 0 else -1

    intro_start_f = to_frame(intro_start)
    intro_end_f = to_frame(intro_end)
    credits_start_f = to_frame(credits_start)

    CHUNK = 128 * 72
    MAX_DUP_RUN = 6  # real telecine/on-twos dupes never exceed 3-4 frames

    proc = subprocess.Popen(
        ["ffmpeg", "-i", input_file, "-vf", vf,
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    prev_data: bytes | None = None
    current_unique = 1
    entries: list[FrameEntry] = []
    frame_num = 0
    dup_run = 0

    while True:
        data = proc.stdout.read(CHUNK)
        if len(data) < CHUNK:
            break
        frame_num += 1

        in_intro = intro_start_f >= 0 and intro_start_f <= frame_num <= intro_end_f
        in_credits = credits_start_f >= 0 and credits_start_f <= frame_num
        mode: Mode = "skip" if (in_intro or in_credits) else "ai"

        is_unique = True
        if prev_data is not None:
            diff = sum(abs(a - b) for a, b in zip(data, prev_data)) / CHUNK
            if diff < threshold:
                is_unique = False

        # Cap consecutive duplicate runs to prevent frozen video on slow pans/static shots
        if not is_unique and mode == "ai":
            dup_run += 1
            if dup_run >= MAX_DUP_RUN:
                is_unique = True
                dup_run = 0
        else:
            dup_run = 0

        # Only ai frames can be sources — skip frames must never become current_unique
        # or the consumer will wait for an upscaled file that was never produced.
        if is_unique and mode == "ai":
            current_unique = frame_num
        entries.append(FrameEntry(frame_num, current_unique, False, mode))
        prev_data = data

    proc.wait()

    # Mark is_last: true when the next frame uses a different source unique
    for i, entry in enumerate(entries):
        entry.is_last = (
            i == len(entries) - 1
            or entries[i + 1].source_unique != entry.source_unique
        )

    unique_ai = dup_count = skip_count = 0
    with open(map_file, "w") as f:
        for entry in entries:
            f.write(f"{entry.frame_num} {entry.source_unique} {int(entry.is_last)} {entry.mode}\n")
            if entry.mode == "skip":
                skip_count += 1
            elif entry.frame_num == entry.source_unique:
                unique_ai += 1
            else:
                dup_count += 1

    return FrameMapStats(unique_ai, dup_count, skip_count, len(entries))
