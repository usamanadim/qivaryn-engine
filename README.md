# Qivaryn

Qivaryn is a high-performance, multi-platform media downloader with a shared command-line interface and Python API. Its platform integrations handle the details that differ between services, while common download options and history management work the same way everywhere.

---

## Features

- **Four platforms, one engine**: Instagram, TikTok, YouTube and Facebook share the same CLI, filtering, batch limits, resume logic and state database.
- **Automatic URL Detection**: Pass a URL from any supported platform and Qivaryn routes it to the right downloader.
- **Native integrations**: Every platform is handled by Qivaryn's own scrapers — no `yt-dlp` or other downloader dependency.
- **High-quality downloads**: Platform integrations pick the best available progressive or adaptive stream (up to 1080p where offered) and download carousel slides in original resolution.
- **Media Filtering**: Filter account downloads by media type where the platform supports it.
- **Audio & Video Merging**: When a platform provides split video/audio streams (Instagram DASH, YouTube adaptive formats), Qivaryn uses FFmpeg to merge them.
- **Batch Limits (`--count`)**: Specify the exact number of **new** items to save. Previously downloaded content is skipped without counting toward the quota.
- **Download Resumption**: Tracks downloaded items per platform in a local SQLite database (`qivaryn_state.db`).
- **Feed Scanning**: Count available and already-downloaded items without downloading them.
- **Dual Interface**: Offers both a command-line interface (CLI) and a Python library API.

### Features added in v0.2.1 (included in PyPI 0.2.2)

- **Scan account feeds without downloading** with `qivaryn scan <PROFILE_URL>`.
  The scan reports the total items found, how many are already downloaded, and
  how many remain. The Python API provides the same feature through
  `QivarynEngine.scan_account()`.
- **Configure early exit during downloads** with `--threshold N` or the
  `consecutive_threshold` Python option. The default `0` scans the full feed;
  a positive value stops after that many consecutive already-downloaded items.
  Early exit applies when `--count` is not set.

### Supported platforms

| Platform | Single items | Account feeds | Playlists | Notes |
| --- | --- | --- | --- | --- |
| **Instagram** | Posts, Reels, carousels | Profile grid + Reels tab | — | Age-gated profiles need an adult session |
| **TikTok** | Videos, photo posts | Profile feed | — | Public posts and profiles need no cookies; best available resolution (up to 720×1280) |
| **YouTube** | Videos, Shorts, live URLs | Videos, Shorts, Streams tabs | Yes | See [limitations](#platform-limitations) |
| **Facebook** | Videos, Reels | Page videos tab (best effort) | — | Single videos work anonymously; some page feeds need a session |

Run `qivaryn platforms` to see the available platforms and filters in your installed version.

---

## Requirements

- **Python 3.10** or newer
- **FFmpeg** available on your system `PATH` when downloading content with split audio/video streams (Instagram DASH, YouTube adaptive formats).
- Authentication is optional but platform-specific. See [Authentication](#authentication).

---

## Installation

### Install from PyPI (Recommended)

```console
python -m pip install --upgrade qivaryn-engine
```

### Install from local checkout (Recommended for development)

```console
git clone https://github.com/usamanadim/qivaryn-engine.git
cd qivaryn-engine
python -m pip install -e .
```

### Install latest development version from GitHub

```bash
python -m pip install --upgrade "git+https://github.com/usamanadim/qivaryn-engine.git@main"
```

---

## Quick Start (Cheatsheet)

```console
# Instagram profile
qivaryn "https://www.instagram.com/USERNAME/"

# TikTok profile
qivaryn "https://www.tiktok.com/@USERNAME"

# YouTube channel
qivaryn "https://www.youtube.com/@CHANNEL/videos"

# Facebook page
qivaryn "https://www.facebook.com/PAGE/videos"
```

Qivaryn detects the platform from the URL, so `--platform` is only needed for the explicit subcommands.

---

## Where Files Are Saved

Everything is organised by platform and by the account that owns the media.
The folders are created automatically the first time content is downloaded and
reused afterwards.

```text
Downloads/
└── Qivaryn/
    ├── Instagram/
    │   ├── account_username_1/
    │   │   ├── reels/
    │   │   └── posts/
    │   └── account_username_2/
    ├── YouTube/
    │   └── channel_name/
    ├── TikTok/
    │   └── account_username/
    └── Facebook/
        └── account_name/
```

- `--output DIR` (default `./Downloads/Qivaryn`) sets the root; the platform
  folders are created inside it. Point it at your system Downloads folder with
  `-o "$HOME/Downloads/Qivaryn"` if you prefer.
- The account folder uses the real owner of the media: the Instagram or TikTok
  username, the YouTube channel name, the Facebook page name.
- Instagram additionally splits each account into `reels/`, `posts/` and
  `carousels/`.
- Media whose owner cannot be determined is stored directly in the platform
  folder.

---

## Command-Line Usage

### 1. Automatic URL Command

```console
qivaryn "<SUPPORTED_URL>" [OPTIONS]
```

| URL shape | What happens |
| --- | --- |
| Instagram `/p/…`, `/reel/…` | Downloads that item (all carousel slides included) |
| Instagram `/<username>/` | Downloads the profile grid, continuing into the Reels tab |
| Instagram `/<username>/reels/` | Scans the dedicated Reels tab |
| TikTok `/@user/video/<id>`, `/@user/photo/<id>` | Downloads that item |
| TikTok `/@user` | Downloads the profile feed |
| TikTok `vm.tiktok.com/…` | Resolves the short link, then downloads that item |
| YouTube `youtu.be/<id>`, `/watch?v=<id>`, `/shorts/<id>` | Downloads that video or Short |
| YouTube `/playlist?list=<id>` | Downloads every video in the playlist |
| YouTube `/@handle`, `/channel/<id>` | Downloads the channel's Videos, Shorts and Streams tabs |
| YouTube `/@handle/videos`, `/shorts`, `/streams` | Downloads that tab only |
| Facebook `/watch/?v=<id>`, `/reel/<id>`, `/page/videos/<id>` | Downloads that video or Reel |
| Facebook `/<page>`, `/<page>/videos` | Scans the page's videos tab (best effort) |

#### Available Options

| Option | Shorthand | Description | Default |
| --- | --- | --- | --- |
| `--platform NAME` | | Platform for explicit subcommands: `instagram`, `tiktok`, `youtube`, `facebook`. | `instagram` |
| `--type TYPE` | | Filter media where supported. Values depend on the platform. | `all` |
| `--quality Q` | `-q Q` | YouTube only: `best`, `worst`, or a maximum height (`1080`, `720p`). Unavailable heights fall back to the closest lower resolution after confirmation. | `best` |
| `--count N` | `-n N`, `--limit N` | Maximum number of **new** items to save before stopping. | Unlimited |
| `--cookies PATH` | `--cookies-file` | Path to a Netscape-format `cookies.txt` file. | None |
| `--sessionid STR` | | Direct `sessionid` cookie value (Instagram and TikTok only). | None |
| `--output DIR` | `-o DIR` | Root directory that holds the platform folders. | `./Downloads/Qivaryn` |
| `--db PATH` | | Path to SQLite state database for download tracking. | `qivaryn_state.db` |
| `--max-pages N` | | Maximum feed pagination pages to scan. | Unlimited |
| `--threshold N` | | Stop after N consecutive already-downloaded items; `0` scans the full feed. | `0` |
| `--force` | `-f` | Re-download and overwrite even if already saved. | `False` |

### Scan without downloading

Use `qivaryn scan` to count items in a supported account feed and see how many
are already downloaded and how many remain:

```console
qivaryn scan "https://www.instagram.com/USERNAME/"
qivaryn scan "https://www.instagram.com/USERNAME/reels/" --cookies cookies.txt
```

Single-item URLs are identified without fetching or downloading the item.

### 2. Explicit Subcommands

#### `qivaryn single`

Downloads a single item by URL or identifier:

```console
qivaryn single "https://www.instagram.com/p/SHORTCODE/"
qivaryn single "https://www.tiktok.com/@user/video/7300000000000000000"
qivaryn single "https://youtu.be/VIDEO_ID"
qivaryn single "https://www.facebook.com/watch/?v=123456789"

# YouTube quality selection
qivaryn single "https://youtu.be/VIDEO_ID" --platform youtube --quality 720
```

`--quality` is YouTube-only. On other platforms Qivaryn keeps the default
stream and prints a warning. If the requested height is unavailable, it asks
whether to continue at the closest lower resolution.

YouTube files are named after the original video title, for example
`youtube_jNQXAC9IVRw_Me at the zoo.mp4`. Characters that are illegal in a
filename are replaced with spaces and the title is trimmed to 100 characters.

#### `qivaryn download`

Downloads account or channel content. Use `--platform` when the account name is not prefixed by a URL:

```console
# Instagram Reels
qivaryn download natgeo --type reels --count 25

# YouTube Shorts from a channel handle
qivaryn download @somechannel --platform youtube --type shorts --count 10

# YouTube at 1080p (or the closest lower resolution after confirmation)
qivaryn download @somechannel --platform youtube --quality 1080 --count 10

# TikTok profile feed
qivaryn download someuser --platform tiktok --count 20

# Facebook page videos
qivaryn download somepage --platform facebook
```

#### `qivaryn platforms`

Lists the supported platforms and their media filters:

```console
qivaryn platforms
```

---

## Media Filtering Guide (`--type`)

| Platform | Values | Notes |
| --- | --- | --- |
| **Instagram** | `all`, `posts`, `reels`, `videos`, `carousels` | `posts` = images + carousels; `videos` = `reels` |
| **TikTok** | `all`, `videos`, `photos` | `photos` includes multi-image photo posts |
| **YouTube** | `all`, `videos`, `shorts`, `streams` | Each value maps to the matching channel tab |
| **Facebook** | `all`, `videos` | Videos and Reels |

Requesting a filter a platform does not support fails with a clear message rather than silently downloading something else.

---

## Authentication

Cookies are **optional** and never required to run Qivaryn. Public TikTok, YouTube and Facebook content downloads anonymously; Instagram is the platform that most often needs a session. When you do need cookies, export them with a browser extension such as **Get cookies.txt LOCALLY** or **Cookie-Editor** (select *Export* → *Netscape*), then:

```console
qivaryn "https://www.instagram.com/USERNAME/" --cookies cookies.txt
```

| Platform | When cookies help |
| --- | --- |
| **Instagram** | Private accounts, age-gated profiles, deeper pagination and fewer HTTP 429 responses. This is the platform most likely to need a session. |
| **TikTok** | Private accounts, or posts that are not visible in the public creator feed |
| **YouTube** | Age-restricted videos and "confirm you're not a bot" challenges |
| **Facebook** | Public page video tabs that Facebook gates behind a login wall |

If a request is refused, Qivaryn retries anonymous fallbacks first and only then suggests `--cookies`.

### Using `--sessionid` Directly

For Instagram and TikTok you can pass the `sessionid` cookie value instead of a cookies file:

```console
qivaryn "https://www.instagram.com/USERNAME/" --sessionid "YOUR_SESSION_ID"
```

> [!IMPORTANT]
> Treat `cookies.txt` and session IDs like passwords. Do not commit them to Git. Qivaryn's `.gitignore` automatically ignores `cookies.txt`.

---

## Managing Download History (`clear-db`)

Download history is stored per platform, so an Instagram shortcode and a TikTok video ID can never collide. `clear-db` manages history without deleting media files:

```console
# Remove a single item so it can be re-downloaded
qivaryn clear-db SHORTCODE
qivaryn clear-db --item "https://www.instagram.com/p/SHORTCODE/"

# Limit the operation to one platform
qivaryn clear-db --platform youtube --yes

# Prune records for media files that were deleted from disk
qivaryn clear-db --missing

# Clear one platform's history (prompts for confirmation)
qivaryn clear-db --platform tiktok
```

Databases created by older versions are migrated automatically: existing rows are kept and attributed to Instagram.

---

## File Naming

- **Instagram** keeps the historical `<shortcode>.<ext>` layout.
- **Other platforms** use `<platform>_<id>.<ext>` so files from different services never overwrite each other.
- **Carousels** append a slide index: `<stem>_1.<ext>`, `<stem>_2.<ext>`, …

---

## Python Library API

```python
from qivaryn import QivarynEngine

engine = QivarynEngine(
    platform="youtube",             # "instagram", "tiktok", "youtube" or "facebook"
    output_dir="./Downloads/Qivaryn",  # Platform folders are created inside it
    db_path="./qivaryn_state.db",
    cookies_file="./cookies.txt",   # Optional: omit for public content
    consecutive_threshold=0,        # 0 scans the full feed; N enables early exit
    allow_network_fallbacks=True,   # Retry without the session when it expires
)

# 1. Download automatically from a URL
engine.download_url(
    "https://www.youtube.com/@CHANNEL/videos",
    count=20,
    media_type="videos",
)

# 2. Download account/channel content
engine.download_account(
    "@CHANNEL",
    media_type_filter="shorts",
    count=10,
)

# 3. Download a single item
engine.download_single("https://youtu.be/VIDEO_ID", force=False)

# 4. Playlists (YouTube)
engine.download_playlist("PLxxxxxxxxxxxxxxxx")

# 5. Count account feed items without downloading
summary = engine.scan_account("@CHANNEL", media_type_filter="all")
```

`InstagramEngine` remains available as an alias of `QivarynEngine`, and the platform clients are importable directly:

```python
from qivaryn import InstagramClient, TikTokClient, YouTubeClient, FacebookClient
```

> [!TIP]
> Set `allow_network_fallbacks=False` to disable the secondary unauthenticated
> requests Qivaryn makes when a session expires (clean retries and HTML timeline
> scraping). This is useful for offline runs and keeps unit tests off the network.

See [`qivaryn/examples/library_usage.py`](qivaryn/examples/library_usage.py) for a complete runnable script.

---

## Platform Limitations

Qivaryn uses native scrapers, so some platform-side restrictions cannot be worked around:

- **YouTube**: Formats whose URLs are protected by a `signatureCipher` require executing YouTube's obfuscated JavaScript player. Qivaryn has no JavaScript engine, so a video offering *only* ciphered formats is reported as unsupported instead of producing a broken file. Some streams also download slower without the `n` parameter transform. Live streams in progress are not downloadable.
- **TikTok**: Public posts and profile feeds work without cookies. Profile pagination follows TikTok's public creator feed 15 posts at a time; private accounts and posts hidden from that feed still require `--cookies`. Qivaryn picks the **largest resolution** TikTok offers for a post (up to 720×1280) and only falls back to the 576×1024 standard stream when no higher rendition is listed. TikTok's 1080×1920 rendition is only exposed through its signed mobile API, so it is not reachable.
- **Facebook**: Single video and reel URLs work anonymously. Some page video tabs are gated behind a login wall and require `--cookies`.
- **Instagram**: Public posts usually work anonymously, but Instagram is the platform most likely to demand a session. Age-gated profiles require an adult (19+) session.

---

## Development & Testing

```console
# Run all unit tests
python -m unittest discover -s tests -v

# Build wheels and source distribution
python -m pip install build
python -m build
```

---

## License and Required Attribution

Qivaryn is licensed under the **Qivaryn Attribution License 1.0** in [`LICENSE`](LICENSE). Commercial use is permitted. Anyone using, modifying, incorporating, or distributing Qivaryn must credit the original author:

> **Qivaryn by Usama Nadeem (@usamanadim)** — https://github.com/usamanadim/qivaryn-engine

Machine-readable attribution metadata is available in [`qivaryn/ATTRIBUTION.json`](qivaryn/ATTRIBUTION.json).
