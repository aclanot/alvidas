import os
import re
import json
import glob
import base64
import shutil
import random
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

INSTAGRAM_COOKIES_BASE64 = os.environ.get("INSTAGRAM_COOKIES_BASE64", "")
YOUTUBE_COOKIES_BASE64 = os.environ.get("YOUTUBE_COOKIES_BASE64", "")
TWITTER_COOKIES_BASE64 = os.environ.get("TWITTER_COOKIES_BASE64", "")
TIKTOK_COOKIES_BASE64 = os.environ.get("TIKTOK_COOKIES_BASE64", "")

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_LIST_RAW = os.environ.get("PROXY_LIST", "")

PROXY_POOL = []


def setup_proxies():
    global PROXY_POOL
    if PROXY_LIST_RAW:
        for line in PROXY_LIST_RAW.strip().split(","):
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pw = parts
                PROXY_POOL.append(f"http://{user}:{pw}@{ip}:{port}")
            elif line.startswith(("http://", "https://", "socks")):
                PROXY_POOL.append(line)
    if PROXY_URL and not PROXY_POOL:
        PROXY_POOL.append(PROXY_URL)
    if PROXY_POOL:
        logger.info("Loaded %d proxies", len(PROXY_POOL))


def get_proxy():
    if PROXY_POOL:
        return random.choice(PROXY_POOL)
    return None


setup_proxies()

TELEGRAM_MAX_SIZE = 50 * 1024 * 1024
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "bot_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_DIR = DOWNLOAD_DIR / "cookies"
COOKIES_DIR.mkdir(exist_ok=True)

PLATFORM_COOKIES = {}

http_session: aiohttp.ClientSession = None
processing = set()

URL_PATTERNS = [
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/[@\w./]+",
    r"https?://(?:www\.)?tiktok\.com/@[\w.]+/video/\d+",
    r"https?://(?:www\.)?tiktok\.com/@[\w.]+/photo/\d+",
    r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+",
    r"https?://(?:www\.)?youtu\.be/[\w-]+",
    r"https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+",
    r"https?://music\.youtube\.com/watch\?v=[\w-]+",
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+",
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[\w-]+",
    r"https?://(?:www\.)?facebook\.com/(?:watch/\?v=|share/v/|reel/)[\w-]+",
    r"https?://(?:www\.)?fb\.watch/[\w-]+",
    r"https?://(?:www\.)?reddit\.com/r/\w+/comments/\w+",
    r"https?://(?:www\.)?redd\.it/\w+",
]

PLATFORM_EMOJI = {
    "tiktok": "\U0001f3b5",
    "youtube": "\u25b6\ufe0f",
    "twitter": "\U0001d54f",
    "instagram": "\U0001f4f7",
    "facebook": "\U0001f4d8",
    "reddit": "\U0001f916",
    "unknown": "\U0001f3ac",
}


def setup_cookies():
    cookie_map = {
        "instagram": INSTAGRAM_COOKIES_BASE64,
        "youtube": YOUTUBE_COOKIES_BASE64,
        "twitter": TWITTER_COOKIES_BASE64,
        "tiktok": TIKTOK_COOKIES_BASE64,
    }
    for platform, b64 in cookie_map.items():
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
            content = raw.decode("utf-8-sig", errors="replace")
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            if not content.startswith("# Netscape HTTP Cookie File"):
                content = "# Netscape HTTP Cookie File\n# https://curl.haxx.se/rfc/cookie_spec.html\n# This is a generated file! Do not edit.\n\n" + content
            p = COOKIES_DIR / f"{platform}.txt"
            p.write_text(content, encoding="utf-8")
            PLATFORM_COOKIES[platform] = str(p)
            lines = [l for l in content.strip().splitlines() if l.strip() and not l.startswith("#")]
            has_sessionid = "sessionid" in content
            logger.info(
                "Cookies for %s: %d bytes, %d data lines, sessionid=%s, first_line=%r, path=%s",
                platform, len(content), len(lines), has_sessionid,
                lines[0][:80] if lines else "EMPTY", p,
            )
        except Exception as e:
            logger.error("Failed to decode %s cookies: %s", platform, e)


setup_cookies()


def detect_platform(url):
    d = urlparse(url).netloc.lower()
    if "tiktok" in d:
        return "tiktok"
    if "instagram" in d:
        return "instagram"
    if "youtube" in d or "youtu.be" in d:
        return "youtube"
    if "twitter" in d or "x.com" in d:
        return "twitter"
    if "facebook" in d or "fb.watch" in d:
        return "facebook"
    if "reddit" in d or "redd.it" in d:
        return "reddit"
    return "unknown"


def extract_urls(text):
    urls = []
    for pattern in URL_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            u = m.group(0)
            if u not in urls:
                urls.append(u)
    return urls


# ──────────────────────────────────────────────
#  Telegram Bot API helpers (raw aiohttp)
# ──────────────────────────────────────────────

async def get_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session


async def api(method, data, timeout=120):
    s = await get_session()
    async with s.post(f"{BOT_API_URL}/{method}", data=data,
                      timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        res = await r.json()
        if not res.get("ok"):
            raise Exception(res.get("description", f"{method} failed"))
        return res


async def send_msg(chat_id, text, reply_to=None):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat_id))
    d.add_field("text", text)
    if reply_to:
        d.add_field("reply_to_message_id", str(reply_to))
    return await api("sendMessage", d, 30)


async def edit_msg(chat_id, mid, text):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat_id))
    d.add_field("message_id", str(mid))
    d.add_field("text", text)
    return await api("editMessageText", d, 30)


async def del_msg(chat_id, mid):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat_id))
    d.add_field("message_id", str(mid))
    try:
        return await api("deleteMessage", d, 30)
    except Exception:
        pass


async def send_video(chat_id, fp, caption, reply_to, dur=0, w=0, h=0):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat_id))
    d.add_field("video", open(fp, "rb"), filename="video.mp4",
                content_type="video/mp4")
    if caption:
        d.add_field("caption", caption[:1024])
    if reply_to:
        d.add_field("reply_to_message_id", str(reply_to))
    if dur:
        d.add_field("duration", str(dur))
    if w:
        d.add_field("width", str(w))
    if h:
        d.add_field("height", str(h))
    d.add_field("supports_streaming", "true")
    return await api("sendVideo", d, 120)


async def delete_later(chat_id, mid, delay=10):
    await asyncio.sleep(delay)
    await del_msg(chat_id, mid)


# ──────────────────────────────────────────────
#  Cobalt API fallback for YouTube
# ──────────────────────────────────────────────

COBALT_INSTANCES = [
    "https://api.cobalt.tools",
]
COBALT_API_KEY = os.environ.get("COBALT_API_KEY", "")


async def cobalt_download(url, out_dir, job_id):
    """Fallback: download YouTube via cobalt API when yt-dlp fails on server"""
    out_dir.mkdir(exist_ok=True)
    session = await get_session()

    for instance in COBALT_INSTANCES:
        try:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if COBALT_API_KEY:
                headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"

            payload = {"url": url, "videoQuality": "720"}
            async with session.post(
                f"{instance}/",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()

            dl_url = data.get("url")
            if not dl_url:
                logger.error("Cobalt no URL: %s", data)
                continue

            # download the file
            final_path = str(out_dir / f"{job_id}.mp4")
            async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    continue
                with open(final_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)

            fsize = os.path.getsize(final_path)
            if fsize < 1000:
                os.remove(final_path)
                continue

            if fsize > TELEGRAM_MAX_SIZE:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, None, 0, 0, 0, f"File too large ({fsize // 1048576} MB > 50 MB)"

            dur, w, h = await ffprobe_meta(final_path)
            title = data.get("filename", "Video").rsplit(".", 1)[0][:100]
            logger.info("Cobalt download OK: %s", title)
            return final_path, title, dur, w, h, None

        except Exception as e:
            logger.error("Cobalt error (%s): %s", instance, e)
            continue

    return None


# ──────────────────────────────────────────────
#  RECLIP download logic  (app.py lines 16-73)
#  Exact same command, made async
# ──────────────────────────────────────────────

async def reclip_download(url):
    """
    Reclip run_download() core command (app.py lines 20-29):
      cmd = ["yt-dlp", "--no-playlist", "-o", out_template,
             "-f", "bestvideo+bestaudio/best",
             "--merge-output-format", "mp4"]
      cmd.append(url)

    Additions for server environment:
      - per-platform cookies from base64 env vars
      - twitter syndication API to avoid guest token error
    Returns (file_path, title, duration, width, height, error)
    """
    job_id = os.urandom(5).hex()
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    out_template = str(out_dir / f"{job_id}.%(ext)s")
    platform = detect_platform(url)

    # ── reclip command ──
    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    # verbose for instagram to debug cookie issues
    if platform == "instagram":
        cmd += ["--verbose"]

    # format: reclip default, but flexible fallback for server clients
    if platform == "youtube":
        cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    # per-platform cookies
    cookie_path = PLATFORM_COOKIES.get(platform)
    if cookie_path:
        cmd += ["--cookies", cookie_path]

    # proxy only for platforms that need it (NOT instagram — proxy IP breaks cookies)
    if platform not in ("instagram",):
        proxy = get_proxy()
        if proxy:
            cmd += ["--proxy", proxy]

    # youtube: try multiple player clients to bypass bot detection
    if platform == "youtube":
        cmd += ["--extractor-args", "youtube:player_client=mweb,default"]

    # twitter needs syndication API on servers (guest token is broken)
    if platform == "twitter":
        cmd += ["--extractor-args", "twitter:api=syndication"]

    cmd.append(url)

    try:
        logger.info("yt-dlp: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            full_err = stderr.decode().strip() if stderr else ""
            err = full_err.split("\n")[-1] if full_err else "unknown"
            if platform == "instagram":
                logger.error("yt-dlp Instagram FULL stderr:\n%s", full_err[-2000:])
            else:
                logger.error("yt-dlp error: %s", err)

            # YouTube fallback: try cobalt API
            if platform == "youtube":
                logger.info("Trying cobalt fallback for YouTube: %s", url)
                cobalt_result = await cobalt_download(url, out_dir, job_id)
                if cobalt_result:
                    return cobalt_result

            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None, 0, 0, 0, err

        # ── exact reclip file selection ──
        files = glob.glob(str(out_dir / f"{job_id}.*"))
        if not files:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None, 0, 0, 0, "Download completed but no file was found"

        target = [f for f in files if f.endswith(".mp4")]
        chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        # size check
        fsize = os.path.getsize(chosen)
        if fsize > TELEGRAM_MAX_SIZE:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, None, 0, 0, 0, f"File too large ({fsize // 1048576} MB > 50 MB limit)"

        # metadata
        dur, w, h = await ffprobe_meta(chosen)

        # title
        title = await get_title(url)

        return chosen, title, dur, w, h, None

    except asyncio.TimeoutError:
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, None, 0, 0, 0, "Download timed out (5 min limit)"
    except Exception as e:
        logger.error("Download exception: %s", e)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, None, 0, 0, 0, str(e)


async def ffprobe_meta(path):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(out.decode())
        dur = int(float(data.get("format", {}).get("duration", 0)))
        w = h = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width", 0), s.get("height", 0)
                break
        return dur, w, h
    except Exception:
        return 0, 0, 0


async def get_title(url):
    try:
        cmd = ["yt-dlp", "--get-title", "--no-warnings", "--no-playlist", url]
        platform = detect_platform(url)
        cookie_path = PLATFORM_COOKIES.get(platform)
        if cookie_path:
            cmd += ["--cookies", cookie_path]
        if platform == "twitter":
            cmd += ["--extractor-args", "twitter:api=syndication"]
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=15)
        t = out.decode().strip()[:100]
        return t if t else "Video"
    except Exception:
        return "Video"


# ──────────────────────────────────────────────
#  Bot update handler
# ──────────────────────────────────────────────

async def handle_update(update):
    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    if not chat_id or not text:
        return

    if text.startswith("/start"):
        await send_msg(
            chat_id,
            "\U0001f3ac Video Downloader\n\n"
            "Send a link from:\n"
            "\u2022 TikTok\n"
            "\u2022 Instagram\n"
            "\u2022 YouTube\n"
            "\u2022 X / Twitter\n"
            "\u2022 Facebook, Reddit, etc.\n\n"
            "Works in groups \u2014 just drop a link.",
            reply_to=msg_id)
        return

    urls = extract_urls(text)
    if not urls:
        return

    for url in urls[:3]:
        h = hash(url)
        if h in processing:
            continue
        processing.add(h)
        try:
            platform = detect_platform(url)
            emoji = PLATFORM_EMOJI.get(platform, "\U0001f3ac")

            st = await send_msg(chat_id, f"{emoji} Downloading\u2026", reply_to=msg_id)
            sid = st.get("result", {}).get("message_id")

            fp, title, dur, w, ht, err = await reclip_download(url)

            if err:
                if sid:
                    await edit_msg(chat_id, sid, f"\u274c {err[:300]}")
                    asyncio.create_task(delete_later(chat_id, sid, 15))
                continue

            try:
                if sid:
                    await edit_msg(chat_id, sid, "\u2b06\ufe0f Uploading\u2026")

                size_mb = os.path.getsize(fp) / 1048576
                cap = f"{emoji} {title}\n{size_mb:.1f} MB"
                await send_video(chat_id, fp, cap, msg_id, dur, w, ht)

                if sid:
                    await del_msg(chat_id, sid)
            except Exception as e:
                logger.error("Upload error: %s", e)
                if sid:
                    try:
                        await edit_msg(chat_id, sid, "\u274c Upload failed")
                    except Exception:
                        pass
                    asyncio.create_task(delete_later(chat_id, sid, 10))
            finally:
                try:
                    shutil.rmtree(Path(fp).parent, ignore_errors=True)
                except Exception:
                    pass
        finally:
            processing.discard(h)


# ──────────────────────────────────────────────
#  Polling
# ──────────────────────────────────────────────

async def polling_loop():
    offset = 0
    s = await get_session()
    logger.info("Polling started")
    while True:
        try:
            async with s.get(
                f"{BOT_API_URL}/getUpdates?offset={offset}&timeout=30",
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                data = await r.json()
            if not data.get("ok"):
                logger.error("getUpdates: %s", data)
                await asyncio.sleep(5)
                continue
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                asyncio.create_task(handle_update(u))
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error("Poll error: %s", e)
            await asyncio.sleep(5)


async def cleanup_loop():
    import time
    while True:
        await asyncio.sleep(300)
        try:
            for item in DOWNLOAD_DIR.iterdir():
                if item.is_dir() and time.time() - item.stat().st_mtime > 300:
                    shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN")
    logger.info("Bot starting")
    asyncio.create_task(cleanup_loop())
    await polling_loop()


if __name__ == "__main__":
    asyncio.run(main())
