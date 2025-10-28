import os
import time
import tempfile
import threading
import uuid
import traceback
import socket
import shlex
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template, send_from_directory
import yt_dlp
import html
import json
import base64

try:
    import browser_cookie3
    BROWSER_COOKIE3_AVAILABLE = True
except Exception:
    BROWSER_COOKIE3_AVAILABLE = False

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config['JSON_SORT_KEYS'] = False
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

TASKS = {}
TASK_LOCK = threading.Lock()

# ---------------- Utilities ----------------
def safe_basename(path: str) -> str:
    return Path(path).name

def add_task_message(task_id, text):
    t = TASKS.get(task_id)
    if not t:
        return
    msgs = t.setdefault("messages", [])
    msgs.append({"ts": int(time.time()), "text": text})

def run_subprocess(cmd, env=None, timeout=None):
    try:
        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        else:
            cmd_list = cmd
        proc = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env, timeout=timeout)
        return proc.returncode, proc.stdout.decode(errors='ignore'), proc.stderr.decode(errors='ignore')
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout: {e}"
    except Exception as e:
        return 1, "", str(e)

# ---------------- Cookie Helpers ----------------
def export_browser_cookies_for_domain(domain: str, out_path: str) -> bool:
    if not BROWSER_COOKIE3_AVAILABLE:
        return False
    try:
        jars = []
        for getter in [browser_cookie3.chrome, browser_cookie3.edge, browser_cookie3.firefox,
                       browser_cookie3.brave, browser_cookie3.opera]:
            try:
                jar = getter(domain_name=domain)
                if jar: jars.append(jar)
            except Exception:
                continue
        cookies = []
        for jar in jars:
            for c in jar:
                cookies.append(c)
        if not cookies:
            return False
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                domain = c.domain or ""
                flag = "TRUE" if domain.startswith(".") else "FALSE"
                path = getattr(c, "path", "/") or "/"
                secure = "TRUE" if getattr(c, "secure", False) else "FALSE"
                exp = getattr(c, "expires", 0)
                name = getattr(c, "name", "")
                value = getattr(c, "value", "")
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{int(exp)}\t{name}\t{value}\n")
        return True
    except Exception:
        app.logger.exception("export_browser_cookies_for_domain failed")
        return False


@app.route("/default_cookies", methods=["GET"])
def default_cookies_route():
    """Serve cookies.txt from the same folder as app.py"""
    base_dir = Path(__file__).parent
    candidates = [
        base_dir / "cookies.txt",
        Path("/tmp/cookies.txt"),
        Path("/mnt/data/cookies.txt"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return send_file(str(p), mimetype="text/plain")
    return ("", 404)


# ---------------- yt-dlp helpers ----------------
def prepare_yt_dlp_opts(cookiefile=None, output_template=None,
                        progress_hook=None, format_override=None,
                        audio_convert=None, merge_output_format="mp4"):
    opts = {
        "format": format_override or "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": output_template or str(DOWNLOAD_DIR / "%(title)s - %(id)s.%(ext)s"),
        "merge_output_format": merge_output_format,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if audio_convert:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_convert.get("codec", "mp3"),
            "preferredquality": str(audio_convert.get("quality", 192))
        }]
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def run_ydl_extract(url, opts):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {"ok": True, "info": info}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/favicon.ico")
def favicon():
    static_dir = Path(app.static_folder)
    fav = static_dir / "favicon.ico"
    if fav.exists():
        return send_from_directory(str(static_dir), "favicon.ico")
    return ("", 204)


@app.route("/info", methods=["POST"])
def info_route():
    url = request.form.get("url")
    if not url:
        return jsonify({"ok": False, "error": "url missing"}), 400

    cookiefile = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile = tmp.name

    opts = prepare_yt_dlp_opts(cookiefile=cookiefile)
    result = run_ydl_extract(url, opts)

    if cookiefile:
        try: os.unlink(cookiefile)
        except Exception: pass

    if result.get("ok"):
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
        return jsonify({"ok": False, "error": result.get("error")})


@app.route("/download", methods=["POST"])
def download_route():
    url = request.form.get("url")
    requested = request.form.get("requested")
    if not url:
        return jsonify({"ok": False, "error": "url missing"}), 400

    cookiefile = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile = tmp.name

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "running", "progress": "0%", "messages": []}
    add_task_message(task_id, "Task started")

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if total:
                pct = int(done * 100 / total)
            else:
                pct = 0
            TASKS[task_id]["progress"] = f"{pct}%"
        elif d.get("status") == "finished":
            TASKS[task_id]["progress"] = "100%"
            TASKS[task_id]["status"] = "processing"

    def worker():
        try:
            opts = prepare_yt_dlp_opts(cookiefile=cookiefile, progress_hook=progress_hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                TASKS[task_id].update({
                    "status": "done", "filename": Path(filename).name,
                    "info": {"title": info.get("title")}
                })
        except Exception as e:
            TASKS[task_id].update({"status": "error", "error": str(e)})
        finally:
            if cookiefile:
                try: os.unlink(cookiefile)
                except Exception: pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/task/<tid>")
def task_status(tid):
    t = TASKS.get(tid)
    if not t:
        return jsonify({"ok": False, "error": "no such task"})
    return jsonify({"ok": True, "task": t})


@app.route("/download_file/<filename>")
def serve_file(filename):
    safe_name = Path(filename).name
    path = DOWNLOAD_DIR / safe_name
    if path.exists():
        return send_file(str(path), as_attachment=True)
    return jsonify({"ok": False, "error": "file not found"}), 404


if __name__ == "__main__":
    port = 5000
    print(f"Server started at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port)
