"""
pipeline.py — YouTube long-form to short-form automation pipeline.

GitHub-ready version:
- API keys are read from environment variables or .env
- configuration lives in config.json
- generated files are written to out/ and tmp/

Python 3.11+ recommended.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import os
import secrets
import shutil
import subprocess
import sys
import textwrap
import time
import random
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from groq import Groq
from google import genai
from pydantic import BaseModel, Field
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TaskProgressColumn,
)

# LOGGING
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)
console = Console()

# Ensure ffmpeg (installed via winget) is on PATH even before a new shell session
_FFMPEG_WINGET = Path(os.environ.get("LOCALAPPDATA", "")) / (
    "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    "/ffmpeg-8.1-full_build/bin"
)
if _FFMPEG_WINGET.is_dir() and str(_FFMPEG_WINGET) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = str(_FFMPEG_WINGET) + os.pathsep + os.environ.get("PATH", "")


# ──────────────────────────────────────────────────────────────────────────────
# FILE PATHS
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR            = Path(__file__).resolve().parent


def load_env_file(path: Path = BASE_DIR / ".env") -> None:
    """Load simple KEY=VALUE lines from .env without requiring extra packages."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

CONFIG_FILE         = BASE_DIR / "config.json"
RAW_CANDIDATES_FILE = BASE_DIR / "raw_candidates.json"
SELECTED_FILE       = BASE_DIR / "selected_videos.json"
USED_VIDEOS_FILE    = BASE_DIR / "used_videos.json"
CACHE_FILE          = BASE_DIR / "channel_cache.json"
LAST_EDIT_RESULT    = BASE_DIR / "last_edit_result.json"
REELSINFO_PATH      = BASE_DIR / "reelsinfo.json"
POST_EXE_PATH       = BASE_DIR / "dist" / "post.exe"
LOGO_PATH           = BASE_DIR / "assets" / "logo.png"
TITLE_FONT_FILE     = BASE_DIR / "assets" / "fonts" / "Poppins-SemiBold.ttf"
PART_FONT_FILE      = BASE_DIR / "assets" / "fonts" / "BebasNeue-Regular.ttf"


# ──────────────────────────────────────────────────────────────────────────────
# FIND-VIDEOS CONFIG
# ──────────────────────────────────────────────────────────────────────────────
MAX_FETCH_WORKERS    = 10
MAX_DETAIL_WORKERS   = 8
CACHE_TTL_HOURS      = 6
GEMINI_DESC_CHARS    = 400


# ──────────────────────────────────────────────────────────────────────────────
# EDIT-VIDEOS CONFIG
# ──────────────────────────────────────────────────────────────────────────────
OUT_DIR             = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

MIN_SHORT_SECONDS   = 60
MAX_SHORT_SECONDS   = 120
MAX_SHORTS          = 14
MAX_TOTAL_SEGMENTS  = 100

WHISPER_LANG        = "en"
WHISPER_MODEL       = "small"

GROQ_MODELS: list[str] = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]
# Set via environment variable for security; fallback to empty string
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "").strip()
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_PLAN_MODELS  = ["gemini-2.0-flash", "gemini-1.5-flash"]

GROQ_MAX_TRANSCRIPT_CHARS = 40_000

PART_TEXT_PREFIX    = "PART "
CENTER_H            = 1100
TITLE_FONT_SIZE     = 86
TITLE_MAX_CHARS     = 23
SUB_FONT_SIZE       = 64
SUB_MAX_CHARS_PER_LINE = 28

LOGO_SIZE           = 140
LOGO_MARGIN_X       = 50
LOGO_MARGIN_Y       = 250
LOGO_OPACITY        = 0.6

CLEAN_TMP_AFTER     = True
VIDEO_EXTS          = (".mp4", ".mkv", ".mov", ".avi", ".webm")
SUBTITLES_ENABLED   = True
USE_HWACCEL         = True
YTDLP_COOKIE_BROWSER = os.environ.get("YTDLP_COOKIE_BROWSER", "").strip().lower()


# ──────────────────────────────────────────────────────────────────────────────
# PROGRESS FACTORY
# ──────────────────────────────────────────────────────────────────────────────
def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GENERIC HELPERS
# ──────────────────────────────────────────────────────────────────────────────
_WS_RE    = re.compile(r"\s+")
_QUOTE_RE = re.compile(r'["\']')


def norm_text(text: str) -> str:
    return _WS_RE.sub(" ", text.lower()).strip() if text else ""


def clean_short_title(title: str, max_len: int = 44) -> str:
    title = _WS_RE.sub(" ", (title or "")).strip()
    title = _QUOTE_RE.sub("", title)
    if len(title) <= max_len:
        return title
    trimmed = title[:max_len].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0].rstrip()
    return trimmed[:max_len].rstrip()


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w\-_\. ]", "_", s, flags=re.UNICODE).strip()
    return s or "video"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def make_unique_5digit_id(base_dir: Path) -> str:
    for _ in range(2_000):
        folder_id = f"{secrets.randbelow(100_000):05d}"
        if not (base_dir / folder_id).exists():
            return folder_id
    raise RuntimeError("Cannot generate unique 5-digit ID.")


def run_cmd(cmd: list, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(x) for x in cmd]
    if capture:
        return subprocess.run(cmd, check=check, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, check=check,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ffmpeg_hwaccel() -> list:
    return ["-hwaccel", "auto"] if USE_HWACCEL else []


def seconds_to_srt(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    hh, ms = divmod(ms, 3_600_000)
    mm, ms = divmod(ms, 60_000)
    ss, ms = divmod(ms, 1_000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def parse_subtitles_flag(value: str, default: bool = True) -> bool:
    if not value:
        return default
    v = value.strip().lower()
    if v in {"s", "si", "si\u0300", "y", "yes", "true", "1"}:
        return True
    if v in {"n", "no", "false", "0"}:
        return False
    return default


# ──────────────────────────────────────────────────────────────────────────────
# USED-VIDEOS I/O  (saved only after successful processing)
# ──────────────────────────────────────────────────────────────────────────────
def load_used_urls(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def load_used_url_set(path: Path) -> set[str]:
    return set(load_used_urls(path))


def save_used_urls(path: Path, urls: list[str] | set[str]) -> None:
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        u = str(u).strip()
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")


def append_used_url(path: Path, url: str) -> None:
    """Atomically append a single URL — called only after a successful edit."""
    existing = load_used_urls(path)
    url = url.strip()
    if url and url not in existing:
        existing.append(url)
    save_used_urls(path, existing)


# ──────────────────────────────────────────────────────────────────────────────
# CHANNEL CACHE
# ──────────────────────────────────────────────────────────────────────────────
def load_channel_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_channel_cache(path: Path, cache: dict) -> None:
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_cached_channel(cache: dict, key: str) -> list[dict] | None:
    entry = cache.get(key)
    if not entry:
        return None
    age_h = (time.time() - float(entry.get("timestamp", 0))) / 3600
    if age_h > CACHE_TTL_HOURS:
        return None
    return entry.get("videos", [])


def set_cached_channel(cache: dict, key: str, videos: list[dict]) -> None:
    cache[key] = {"timestamp": time.time(), "videos": videos}


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    cfg: dict = json.loads(path.read_text(encoding="utf-8"))
    for key in ("channels", "filters", "search", "ranking", "gemini"):
        if key not in cfg:
            raise ValueError(f"Missing key '{key}' in config.json")
    if not isinstance(cfg["channels"], list) or not cfg["channels"]:
        raise ValueError("'channels' must be a non-empty list")

    cfg["search"].setdefault("per_channel_results", 40)
    cfg["search"].setdefault("candidate_pool_size", 100)
    cfg["search"].setdefault("final_target", 40)
    cfg["search"].setdefault("random_seed", None)
    cfg["search"].setdefault("max_pool_per_channel", 25)
    cfg["search"].setdefault("max_final_per_channel", 12)

    cfg["filters"].setdefault("min_duration_sec", 240)
    cfg["filters"].setdefault("max_duration_sec", 3600)

    cfg["ranking"].setdefault("preferred_duration_min_sec", 480)
    cfg["ranking"].setdefault("preferred_duration_max_sec", 1800)

    cfg["gemini"].setdefault("enabled", True)
    cfg["gemini"].setdefault("model", "gemini-2.5-flash")
    cfg["gemini"].setdefault("api_key", os.environ.get("GEMINI_API_KEY", ""))

    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# LOCAL RANKING
# ──────────────────────────────────────────────────────────────────────────────
def duration_score(dur: int, pref_min: int, pref_max: int) -> float:
    if dur <= 0:
        return 0.0
    if pref_min <= dur <= pref_max:
        return 20.0
    if dur < pref_min:
        return max(0.0, 20.0 - (pref_min - dur) / 15.0)
    return max(0.0, 20.0 - (dur - pref_max) / 60.0)


def views_score(views: int) -> float:
    if not views or views <= 0:
        return 0.0
    return min(10.0, math.log10(views + 1) * 2.0)


def local_fitness_score(video: dict, cfg: dict) -> tuple[float, list[str]]:
    dur   = int(video.get("duration") or 0)
    views = int(video.get("view_count") or 0)
    desc  = (video.get("description") or "").strip()

    min_dur = int(cfg["filters"]["min_duration_sec"])
    max_dur = int(cfg["filters"]["max_duration_sec"])

    if dur and dur < min_dur:
        return -9999.0, ["too_short"]
    if dur and dur > max_dur:
        return -9999.0, ["too_long"]

    score: float = 0.0
    reasons: list[str] = []

    ds = duration_score(dur,
                        int(cfg["ranking"]["preferred_duration_min_sec"]),
                        int(cfg["ranking"]["preferred_duration_max_sec"]))
    score += ds
    if ds > 0:
        reasons.append(f"duration:{ds:.2f}")

    vs = views_score(views)
    score += vs
    if vs > 0:
        reasons.append(f"views:{vs:.2f}")

    dl = len(desc)
    if dl >= 80:
        score += 4.0; reasons.append("rich_description")
    elif dl >= 20:
        score += 2.0; reasons.append("has_description")

    meta = sum([bool(video.get("title")), bool(video.get("channel")),
                bool(video.get("upload_date")), views > 0, dur > 0])
    score += meta * 1.2
    reasons.append(f"metadata:{meta}")

    return score, reasons


# ──────────────────────────────────────────────────────────────────────────────
# YOUTUBE SCRAPING
# ──────────────────────────────────────────────────────────────────────────────
class _SilentLogger:
    def debug(self, _: str) -> None: pass
    def info(self, _: str) -> None: pass
    def warning(self, _: str) -> None: pass
    def error(self, _: str) -> None: pass


def _ydl_flat_opts() -> dict:
    return {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": True, "noplaylist": False,
        "nocheckcertificate": True, "ignoreerrors": True,
        "playlistend": 200, "logger": _SilentLogger(),
        "socket_timeout": 20, "retries": 3, "file_access_retries": 2,
    }


def _ydl_detail_opts() -> dict:
    return {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": False, "noplaylist": True,
        "nocheckcertificate": True, "ignoreerrors": True,
        "logger": _SilentLogger(), "socket_timeout": 15,
        "retries": 2, "file_access_retries": 1,
    }


def normalize_channel_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return value
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("@"):
        return f"https://www.youtube.com/{value}/videos"
    return f"https://www.youtube.com/{value}"


def fetch_video_detail(video_id: str, url: str, source_channel: str) -> dict | None:
    try:
        with yt_dlp.YoutubeDL(_ydl_detail_opts()) as ydl:
            e = ydl.extract_info(url, download=False)
        if not e:
            return None
        return {
            "id":             video_id,
            "title":          e.get("title") or "",
            "url":            e.get("webpage_url") or url,
            "description":    e.get("description") or "",
            "duration":       e.get("duration") or 0,
            "view_count":     e.get("view_count") or 0,
            "channel":        e.get("channel") or e.get("uploader") or "",
            "uploader":       e.get("uploader") or "",
            "upload_date":    e.get("upload_date") or "",
            "source_channel": source_channel,
        }
    except Exception as exc:
        log.debug("fetch_video_detail failed for %s: %s", video_id, exc)
        return None


def fetch_videos_for_channel(
    channel_value: str,
    per_channel_results: int,
    min_dur: int,
    max_dur: int,
    cache: dict,
    cache_path: Path,
) -> list[dict]:
    cached = get_cached_channel(cache, channel_value)
    if cached is not None:
        console.print(f"[dim]Cache hit:[/dim] {channel_value[:50]}")
        return cached

    channel_url = normalize_channel_url(channel_value)
    try:
        with yt_dlp.YoutubeDL(_ydl_flat_opts()) as ydl:
            data = ydl.extract_info(channel_url, download=False)
    except Exception as exc:
        log.warning("Flat fetch failed for %s: %s", channel_value, exc)
        return []

    if not data:
        return []

    candidates: list[tuple[str, str]] = []
    for e in (data.get("entries") or []):
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        url = e.get("url") or e.get("webpage_url")
        if not url or not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        approx = e.get("duration")
        if approx:
            approx = int(approx)
            if approx < min_dur or approx > max_dur:
                continue
        candidates.append((vid, url))

    random.shuffle(candidates)
    candidates = candidates[:per_channel_results]
    if not candidates:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_DETAIL_WORKERS) as ex:
        futures = {ex.submit(fetch_video_detail, vid, url, channel_value): vid
                   for vid, url in candidates}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)

    set_cached_channel(cache, channel_value, results)
    save_channel_cache(cache_path, cache)
    return results


def fetch_all_channels(
    channels: list[str],
    per_channel_results: int,
    min_dur: int,
    max_dur: int,
    cache: dict,
    cache_path: Path,
    progress: Progress,
    task_id: int,
) -> list[dict]:
    lock_results: list[list[dict] | None] = [None] * len(channels)
    with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(channels))) as ex:
        future_to_idx = {
            ex.submit(fetch_videos_for_channel,
                      ch, per_channel_results, min_dur, max_dur, cache, cache_path): i
            for i, ch in enumerate(channels)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                lock_results[idx] = fut.result()
            except Exception:
                lock_results[idx] = []
            progress.update(task_id,
                            description=f"Channel done: {channels[idx][:40]}",
                            advance=1)

    all_results: list[dict] = []
    for r in lock_results:
        if r:
            all_results.extend(r)
    return all_results


# ──────────────────────────────────────────────────────────────────────────────
# DEDUP + BALANCE
# ──────────────────────────────────────────────────────────────────────────────
def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for c in candidates:
        vid = c["id"]
        if vid not in seen:
            seen[vid] = c
        else:
            old = seen[vid]
            if (len(c.get("description") or "") > len(old.get("description") or "")
                    or int(c.get("view_count") or 0) > int(old.get("view_count") or 0)):
                seen[vid] = c
    return list(seen.values())


def _sort_by(vids: list[dict], key: str) -> list[dict]:
    return sorted(vids,
                  key=lambda x: (float(x.get(key, 0) or 0),
                                 int(x.get("view_count", 0)),
                                 len(x.get("description", "") or "")),
                  reverse=True)


def _group_by_channel(videos: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for v in videos:
        k = str(v.get("source_channel") or v.get("channel") or "unknown").strip()
        g[k].append(v)
    return dict(g)


def balanced_pool_selection(videos: list[dict], target: int, max_per_ch: int) -> list[dict]:
    grouped  = {ch: _sort_by(vids, "local_score") for ch, vids in _group_by_channel(videos).items()}
    selected: list[dict] = []
    counts:   dict[str, int] = defaultdict(int)
    ptrs:     dict[str, int] = {ch: 0 for ch in grouped}

    while len(selected) < target:
        added = False
        for ch, vids in grouped.items():
            if len(selected) >= target:
                break
            p = ptrs[ch]
            if counts[ch] >= max_per_ch or p >= len(vids):
                continue
            selected.append(vids[p])
            ptrs[ch] += 1
            counts[ch] += 1
            added = True
        if not added:
            break
    return selected


def enforce_channel_balance(videos: list[dict], target: int, max_per_ch: int,
                             score_key: str = "gemini_score") -> list[dict]:
    grouped  = {ch: _sort_by(vids, score_key) for ch, vids in _group_by_channel(videos).items()}
    selected: list[dict] = []
    counts:   dict[str, int] = defaultdict(int)
    ptrs:     dict[str, int] = {ch: 0 for ch in grouped}

    while len(selected) < target:
        added = False
        for ch, vids in grouped.items():
            if len(selected) >= target:
                break
            p = ptrs[ch]
            if counts[ch] >= max_per_ch or p >= len(vids):
                continue
            selected.append(vids[p])
            ptrs[ch] += 1
            counts[ch] += 1
            added = True
        if not added:
            break

    if len(selected) < target:
        selected_ids = {v["id"] for v in selected}
        leftovers = sorted(
            (v for vids in grouped.values() for v in vids if v["id"] not in selected_ids),
            key=lambda x: (float(x.get(score_key, 0) or 0),
                           float(x.get("local_score", 0.0)),
                           int(x.get("view_count", 0))),
            reverse=True,
        )
        for v in leftovers:
            if len(selected) >= target:
                break
            selected.append(v)

    return selected[:target]


def score_candidates_parallel(candidates: list[dict], cfg: dict,
                               progress: Progress, task_id: int) -> list[dict]:
    scored: list[dict] = []

    def _one(v: dict) -> dict | None:
        s, reasons = local_fitness_score(v, cfg)
        if s <= -9999:
            return None
        return {**v, "local_score": s, "local_reasons": reasons}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_one, v): v for v in candidates}
        for fut in as_completed(futures):
            r = fut.result()
            if r is not None:
                scored.append(r)
            progress.advance(task_id)

    return scored


# ──────────────────────────────────────────────────────────────────────────────
# GEMINI (find-videos selection)
# ──────────────────────────────────────────────────────────────────────────────
class SelectedVideo(BaseModel):
    id:          str = Field(description="YouTube video id")
    score:       int = Field(description="Score 1-100")
    reason:      str = Field(description="Why this video works for shorts")
    short_angle: str = Field(description="Suggested hook/angle")
    short_title: str = Field(description="Catchy title max 44 chars")


class GeminiSelection(BaseModel):
    selected: list[SelectedVideo]


_GEMINI_RETRY = frozenset(["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                            "408", "DEADLINE_EXCEEDED", "INTERNAL", "500", "502", "504"])


def _is_gemini_retryable(exc: Exception) -> bool:
    return any(s in str(exc).upper() for s in _GEMINI_RETRY)


def _compact_for_gemini(v: dict) -> dict:
    return {
        "id":             v["id"],
        "title":          v.get("title", ""),
        "url":            v.get("url", ""),
        "description":    (v.get("description", "") or "")[:GEMINI_DESC_CHARS],
        "duration_sec":   int(v.get("duration") or 0),
        "view_count":     int(v.get("view_count") or 0),
        "channel":        v.get("channel", ""),
        "upload_date":    v.get("upload_date", ""),
        "source_channel": v.get("source_channel", ""),
        "local_score":    round(float(v.get("local_score", 0.0)), 2),
    }


def gemini_generate_with_retry(client: genai.Client, model: str, prompt: str,
                                progress: Progress | None = None,
                                task_id: int | None = None):
    attempt = 0
    while True:
        attempt += 1
        try:
            if progress and task_id is not None:
                progress.update(task_id, description=f"Gemini attempt {attempt}...",
                                total=100, completed=35)
            return client.models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json",
                        "response_schema": GeminiSelection},
            )
        except Exception as exc:
            if not _is_gemini_retryable(exc):
                raise
            delay = min(120.0, 2 ** min(attempt, 7) + random.uniform(0, 2))
            console.print(f"[yellow]Gemini overload (attempt {attempt}) — retry in {delay:.1f}s[/yellow]")
            time.sleep(delay)


def gemini_pick_best_videos(candidates: list[dict], cfg: dict,
                             progress: Progress | None = None,
                             task_id: int | None = None) -> list[dict]:
    target     = int(cfg["search"]["final_target"])
    model_name = cfg["gemini"]["model"]
    api_key    = (cfg["gemini"].get("api_key") or GEMINI_API_KEY).strip()
    if not api_key:
        raise ValueError("Missing Gemini API key. Set GEMINI_API_KEY in .env or gemini.api_key in config.json")

    if progress and task_id is not None:
        progress.update(task_id, description="Preparing Gemini payload...", total=100, completed=15)

    compact = [_compact_for_gemini(v) for v in candidates]
    prompt = (
        f"You are selecting YouTube videos perfect for viral Shorts.\n"
        f"Goal: select exactly {target} videos.\n"
        "Rules for short_title: max 44 chars, catchy, no hashtags/emojis/quotes.\n"
        "Return only valid JSON, no markdown.\n\n"
        f"Candidate videos:\n{json.dumps(compact, ensure_ascii=False)}"
    )

    if progress and task_id is not None:
        progress.update(task_id, description="Gemini selecting...", completed=35)

    client   = genai.Client(api_key=api_key)
    response = gemini_generate_with_retry(client, model_name, prompt, progress, task_id)

    if progress and task_id is not None:
        progress.update(task_id, description="Parsing Gemini response...", completed=80)

    parsed = response.parsed
    if parsed is None:
        raise RuntimeError("Gemini returned no valid structured JSON.")

    chosen = {item.id: item for item in parsed.selected}
    final: list[dict] = []
    for v in candidates:
        c = chosen.get(v["id"])
        if c:
            final.append({**v,
                          "gemini_score": int(c.score),
                          "gemini_reason": c.reason,
                          "short_angle":   c.short_angle,
                          "short_title":   clean_short_title(c.short_title, 44)})

    final.sort(key=lambda x: (int(x.get("gemini_score", 0)),
                               float(x.get("local_score", 0.0)),
                               int(x.get("view_count", 0))), reverse=True)

    if progress and task_id is not None:
        progress.update(task_id, description="Gemini selection done", completed=100)

    return final[:target]


# ──────────────────────────────────────────────────────────────────────────────
# FIND-VIDEOS PIPELINE
# ──────────────────────────────────────────────────────────────────────────────
def run_find_videos(cfg: dict) -> list[dict]:
    """Fetch → score → Gemini pick → save selected_videos.json.
    Does NOT touch used_videos.json (updated only after successful edits)."""
    channels         = cfg["channels"]
    per_ch           = int(cfg["search"]["per_channel_results"])
    pool_size        = int(cfg["search"]["candidate_pool_size"])
    final_target     = int(cfg["search"]["final_target"])
    seed             = cfg["search"].get("random_seed")
    max_pool_per_ch  = int(cfg["search"]["max_pool_per_channel"])
    max_final_per_ch = int(cfg["search"]["max_final_per_channel"])
    min_dur          = int(cfg["filters"]["min_duration_sec"])
    max_dur          = int(cfg["filters"]["max_duration_sec"])

    if seed is not None:
        random.seed(seed)

    used_urls = load_used_url_set(USED_VIDEOS_FILE)
    cache     = load_channel_cache(CACHE_FILE)

    console.print(f"[bold]Channels:[/bold] {len(channels)}")
    console.print(f"[bold]Already used:[/bold] {len(used_urls)}")

    with make_progress() as progress:
        task_ch = progress.add_task("Reading channels...", total=len(channels))
        all_candidates = fetch_all_channels(
            channels, per_ch, min_dur, max_dur, cache, CACHE_FILE, progress, task_ch)

        task_dd = progress.add_task("Deduplicating...", total=100)
        unique = dedupe_candidates(all_candidates)
        unique = [v for v in unique if (v.get("url") or "").strip() not in used_urls]
        progress.update(task_dd, completed=100, description="Dedup done")

        task_sc = progress.add_task("Local scoring...", total=max(1, len(unique)))
        scored  = score_candidates_parallel(unique, cfg, progress, task_sc)

        task_sort = progress.add_task("Balancing candidates...", total=100)
        random.shuffle(scored)
        scored.sort(key=lambda x: (float(x.get("local_score", 0.0)),
                                   int(x.get("view_count", 0)),
                                   len(x.get("description", "") or "")), reverse=True)
        top = balanced_pool_selection(scored, target=pool_size, max_per_ch=max_pool_per_ch)
        progress.update(task_sort, completed=100, description="Balancing done")

        RAW_CANDIDATES_FILE.write_text(
            json.dumps(top, ensure_ascii=False, indent=2), encoding="utf-8")

        if not top:
            console.print("[yellow]No candidates after filters.[/yellow]")
            return []

        if not cfg["gemini"]["enabled"]:
            raise RuntimeError("Gemini must be enabled (set gemini.enabled = true in config).")

        task_gem = progress.add_task("Gemini final selection...", total=100)
        final    = gemini_pick_best_videos(top, cfg, progress, task_gem)
        final    = enforce_channel_balance(final, final_target, max_final_per_ch)

        output: list[dict] = []
        for v in final:
            url = (v.get("url") or "").strip()
            output.append({
                "title":          v.get("title", ""),
                "url":            url,
                "description":    v.get("description", ""),
                "duration_sec":   int(v.get("duration", 0) or 0),
                "view_count":     int(v.get("view_count", 0) or 0),
                "channel":        v.get("channel", ""),
                "source_channel": v.get("source_channel", ""),
                "local_score":    round(float(v.get("local_score", 0.0)), 2),
                "gemini_score":   v.get("gemini_score"),
                "gemini_reason":  v.get("gemini_reason"),
                "short_angle":    v.get("short_angle"),
                "short_title":    clean_short_title(v.get("short_title", ""), 44),
            })

        SELECTED_FILE.write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]✅ selected_videos.json written ({len(output)} videos)[/green]")

    return output


# ──────────────────────────────────────────────────────────────────────────────
# MEDIA HELPERS (edit-videos)
# ──────────────────────────────────────────────────────────────────────────────
def download_youtube(url: str, tmp_dir: Path,
                     progress: Progress | None = None,
                     task_id: int | None = None) -> Path:
    outtmpl = str(tmp_dir / "source.%(ext)s")

    def hook(d: dict) -> None:
        if not progress or task_id is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done  = d.get("downloaded_bytes", 0)
            if total and total > 0:
                progress.update(task_id, total=100, completed=done / total * 100,
                                description="Downloading video...")
        elif d.get("status") == "finished":
            progress.update(task_id, total=100, completed=100,
                            description="Download done, post-processing...")

    _ffmpeg = shutil.which("ffmpeg")
    opts = {
        "format": "bv*+ba/b" if _ffmpeg else "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "logger": _SilentLogger(),
        "progress_hooks": [hook],
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
    }
    if YTDLP_COOKIE_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIE_BROWSER,)
    if _ffmpeg:
        opts["ffmpeg_location"] = str(Path(_ffmpeg).parent)

    if progress and task_id is not None:
        progress.update(task_id, total=100, completed=0, description="Starting download...")

    def _download(o: dict) -> None:
        try:
            with yt_dlp.YoutubeDL(o) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "cookiesfrombrowser" in o and ("Could not copy" in msg or "failed to load cookies" in msg):
                console.print("[yellow]Browser cookies unavailable — retrying without cookies.[/yellow]")
                o2 = {k: v for k, v in o.items() if k != "cookiesfrombrowser"}
                with yt_dlp.YoutubeDL(o2) as ydl:
                    ydl.download([url])
            else:
                raise

    _download(opts)

    if progress and task_id is not None:
        progress.update(task_id, total=100, completed=100, description="Video downloaded")

    for p in tmp_dir.iterdir():
        if p.name.startswith("source.") and p.suffix.lower() in VIDEO_EXTS:
            return p
    for p in tmp_dir.iterdir():
        if p.suffix.lower() == ".mp4":
            return p

    raise FileNotFoundError("Downloaded video not found in tmp/.")


def get_duration(video_path: Path) -> float:
    cmd  = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(video_path)]
    out  = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    return float(data["format"]["duration"])


def extract_audio(source: Path, tmp_dir: Path,
                  progress: Progress | None = None,
                  task_id: int | None = None) -> Path:
    audio = tmp_dir / "full_audio.wav"
    if progress and task_id is not None:
        progress.update(task_id, description="Extracting audio...", total=None)
    run_cmd(["ffmpeg", "-y", *ffmpeg_hwaccel(), "-i", str(source), "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
    if not audio.exists():
        raise FileNotFoundError(f"Audio not found: {audio}")
    if progress and task_id is not None:
        progress.update(task_id, description="Audio extracted", total=100, completed=100)
    return audio


def whisper_transcribe(audio: Path, tmp_dir: Path,
                       progress: Progress | None = None,
                       task_id: int | None = None) -> Path:
    cmd = ["whisper", str(audio), "--model", WHISPER_MODEL,
           "--language", WHISPER_LANG, "--output_dir", str(tmp_dir),
           "--output_format", "srt"]
    if progress and task_id is not None:
        progress.update(task_id, description=f"Transcribing {audio.name}...", total=None)
    run_cmd(cmd)
    srt = tmp_dir / (audio.stem + ".srt")
    if not srt.exists():
        raise FileNotFoundError(f"SRT not found: {srt}")
    if progress and task_id is not None:
        progress.update(task_id, description="Transcription done", total=100, completed=100)
    return srt


def detect_scene_cuts(source: Path,
                      progress: Progress | None = None,
                      task_id: int | None = None,
                      threshold: float = 0.35) -> list[float]:
    if progress and task_id is not None:
        progress.update(task_id, description="Detecting scenes...", total=100, completed=5)

    cmd  = ["ffmpeg", "-i", str(source),
            "-filter:v", f"select='gt(scene,{threshold})',metadata=print",
            "-an", "-f", "null", "-"]
    proc = subprocess.run([str(x) for x in cmd], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, check=False)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    pts  = sorted({float(m.group(1))
                   for m in re.finditer(r"pts_time:([0-9]+\.[0-9]+)", text)})

    if progress and task_id is not None:
        progress.update(task_id, completed=100, description="Scenes detected")
    return pts


# ──────────────────────────────────────────────────────────────────────────────
# SRT / ASS HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def srt_time_to_seconds(t: str) -> float:
    hh, mm, rest = t.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt_blocks(srt_path: Path) -> list[dict]:
    raw    = srt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", raw.strip(), flags=re.M)
    items: list[dict] = []
    for block in blocks:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        if len(lines) < 3:
            continue
        tl = next((x for x in lines if "-->" in x), None)
        if not tl:
            continue
        s_str, e_str = [x.strip() for x in tl.split("-->")]
        start = srt_time_to_seconds(s_str)
        end   = srt_time_to_seconds(e_str)
        text  = re.sub(r"\s+", " ", " ".join(lines[lines.index(tl) + 1:])).strip()
        if text:
            items.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return items


def build_segment_srt(items: list[dict], seg_start: float,
                      seg_end: float, srt_path: Path) -> None:
    lines: list[str] = []
    idx = 1
    for item in items:
        s, e = float(item["start"]), float(item["end"])
        text = str(item["text"]).strip()
        if not text or e <= seg_start or s >= seg_end:
            continue
        cs = max(s, seg_start) - seg_start
        ce = min(e, seg_end)   - seg_start
        if ce <= cs:
            continue
        lines += [str(idx), f"{seconds_to_srt(cs)} --> {seconds_to_srt(ce)}", text, ""]
        idx += 1
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def wrap_srt(srt_path: Path, max_chars: int = 34) -> None:
    lines = srt_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i]); i += 1
        if i < len(lines) and "-->" in lines[i]:
            out.append(lines[i]); i += 1
            text_lines: list[str] = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip()); i += 1
            full = re.sub(r"\s+", " ", " ".join(text_lines)).strip()
            out.extend(textwrap.wrap(full, width=max_chars,
                                     break_long_words=True, break_on_hyphens=True))
            if i < len(lines) and not lines[i].strip():
                out.append(""); i += 1
    srt_path.write_text("\n".join(out), encoding="utf-8")


def srt_time_to_ass(t: str) -> str:
    hh, mm, rest = t.split(":")
    ss, ms = rest.split(",")
    return f"{int(hh)}:{mm}:{ss}.{ms[:2]}"


def srt_to_ass(srt_path: Path, ass_path: Path, font_size: int,
               x: int, y1: int, font_name: str = "Montserrat SemiBold") -> None:
    srt    = srt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", srt.strip(), flags=re.M)

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "0,0,0,0,100,100,0,0,1,4,2,8,80,80,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def _safe(s: str) -> str:
        s = re.sub(r"\s+", " ", s).strip()
        return s.replace("{", "(").replace("}", ")")

    def _ts_to_sec(ts: str) -> float:
        h, m, rest = ts.split(":")
        s_part, cs = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s_part) + int(cs) / 100.0

    def _fmt(t: float) -> str:
        hh = int(t // 3600); mm = int((t % 3600) // 60)
        ss = int(t % 60);    cs = int((t - int(t)) * 100)
        return f"{hh}:{mm:02d}:{ss:02d}.{cs:02d}"

    events: list[str] = []
    for b in blocks:
        lines = [l.rstrip("\r") for l in b.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        tl = next((l for l in lines if "-->" in l), None)
        if not tl:
            continue
        s_srt, e_srt = [p.strip() for p in tl.split("-->")]
        s_ass = srt_time_to_ass(s_srt)
        e_ass = srt_time_to_ass(e_srt)
        ti    = lines.index(tl) + 1
        text  = " ".join(_safe(l) for l in lines[ti:] if _safe(l))
        text  = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        wrapped = textwrap.wrap(text, width=SUB_MAX_CHARS_PER_LINE,
                                break_long_words=True, break_on_hyphens=True)
        chunks  = [wrapped[i:i + 2] for i in range(0, len(wrapped), 2)]
        if not chunks:
            continue

        total_dur = _ts_to_sec(e_ass) - _ts_to_sec(s_ass)
        per_chunk = total_dur / len(chunks) if total_dur > 0 else 0

        for ci, chunk in enumerate(chunks):
            cs_t = _ts_to_sec(s_ass) + ci * per_chunk
            ce_t = cs_t + per_chunk
            ass_text = r"\N".join(chunk)
            events.append(
                f"Dialogue: 0,{_fmt(cs_t)},{_fmt(ce_t)},Default,,0,0,0,,"
                f"{{\\an8\\pos({x},{y1})\\fn{font_name}}}{ass_text}"
            )

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# GROQ SEGMENT PLANNING
# ──────────────────────────────────────────────────────────────────────────────
_GROQ_RETRY = frozenset(["503", "UNAVAILABLE", "429", "RATE_LIMIT_EXCEEDED",
                          "RATE LIMIT", "408", "DEADLINE_EXCEEDED",
                          "500", "502", "504", "TOO MANY REQUESTS"])


def _is_groq_retryable(exc: Exception) -> bool:
    return any(s in str(exc).upper() for s in _GROQ_RETRY)


def _groq_call_with_retry(client: Groq, prompt: str,
                           progress: Progress | None = None,
                           task_id: int | None = None):
    for model_name in GROQ_MODELS:
        attempt = 0
        while attempt < 6:
            attempt += 1
            try:
                if progress and task_id is not None:
                    progress.update(task_id,
                                    description=f"Groq [{model_name}] attempt {attempt}...",
                                    total=100, completed=35)
                return client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system",
                         "content": ("You are an elite short-form video editor. "
                                     "Always respond with valid JSON only.")},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                    max_tokens=4096,
                )
            except Exception as exc:
                if not _is_groq_retryable(exc):
                    console.print(f"[red]Non-retryable Groq error on {model_name}: {exc}[/red]")
                    break
                delay = min(60.0, 2 ** min(attempt, 6) + random.uniform(0, 3))
                console.print(f"[yellow]Groq rate-limit [{model_name}] attempt {attempt}/6"
                               f" — retry in {delay:.1f}s[/yellow]")
                time.sleep(delay)
        console.print(f"[yellow]Model {model_name} exhausted, trying next...[/yellow]")

    raise RuntimeError(f"All Groq models failed: {', '.join(GROQ_MODELS)}")


def _gemini_plan_segments(prompt: str,
                           progress: Progress | None = None,
                           task_id: int | None = None) -> str:
    """Call Gemini as fallback when all Groq models fail. Returns raw text."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    for model_name in GEMINI_PLAN_MODELS:
        for attempt in range(1, 5):
            try:
                if progress and task_id is not None:
                    progress.update(task_id,
                                    description=f"Gemini [{model_name}] attempt {attempt}...",
                                    total=100, completed=35)
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return resp.text or ""
            except Exception as exc:
                msg = str(exc)
                retryable = any(s in msg for s in ("429", "500", "503", "RESOURCE_EXHAUSTED",
                                                    "UNAVAILABLE", "quota"))
                if not retryable:
                    console.print(f"[red]Gemini non-retryable on {model_name}: {exc}[/red]")
                    break
                delay = min(60.0, 2 ** attempt + random.uniform(0, 2))
                console.print(f"[yellow]Gemini rate-limit [{model_name}] attempt {attempt}/4"
                               f" — retry in {delay:.1f}s[/yellow]")
                time.sleep(delay)
        console.print(f"[yellow]Gemini model {model_name} exhausted, trying next...[/yellow]")
    raise RuntimeError(f"All Gemini models failed: {', '.join(GEMINI_PLAN_MODELS)}")


def _build_transcript_text(items: list[dict], max_chars: int = 120_000) -> str:
    parts: list[str] = []
    total = 0
    for item in items:
        line = f"[{item['start']:.2f}-{item['end']:.2f}] {item['text']}"
        total += len(line) + 1
        if total > max_chars:
            break
        parts.append(line)
    return "\n".join(parts)


def _snap_to_scene(value: float, cuts: list[float], tol: float = 2.0) -> float:
    if not cuts:
        return value
    best = min(cuts, key=lambda x: abs(x - value))
    return best if abs(best - value) <= tol else value


def _normalize_segments(raw: list[dict], duration: float, cuts: list[float],
                         min_len: int, max_len: int,
                         max_shorts: int | None) -> list[dict]:
    if not raw:
        return []
    raw = sorted(raw, key=lambda x: float(x["start"]))
    out: list[dict] = []
    prev_end: float | None = None

    for i, seg in enumerate(raw, 1):
        start = clamp(float(seg["start"]), 0.0, duration)
        end   = clamp(float(seg["end"]),   0.0, duration)

        if prev_end is not None:
            start = prev_end
        if end <= start:
            continue
        if i < len(raw):
            end = _snap_to_scene(end, cuts, 1.5)
            end = clamp(end, start + 1.0, duration)

        dur = end - start
        if dur < min_len:
            end = clamp(start + min_len, start, duration); dur = end - start
        if dur > max_len:
            end = _snap_to_scene(start + max_len, cuts, 1.0)
            end = clamp(end, start + min_len, min(start + max_len, duration))
            dur = end - start

        if dur < min_len or dur > max_len:
            continue

        out.append({"index": len(out) + 1, "start": round(start, 3),
                    "end": round(end, 3), "duration": round(dur, 3),
                    "hook": seg.get("hook", ""), "reason": seg.get("reason", "")})
        prev_end = end

        if max_shorts is not None and len(out) >= max_shorts:
            break

    # Merge last tiny segment into previous if possible
    if len(out) >= 2:
        last, prev = out[-1], out[-2]
        if last["duration"] < min_len and (last["end"] - prev["start"]) <= max_len:
            prev["end"]      = last["end"]
            prev["duration"] = round(prev["end"] - prev["start"], 3)
            out.pop()

    for idx, seg in enumerate(out, 1):
        seg["index"] = idx

    return out


def plan_segments_with_groq(
    transcript_items: list[dict],
    scene_cuts: list[float],
    duration: float,
    max_shorts: int,
    min_len: int = MIN_SHORT_SECONDS,
    max_len: int = MAX_SHORT_SECONDS,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> list[dict]:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set. Export it as an environment variable.")
    if not transcript_items:
        raise RuntimeError("Empty transcript — cannot plan segments.")

    if progress and task_id is not None:
        progress.update(task_id, description="Planning segments with Groq...", total=100, completed=10)

    client = Groq(api_key=GROQ_API_KEY)
    t_text = _build_transcript_text(transcript_items, max_chars=GROQ_MAX_TRANSCRIPT_CHARS)
    cuts_t = [round(x, 2) for x in scene_cuts[:500]]

    prompt = f"""
You are an elite short-form video editor.

Create a CONTINUOUS series of short-form episodes from a single long video transcript.

HARD RULES:
- Output at most {max_shorts} segments.
- Each segment: {min_len}–{max_len} seconds.
- Segments MUST be chronologically continuous with no major gaps.
- Prefer starting on strong hooks and ending on natural pauses or cliffhangers.
- Return ONLY valid JSON, no markdown fences.

Video duration: {duration:.2f} sec
Scene cuts: {json.dumps(cuts_t)}
Transcript:
{t_text}

Return EXACTLY:
{{
  "selected_span": {{"start": 0, "end": 0}},
  "segments": [
    {{"index": 1, "start": 0, "end": 0, "duration": 0,
      "hook": "short description", "reason": "why it works"}}
  ]
}}
""".strip()

    text = ""
    try:
        response = _groq_call_with_retry(client, prompt, progress, task_id)
        text = (response.choices[0].message.content or "").strip()
        provider = "Groq"
    except RuntimeError:
        console.print("[yellow]Groq exhausted — falling back to Gemini...[/yellow]")
        text = _gemini_plan_segments(prompt, progress, task_id)
        provider = "Gemini"

    if progress and task_id is not None:
        progress.update(task_id, description=f"Parsing {provider} response...", total=100, completed=80)

    text = re.sub(r"^```json\s*", "", text, flags=re.I)
    text = re.sub(r"^```\s*",     "", text, flags=re.I)
    text = re.sub(r"\s*```$",     "", text)

    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise RuntimeError(f"No JSON found in {provider} response:\n{text[:500]}")

    data     = json.loads(m.group(0))
    segments = data.get("segments", [])
    if not isinstance(segments, list) or not segments:
        raise RuntimeError(f"{provider} returned no valid segments.")

    normalized = _normalize_segments(segments, duration, scene_cuts,
                                      min_len, max_len, max_shorts)
    if not normalized:
        raise RuntimeError("No valid segments after normalization.")

    if progress and task_id is not None:
        progress.update(task_id, description=f"{provider} planning done", total=100, completed=100)

    return normalized


# ──────────────────────────────────────────────────────────────────────────────
# RENDER
# ──────────────────────────────────────────────────────────────────────────────
def _ffmpeg_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:")


def drawtext_font_arg(font_path: Path, fallback_font: str = "Arial") -> str:
    """Return a safe drawtext font argument. Uses a bundled font only if present."""
    if font_path.exists():
        return f"fontfile='{_ffmpeg_path(font_path)}':"
    return f"font='{fallback_font}':"


def render_short(source: Path, out_path: Path, part_idx: int, total: int,
                 start: float, length: float, title: str = "",
                 ass_path: Path | None = None,
                 progress: Progress | None = None,
                 task_id: int | None = None) -> None:
    if progress and task_id is not None:
        progress.update(task_id, description=f"Short {part_idx}/{total}: rendering", completed=75)

    ass_ff: str | None = None
    if ass_path and ass_path.exists():
        ass_ff = _ffmpeg_path(ass_path)

    title_font_arg = drawtext_font_arg(TITLE_FONT_FILE, "Arial")
    part_font_arg = drawtext_font_arg(PART_FONT_FILE, "Arial")

    part_text = f"{PART_TEXT_PREFIX}{part_idx}/{total}"
    top_pad   = (1920 - CENTER_H) // 2
    title_y   = max(50, (top_pad // 2) - 20)
    part_y    = top_pad + CENTER_H + 50

    vf = (
        f"[0:v]split=2[orig][bg];"
        f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,gblur=sigma=35,eq=brightness=-0.08[blur];"
        f"[orig]scale=1080:{CENTER_H}:force_original_aspect_ratio=increase,"
        f"crop=1080:{CENTER_H}[fg];"
        f"[blur][fg]overlay=(W-w)/2:{top_pad}[base0];"
        f"[1:v]scale={LOGO_SIZE}:{LOGO_SIZE},format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),(W/2)*(W/2)),255,0)',"
        f"colorchannelmixer=aa={LOGO_OPACITY}[logo];"
        f"[base0][logo]overlay={LOGO_MARGIN_X}:(H-h-{LOGO_MARGIN_Y})[base1]"
    )

    curr = "[base1]"
    if title:
        for i, ln in enumerate(textwrap.wrap(title, width=TITLE_MAX_CHARS,
                                              break_long_words=True, break_on_hyphens=True)):
            t   = ln.replace("\\", "\\\\").replace("'", "\\'").replace("%", "%%")
            yl  = title_y + i * (TITLE_FONT_SIZE + 6)
            nxt = f"[t{i}]"
            vf += (
                f";{curr}drawtext={title_font_arg}"
                f"text='{t}':x=(w-text_w)/2:y={yl}:"
                f"fontsize={TITLE_FONT_SIZE}:fontcolor=yellow:"
                f"borderw=2:bordercolor=black@0.85{nxt}"
            )
            curr = nxt

    vf += (
        f";{curr}drawtext={part_font_arg}"
        f"text='{part_text}':x=(w-text_w)/2:y={part_y}:"
        f"fontsize=80:fontcolor=white:borderw=4:bordercolor=black@0.85[with_part]"
    )
    vf += f";[with_part]ass='{ass_ff}'[v]" if ass_ff else ";[with_part]null[v]"

    run_cmd(["ffmpeg", "-y", *ffmpeg_hwaccel(),
             "-ss", str(start), "-t", str(length),
             "-i", str(source), "-i", str(LOGO_PATH),
             "-filter_complex", vf,
             "-map", "[v]", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(out_path)])

    if progress and task_id is not None:
        progress.update(task_id, completed=100, description=f"Short {part_idx}/{total}: done")


def _copy_into(src: Path, dst_folder: Path) -> None:
    if not src.exists():
        console.print(f"[yellow]⚠ File not found, skip: {src}[/yellow]")
        return
    shutil.copy2(src, dst_folder / src.name)
    console.print(f"[green]📌 Copied:[/green] {src.name}")


def _copy_reelsinfo(src: Path, dst_folder: Path, base_title: str) -> None:
    if not src.exists():
        console.print("[yellow]⚠ reelsinfo.json not found, skip[/yellow]")
        return
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("reelsinfo.json must contain a JSON object")
        data["base_title"] = (base_title or "Amazing Build Transformation").strip() \
                             or "Amazing Build Transformation"
        (dst_folder / src.name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print("[green]📌 reelsinfo.json updated and copied[/green]")
    except Exception as exc:
        console.print(f"[yellow]⚠ Error copying reelsinfo.json: {exc}[/yellow]")


# ──────────────────────────────────────────────────────────────────────────────
# EDIT-VIDEO PIPELINE  (single video)
# ──────────────────────────────────────────────────────────────────────────────
def edit_video(url: str, title: str = "",
               include_subs: bool = SUBTITLES_ENABLED) -> int:
    """Download, transcribe, plan, render. Returns segments created.
    Raises on any fatal error — caller decides whether to mark URL as used."""
    run_id  = f"{secrets.randbelow(100_000):05d}"
    tmp_dir = BASE_DIR / "tmp" / run_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with make_progress() as progress:
            task_dl = progress.add_task("Preparing download...", total=None)
            source  = download_youtube(url, tmp_dir, progress, task_dl)

            base_name = safe_name(source.stem)
            duration  = get_duration(source)

            if duration < MIN_SHORT_SECONDS:
                raise RuntimeError("Video too short to create 60-second shorts.")

            folder_id  = make_unique_5digit_id(OUT_DIR)
            out_folder = OUT_DIR / folder_id
            out_folder.mkdir(parents=True, exist_ok=True)

            task_audio = progress.add_task("Extracting audio...", total=None)
            full_audio = extract_audio(source, tmp_dir, progress, task_audio)

            task_tr  = progress.add_task("Transcribing...", total=None)
            full_srt = whisper_transcribe(full_audio, tmp_dir, progress, task_tr)
            items    = parse_srt_blocks(full_srt)

            task_sc = progress.add_task("Detecting scenes...", total=100)
            cuts    = detect_scene_cuts(source, progress, task_sc)

            task_groq = progress.add_task("Planning segments (Groq)...", total=100)
            segments  = plan_segments_with_groq(
                items, cuts, duration, MAX_SHORTS,
                MIN_SHORT_SECONDS, MAX_SHORT_SECONDS, progress, task_groq)

            total = len(segments)
            if total <= 0:
                raise RuntimeError("No segments generated.")

            (out_folder / "segments_plan.json").write_text(
                json.dumps(segments, indent=2, ensure_ascii=False), encoding="utf-8")

            top_pad = (1920 - CENTER_H) // 2
            sub_y1  = top_pad + CENTER_H - 150

            batch = progress.add_task("Rendering shorts", total=total)
            for seg in segments:
                idx    = seg["index"]
                start  = seg["start"]
                length = seg["end"] - seg["start"]
                if length <= 0:
                    progress.advance(batch); continue

                short_task = progress.add_task(f"Short {idx}/{total}: init", total=100)
                ass_path: Path | None = None

                if include_subs:
                    stem = f"{base_name}_part_{idx:03d}"
                    srt  = tmp_dir / f"{stem}.srt"
                    progress.update(short_task, description=f"Short {idx}: SRT", completed=20)
                    build_segment_srt(items, start, seg["end"], srt)
                    progress.update(short_task, description=f"Short {idx}: wrap", completed=45)
                    wrap_srt(srt, max_chars=SUB_MAX_CHARS_PER_LINE)
                    progress.update(short_task, description=f"Short {idx}: ASS", completed=60)
                    ass_path = tmp_dir / f"{stem}.ass"
                    srt_to_ass(srt, ass_path, SUB_FONT_SIZE, 540, sub_y1)
                else:
                    progress.update(short_task, description=f"Short {idx}: no subs", completed=60)

                out_path = out_folder / f"{base_name}_reel_part_{idx:03d}.mp4"
                render_short(source, out_path, idx, total, start, length,
                             title, ass_path, progress, short_task)
                progress.advance(batch)

            task_cp = progress.add_task("Copying extras...", total=100)
            _copy_into(POST_EXE_PATH, out_folder)
            progress.update(task_cp, completed=50)
            _copy_reelsinfo(REELSINFO_PATH, out_folder, title)
            progress.update(task_cp, completed=100, description="Extras copied")

        console.print(f"[bold green]✅ Done — out/{folder_id}[/bold green]")
        return total

    finally:
        if CLEAN_TMP_AFTER:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            console.print(f"[green]tmp/{run_id} cleaned[/green]")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ──────────────────────────────────────────────────────────────────────────────
def _load_selected_videos(path: Path = SELECTED_FILE) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Selected videos file not found: {path}. Run: python pipeline.py find")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name} must contain a JSON list")
    return data


def process_videos(videos: list[dict]) -> None:
    """Edit a list of selected videos and update used_videos.json only after success."""
    if not videos:
        console.print("[yellow]No videos to process. Exiting.[/yellow]")
        return

    console.print(f"[bold]Videos to process:[/bold] {len(videos)}")
    console.print("\n[bold cyan]=== STEP 2: editing videos ===[/bold cyan]")

    success_count  = 0
    error_count    = 0
    total_segments = 0
    stopped_early  = False

    for idx, video in enumerate(videos, 1):
        if total_segments >= MAX_TOTAL_SEGMENTS:
            console.print(
                f"\n[bold yellow]Segment limit ({MAX_TOTAL_SEGMENTS}) reached. Stopping.[/bold yellow]")
            stopped_early = True
            break

        url         = str(video.get("url", "")).strip()
        short_title = str(video.get("short_title", "")).strip() or "Amazing Build Transformation"

        if not url:
            console.print(f"[{idx}/{len(videos)}] Skipped: missing URL")
            error_count += 1
            continue

        console.print(f"\n[bold][{idx}/{len(videos)}][/bold] URL: {url}")
        console.print(f"  Title   : {short_title}")
        console.print(f"  Segments: {total_segments}/{MAX_TOTAL_SEGMENTS}")

        try:
            created = edit_video(url, title=short_title, include_subs=True)
            append_used_url(USED_VIDEOS_FILE, url)
            total_segments += created
            success_count  += 1
            console.print(f"  [green]✅ {created} segments created "
                          f"(running total: {total_segments})[/green]")

            if total_segments >= MAX_TOTAL_SEGMENTS:
                console.print(
                    f"\n[bold yellow]Limit reached after video {idx}.[/bold yellow]")
                stopped_early = True
                break

        except Exception as exc:
            error_count += 1
            console.print(f"  [red]❌ Error on video {idx}: {exc}[/red]")
            log.exception("edit_video failed for %s", url)

    console.print("\n[bold cyan]=== SUMMARY ===[/bold cyan]")
    console.print(f"  Completed : {success_count}")
    console.print(f"  Errors    : {error_count}")
    console.print(f"  Segments  : {total_segments}")
    if stopped_early:
        console.print("  [yellow]Stopped early — segment limit reached.[/yellow]")
    else:
        console.print("  [green]All videos processed.[/green]")

    LAST_EDIT_RESULT.write_text(
        json.dumps({"total_segments": total_segments}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find YouTube videos and convert them into vertical short-form clips."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["all", "find", "edit-selected", "edit-url"],
        default="all",
        help="all: find and edit; find: only create selected_videos.json; edit-selected: edit selected_videos.json; edit-url: edit one URL",
    )
    parser.add_argument("--config", default=str(CONFIG_FILE), help="Path to config.json")
    parser.add_argument("--url", help="YouTube URL for edit-url mode")
    parser.add_argument("--title", default="Amazing Build Transformation", help="Title shown on the generated shorts")
    parser.add_argument("--no-subs", action="store_true", help="Disable subtitles for edit-url mode")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()

    if args.mode == "edit-url":
        if not args.url:
            parser.error("edit-url mode requires --url")
        created = edit_video(args.url, title=args.title, include_subs=not args.no_subs)
        append_used_url(USED_VIDEOS_FILE, args.url)
        LAST_EDIT_RESULT.write_text(
            json.dumps({"total_segments": created}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        console.print(f"[bold green]Created {created} segment(s).[/bold green]")
        return

    cfg = load_config(config_path)

    if args.mode in {"all", "find"}:
        console.print("\n[bold cyan]=== STEP 1: finding videos ===[/bold cyan]")
        videos = run_find_videos(cfg)
        if args.mode == "find":
            return
    else:
        videos = _load_selected_videos()

    process_videos(videos)

if __name__ == "__main__":
    main()