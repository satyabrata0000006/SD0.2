import os
import time
import tempfile
import threading
import uuid
import traceback
import subprocess
import shlex
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template, send_from_directory
import yt_dlp
import base64

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
TASKS = {}
TASK_LOCK = threading.Lock()

# ---------------- Cookie Setup ----------------
def write_text_file(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(data)

def ensure_cookies_from_env_once() -> str | None:
    """Restore cookies.txt from ENV or COOKIES_URL"""
    try:
        target = Path("/tmp/cookies.txt")
        b64 = os.environ.get("YTDLP_COOKIES_B64")
        raw = os.environ.get("YTDLP_COOKIES")

        if b64:
            data = base64.b64decode(b64)
            write_text_file(target, data)
            app.logger.info("✅ Cookies.txt restored from YTDLP_COOKIES_B64 → /tmp/cookies.txt")
            return str(target)
        elif raw:
            write_text_file(target, raw)
            app.logger.info("✅ Cookies.txt restored from YTDLP_COOKIES → /tmp/cookies.txt")
            return str(target)
        else:
            url = os.environ.get("COOKIES_URL")
            if url:
                import urllib.request
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = r.read()
                    if data and len(data) > 10:
                        write_text_file(target, data)
                        app.logger.info("✅ Cookies.txt downloaded from COOKIES_URL → /tmp/cookies.txt")
                        return str(target)
            return None
    except Exception as e:
        app.logger.error(f"Cookie setup failed: {e}")
        return None

ensure_cookies_from_env_once()

# ---------------- yt-dlp Options ----------------
def prepare_yt_dlp_opts(cookiefile=None, output_template=None, progress_hook=None,
                        format_override=None, audio_convert=None):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; SM-G991B Build/SP1A.210812.016) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.6613.84 Mobile Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    opts = {
        "format": format_override or "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": output_template or str(DOWNLOAD_DIR / "%(title)s - %(id)s.%(ext)s"),
        "http_headers": headers,
        "retries": 5,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],  # Android fallback avoids bot-check
                "skip": ["hls_dash"]
            }
        }
    }

    proxy = os.environ.get("YTDLP_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        opts["proxy"] = proxy

    # Choose correct cookie file
    if cookiefile:
        opts["cookiefile"] = cookiefile
    else:
        tmp_cookie = Path("/tmp/cookies.txt")
        if tmp_cookie.exists() and tmp_cookie.stat().st_size > 10:
            opts["cookiefile"] = str(tmp_cookie)
        else:
            local_cookie = BASE_DIR / "cookies.txt"
            if local_cookie.exists():
                opts["cookiefile"] = str(local_cookie)

    if audio_convert:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_convert.get("codec", "mp3"),
            "preferredquality": str(audio_convert.get("quality", 192))
        }]

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    return opts

def ydl_extract_info(url, opts, download=False):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
            filename = ydl.prepare_filename(info) if download else None
            return {"ok": True, "info": info, "filename": filename}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------- Utility ----------------
def _cookie_path_candidates():
    return [Path("/tmp/cookies.txt"), BASE_DIR / "cookies.txt", Path("/mnt/data/cookies.txt")]

@app.route("/cookie_status", methods=["GET"])
def cookie_status():
    out = []
    for p in _cookie_path_candidates():
        try:
            if p.exists():
                out.append({"path": str(p), "exists": True, "size": p.stat().st_size})
            else:
                out.append({"path": str(p), "exists": False, "size": 0})
        except Exception as e:
            out.append({"path": str(p), "error": str(e)})
    return jsonify({"ok": True, "candidates": out})

@app.route("/default_cookies", methods=["GET"])
def default_cookies_route_strict():
    for p in _cookie_path_candidates():
        if p.exists() and p.is_file():
            sz = p.stat().st_size
            if sz >= 10:
                return send_file(str(p), mimetype="text/plain")
    return ("", 404)

# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/info", methods=["POST"])
def info():
    url = request.form.get("url")
    if not url:
        return jsonify({"ok": False, "error": "URL missing"}), 400

    cookiefile = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile = tmp.name

    try:
        opts = prepare_yt_dlp_opts(cookiefile=cookiefile)
        result = ydl_extract_info(url, opts, download=False)
    finally:
        if cookiefile:
            try:
                os.unlink(cookiefile)
            except:
                pass

    if result["ok"]:
        info = result["info"]
        return jsonify({
            "ok": True,
            "id": info.get("id"),
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
