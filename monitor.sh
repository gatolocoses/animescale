#!/bin/bash
# Anime upscale pipeline monitor — works with upscale.sh
# Auto-detects active work directory, shows dedup/intro-skip stats, GPU, temps, ETA.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMP_DIR="${TMPDIR:-/tmp}/animescale"
LOG_FILE="$SCRIPT_DIR/upscale.log"
LOCK_FILE="$SCRIPT_DIR/.upscale.lock"

format_time() {
    local s=$1
    [ -z "$s" ] || [ "$s" -lt 0 ] 2>/dev/null && s=0
    [ "$s" -lt 60 ] && echo "${s}s" && return
    [ "$s" -lt 3600 ] && echo "$((s/60))m $((s%60))s" && return
    echo "$((s/3600))h $((s%3600/60))m"
}

bar() {
    local v=$1 t=$2 w=${3:-30}
    [ "$t" -le 0 ] 2>/dev/null && t=1
    local fill=$(awk "BEGIN {x=int($v/$t*$w); if(x<0)x=0; if(x>$w)x=$w; printf \"%d\", x}")
    printf "%${fill}s" | tr ' ' '#'; printf "%$((w-fill))s" | tr ' ' '-'
}

while true; do
    clear
    NOW=$(date +%s)

    # Find active work directory
    WORK_DIR=""
    if [ -d "$TEMP_DIR" ]; then
        WORK_DIR=$(find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)
    fi

    # Detect processes
    PIPELINE_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    PIPELINE_ALIVE=""
    [ -n "$PIPELINE_PID" ] && kill -0 "$PIPELINE_PID" 2>/dev/null && PIPELINE_ALIVE=1
    UPSCALE_PID=$(pgrep -f "realesrgan-ncnn-vulkan" | head -1)
    ENCODE_PID=$(pgrep -f "ffmpeg.*image2pipe" | head -1)
    EXTRACT_PID=$(ps -C ffmpeg -o pid=,args= 2>/dev/null | grep -v image2pipe | grep "frames/f_" | awk '{print $1}' | head -1)
    SCALE_PID=$(ps -C ffmpeg -o pid=,args= 2>/dev/null | grep "lanczos" | awk '{print $1}' | head -1)

    CURRENT_LABEL=""
    [ -n "$WORK_DIR" ] && CURRENT_LABEL=$(basename "$WORK_DIR")

    echo "======================  ANIME UPSCALE PIPELINE  ======================"

    # Read output dir from last log start block
    OUTPUT_DIR=$(grep "^.*Output:" "$LOG_FILE" 2>/dev/null | tail -1 | sed 's/.*Output: *//;s/ (.*//')
    if [ -n "$OUTPUT_DIR" ] && [ -d "$OUTPUT_DIR" ]; then
        COMPLETED=$(find "$OUTPUT_DIR" -maxdepth 1 -name '*.mkv' -o -name '*.mp4' 2>/dev/null | wc -l)
        [ "$COMPLETED" -gt 0 ] && echo "  Completed:   $COMPLETED file(s) in $OUTPUT_DIR"
    fi

    if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
        TOTAL_FRAMES=$(cat "$WORK_DIR/.total_frames" 2>/dev/null || echo 0)
        ENCODED=$(cat "$WORK_DIR/.encoded_frames" 2>/dev/null || echo 0)

        # Parse frame_map stats
        AI_UNIQUE=0; DUP_COUNT=0; SKIP_COUNT=0; MAP_TOTAL=0
        if [ -f "$WORK_DIR/frame_map.txt" ]; then
            MAP_TOTAL=$(wc -l < "$WORK_DIR/frame_map.txt")
            AI_UNIQUE=$(awk '$4=="ai" && $1==$2' "$WORK_DIR/frame_map.txt" | wc -l)
            DUP_COUNT=$(awk '$4=="ai" && $1!=$2' "$WORK_DIR/frame_map.txt" | wc -l)
            SKIP_COUNT=$(awk '$4=="skip"' "$WORK_DIR/frame_map.txt" | wc -l)
            GPU_SAVED=$(( MAP_TOTAL > 0 ? (DUP_COUNT + SKIP_COUNT) * 100 / MAP_TOTAL : 0 ))
        fi

        # Count frames in each directory
        EXTRACTED=$(find "$WORK_DIR/frames" -name '*.png' 2>/dev/null | wc -l)
        UNIQUE_LINKED=$(find "$WORK_DIR/unique" -name '*.png' 2>/dev/null | wc -l)
        SCALED_DONE=$(find "$WORK_DIR/scaled" -name '*.png' 2>/dev/null | wc -l)
        UPSCALED_DONE=$(find "$WORK_DIR/upscaled_unique" -name '*.png' 2>/dev/null | wc -l)

        echo "  Episode:     $CURRENT_LABEL"
        echo ""

        # Determine stage
        if [ -n "$EXTRACT_PID" ] && [ "$EXTRACTED" -lt "$MAP_TOTAL" ] && [ "$MAP_TOTAL" -gt 0 ]; then
            # Stage: Extracting frames
            PCT=$(awk "BEGIN {printf \"%.1f\", $EXTRACTED/$MAP_TOTAL*100}")
            echo "  Stage:       EXTRACTING FRAMES"
            echo "  Extract:     [$(bar "$EXTRACTED" "$MAP_TOTAL")] ${PCT}%  ($EXTRACTED / $MAP_TOTAL)"

        elif [ -n "$SCALE_PID" ] || { [ "$UNIQUE_LINKED" -lt "$AI_UNIQUE" ] && [ -z "$UPSCALE_PID" ] && [ -z "$ENCODE_PID" ]; }; then
            # Stage: Linking unique + fast-scaling intro/outro
            echo "  Stage:       PREPARING (linking + fast-scaling)"
            [ "$AI_UNIQUE" -gt 0 ] && echo "  Unique:      [$(bar "$UNIQUE_LINKED" "$AI_UNIQUE")] $(awk "BEGIN {printf \"%.1f\", $UNIQUE_LINKED/$AI_UNIQUE*100}")%  ($UNIQUE_LINKED / $AI_UNIQUE linked)"
            [ "$SKIP_COUNT" -gt 0 ] && echo "  Fast-scale:  [$(bar "$SCALED_DONE" "$SKIP_COUNT")] $(awk "BEGIN {printf \"%.1f\", $SCALED_DONE/$SKIP_COUNT*100}")%  ($SCALED_DONE / $SKIP_COUNT lanczos)"

        elif [ -n "$UPSCALE_PID" ] || [ -n "$ENCODE_PID" ] || [ "$ENCODED" -gt 0 ]; then
            # Stage: Upscaling + Encoding in parallel
            echo "  Stage:       UPSCALING + ENCODING"

            # Upscaler progress: upscaled_unique count vs AI_UNIQUE
            # Account for frames already consumed (deleted by encoder)
            UPSCALE_TOTAL=$AI_UNIQUE
            if [ "$UPSCALE_TOTAL" -gt 0 ]; then
                # Frames done = upscaled still on disk + already consumed by encoder
                # Consumed AI frames = encoded frames that were AI mode, roughly:
                # encoded - skip frames consumed so far. Approximate with ratio.
                UPSCALE_PROGRESS=$UPSCALED_DONE
                if [ "$ENCODED" -gt 0 ] && [ "$MAP_TOTAL" -gt 0 ]; then
                    # Count how many AI-unique frames the encoder has consumed (deleted)
                    AI_CONSUMED=$(head -n "$ENCODED" "$WORK_DIR/frame_map.txt" 2>/dev/null | awk '$4=="ai" && $1==$2' | wc -l)
                    UPSCALE_PROGRESS=$((UPSCALED_DONE + AI_CONSUMED))
                    [ "$UPSCALE_PROGRESS" -gt "$UPSCALE_TOTAL" ] && UPSCALE_PROGRESS=$UPSCALE_TOTAL
                fi
                echo "  Upscale:     [$(bar "$UPSCALE_PROGRESS" "$UPSCALE_TOTAL")] $(awk "BEGIN {printf \"%.1f\", $UPSCALE_PROGRESS/$UPSCALE_TOTAL*100}")%  ($UPSCALE_PROGRESS / $UPSCALE_TOTAL AI frames)  pending: $UPSCALED_DONE"
            fi

            # Encoder progress
            if [ "$MAP_TOTAL" -gt 0 ]; then
                ENC_PCT=$(awk "BEGIN {printf \"%.1f\", $ENCODED/$MAP_TOTAL*100}")
                OUT_FILE=$(find "$WORK_DIR" -maxdepth 1 -name 'out.*' 2>/dev/null | head -1)
                OUT_SIZE="0MB"
                [ -n "$OUT_FILE" ] && OUT_SIZE="$(du -m "$OUT_FILE" 2>/dev/null | cut -f1)MB"
                echo "  Encode:      [$(bar "$ENCODED" "$MAP_TOTAL")] ${ENC_PCT}%  ($ENCODED / $MAP_TOTAL)  $OUT_SIZE"
            fi

            # ETA based on encode progress
            if [ "$ENCODED" -gt 0 ] && [ "$MAP_TOTAL" -gt 0 ]; then
                if [ ! -f "$WORK_DIR/.encode_start" ]; then
                    echo "$NOW" > "$WORK_DIR/.encode_start"
                fi
                ENCODE_START=$(cat "$WORK_DIR/.encode_start" 2>/dev/null || echo "$NOW")
                ELAPSED=$((NOW - ENCODE_START))
                if [ "$ELAPSED" -gt 0 ]; then
                    RATE=$(awk "BEGIN {printf \"%.2f\", $ENCODED/$ELAPSED}")
                    REMAINING=$((MAP_TOTAL - ENCODED))
                    ETA_SECS=$(awk "BEGIN {printf \"%.0f\", $REMAINING/$RATE}")
                    echo "               Elapsed: $(format_time $ELAPSED)  |  ETA: $(format_time "$ETA_SECS")  |  Rate: $RATE fps"
                fi
            fi
        else
            # Idle or between episodes
            if [ -n "$PIPELINE_ALIVE" ]; then
                echo "  Stage:       Running (detecting...)"
            else
                echo "  Stage:       Idle"
            fi
        fi

        # Dedup / skip stats
        if [ "$MAP_TOTAL" -gt 0 ]; then
            echo ""
            echo "  Dedup:       $AI_UNIQUE AI  |  $DUP_COUNT dedup  |  $SKIP_COUNT intro/outro skip  (${GPU_SAVED}% GPU saved)"
        fi

        # PIDs
        echo ""
        PIDS="Pipeline: ${PIPELINE_PID:-—}"
        [ -n "$UPSCALE_PID" ] && PIDS="$PIDS  Upscaler: $UPSCALE_PID"
        [ -n "$ENCODE_PID" ] && PIDS="$PIDS  Encoder: $ENCODE_PID"
        [ -n "$EXTRACT_PID" ] && PIDS="$PIDS  Extract: $EXTRACT_PID"
        echo "  PIDs:        $PIDS"

    else
        # No work directory
        if [ -n "$PIPELINE_ALIVE" ]; then
            echo "  Status:      Running (no work dir yet...)"
        else
            echo "  Status:      Idle"
        fi
    fi

    echo ""
    echo "-------------------------  SYSTEM  -------------------------"
    IGPU=$(cat /sys/class/drm/card0/device/gpu_busy_percent 2>/dev/null || echo "?")
    GPU1=$(cat /sys/class/drm/card1/device/gpu_busy_percent 2>/dev/null || echo "?")
    echo "  GPU:   iGPU ${IGPU}%  |  RX 6650 XT ${GPU1}%"
    echo "  Temp:  CPU $(sensors 2>/dev/null | grep -m1 "Tctl:" | awk '{print $2}')  |  GPU $(sensors 2>/dev/null | grep "junction:" | tail -1 | awk '{print $2}')"
    FREE=$(df -h "$TEMP_DIR" 2>/dev/null | tail -1 | awk '{print $4}')
    USED=$(du -sh "$TEMP_DIR" 2>/dev/null | cut -f1)
    echo "  NVMe:  ${USED:-0} used  |  ${FREE:-?} free"
    echo ""
    echo "  Log:   $(tail -1 "$LOG_FILE" 2>/dev/null | cut -c1-75)"
    echo ""
    echo "=================================================  $(date '+%H:%M:%S')  5s"
    sleep 5
done
