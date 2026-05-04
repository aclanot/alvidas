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
from urllib.parse import urlparse, parse_qs

import aiohttp
import yt_dlp

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ── config ──
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
INSTAGRAM_COOKIES_B64 = os.environ.get("INSTAGRAM_COOKIES_BASE64", "")
PROXY_LIST_RAW = os.environ.get("PROXY_LIST", "")
COBALT_API_URL = (os.environ.get("COBALT_API_URL") or "https://api.cobalt.tools/").strip()
if not COBALT_API_URL:
    COBALT_API_URL = "https://api.cobalt.tools/"
if not COBALT_API_URL.endswith("/"):
    COBALT_API_URL += "/"
COBALT_API_KEY = os.environ.get("COBALT_API_KEY", "").strip()
MAX_TG = 50 * 1024 * 1024
TG_CAPTION_LIMIT = 1024
DESCRIPTION_CHUNK_LIMIT = 3900
DL_DIR = Path(tempfile.gettempdir()) / "downloads"
DL_DIR.mkdir(exist_ok=True)
COOKIE_DIR = DL_DIR / "cookies"
COOKIE_DIR.mkdir(exist_ok=True)
COOKIES = {}

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
    "tiktok":    [r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+"],
    "instagram": [r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv|stories)/[\w.-]+(?:/[\w.-]+)?"],
    "twitter":   [r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+"],
    "youtube":   [
        r"https?://(?:www\.)?youtube\.com/(?:watch\?v=|shorts/)[\w-]+",
        r"https?://youtu\.be/[\w-]+",
        r"https?://music\.youtube\.com/watch\?v=[\w-]+",
    ],
}
EMOJI = {"tiktok": "🎵", "instagram": "📷", "twitter": "𝕏", "youtube": "▶️"}

session: aiohttp.ClientSession = None
busy = set()


async def get_session():
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    return session


def current_proxy():
    if not PROXIES:
        return None
    return random.choice(PROXIES)


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


def proxy_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "Check proxies", "callback_data": "proxy_check"},
        ]]
    }


def proxy_status_text(prefix="Proxy settings"):
    if not PROXIES:
        return f"{prefix}\n\nNo proxies configured. Set PROXY_LIST in Railway to enable proxies."
    return (
        f"{prefix}\n\n"
        f"Configured: {len(PROXIES)}\n"
        "Downloads use the configured proxies automatically."
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
        return False, str(e).split("\n")[0][:80], None


async def check_proxies_text():
    if not PROXIES:
        return proxy_status_text("Proxy check")
    lines = [proxy_status_text("Proxy check"), ""]
    results = await asyncio.gather(*(check_one_proxy(proxy) for proxy in PROXIES))
    for i, (proxy, result) in enumerate(zip(PROXIES, results), start=1):
        ok, detail, elapsed = result
        status = "OK" if ok else "FAIL"
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
        f"Busy downloads: {len(busy)}\n"
        f"Proxies configured: {len(PROXIES)}\n"
        f"Cookies loaded: {', '.join(sorted(COOKIES)) if COOKIES else 'none'}"
    )


def find_urls(text):
    found = []
    for platform, pats in PATTERNS.items():
        for pat in pats:
            for m in re.finditer(pat, text, re.IGNORECASE):
                url = m.group(0)
                if url not in [u for _, u in found]:
                    found.append((platform, url))
    return found


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
    async with s.post(f"{API}/{method}", data=data, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        res = await r.json()
        if not res.get("ok"):
            raise Exception(res.get("description", method))
        return res


async def send_text(chat, text, reply=None, reply_markup=None):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("text", text)
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    if reply_markup:
        d.add_field("reply_markup", json.dumps(reply_markup))
    return await tg("sendMessage", d, 30)


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


async def send_video(chat, path, caption, reply, dur=0, w=0, h=0):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("video", open(path, "rb"), filename="video.mp4", content_type="video/mp4")
    d.add_field("caption", caption[:TG_CAPTION_LIMIT])
    d.add_field("supports_streaming", "true")
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    if dur:
        d.add_field("duration", str(dur))
    if w:
        d.add_field("width", str(w))
    if h:
        d.add_field("height", str(h))
    return await tg("sendVideo", d, 180)


# FIX: use real file extension and correct content-type instead of always audio/mpeg
async def send_audio(chat, path, caption, reply):
    ext = Path(path).suffix.lower() or ".ogg"
    _CT = {
        ".mp3":  "audio/mpeg",
        ".mp4":  "audio/mp4",
        ".m4a":  "audio/mp4",
        ".ogg":  "audio/ogg",
        ".opus": "audio/ogg",
        ".webm": "audio/webm",
    }
    ct = _CT.get(ext, "audio/octet-stream")
    filename = f"audio{ext}"
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("audio", open(path, "rb"), filename=filename, content_type=ct)
    d.add_field("caption", caption[:TG_CAPTION_LIMIT])
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    return await tg("sendAudio", d, 60)


async def send_photo(chat, path, caption, reply):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("photo", open(path, "rb"), filename="photo.jpg", content_type="image/jpeg")
    if caption:
        d.add_field("caption", caption[:TG_CAPTION_LIMIT])
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    return await tg("sendPhoto", d, 60)


async def send_media_group(chat, paths, caption, reply):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    media = []
    for i, p in enumerate(paths[:10]):
        key = f"photo{i}"
        d.add_field(key, open(p, "rb"), filename=f"{key}.jpg", content_type="image/jpeg")
        entry = {"type": "photo", "media": f"attach://{key}"}
        if i == 0 and caption:
            entry["caption"] = caption[:TG_CAPTION_LIMIT]
        media.append(entry)
    d.add_field("media", json.dumps(media))
    return await tg("sendMediaGroup", d, 120)


# ── fast paths: direct API downloads ──

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
                         headers={"User-Agent": "BotikDodik/1.0"},
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
                video_url = videos[0].get("url", "")
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
                log.info("[twitter/fxtwitter] OK: %s (%d bytes)", title, size)
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


async def _has_video_stream_strict(path):
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=codec_type", "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        data = json.loads(out)
        return len(data.get("streams", [])) > 0
    except Exception:
        return False


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


def _ext_from_content_type(content_type, default_ext):
    content_type = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
    }.get(content_type, default_ext)


def _ext_from_cobalt(filename, media_url, content_type, default_ext):
    ext = Path(filename or "").suffix.lower()
    if ext and len(ext) <= 6:
        return ext
    ext = _ext_from_content_type(content_type, "")
    if ext:
        return ext
    return _ext_from_url(media_url, default_ext)


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


async def _download_cobalt_media(s, media_url, out_dir, stem, default_ext, filename=None, timeout=90):
    tmp_path = out_dir / f"{stem}.tmp"
    try:
        async with s.get(media_url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                return None
            content_type = r.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip().lower() in {"application/json", "text/html", "text/plain"}:
                return None
            with open(tmp_path, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)

        if not tmp_path.exists() or tmp_path.stat().st_size < 100:
            tmp_path.unlink(missing_ok=True)
            return None

        ext = _ext_from_cobalt(filename, media_url, content_type, default_ext)
        path = out_dir / f"{stem}{ext}"
        tmp_path.replace(path)
        return str(path)
    except Exception as e:
        log.warning("[instagram/cobalt] file download failed: %s", e)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


async def _instagram_fast(url):
    """Download Instagram via cobalt API without Instagram cookies."""
    s = await get_session()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"

    payload = {
        "url": url,
        "videoQuality": "1080",
        "filenameStyle": "basic",
    }
    out_dir = None

    try:
        async with s.post(
            COBALT_API_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            if r.status != 200:
                body = await r.text()
                log.info("[instagram/cobalt] API HTTP %s: %s", r.status, body[:160])
                return None, None
            data = await r.json(content_type=None)

        status = data.get("status")
        if status == "error":
            error = data.get("error") or {}
            log.warning("[instagram/cobalt] error=%s", error.get("code") or error)
            return None, None
        if status not in ("tunnel", "redirect", "stream", "picker"):
            log.info("[instagram/cobalt] unsupported status=%s", status)
            return None, None

        job = os.urandom(5).hex()
        out_dir = DL_DIR / job
        out_dir.mkdir(exist_ok=True)
        title = "Instagram"

        if status == "picker":
            photo_paths = []
            video_paths = []
            for i, item in enumerate((data.get("picker") or [])[:10]):
                item_url = item.get("url")
                if not item_url:
                    continue
                item_type = item.get("type")
                if item_type in ("video", "gif"):
                    path = await _download_cobalt_media(s, item_url, out_dir, f"video_{i}", ".mp4", timeout=90)
                    if path and await _has_video_stream_strict(path):
                        video_paths.append(path)
                    elif path:
                        Path(path).unlink(missing_ok=True)
                    continue

                path = await _download_cobalt_media(s, item_url, out_dir, f"photo_{i}", ".jpg", timeout=30)
                if path:
                    photo_paths.append(path)

            audio_path = None
            audio_url = data.get("audio")
            if audio_url:
                audio_path = await _download_cobalt_media(
                    s, audio_url, out_dir, "audio", ".m4a", data.get("audioFilename"), timeout=60
                )

            if photo_paths:
                log.info("[instagram/cobalt] %d photos, audio=%s", len(photo_paths), bool(audio_path))
                return {
                    "type": "photos",
                    "title": title,
                    "description": "",
                    "photo_paths": photo_paths,
                    "audio_path": audio_path,
                    "dir": str(out_dir),
                }, None

            if video_paths:
                path = video_paths[0]
                dur, w, h = await _ffprobe(path)
                size = Path(path).stat().st_size
                log.info("[instagram/cobalt] picker video OK: %d bytes", size)
                return {
                    "type": "video",
                    "path": path,
                    "title": title,
                    "description": "",
                    "duration": dur,
                    "width": w,
                    "height": h,
                    "size": size,
                    "dir": str(out_dir),
                }, None

            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None

        media_url = data.get("url")
        if not media_url:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None

        path = await _download_cobalt_media(
            s, media_url, out_dir, job, ".mp4", data.get("filename"), timeout=90
        )
        if not path:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None

        suffix = Path(path).suffix.lower()
        size = Path(path).stat().st_size
        if suffix in IMAGE_EXT:
            log.info("[instagram/cobalt] photo OK: %d bytes", size)
            return {
                "type": "photos",
                "title": title,
                "description": "",
                "photo_paths": [path],
                "audio_path": None,
                "dir": str(out_dir),
            }, None

        if not await _has_video_stream_strict(path):
            if await has_audio_stream(path):
                log.info("[instagram/cobalt] audio OK: %d bytes", size)
                return {
                    "type": "audio",
                    "path": path,
                    "title": title,
                    "description": "",
                    "size": size,
                    "dir": str(out_dir),
                }, None
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None

        dur, w, h = await _ffprobe(path)
        log.info("[instagram/cobalt] video OK: %d bytes", size)
        return {
            "type": "video",
            "path": path,
            "title": title,
            "description": "",
            "duration": dur,
            "width": w,
            "height": h,
            "size": size,
            "dir": str(out_dir),
        }, None

    except Exception as e:
        log.warning("[instagram/cobalt] %s", e)
        if out_dir:
            shutil.rmtree(out_dir, ignore_errors=True)
        return None, None


def make_caption(emoji, title, description="", extra=""):
    lines = [f"{emoji} {title}".strip()]
    if extra:
        lines.append(extra.strip())
    base = "\n\n".join([line for line in lines if line])
    description = (description or "").strip()
    if not description or description in base:
        return base[:TG_CAPTION_LIMIT]

    full = f"{base}\n\n{description}" if base else description
    if len(full) <= TG_CAPTION_LIMIT:
        return full
    return base[:TG_CAPTION_LIMIT]


def split_text(text, limit=DESCRIPTION_CHUNK_LIMIT):
    text = (text or "").strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunk = text[:cut].strip()
        if chunk:
            yield chunk
        text = text[cut:].strip()
    if text:
        yield text


async def send_description_if_needed(chat, emoji, result, caption, reply):
    description = (result.get("description") or "").strip()
    if not description or description in caption:
        return
    for i, chunk in enumerate(split_text(description)):
        prefix = f"{emoji} Description:\n" if i == 0 else ""
        await send_text(chat, f"{prefix}{chunk}", reply)


async def download_media(url, platform):
    if platform == "instagram":
        result, err = await _instagram_fast(url)
        if result:
            return result, None
        log.info("[instagram] cobalt failed, falling back to yt-dlp")

    if platform == "twitter":
        result, err = await _twitter_fast(url)
        if result:
            return result, None

    if platform == "tiktok":
        result, err = await _tiktok_fast(url)
        if result:
            return result, None

    return await _ytdlp_download(url, platform)


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

    if platform not in ("instagram", "youtube") and current_proxy():
        opts["proxy"] = current_proxy()

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


# ── cobalt fallback for YouTube ──

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
    m = re.search(r"(?:v=|shorts/|youtu\.be/)([\w-]{11})", url)
    return m.group(1) if m else None


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
                    if stream_data.get("videoStreams"):
                        log.info("[piped] using %s", instance)
                        break
                    stream_data = None
            except Exception as e:
                log.warning("[piped] %s failed: %s", instance, e)
                continue

        if not stream_data:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "All Piped instances failed"

        title    = stream_data.get("title", "Video")[:100]
        duration = stream_data.get("duration", 0)

        best_url = None
        best_w = best_h = 0
        for vs in stream_data.get("videoStreams", []):
            if not vs.get("videoOnly", True) and vs.get("url"):
                h = vs.get("height", 0)
                if h <= 720 and h > best_h:
                    best_url = vs["url"]
                    best_w   = vs.get("width", 0)
                    best_h   = h

        if not best_url:
            for vs in stream_data.get("videoStreams", []):
                if vs.get("url"):
                    h = vs.get("height", 0)
                    if h <= 720 and h > best_h:
                        best_url = vs["url"]
                        best_w   = vs.get("width", 0)
                        best_h   = h

        if not best_url:
            hls = stream_data.get("hls")
            if hls:
                best_url = hls

        if not best_url:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "No suitable stream found"

        async with s.get(best_url, timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status != 200:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, f"Piped stream HTTP {r.status}"
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)

        size = os.path.getsize(path)
        if size < 1000:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Piped empty file"
        if size > MAX_TG:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, f"Too large ({size // 1048576} MB, limit 50 MB)"

        log.info("[piped] OK: %s (%d bytes)", title, size)
        return {
            "type": "video", "path": path, "title": title,
            "duration": duration, "width": best_w, "height": best_h,
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


async def handle(update):
    if update.get("callback_query"):
        await handle_callback(update["callback_query"])
        return

    msg  = update.get("message", {})
    text = msg.get("text", "")
    chat = msg.get("chat", {}).get("id")
    mid  = msg.get("message_id")
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

    if text.startswith("/start"):
        await send_text(chat, (
            "🎬 Video Downloader\n\n"
            "Send a link from:\n"
            "• TikTok\n• Instagram\n• YouTube\n• X / Twitter\n\n"
            "Works in groups — just drop a link."
        ), mid, proxy_keyboard())
        return

    urls = find_urls(text)
    if not urls:
        return

    for platform, url in urls[:3]:
        key = hash(url)
        if key in busy:
            continue
        busy.add(key)
        result = None
        try:
            emoji = EMOJI.get(platform, "🎬")
            res   = await send_text(chat, f"{emoji} Downloading…", mid)
            sid   = res.get("result", {}).get("message_id")

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
                    else:
                        err = "Could not cut requested clip"

            if err and platform == "youtube" and "sign in" in err.lower():
                err = "YouTube blocked this server. Set YOUTUBE_COOKIES_BASE64 env var."

            if err:
                if sid:
                    await edit_text(chat, sid, f"❌ {err[:300]}")
                asyncio.create_task(_del_later(chat, sid, 15))
                continue

            try:
                if sid:
                    await edit_text(chat, sid, "⬆️ Uploading…")

                if result["type"] == "photos":
                    paths = result["photo_paths"]
                    cap   = make_caption(emoji, result["title"], result.get("description", ""))
                    for offset in range(0, len(paths), 10):
                        chunk = paths[offset:offset + 10]
                        chunk_caption = cap if offset == 0 else ""
                        if len(chunk) == 1:
                            await send_photo(chat, chunk[0], chunk_caption, mid)
                        else:
                            await send_media_group(chat, chunk, chunk_caption, mid)
                    if result.get("audio_path"):
                        await send_audio(chat, result["audio_path"], make_caption(emoji, result["title"], extra="Audio"), mid)
                    await send_description_if_needed(chat, emoji, result, cap, mid)

                # FIX: route audio-only results to sendAudio instead of sendVideo
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
                    cap     = make_caption(emoji, result["title"], result.get("description", ""), f"{size_mb:.1f} MB{clip_note}")
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
