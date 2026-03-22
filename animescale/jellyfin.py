"""Jellyfin Intro Skipper integration."""
import json
import os
from dataclasses import dataclass
from urllib.parse import quote
from urllib.request import urlopen


@dataclass
class Segments:
    intro_start: float = -1
    intro_end: float = -1
    credits_start: float = -1
    credits_end: float = -1

    def has_intro(self) -> bool:
        return self.intro_start >= 0

    def has_credits(self) -> bool:
        return self.credits_start >= 0

    def description(self) -> str:
        parts = []
        if self.has_intro():
            parts.append(f"intro {int(self.intro_start)}s–{int(self.intro_end)}s")
        if self.has_credits():
            parts.append(f"credits {int(self.credits_start)}s–end")
        return " ".join(parts)


def _find_item_id(filepath: str, base_url: str, api_key: str) -> str | None:
    parent_dir = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
    if not parent_dir:
        parent_dir = os.path.basename(os.path.dirname(filepath))

    url = (
        f"{base_url}/Items?api_key={api_key}"
        f"&searchTerm={quote(parent_dir)}&IncludeItemTypes=Series&Recursive=true&limit=5"
    )
    with urlopen(url) as r:
        series_data = json.load(r)

    if not series_data.get("Items"):
        return None
    series_id = series_data["Items"][0]["Id"]

    offset = 0
    while True:
        url = (
            f"{base_url}/Shows/{series_id}/Episodes"
            f"?api_key={api_key}&Fields=Path&startIndex={offset}&limit=200"
        )
        with urlopen(url) as r:
            eps_data = json.load(r)
        items = eps_data.get("Items", [])
        if not items:
            break
        for ep in items:
            if ep.get("Path", "") == filepath:
                return ep["Id"]
        offset += len(items)
        if offset >= eps_data.get("TotalRecordCount", 0):
            break
    return None


def get_segments(filepath: str, base_url: str, api_key: str) -> Segments | None:
    """Query Jellyfin for intro/credits segment timestamps. Returns None on failure."""
    try:
        item_id = _find_item_id(filepath, base_url, api_key)
        if not item_id:
            return None

        url = f"{base_url}/Episode/{item_id}/IntroSkipperSegments?api_key={api_key}"
        with urlopen(url) as r:
            data = json.load(r)

        intro = data.get("Introduction", {})
        credits = data.get("Credits", {})

        return Segments(
            intro_start=intro.get("Start", -1) if intro.get("Valid") else -1,
            intro_end=intro.get("End", -1) if intro.get("Valid") else -1,
            credits_start=credits.get("Start", -1) if credits.get("Valid") else -1,
            credits_end=credits.get("End", -1) if credits.get("Valid") else -1,
        )
    except Exception:
        return None
