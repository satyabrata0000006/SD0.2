function humanFileSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  const thresh = 1024;
  if (Math.abs(bytes) < thresh) return bytes + " B";
  const units = ["KB","MB","GB","TB"];
  let u=-1;
  do { bytes /= thresh; ++u; } while (Math.abs(bytes) >= thresh && u < units.length-1);
  return bytes.toFixed(1) + " " + units[u];
}

const urlInput = document.getElementById("urlInput");
const getInfoBtn = document.getElementById("getInfoBtn");
const downloadBtn = document.getElementById("downloadBtn");
const formatSelect = document.getElementById("formatSelect");
const audioSelect = document.getElementById("audioSelect") || { value: "" };
const cookiesFile = document.getElementById("cookiesFile");
const infoSection = document.getElementById("infoSection");
const thumb = document.getElementById("thumb");
const videoTitle = document.getElementById("videoTitle");
const metaRow = document.getElementById("metaRow");
const progressBar = document.getElementById("progressBar");
const progressInner = document.getElementById("progressInner");
const statusText = document.getElementById("statusText");
const liveLog = document.getElementById("liveLog");

let defaultCookieBlob = null;
let defaultCookieName = null;

function appendLog(msg) {
  const ts = new Date().toLocaleTimeString();
  const div = document.createElement("div");
  div.textContent = `[${ts}] ${msg}`;
  liveLog.appendChild(div);
  liveLog.scrollTop = liveLog.scrollHeight;
}

function showDefaultCookieLoaded(filename) {
  let el = document.getElementById("defaultCookieNotice");
  if (!el) {
    el = document.createElement("div");
    el.id = "defaultCookieNotice";
    el.className = "text-sm text-gray-500 mt-1";
    cookiesFile.parentNode.insertBefore(el, cookiesFile.nextSibling);
  }
  el.textContent = `✅ Default cookies loaded: ${filename} (auto-attached)`;
}

// Auto-load cookies.txt from server (same folder as app.py)
(async function tryLoadDefaultCookie(){
  try {
    const res = await fetch("/default_cookies");
    if (!res.ok) return;
    const blob = await res.blob();
    defaultCookieBlob = blob;
    defaultCookieName = "cookies.txt";
    showDefaultCookieLoaded(defaultCookieName);
    appendLog("Default cookies loaded from server");
  } catch (e) {
    console.log("No default cookies available");
  }
})();

function buildFormDataFor(url, requested) {
  const fd = new FormData();
  fd.append("url", url);
  if (requested !== undefined) fd.append("requested", requested);
  if (cookiesFile.files && cookiesFile.files.length) {
    fd.append("cookies", cookiesFile.files[0]);
  } else if (defaultCookieBlob) {
    fd.append("cookies", new File([defaultCookieBlob], defaultCookieName || "cookies.txt", { type: "text/plain" }));
  }
  return fd;
}

getInfoBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) return alert("Paste a URL first!");

  getInfoBtn.disabled = true;
  statusText.textContent = "Fetching info...";

  try {
    const fd = buildFormDataFor(url);
    const res = await fetch("/info", { method: "POST", body: fd });
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      const text = await res.text();
      throw new Error("Expected JSON but got HTML:\n" + text.slice(0,200));
    }
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Failed");

    infoSection.classList.remove("hidden");
    thumb.src = data.thumbnail || "";
    videoTitle.textContent = data.title || "(No title)";

    const duration = data.duration;
    let meta = data.uploader ? `By ${data.uploader}` : "";
    if (duration) {
      const m = Math.floor(duration / 60);
      const s = duration % 60;
      meta += (meta ? " • " : "") + `${m}m ${s}s`;
    }
    metaRow.textContent = meta;

    formatSelect.innerHTML = "";
    const fmts = (data.formats || []).filter(f => f.ext);
    fmts.sort((a,b) => ((b.height||0)-(a.height||0)) || ((b.abr||0)-(a.abr||0)));
    const seen = new Set();
    const uniq = [];
    for (const f of fmts) {
      const key = `${f.ext}-${f.height||0}-${f.abr||0}-${f.fps||0}`;
      if (!seen.has(key)) { seen.add(key); uniq.push(f); }
    }
    const bestOpt = document.createElement("option");
    bestOpt.value = "";
    bestOpt.textContent = "Best available (auto)";
    formatSelect.appendChild(bestOpt);
    for (const f of uniq) {
      const opt = document.createElement("option");
      opt.value = f.format_id;
      const parts = [];
      if (f.height) parts.push(`${f.height}p`);
      else if (f.abr) parts.push(`${f.abr} kbps`);
      parts.push(`.${f.ext}`);
      const v = f.vcodec || "none", a = f.acodec || "none";
      if (v !== "none" && a !== "none") parts.push("• muxed");
      else if (v !== "none" && a === "none") parts.push("• video-only");
      else if (v === "none" && a !== "none") parts.push("• audio-only");
      if (f.filesize || f.filesize_approx) parts.push(`• ${humanFileSize(f.filesize || f.filesize_approx)}`);
      opt.textContent = parts.join(" ");
      formatSelect.appendChild(opt);
    }

    appendLog("Video info loaded");
    statusText.textContent = "Info loaded successfully.";
  } catch (e) {
    appendLog("Error fetching info: " + e.message);
    alert("Error fetching info: " + e.message);
  } finally {
    getInfoBtn.disabled = false;
  }
});

downloadBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) return alert("Paste a URL first!");
  const fmt = formatSelect.value || "";
  const audioReq = audioSelect.value || ""; // e.g., audio:mp3

  downloadBtn.disabled = true;
  statusText.textContent = "Starting download...";
  progressBar.classList.remove("hidden");
  progressInner.style.width = "0%";

  try {
    const requested = audioReq ? audioReq : fmt;
    const fd = buildFormDataFor(url, requested);
    const res = await fetch("/download", { method: "POST", body: fd });
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      const text = await res.text();
      throw new Error("Expected JSON but got HTML:\n" + text.slice(0,200));
    }
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "failed");

    const task_id = data.task_id;
    appendLog("Task started: " + task_id);

    const poll = setInterval(async () => {
      try {
        const r = await fetch(`/task/${task_id}`);
        const j = await r.json();
        if (!j.ok) return;
        const task = j.task || {};
        progressInner.style.width = task.progress || "0%";
        statusText.textContent = `Status: ${task.status} (${task.progress})`;

        if (task.status === "done" && task.filename) {
          clearInterval(poll);
          appendLog("Download complete");
          progressInner.style.width = "100%";
          window.location.href = `/download_file/${encodeURIComponent(task.filename)}`;
          downloadBtn.disabled = false;
        } else if (task.status === "error") {
          clearInterval(poll);
          appendLog("Download failed: " + (task.error || "unknown"));
          alert("Download failed: " + (task.error || "unknown"));
          downloadBtn.disabled = false;
        }
      } catch (err) {
        console.error("Poll error", err);
      }
    }, 1500);
  } catch (e) {
    alert("Error starting download: " + e.message);
    appendLog("Error starting download: " + e.message);
    downloadBtn.disabled = false;
    progressBar.classList.add("hidden");
  }
});
