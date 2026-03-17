#!/bin/bash
# ============================================================
# Anime Upscaling Pipeline — Real-ESRGAN + FFmpeg
# ============================================================
# Features:
#   - Auto-detects interlaced/telecined content (per-file yadif)
#   - Skips duplicate frames (anime "on twos/threes" + telecine)
#   - Jellyfin Intro Skipper integration (fast-scales intro/outro)
#   - 10-bit HEVC for banding-free gradients
#   - Streams upscaled frames to encoder in parallel
#
# Usage:
#   ./upscale.sh <input_dir|file> <output_dir>
# ============================================================

set -uo pipefail

# ==================== Configuration ====================
SCALE=2                         # 2 = 2x, 4 = 4x
MODEL="realesr-animevideov3"    # 2x anime: realesr-animevideov3
                                # 4x anime: realesrgan-x4plus-anime
CODEC="libx265"                 # libx264 | libx265 | libsvtav1
CRF=14                          # Near-transparent for anime
PRESET="medium"                 # Fast enough, quality identical at CRF 14
PIX_FMT="yuv420p10le"           # 10-bit (eliminates banding)
OUTPUT_EXT="mkv"                # mkv supports all codecs/subs
TEMP_DIR="${TMPDIR:-/tmp}/animescale"
MIN_FREE_GB=25
DUP_THRESHOLD=1.0               # Duplicate detection (lower = stricter)

# Jellyfin Intro Skipper — set API key to enable, empty to disable
JELLYFIN_URL="http://localhost:8096"
JELLYFIN_API_KEY=""             # set your API key here to enable intro/credits skip
# =======================================================

# ---- Args ----
if [ $# -lt 2 ]; then
    echo "Usage: $0 <input_dir|file> <output_dir>"
    echo ""
    echo "  Upscales anime using Real-ESRGAN (${SCALE}x)."
    echo "  Auto-detects interlacing and duplicate frames."
    echo "  Queries Jellyfin for intro/outro segments (fast-scale instead of AI)."
    exit 1
fi

INPUT_DIR="$1"
OUTPUT_DIR="$2"

# ---- Internals ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/upscale.log"
LOCK_FILE="$SCRIPT_DIR/.upscale.lock"
UPSCALER_PID=""

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# ---- Signal handling ----
cleanup() {
    echo ""
    log "Interrupted — stopping child processes..."
    [ -n "$UPSCALER_PID" ] && kill "$UPSCALER_PID" 2>/dev/null
    pkill -P $$ ffmpeg 2>/dev/null
    wait 2>/dev/null
    rm -f "$LOCK_FILE"
    log "Cleaned up. Work directory preserved: $TEMP_DIR"
    exit 130
}
trap cleanup INT TERM
trap 'rm -f "$LOCK_FILE"' EXIT

# ---- Wait for frame to exist AND be fully written ----
wait_frame() {
    local file=$1 pid=$2
    while [ ! -f "$file" ]; do
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.08
    done
    local prev=-1 cur
    while true; do
        cur=$(stat -c%s "$file" 2>/dev/null || echo 0)
        [ "$cur" -gt 0 ] && [ "$cur" -eq "$prev" ] && return 0
        prev=$cur
        sleep 0.04
    done
}

# ---- Codec flags ----
build_vcodec_flags() {
    case "$CODEC" in
        libx264)
            VCODEC=(-c:v libx264 -preset "$PRESET" -crf "$CRF"
                    -profile:v high10 -pix_fmt "$PIX_FMT"
                    -g 240 -x264-params "keyint=240:scenecut=1") ;;
        libx265)
            VCODEC=(-c:v libx265 -preset "$PRESET" -crf "$CRF"
                    -pix_fmt "$PIX_FMT" -tag:v hvc1
                    -x265-params "keyint=240:min-keyint=24:scenecut=40") ;;
        libsvtav1)
            VCODEC=(-c:v libsvtav1 -preset "$PRESET" -crf "$CRF" -b:v 0
                    -pix_fmt "$PIX_FMT" -g 240
                    -svtav1-params "keyint=240:scd=1") ;;
        *)  log "Unknown codec: $CODEC"; exit 1 ;;
    esac
}

# ---- Detect interlacing (samples 3 points) ----
detect_interlace() {
    local file=$1 tff_total=0 prog_total=0
    local duration
    duration=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=duration -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    duration=${duration%.*}
    [ -z "$duration" ] || [ "$duration" -lt 30 ] && duration=30

    local quarter=$((duration / 4))
    for offset in "$quarter" "$((quarter * 2))" "$((quarter * 3))"; do
        local result
        result=$(ffmpeg -ss "$offset" -i "$file" -t 3 -vf idet -f null - 2>&1 \
            | grep "Multi frame detection" | tail -1)
        local tff prog
        tff=$(echo "$result" | grep -oP 'TFF:\s*\K\d+' || echo 0)
        prog=$(echo "$result" | grep -oP 'Progressive:\s*\K\d+' || echo 0)
        tff_total=$((tff_total + tff))
        prog_total=$((prog_total + prog))
    done

    [ "$tff_total" -gt "$prog_total" ] && echo "1" || echo "0"
}

# ---- Query Jellyfin for intro/outro segment times ----
# Returns "intro_start intro_end credits_start credits_end" or empty on failure.
# Looks up the episode by matching the file path in Jellyfin's library.
get_jellyfin_segments() {
    local filepath=$1
    [ -z "$JELLYFIN_API_KEY" ] && return

    # Find the Jellyfin item ID by matching file path through series library
    local item_id
    item_id=$(python3 - "$filepath" "$JELLYFIN_URL" "$JELLYFIN_API_KEY" << 'PYEOF' 2>/dev/null
import urllib.request, json, sys, os
from urllib.parse import quote

filepath = sys.argv[1]
base_url = sys.argv[2]
api_key = sys.argv[3]

# Step 1: Find the parent series by scanning the directory's parent folder name
parent_dir = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
if not parent_dir:
    parent_dir = os.path.basename(os.path.dirname(filepath))

url = f"{base_url}/Items?api_key={api_key}&searchTerm={quote(parent_dir)}&IncludeItemTypes=Series&Recursive=true&limit=5"
with urllib.request.urlopen(url) as r:
    series_data = json.load(r)

if not series_data.get("Items"):
    sys.exit(1)

series_id = series_data["Items"][0]["Id"]

# Step 2: Get all episodes from the series and match by path
offset = 0
while True:
    url = f"{base_url}/Shows/{series_id}/Episodes?api_key={api_key}&Fields=Path&startIndex={offset}&limit=200"
    with urllib.request.urlopen(url) as r:
        eps_data = json.load(r)
    items = eps_data.get("Items", [])
    if not items:
        break
    for ep in items:
        if ep.get("Path", "") == filepath:
            print(ep["Id"])
            sys.exit(0)
    offset += len(items)
    if offset >= eps_data.get("TotalRecordCount", 0):
        break
PYEOF
    )

    [ -z "$item_id" ] && return

    # Get intro/outro segments
    python3 - "$item_id" "$JELLYFIN_URL" "$JELLYFIN_API_KEY" << 'PYEOF' 2>/dev/null
import urllib.request, json, sys

item_id = sys.argv[1]
base_url = sys.argv[2]
api_key = sys.argv[3]

url = f"{base_url}/Episode/{item_id}/IntroSkipperSegments?api_key={api_key}"
with urllib.request.urlopen(url) as r:
    data = json.load(r)

intro = data.get("Introduction", {})
credits = data.get("Credits", {})

intro_start = intro.get("Start", -1) if intro.get("Valid") else -1
intro_end = intro.get("End", -1) if intro.get("Valid") else -1
credits_start = credits.get("Start", -1) if credits.get("Valid") else -1
credits_end = credits.get("End", -1) if credits.get("Valid") else -1

print(f"{intro_start} {intro_end} {credits_start} {credits_end}")
PYEOF
}

# ---- Detect duplicates + mark intro/outro for fast-scale ----
# Output frame_map.txt: "FRAME_NUM SOURCE_UNIQUE IS_LAST MODE"
#   MODE: ai = Real-ESRGAN upscale, skip = lanczos fast-scale
detect_duplicates() {
    local input_file=$1 map_file=$2 threshold=$3 deinterlace=$4
    local intro_start=${5:--1} intro_end=${6:--1}
    local credits_start=${7:--1} credits_end=${8:--1}

    python3 - "$input_file" "$map_file" "$threshold" "$deinterlace" \
        "$intro_start" "$intro_end" "$credits_start" "$credits_end" << 'PYEOF'
import subprocess, sys

input_file = sys.argv[1]
map_file = sys.argv[2]
threshold = float(sys.argv[3])
deinterlace = sys.argv[4] == "1"
intro_start = float(sys.argv[5])
intro_end = float(sys.argv[6])
credits_start = float(sys.argv[7])
credits_end = float(sys.argv[8])

vf = "yadif=mode=0:parity=0:deint=0," if deinterlace else ""
vf += "scale=128:72,format=gray"

# Get FPS to convert time→frame numbers
fps_proc = subprocess.run([
    'ffprobe', '-v', 'error', '-select_streams', 'v:0',
    '-show_entries', 'stream=r_frame_rate',
    '-of', 'default=noprint_wrappers=1:nokey=1', input_file
], capture_output=True, text=True)
fps_str = fps_proc.stdout.strip()
if '/' in fps_str:
    num, den = map(int, fps_str.split('/'))
    fps = num / den
else:
    fps = float(fps_str)

# Convert segment times to frame numbers
intro_start_f = int(intro_start * fps) if intro_start >= 0 else -1
intro_end_f = int(intro_end * fps) if intro_end >= 0 else -1
credits_start_f = int(credits_start * fps) if credits_start >= 0 else -1
credits_end_f = int(credits_end * fps) if credits_end >= 0 else -1

proc = subprocess.Popen([
    'ffmpeg', '-i', input_file, '-vf', vf,
    '-f', 'rawvideo', '-pix_fmt', 'gray', '-'
], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

CHUNK = 128 * 72
MAX_DUP_RUN = 6          # max consecutive duplicate frames (~200ms at 30fps)
                         # real telecine/on-twos dupes are 2-3 frames max
prev_data = None
current_unique = 1
entries = []
frame_num = 0
dup_run = 0

while True:
    data = proc.stdout.read(CHUNK)
    if len(data) < CHUNK:
        break
    frame_num += 1

    # Determine mode: skip (intro/outro) or ai (content)
    in_intro = intro_start_f >= 0 and intro_start_f <= frame_num <= intro_end_f
    in_credits = credits_start_f >= 0 and credits_start_f <= frame_num
    mode = "skip" if (in_intro or in_credits) else "ai"

    is_unique = True
    if prev_data is not None:
        diff = sum(abs(a - b) for a, b in zip(data, prev_data)) / CHUNK
        if diff < threshold:
            is_unique = False

    # Cap consecutive duplicate runs — slow pans and static shots can produce
    # hundreds of near-identical frames at low resolution; cap prevents frozen
    # video. Real telecine/on-twos duplicates never exceed 3-4 frames in a row.
    if not is_unique and mode == "ai":
        dup_run += 1
        if dup_run >= MAX_DUP_RUN:
            is_unique = True
            dup_run = 0
    else:
        dup_run = 0

    # Only ai frames can be sources for other ai frames.
    # Skip frames must never become current_unique or the consumer will wait
    # for an upscaled file that was never produced.
    if is_unique and mode == "ai":
        current_unique = frame_num
    entries.append((frame_num, current_unique, mode))
    prev_data = data

proc.wait()

unique_ai = 0
dup_count = 0
skip_count = 0
with open(map_file, 'w') as f:
    for i, (fnum, src, mode) in enumerate(entries):
        is_last = 1 if (i == len(entries) - 1 or entries[i + 1][1] != src) else 0
        f.write(f"{fnum} {src} {is_last} {mode}\n")
        if mode == "skip":
            skip_count += 1
        elif fnum == src:
            unique_ai += 1
        else:
            dup_count += 1

total = len(entries)
ai_total = unique_ai + dup_count
print(f"{unique_ai} AI-upscale, {dup_count} dedup, {skip_count} fast-scale ({(dup_count+skip_count)*100//total}% GPU saved)")
PYEOF
}

# ---- Fast-scale a frame to target resolution with lanczos ----
fast_scale_frame() {
    local src=$1 dst=$2 target_w=$3 target_h=$4
    ffmpeg -i "$src" -vf "scale=${target_w}:${target_h}:flags=lanczos" \
        "$dst" -y 2>/dev/null
}

# ---- Validate dependencies ----
for cmd in realesrgan-ncnn-vulkan ffmpeg ffprobe python3; do
    command -v "$cmd" &>/dev/null || { log "Missing: $cmd"; exit 1; }
done

# ---- Input handling ----
if [ -f "$INPUT_DIR" ]; then
    SINGLE_FILE="$INPUT_DIR"
    INPUT_DIR="$(dirname "$INPUT_DIR")"
elif [ ! -d "$INPUT_DIR" ]; then
    log "Input not found: $INPUT_DIR"
    exit 1
fi

# ---- Lock ----
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "Already running (PID $OLD_PID). Remove $LOCK_FILE if stale."
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"

mkdir -p "$OUTPUT_DIR" "$TEMP_DIR"

# ---- Find video files ----
if [ -n "${SINGLE_FILE:-}" ]; then
    FILES=("$SINGLE_FILE")
else
    mapfile -t FILES < <(find "$INPUT_DIR" -maxdepth 1 -type f \
        \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.avi' \
           -o -iname '*.mov' -o -iname '*.webm' -o -iname '*.ts' \) | sort)
fi

[ ${#FILES[@]} -eq 0 ] && { log "No video files found in $INPUT_DIR"; exit 1; }

GPU_FLAG="0"
build_vcodec_flags

# Compute target resolution
TARGET_W=$((1920 * SCALE))
TARGET_H=$((1080 * SCALE))

TOTAL=${#FILES[@]}
CURRENT=0 DONE=0 SKIPPED=0 FAILED=0

log ""
log "=========================================="
log "Anime Upscale Pipeline"
log "Input:  $INPUT_DIR ($TOTAL files)"
log "Output: $OUTPUT_DIR (.$OUTPUT_EXT)"
log "Config: ${SCALE}x $MODEL | $CODEC CRF $CRF $PRESET 10-bit"
log "GPU:    $GPU_FLAG | Dedup: $DUP_THRESHOLD | Jellyfin: $([ -n "$JELLYFIN_API_KEY" ] && echo "enabled" || echo "disabled")"
log "=========================================="

for INPUT_FILE in "${FILES[@]}"; do
    BASENAME="$(basename "$INPUT_FILE")"
    NAME="${BASENAME%.*}"
    OUTPUT_FILE="$OUTPUT_DIR/${NAME}.${OUTPUT_EXT}"
    WORK="$TEMP_DIR/${NAME}"

    CURRENT=$((CURRENT + 1))

    if [ -f "$OUTPUT_FILE" ]; then
        log "[$CURRENT/$TOTAL] $BASENAME — done, skipping"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    FREE_GB=$(df --output=avail -BG "$TEMP_DIR" | tail -1 | tr -dc '0-9')
    if [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
        log "[$CURRENT/$TOTAL] $BASENAME — ${FREE_GB}GB free < ${MIN_FREE_GB}GB. Stopping."
        FAILED=$((FAILED + 1))
        break
    fi

    [ -d "$WORK" ] && rm -rf "$WORK"
    mkdir -p "$WORK/frames" "$WORK/unique" "$WORK/upscaled_unique" "$WORK/scaled"

    log ""
    log "[$CURRENT/$TOTAL] ===== $BASENAME ====="

    # ---- 1. Detect interlacing ----
    log "[$CURRENT/$TOTAL] Detecting interlacing..."
    DEINTERLACE=$(detect_interlace "$INPUT_FILE")
    if [ "$DEINTERLACE" = "1" ]; then
        log "[$CURRENT/$TOTAL] Interlaced/telecined — will deinterlace"
        VF_EXTRACT=(-vf "yadif=mode=0:parity=0:deint=0")
    else
        log "[$CURRENT/$TOTAL] Progressive — no deinterlace needed"
        VF_EXTRACT=()
    fi

    # ---- 2. Query Jellyfin for intro/outro ----
    INTRO_START=-1; INTRO_END=-1; CREDITS_START=-1; CREDITS_END=-1
    if [ -n "$JELLYFIN_API_KEY" ]; then
        log "[$CURRENT/$TOTAL] Querying Jellyfin for intro/outro..."
        SEGMENTS=$(get_jellyfin_segments "$INPUT_FILE")
        if [ -n "$SEGMENTS" ]; then
            read -r INTRO_START INTRO_END CREDITS_START CREDITS_END <<< "$SEGMENTS"
            INTRO_MSG=""
            [ "$(echo "$INTRO_START >= 0" | bc)" -eq 1 ] && \
                INTRO_MSG="intro ${INTRO_START%.*}s–${INTRO_END%.*}s"
            CREDITS_MSG=""
            [ "$(echo "$CREDITS_START >= 0" | bc)" -eq 1 ] && \
                CREDITS_MSG="credits ${CREDITS_START%.*}s–end"
            [ -n "$INTRO_MSG" ] || [ -n "$CREDITS_MSG" ] && \
                log "[$CURRENT/$TOTAL] Segments: ${INTRO_MSG:+$INTRO_MSG }${CREDITS_MSG:+$CREDITS_MSG }(fast-scale)"
        else
            log "[$CURRENT/$TOTAL] No Jellyfin segments found — full AI upscale"
        fi
    fi

    # ---- 3. Detect duplicates + mark segments ----
    log "[$CURRENT/$TOTAL] Analyzing frames..."
    DUP_RESULT=$(detect_duplicates "$INPUT_FILE" "$WORK/frame_map.txt" \
        "$DUP_THRESHOLD" "$DEINTERLACE" \
        "$INTRO_START" "$INTRO_END" "$CREDITS_START" "$CREDITS_END")
    log "[$CURRENT/$TOTAL] $DUP_RESULT"

    # ---- 4. Extract frames ----
    log "[$CURRENT/$TOTAL] Extracting frames..."
    if ! ffmpeg -i "$INPUT_FILE" "${VF_EXTRACT[@]}" -q:v 1 \
         "$WORK/frames/f_%06d.png" -y 2>"$WORK/extract.log"; then
        log "[$CURRENT/$TOTAL] FAILED: Frame extraction error"
        log "  $(tail -2 "$WORK/extract.log")"
        FAILED=$((FAILED + 1))
        continue
    fi

    FRAME_COUNT=$(find "$WORK/frames" -name '*.png' -printf '.' | wc -c)
    FPS=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=r_frame_rate \
        -of default=noprint_wrappers=1:nokey=1 "$INPUT_FILE")

    if [ "$FRAME_COUNT" -eq 0 ]; then
        log "[$CURRENT/$TOTAL] FAILED: No frames extracted"
        FAILED=$((FAILED + 1))
        rm -rf "$WORK"
        continue
    fi

    echo "$FRAME_COUNT" > "$WORK/.total_frames"

    # ---- 5. Sanity check frame count ----
    MAP_LINES=$(wc -l < "$WORK/frame_map.txt")
    if [ "$MAP_LINES" -ne "$FRAME_COUNT" ]; then
        log "[$CURRENT/$TOTAL] Frame mismatch (map=$MAP_LINES, frames=$FRAME_COUNT) — disabling dedup"
        seq 1 "$FRAME_COUNT" | awk '{print $1, $1, 1, "ai"}' > "$WORK/frame_map.txt"
    fi

    # ---- 6. Prepare frames: AI-upscale unique + fast-scale intros/outros ----
    UNIQUE_COUNT=$(awk '$4=="ai" && $1==$2' "$WORK/frame_map.txt" | wc -l)
    SKIP_SCALED=$(awk '$4=="skip"' "$WORK/frame_map.txt" | wc -l)

    # Hard-link unique AI frames in bulk (xargs batches many per ln call, no per-frame subprocess)
    log "[$CURRENT/$TOTAL] Linking $UNIQUE_COUNT unique frames..."
    awk -v w="$WORK" '$4=="ai" && $1==$2 {printf "%s/frames/f_%06d.png\n", w, $1}' \
        "$WORK/frame_map.txt" | xargs -d'\n' ln -t "$WORK/unique/"

    # Fast-scale skip frames in parallel (all CPU cores, one ffmpeg per frame)
    if [ "$SKIP_SCALED" -gt 0 ]; then
        log "[$CURRENT/$TOTAL] Fast-scaling $SKIP_SCALED skip frames (parallel)..."
        export _FS_WORK="$WORK" _FS_TW="$TARGET_W" _FS_TH="$TARGET_H"
        awk '$4=="skip" {printf "%06d\n", $1}' "$WORK/frame_map.txt" | \
            xargs -d'\n' -P "$(nproc)" -I FNUM \
                bash -c 'ffmpeg -i "$_FS_WORK/frames/f_FNUM.png" \
                    -vf "scale=${_FS_TW}:${_FS_TH}:flags=lanczos" \
                    "$_FS_WORK/scaled/f_FNUM.png" -y 2>/dev/null'
        unset _FS_WORK _FS_TW _FS_TH
    fi

    log "[$CURRENT/$TOTAL] $FRAME_COUNT frames @ $FPS fps — AI: $UNIQUE_COUNT, fast-scale: $SKIP_SCALED (${FREE_GB}GB free)"

    # ---- 7. Upscale unique content frames (background) ----
    log "[$CURRENT/$TOTAL] Upscaling $UNIQUE_COUNT frames (${SCALE}x)..."
    realesrgan-ncnn-vulkan \
        -i "$WORK/unique" -o "$WORK/upscaled_unique" \
        -n "$MODEL" -s "$SCALE" -f png \
        -g "$GPU_FLAG" \
        > "$WORK/upscale.log" 2>&1 &
    UPSCALER_PID=$!

    # ---- 8. Stream to encoder in parallel ----
    # For each frame:
    #   mode=ai:   wait for AI-upscaled source from upscaler
    #   mode=skip: use pre-computed lanczos-scaled frame (instant)
    # Duplicate frames reuse the same upscaled PNG. IS_LAST controls cleanup.
    log "[$CURRENT/$TOTAL] Encoding ($CODEC CRF $CRF $PRESET 10-bit)..."

    # Extract audio/subs first — decouples them from the slow video pipe,
    # preventing drift when the pipe stalls waiting for upscaled frames.
    ffmpeg -i "$INPUT_FILE" -vn \
        -c:a copy -c:s copy \
        "$WORK/audio.mkv" -y 2>/dev/null

    set +eo pipefail
    (
        while IFS=' ' read -r fnum src is_last mode; do
            FNAME=$(printf "f_%06d.png" "$fnum")
            SRC_FNAME=$(printf "f_%06d.png" "$src")
            O_FILE="$WORK/frames/$FNAME"

            if [ "$mode" = "skip" ]; then
                # Fast-scaled frame (already done)
                S_FILE="$WORK/scaled/$FNAME"
                cat "$S_FILE"
                rm -f "$S_FILE" "$O_FILE"
            else
                # AI-upscaled frame
                U_FILE="$WORK/upscaled_unique/$SRC_FNAME"
                if ! wait_frame "$U_FILE" "$UPSCALER_PID"; then
                    echo "Upscaler died at frame $fnum" >&2
                    break
                fi
                cat "$U_FILE"
                rm -f "$O_FILE"
                [ "$is_last" -eq 1 ] && rm -f "$U_FILE"
            fi
            echo "$fnum" > "$WORK/.encoded_frames"
        done < "$WORK/frame_map.txt"
    ) | ffmpeg -framerate "$FPS" -f image2pipe -vcodec png -i - \
        "${VCODEC[@]}" \
        "$WORK/video.mkv" -y 2>"$WORK/encode.log"
    PIPE_EXIT=$?
    set -eo pipefail

    # Mux video + audio — both streams have clean timestamps, guaranteed sync
    if [ $PIPE_EXIT -eq 0 ]; then
        ffmpeg -i "$WORK/video.mkv" -i "$WORK/audio.mkv" \
            -map 0:v -map 1:a? -map 1:s? \
            -c:v copy -c:a copy -c:s copy \
            -movflags +faststart \
            "$WORK/out.${OUTPUT_EXT}" -y 2>/dev/null
    fi

    wait "$UPSCALER_PID" 2>/dev/null
    UPSCALE_EXIT=$?
    UPSCALER_PID=""

    # ---- 9. Validate and move output ----
    if [ $PIPE_EXIT -ne 0 ] || [ ! -f "$WORK/out.${OUTPUT_EXT}" ]; then
        FAILED=$((FAILED + 1))
        log "[$CURRENT/$TOTAL] FAILED (pipe=$PIPE_EXIT, upscaler=$UPSCALE_EXIT)"
        log "  Upscaler: $(tail -2 "$WORK/upscale.log" 2>/dev/null)"
        log "  Encoder:  $(tail -2 "$WORK/encode.log" 2>/dev/null)"
        continue
    fi

    if ! ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
         -of csv=p=0 "$WORK/out.${OUTPUT_EXT}" &>/dev/null; then
        FAILED=$((FAILED + 1))
        log "[$CURRENT/$TOTAL] FAILED: Output corrupt"
        continue
    fi

    SIZE=$(du -h "$WORK/out.${OUTPUT_EXT}" | cut -f1)
    mv "$WORK/out.${OUTPUT_EXT}" "$OUTPUT_FILE"
    rm -rf "$WORK"
    DONE=$((DONE + 1))
    log "[$CURRENT/$TOTAL] DONE — $SIZE"
done

log ""
log "=========================================="
log "COMPLETE: $DONE done, $SKIPPED skipped, $FAILED failed"
log "Output: $OUTPUT_DIR"
log "=========================================="
