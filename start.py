import subprocess
import sys

print("[start] Upgrading yt-dlp to latest...", flush=True)
subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"], check=False)

try:
    import yt_dlp
    print(f"[start] yt-dlp version: {yt_dlp.version.__version__}", flush=True)
except Exception:
    print("[start] yt-dlp version: unknown", flush=True)

print("[start] Starting bot...", flush=True)
import bot
import asyncio
asyncio.run(bot.main())
