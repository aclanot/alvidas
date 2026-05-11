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


## Telegram upload speed

Changing Python wrapper libraries usually does not make uploads faster because every wrapper still calls the same Telegram Bot API upload methods. This bot uses direct `aiohttp` calls for low overhead and predictable file handling.

For the biggest Telegram upload improvement, run the official Telegram Bot API server close to the bot and enable local mode:

```env
TELEGRAM_API_BASE=http://127.0.0.1:8081
TELEGRAM_LOCAL_MODE=true
```

Only enable `TELEGRAM_LOCAL_MODE=true` when the Bot API server runs with `--local` and can read the same absolute download paths as the bot. In that mode the bot sends `file://` paths instead of streaming multipart file bytes through Python, which reduces bot-side upload overhead.

`HTTP_CONNECTION_LIMIT` controls the shared aiohttp connection pool. Higher values can help busy deployments, but Telegram flood limits still apply.
