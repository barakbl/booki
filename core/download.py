#!/usr/bin/env python3
"""
download.py — download a YouTube video (or its audio) locally via yt-dlp.

Usage:
    booki download <url-or-video-id>            # mp4, ≤1080p
    booki download <url-or-video-id> --audio    # mp3 audio-only

The path layout, quality cap, subtitles and thumbnails are all read from
[downloads] in config.toml. After a successful download, the video's .md
file (if any) is updated with:

    downloaded:        true
    download_format:   "video" | "audio"
    download_path:     <relative path under bookmarks_dir>
    downloaded_at:     YYYY-MM-DD

The same helpers are used by web.py's background /api/bookmarks/{id}/download
endpoint — both paths go through `download_one()`.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from .store import ItemStore, today_str


ROOT            = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG  = ROOT / "config.toml"
VIDEO_ID_RE     = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


# ─── Config ──────────────────────────────────────────────────────────────────

@dataclass
class DownloadConfig:
    dir:              Path
    video_height_max: int   = 1080
    write_subs:       bool  = True
    write_thumbnail:  bool  = True
    sub_langs:        str   = "en.*"     # yt-dlp wildcard — English + variants

    @classmethod
    def from_toml(cls, cfg: dict, config_path: Path) -> "DownloadConfig":
        d = cfg.get("downloads", {}) or {}
        raw_dir = d.get("dir", "./downloads")
        p = Path(raw_dir).expanduser()
        if not p.is_absolute():
            p = (config_path.parent / p).resolve()
        return cls(
            dir              = p,
            video_height_max = int(d.get("video_height_max", 1080)),
            write_subs       = bool(d.get("write_subs", True)),
            write_thumbnail  = bool(d.get("write_thumbnail", True)),
            sub_langs        = str(d.get("sub_langs", "en.*")),
        )


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


# ─── URL helpers ─────────────────────────────────────────────────────────────

def normalize_video_url(url_or_id: str) -> str:
    """Accept a full URL or a bare 11-char video ID — always return a URL."""
    s = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return f"https://www.youtube.com/watch?v={s}"
    return s


def extract_video_id(url: str) -> Optional[str]:
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


# ─── The download itself ─────────────────────────────────────────────────────

@dataclass
class DownloadResult:
    ok:            bool
    path:          Optional[Path]   = None     # the media file (relative or absolute)
    format:        str              = "video"  # "video" | "audio"
    title:         str              = ""
    channel:       str              = ""
    error:         str              = ""


def download_one(url_or_id: str, dl_cfg: DownloadConfig, *,
                 audio: bool = False) -> DownloadResult:
    """Download one video (or its audio) to dl_cfg.dir — returns DownloadResult."""
    try:
        import yt_dlp
    except ImportError:
        return DownloadResult(ok=False, error="yt-dlp not installed: pip install yt-dlp")

    url = normalize_video_url(url_or_id)
    dl_cfg.dir.mkdir(parents=True, exist_ok=True)
    # %(uploader)s groups by channel; %(title).200B trims titles with unicode safety.
    outtmpl = str(dl_cfg.dir / "%(uploader)s" / "%(title).200B [%(id)s].%(ext)s")

    opts: dict = {
        "outtmpl":           outtmpl,
        "restrictfilenames": True,     # safe filenames across OS
        "noprogress":        True,     # yt-dlp's own progress bar is noisy in threads
        "quiet":             True,
        "no_warnings":       True,
        "writesubtitles":    dl_cfg.write_subs,
        "writeautomaticsub": dl_cfg.write_subs,
        "subtitleslangs":    [dl_cfg.sub_langs] if dl_cfg.write_subs else [],
        "writethumbnail":    dl_cfg.write_thumbnail,
        "postprocessors":    [],
    }

    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
    else:
        h = dl_cfg.video_height_max
        # Prefer mp4 streams so we avoid ffmpeg muxing when possible; fall back
        # to best+mux if pure-mp4 isn't available at this height.
        opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={h}][ext=mp4]/"
            f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        )
        opts["merge_output_format"] = "mp4"

    if dl_cfg.write_thumbnail:
        opts["postprocessors"].append({
            "key": "FFmpegThumbnailsConvertor",
            "format": "jpg",
            "when": "before_dl",
        })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        return DownloadResult(ok=False, error=str(e))

    # Resolve the final media path. yt-dlp's reported filename points at the
    # pre-postprocessing file; for audio we swap the extension to .mp3.
    requested = info.get("requested_downloads") or []
    final: Optional[str] = None
    if requested:
        final = requested[0].get("filepath") or requested[0].get("_filename")
    if not final:
        final = info.get("_filename") or info.get("filename")
    if not final:
        return DownloadResult(ok=False, error="yt-dlp did not report a filename")

    path = Path(final)
    if audio:
        path = path.with_suffix(".mp3")

    return DownloadResult(
        ok      = True,
        path    = path,
        format  = "audio" if audio else "video",
        title   = info.get("title", "") or "",
        channel = info.get("uploader", "") or "",
    )


# ─── Frontmatter update ──────────────────────────────────────────────────────

def update_md_for_download(store: ItemStore, video_url: str,
                           result: DownloadResult,
                           dl_cfg: "DownloadConfig") -> Optional[Path]:
    """
    If a .md exists anywhere in the store for this URL, stamp its per-format
    download fields. Returns the .md path we touched (or None).

    We track *each format independently* so a video can have both mp4 and
    mp3 local copies at once:
        downloaded_video / download_path_video / downloaded_at_video
        downloaded_audio / download_path_audio / downloaded_at_audio
    `downloaded` remains a convenience bool = "any local copy exists".
    """
    if not result.ok or not result.path:
        return None
    md_path = store.find_anywhere(video_url)
    if not md_path:
        return None

    # Store path as relative-to-downloads-dir — so web.py can mount /downloads
    # and the UI just uses /downloads/<stored-path> as the href.
    try:
        rel = result.path.relative_to(dl_cfg.dir) if result.path.is_absolute() else result.path
    except ValueError:
        rel = result.path

    suffix = result.format   # "video" | "audio"
    store.update_fields(
        md_path,
        downloaded                  = True,
        **{f"download_path_{suffix}":   str(rel)},
        **{f"downloaded_at_{suffix}":   today_str()},
    )
    return md_path


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Download a YouTube video or its audio.")
    p.add_argument("url", help="YouTube URL or 11-char video id")
    p.add_argument("--audio", action="store_true", help="Audio-only (mp3)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = p.parse_args()

    cfg = load_config(args.config)
    dl_cfg = DownloadConfig.from_toml(cfg, args.config)

    bookmarks_dir = Path(cfg["bookmarks"]["dir"]).expanduser()
    if not bookmarks_dir.is_absolute():
        bookmarks_dir = (args.config.parent / bookmarks_dir).resolve()

    print(f"⬇  Downloading {'audio' if args.audio else 'video'} → {dl_cfg.dir}")
    result = download_one(args.url, dl_cfg, audio=args.audio)

    if not result.ok:
        sys.exit(f"✗ {result.error}")

    print(f"✓ {result.title}")
    print(f"  {result.path}")

    # Best-effort: also update the .md in the store if one exists.
    store = ItemStore(bookmarks_dir)
    md = update_md_for_download(store, normalize_video_url(args.url), result, dl_cfg)
    if md:
        print(f"  frontmatter updated: {md.relative_to(bookmarks_dir)}")


if __name__ == "__main__":
    main()
