# Qivaryn

Qivaryn is a high-performance, multi-platform media downloader with a shared command-line interface and Python API. Its platform integrations handle the details that differ between services, while common download options and history management work the same way wherever supported. Instagram is the first supported platform; support for additional platforms is planned.

---

## Features

- **Multi-platform design**: A shared downloader interface is built to support multiple media platforms through platform-specific integrations.
- **Automatic URL Detection**: Pass a URL from a supported platform and Qivaryn routes it to the appropriate downloader.
- **High-quality downloads**: The current Instagram integration selects available progressive or DASH video streams (up to 1080p) and downloads carousel slides in original resolution.
- **Media Filtering**: Filter account downloads by media type where the platform supports it. Available types can vary by platform.
- **DASH Audio & Video Merging**: On platforms that provide split DASH streams, Qivaryn uses FFmpeg to merge audio and video.
- **Batch Limits (`--count`)**: Specify the exact number of **new** items to save. Previously downloaded content is automatically skipped without counting toward the quota.
- **Download Resumption**: Tracks downloaded item identifiers in a local SQLite database (`qivaryn_state.db`), allowing previously saved items to be skipped. The current Instagram integration tracks post shortcodes.
- **Dual Interface**: Offers both a command-line interface (CLI) and a Python library API.

### Supported platforms

- **Instagram** — currently supported.
- **Additional platforms** — planned; not supported yet.

Commands and common options are intended to remain consistent across platforms. URL formats, authentication methods, media filters, and other service-specific options may differ.

---

## Requirements

- **Python 3.8** or newer
- **FFmpeg** available on your system `PATH` when downloading from a platform that requires it to merge DASH audio and video.
- Authentication is platform-specific. For Instagram, an optional session (`--cookies cookies.txt` or `--sessionid`) may be needed for private content, deeper pagination, or higher rate limits.

---

## Installation

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

Download content from a supported platform with one command. This example uses Instagram, the platform currently supported:

```bash
qivaryn "https://www.instagram.com/USERNAME/"
```

---

## Command-Line Usage

### 1. Automatic URL Command

Pass a URL from a supported platform directly to the CLI. Qivaryn detects the platform and content type from the URL:

```console
qivaryn "<SUPPORTED_URL>" [OPTIONS]
```

For the currently supported Instagram URLs, Qivaryn routes automatically:

- **Single Post / Reel URL** (`/p/SHORTCODE/` or `/reel/SHORTCODE/`):
  Downloads that specific item. If it is a carousel, all slides (images and videos) are downloaded.
- **Reels Tab URL** (`/<username>/reels/`):
  Scans the dedicated Reels tab and downloads Reels chronologically.
- **Account Profile URL** (`/<username>/`):
  - When `--type` is omitted or `all`: downloads grid posts (photos and carousels) and continues into the dedicated Reels tab.
  - When `--type posts`: downloads photos and carousels from the profile grid.
  - When `--type carousels`: downloads only multi-slide carousel albums.
  - When `--type reels`: downloads videos from the Reels tab.

#### Available Options

| Option | Shorthand | Description | Default |
| --- | --- | --- | --- |
| `--type TYPE` | | Filter media where supported. Instagram types include `all`, `posts`, `reels`, `videos`, or `carousels`. | `all` |
| `--count N` | `-n N`, `--limit N` | Maximum number of **new** items to save before stopping. | Unlimited |
| `--cookies PATH` | `--cookies-file` | Instagram: path to a Netscape-format `cookies.txt` file. Authentication options may vary by platform. | None |
| `--sessionid STR` | | Instagram-specific `sessionid` cookie value. | None |
| `--output DIR` | `-o DIR` | Directory to save downloaded media files. | `./downloads` |
| `--db PATH` | | Path to SQLite state database for download tracking. | `qivaryn_state.db` |
| `--max-pages N` | | Maximum feed pagination pages to scan. | Unlimited |
| `--force` | `-f` | Re-download and overwrite even if already saved. | `False` |

The explicit `single`, `download`, and `clear-db` subcommands also accept `--platform`. At present, the available platform choice is `instagram`.

---

### 2. Explicit Subcommands

If you prefer explicit subcommands instead of automatic URL detection:

#### `qivaryn single`

Downloads a single item by URL or identifier. The accepted identifier depends on the selected platform. For Instagram:

```console
qivaryn single "https://www.instagram.com/p/SHORTCODE/"
qivaryn single "https://www.instagram.com/p/SHORTCODE/" --output ./my_media
qivaryn single SHORTCODE --cookies ./cookies.txt
```

#### `qivaryn download`

Downloads account content using a platform-specific account name or identifier. For Instagram:

```console
# Download up to 25 new Reels from an account
qivaryn download natgeo --type reels --count 25

# Download up to 10 image / carousel posts
qivaryn download natgeo --type posts --count 10

# Download only multi-item carousel albums
qivaryn download natgeo --type carousels --count 5

# Download with an explicit early-exit threshold (halt after 5 consecutive known items)
qivaryn download natgeo --threshold 5
```

---

## Media Filtering Guide (`--type`)

Media type names are platform-specific. The following values describe the current Instagram integration; available content and filters may differ for other platform integrations in the future.

| Value | What It Downloads | Excludes |
| --- | --- | --- |
| `all` *(default)* | All posts, photos, carousels, and Reels. | None |
| `posts` | Profile grid image posts and multi-item carousels. | Standalone video Reels |
| `carousels` | Multi-slide carousel albums only (multi-image / multi-video). | Single-photo posts and Reels |
| `reels` (or `videos`) | Video Reels from the dedicated Reels tab or feed. | Static image posts |

---

## Instagram Authentication (`cookies.txt` and `--sessionid`)

Authentication requirements depend on the platform. For Instagram, public accounts and Reels can often be downloaded without logging in. An authenticated session may be needed for:

- Private accounts you follow.
- Deep pagination of account post grids.
- Avoiding Instagram rate limits (HTTP 429).

### How to Export `cookies.txt` from Your Browser

1. In Google Chrome, Microsoft Edge, or Firefox, install a cookie export extension:
   - **Get cookies.txt LOCALLY** (recommended, open source)
   - **Cookie-Editor** (select *Export* → *Netscape*)
2. Navigate to [instagram.com](https://www.instagram.com) and ensure you are logged into your account.
3. Click the extension icon and export the cookies in **Netscape format**.
4. Save the file as `cookies.txt` in your project folder (or pass its absolute path):
   ```console
   qivaryn "https://www.instagram.com/USERNAME/" --cookies cookies.txt
   ```

### Using `--sessionid` Directly

Alternatively, copy your `sessionid` cookie value directly from your browser:

1. Open [instagram.com](https://www.instagram.com) in your browser.
2. Press `F12` to open Developer Tools.
3. Go to **Application** (or **Storage** in Firefox) → **Cookies** → `https://www.instagram.com`.
4. Copy the value of the `sessionid` cookie and pass it:
   ```console
   qivaryn "https://www.instagram.com/USERNAME/" --sessionid "YOUR_SESSION_ID"
   ```

> [!IMPORTANT]
> Treat `cookies.txt` and session IDs like passwords. Do not commit them to Git. Qivaryn's `.gitignore` automatically ignores `cookies.txt`.

---

## Managing Download History (`clear-db`)

Qivaryn records downloaded item identifiers in `qivaryn_state.db` so you can skip previously saved files. For Instagram, these identifiers are post shortcodes. The `clear-db` command manages download history without deleting media files from disk:

```console
# Remove a single post from the database so it can be re-downloaded
qivaryn clear-db SHORTCODE
qivaryn clear-db --item "https://www.instagram.com/p/SHORTCODE/"

# Prune database records for media files that were deleted from disk
qivaryn clear-db --missing

# Clear the entire download history (prompts for confirmation)
qivaryn clear-db

# Clear the entire history without a confirmation prompt
qivaryn clear-db --yes
```

---

## Python Library API

You can embed Qivaryn into your own Python applications, automation scripts, and background workers. The example below uses the current Instagram integration; available methods and options may vary by platform:

```python
from qivaryn import QivarynEngine

# Initialize the engine
engine = QivarynEngine(
    output_dir="./downloads",
    db_path="./qivaryn_state.db",
    cookies_file="./cookies.txt",  # Optional: omit for public content
    # sessionid="YOUR_SESSION_ID", # Alternative to cookies_file
    consecutive_threshold=3,       # Stop after N consecutive known items
)

# 1. Download automatically based on URL with count limit and media type filter
engine.download_url(
    "https://www.instagram.com/natgeo/",
    count=20,
    media_type="posts",  # 'all', 'posts', 'reels', 'videos', or 'carousels'
)

# 2. Download from an account by username
engine.download_account(
    username="natgeo",
    count=10,
    media_type_filter="carousels",
)

# 3. Download a single post or Reel
engine.download_single(
    "https://www.instagram.com/reel/C7xYabcdefG/",
    force=False,
)
```

See [`qivaryn/examples/library_usage.py`](qivaryn/examples/library_usage.py) for a complete runnable script.

---

## Development & Testing

Activate your virtual environment and install test dependencies:

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
