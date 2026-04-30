"""
plugins.exporters.offline_video — background plugin: download videos via
yt-dlp, generate a themed index.html, zip everything into a single archive.

Per-video failure handling: skip + retry-once. If both attempts fail, log
the reason and continue. The task ends as `success` even with skipped items
(they appear in a "skipped" section in the index page).

Working files live under `<artifact_dir>/work/`. yt-dlp's `continue=True`
plus `overwrites=False` means a partial download survives a worker crash —
on retry the framework re-runs run_background, which finds the partial in
work/ and resumes from there. The work dir is wiped only after the zip is
sealed.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.exporter import Exporter, TaskHandle, register_exporter

_SLUG_BAD = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_WS = re.compile(r"\s+")


def _slug(title: str) -> str:
    s = _SLUG_BAD.sub("", title or "").strip()
    s = _SLUG_WS.sub("-", s)
    return (s[:80] or "untitled").lower()


@register_exporter
class OfflineVideoExporter(Exporter):
    slug = "offline_video"
    name = "Offline video archive"
    description = "Download videos with yt-dlp, bundle with a themed index.html into a zip."
    applicable_kinds = ["video"]
    execution_mode = "background"
    uses_themes = True

    options_schema = [
        {"name": "page_title", "type": "text", "label": "Page title",
         "default": "My Videos"},
        {"name": "quality", "type": "select", "label": "Max quality",
         "options": ["360p", "480p", "720p", "1080p", "best"], "default": "720p"},
        {"name": "include_subs", "type": "bool", "label": "Include subtitles",
         "default": True},
        {"name": "sub_lang", "type": "text", "label": "Subtitle language",
         "default": "en", "help": "yt-dlp lang code (e.g. en, en.*, he, fr)."},
    ]

    def run_background(self, items, options, theme, theme_vars, task: TaskHandle):
        try:
            import yt_dlp  # type: ignore
        except ImportError as e:
            raise RuntimeError("yt-dlp is not installed. Run `pip install yt-dlp`.") from e
        if theme is None:
            raise ValueError("Offline video exporter requires a theme.")

        page_title = options.get("page_title") or "My Videos"
        quality = options.get("quality") or "720p"
        include_subs = bool(options.get("include_subs", True))
        sub_lang = (options.get("sub_lang") or "en").strip() or "en"

        artifact_dir = task.artifact_dir
        work_dir = artifact_dir / "work"
        out_dir = artifact_dir / "out"
        work_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        format_str = _format_for_quality(quality)

        rendered: list[dict] = []
        skipped: list[dict] = []
        used_slugs: set[str] = set()
        total = len(items)

        for i, it in enumerate(items, start=1):
            title = (it.get("title") or "untitled").strip()
            url = (it.get("url") or "").strip()
            task.progress(i - 1, total)
            task.log(f"[{i}/{total}] {title}")

            if not url:
                task.log("  skipped: no URL")
                skipped.append({"title": title, "reason": "no URL"})
                continue

            slug = _unique_slug(title, used_slugs)
            used_slugs.add(slug)

            err = ""
            video_path: Path | None = None
            thumb_path: Path | None = None
            for attempt in (1, 2):
                try:
                    video_path, thumb_path = _download(
                        yt_dlp, url, work_dir, slug,
                        format_str=format_str,
                        include_subs=include_subs,
                        sub_lang=sub_lang,
                    )
                    err = ""
                    break
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    task.log(f"  attempt {attempt} failed: {err}")

            if video_path is None:
                skipped.append({"title": title, "reason": err or "unknown error"})
                continue

            # Move into out/ — the zip is built from out/.
            try:
                video_dest = out_dir / video_path.name
                shutil.move(str(video_path), str(video_dest))
            except OSError as e:
                skipped.append({"title": title, "reason": f"move failed: {e}"})
                continue

            thumb_dest = None
            if thumb_path and thumb_path.exists():
                try:
                    thumb_dest = out_dir / thumb_path.name
                    shutil.move(str(thumb_path), str(thumb_dest))
                except OSError:
                    thumb_dest = None

            rendered.append({
                "title": title,
                "url": url,
                "slug": slug,
                "filename": video_dest.name,
                "thumb": thumb_dest.name if thumb_dest else None,
                "channel": it.get("channel") or "",
                "summary": it.get("summary") or "",
            })

        task.progress(total, total)

        # Render index.html
        env = Environment(
            loader=FileSystemLoader(str(theme.path)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        tmpl = env.get_template("main.html.j2")
        html = tmpl.render(
            title=page_title,
            videos=rendered,
            skipped=skipped,
            theme_vars=theme_vars,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        (out_dir / "index.html").write_text(html, encoding="utf-8")

        # Zip it. ZIP_STORED keeps mp4s un-recompressed (already compressed).
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
        zip_path = artifact_dir / f"booki-videos-{ts}.zip"
        task.log(f"zipping {len(rendered)} video{'' if len(rendered) == 1 else 's'} → {zip_path.name}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for p in sorted(out_dir.rglob("*")):
                if p.is_file():
                    z.write(p, arcname=p.relative_to(out_dir))

        # Now that the zip is sealed, drop the working tree to free disk.
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)

        suffix = f", {len(skipped)} skipped" if skipped else ""
        task.log(f"done: {zip_path.name} ({len(rendered)} videos{suffix})")
        return zip_path


# ─── helpers ────────────────────────────────────────────────────────────────

def _format_for_quality(quality: str) -> str:
    if quality == "best":
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    h = int(str(quality).rstrip("p"))
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={h}][ext=mp4]/"
        f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
    )


def _unique_slug(title: str, used: set[str]) -> str:
    base = _slug(title)
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def _download(yt_dlp, url: str, work_dir: Path, slug: str, *,
              format_str: str, include_subs: bool, sub_lang: str
              ) -> tuple[Path, Path | None]:
    outtmpl = str(work_dir / f"{slug}.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "restrictfilenames": False,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "format": format_str,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "writesubtitles": include_subs,
        "writeautomaticsub": include_subs,
        "subtitleslangs": [sub_lang] if include_subs else [],
        "continue": True,
        "overwrites": False,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    requested = info.get("requested_downloads") or []
    raw = None
    if requested:
        raw = requested[0].get("filepath") or requested[0].get("_filename")
    if not raw:
        raw = info.get("_filename") or info.get("filename")
    if not raw:
        raise RuntimeError("yt-dlp did not report a filename")
    video_path = Path(raw)
    if not video_path.exists():
        # Sometimes yt-dlp reports the pre-merge name; check the .mp4 sibling.
        merged = video_path.with_suffix(".mp4")
        if merged.exists():
            video_path = merged
        else:
            raise RuntimeError(f"downloaded file missing: {video_path}")

    thumb_path: Path | None = None
    for ext in (".jpg", ".jpeg", ".webp", ".png"):
        candidate = video_path.with_suffix(ext)
        if candidate.exists() and candidate != video_path:
            thumb_path = candidate
            break

    return video_path, thumb_path
