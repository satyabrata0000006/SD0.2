const urlInput = document.getElementById("urlInput");
const getInfoBtn = document.getElementById("getInfoBtn");
const downloadBtn = document.getElementById("downloadBtn");
const formatSelect = document.getElementById("formatSelect");
const cookiesFile = document.getElementById("cookiesFile");
const useBrowserCookies = document.getElementById("useBrowserCookies");
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
  el.textContent = `✅ Default cookies loaded: ${filename}`;
}

// auto-load cookies.txt from same dir as app.py
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

getInfoBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) return alert("Paste a URL first!");

  getInfoBtn.disabled = true;
  statusText.textContent = "Fetching info...";

  try {
    const fd = new FormData();
    fd.append("url", url);
    if (cookiesFile.files.length) {
      fd.append("cookies", cookiesFile.files[0]);
    } else if (defaultCookieBlob) {
      fd.append("cookies", new File([defaultCookieBlob], defaultCookieName, { type: "text/plain" }));
    }

    const res = await fetch("/info", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "failed");

    infoSection.classList.remove("hidden");
    thumb.src = data.thumbnail || "";
    videoTitle.textContent = data.title || "(No title)";
    metaRow.textContent = data.uploader ? `By ${data.uploader}` : "";

    formatSelect.innerHTML = "";
    for (const f of data.formats) {
      const opt = document.createElement("option");
      opt.value = f.format_id;
      opt.textContent = `${f.height || ""}p • ${f.ext || ""}`;
      formatSelect.appendChild(opt);
    }

    appendLog("Video info loaded");
    statusText.textContent = "Info loaded successfully.";
  } catch (e) {
    appendLog("Error fetching info: " + e);
    alert("Error fetching info: " + e);
  } finally {
    getInfoBtn.disabled = false;
  }
});

downloadBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) return alert("Paste a URL first!");
  const fmt = formatSelect.value || "";

  downloadBtn.disabled = true;
  statusText.textContent = "Starting download...";
  progressBar.classList.remove("hidden");

  try {
    const fd = new FormData();
    fd.append("url", url);
    fd.append("requested", fmt);
    if (cookiesFile.files.length) {
      fd.append("cookies", cookiesFile.files[0]);
    } else if (defaultCookieBlob) {
      fd.append("cookies", new File([defaultCookieBlob], defaultCookieName, { type: "text/plain" }));
    }

    const res = await fetch("/download", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "failed");

    const task_id = data.task_id;
    appendLog("Task started: " + task_id);

    const poll = setInterval(async () => {
      const r = await fetch(`/task/${task_id}`);
      const j = await r.json();
      if (!j.ok) return;
      const task = j.task;
      progressInner.style.width = task.progress || "0%";
      statusText.textContent = `Status: ${task.status} (${task.progress})`;
      if (task.status === "done") {
        clearInterval(poll);
        appendLog("Download complete");
        window.location.href = `/download_file/${encodeURIComponent(task.filename)}`;
        progressInner.style.width = "100%";
        downloadBtn.disabled = false;
      } else if (task.status === "error") {
        clearInterval(poll);
        alert("Download failed: " + task.error);
        appendLog("Download failed: " + task.error);
        downloadBtn.disabled = false;
      }
    }, 1500);
  } catch (e) {
    alert("Error starting download: " + e);
    appendLog("Error starting download: " + e);
    downloadBtn.disabled = false;
  }
});
