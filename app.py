import os
import time
import tempfile
import threading
import uuid
import traceback
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


# ---------------- Cookie helpers ----------------
def write_text_file(path: Path, data: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (bytes, bytearray)):
        with open(path, "wb") as f:
            f.write(data)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

def ensure_cookies_from_env_once() -> str | None:
    """
    Restore cookies.txt into /tmp/cookies.txt from env or COOKIES_URL.
    Return the path if created.
    """
    try:
        target = Path("/tmp/cookies.txt")
        b64 = os.environ.get("YTDLP_COOKIES_B64")
        raw = os.environ.get("YTDLP_COOKIES")
        url = os.environ.get("COOKIES_URL")

        if b64:
            data = base64.b64decode(b64)
            write_text_file(target, data)
            app.logger.info("✅ Cookies from YTDLP_COOKIES_B64 -> /tmp/cookies.txt")
            return str(target)

        if raw:
            write_text_file(target, raw)
            app.logger.info("✅ Cookies from YTDLP_COOKIES -> /tmp/cookies.txt")
            return str(target)

        if url:
            import urllib.request
            try:
                with urllib.request.urlopen(url, timeout=12) as r:
                    data = r.read()
                    if data and len(data) > 10:
                        write_text_file(target, data)
                        app.logger.info("✅ Cookies downloaded from COOKIES_URL -> /tmp/cookies.txt")
                        return str(target)
            except Exception as e:
                app.logger.warning(f"COOKIES_URL fetch failed: {e}")

        return None
    except Exception as e:
        app.logger.error(f"ensure_cookies_from_env_once failed: {e}")
        return None

ensure_cookies_from_env_once()


def cookie_candidates():
    return [Path("/tmp/cookies.txt"), BASE_DIR / "cookies.txt", Path("/mnt/data/cookies.txt")]


# ---------------- yt-dlp helpers ----------------
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
        "extractor_args": {"youtube": {"player_client": ["android", "web"], "skip": ["hls_dash"]}},
    }

    proxy = os.environ.get("YTDLP_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        opts["proxy"] = proxy

    # cookies precedence: uploaded -> /tmp -> local file next to app.py
    if cookiefile:
        opts["cookiefile"] = cookiefile
    else:
        tmp_cookie = Path("/tmp/cookies.txt")
        if tmp_cookie.exists() and tmp_cookie.stat().st_size > 10:
            opts["cookiefile"] = str(tmp_cookie)
        else:
            local_cookie = BASE_DIR / "cookies.txt"
            if local_cookie.exists() and local_cookie.stat().st_size > 10:
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
            fn = ydl.prepare_filename(info) if download else None
            return {"ok": True, "info": info, "filename": fn}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- Diagnostics routes ----------------
@app.route("/cookie_status", methods=["GET"])
def cookie_status():
    rows = []
    for p in cookie_candidates():
        try:
            if p.exists():
                rows.append({"path": str(p), "exists": True, "size": p.stat().st_size})
            else:
                rows.append({"path": str(p), "exists": False, "size": 0})
        except Exception as e:
            rows.append({"path": str(p), "error": str(e)})
    return jsonify({"ok": True, "candidates": rows})


@app.route("/default_cookies", methods=["GET"])
def default_cookies_route():
    for p in cookie_candidates():
        if p.exists() and p.is_file() and p.stat().st_size >= 10:
            return send_file(str(p), mimetype="text/plain")
    return ("", 404)


# ---------------- App routes ----------------
@app.route("/", methods=["GET"])
def index():
    try:
        return render_template("index.html")
    except Exception:
        # Fallback to static index
        static_index = BASE_DIR / "static" / "index.html"
        if static_index.exists():
            return send_from_directory(str(static_index.parent), static_index.name)
        return ("<h3>Index not found</h3>", 200)

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
        err = result.get("error", "")
        hint = None
        if ("Sign in to confirm" in err) or ("bot" in err) or ("403" in err):
            hint = "YouTube bot-check; update cookies (YTDLP_COOKIES_B64) or verify the account. Proxy may help."
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
    with TASK_LOCK:
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
            res = ydl_extract_info(url, opts, download=True)
            if res.get("ok"):
                fn = Path(res["filename"]).name if res.get("filename") else None
                TASKS[task_id].update({"status": "done", "filename": fn})
            else:
                TASKS[task_id].update({"status": "error", "error": res.get("error")})
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
        return jsonify({"ok": False, "error": "Task not found"}), 404
    return jsonify({"ok": True, "task": t}), 200


@app.route("/download_file/<filename>", methods=["GET"])
def serve_file(filename):
    p = DOWNLOAD_DIR / Path(filename).name
    if p.exists():
        return send_file(str(p), as_attachment=True)
    return jsonify({"ok": False, "error": "File not found"}), 404


# ---------------- Error handlers ----------------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"ok": False, "error": "not_found"}), 404

@app.errorhandler(500)
def server_error(e):
    tb = traceback.format_exc()
    app.logger.error(tb)
    return jsonify({"ok": False, "error": "internal_server_error", "trace": tb}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Server started at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port)
