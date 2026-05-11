# Alvidas Telegram Media Downloader

Fast Telegram bot for downloading public media from TikTok, Instagram, YouTube, YouTube Shorts, and X/Twitter links.

## Features

- Downloads videos, shorts/reels, photo posts, and carousels.
- Supports Instagram and TikTok photo slideshows with optional audio when available.
- Preserves mixed Instagram carousel order for photos and videos.
- Bounded download/upload/FFmpeg concurrency for stable simultaneous handling.
- Optional cookies and proxies for better reliability on rate-limited platforms.
- Telegram-size validation before upload.

## Required environment

```env
TELEGRAM_BOT_TOKEN=123456:bot_token
```

## Optional environment

See `.env.example` for all options. Most deployments should configure cookies for Instagram and YouTube when public anonymous downloads start failing.

Cookie values must be base64-encoded Netscape `cookies.txt` content:

```bash
base64 -w0 cookies.txt
```

Proxies can be supplied in `PROXY_LIST` as comma-separated full proxy URLs or `host:port:user:pass` values.

## Railway/Docker deployment

The Docker image installs Python dependencies and FFmpeg at build time. Runtime startup only prints versions and starts the bot, so deployments are reproducible and faster than upgrading packages on every boot.
