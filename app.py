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

# ---------- App setup ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

TASKS = {}
TASK_LOCK = threading.Lock()

# ---------- Utilities ----------
def add_task_message(task_id, text):
    t = TASKS.get(task_id)
    if not t:
        return
    msgs = t.setdefault("messages", [])
    msgs.append({"ts": int(time.time()), "text": text})

def run_cmd(cmd_list, timeout=None):
    try:
        proc = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)
        return proc.returncode, proc.stdout.decode(errors="ignore"), proc.stderr.decode(errors="ignore")
    except Exception as e:
        return 1, "", str(e)

# ---------- Cookies auto-manage ----------
def write_text_file(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(data)

def ensure_cookies_from_env_once() -> str | None:
    """
    Re-create cookies.txt next to app.py from ENV if provided.
    Supports:
      - YTDLP_COOKIES_B64  (base64 of full Netscape cookies.txt)
      - YTDLP_COOKIES      (raw Netscape cookies content)
      - COOKIES_URL        (fallback; downloaded by background thread)
    Returns path string if created, else None.
    """
    try:
        target = BASE_DIR / "cookies.txt"

        b64 = os.environ.get("YTDLP_COOKIES_B64")
        raw = os.environ.get("YTDLP_COOKIES")

        if b64:
            data = base64.b64decode(b64)
            write_text_file(target, data)
            app.logger.info("✅ cookies.txt restored from YTDLP_COOKIES_B64")
            return str(target)

        if raw:
            write_text_file(target, raw)
            app.logger.info("✅ cookies.txt restored from YTDLP_COOKIES")
            return str(target)

        # If neither present, do nothing here. COOKIES_URL will be handled by refresher thread.
        return None
    except Exception as e:
        app.logger.error(f"Failed to restore cookies from ENV: {e}")
        return None

def _download_text(url: str, timeout: int = 12) -> str | None:
    try:
        # Use Python's urllib to avoid external deps
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ct = resp.headers.get("content-type", "")
            text = resp.read().decode("utf-8", errors="ignore")
            if not text.strip():
                return None
            return text
    except Exception as e:
        app.logger.warning(f"cookies fetch failed from URL: {e}")
        return None

def start_cookies_refresher():
    """
    If COOKIES_URL is set, refresh cookies.txt from that URL at startup and then every 6 hours.
    """
    url = os.environ.get("COOKIES_URL")
    if not url:
        return

    def worker():
        while True:
            try:
                txt = _download_text(url)
                if txt and len(txt.strip()) > 10:  # very small files are likely invalid
                    write_text_file(BASE_DIR / "cookies.txt", txt)
                    app.logger.info("✅ cookies.txt refreshed from COOKIES_URL")
            except Exception as e:
                app.logger.warning(f"cookies refresher error: {e}")
            # Sleep 6 hours
            time.sleep(6 * 60 * 60)

    th = threading.Thread(target=worker, daemon=True)
    th.start()

# Restore from ENV immediately at import time (works with gunicorn)
ensure_cookies_from_env_once()
# Then start URL refresher thread if configured
start_cookies_refresher()

# ---------- yt-dlp helpers ----------
def prepare_yt_dlp_opts(cookiefile=None, output_template=None, progress_hook=None,
                        format_override=None, audio_convert=None):
    headers = {
        # modern UA helps reduce 403
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.6613.84 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    opts = {
        "format": format_override or "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": output_template or str(DOWNLOAD_DIR / "%(title)s - %(id)s.%(ext)s"),
        "http_headers": headers,
        # a few network resiliency knobs
        "retries": 5,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
    }
    proxy = os.environ.get("YTDLP_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        opts["proxy"] = proxy

    if cookiefile:
        opts["cookiefile"] = cookiefile
    else:
        # If no uploaded cookies, use server default if present
        server_cookie = BASE_DIR / "cookies.txt"
        if server_cookie.exists():
            opts["cookiefile"] = str(server_cookie)

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
            return {"ok": True, "info": info, "filename": (ydl.prepare_filename(info) if download else None)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def index():
    try:
        return render_template("index.html")
    except Exception:
        # Fallback to static if templates missing
        static_index = app.static_folder and (Path(app.static_folder) / "index.html")
        if static_index and static_index.exists():
            return send_from_directory(app.static_folder, "index.html")
        return ("<h3>Index not found</h3>", 200)

@app.route("/favicon.ico")
def favicon():
    static_dir = Path(app.static_folder or "static")
    fav = static_dir / "favicon.ico"
    if fav.exists():
        return send_from_directory(str(static_dir), "favicon.ico")
    return ("", 204)

@app.route("/default_cookies", methods=["GET"])
def default_cookies_route():
    """
    Serve cookies.txt from same folder as app.py (used by front-end to auto-attach).
    """
    path = BASE_DIR / "cookies.txt"
    if path.exists() and path.is_file():
        return send_file(str(path), mimetype="text/plain")
    # Also allow /tmp or /mnt/data as fallback if you ever store there
    for p in (Path("/tmp/cookies.txt"), Path("/mnt/data/cookies.txt")):
        if p.exists() and p.is_file():
            return send_file(str(p), mimetype="text/plain")
    return ("", 404)

@app.route("/info", methods=["POST"])
def info_route():
    url = request.form.get("url")
    if not url:
        return jsonify({"ok": False, "error": "url missing"}), 400

    # uploaded cookies take precedence
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
            except Exception:
                pass

    if result.get("ok"):
        info = result["info"]
        return jsonify({
            "ok": True,
            "id": info.get("id"),
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "formats": info.get("formats", []),
        }), 200
    else:
        # Common 403 hint
        err = result.get("error", "")
        hint = None
        if "403" in err or "Forbidden" in err:
            hint = "403 from upstream — ensure valid cookies or set YTDLP_COOKIES_B64 / COOKIES_URL."
        return jsonify({"ok": False, "error": err, "hint": hint}), 422

@app.route("/download", methods=["POST"])
def download_route():
    url = request.form.get("url")
    requested = request.form.get("requested", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url missing"}), 400

    # uploaded cookies take precedence
    cookiefile = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile = tmp.name

    task_id = str(uuid.uuid4())
    with TASK_LOCK:
        TASKS[task_id] = {
            "status": "running", "progress": "0%",
            "messages": [], "created": time.time()
        }
    add_task_message(task_id, "Task started")

    def progress_hook(d):
        try:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes", 0)
                pct = int(done * 100 / total) if total else 0
                TASKS[task_id]["progress"] = f"{pct}%"
            elif d.get("status") == "finished":
                TASKS[task_id]["progress"] = "100%"
                TASKS[task_id]["status"] = "processing"
        except Exception:
            pass

    def worker():
        try:
            fmt = None
            audio_convert = None
            if requested:
                if requested.startswith("audio:"):
                    audio_convert = {"codec": requested.split(":", 1)[1], "quality": 192}
                    fmt = "bestaudio/best"
                else:
                    fmt = requested

            opts = prepare_yt_dlp_opts(cookiefile=cookiefile, progress_hook=progress_hook,
                                       format_override=fmt, audio_convert=audio_convert)

            res = ydl_extract_info(url, opts, download=True)
            if res.get("ok"):
                filename = Path(res["filename"]).name if res.get("filename") else None
                TASKS[task_id].update({"status": "done", "filename": filename})
                add_task_message(task_id, f"Done: {filename or '(unknown)'}")
            else:
                TASKS[task_id].update({"status": "error", "error": res.get("error")})
                add_task_message(task_id, f"Error: {res.get('error')}")
        except Exception as e:
            TASKS[task_id].update({"status": "error", "error": str(e)})
        finally:
            if cookiefile:
                try:
                    os.unlink(cookiefile)
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id}), 200

@app.route("/task/<tid>", methods=["GET"])
def task_status(tid):
    t = TASKS.get(tid)
    if not t:
        return jsonify({"ok": False, "error": "no such task"}), 404
    return jsonify({"ok": True, "task": t}), 200

@app.route("/download_file/<filename>", methods=["GET"])
def serve_file(filename):
    safe = Path(filename).name
    p = DOWNLOAD_DIR / safe
    if p.exists():
        return send_file(str(p), as_attachment=True)
    return jsonify({"ok": False, "error": "file not found"}), 404

# ---------- Global JSON error handlers ----------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"ok": False, "error": "not_found", "detail": str(e)}), 404

@app.errorhandler(500)
def internal_err(e):
    tb = traceback.format_exc()
    app.logger.error("500: %s", tb)
    return jsonify({"ok": False, "error": "internal_server_error", "trace": tb}), 500

# ---------- Local run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Server started at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port)
