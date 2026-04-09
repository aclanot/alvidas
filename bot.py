import os
import sys
import re
import uuid
import glob
import json
import base64
import subprocess
import threading
import tempfile
import asyncio
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Configuration from environment variables
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
INSTAGRAM_COOKIES_BASE64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit
DOWNLOAD_DIR = tempfile.gettempdir()

# Store active jobs
jobs = {}


def get_cookies_path():
    """Decode base64 cookies to temporary file if provided"""
    if not INSTAGRAM_COOKIES_BASE64:
        return None
    try:
        cookies_content = base64.b64decode(INSTAGRAM_COOKIES_BASE64).decode('utf-8')
        cookies_path = os.path.join(tempfile.gettempdir(), 'cookies.txt')
        with open(cookies_path, 'w') as f:
            f.write(cookies_content)
        return cookies_path
    except Exception as e:
        print(f"Error decoding cookies: {e}")
        return None


def detect_platform(url):
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
    elif 'reddit' in domain:
        return 'reddit'
    else:
        return 'generic'


def get_video_info(url, cookies_path=None):
    """Get video info using yt-dlp (from reclip logic)"""
    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    
    if cookies_path and detect_platform(url) == 'instagram':
        cmd.extend(["--cookies", cookies_path])
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None, result.stderr.strip().split("\n")[-1]
        
        info = json.loads(result.stdout)
        return info, None
    except subprocess.TimeoutExpired:
        return None, "Timed out fetching video info"
    except Exception as e:
        return None, str(e)


def run_download(job_id, url, format_choice, cookies_path=None):
    """Download video using reclip logic"""
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    
    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]
    
    # Add cookies for Instagram
    if cookies_path and detect_platform(url) == 'instagram':
        cmd.extend(["--cookies", cookies_path])
    
    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    else:
        # Best quality video
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
    
    # Add user-agent to avoid blocks
    cmd += ["--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    
    cmd.append(url)
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return
        
        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return
        
        # Pick the right file
        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]
        
        # Clean up extra files
        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass
        
        job["status"] = "done"
        job["file"] = chosen
        job["file_size"] = os.path.getsize(chosen)
        
        # Generate filename
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:30]
            job["filename"] = f"{safe_title}{ext}" if safe_title else f"{job_id}{ext}"
        else:
            job["filename"] = f"{job_id}{ext}"
            
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    welcome_text = """
🎥 *Video Downloader Bot*

Send me links from:
• TikTok
• Instagram (with cookies)
• YouTube
• X/Twitter
• Facebook
• Reddit
• And 1000+ more sites!

Just paste the link and I'll download the video for you.
    """
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = """
*How to use:*

1. Send any video link
2. Wait for download
3. Receive the video file

*Supported platforms:*
• TikTok
• Instagram (requires cookies setup)
• YouTube
• X/Twitter
• Facebook
• Reddit
• And many more!

*Instagram Setup:*
For private Instagram content, set INSTAGRAM_COOKIES_BASE64 environment variable with base64-encoded cookies.txt content.
    """
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming URL messages"""
    message = update.message
    chat_id = update.effective_chat.id
    
    # Get text - handle both direct messages and group mentions
    text = message.text or ""
    
    # In group chats, check if bot was mentioned or if message is a URL
    bot_username = context.bot.username
    if message.chat.type in ['group', 'supergroup']:
        # Remove bot mention from message if present
        text = text.replace(f"@{bot_username}", "").strip()
        # Also handle other mentions that might be in the message
        text = re.sub(r'@\w+\s*', '', text).strip()
    
    url = text.strip()
    
    # Basic URL validation
    if not url.startswith(('http://', 'https://')):
        # Only respond in private chats or when explicitly mentioned in groups
        if message.chat.type == 'private':
            await update.message.reply_text("❌ Please send a valid URL starting with http:// or https://")
        return
    
    # Show processing message
    processing_msg = await update.message.reply_text("🔍 Analyzing link...")
    
    # Get cookies path if available
    cookies_path = get_cookies_path()
    
    # Get video info
    info, error = get_video_info(url, cookies_path)
    
    if error:
        await processing_msg.edit_text(f"❌ Error: {error}")
        return
    
    if not info:
        await processing_msg.edit_text("❌ Could not fetch video information")
        return
    
    # Extract info
    title = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    uploader = info.get("uploader", "Unknown")
    platform = detect_platform(url)
    
    # Create job
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "chat_id": chat_id,
        "message_id": processing_msg.message_id
    }
    
    # Show video info and start download
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "Unknown"
    
    info_text = f"""
🎬 *{title[:100]}*
👤 {uploader}
⏱ Duration: {duration_str}
📱 Platform: {platform.capitalize()}

⬇️ Downloading... Please wait.
    """
    
    await processing_msg.edit_text(info_text, parse_mode="Markdown")
    
    # Start download in background thread
    def download_callback():
        run_download(job_id, url, "video", cookies_path)
        # Schedule upload via asyncio
        asyncio.run_coroutine_threadsafe(
            upload_video(job_id, context),
            asyncio.get_event_loop()
        )
    
    thread = threading.Thread(target=download_callback)
    thread.daemon = True
    thread.start()


async def upload_video(job_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Upload completed video to Telegram"""
    job = jobs.get(job_id)
    if not job:
        return
    
    chat_id = job["chat_id"]
    message_id = job["message_id"]
    
    if job["status"] == "error":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ Download failed: {job.get('error', 'Unknown error')}"
        )
        return
    
    if job["status"] != "done":
        return
    
    file_path = job["file"]
    file_size = job.get("file_size", 0)
    filename = job["filename"]
    
    # Check file size
    if file_size > MAX_FILE_SIZE:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ File too large ({file_size / 1024 / 1024:.1f}MB). Telegram limit is 50MB.\n\nVideo title: {job.get('title', 'Unknown')}"
        )
        # Clean up
        try:
            os.remove(file_path)
        except:
            pass
        return
    
    # Update message
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=f"✅ Download complete! Uploading to Telegram..."
    )
    
    try:
        # Send video
        with open(file_path, 'rb') as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=f"🎬 {job.get('title', 'Video')[:200]}",
                supports_streaming=True,
                filename=filename
            )
        
        # Delete the processing message
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ Upload failed: {str(e)}"
        )
    finally:
        # Clean up file
        try:
            os.remove(file_path)
        except:
            pass
        # Clean up job
        jobs.pop(job_id, None)


async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to check active downloads"""
    if not jobs:
        await update.message.reply_text("No active downloads")
        return
    
    status_text = "Active downloads:\n"
    for job_id, job in jobs.items():
        status_text += f"• {job_id}: {job['status']} - {job.get('title', 'Unknown')[:30]}\n"
    
    await update.message.reply_text(status_text)


def main():
    """Start the bot"""
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set")
        sys.exit(1)
    
    # Check if yt-dlp is installed
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        print(f"yt-dlp version: {result.stdout.strip()}")
    except FileNotFoundError:
        print("Error: yt-dlp not found. Please install yt-dlp.")
        sys.exit(1)
    
    # Check cookies configuration
    if INSTAGRAM_COOKIES_BASE64:
        print("✅ Instagram cookies configured")
    else:
        print("⚠️ No Instagram cookies configured (set INSTAGRAM_COOKIES_BASE64 env var)")
    
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", check_status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_url))
    
    # Run the bot
    print("🤖 Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
