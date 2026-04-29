# YouTube source plugin

The YouTube plugin logs into your Google account (read-only) and fetches:

- **Subscriptions** → one `channel` item per subscribed channel
- **Liked videos** → one `video` item each, flagged `liked=true, watched=true`
- **Recent uploads from every subscribed channel** → `video` items flagged `subscribed_to_channel=true`
- (Optional) **Watch history from a Google Takeout export** → flags matching videos `watched=true`. The API no longer exposes watch history directly, so Takeout is the only way to get it.

Videos appearing in multiple streams (e.g. a liked video that is also a recent upload from a subscribed channel) are merged into **one** `.md` file with the union of flags set.

## One-time OAuth setup

### 1. Install the Python deps

```bash
source .venv/bin/activate
pip install google-api-python-client google-auth-oauthlib
```

(These are commented out in `requirements.txt` — install only if you use the YouTube source.)

### 2. Create a Google Cloud project and enable the YouTube Data API

- Go to https://console.cloud.google.com/
- Top bar → project picker → **New Project** (any name, e.g. `booki`)
- Left menu → **APIs & Services → Library** → search "YouTube Data API v3" → **Enable**

### 3. Configure the OAuth consent screen

This is required before you can create credentials.

- **APIs & Services → OAuth consent screen**
- User type: **External** → Create
- App name: `booki` (anything), support email, developer email → Save & Continue
- Scopes page → Save & Continue (we request the scope at auth time; nothing to add here)
- **Test users → Add users** — add your own Gmail address. While the app stays in "Testing" mode, only listed test users can sign in. → Save & Continue

### 4. Create an OAuth client ID

- **APIs & Services → Credentials → Create Credentials → OAuth client ID**
- Application type: **Desktop app** ← important, not "Web"
- Name: `booki-cli` → Create
- In the popup, click **Download JSON**

### 5. Drop the file where Booki expects it

```bash
mkdir -p ~/.booki
mv ~/Downloads/client_secret_*.json ~/.booki/youtube-client-secret.json
```

Or put it anywhere and point `client_secret` in `[sources.youtube]` at the path.

### 6. First run — browser consent

```bash
python sync.py --source youtube
```

- A browser tab opens → pick your Google account
- You'll see a "**Google hasn't verified this app**" warning (expected while the app is in Testing) → **Advanced → Go to booki (unsafe) → Allow**
- A refresh token is cached at `~/.booki/youtube-token.json`. Future runs are silent; tokens auto-renew.

## Frontmatter fields

YouTube **video** items add:

```yaml
kind: video
channel: "Fireship"
channel_id: UCsBjURrPoezykLs9EqgamOA
video_id: abc123xyz
published_at: 2025-11-03T14:22:11Z
duration: "1:23"
view_count: 842311
liked: true                   # in your Liked-videos playlist
watched: true                 # liked OR present in Google Takeout watch history
subscribed_to_channel: true   # channel is in your subscriptions
description: "first 600 chars of the video description…"
youtube_tags: ["rust", "intro"]   # the video's own tags (separate from user tags)
```

YouTube **channel** items add: `subscribed`, `subscriber_count`, `video_count`, `uploads_playlist_id`, `description`.

## Config options

```toml
[sources.youtube]
client_secret = "~/.booki/youtube-client-secret.json"
token_path    = "~/.booki/youtube-token.json"

fetch_subscriptions         = true   # emit one channel item per subscription
fetch_liked                 = true   # emit liked videos (watched=true, liked=true)
fetch_subscription_uploads  = true   # emit recent uploads from subscribed channels
uploads_per_channel         = 10     # how many recent uploads per channel to pull
max_liked                   = 500    # cap on liked-videos pulled

# Optional — mark videos as watched=true based on a Google Takeout export:
# watch_history_json = "~/Downloads/Takeout/YouTube/history/watch-history.json"
```

## Downloading videos locally

Videos can be downloaded to disk via [yt-dlp](https://github.com/yt-dlp/yt-dlp) (requires `ffmpeg` on PATH for muxing/audio extraction).

```bash
pip install yt-dlp
# one video, mp4 ≤ [downloads].video_height_max
python download.py https://www.youtube.com/watch?v=VIDEO_ID
# audio-only (.mp3)
python download.py VIDEO_ID --audio
```

In the web UI, videos show **⬇️ mp4** and **🎵 mp3** buttons in the detail drawer. Clicks are fire-and-forget: a background thread runs yt-dlp and the item's frontmatter is stamped with `downloaded: true` + `download_path` when finished. The drawer polls and switches to ✓ Downloaded.

Files land under `[downloads].dir` (default: `./downloads/<uploader>/<title> [<id>].<ext>`) — configurable in `config.toml`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `access_denied` / `403` during consent | Your Gmail isn't in the OAuth consent screen's test-user list, or the API isn't enabled on the project. |
| `invalid_client` | Wrong OAuth-client type. Must be **Desktop app**, not Web. |
| `ImportError: google.oauth2` | `pip install google-api-python-client google-auth-oauthlib` |
| Token seems stale / want to re-auth | `rm ~/.booki/youtube-token.json` and re-run; or revoke at https://myaccount.google.com/permissions |
| Liked videos empty | Your "Liked videos" playlist is private to the API unless you're logged in as the same Google account you authorized — normally it just works. |
