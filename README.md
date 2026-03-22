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
- Python 3.11+
- Any Vulkan-capable GPU (AMD, NVIDIA, Intel)

## Installation

### Arch Linux (AUR)

```bash
yay -S animescale
```

### Manual

```bash
git clone https://github.com/gatolocoses/animescale
cd animescale
pip install .
```


## Usage

```bash
# Upscale a single file
animescale input.mkv /output/dir/

# Upscale all episodes in a directory
animescale /input/dir/ /output/dir/

# 4x upscale with a different codec
animescale /input/dir/ /output/dir/ --scale 4 --model realesrgan-x4plus-anime --codec libx264

# Monitor progress in a separate terminal
animescale-monitor
```

## Options

```
animescale <input> <output> [options]

  --scale {2,4}             upscale factor (default: 2)
  --model MODEL             Real-ESRGAN model name (default: realesr-animevideov3)
  --codec CODEC             libx264 | libx265 | libsvtav1 (default: libx265)
  --crf INT                 quality — lower is better (default: 14)
  --preset PRESET           encoder preset (default: medium)
  --gpu INT                 Vulkan GPU device index (default: 0)
  --dup-threshold FLOAT     duplicate detection sensitivity, lower = stricter (default: 1.0)
  --jellyfin-url URL        Jellyfin server URL (default: http://localhost:8096)
  --jellyfin-key KEY        Jellyfin API key — enables intro/credits fast-scaling
```

## How it works

```
Input video
    │
    ├─ detect interlacing (idet filter, 3 sample points)
    ├─ query Jellyfin for intro/credits timestamps
    ├─ fast decode at 128x72 grayscale → detect duplicate frames
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
               └─ ffmpeg (libx265 CRF 14 medium 10-bit) → output.mkv
```

## Performance

| Content | Unique frames | GPU saved |
|---------|--------------|-----------|
| Action-heavy episode | ~30% | ~52% |
| Dialogue-heavy episode | ~15% | ~65%+ |

Typical encode: 1–4 hours per episode depending on content and dedup rate.
