def download_media(url):
    # use yt-dlp to download media
    import yt_dlp
    ydl_opts = {}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return True