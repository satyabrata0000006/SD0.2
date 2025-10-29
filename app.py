import os
import time
import tempfile
import threading
import uuid
import traceback
import shlex
import subprocess
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


# ---------------- Utility ----------------
def write_text_file(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(data)


# ---------------- Auto Cookie Setup ----------------
def ensure_cookies_from_env_once() -> str | None:
    """Restore cookies.txt from ENV if present"""
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
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        data = r.read()
                        if data and len(data) > 10:
                            write_text_file(target, data)
                            app.logger.info("✅ Cookies.txt downloaded from COOKIES_URL → /tmp/cookies.txt")
                            return str(target)
                except Exception as e:
                    app.logger.warning(f"Failed to download cookies.txt: {e}")
            return None
    except Exception as e:
        app.logger.error(f"Cookie setup failed: {e}")
        return None


# Run cookie restore at startup
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

    # Proxy support
    proxy = os.environ.get("YTDLP_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        opts["proxy"] = proxy

    # Use uploaded or default cookies
    if cookiefile:
        opts["cookiefile"] = cookiefile
    else:
        tmp_cookie = Path("/tmp/cookies.txt")
        if tmp_cookie.exists():
            opts["cookiefile"] = str(tmp_cookie)
        else:
            local_cookie = BASE_DIR / "cookies.txt"
            if local_cookie.exists():
                opts["cookiefile"] = str(local_cookie)

    # Audio extract option
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


# ---------------- Flask Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/default_cookies")
def default_cookies():
    path = Path("/tmp/cookies.txt")
    if path.exists():
        return send_file(str(path), mimetype="text/plain")
    local = BASE_DIR / "cookies.txt"
    if local.exists():
        return send_file(str(local), mimetype="text/plain")
    return ("", 404)


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
            "duration": info.get("duration"),
            "formats": info.get("formats", [])
        })
    else:
        err = result.get("error", "")
        hint = None
        if "Sign in to confirm" in err or "bot" in err or "403" in err:
            hint = "YouTube bot-check: please update cookies or verify account."
        return jsonify({"ok": False, "error": err, "hint": hint}), 422


@app.route("/download", methods=["POST"])
def download():
    url = request.form.get("url")
    requested = request.form.get("requested", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL missing"}), 400

    cookiefile = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile = tmp.name

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "running", "progress": "0%", "messages": []}

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            pct = int(done * 100 / total) if total else 0
            TASKS[task_id]["progress"] = f"{pct}%"
        elif d.get("status") == "finished":
            TASKS[task_id]["progress"] = "100%"
            TASKS[task_id]["status"] = "processing"

    def worker():
        try:
            fmt = None
            audio_convert = None
            if requested.startswith("audio:"):
                audio_convert = {"codec": requested.split(":", 1)[1], "quality": 192}
                fmt = "bestaudio/best"
            elif requested:
                fmt = requested

            opts = prepare_yt_dlp_opts(cookiefile=cookiefile, progress_hook=progress_hook,
                                       format_override=fmt, audio_convert=audio_convert)

            result = ydl_extract_info(url, opts, download=True)
            if result["ok"]:
                filename = Path(result["filename"]).name
                TASKS[task_id].update({"status": "done", "filename": filename})
            else:
                TASKS[task_id].update({"status": "error", "error": result["error"]})
        except Exception as e:
            TASKS[task_id].update({"status": "error", "error": str(e)})
        finally:
            if cookiefile:
                try:
                    os.unlink(cookiefile)
                except:
                    pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/task/<tid>")
def task_status(tid):
    t = TASKS.get(tid)
    if not t:
        return jsonify({"ok": False, "error": "Task not found"}), 404
    return jsonify({"ok": True, "task": t})


@app.route("/download_file/<filename>")
def serve_file(filename):
    f = DOWNLOAD_DIR / Path(filename).name
    if f.exists():
        return send_file(str(f), as_attachment=True)
    return jsonify({"ok": False, "error": "File not found"}), 404


# ---------------- Error Handlers ----------------
@app.errorhandler(404)
def nf(e):
    return jsonify({"ok": False, "error": "not_found"}), 404

@app.errorhandler(500)
def err(e):
    tb = traceback.format_exc()
    app.logger.error(f"Server Error: {tb}")
    return jsonify({"ok": False, "error": "internal_error", "trace": tb}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Server started at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port)
