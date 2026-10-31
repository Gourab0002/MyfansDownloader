let session = null;
let eventSource = null;
let sawError = false;
let jobStopped = false;
let previewController = null;
let pendingDownload = null;

document.addEventListener("DOMContentLoaded", async () => {
    document.getElementById("openFolder")?.addEventListener("click", openFolder);
    try {
        await loadSession();
    } catch (err) {
        const pill = document.getElementById("tokenPill");
        if (pill) pill.textContent = "Status unavailable";
    }
    if (document.body.dataset.page === "download") initDownload();
    if (document.body.dataset.page === "settings") initSettings();
});

async function loadSession() {
    const response = await fetch("/api/settings");
    if (!response.ok) throw new Error("settings");
    session = await response.json();
    paintSession(session);
    return session;
}

function paintSession(data) {
    const pill = document.getElementById("tokenPill");
    if (pill) {
        pill.textContent = data.auth_token_set ? "Token ready" : "Token needed";
        pill.className = data.auth_token_set ? "pill ok" : "pill warn";
    }
    const savePath = document.getElementById("savePath");
    if (savePath) savePath.textContent = data.output_dir || "downloads";
    const ffmpegNote = document.getElementById("ffmpegNote");
    if (ffmpegNote) ffmpegNote.hidden = data.ffmpeg_available !== false;
    const tokenHint = document.getElementById("tokenHint");
    if (tokenHint) tokenHint.hidden = !!data.auth_token_set;
}

async function openFolder() {
    const button = document.getElementById("openFolder");
    const original = button.textContent;
    button.disabled = true;
    try {
        const response = await fetch("/api/open-folder", { method: "POST" });
        if (!response.ok) throw new Error("open");
        button.textContent = original;
    } catch (err) {
        button.textContent = "Could not open";
        window.setTimeout(() => {
            button.textContent = original;
        }, 1600);
    } finally {
        button.disabled = false;
    }
}

function bindSegment(root, onChange) {
    const buttons = [...root.querySelectorAll("button")];
    function select(button) {
        buttons.forEach((item) => {
            item.setAttribute("aria-checked", item === button ? "true" : "false");
        });
        if (onChange) onChange(button.dataset.value);
    }
    buttons.forEach((button) => button.addEventListener("click", () => select(button)));
    return () => buttons.find((button) => button.getAttribute("aria-checked") === "true")?.dataset.value;
}

function initDownload() {
    const form = document.getElementById("downloadForm");
    const kind = bindSegment(document.getElementById("kind"), syncKind);
    const target = bindSegment(document.getElementById("target"), syncTarget);
    const filter = bindSegment(document.getElementById("filter"));
    const quality = bindSegment(document.getElementById("quality"));
    const formError = document.getElementById("formError");
    const startBtn = document.getElementById("startBtn");
    const stopBtn = document.getElementById("stopBtn");
    const previewCard = document.getElementById("previewCard");
    const previewText = document.getElementById("previewText");
    const confirmBtn = document.getElementById("confirmBtn");

    function syncKind(value) {
        document.getElementById("qualityFields").hidden = value === "images";
        formError.hidden = true;
        hidePreview();
    }

    function syncTarget(value) {
        document.getElementById("creatorFields").hidden = value !== "creator";
        document.getElementById("postFields").hidden = value !== "post";
        formError.hidden = true;
        hidePreview();
    }

    syncKind(kind());
    syncTarget(target());
    clearLog();
    loadLibrary();
    loadHistoryNote();

    document.getElementById("refreshLibrary").addEventListener("click", loadLibrary);
    document.getElementById("previewBack").addEventListener("click", () => {
        cancelPreviewRequest();
        hidePreview();
        setChecking(false);
    });
    stopBtn.addEventListener("click", stopJob);
    confirmBtn.addEventListener("click", () => {
        if (!pendingDownload) return;
        const payload = pendingDownload;
        hidePreview();
        beginDownload(payload);
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        formError.hidden = true;
        const payload = readPayload();
        if (!payload) return;
        await runPreview(payload);
    });

    function readPayload() {
        const payload = {
            type: kind(),
            resolution: quality() || "best",
        };
        if (target() === "creator") {
            payload.username = cleanUsername(document.getElementById("username").value);
            payload.download_type = filter() || "all";
            if (!payload.username) {
                showFormError("Enter a creator name.");
                return null;
            }
        } else {
            payload.post_id = cleanPostId(document.getElementById("postId").value);
            payload.download_type = "single";
            if (!payload.post_id) {
                showFormError("Paste a post link or ID.");
                return null;
            }
        }
        return payload;
    }

    function showFormError(text) {
        formError.textContent = text;
        formError.hidden = false;
    }

    function hidePreview() {
        previewCard.hidden = true;
        pendingDownload = null;
        const running = !stopBtn.hidden && stopBtn.textContent !== "Cancel";
        if (!running) startBtn.hidden = false;
    }

    function setChecking(checking) {
        startBtn.disabled = checking;
        startBtn.hidden = checking;
        startBtn.textContent = checking ? "Checking…" : "Start download";
        stopBtn.hidden = !checking;
        stopBtn.disabled = false;
        stopBtn.textContent = checking ? "Cancel" : "Stop";
    }

    async function runPreview(payload) {
        cancelPreviewRequest();
        hidePreview();
        previewController = new AbortController();
        setChecking(true);
        let response;
        try {
            response = await fetch("/api/preview", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
                signal: previewController.signal,
            });
        } catch (err) {
            if (err.name === "AbortError") {
                setChecking(false);
                return;
            }
            showFormError("Could not check that target.");
            setChecking(false);
            return;
        }
        const result = await response.json().catch(() => ({}));
        setChecking(false);
        if (!response.ok || !result.ok) {
            showFormError(result.error || "Could not check that target.");
            return;
        }
        pendingDownload = payload;
        previewText.textContent = previewCopy(result);
        confirmBtn.hidden = !(result.to_download > 0);
        confirmBtn.textContent = confirmLabel(result);
        previewCard.hidden = false;
        startBtn.hidden = true;
    }

    async function beginDownload(payload) {
        if (eventSource) {
            const source = eventSource;
            eventSource = null;
            source.close();
        }
        sawError = false;
        jobStopped = false;
        clearLog();
        resetProgress();
        setJobState("running", "Running");
        startBtn.hidden = true;
        startBtn.disabled = true;
        stopBtn.hidden = false;
        stopBtn.disabled = false;
        stopBtn.textContent = "Stop";

        let response;
        try {
            response = await fetch("/download", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        } catch (err) {
            appendLog("Could not reach the downloader.", "error");
            setJobState("error", "Needs attention");
            showStart();
            return;
        }
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            appendLog(error.error || "Could not start the download.", "error");
            setJobState("error", "Needs attention");
            showStart();
            return;
        }

        eventSource = new EventSource("/progress");
        eventSource.addEventListener("progress", (message) => {
            try {
                paintProgress(JSON.parse(message.data));
            } catch (err) {
                /* ignore a malformed progress event */
            }
        });
        eventSource.onmessage = (message) => {
            if (message.data === "DONE") {
                finishJob();
                return;
            }
            if (message.data === "Stopped.") {
                jobStopped = true;
            }
            noteSegmentProgress(message.data);
            const tone = classify(message.data);
            appendLog(message.data, tone);
        };
        eventSource.onerror = () => {
            if (!eventSource) return;
            eventSource.close();
            eventSource = null;
            if (document.getElementById("jobState").dataset.state === "running") {
                appendLog("The progress connection closed.", "error");
                setJobState("error", "Needs attention");
                stopBtn.hidden = false;
                stopBtn.disabled = false;
                stopBtn.textContent = "Stop";
                startBtn.hidden = true;
            }
        };
    }

    async function stopJob() {
        if (stopBtn.textContent === "Cancel") {
            cancelPreviewRequest();
            setChecking(false);
            return;
        }
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping…";
        try {
            await fetch("/download/stop", { method: "POST" });
            if (!eventSource) {
                jobStopped = true;
                showStart();
                setJobState("stopped", "Stopped");
            }
        } catch (err) {
            appendLog("Could not stop the download.", "error");
            stopBtn.disabled = false;
            stopBtn.textContent = "Stop";
        }
    }

    function showStart() {
        startBtn.hidden = false;
        startBtn.disabled = false;
        startBtn.textContent = "Start download";
        stopBtn.hidden = true;
        stopBtn.disabled = false;
        stopBtn.textContent = "Stop";
    }

    function finishJob() {
        if (eventSource) {
            const source = eventSource;
            eventSource = null;
            source.close();
        }
        showStart();
        if (jobStopped) setJobState("stopped", "Stopped");
        else if (sawError) setJobState("error", "Needs attention");
        else setJobState("done", "Finished");
        loadLibrary();
        loadHistoryNote();
    }
}

function cancelPreviewRequest() {
    if (!previewController) return;
    previewController.abort();
    previewController = null;
}

function confirmLabel(result) {
    const count = result.to_download || 0;
    const noun = result.post_type === "images" ? "image" : "video";
    return count === 1 ? `Download 1 ${noun}` : `Download ${count}`;
}

function previewCopy(result) {
    if (result.mode === "post") {
        const title = result.title || "This post";
        if (!result.available) return `${title} is not available.`;
        if (result.already_saved) return `${title} is already saved.`;
        if (!result.token_set) return `${title} is ready. No token is set, so only free posts can be saved.`;
        return `${title} is ready to download.`;
    }
    const name = result.name || result.username || "this creator";
    const noun = result.post_type === "images" ? "image post" : "video";
    const matched = Number(result.matched) || 0;
    const saved = Number(result.already_saved) || 0;
    const todo = Number(result.to_download) || 0;
    let text = `${matched} ${plural(noun, matched)} from ${name}.`;
    if (saved) text += ` ${saved} already saved.`;
    if (!matched) text += " Nothing matches that filter.";
    else if (!todo) text += " Nothing new to download.";
    else text += ` ${todo} will download.`;
    if (!result.token_set) text += " No token is set, so only free posts can be saved.";
    return text;
}

function plural(noun, count) {
    return count === 1 ? noun : `${noun}s`;
}

function resetProgress() {
    const wrap = document.getElementById("jobProgress");
    const bar = document.getElementById("jobBar");
    const count = document.getElementById("jobCount");
    if (!wrap) return;
    wrap.hidden = true;
    bar.style.width = "0%";
    count.textContent = "";
}

function paintProgress(data) {
    const wrap = document.getElementById("jobProgress");
    const bar = document.getElementById("jobBar");
    const count = document.getElementById("jobCount");
    if (!wrap || data.total == null) return;
    const done = Number(data.done) || 0;
    const total = Number(data.total) || 0;
    const skipped = Number(data.skipped) || 0;
    const failed = Number(data.failed) || 0;
    wrap.hidden = false;
    const width = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    bar.style.width = `${width}%`;
    const noun = data.detail === "segments" ? "segments" : "posts";
    let text = `${done} of ${total} ${noun}`;
    if (data.detail !== "segments" && skipped) text += ` · ${skipped} already saved`;
    if (failed) text += ` · ${failed} failed`;
    count.textContent = text;
    if (data.stopped) jobStopped = true;
}

function noteSegmentProgress(text) {
    const match = String(text).match(/Progress:\s+(\d+)\/(\d+)\s+segments/i);
    if (!match) return;
    paintProgress({ done: Number(match[1]), total: Number(match[2]), detail: "segments" });
}

function cleanUsername(value) {
    const trimmed = value.trim().replace(/^@+/, "");
    if (!trimmed.includes("/") && !trimmed.includes("://")) return trimmed;
    try {
        const url = new URL(trimmed, "https://myfans.jp");
        const parts = url.pathname.split("/").filter(Boolean);
        return (parts[parts.length - 1] || "").replace(/^@+/, "");
    } catch (err) {
        return trimmed;
    }
}

function cleanPostId(value) {
    const trimmed = value.trim();
    if (!trimmed.includes("/") && !trimmed.includes("://")) return trimmed;
    try {
        const url = new URL(trimmed, "https://myfans.jp");
        const parts = url.pathname.split("/").filter(Boolean);
        return parts[parts.length - 1] || trimmed;
    } catch (err) {
        return trimmed;
    }
}

function classify(text) {
    const lower = text.toLowerCase();
    if (lower.startsWith("already saved")) return "saved";
    if (lower.startsWith("error") || lower.includes("failed") || lower.includes("401") || lower.includes("403")) {
        return "error";
    }
    if (lower.startsWith("warning") || lower === "stopped.") return "warn";
    return "";
}

function setJobState(state, label) {
    const node = document.getElementById("jobState");
    node.dataset.state = state;
    node.textContent = label;
}

function clearLog() {
    const log = document.getElementById("log");
    log.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Nothing running yet. Choose a creator or a post, then start.";
    log.appendChild(empty);
}

function appendLog(text, tone) {
    const log = document.getElementById("log");
    const empty = log.querySelector(".empty");
    if (empty) empty.remove();
    const line = document.createElement("p");
    if (tone) line.className = tone;
    const stamp = new Date().toLocaleTimeString();
    line.textContent = `${stamp}  ${text}`;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
    if (tone === "error") sawError = true;
}

async function loadHistoryNote() {
    const note = document.getElementById("historyNote");
    if (!note) return;
    try {
        const response = await fetch("/status");
        const status = await response.json();
        const count = (status.completed_files || []).length;
        note.textContent = count ? `${count} saved in earlier runs` : "";
    } catch (err) {
        note.textContent = "";
    }
}

async function loadLibrary() {
    const list = document.getElementById("libraryList");
    if (!list) return;
    try {
        const response = await fetch("/api/library");
        const payload = await response.json();
        paintLibrary(payload.files || []);
    } catch (err) {
        list.replaceChildren();
        const item = document.createElement("li");
        item.className = "empty";
        item.textContent = "Could not read the save folder.";
        list.appendChild(item);
    }
}

function paintLibrary(files) {
    const list = document.getElementById("libraryList");
    list.replaceChildren();
    if (!files.length) {
        const item = document.createElement("li");
        item.className = "empty";
        item.textContent = "Saved videos and images will show up here.";
        list.appendChild(item);
        return;
    }
    files.forEach((file) => {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.className = "file-name";
        name.textContent = file.name;
        const meta = document.createElement("span");
        meta.className = "file-meta";
        const bits = [file.folder, formatSize(file.size)].filter(Boolean);
        meta.textContent = bits.join(" · ");
        item.append(name, meta);
        list.appendChild(item);
    });
}

function formatSize(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
    return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function initSettings() {
    const form = document.getElementById("settingsForm");
    const pattern = document.getElementById("filename_pattern");
    const separator = document.getElementById("filename_separator");
    const preview = document.getElementById("namePreview");
    const note = document.getElementById("saveNote");
    const token = document.getElementById("auth_token");

    function paintPreview() {
        preview.textContent = filenamePreview(pattern.value, separator.value);
    }

    if (session) {
        pattern.value = session.filename_pattern || "{creator}_{date}_{id}";
        separator.value = session.filename_separator || "_";
        document.getElementById("output_dir").value = session.output_dir || "";
        document.getElementById("thread_count").value = session.thread_count || 3;
        token.value = "";
        token.placeholder = session.auth_token_set
            ? "Token is set. Leave blank to keep it."
            : "Paste the _mfans_token cookie";
        const metadata = String(session.write_metadata || "0").toLowerCase();
        document.getElementById("write_metadata").checked = ["1", "true", "yes", "on"].includes(metadata);
        paintPreview();
    }

    pattern.addEventListener("input", paintPreview);
    separator.addEventListener("input", paintPreview);

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        note.textContent = "Saving…";
        const body = {
            filename_pattern: pattern.value,
            filename_separator: separator.value,
            auth_token: token.value,
            thread_count: parseInt(document.getElementById("thread_count").value, 10) || 3,
            write_metadata: document.getElementById("write_metadata").checked,
            output_dir: document.getElementById("output_dir").value.trim(),
        };
        try {
            const response = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                note.textContent = payload.error || "Could not save.";
                return;
            }
            token.value = "";
            await loadSession();
            if (session && session.output_dir) {
                document.getElementById("output_dir").value = session.output_dir;
            }
            token.placeholder = session && session.auth_token_set
                ? "Token is set. Leave blank to keep it."
                : "Paste the _mfans_token cookie";
            note.textContent = "Saved.";
        } catch (err) {
            note.textContent = "Could not save.";
        }
    });
}

function filenamePreview(pattern, separator) {
    const sep = separator || "_";
    const sample = {
        "{creator}": "creator",
        "{date}": "2026-10-31",
        "{id}": "a1b2c3",
        "{title}": "post",
        "{number}": "",
        "{letter}": "",
        "{separator}": sep,
    };
    let name = pattern || "{creator}_{date}_{id}";
    Object.entries(sample).forEach(([token, value]) => {
        name = name.split(token).join(value);
    });
    return `${name}.mp4`;
}
