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

import aiohttp

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ── config ──
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

INSTAGRAM_COOKIES_B64 = os.environ.get("INSTAGRAM_COOKIES_BASE64", "")
PROXY_LIST_RAW = os.environ.get("PROXY_LIST", "")

MAX_TG_SIZE = 50 * 1024 * 1024
DL_DIR = Path(tempfile.gettempdir()) / "downloads"
DL_DIR.mkdir(exist_ok=True)
COOKIE_FILE = DL_DIR / "instagram_cookies.txt"

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

# ── instagram cookies ──
if INSTAGRAM_COOKIES_B64:
    try:
        raw = base64.b64decode(INSTAGRAM_COOKIES_B64)
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("# Netscape"):
            text = "# Netscape HTTP Cookie File\n\n" + text
        COOKIE_FILE.write_text(text, encoding="utf-8")
        log.info("Instagram cookies: %d bytes, sessionid=%s", len(text), "sessionid" in text)
    except Exception as e:
        log.error("Bad Instagram cookies: %s", e)
        COOKIE_FILE = None
else:
    COOKIE_FILE = None

# ── url detection ──
PATTERNS = {
    "tiktok": [
        r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+",
    ],
    "instagram": [
        r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[\w-]+",
    ],
    "twitter": [
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+",
    ],
    "youtube": [
        r"https?://(?:www\.)?youtube\.com/(?:watch\?v=|shorts/)[\w-]+",
        r"https?://youtu\.be/[\w-]+",
        r"https?://music\.youtube\.com/watch\?v=[\w-]+",
    ],
}

EMOJI = {"tiktok": "\U0001f3b5", "instagram": "\U0001f4f7", "twitter": "\U0001d54f", "youtube": "\u25b6\ufe0f"}

session: aiohttp.ClientSession = None
busy = set()


async def get_session():
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    return session


def find_urls(text):
    found = []
    for platform, pats in PATTERNS.items():
        for pat in pats:
            for m in re.finditer(pat, text, re.IGNORECASE):
                url = m.group(0)
                if url not in found:
                    found.append((platform, url))
    return found


# ── telegram helpers ──

async def tg(method, data, timeout=120):
    s = await get_session()
    async with s.post(f"{API}/{method}", data=data, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        res = await r.json()
        if not res.get("ok"):
            raise Exception(res.get("description", method))
        return res


async def send_text(chat, text, reply=None):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("text", text)
    if reply:
        d.add_field("reply_to_message_id", str(reply))
    return await tg("sendMessage", d, 30)


async def edit_text(chat, mid, text):
    d = aiohttp.FormData()
    d.add_field("chat_id", str(chat))
    d.add_field("message_id", str(mid))
    d.add_field("text", text)
    return await tg("editMessageText", d, 30)


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
    return await tg("sendVideo", d, 180)


# ── ffprobe ──

async def probe(path):
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


# ── download: yt-dlp (reclip style) ──

async def ytdlp_download(url, platform):
    """
    Core download — same as reclip app.py:
      yt-dlp --no-playlist -f bestvideo+bestaudio/best --merge-output-format mp4
    """
    job = os.urandom(5).hex()
    out_dir = DL_DIR / job
    out_dir.mkdir(exist_ok=True)
    tmpl = str(out_dir / f"{job}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", tmpl, "--no-warnings",
           "-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    # instagram: cookies, no proxy
    if platform == "instagram":
        if COOKIE_FILE and COOKIE_FILE.exists():
            cmd += ["--cookies", str(COOKIE_FILE)]

    # twitter: syndication api (fixes guest token error on servers)
    if platform == "twitter":
        cmd += ["--extractor-args", "twitter:api=syndication"]

    # youtube: flexible format + mweb client (bypasses bot check)
    if platform == "youtube":
        cmd = ["yt-dlp", "--no-playlist", "-o", tmpl, "--no-warnings",
               "-f", "bv*+ba/b", "--merge-output-format", "mp4",
               "--extractor-args", "youtube:player_client=mweb,default"]

    # proxy for everything except instagram (proxy IP breaks cookie session)
    if platform != "instagram" and PROXIES:
        cmd += ["--proxy", random.choice(PROXIES)]

    cmd.append(url)
    log.info("[%s] %s", platform, " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), 300)

        if proc.returncode != 0:
            err = stderr.decode().strip().split("\n")[-1] if stderr else "unknown error"
            log.error("[%s] yt-dlp failed: %s", platform, err)
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, err

        # find downloaded file (reclip logic)
        files = glob.glob(str(out_dir / f"{job}.*"))
        if not files:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "No file found after download"

        mp4 = [f for f in files if f.endswith(".mp4")]
        chosen = mp4[0] if mp4 else files[0]
        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        size = os.path.getsize(chosen)
        if size > MAX_TG_SIZE:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, f"Too large ({size // 1048576} MB, limit 50 MB)"

        return chosen, None

    except asyncio.TimeoutError:
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, "Download timed out"
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, str(e)


# ── download: cobalt fallback for youtube ──

async def cobalt_download(url):
    """Fallback for YouTube when yt-dlp fails on server IPs."""
    job = os.urandom(5).hex()
    out_dir = DL_DIR / job
    out_dir.mkdir(exist_ok=True)
    path = str(out_dir / f"{job}.mp4")

    s = await get_session()
    try:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        api_key = os.environ.get("COBALT_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Api-Key {api_key}"

        async with s.post("https://api.cobalt.tools/", json={"url": url, "videoQuality": "720"},
                          headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()

        dl_url = data.get("url")
        if not dl_url:
            log.error("[cobalt] no url: %s", data)
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Cobalt API returned no URL"

        async with s.get(dl_url, timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status != 200:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, f"Cobalt download HTTP {r.status}"
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)

        size = os.path.getsize(path)
        if size < 1000:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, "Cobalt returned empty file"
        if size > MAX_TG_SIZE:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None, f"Too large ({size // 1048576} MB, limit 50 MB)"

        log.info("[cobalt] OK: %d bytes", size)
        return path, None

    except Exception as e:
        log.error("[cobalt] error: %s", e)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, str(e)


# ── get title ──

async def get_title(url, platform):
    try:
        cmd = ["yt-dlp", "--get-title", "--no-warnings", "--no-playlist"]
        if platform == "instagram" and COOKIE_FILE and COOKIE_FILE.exists():
            cmd += ["--cookies", str(COOKIE_FILE)]
        if platform == "twitter":
            cmd += ["--extractor-args", "twitter:api=syndication"]
        if platform == "youtube":
            cmd += ["--extractor-args", "youtube:player_client=mweb,default"]
        if platform != "instagram" and PROXIES:
            cmd += ["--proxy", random.choice(PROXIES)]
        cmd.append(url)
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 15)
        t = out.decode().strip()[:100]
        return t if t else "Video"
    except Exception:
        return "Video"


# ── main download router ──

async def download(url, platform):
    """Try yt-dlp first. For YouTube, fall back to cobalt if yt-dlp fails."""
    path, err = await ytdlp_download(url, platform)

    if err and platform == "youtube":
        log.info("[youtube] yt-dlp failed, trying cobalt: %s", url)
        path, err = await cobalt_download(url)

    return path, err


# ── handle message ──

async def handle(update):
    msg = update.get("message", {})
    text = msg.get("text", "")
    chat = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    if not chat or not text:
        return

    if text.startswith("/start"):
        await send_text(chat, (
            "\U0001f3ac Video Downloader\n\n"
            "Send a link from:\n"
            "\u2022 TikTok\n"
            "\u2022 Instagram\n"
            "\u2022 YouTube\n"
            "\u2022 X / Twitter\n\n"
            "Works in groups \u2014 just drop a link."
        ), mid)
        return

    urls = find_urls(text)
    if not urls:
        return

    for platform, url in urls[:3]:
        key = hash(url)
        if key in busy:
            continue
        busy.add(key)

        try:
            emoji = EMOJI.get(platform, "\U0001f3ac")

            # status message
            res = await send_text(chat, f"{emoji} Downloading\u2026", mid)
            sid = res.get("result", {}).get("message_id")

            # download
            path, err = await download(url, platform)

            if err:
                if sid:
                    await edit_text(chat, sid, f"\u274c {err[:300]}")
                    asyncio.create_task(_delete_later(chat, sid, 15))
                continue

            # get metadata
            title = await get_title(url, platform)
            dur, w, h = await probe(path)
            size_mb = os.path.getsize(path) / 1048576

            # upload
            try:
                if sid:
                    await edit_text(chat, sid, "\u2b06\ufe0f Uploading\u2026")
                await send_video(chat, path, f"{emoji} {title}\n{size_mb:.1f} MB", mid, dur, w, h)
                if sid:
                    await delete_msg(chat, sid)
            except Exception as e:
                log.error("Upload error: %s", e)
                if sid:
                    await edit_text(chat, sid, "\u274c Upload failed")
                    asyncio.create_task(_delete_later(chat, sid, 15))
            finally:
                shutil.rmtree(Path(path).parent, ignore_errors=True)

        finally:
            busy.discard(key)


async def _delete_later(chat, mid, delay):
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
                if d.is_dir() and time.time() - d.stat().st_mtime > 300:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


async def main():
    log.info("Bot starting")
    asyncio.create_task(cleanup())
    await poll()


if __name__ == "__main__":
    asyncio.run(main())
