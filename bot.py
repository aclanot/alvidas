import os
import re
import json
import base64
import shutil
import random
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import yt_dlp

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ── config ──
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
API = f"{BOT_API_BASE}/bot{BOT_TOKEN}"
TELEGRAM_LOCAL_MODE = os.environ.get("TELEGRAM_LOCAL_MODE", "").lower() in {"1", "true", "yes", "on"}
INSTAGRAM_COOKIES_B64 = os.environ.get("INSTAGRAM_COOKIES_BASE64", "")
PROXY_LIST_RAW = os.environ.get("PROXY_LIST", "")
ADMIN_CHAT_IDS_RAW = os.environ.get("ADMIN_CHAT_IDS") or os.environ.get("ADMIN_CHAT_ID", "")
MAX_TG = 50 * 1024 * 1024
DL_DIR = Path(tempfile.gettempdir()) / "downloads"
DL_DIR.mkdir(exist_ok=True)
COOKIE_DIR = DL_DIR / "cookies"
COOKIE_DIR.mkdir(exist_ok=True)
COOKIES = {}
MAX_PARALLEL_DOWNLOADS = max(1, int(os.environ.get("MAX_PARALLEL_DOWNLOADS", "3")))
MAX_PARALLEL_UPLOADS = max(1, int(os.environ.get("MAX_PARALLEL_UPLOADS", "2")))
MAX_PARALLEL_FFMPEG = max(1, int(os.environ.get("MAX_PARALLEL_FFMPEG", "1")))
MAX_LINKS_PER_MESSAGE = max(1, int(os.environ.get("MAX_LINKS_PER_MESSAGE", "20")))
HTTP_CONNECTION_LIMIT = max(10, int(os.environ.get("HTTP_CONNECTION_LIMIT", "100")))


# ── proxies ──
PROXIES = []
for entry in PROXY_LIST_RAW.split(","):
    entry = entry.strip()
    if not entry:
        continue
    parts = entry.split(":")
    if len(parts) == 4:
        PROXIES.append(f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}")
    elif entry.startswith(("http", "socks")):
        PROXIES.append(entry)
if PROXIES:
    log.info("Loaded %d proxies", len(PROXIES))
ADMIN_CHAT_IDS = [chat_id.strip() for chat_id in ADMIN_CHAT_IDS_RAW.split(",") if chat_id.strip()]
DISABLED_PROXIES = set()
PROXY_LAST_ERROR = {}

# ── cookies from base64 env vars ──
COOKIE_ENV = {
    "instagram": INSTAGRAM_COOKIES_B64,
    "youtube":   os.environ.get("YOUTUBE_COOKIES_BASE64", ""),
    "twitter":   os.environ.get("TWITTER_COOKIES_BASE64", ""),
    "tiktok":    os.environ.get("TIKTOK_COOKIES_BASE64", ""),
}
for platform, b64 in COOKIE_ENV.items():
    if not b64:
        continue
    try:
        raw = base64.b64decode(b64)
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("latin-1")
        text = text.replace("\r\n", "\n")
        fixed_lines = []
        for line in text.split("\n"):
            if line.startswith("#") or not line.strip():
                fixed_lines.append(line)
            elif "\t" not in line and " " in line:
                fixed_lines.append(re.sub(r" +", "\t", line))
            else:
                fixed_lines.append(line)
        text = "\n".join(fixed_lines)
        if not text.startswith("# Netscape"):
            text = "# Netscape HTTP Cookie File\n\n" + text
        p = COOKIE_DIR / f"{platform}.txt"
        p.write_text(text, encoding="utf-8")
        COOKIES[platform] = str(p)
        data_lines = [l for l in text.split("\n") if l.strip() and not l.startswith("#")]
        tabs_ok = all("\t" in l for l in data_lines)
        log.info("Cookies %s: %d lines, tabs=%s, path=%s", platform, len(data_lines), tabs_ok, p)
    except Exception as e:
        log.error("Cookie error %s: %s", platform, e)

# ── URL patterns ──
PATTERNS = {
    "tiktok": [r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+"],
    "instagram": [r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv|stories|share)/(?:[\w.-]+/?){1,3}(?:\?\S*)?"],
    "twitter": [
        r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/\w+/status/\d+(?:\?\S*)?",
        r"https?://(?:www\.)?x\.com/i/status/\d+(?:\?\S*)?",
    ],
    "youtube": [
        r"https?://(?:www\.|m\.)?youtube\.com/watch\?[^\s]*v=[\w-]+[^\s]*",
        r"https?://(?:www\.|m\.)?youtube\.com/(?:shorts|embed|live)/[\w-]+(?:\?\S*)?",
        r"https?://youtu\.be/[\w-]+(?:\?\S*)?",
        r"https?://music\.youtube\.com/watch\?[^\s]*v=[\w-]+[^\s]*",
    ],
}
EMOJI = {"tiktok": "🎵", "instagram": "📷", "twitter": "𝕏", "youtube": "▶️"}

session: aiohttp.ClientSession = None
busy = set()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
UPLOAD_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)
FFMPEG_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_FFMPEG)


async def get_session():
    global session
    if session is None or session.closed:
        connector = aiohttp.TCPConnector(
            limit=HTTP_CONNECTION_LIMIT,
            limit_per_host=max(10, HTTP_CONNECTION_LIMIT // 2),
            ttl_dns_cache=300,
            keepalive_timeout=30,
        )
        session = aiohttp.ClientSession(connector=connector)
    return session


def current_proxy():
    proxies = active_proxies()
    if not proxies:
        return None
    return random.choice(proxies)


def active_proxies():
    return [proxy for proxy in PROXIES if proxy not in DISABLED_PROXIES]


def current_http_proxy():
    proxy = current_proxy()
    if proxy and proxy.startswith(("http://", "https://")):
        return proxy
    return None


def mask_proxy(proxy):
    if not proxy:
        return "none"
    parsed = urlparse(proxy)
    if parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    return re.sub(r"://[^:@]+:[^@]+@", "://***:***@", proxy)


def sanitize_proxy_detail(detail):
    return re.sub(r"://[^:@\s]+:[^@\s]+@", "://***:***@", str(detail))[:120]


def proxy_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "Check proxies", "callback_data": "proxy_check"},
        ]]
    }


def proxy_status_text(prefix="Proxy settings"):
    if not PROXIES:
        return f"{prefix}\n\nNo proxies configured. Set PROXY_LIST in Railway to enable proxies."
    active = len(active_proxies())
    disabled = len(DISABLED_PROXIES)
    return (
        f"{prefix}\n\n"
        f"Configured: {len(PROXIES)}\n"
        f"Active: {active}\n"
        f"Disabled: {disabled}\n"
        "Downloads use active proxies automatically."
    )


async def check_one_proxy(proxy):
    s = await get_session()
    started = asyncio.get_event_loop().time()
    try:
        async with s.get(
            "https://api.ipify.org?format=json",
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            if r.status != 200:
                return False, f"HTTP {r.status}", None
            data = await r.json()
            elapsed = asyncio.get_event_loop().time() - started
            return True, data.get("ip", "ok"), elapsed
    except Exception as e:
        return False, sanitize_proxy_detail(str(e).split("\n")[0]), None


async def check_proxies_text():
    if not PROXIES:
        return proxy_status_text("Proxy check")
    results = await asyncio.gather(*(check_one_proxy(proxy) for proxy in PROXIES))
    newly_disabled = []
    recovered = []

    for i, (proxy, result) in enumerate(zip(PROXIES, results), start=1):
        ok, detail, elapsed = result
        if ok:
            PROXY_LAST_ERROR.pop(proxy, None)
            if proxy in DISABLED_PROXIES:
                DISABLED_PROXIES.discard(proxy)
                recovered.append(proxy)
        else:
            PROXY_LAST_ERROR[proxy] = detail
            if proxy not in DISABLED_PROXIES:
                DISABLED_PROXIES.add(proxy)
                newly_disabled.append((proxy, detail))

    if newly_disabled:
        lines = ["Proxy alert: disabled failed proxies", ""]
        for proxy, detail in newly_disabled:
            lines.append(f"- {mask_proxy(proxy)} - {detail}")
        lines.append("")
        lines.append(f"Active: {len(active_proxies())}/{len(PROXIES)}")
        await alert_admins("\n".join(lines)[:3900])

    if recovered:
        lines = ["Proxy alert: recovered proxies", ""]
        for proxy in recovered:
            lines.append(f"- {mask_proxy(proxy)}")
        lines.append("")
        lines.append(f"Active: {len(active_proxies())}/{len(PROXIES)}")
        await alert_admins("\n".join(lines)[:3900])

    lines = [proxy_status_text("Proxy check"), ""]
    for i, (proxy, result) in enumerate(zip(PROXIES, results), start=1):
        ok, detail, elapsed = result
        status = "OK" if ok else "DISABLED"
        timing = f" {elapsed:.1f}s" if elapsed is not None else ""
        lines.append(f"{i}. {status} - {mask_proxy(proxy)} - {detail}{timing}")
    return "\n".join(lines)[:3900]


def bot_help_text():
    return (
        "Video Downloader\n\n"
        "Send a TikTok, Instagram, YouTube, or X/Twitter link and I will download the media.\n\n"
        "Commands:\n"
        "/start - open bot panel\n"
        "/help - show commands\n"
        "/status - show bot status\n"
        "/proxies - check configured proxies\n\n"
        "Instagram photo posts/carousels send all images, audio when available, and description."
    )


def bot_status_text():
    return (
        "Bot status\n\n"
        f"Busy requests: {len(busy)}\n"
        f"Download slots: {MAX_PARALLEL_DOWNLOADS}\n"
        f"Upload slots: {MAX_PARALLEL_UPLOADS}\n"
        f"FFmpeg slots: {MAX_PARALLEL_FFMPEG}\n"
        f"Links per message: {MAX_LINKS_PER_MESSAGE}\n"
        f"HTTP connections: {HTTP_CONNECTION_LIMIT}\n"
        f"Telegram API: {BOT_API_BASE}\n"
        f"Telegram local mode: {TELEGRAM_LOCAL_MODE}\n"
        f"Proxies configured: {len(PROXIES)}\n"
        f"Proxies active: {len(active_proxies())}\n"
        f"Proxies disabled: {len(DISABLED_PROXIES)}\n"
        f"Cookies loaded: {', '.join(sorted(COOKIES)) if COOKIES else 'none'}"
    )


def normalize_url(platform, url):
    url = url.rstrip('.,;!?) ]')
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path)

    if platform == "twitter":
        m = re.search(r"/(\w+)/status/(\d+)", path)
        if m:
            return f"https://x.com/{m.group(1)}/status/{m.group(2)}"
        m = re.search(r"/i/status/(\d+)", path)
        if m:
            return f"https://x.com/i/status/{m.group(1)}"

    if platform == "instagram":
        keep = {}
        params = parse_qs(parsed.query)
        if "img_index" in params:
            keep["img_index"] = params["img_index"][0]
        return urlunparse((parsed.scheme or "https", host or "www.instagram.com", path.rstrip("/") + "/", "", urlencode(keep), ""))

    if platform == "youtube":
        params = parse_qs(parsed.query)
        keep = {}
        if "v" in params:
            keep["v"] = params["v"][0]
        if "t" in params:
            keep["t"] = params["t"][0]
        if "start" in params and "t" not in keep:
            keep["t"] = params["start"][0]
        query = urlencode(keep)
        return urlunparse((parsed.scheme or "https", host, path, "", query, ""))

    if platform == "tiktok":
        return urlunparse((parsed.scheme or "https", host, path.rstrip("/"), "", "", ""))

    return url


def find_urls(text):
    matches = []
    seen = set()
    for platform, pats in PATTERNS.items():
        for pat in pats:
            for m in re.finditer(pat, text, re.IGNORECASE):
                url = normalize_url(platform, m.group(0))
                if url in seen:
                    continue
                seen.add(url)
                matches.append((m.start(), platform, url))
    matches.sort(key=lambda item: item[0])
    return [(platform, url) for _, platform, url in matches]

def _parse_timecode_to_seconds(value):
    parts = value.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + int(ss)
    if len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    return None


def _parse_yt_t_param(raw):
    if raw.isdigit():
        return int(raw)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw.strip().lower())
    if not m:
        return None
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    sec = int(m.group(3) or 0)
    total = h * 3600 + mins * 60 + sec
    return total if total > 0 else None


def extract_clip_request(text, url):
    # Supports:
    # - range: "13:20 14:12"
    # - single point: "13:15"
    # - optional duration: "5s/5 sec/5 seconds"
    start = None
    end = None
    duration = None

    timecode_matches = re.findall(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", text)
    parsed_timecodes = [t for t in (_parse_timecode_to_seconds(v) for v in timecode_matches) if t is not None]

    if len(parsed_timecodes) >= 2:
        start = parsed_timecodes[0]
        end = parsed_timecodes[1]
        if end > start:
            duration = end - start
        else:
            return None
    elif len(parsed_timecodes) == 1:
        start = parsed_timecodes[0]

    dur_match = re.search(r"\b(\d{1,4})\s*(?:s|sec|secs|second|seconds)\b", text, re.IGNORECASE)
    if dur_match:
        duration = int(dur_match.group(1))

    if start is None:
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            t_raw = (params.get("t") or [None])[0]
            if t_raw:
                start = _parse_yt_t_param(t_raw)
        except Exception:
            start = None

    if start is None:
        return None

    if duration is None:
        duration = 5

    # Keep sane limits
    duration = max(1, min(duration, 900))
    clip = {"start": start, "duration": duration}
    if end is not None:
        clip["end"] = end
    return clip


# ── telegram API ──

async def tg(method, data, timeout=120):
    s = await get_session()
    last_error = None
    for attempt in range(3):
        try:
            async with s.post(f"{API}/{method}", data=data, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    res = await r.json()
                except Exception:
                    body = await r.text()
                    res = {"ok": False, "description": body[:300], "error_code": r.status}

                if res.get("ok"):
                    return res

                error_code = res.get("error_code") or r.status
                description = res.get("description", method)
                retry_after = (res.get("parameters") or {}).get("retry_after")
                can_retry_body = not getattr(data, "_is_multipart", False)
                if can_retry_body and error_code == 429 and retry_after and attempt < 2:
                    wait = int(retry_after) + 1
                    log.warning("[telegram] %s flood-wait %ss", method, wait)
                    await asyncio.sleep(wait)
                    continue
                if can_retry_body and error_code >= 500 and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise Exception(description)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = e
            can_retry_body = not getattr(data, "_is_multipart", False)
            if can_retry_body and attempt < 2:
                log.warning("[telegram] %s retry %d: %s", method, attempt + 1, e)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise Exception(str(last_error) if last_error else method)


async def send_text(chat, text, reply=None, reply_markup=None):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("text", text)
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    if reply_markup:
        d.add_field("reply_markup", json.dumps(reply_markup))
    return await tg("sendMessage", d, 30)


async def alert_admins(text):
    if not ADMIN_CHAT_IDS:
        return
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await send_text(chat_id, text)
        except Exception as e:
            log.warning("[admin-alert] %s", e)


async def edit_text(chat, mid, text, reply_markup=None):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("message_id", str(mid))
    d.add_field("text", text)
    if reply_markup:
        d.add_field("reply_markup", json.dumps(reply_markup))
    return await tg("editMessageText", d, 30)


async def answer_callback(callback_id, text=""):
    d = aiohttp.FormData()
    d.add_field("callback_query_id", str(callback_id))
    if text:
        d.add_field("text", text[:200])
    return await tg("answerCallbackQuery", d, 10)


async def delete_msg(chat, mid):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("message_id", str(mid))
    try:
        await tg("deleteMessage", d, 10)
    except Exception:
        pass


def telegram_local_file_uri(path):
    return Path(path).resolve().as_uri()


def add_upload_field(form, field_name, path, filename, content_type, opened_files):
    if TELEGRAM_LOCAL_MODE:
        form.add_field(field_name, telegram_local_file_uri(path))
        return
    f = open(path, "rb")
    opened_files.append(f)
    form.add_field(field_name, f, filename=filename, content_type=content_type)


async def send_video(chat, path, caption, reply, dur=0, w=0, h=0):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("caption", caption[:1024])
    d.add_field("supports_streaming", "true")
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    if dur:
        d.add_field("duration", str(dur))
    if w:
        d.add_field("width", str(w))
    if h:
        d.add_field("height", str(h))
    opened = []
    try:
        add_upload_field(d, "video", path, "video.mp4", "video/mp4", opened)
        return await tg("sendVideo", d, 180)
    except Exception:
        log.exception("[upload] sendVideo failed for %s", path)
        raise
    finally:
        for f in opened:
            f.close()


async def send_audio(chat, path, caption, reply):
    ext = Path(path).suffix.lower() or ".ogg"
    content_types = {
        ".mp3": "audio/mpeg", ".mp4": "audio/mp4", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm",
    }
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("caption", caption[:1024])
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    opened = []
    try:
        add_upload_field(d, "audio", path, f"audio{ext}", content_types.get(ext, "audio/octet-stream"), opened)
        return await tg("sendAudio", d, 60)
    except Exception:
        log.exception("[upload] sendAudio failed for %s", path)
        raise
    finally:
        for f in opened:
            f.close()


async def send_photo(chat, path, caption, reply):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    if caption:
        d.add_field("caption", caption[:1024])
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    opened = []
    try:
        add_upload_field(d, "photo", path, "photo.jpg", "image/jpeg", opened)
        return await tg("sendPhoto", d, 60)
    except Exception:
        log.exception("[upload] sendPhoto failed for %s", path)
        raise
    finally:
        for f in opened:
            f.close()


def _media_entry(item):
    if isinstance(item, dict):
        return item
    return {"type": "photo", "path": item}


async def send_media_group(chat, media_items, caption, reply):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    media = []
    opened = []
    try:
        for i, raw in enumerate(media_items[:10]):
            item = _media_entry(raw)
            media_type = item.get("type", "photo")
            path = item["path"]
            key = f"{media_type}{i}"
            if media_type == "video":
                if TELEGRAM_LOCAL_MODE:
                    media_ref = telegram_local_file_uri(path)
                else:
                    add_upload_field(d, key, path, f"{key}.mp4", "video/mp4", opened)
                    media_ref = f"attach://{key}"
                entry = {"type": "video", "media": media_ref, "supports_streaming": True}
            else:
                if TELEGRAM_LOCAL_MODE:
                    media_ref = telegram_local_file_uri(path)
                else:
                    add_upload_field(d, key, path, f"{key}.jpg", "image/jpeg", opened)
                    media_ref = f"attach://{key}"
                entry = {"type": "photo", "media": media_ref}
            if i == 0 and caption:
                entry["caption"] = caption[:1024]
            media.append(entry)
        d.add_field("media", json.dumps(media))
        return await tg("sendMediaGroup", d, 120)
    except Exception:
        log.exception("[upload] sendMediaGroup failed for %s", [(_media_entry(i).get("path")) for i in media_items[:10]])
        raise
    finally:
        for f in opened:
            try:
                f.close()
            except Exception:
                pass


# ── fast paths: direct API downloads ──

def _twitter_video_url(video):
    candidates = []
    for key in ("formats", "variants"):
        for item in video.get(key) or []:
            media_url = item.get("url", "")
            content_type = (item.get("content_type") or "").lower()
            container = (item.get("container") or "").lower()
            if not media_url:
                continue
            if ".mp4" not in media_url and content_type != "video/mp4" and container != "mp4":
                continue

            width = item.get("width") or 0
            height = item.get("height") or 0
            if not width or not height:
                m = re.search(r"/(\d+)x(\d+)/", media_url)
                if m:
                    width, height = int(m.group(1)), int(m.group(2))

            candidates.append({
                "url": media_url,
                "bitrate": item.get("bitrate") or 0,
                "width": width,
                "height": height,
            })

    if not candidates:
        return video.get("url", ""), video.get("width", 0), video.get("height", 0), 0

    preferred = [
        c for c in candidates
        if c["height"] and c["height"] <= 540 and (not c["bitrate"] or c["bitrate"] <= 1200000)
    ]
    playable = [c for c in candidates if not c["height"] or c["height"] <= 720]
    chosen = max(preferred or playable or candidates, key=lambda c: (c["height"], c["bitrate"]))
    return chosen["url"], chosen["width"], chosen["height"], chosen["bitrate"]


async def _twitter_fast(url):
    """Download Twitter/X via fxtwitter API — instant, no yt-dlp."""
    m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url)
    if not m:
        return None, None
    username, tweet_id = m.group(1), m.group(2)
    s = await get_session()
    proxy = current_http_proxy()
    try:
        async with s.get(f"https://api.fxtwitter.com/{username}/status/{tweet_id}",
                         headers={"User-Agent": "AlvidasBot/1.0"},
                         proxy=proxy,
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
            tweet = data.get("tweet", {})
            if not tweet:
                return None, None
            title = (tweet.get("text") or "Tweet")[:100]
            media = tweet.get("media", {})
            videos = media.get("videos") or []
            photos = media.get("photos") or []

            job = os.urandom(5).hex()
            out_dir = DL_DIR / job
            out_dir.mkdir(exist_ok=True)

            if videos:
                video_url, chosen_w, chosen_h, chosen_bitrate = _twitter_video_url(videos[0])
                if not video_url:
                    return None, None
                path = str(out_dir / f"{job}.mp4")
                async with s.get(video_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=60)) as vr:
                    if vr.status != 200:
                        return None, None
                    with open(path, "wb") as f:
                        async for chunk in vr.content.iter_chunked(65536):
                            f.write(chunk)
                size = os.path.getsize(path)
                if size < 1000:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, None
                dur, w, h = await _ffprobe(path)
                w = w or chosen_w
                h = h or chosen_h
                log.info("[twitter/fxtwitter] OK: %s (%d bytes, %sp, %s bps)", title, size, h or "?", chosen_bitrate or "?")
                return {"type": "video", "path": path, "title": title,
                        "duration": dur, "width": w, "height": h,
                        "size": size, "dir": str(out_dir)}, None

            if photos:
                photo_paths = []
                for i, p in enumerate(photos[:10]):
                    img_url = p.get("url", "")
                    if not img_url:
                        continue
                    try:
                        async with s.get(img_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)) as pr:
                            if pr.status != 200:
                                continue
                            content = await pr.read()
                            pp = out_dir / f"photo_{i}.jpg"
                            pp.write_bytes(content)
                            photo_paths.append(str(pp))
                    except Exception:
                        continue
                if photo_paths:
                    log.info("[twitter/fxtwitter] %d photos", len(photo_paths))
                    return {"type": "photos", "title": title,
                            "description": title, "photo_paths": photo_paths,
                            "audio_path": None, "dir": str(out_dir)}, None

            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None
    except Exception as e:
        log.warning("[twitter/fxtwitter] %s", e)
        return None, None


async def _tiktok_fast(url):
    """Download TikTok via tikwm API — much faster than yt-dlp."""
    s = await get_session()
    proxy = current_http_proxy()
    try:
        async with s.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1},
                         headers={"User-Agent": "Mozilla/5.0"},
                         proxy=proxy,
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
            if data.get("code") != 0:
                return None, None
            vdata = data.get("data", {})
            title = (vdata.get("title") or "TikTok")[:100]

            job = os.urandom(5).hex()
            out_dir = DL_DIR / job
            out_dir.mkdir(exist_ok=True)

            # photo slideshow
            images = vdata.get("images")
            if images:
                photo_paths = []
                for i, img_url in enumerate(images):
                    try:
                        async with s.get(img_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)) as ir:
                            if ir.status != 200:
                                continue
                            pp = out_dir / f"photo_{i}.jpg"
                            pp.write_bytes(await ir.read())
                            photo_paths.append(str(pp))
                    except Exception:
                        continue
                if photo_paths:
                    audio_path = None
                    music_info = vdata.get("music_info") or {}
                    audio_url = vdata.get("music") or music_info.get("play")
                    if audio_url:
                        try:
                            ap = out_dir / "audio.mp3"
                            async with s.get(audio_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=30)) as ar:
                                if ar.status == 200:
                                    ap.write_bytes(await ar.read())
                                    if ap.stat().st_size > 100:
                                        audio_path = str(ap)
                        except Exception:
                            pass
                    log.info("[tiktok/tikwm] %d photos", len(photo_paths))
                    return {"type": "photos", "title": title,
                            "description": title, "photo_paths": photo_paths,
                            "audio_path": audio_path, "dir": str(out_dir)}, None

            # video — prefer safe codecs
            SAFE_CODECS = {"h264", "avc1", "avc", "vp9", "vp8"}
            candidate_urls = [u for u in [
                vdata.get("play2"),
                vdata.get("play"),
                vdata.get("wmplay"),
                vdata.get("hdplay"),
            ] if u]

            if not candidate_urls:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, None

            path = None
            for video_url in candidate_urls:
                candidate_path = str(out_dir / f"{job}_candidate.mp4")
                try:
                    async with s.get(video_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=60)) as vr:
                        if vr.status != 200:
                            continue
                        with open(candidate_path, "wb") as f:
                            async for chunk in vr.content.iter_chunked(65536):
                                f.write(chunk)
                except Exception as e:
                    log.warning("[tiktok/tikwm] URL fetch failed: %s", e)
                    continue

                if os.path.getsize(candidate_path) < 1000:
                    os.remove(candidate_path)
                    continue

                codec = await _get_video_codec(candidate_path)
                if codec and codec not in SAFE_CODECS:
                    log.info("[tiktok/tikwm] skipping URL with codec %s, trying next", codec)
                    os.remove(candidate_path)
                    continue

                final_path = str(out_dir / f"{job}.mp4")
                os.rename(candidate_path, final_path)
                path = final_path
                log.info("[tiktok/tikwm] accepted URL with codec=%s", codec)
                break

            if not path:
                video_url = vdata.get("hdplay") or vdata.get("play")
                if not video_url:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, None
                fallback_path = str(out_dir / f"{job}_raw.mp4")
                try:
                    async with s.get(video_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=60)) as vr:
                        if vr.status != 200:
                            shutil.rmtree(out_dir, ignore_errors=True)
                            return None, None
                        with open(fallback_path, "wb") as f:
                            async for chunk in vr.content.iter_chunked(65536):
                                f.write(chunk)
                except Exception as e:
                    log.error("[tiktok/tikwm] fallback fetch failed: %s", e)
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, None

                log.info("[tiktok/tikwm] no safe URL found, re-encoding fallback")
                reencoded = str(out_dir / f"{job}.mp4")
                ok = await _reencode_h264(fallback_path, reencoded)
                try:
                    os.remove(fallback_path)
                except Exception:
                    pass
                if not ok:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, None
                path = reencoded

            size = os.path.getsize(path)
            dur, w, h = await _ffprobe(path)
            log.info("[tiktok/tikwm] OK: %s (%d bytes)", title, size)
            return {"type": "video", "path": path, "title": title,
                    "duration": dur, "width": w, "height": h,
                    "size": size, "dir": str(out_dir)}, None

    except Exception as e:
        log.warning("[tiktok/tikwm] %s", e)
        return None, None


async def _get_video_codec(path):
    """Get video codec name via ffprobe."""
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        data = json.loads(out)
        for s in data.get("streams", []):
            return s.get("codec_name", "").lower()
    except Exception:
        pass
    return None


async def _reencode_h264(input_path, output_path):
    """Re-encode video to H.264 for Telegram compatibility."""
    try:
        cmd = ["ffmpeg", "-y", "-i", input_path,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", "-pix_fmt", "yuv420p",
               output_path]
        async with FFMPEG_SEMAPHORE:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(p.communicate(), 120)
        if p.returncode != 0:
            log.error("[reencode] ffmpeg error: %s", stderr.decode()[:300] if stderr else "")
            return False
        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except Exception as e:
        log.error("[reencode] %s", e)
        return False


async def _ffprobe(path):
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        data = json.loads(out)
        dur = int(float(data.get("format", {}).get("duration", 0)))
        w = h = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width", 0), s.get("height", 0)
                break
        return dur, w, h
    except Exception:
        return 0, 0, 0


async def cut_video_clip(input_path, output_path, start_sec, duration_sec):
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-t", str(duration_sec),
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        async with FFMPEG_SEMAPHORE:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(p.communicate(), 180)
        if p.returncode != 0:
            log.error("[clip] ffmpeg failed: %s", stderr.decode()[:300] if stderr else "")
            return False
        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except Exception as e:
        log.error("[clip] %s", e)
        return False


# FIX: helper to confirm a file actually contains a video stream
async def has_video_stream(path):
    """Return True if the file contains at least one video stream."""
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=codec_type", "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        data = json.loads(out)
        return len(data.get("streams", [])) > 0
    except Exception:
        return True  # assume video on error, preserve old behaviour


# ── download with yt-dlp as Python library ──

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mkv"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".webm", ".mp4"}

_INSTAGRAM_SHORTCODE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _instagram_shortcode_to_pk(shortcode):
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    value = 0
    for char in shortcode:
        value = value * 64 + _INSTAGRAM_SHORTCODE_CHARS.index(char)
    return str(value)


def _instagram_shortcode_from_url(url):
    m = re.search(r"instagram\.com/(?:[^/?#]+/)?(?:p|tv|reels?)/([^/?#&]+)", url, re.IGNORECASE)
    return m.group(1) if m else None


def _cookie_header(platform):
    cookiefile = COOKIES.get(platform)
    if not cookiefile:
        return ""
    pairs = []
    try:
        for line in Path(cookiefile).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs.append(f"{parts[5]}={parts[6]}")
    except Exception as e:
        log.warning("[%s] cookie read failed: %s", platform, e)
    return "; ".join(pairs)


def _pick_instagram_image_url(media):
    candidates = media.get("image_versions2", {}).get("candidates") or []
    if not candidates:
        candidates = [{"url": media.get("display_url") or media.get("thumbnail_url")}]
    candidates = [c for c in candidates if c.get("url")]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    return best.get("url")


def _pick_instagram_video_url(media):
    candidates = media.get("video_versions") or []
    candidates = [c for c in candidates if c.get("url")]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    return best.get("url")


def _instagram_description(item):
    caption = item.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text")
        if text:
            return text.strip()
    return ""


def _instagram_audio_url(obj, path=()):
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered_path = tuple(str(p).lower() for p in (*path, key))
            if key == "progressive_download_url" and any(
                token in lowered_path for token in ("music_info", "music_asset_info", "original_sound_info", "additional_audio_info")
            ):
                return value
            found = _instagram_audio_url(value, (*path, key))
            if found:
                return found
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            found = _instagram_audio_url(value, (*path, str(idx)))
            if found:
                return found
    return None


def _ext_from_url(url, default_ext):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext and len(ext) <= 6:
        return ext
    return default_ext


async def _download_url(url, path, headers=None, timeout=60):
    s = await get_session()
    async with s.get(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            return False
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(65536):
                f.write(chunk)
    return Path(path).exists() and Path(path).stat().st_size > 100


async def has_audio_stream(path):
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type", "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        data = json.loads(out)
        return len(data.get("streams", [])) > 0
    except Exception:
        return False


async def _extract_audio(input_path, output_path):
    try:
        async with FFMPEG_SEMAPHORE:
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", input_path, "-vn",
                "-c:a", "aac", "-b:a", "128k", output_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(p.communicate(), 120)
        if p.returncode != 0:
            log.warning("[audio] extract failed: %s", stderr.decode(errors="ignore")[:300] if stderr else "")
            return False
        return Path(output_path).exists() and Path(output_path).stat().st_size > 100
    except Exception as e:
        log.warning("[audio] %s", e)
        return False


def _description_from_info(info):
    if not isinstance(info, dict):
        return ""
    description = info.get("description") or ""
    if description:
        return description.strip()
    for entry in info.get("entries") or []:
        description = _description_from_info(entry)
        if description:
            return description
    return ""


async def _instagram_fast(url):
    shortcode = _instagram_shortcode_from_url(url)
    cookie = _cookie_header("instagram")
    if not shortcode or not cookie:
        return None, None

    job = os.urandom(5).hex()
    out_dir = DL_DIR / job
    out_dir.mkdir(exist_ok=True)

    headers = {
        "User-Agent": "Instagram 219.0.0.12.117 Android",
        "Accept": "*/*",
        "X-IG-App-ID": "936619743392459",
        "X-ASBD-ID": "198387",
        "Origin": "https://www.instagram.com",
        "Referer": url,
        "Cookie": cookie,
    }

    try:
        s = await get_session()
        pk = _instagram_shortcode_to_pk(shortcode)
        async with s.get(
            f"https://i.instagram.com/api/v1/media/{pk}/info/",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, None
            data = await r.json()

        item = (data.get("items") or [None])[0]
        if not item:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None

        user = item.get("user") or {}
        username = user.get("username") or "instagram"
        title = (item.get("title") or f"Instagram by {username}")[:100]
        description = _instagram_description(item)

        media_items = item.get("carousel_media") or [item]
        media_paths = []
        photo_paths = []
        video_paths = []

        for i, media in enumerate(media_items):
            media_type = media.get("media_type")
            if media_type == 2:
                video_url = _pick_instagram_video_url(media)
                if not video_url:
                    continue
                video_path = out_dir / f"video_{i}{_ext_from_url(video_url, '.mp4')}"
                if await _download_url(video_url, str(video_path), headers=headers):
                    path = str(video_path)
                    video_paths.append(path)
                    dur, w, h = await _ffprobe(path)
                    media_paths.append({"type": "video", "path": path, "duration": dur, "width": w, "height": h})
                continue

            image_url = _pick_instagram_image_url(media)
            if not image_url:
                continue
            image_path = out_dir / f"photo_{i}{_ext_from_url(image_url, '.jpg')}"
            if await _download_url(image_url, str(image_path), headers=headers, timeout=20):
                path = str(image_path)
                photo_paths.append(path)
                media_paths.append({"type": "photo", "path": path})

        audio_path = None
        audio_url = _instagram_audio_url(item)
        if audio_url:
            candidate = out_dir / f"audio{_ext_from_url(audio_url, '.m4a')}"
            if await _download_url(audio_url, str(candidate), headers=headers, timeout=60):
                audio_path = str(candidate)

        if not audio_path and photo_paths and video_paths:
            for video_path in video_paths:
                if await has_audio_stream(video_path):
                    candidate = str(out_dir / "audio.m4a")
                    if await _extract_audio(video_path, candidate):
                        audio_path = candidate
                    break

        if photo_paths and video_paths:
            log.info("[instagram/api] %d mixed items, audio=%s", len(media_paths), bool(audio_path))
            return {
                "type": "media",
                "title": title,
                "description": description,
                "media": media_paths,
                "audio_path": audio_path,
                "dir": str(out_dir),
            }, None

        if photo_paths:
            log.info("[instagram/api] %d photos, audio=%s", len(photo_paths), bool(audio_path))
            return {
                "type": "photos",
                "title": title,
                "description": description,
                "photo_paths": photo_paths,
                "audio_path": audio_path,
                "dir": str(out_dir),
            }, None

        if video_paths:
            path = video_paths[0]
            dur, w, h = await _ffprobe(path)
            return {
                "type": "video",
                "path": path,
                "title": title,
                "description": description,
                "duration": dur,
                "width": w,
                "height": h,
                "size": Path(path).stat().st_size,
                "dir": str(out_dir),
            }, None

        shutil.rmtree(out_dir, ignore_errors=True)
        return None, None

    except Exception as e:
        log.warning("[instagram/api] %s", e)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, None


MAX_TELEGRAM_CAPTION = 1024
MAX_TELEGRAM_TEXT = 4096


def make_caption(emoji, title, description="", extra=""):
    lines = [f"{emoji} {title}".strip()]
    if extra:
        lines.append(extra.strip())

    description = (description or "").strip()
    if description and description not in lines[0]:
        candidate = "\n\n".join([*lines, description])
        if len(candidate) <= MAX_TELEGRAM_CAPTION:
            lines.append(description)

    return "\n\n".join([line for line in lines if line])[:MAX_TELEGRAM_CAPTION]


async def send_description_if_needed(chat, emoji, result, caption, reply):
    description = (result.get("description") or "").strip()
    if not description:
        return

    # If the full description fit into the media caption, do not send it again.
    # Long descriptions are intentionally omitted from captions and sent once as text.
    if description in caption:
        return

    await send_text(chat, f"{emoji} Description:\n{description[:MAX_TELEGRAM_TEXT - 32]}", reply)


def _result_files(result):
    if not result:
        return []
    files = []
    if result.get("path"):
        files.append(result["path"])
    files.extend(result.get("photo_paths") or [])
    files.extend(item.get("path") for item in result.get("media") or [] if item.get("path"))
    if result.get("audio_path"):
        files.append(result["audio_path"])
    return files


def validate_result_size(result):
    for file_path in _result_files(result):
        try:
            size = Path(file_path).stat().st_size
        except FileNotFoundError:
            return f"Downloaded file missing: {Path(file_path).name}"
        if size > MAX_TG:
            return f"Too large for Telegram bot upload ({size // 1048576} MB, limit 50 MB)"
    return None


async def _validated(result, err):
    if result:
        size_err = validate_result_size(result)
        if size_err:
            shutil.rmtree(result.get("dir", ""), ignore_errors=True)
            return None, size_err
    return result, err


async def download_media(url, platform):
    if platform == "instagram":
        result, err = await _instagram_fast(url)
        if result:
            return await _validated(result, None)

    if platform == "twitter":
        result, err = await _twitter_fast(url)
        if result:
            return await _validated(result, None)

    if platform == "tiktok":
        result, err = await _tiktok_fast(url)
        if result:
            return await _validated(result, None)

    result, err = await _ytdlp_download(url, platform)
    if result:
        return await _validated(result, None)

    if platform == "youtube":
        piped_result, piped_err = await piped_download(url)
        if piped_result:
            return await _validated(piped_result, None)
        return None, err or piped_err

    return None, err


async def _ytdlp_download(url, platform):
    job = os.urandom(5).hex()
    out_dir = DL_DIR / job
    out_dir.mkdir(exist_ok=True)
    tmpl = str(out_dir / f"{job}.%(ext)s")

    opts = {
        "outtmpl":            tmpl,
        "noplaylist":         True,
        "no_warnings":        True,
        "format":             "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "max_filesize":       MAX_TG,
        "socket_timeout":     30,
        "quiet":              True,
    }

    if platform == "instagram":
        opts["noplaylist"] = False
        opts["outtmpl"] = str(out_dir / f"{job}_%(playlist_index|0)s_%(id)s.%(ext)s")

    if shutil.which("bun"):
        opts["js_runtimes"]      = {"bun": {"path": "bun"}}
        opts["remote_components"] = {"ejs:github"}

    cookie = COOKIES.get(platform)
    if cookie:
        opts["cookiefile"] = cookie

    proxy = current_proxy() if platform not in ("instagram", "youtube") else None
    if proxy:
        opts["proxy"] = proxy

    if platform == "twitter":
        opts["extractor_args"] = {"twitter": ["api=syndication"]}

    if platform == "tiktok":
        opts["format"] = "bestvideo[vcodec^=avc1]+bestaudio/bestvideo[vcodec^=avc1]/best[vcodec^=avc1]/best"

    # FIX: prefer combined progressive streams first to avoid audio-only DASH results
    if platform == "youtube":
        opts["format"] = (
            "best[ext=mp4][vcodec!=none][acodec!=none][height<=720]/"
            "best[vcodec!=none][acodec!=none][height<=720]/"
            "bv*[height<=720]+ba/b"
        )

    loop = asyncio.get_event_loop()
    try:
        log.info("[%s] downloading %s", platform, url)
        info = await loop.run_in_executor(None, lambda: _ytdlp_extract(url, opts))
        if info is None:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Download failed"

        downloads = info.get("requested_downloads") or []
        all_files  = list(out_dir.iterdir())

        images = [f for f in all_files if f.suffix.lower() in IMAGE_EXT]
        videos = [f for f in all_files if f.suffix.lower() in VIDEO_EXT]
        audios = [f for f in all_files if f.suffix.lower() in AUDIO_EXT and f not in videos]

        title = info.get("title", "Media")[:100]
        description = _description_from_info(info)

        if images and not videos:
            return {
                "type":        "photos",
                "title":       title,
                "description": description,
                "photo_paths": sorted([str(p) for p in images]),
                "audio_path":  str(audios[0]) if audios else None,
                "dir":         str(out_dir),
            }, None

        # ── primary path: use requested_downloads ──
        if downloads:
            filepath = downloads[0].get("filepath")
            if filepath and Path(filepath).exists():
                # TikTok codec safety check
                if platform == "tiktok":
                    codec = await _get_video_codec(filepath)
                    SAFE_CODECS = {"h264", "avc1", "avc", "vp9", "vp8"}
                    if codec and codec not in SAFE_CODECS:
                        log.info("[tiktok/ytdlp] codec %s - re-encoding to h264", codec)
                        reencoded = str(out_dir / "reencoded.mp4")
                        ok = await _reencode_h264(filepath, reencoded)
                        if ok:
                            try:
                                os.remove(filepath)
                            except Exception:
                                pass
                            filepath = reencoded
                        else:
                            shutil.rmtree(out_dir, ignore_errors=True)
                            return None, "Re-encode failed"

                # FIX: verify the file actually has a video stream before treating as video
                if not await has_video_stream(filepath):
                    log.warning("[%s] file has no video stream — sending as audio: %s", platform, filepath)
                    return {
                        "type":  "audio",
                        "path":  filepath,
                        "title": title,
                        "description": description,
                        "size":  Path(filepath).stat().st_size,
                        "dir":   str(out_dir),
                    }, None

                size = Path(filepath).stat().st_size
                return {
                    "type":     "video",
                    "path":     filepath,
                    "title":    title,
                    "description": description,
                    "duration": info.get("duration") or 0,
                    "width":    downloads[0].get("width") or info.get("width") or 0,
                    "height":   downloads[0].get("height") or info.get("height") or 0,
                    "size":     size,
                    "dir":      str(out_dir),
                }, None

        # ── fallback: pick any video-extension file ──
        if videos:
            v = videos[0]
            if platform == "tiktok":
                codec = await _get_video_codec(str(v))
                SAFE_CODECS = {"h264", "avc1", "avc", "vp9", "vp8"}
                if codec and codec not in SAFE_CODECS:
                    log.info("[tiktok/ytdlp-fallback] codec %s - re-encoding to h264", codec)
                    reencoded = str(out_dir / "reencoded.mp4")
                    ok = await _reencode_h264(str(v), reencoded)
                    if ok:
                        try:
                            os.remove(str(v))
                        except Exception:
                            pass
                        v = Path(reencoded)
                    else:
                        shutil.rmtree(out_dir, ignore_errors=True)
                        return None, "Re-encode failed"

            # FIX: verify video stream before sending as video
            if not await has_video_stream(str(v)):
                log.warning("[%s] fallback file has no video stream — sending as audio: %s", platform, v)
                return {
                    "type":  "audio",
                    "path":  str(v),
                    "title": title,
                    "description": description,
                    "size":  v.stat().st_size,
                    "dir":   str(out_dir),
                }, None

            return {
                "type":     "video",
                "path":     str(v),
                "title":    title,
                "description": description,
                "duration": info.get("duration") or 0,
                "width":    info.get("width") or 0,
                "height":   info.get("height") or 0,
                "size":     v.stat().st_size,
                "dir":      str(out_dir),
            }, None

        shutil.rmtree(out_dir, ignore_errors=True)
        return None, "No media found after download"

    except Exception as e:
        err = str(e)
        log.error("[%s] error: %s", platform, err[:500])
        shutil.rmtree(out_dir, ignore_errors=True)
        lower = err.lower()
        if "sign in" in lower and "youtube" in lower:
            return None, "YouTube is blocking downloads from this server"
        if "login required" in lower or "rate-limit" in lower:
            return None, "Login required or rate limited"
        if "not available" in lower:
            return None, "Content not available"
        return None, err.split("\n")[-1][:200]


def _ytdlp_extract(url, opts):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    except Exception as e:
        raise e


# ── Piped fallback for YouTube ──

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi-libre.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.yt",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.drgns.space",
    "https://pipedapi.ducks.party",
    "https://pipedapi.reallyaweso.me",
]


def _extract_video_id(url):
    m = re.search(r"(?:v=|shorts/|embed/|live/|youtu\.be/)([\w-]{11})", url)
    return m.group(1) if m else None


async def _download_stream_to_file(url, path, timeout=120):
    s = await get_session()
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            return f"HTTP {r.status}"
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(65536):
                f.write(chunk)
    return None


def _pick_piped_progressive(streams):
    best = None
    for stream in streams:
        if stream.get("videoOnly", True) or not stream.get("url"):
            continue
        h = stream.get("height") or 0
        if h and h > 720:
            continue
        if best is None or h > (best.get("height") or 0):
            best = stream
    return best


def _pick_piped_video_only(streams):
    best = None
    for stream in streams:
        if not stream.get("videoOnly") or not stream.get("url"):
            continue
        h = stream.get("height") or 0
        if h and h > 720:
            continue
        if best is None or h > (best.get("height") or 0):
            best = stream
    return best


def _pick_piped_audio(streams):
    best = None
    for stream in streams:
        if not stream.get("url"):
            continue
        bitrate = stream.get("bitrate") or stream.get("quality") or 0
        if best is None or bitrate > (best.get("bitrate") or best.get("quality") or 0):
            best = stream
    return best


async def _ffmpeg_copy(input_args, output_path, timeout=240):
    cmd = ["ffmpeg", "-y", *input_args, "-c", "copy", "-movflags", "+faststart", output_path]
    async with FFMPEG_SEMAPHORE:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(p.communicate(), timeout)
    if p.returncode != 0:
        return stderr.decode(errors="ignore")[:300] if stderr else "ffmpeg failed"
    return None


async def piped_download(url):
    vid = _extract_video_id(url)
    if not vid:
        return None, "Could not extract video ID"

    job = os.urandom(5).hex()
    out_dir = DL_DIR / job
    out_dir.mkdir(exist_ok=True)
    path = str(out_dir / f"{job}.mp4")
    s = await get_session()

    try:
        stream_data = None
        for instance in PIPED_INSTANCES:
            try:
                async with s.get(f"{instance}/streams/{vid}",
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        continue
                    stream_data = await r.json()
                    if stream_data.get("videoStreams") or stream_data.get("hls"):
                        log.info("[piped] using %s", instance)
                        break
                    stream_data = None
            except Exception as e:
                log.warning("[piped] %s failed: %s", instance, e)
                continue

        if not stream_data:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "All Piped instances failed"

        title = stream_data.get("title", "Video")[:100]
        duration = stream_data.get("duration", 0)
        video_streams = stream_data.get("videoStreams") or []
        audio_streams = stream_data.get("audioStreams") or []

        progressive = _pick_piped_progressive(video_streams)
        best_w = best_h = 0
        if progressive:
            err = await _download_stream_to_file(progressive["url"], path)
            if err:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, f"Piped stream {err}"
            best_w = progressive.get("width") or 0
            best_h = progressive.get("height") or 0
        else:
            video_only = _pick_piped_video_only(video_streams)
            audio = _pick_piped_audio(audio_streams)
            if video_only and audio:
                video_path = str(out_dir / "video_only.mp4")
                audio_path = str(out_dir / "audio_only.m4a")
                err = await _download_stream_to_file(video_only["url"], video_path)
                if err:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, f"Piped video stream {err}"
                err = await _download_stream_to_file(audio["url"], audio_path)
                if err:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, f"Piped audio stream {err}"
                err = await _ffmpeg_copy(["-i", video_path, "-i", audio_path], path)
                if err:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, f"Piped merge failed: {err}"
                best_w = video_only.get("width") or 0
                best_h = video_only.get("height") or 0
            elif stream_data.get("hls"):
                err = await _ffmpeg_copy(["-i", stream_data["hls"]], path, timeout=300)
                if err:
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return None, f"HLS conversion failed: {err}"
            else:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, "No playable Piped stream with audio found"

        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size < 1000:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Piped empty file"
        if size > MAX_TG:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, f"Too large ({size // 1048576} MB, limit 50 MB)"
        if not await has_video_stream(path):
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Piped result has no video stream"
        if not await has_audio_stream(path):
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Piped result has no audio stream"

        dur, probed_w, probed_h = await _ffprobe(path)
        log.info("[piped] OK: %s (%d bytes)", title, size)
        return {
            "type": "video", "path": path, "title": title,
            "duration": dur or duration, "width": probed_w or best_w, "height": probed_h or best_h,
            "size": size, "dir": str(out_dir),
        }, None

    except Exception as e:
        log.error("[piped] %s", e)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, str(e)


# ── handle messages ──

async def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    msg = callback.get("message", {})
    chat = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")

    if callback_id:
        await answer_callback(callback_id, "Working...")
    if not chat or not mid:
        return

    if data == "proxy_check":
        await edit_text(chat, mid, "Checking proxies...", proxy_keyboard())
        text = await check_proxies_text()
        await edit_text(chat, mid, text, proxy_keyboard())
        return

    await edit_text(chat, mid, bot_status_text(), proxy_keyboard())


async def _send_photo_collection(chat, result, caption, reply):
    paths = result["photo_paths"]
    for offset in range(0, len(paths), 10):
        chunk = paths[offset:offset + 10]
        chunk_caption = caption if offset == 0 else ""
        if len(chunk) == 1:
            await send_photo(chat, chunk[0], chunk_caption, reply)
        else:
            await send_media_group(chat, chunk, chunk_caption, reply)


async def _send_mixed_media(chat, result, caption, reply):
    media = result.get("media") or []
    for offset in range(0, len(media), 10):
        chunk = media[offset:offset + 10]
        chunk_caption = caption if offset == 0 else ""
        if len(chunk) == 1:
            item = chunk[0]
            if item.get("type") == "video":
                await send_video(
                    chat, item["path"], chunk_caption, reply,
                    item.get("duration", 0), item.get("width", 0), item.get("height", 0),
                )
            else:
                await send_photo(chat, item["path"], chunk_caption, reply)
        else:
            await send_media_group(chat, chunk, chunk_caption, reply)


async def process_url(chat, mid, text, platform, url):
    key = (chat, url)
    if key in busy:
        await send_text(chat, "Already downloading this link for this chat…", mid)
        return

    busy.add(key)
    result = None
    sid = None
    try:
        emoji = EMOJI.get(platform, "🎬")
        res = await send_text(chat, f"{emoji} Downloading…", mid)
        sid = res.get("result", {}).get("message_id")

        log.info("[%s] waiting for download slot: %s", platform, url)
        async with DOWNLOAD_SEMAPHORE:
            log.info("[%s] download started: %s", platform, url)
            result, err = await download_media(url, platform)

        clip_request = None
        if platform == "youtube":
            clip_request = extract_clip_request(text, url)
            if clip_request and result and result.get("type") == "video":
                clip_path = str(Path(result["dir"]) / "clip.mp4")
                ok = await cut_video_clip(
                    result["path"],
                    clip_path,
                    clip_request["start"],
                    clip_request["duration"],
                )
                if ok:
                    try:
                        os.remove(result["path"])
                    except Exception:
                        pass
                    dur, w, h = await _ffprobe(clip_path)
                    result["path"] = clip_path
                    result["size"] = Path(clip_path).stat().st_size
                    result["duration"] = dur
                    result["width"] = w or result.get("width", 0)
                    result["height"] = h or result.get("height", 0)
                    size_err = validate_result_size(result)
                    if size_err:
                        err = size_err
                else:
                    err = "Could not cut requested clip"

        if err and platform == "youtube" and "sign in" in err.lower():
            err = "YouTube blocked this server. Set YOUTUBE_COOKIES_BASE64 env var."

        if err:
            if sid:
                await edit_text(chat, sid, f"❌ {err[:300]}")
            asyncio.create_task(_del_later(chat, sid, 15))
            return

        try:
            if sid:
                await edit_text(chat, sid, "⬆️ Uploading…")

            async with UPLOAD_SEMAPHORE:
                if result["type"] == "photos":
                    cap = make_caption(emoji, result["title"], result.get("description", ""))
                    await _send_photo_collection(chat, result, cap, mid)
                    if result.get("audio_path"):
                        await send_audio(chat, result["audio_path"], make_caption(emoji, result["title"], extra="Audio"), mid)
                    await send_description_if_needed(chat, emoji, result, cap, mid)

                elif result["type"] == "media":
                    cap = make_caption(emoji, result["title"], result.get("description", ""))
                    await _send_mixed_media(chat, result, cap, mid)
                    if result.get("audio_path"):
                        await send_audio(chat, result["audio_path"], make_caption(emoji, result["title"], extra="Audio"), mid)
                    await send_description_if_needed(chat, emoji, result, cap, mid)

                elif result["type"] == "audio":
                    cap = make_caption(emoji, result["title"], result.get("description", ""), "audio only")
                    await send_audio(chat, result["path"], cap, mid)
                    await send_description_if_needed(chat, emoji, result, cap, mid)

                else:
                    size_mb = result["size"] / 1048576
                    clip_note = ""
                    if clip_request:
                        if "end" in clip_request:
                            clip_note = f"\nClip: {clip_request['start']}s → {clip_request['end']}s"
                        else:
                            clip_note = f"\nClip: {clip_request['start']}s +{clip_request['duration']}s"
                    cap = make_caption(emoji, result["title"], result.get("description", ""), f"{size_mb:.1f} MB{clip_note}")
                    await send_video(chat, result["path"], cap, mid,
                                     result["duration"], result["width"], result["height"])
                    await send_description_if_needed(chat, emoji, result, cap, mid)

            if sid:
                await delete_msg(chat, sid)

        except Exception as e:
            log.error("Upload: %s", e)
            if sid:
                await edit_text(chat, sid, "❌ Upload failed")
            asyncio.create_task(_del_later(chat, sid, 15))

    finally:
        shutil.rmtree(result.get("dir", "") if result else "", ignore_errors=True)
        busy.discard(key)


async def handle(update):
    if update.get("callback_query"):
        await handle_callback(update["callback_query"])
        return

    msg = update.get("message", {})
    text = msg.get("text", "")
    chat = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    if not chat or not text:
        return

    if text.startswith("/help"):
        await send_text(chat, bot_help_text(), mid, proxy_keyboard())
        return

    if text.startswith("/status"):
        await send_text(chat, bot_status_text(), mid, proxy_keyboard())
        return

    if text.startswith("/proxies"):
        await send_text(chat, await check_proxies_text(), mid, proxy_keyboard())
        return

    if text.startswith("/start"):
        await send_text(chat, f"{bot_help_text()}\n\n{proxy_status_text()}", mid, proxy_keyboard())
        return

    urls = find_urls(text)
    if not urls:
        return

    await asyncio.gather(*(process_url(chat, mid, text, platform, url) for platform, url in urls[:MAX_LINKS_PER_MESSAGE]))


async def _del_later(chat, mid, delay):
    await asyncio.sleep(delay)
    await delete_msg(chat, mid)


# ── polling ──

async def poll():
    offset = 0
    s = await get_session()
    log.info("Polling started")
    while True:
        try:
            async with s.get(f"{API}/getUpdates?offset={offset}&timeout=30",
                              timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = await r.json()
                if not data.get("ok"):
                    log.error("getUpdates: %s", data)
                    await asyncio.sleep(5)
                    continue
                for u in data.get("result", []):
                    offset = u["update_id"] + 1
                    asyncio.create_task(handle(u))
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            log.error("Poll: %s", e)
            await asyncio.sleep(5)


async def cleanup():
    import time
    while True:
        await asyncio.sleep(300)
        try:
            for d in DL_DIR.iterdir():
                if d.is_dir() and d.name != "cookies" and time.time() - d.stat().st_mtime > 300:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


async def main():
    log.info("yt-dlp %s | bun: %s", yt_dlp.version.__version__, "yes" if shutil.which("bun") else "no")
    log.info("Bot starting")
    asyncio.create_task(cleanup())
    await poll()


if __name__ == "__main__":
    asyncio.run(main())
