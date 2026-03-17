# animescale

AI upscaling pipeline for anime video. Built around Real-ESRGAN + ffmpeg with a focus on quality and efficiency.

## Features

- **2x or 4x AI upscaling** via Real-ESRGAN (realesr-animevideov3 or realesrgan-x4plus-anime)
- **Duplicate frame detection** — anime "on twos/threes" and telecine duplicates are skipped, GPU only processes unique frames (~50% savings typical)
- **Auto-deinterlace** — detects telecined/interlaced content per-file, applies yadif automatically
- **Jellyfin Intro Skipper integration** — intro and credits are fast-scaled with lanczos instead of AI upscaled
- **10-bit HEVC encoding** — eliminates gradient banding, CRF 14 for near-transparent quality
- **Parallel streaming** — upscaler and encoder run simultaneously, no waiting for all frames before encoding starts

## Requirements

- `realesrgan-ncnn-vulkan` — [xinntao/Real-ESRGAN-ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan)
- `ffmpeg` + `ffprobe`
- `python3`
- Vulkan-capable GPU (AMD, NVIDIA, Intel)

## Usage

```bash
# Upscale a single file
./upscale.sh input.mkv /output/dir/

# Upscale all episodes in a directory
./upscale.sh /input/dir/ /output/dir/

# Monitor progress in a separate terminal
./monitor.sh
```

## Configuration

Edit the configuration section at the top of `upscale.sh`:

```bash
SCALE=2                         # 2x or 4x
MODEL="realesr-animevideov3"    # model name
CODEC="libx265"                 # libx264 | libx265 | libsvtav1
CRF=14                          # quality (lower = better)
PRESET="slow"                   # encoding preset
DUP_THRESHOLD=1.0               # duplicate detection sensitivity (lower = stricter)

# Optional: Jellyfin Intro Skipper integration
JELLYFIN_URL="http://localhost:8096"
JELLYFIN_API_KEY=""             # leave empty to disable
```

## How it works

```
Input video
    │
    ├─ detect interlacing (idet filter, 3 sample points)
    ├─ query Jellyfin for intro/credits timestamps
    ├─ fast decode at 64x36 grayscale → detect duplicate frames
    │
    ├─ extract frames (with yadif if interlaced)
    ├─ hard-link unique AI frames → unique/
    ├─ lanczos-scale intro/credits frames → scaled/  (parallel, all CPU cores)
    │
    ├─ realesrgan-ncnn-vulkan on unique/ → upscaled_unique/  (background)
    │
    └─ consumer loop:
           dedup frame  → reuse upscaled PNG (instant)
           skip frame   → read from scaled/ (instant)
           ai frame     → wait for upscaled_unique/, pipe to encoder
               └─ ffmpeg (libx265 CRF 14 slow 10-bit) → output.mkv
```

## Performance (AMD RX 6650 XT)

| Content | Unique frames | GPU saved |
|---------|--------------|-----------|
| Action-heavy episode | ~30% | ~52% |
| Dialogue-heavy episode | ~15% | ~65%+ |

Typical encode: 1–4 hours per episode depending on content and dedup rate.
