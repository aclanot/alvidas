# Telegram Video Downloader Bot

A Telegram bot that downloads videos from TikTok, Instagram, YouTube, X/Twitter, Facebook, Reddit, and 1000+ other sites using the [reclip](https://github.com/averygan/reclip) download engine.

## Features

- Downloads videos from 1000+ sites (via yt-dlp)
- Supports Instagram with cookies (base64 encoded)
- Runs as a Telegram bot - just send links
- Deploys easily on Railway

## Deployment on Railway

### 1. Fork/Clone this repository

Push this code to a GitHub repository or create a new one.

### 2. Create Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow instructions
3. Copy the **bot token** (looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 3. Deploy to Railway

1. Go to [Railway](https://railway.app) and create an account
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your repository
4. Add environment variables (see below)
5. Deploy!

### 4. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | Your bot token from @BotFather |
| `INSTAGRAM_COOKIES_BASE64` | ❌ Optional | Base64-encoded cookies.txt for Instagram |

### 5. Getting Instagram Cookies (Optional)

For private Instagram content, you need to provide cookies:

1. Install browser extension like "Get cookies.txt" (Chrome/Firefox)
2. Log into Instagram in your browser
3. Export cookies as `cookies.txt`
4. Encode to base64:
   ```bash
   base64 -i cookies.txt -o cookies_base64.txt
   ```
   Or:
   ```bash
   cat cookies.txt | base64
   ```
5. Copy the base64 string to `INSTAGRAM_COOKIES_BASE64` environment variable

## File Structure

```
.
├── bot.py              # Main bot code (reclip logic)
├── requirements.txt    # Python dependencies
├── Procfile            # Railway worker process
├── railway.toml        # Railway configuration
└── README.md           # This file
```

## Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Install yt-dlp and ffmpeg
# macOS: brew install yt-dlp ffmpeg
# Ubuntu: apt install ffmpeg && pip install yt-dlp

# Set environment variables
export TELEGRAM_BOT_TOKEN=your_token_here
export INSTAGRAM_COOKIES_BASE64=your_base64_cookies_optional

# Run
python bot.py
```

## How It Works

The bot uses the **same download logic** from [reclip](https://github.com/averygan/reclip):

1. **Video Info** - Uses `yt-dlp -j` to fetch metadata
2. **Download** - Uses `yt-dlp` with appropriate format options
3. **Upload** - Sends the video file to Telegram chat

### Key Code from reclip (app.py lines 16-74):

```python
def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)
    # ... subprocess run
```

### My Adaptation:

- Integrated into Telegram bot framework
- Added Instagram cookies support via base64 env variable
- Added background threading for downloads
- Added Telegram upload with size limits (50MB)

## Bot Commands

- `/start` - Welcome message
- `/help` - Usage instructions
- `/status` - Check active downloads (admin)

## Supported Sites

All sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md):

- TikTok
- Instagram (with cookies)
- YouTube
- X/Twitter
- Facebook
- Reddit
- Vimeo
- Twitch
- Dailymotion
- SoundCloud
- Loom
- Pinterest
- Tumblr
- And 1000+ more!

## License

MIT (same as reclip)
