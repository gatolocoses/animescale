"""Configuration for the animescale pipeline."""
from dataclasses import dataclass, field
import os


@dataclass
class Config:
    scale: int = 2                              # 2 or 4
    model: str = "realesr-animevideov3"         # 2x: realesr-animevideov3, 4x: realesrgan-x4plus-anime
    codec: str = "libx265"                      # libx264 | libx265 | libsvtav1
    crf: int = 14                               # near-transparent for anime
    preset: str = "medium"
    pix_fmt: str = "yuv420p10le"                # 10-bit eliminates banding
    output_ext: str = "mkv"
    temp_dir: str = field(default_factory=lambda: os.path.join(os.environ.get("TMPDIR", "/tmp"), "animescale"))
    min_free_gb: int = 25
    dup_threshold: float = 1.0                  # lower = stricter duplicate detection
    gpu: str = "0"

    # Jellyfin Intro Skipper — set api_key to enable
    jellyfin_url: str = "http://localhost:8096"
    jellyfin_api_key: str = ""

    @property
    def target_width(self) -> int:
        return 1920 * self.scale

    @property
    def target_height(self) -> int:
        return 1080 * self.scale
