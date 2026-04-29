"""
plugins/youtube — YouTube account as a source.

What it fetches (all opt-in via [sources.youtube] config):

  • Subscriptions              → Item(kind="channel")
  • Liked videos (LL playlist) → Item(kind="video", liked=true, watched=true)
  • Recent uploads from every  → Item(kind="video", subscribed_to_channel=true)
    subscribed channel
  • (optional) Takeout         → videos flagged watched=true
    watch-history.json

Videos that appear in multiple streams (e.g. a liked video that's also a
recent upload from a subscribed channel) are merged into one Item with the
union of flags set — one MD file per video, one source of truth.

Auth:
  YouTube Data API v3 OAuth2. User downloads an OAuth client secret JSON
  from https://console.cloud.google.com/ (Desktop app credentials, YouTube
  Data API v3 enabled) and drops it at the configured path. First run opens
  a browser; refresh token is persisted so subsequent runs are silent.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..base import Item, Source, register


SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
LIKED_PLAYLIST_ID = "LL"   # well-known id for "Liked videos"

DEFAULT_CLIENT_SECRET = "~/.booki/youtube-client-secret.json"
DEFAULT_TOKEN_PATH    = "~/.booki/youtube-token.json"

# Conservative defaults — easy to tune upward in config.
DEFAULT_UPLOADS_PER_CHANNEL = 10
DEFAULT_MAX_LIKED           = 500
DEFAULT_DESCRIPTION_CHARS   = 600


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _batched(seq, n):
    buf: list = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


_ISO_DURATION_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$"
)


def _iso_duration_to_human(iso: str) -> str:
    """"PT1H23M45S" → "1:23:45" ; "PT4M" → "4:00" ; unknown → original."""
    if not iso:
        return ""
    m = _ISO_DURATION_RE.match(iso)
    if not m:
        return iso
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _expand(path_str: str) -> Path:
    return Path(path_str).expanduser()


def _deps_available() -> tuple[bool, str]:
    try:
        import google.oauth2.credentials   # noqa: F401
        import google_auth_oauthlib.flow   # noqa: F401
        import googleapiclient.discovery   # noqa: F401
        return True, ""
    except ImportError as e:
        return False, (
            f"missing dependency: {e.name or e}. Install with:\n"
            "    pip install google-api-python-client google-auth-oauthlib"
        )


# ─── Source ───────────────────────────────────────────────────────────────────

@register
class YouTubeSource(Source):
    name = "youtube"

    # ── web UI schema ────────────────────────────────────────────────────────

    @classmethod
    def kind_specs(cls) -> list[dict]:
        return [
            {"slug": "video",   "glyph": "🎬", "label": "Video"},
            {"slug": "channel", "glyph": "📺", "label": "Channel"},
        ]

    @classmethod
    def field_specs(cls) -> list[dict]:
        return [
            {"name": "channel",               "label": "Channel",       "group": "YouTube", "format": "text",     "kinds": ("video",)},
            {"name": "channel_id",            "label": "Channel ID",    "group": "YouTube", "format": "url", "url_template": "https://www.youtube.com/channel/{value}"},
            {"name": "published_at",          "label": "Published",     "group": "YouTube", "format": "date",     "kinds": ("video",)},
            {"name": "duration",              "label": "Duration",      "group": "YouTube", "format": "duration", "icon": "⏱️", "kinds": ("video",)},
            {"name": "view_count",            "label": "Views",         "group": "YouTube", "format": "number",   "kinds": ("video",)},
            {"name": "like_count",            "label": "Likes",         "group": "YouTube", "format": "number",   "kinds": ("video",)},
            {"name": "liked",                 "label": "Liked",         "group": "YouTube", "format": "bool", "icon": "❤️", "kinds": ("video",)},
            {"name": "watched",               "label": "Watched",       "group": "YouTube", "format": "bool", "icon": "✅", "kinds": ("video",)},
            {"name": "subscribed_to_channel", "label": "From subscribed channel", "group": "YouTube", "format": "bool", "icon": "🔔", "kinds": ("video",)},
            {"name": "subscribed",            "label": "Subscribed",    "group": "YouTube", "format": "bool", "icon": "🔔", "kinds": ("channel",)},
            {"name": "subscriber_count",      "label": "Subscribers",   "group": "YouTube", "format": "number",   "kinds": ("channel",)},
            {"name": "video_count",           "label": "Videos",        "group": "YouTube", "format": "number",   "kinds": ("channel",)},
            {"name": "youtube_tags",          "label": "YouTube tags",  "group": "YouTube", "format": "tags",     "kinds": ("video",)},
            {"name": "description",           "label": "Description",   "group": "Description", "format": "text"},
            {"name": "download_path_video",   "label": "Local mp4",     "group": "Downloads", "format": "file_link", "url_prefix": "/downloads/", "kinds": ("video",)},
            {"name": "download_path_audio",   "label": "Local mp3",     "group": "Downloads", "format": "file_link", "url_prefix": "/downloads/", "kinds": ("video",)},
            {"name": "downloaded_at_video",   "label": "mp4 added",     "group": "Downloads", "format": "date",      "kinds": ("video",)},
            {"name": "downloaded_at_audio",   "label": "mp3 added",     "group": "Downloads", "format": "date",      "kinds": ("video",)},
        ]

    # ── availability ─────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        ok, _ = _deps_available()
        if not ok:
            return False
        secret = _expand(self.cfg.get("client_secret", DEFAULT_CLIENT_SECRET))
        return secret.exists()

    def availability_hint(self) -> str:
        ok, msg = _deps_available()
        if not ok:
            return msg
        secret = _expand(self.cfg.get("client_secret", DEFAULT_CLIENT_SECRET))
        return (
            f"OAuth client secret not found at {secret}. Create Desktop-app "
            "credentials in Google Cloud Console (with YouTube Data API v3 "
            "enabled) and save the JSON there."
        )

    # ── fetch ───────────────────────────────────────────────────────────────

    def fetch(self) -> Iterator[Item]:
        yt = self._build_client()

        fetch_subs       = bool(self.cfg.get("fetch_subscriptions", True))
        fetch_liked      = bool(self.cfg.get("fetch_liked", True))
        fetch_uploads    = bool(self.cfg.get("fetch_subscription_uploads", True))
        uploads_per_ch   = int(self.cfg.get("uploads_per_channel", DEFAULT_UPLOADS_PER_CHANNEL))
        max_liked        = int(self.cfg.get("max_liked", DEFAULT_MAX_LIKED))
        watch_history    = self.cfg.get("watch_history_json") or ""

        # 1. Subscriptions — emit channel items, and collect uploads playlists.
        subscriptions: list[dict] = []
        if fetch_subs or fetch_uploads:
            subscriptions = self._fetch_subscriptions(yt)
            print(f"  [youtube] {len(subscriptions)} subscription(s)")

        if fetch_subs:
            for ch in subscriptions:
                yield self._channel_item(ch, subscribed=True)

        # 2. Gather video flags across all streams, then fetch details once.
        # Flags: liked, watched, subscribed_to_channel. Booleans OR-merge.
        flags: dict[str, dict] = {}

        def _mark(vid: str, **kw):
            f = flags.setdefault(vid, {
                "liked": False, "watched": False, "subscribed_to_channel": False,
            })
            for k, v in kw.items():
                f[k] = f[k] or v

        if fetch_liked:
            liked_count = 0
            for vid in self._iter_playlist_video_ids(yt, LIKED_PLAYLIST_ID, limit=max_liked):
                _mark(vid, liked=True, watched=True)
                liked_count += 1
            print(f"  [youtube] {liked_count} liked video(s)")

        if fetch_uploads and subscriptions:
            total = 0
            for ch in subscriptions:
                uploads_pid = ch.get("uploads_playlist_id")
                if not uploads_pid:
                    continue
                for vid in self._iter_playlist_video_ids(yt, uploads_pid, limit=uploads_per_ch):
                    _mark(vid, subscribed_to_channel=True)
                    total += 1
            print(f"  [youtube] {total} upload(s) across subscriptions")

        if watch_history:
            wh_ids = self._load_takeout_watch_history(_expand(watch_history))
            for vid in wh_ids:
                _mark(vid, watched=True)
            print(f"  [youtube] {len(wh_ids)} video(s) from Takeout history")

        if not flags:
            return

        # 3. Batch-fetch full video metadata for every collected ID.
        details = self._fetch_video_details(yt, list(flags.keys()))

        # 4. Emit merged video items.
        missing = 0
        for vid, fl in flags.items():
            d = details.get(vid)
            if not d:
                missing += 1
                continue
            yield self._video_item(d, fl)
        if missing:
            print(f"  [youtube] {missing} video(s) unavailable (private/deleted) — skipped")

    # ── OAuth ───────────────────────────────────────────────────────────────

    def _build_client(self):
        from googleapiclient.discovery import build
        creds = self._get_credentials()
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _get_credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        secret_path = _expand(self.cfg.get("client_secret", DEFAULT_CLIENT_SECRET))
        token_path  = _expand(self.cfg.get("token_path",    DEFAULT_TOKEN_PATH))

        creds = None
        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except Exception as e:
                print(f"  [youtube] cached token unreadable ({e}) — re-authorizing", file=sys.stderr)
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
                print("  [youtube] opening browser for OAuth consent...")
                creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())

        return creds

    # ── API wrappers ────────────────────────────────────────────────────────

    def _fetch_subscriptions(self, yt) -> list[dict]:
        """Returns list of dicts with channel_id, channel_title, uploads_playlist_id, ..."""
        subs: list[dict] = []
        req = yt.subscriptions().list(part="snippet", mine=True, maxResults=50)
        while req is not None:
            resp = req.execute()
            for it in resp.get("items", []):
                sn = it["snippet"]
                subs.append({
                    "channel_id":    sn["resourceId"]["channelId"],
                    "channel_title": sn.get("title", ""),
                    "description":   sn.get("description", ""),
                    "thumbnail":     sn.get("thumbnails", {}).get("default", {}).get("url", ""),
                })
            req = yt.subscriptions().list_next(req, resp)

        # Fetch uploads playlist id + statistics for each channel, 50 at a time.
        by_id = {s["channel_id"]: s for s in subs}
        for batch in _batched(list(by_id.keys()), 50):
            resp = yt.channels().list(
                part="contentDetails,statistics",
                id=",".join(batch),
                maxResults=50,
            ).execute()
            for c in resp.get("items", []):
                s = by_id.get(c["id"])
                if not s:
                    continue
                related = c.get("contentDetails", {}).get("relatedPlaylists", {})
                s["uploads_playlist_id"] = related.get("uploads", "")
                stats = c.get("statistics", {})
                s["subscriber_count"] = int(stats.get("subscriberCount", 0) or 0)
                s["video_count"]      = int(stats.get("videoCount", 0) or 0)
        return subs

    def _iter_playlist_video_ids(self, yt, playlist_id: str,
                                 limit: Optional[int] = None) -> Iterator[str]:
        from googleapiclient.errors import HttpError
        req = yt.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
        )
        fetched = 0
        while req is not None:
            try:
                resp = req.execute()
            except HttpError as e:
                print(f"  [youtube] playlist {playlist_id}: {e}", file=sys.stderr)
                return
            for it in resp.get("items", []):
                vid = it.get("contentDetails", {}).get("videoId")
                if not vid:
                    continue
                yield vid
                fetched += 1
                if limit and fetched >= limit:
                    return
            req = yt.playlistItems().list_next(req, resp)

    def _fetch_video_details(self, yt, video_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for batch in _batched(video_ids, 50):
            resp = yt.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(batch),
                maxResults=50,
            ).execute()
            for v in resp.get("items", []):
                out[v["id"]] = v
        return out

    # ── Takeout parsing ─────────────────────────────────────────────────────

    @staticmethod
    def _load_takeout_watch_history(path: Path) -> set[str]:
        """
        Read a Google Takeout `watch-history.json` (YouTube > history) and
        return the set of 11-char video IDs referenced.
        """
        if not path.exists():
            print(f"  [youtube] watch_history_json path not found: {path}", file=sys.stderr)
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [youtube] reading watch history: {e}", file=sys.stderr)
            return set()
        pat = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
        ids: set[str] = set()
        for rec in data if isinstance(data, list) else []:
            url = rec.get("titleUrl", "") or ""
            m = pat.search(url)
            if m:
                ids.add(m.group(1))
        return ids

    # ── Item builders ───────────────────────────────────────────────────────

    def _channel_item(self, ch: dict, subscribed: bool) -> Item:
        channel_id = ch["channel_id"]
        return Item(
            title=ch["channel_title"] or "(unnamed channel)",
            url=f"https://www.youtube.com/channel/{channel_id}",
            source=self.name,
            kind="channel",
            path=["YouTube", "channels"],
            date_added=None,
            extras={
                "channel_id":          channel_id,
                "subscribed":          bool(subscribed),
                "subscriber_count":    int(ch.get("subscriber_count", 0)),
                "video_count":         int(ch.get("video_count", 0)),
                "description":         (ch.get("description") or "")[:DEFAULT_DESCRIPTION_CHARS],
                "uploads_playlist_id": ch.get("uploads_playlist_id", ""),
                "thumbnail":           ch.get("thumbnail", ""),
            },
        )

    def _video_item(self, d: dict, fl: dict) -> Item:
        sn = d.get("snippet", {})
        cd = d.get("contentDetails", {})
        st = d.get("statistics", {})

        video_id = d["id"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        channel_title = sn.get("channelTitle", "") or "Unknown"
        published = sn.get("publishedAt", "") or ""
        description = (sn.get("description") or "").strip()

        # YouTube tags are distinct from the user's bookmark tags — keep them
        # in a dedicated field so USER_EDITABLE.tags stays the user's own.
        yt_tags = sn.get("tags", []) or []

        return Item(
            title=sn.get("title", "") or "(untitled)",
            url=url,
            source=self.name,
            kind="video",
            path=["YouTube", "videos", channel_title],
            date_added=published[:10] or None,
            extras={
                "video_id":              video_id,
                "channel":               channel_title,
                "channel_id":            sn.get("channelId", ""),
                "published_at":          published,
                "duration":              _iso_duration_to_human(cd.get("duration", "")),
                "view_count":            int(st.get("viewCount", 0) or 0),
                "like_count":            int(st.get("likeCount", 0) or 0),
                "liked":                 bool(fl.get("liked")),
                "watched":               bool(fl.get("watched")),
                "subscribed_to_channel": bool(fl.get("subscribed_to_channel")),
                "description":           description[:DEFAULT_DESCRIPTION_CHARS],
                "youtube_tags":          [str(t) for t in yt_tags[:20]],
            },
        )
