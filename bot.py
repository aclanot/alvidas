import os
import sys
import re
import hashlib
import json
import base64
import glob
import subprocess
import asyncio
import logging
import tempfile
import shutil
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
TIKTOK_COOKIES_BASE64 = os.environ.get("TIKTOK_COOKIES_BASE64", "")
TWITTER_COOKIES_BASE64 = os.environ.get("TWITTER_COOKIES_BASE64", "")

TELEGRAM_MAX_SIZE = 50 * 1024 * 1024
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "bot_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

http_session: aiohttp.ClientSession = None
processing = set()

# URL patterns for supported platforms
URL_PATTERNS = [
    # TikTok
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/[@\w./]+",
    r"https?://(?:www\.)?tiktok\.com/@[\w.]+/video/\d+",
    r"https?://(?:www\.)?tiktok\.com/@[\w.]+/photo/\d+",
    # YouTube
    r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+",
    r"https?://(?:www\.)?youtu\.be/[\w-]+",
    r"https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+",
    r"https?://music\.youtube\.com/watch\?v=[\w-]+",
    # Twitter/X
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+",
    # Instagram
    r"https?://(?:www\.)?instagram\.com/reel/[\w-]+",
    r"https?://(?:www\.)?instagram\.com/reels/[\w-]+",
    r"https?://(?:www\.)?instagram\.com/p/[\w-]+",
    r"https?://(?:www\.)?instagram\.com/tv/[\w-]+",
    # Facebook
    r"https?://(?:www\.)?facebook\.com/(?:watch/\?v=|share/v/|reel/)[\w-]+",
    r"https?://(?:www\.)?fb\.watch/[\w-]+",
    # Reddit
    r"https?://(?:www\.)?reddit\.com/r/\w+/comments/\w+/",
    r"https?://(?:www\.)?redd\.it/\w+",
]

PLATFORM_EMOJI = {
    "tiktok": "🎵",
    "youtube": "▶️",
    "twitter": "𝕏",
    "instagram": "📷",
    "facebook": "📘",
    "reddit": "🤖",
    "unknown": "🎬",
}


def get_cookies_path(platform: str) -> Path | None:
    """Get cookies file path for platform if configured via base64 env var"""
    env_map = {
        "instagram": INSTAGRAM_COOKIES_BASE64,
        "tiktok": TIKTOK_COOKIES_BASE64,
        "twitter": TWITTER_COOKIES_BASE64,
    }
    
    base64_cookies = env_map.get(platform, "")
    if not base64_cookies:
        return None
    
    try:
        cookies_content = base64.b64decode(base64_cookies).decode('utf-8')
        cookies_path = DOWNLOAD_DIR / f"{platform}_cookies.txt"
        cookies_path.write_text(cookies_content)
        return cookies_path
    except Exception as e:
        logger.error(f"Error decoding cookies for {platform}: {e}")
        return None


def detect_platform(url: str) -> str:
    """Detect which platform the URL belongs to"""
    domain = urlparse(url).netloc.lower()
    
    if 'tiktok' in domain:
        return 'tiktok'
    elif 'instagram' in domain:
        return 'instagram'
    elif 'youtube' in domain or 'youtu.be' in domain:
        return 'youtube'
    elif 'twitter' in domain or 'x.com' in domain:
        return 'twitter'
    elif 'facebook' in domain or 'fb.watch' in domain:
        return 'facebook'
    elif 'reddit' in domain or 'redd.it' in domain:
        return 'reddit'
    else:
        return 'unknown'


def extract_urls(text: str) -> list:
    """Extract video URLs from text"""
    urls = []
    for pattern in URL_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = match.group(0)
            if url not in urls:
                urls.append(url)
    return urls


async def get_session() -> aiohttp.ClientSession:
    """Get or create HTTP session"""
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session


async def bot_api_call(method: str, data: aiohttp.FormData, timeout: int = 120):
    """Make raw Telegram Bot API call"""
    session = await get_session()
    async with session.post(f"{BOT_API_URL}/{method}", data=data, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        result = await resp.json()
        if not result.get("ok"):
            raise Exception(result.get("description", f"Bot API {method} failed"))
        return result


async def bot_api_send_message(chat_id: int, text: str, reply_to: int = None):
    """Send text message via Bot API"""
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("text", text)
    if reply_to:
        data.add_field("reply_to_message_id", str(reply_to))
    return await bot_api_call("sendMessage", data, timeout=30)


async def bot_api_edit_message(chat_id: int, message_id: int, text: str):
    """Edit message text via Bot API"""
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("message_id", str(message_id))
    data.add_field("text", text)
    return await bot_api_call("editMessageText", data, timeout=30)


async def bot_api_delete_message(chat_id: int, message_id: int):
    """Delete message via Bot API"""
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("message_id", str(message_id))
    try:
        return await bot_api_call("deleteMessage", data, timeout=30)
    except Exception:
        pass


async def bot_api_send_video(chat_id: int, file_path: str, caption: str, reply_to: int,
                             duration: int = 0, width: int = 0, height: int = 0):
    """Send video via Bot API"""
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("video", open(file_path, "rb"), filename="video.mp4", content_type="video/mp4")
    if caption:
        data.add_field("caption", caption[:1024])
    if reply_to:
        data.add_field("reply_to_message_id", str(reply_to))
    if duration:
        data.add_field("duration", str(duration))
    if width:
        data.add_field("width", str(width))
    if height:
        data.add_field("height", str(height))
    data.add_field("supports_streaming", "true")
    return await bot_api_call("sendVideo", data, timeout=120)


async def delete_later(chat_id: int, message_id: int, delay: int = 10):
    """Delete message after delay"""
    await asyncio.sleep(delay)
    await bot_api_delete_message(chat_id, message_id)


# ============= RECLIP DOWNLOAD LOGIC =============

async def get_video_info(url: str, cookies_path: Path = None) -> tuple:
    """Get video info using yt-dlp (from reclip logic)"""
    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    
    if cookies_path:
        cmd.extend(["--cookies", str(cookies_path)])
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        
        if proc.returncode != 0:
            return None, stderr.decode().strip().split("\n")[-1]
        
        info = json.loads(stdout.decode())
        return info, None
    except asyncio.TimeoutError:
        return None, "Timed out fetching video info"
    except Exception as e:
        return None, str(e)


async def download_with_reclip(url: str, platform: str) -> tuple:
    """
    Download video using reclip's yt-dlp logic
    Returns: (file_path, title, error)
    """
    job_id = os.urandom(4).hex()
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(exist_ok=True)
    
    out_template = str(output_dir / "video.%(ext)s")
    final_path = str(output_dir / "video_final.mp4")
    
    # Get cookies for this platform
    cookies_path = get_cookies_path(platform)
    
    # Build yt-dlp command (based on reclip app.py)
    cmd = ["yt-dlp", "--no-playlist", "-o", out_template, "--no-warnings"]
    
    if cookies_path:
        cmd.extend(["--cookies", str(cookies_path)])
    
    # Platform-specific format selection
    if platform == "tiktok":
        # Prefer H.264 codec for compatibility
        cmd.extend(["-f", "bestvideo[vcodec^=avc1]+bestaudio/bestvideo[vcodec^=avc1]/best[vcodec^=avc1]"])
        cmd.extend(["--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64)"])
        cmd.extend(["--merge-output-format", "mp4"])
    elif platform == "twitter":
        cmd.extend(["-f", "best[vcodec^=avc1][height<=720]/best[vcodec^=avc1]/best[height<=720]/best"])
    elif platform == "instagram":
        cmd.extend(["-f", "best[vcodec^=avc1]/best"])
    else:
        cmd.extend(["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"])
    
    # User agent for all platforms
    cmd.extend(["--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"])
    
    cmd.append(url)
    
    try:
        logger.info(f"Downloading: {url}")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        
        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            logger.error(f"yt-dlp error: {error_msg[:500]}")
            shutil.rmtree(output_dir, ignore_errors=True)
            return None, None, f"❌ Download failed: {error_msg.strip().split(chr(10))[-1][:200]}"
        
        # Find downloaded files
        files = list(output_dir.glob("video.*"))
        if not files:
            shutil.rmtree(output_dir, ignore_errors=True)
            return None, None, "❌ No media found"
        
        # Select best file (prefer mp4)
        mp4_files = [f for f in files if f.suffix.lower() == ".mp4"]
        chosen = mp4_files[0] if mp4_files else files[0]
        
        # Move to final path
        shutil.move(str(chosen), final_path)
        
        # Clean up extra files
        for f in files:
            if f.exists():
                try:
                    f.unlink()
                except:
                    pass
        
        # Get file size
        file_size = Path(final_path).stat().st_size
        
        # Check Telegram size limit
        if file_size > TELEGRAM_MAX_SIZE:
            shutil.rmtree(output_dir, ignore_errors=True)
            return None, None, f"❌ Too large ({file_size // 1024 // 1024} MB). Telegram limit is 50 MB"
        
        # Get video metadata
        duration, width, height = await get_video_metadata(final_path)
        
        # Get title
        title = "Video"
        try:
            title_cmd = ["yt-dlp", "--get-title", "--no-warnings", url]
            if cookies_path:
                title_cmd.extend(["--cookies", str(cookies_path)])
            tp = await asyncio.create_subprocess_exec(
                *title_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            tout, _ = await asyncio.wait_for(tp.communicate(), timeout=10)
            title = tout.decode().strip()[:100] if tout else "Video"
        except:
            pass
        
        return final_path, title, (duration, width, height)
        
    except asyncio.TimeoutError:
        shutil.rmtree(output_dir, ignore_errors=True)
        return None, None, "❌ Download timed out"
    except Exception as e:
        logger.error(f"Download error: {e}")
        shutil.rmtree(output_dir, ignore_errors=True)
        return None, None, f"❌ Download failed: {str(e)}"


async def get_video_metadata(video_path: str):
    """Get video metadata using ffprobe"""
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", video_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(stdout.decode())
        
        duration = int(float(data.get("format", {}).get("duration", 0)))
        width, height = 0, 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = stream.get("width", 0)
                height = stream.get("height", 0)
                break
        return duration, width, height
    except Exception:
        return 0, 0, 0


def cleanup_media(file_path: str):
    """Clean up downloaded media and temp directory"""
    try:
        if file_path:
            dir_path = Path(file_path).parent
            shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


async def cleanup_old_files():
    """Clean up old download directories"""
    import time
    try:
        for item in DOWNLOAD_DIR.iterdir():
            if item.is_dir() and time.time() - item.stat().st_mtime > 300:
                shutil.rmtree(item, ignore_errors=True)
    except Exception:
        pass


# ============= BOT HANDLERS =============

async def handle_update(update: dict):
    """Handle incoming Telegram update"""
    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    
    if not chat_id or not text:
        return
    
    # Handle /start command
    if text.startswith("/start"):
        await bot_api_send_message(
            chat_id,
            "🎬 Video Downloader Bot\n\n"
            "Send a link from:\n"
            "• TikTok\n"
            "• Instagram (with cookies)\n"
            "• YouTube\n"
            "• X / Twitter\n"
            "• Facebook\n"
            "• Reddit\n\n"
            "Works in groups — just drop a link.",
            reply_to=msg_id,
        )
        return
    
    # Extract URLs from message
    urls = extract_urls(text)
    if not urls:
        return  # No URLs found, don't respond
    
    # Process up to 3 URLs
    for url in urls[:3]:
        url_hash = hash(url)
        if url_hash in processing:
            continue
        processing.add(url_hash)
        
        try:
            platform = detect_platform(url)
            emoji = PLATFORM_EMOJI.get(platform, "🎬")
            
            # Send status message
            status_result = await bot_api_send_message(chat_id, f"{emoji} Downloading…", reply_to=msg_id)
            status_id = status_result.get("result", {}).get("message_id")
            
            # Download using reclip logic
            file_path, title, metadata = await download_with_reclip(url, platform)
            
            if not file_path:
                # Download failed
                if status_id:
                    await bot_api_edit_message(chat_id, status_id, metadata)
                    asyncio.create_task(delete_later(chat_id, status_id, 15))
                continue
            
            # Success - send the video
            try:
                if status_id:
                    await bot_api_edit_message(chat_id, status_id, "⬆️ Uploading…")
                
                duration, width, height = metadata
                size_mb = Path(file_path).stat().st_size / 1024 / 1024
                caption = f"{emoji} {title}\n{size_mb:.1f} MB"
                
                await bot_api_send_video(
                    chat_id, file_path, caption, msg_id,
                    duration=duration, width=width, height=height
                )
                
                # Delete status message after successful upload
                if status_id:
                    await bot_api_delete_message(chat_id, status_id)
                    
            except Exception as e:
                logger.error(f"Upload error: {e}")
                if status_id:
                    await bot_api_edit_message(chat_id, status_id, "❌ Upload failed")
                    asyncio.create_task(delete_later(chat_id, status_id, 10))
            finally:
                cleanup_media(file_path)
                
        finally:
            processing.discard(url_hash)


async def polling_loop():
    """Main polling loop for updates"""
    offset = 0
    session = await get_session()
    logger.info("Bot polling started")
    
    while True:
        try:
            url = f"{BOT_API_URL}/getUpdates?offset={offset}&timeout=30"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                data = await resp.json()
            
            if not data.get("ok"):
                logger.error(f"getUpdates error: {data}")
                await asyncio.sleep(5)
                continue
            
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                asyncio.create_task(handle_update(update))
                
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)


async def cleanup_loop():
    """Background cleanup of old files"""
    while True:
        await asyncio.sleep(300)
        await cleanup_old_files()


async def main():
    """Main entry point"""
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable")
    
    # Check yt-dlp
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        logger.info(f"yt-dlp version: {result.stdout.strip()}")
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found. Please install yt-dlp.")
    
    # Log cookie status
    if INSTAGRAM_COOKIES_BASE64:
        logger.info("✅ Instagram cookies configured")
    if TIKTOK_COOKIES_BASE64:
        logger.info("✅ TikTok cookies configured")
    if TWITTER_COOKIES_BASE64:
        logger.info("✅ Twitter cookies configured")
    
    logger.info("Bot starting (Raw Bot API, 50 MB limit)")
    
    asyncio.create_task(cleanup_loop())
    await polling_loop()


if __name__ == "__main__":
    asyncio.run(main())
