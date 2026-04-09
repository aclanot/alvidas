# Telegram Video Downloader Bot

A Telegram bot that downloads videos from TikTok, Instagram, YouTube, X/Twitter, Facebook, Reddit, and 1000+ other sites.

Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) via the **reclip** download engine approach.

## Features

- Downloads videos from 1000+ sites (via yt-dlp)
- Supports Instagram/TikTok/Twitter with cookies (base64 encoded)
- Raw Bot API (no heavy libraries) - 50MB limit
- Works in groups — only responds when URLs are detected
- Auto-upgrades yt-dlp on every container start

## Deployment on Railway

### 1. Fork/Clone this repository

Push this code to GitHub.

### 2. Create Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow instructions
3. Copy the **bot token** (looks like `123456789:ABCdef...`)

### 3. Deploy to Railway

1. Go to [Railway](https://railway.app) → New Project → Deploy from GitHub repo
2. Select your repository
3. Add environment variable: `TELEGRAM_BOT_TOKEN`
4. Deploy!

### 4. Optional: Instagram Cookies

For private Instagram content or to avoid rate limits:

1. Log into Instagram in your browser
2. Use extension like "Get cookies.txt" to export cookies
3. Base64 encode: `cat cookies.txt | base64` or `base64 -i cookies.txt`
4. Set `INSTAGRAM_COOKIES_BASE64` environment variable in Railway

## How it Works in Chat

**Private chat:**
```
You: https://tiktok.com/@user/video/123
Bot: 🎵 Downloading…
Bot: [video] 🎵 Video Title 12.5 MB
```

**Group chat:**
```
Someone: Check this out! https://youtube.com/shorts/abc
Bot: ▶️ Downloading…
Bot: [video] ▶️ Video Title 8.2 MB
```

Bot **only responds** when URLs are found. No URL = no response.

## Supported Sites

All sites supported by yt-dlp:

- TikTok (videos + photos)
- Instagram (reels, posts, stories with cookies)
- YouTube (Shorts, videos)
- X / Twitter (videos + photos)
- Facebook
- Reddit
- Vimeo, Dailymotion, Twitch, SoundCloud
- And 1000+ more!

## Architecture

- **bot.py** - Main bot logic, raw Telegram Bot API polling
- **start.py** - Auto-upgrades yt-dlp before starting bot
- **Download logic** - Uses reclip approach: subprocess calls to yt-dlp
- **Dockerfile** - Python slim + ffmpeg

## File Structure

```
.
├── bot.py              # Main bot (raw Bot API, reclip logic)
├── start.py            # Entry point (auto-updates yt-dlp)
├── Dockerfile          # Container with ffmpeg
├── requirements.txt    # aiohttp, yt-dlp
├── railway.json       # Railway config
├── .env.example       # Environment variables template
└── README.md          # This file
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | Bot token from @BotFather |
| `INSTAGRAM_COOKIES_BASE64` | ❌ Optional | Base64 cookies for Instagram |
| `TIKTOK_COOKIES_BASE64` | ❌ Optional | Base64 cookies for TikTok |
| `TWITTER_COOKIES_BASE64` | ❌ Optional | Base64 cookies for Twitter/X |

## Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Install ffmpeg (required)
# macOS: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg

# Set token
export TELEGRAM_BOT_TOKEN=your_token_here

# Run
python start.py
```

## License

MIT
