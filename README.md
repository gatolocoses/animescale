# animescale

AI upscaling pipeline for anime video. Built around Real-ESRGAN + ffmpeg with a focus on quality and efficiency.

## Features

- **2x or 4x AI upscaling** via Real-ESRGAN (realesr-animevideov3 or realesrgan-x4plus-anime)
- **Duplicate frame detection** — anime "on twos/threes" and telecine duplicates are skipped, GPU only processes unique frames (~50% savings typical)
- **Auto-deinterlace** — detects telecined/interlaced content per-file, applies yadif automatically
- **Jellyfin Intro Skipper integration** — intro and credits are fast-scaled with lanczos instead of AI upscaled
- **10-bit HEVC encoding** — eliminates gradient banding, CRF 14 for near-transparent quality
- **Parallel streaming** — upscaler and encoder run simultaneously, no waiting for all frames before encoding starts
- **Hardware encoding** — NVENC (NVIDIA), VAAPI (AMD/Intel), AMF (AMD) via `--codec`
- **Resume support** — `--resume` picks up an interrupted upscale run without re-extracting frames
- **Dry run** — `--dry-run` previews what would be processed without doing any work

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

# 4x upscale with GPU-accelerated encoding (NVIDIA)
animescale /input/dir/ /output/dir/ --scale 4 --codec hevc_nvenc

# Preview what would be processed without doing any work
animescale /input/dir/ /output/dir/ --dry-run

# Resume an interrupted upscale (reuses already-extracted frames)
animescale input.mkv /output/dir/ --resume

# Monitor progress in a separate terminal
animescale-monitor
```

## Options

```
animescale <input> <output> [options]

video quality:
  --scale {2,4}             upscale factor (default: 2)
  --model MODEL             Real-ESRGAN model name (default: realesr-animevideov3)
  --models-dir PATH         directory containing model files
                            (default: /usr/share/realesrgan-ncnn-vulkan/models)
  --codec CODEC             SW: libx264 | libx265 | libsvtav1
                            HW: h264_nvenc | hevc_nvenc (NVIDIA)
                                hevc_vaapi | h264_vaapi (AMD/Intel)
                                hevc_amf   | h264_amf   (AMD)
                            (default: libx265)
  --crf INT                 quality — lower is better (default: 14)
                            used as -cq for NVENC, -qp for VAAPI/AMF
  --preset PRESET           encoder speed preset (default: medium)
                            SW: ultrafast…placebo  |  HW: fast/medium/slow
  --vaapi-device PATH       VAAPI render node (default: /dev/dri/renderD128)

performance:
  --gpu INT                 Vulkan GPU device index (default: 0)
  --dup-threshold FLOAT     duplicate detection sensitivity, lower = stricter (default: 1.0)
  --temp-dir PATH           work directory for temporary files (default: /tmp/animescale)
  --min-free-gb GB          abort if temp dir has less than this much free space (default: 25)

misc:
  --dry-run                 show what would be processed without doing any work
  --resume                  reuse extracted frames from an interrupted run

jellyfin intro skipper:
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
