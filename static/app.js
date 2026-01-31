let currentJobId = null;
let ws = null;
let pollInterval = null;

async function submitPlaylist() {
    const urlInput = document.getElementById("playlist-url");
    const btn = document.getElementById("download-btn");
    const errorMsg = document.getElementById("error-msg");
    const url = urlInput.value.trim();

    if (!url) {
        showError("Please enter a Spotify playlist URL");
        return;
    }

    errorMsg.classList.add("hidden");
    btn.disabled = true;
    btn.textContent = "Loading...";

    try {
        const resp = await fetch("/api/playlist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || "Failed to start download");
        }

        const data = await resp.json();
        showJob(data.job_id, data.playlist_name, data.track_count);
    } catch (e) {
        showError(e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "Download";
    }
}

function showJob(jobId, playlistName, trackCount) {
    currentJobId = jobId;
    document.getElementById("playlist-name").textContent = playlistName;
    document.getElementById("stats-total").textContent = `${trackCount} tracks`;
    document.getElementById("job-info").classList.remove("hidden");
    document.getElementById("track-table-wrapper").classList.remove("hidden");
    document.getElementById("job-controls").classList.remove("hidden");
    connectWebSocket(jobId);
}

function showError(msg) {
    const el = document.getElementById("error-msg");
    el.textContent = msg;
    el.classList.remove("hidden");
}

function connectWebSocket(jobId) {
    if (ws) {
        ws.close();
    }

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws/jobs/${jobId}`);

    ws.onmessage = (event) => {
        const job = JSON.parse(event.data);
        if (job.error) {
            showError(job.error);
            return;
        }
        renderJob(job);
    };

    ws.onclose = () => {
        // Fallback: poll once more
        setTimeout(() => pollJob(jobId), 2000);
    };

    ws.onerror = () => {
        // Fallback to polling
        ws.close();
        startPolling(jobId);
    };
}

function startPolling(jobId) {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(() => pollJob(jobId), 2000);
}

async function pollJob(jobId) {
    try {
        const resp = await fetch(`/api/jobs/${jobId}`);
        if (!resp.ok) return;
        const job = await resp.json();
        renderJob(job);
        if ((job.status === "complete" || job.status === "stopped") && pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    } catch (e) {
        // ignore
    }
}

function renderJob(job) {
    const tracks = job.tracks || [];
    const total = tracks.length;
    const complete = tracks.filter(t => t.status === "complete").length;
    const failed = tracks.filter(t => t.status === "failed" || t.status === "not_found").length;

    document.getElementById("stats-total").textContent = `${total} tracks`;
    document.getElementById("stats-complete").textContent = `${complete} complete`;
    document.getElementById("stats-failed").textContent = failed > 0 ? `${failed} failed` : "";

    const pct = total > 0 ? ((complete + failed) / total) * 100 : 0;
    document.getElementById("overall-progress").style.width = `${pct}%`;

    // Update control buttons based on job status
    updateControls(job.status);

    const tbody = document.getElementById("track-tbody");
    // Build or update rows
    if (tbody.children.length !== tracks.length) {
        tbody.innerHTML = "";
        tracks.forEach((t, i) => {
            const tr = document.createElement("tr");
            tr.id = `track-row-${i}`;
            tr.innerHTML = buildRowHTML(t, i);
            tbody.appendChild(tr);
        });
    } else {
        tracks.forEach((t, i) => {
            const tr = document.getElementById(`track-row-${i}`);
            if (tr) tr.innerHTML = buildRowHTML(t, i);
        });
    }
}

function updateControls(status) {
    const stopBtn = document.getElementById("stop-btn");
    const resumeBtn = document.getElementById("resume-btn");

    if (status === "running") {
        stopBtn.classList.remove("hidden");
        stopBtn.disabled = false;
        stopBtn.textContent = "Stop";
        resumeBtn.classList.add("hidden");
    } else if (status === "stopped") {
        stopBtn.classList.add("hidden");
        resumeBtn.classList.remove("hidden");
        resumeBtn.disabled = false;
    } else if (status === "complete") {
        stopBtn.classList.add("hidden");
        resumeBtn.classList.add("hidden");
    }
}

async function stopJob() {
    if (!currentJobId) return;
    const btn = document.getElementById("stop-btn");
    btn.disabled = true;
    btn.textContent = "Stopping...";
    try {
        await fetch(`/api/jobs/${currentJobId}/stop`, { method: "POST" });
    } catch (e) {
        showError("Failed to stop job");
    }
}

async function resumeJob() {
    if (!currentJobId) return;
    const btn = document.getElementById("resume-btn");
    btn.disabled = true;
    btn.textContent = "Resuming...";
    try {
        const resp = await fetch(`/api/jobs/${currentJobId}/resume`, { method: "POST" });
        if (resp.ok) {
            connectWebSocket(currentJobId);
        }
    } catch (e) {
        showError("Failed to resume job");
        btn.disabled = false;
    }
}

function buildRowHTML(trackJob, index) {
    const t = trackJob.track;
    const status = trackJob.status;
    const progress = trackJob.progress_pct || 0;
    const errorTitle = trackJob.error ? ` title="${escapeHtml(trackJob.error)}"` : "";

    let progressCell = "";
    if (status === "downloading") {
        progressCell = `
            <div class="track-progress">
                <div class="track-progress-fill" style="width:${progress.toFixed(0)}%"></div>
            </div>
            <span style="margin-left:6px;font-size:0.75rem;color:#888">${progress.toFixed(0)}%</span>
        `;
    } else if (status === "complete") {
        progressCell = `<span style="color:#1db954;font-size:0.8rem">Done</span>`;
    } else if (status === "failed" || status === "not_found") {
        progressCell = `<span style="color:#e74c3c;font-size:0.8rem">${status === "not_found" ? "Not found" : "Error"}</span>`;
    }

    return `
        <td>${index + 1}</td>
        <td>${escapeHtml(t.title)}</td>
        <td>${escapeHtml(t.artist)}</td>
        <td>${escapeHtml(t.album)}</td>
        <td><span class="status-badge status-${status}"${errorTitle}>${formatStatus(status)}</span></td>
        <td>${progressCell}</td>
    `;
}

function formatStatus(status) {
    const map = {
        pending: "Pending",
        searching: "Searching",
        found: "Found",
        downloading: "Downloading",
        tagging: "Tagging",
        complete: "Complete",
        failed: "Failed",
        not_found: "Not Found",
    };
    return map[status] || status;
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// Load existing jobs on page load
async function loadExistingJobs() {
    try {
        const resp = await fetch("/api/jobs");
        if (!resp.ok) return;
        const jobs = await resp.json();

        // Find the most recent running or stopped job
        const activeJob = jobs.find(j => j.status === "running" || j.status === "stopped");
        if (activeJob) {
            showJob(activeJob.job_id, activeJob.playlist_name, activeJob.track_count);
            return;
        }

        // Show most recent completed job if any
        if (jobs.length > 0) {
            const last = jobs[jobs.length - 1];
            showJob(last.job_id, last.playlist_name, last.track_count);
        }
    } catch (e) {
        // No existing jobs, that's fine
    }
}

// Health check on load
async function checkHealth() {
    const dot = document.getElementById("health-dot");
    const text = document.getElementById("health-text");
    try {
        const resp = await fetch("/api/health");
        const data = await resp.json();
        if (data.slskd_connected) {
            dot.className = "health-dot ok";
            text.textContent = "slskd connected";
        } else {
            dot.className = "health-dot degraded";
            text.textContent = "slskd not reachable";
        }
    } catch {
        dot.className = "health-dot degraded";
        text.textContent = "Server offline";
    }
}

// Enter key to submit
document.getElementById("playlist-url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitPlaylist();
});

checkHealth();
setInterval(checkHealth, 30000);
loadExistingJobs();
