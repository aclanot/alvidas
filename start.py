import subprocess, sys

print("[start] Upgrading yt-dlp...", flush=True)
subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"], check=False)

try:
    import yt_dlp
    print(f"[start] yt-dlp {yt_dlp.version.__version__}", flush=True)
except Exception:
    print("[start] yt-dlp: unknown version", flush=True)

print("[start] Starting bot...", flush=True)
import bot, asyncio
asyncio.run(bot.main())
