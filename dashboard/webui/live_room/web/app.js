const $ = (id) => document.getElementById(id);
let selectedId = null;
let paused = false;
let currentMessageSignature = "";
let pollTimer = null;
let followLive = true;
let refreshInFlight = false;

function safeText(value) { return String(value ?? ""); }
function formatTime(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
}
function shortDate(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
}
function setConnection(ok, text) {
  const el = $("connection");
  el.classList.toggle("online", !!ok);
  el.lastChild.textContent = " " + text;
}
async function api(url) {
  const response = await fetch(url, {cache:"no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function renderSessions(items) {
  const host = $("sessions");
  if (!items.length) {
    host.innerHTML = '<div class="empty-side">No discussions yet.<br>Start one from ChatGPT Desktop.</div>';
    return;
  }
  host.replaceChildren();
  for (const s of items) {
    const button = document.createElement("button");
    button.className = "session-item" + (s.session_id === selectedId ? " selected" : "");
    const title = document.createElement("span"); title.className = "session-title"; title.textContent = s.topic || "Untitled discussion";
    const preview = document.createElement("span"); preview.className = "session-preview"; preview.textContent = s.last_content || "No messages yet";
    const foot = document.createElement("span"); foot.className = "session-foot";
    const date = document.createElement("span"); date.textContent = shortDate(s.last_message_at || s.updated_at);
    const pill = document.createElement("span"); pill.className = "pill " + safeText(s.status); pill.textContent = s.status;
    foot.append(date,pill); button.append(title,preview,foot);
    button.addEventListener("click", () => selectSession(s.session_id, true));
    host.append(button);
  }
}
function renderMessages(messages) {
  const host = $("timeline");
  const signature = messages.map(m => `${m.message_id}:${m.sender}:${m.created_at}`).join("|");
  if (signature === currentMessageSignature) return;
  currentMessageSignature = signature;
  const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 100;
  host.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("div"); empty.className = "empty-note"; empty.textContent = "Waiting for the first message…"; host.append(empty); return;
  }
  for (const m of messages) {
    const sender = safeText(m.sender).toLowerCase();
    const row = document.createElement("div"); row.className = "message-row " + (sender === "claud" ? "claud" : sender === "user" ? "user" : "chatgpt");
    const avatar = document.createElement("div"); avatar.className = "avatar " + (sender === "claud" ? "claud-avatar" : sender === "user" ? "user-avatar" : "chatgpt-avatar"); avatar.textContent = sender === "claud" ? "C" : sender === "user" ? "Y" : "G";
    const content = document.createElement("div"); content.className = "message-content";
    const header = document.createElement("div"); header.className = "message-header";
    const name = document.createElement("strong"); name.textContent = sender === "claud" ? "CLAUD AI" : sender === "user" ? "YOU" : "CHATGPT";
    const time = document.createElement("time"); time.textContent = formatTime(m.created_at);
    const text = document.createElement("div"); text.className = "message-text"; text.textContent = safeText(m.content);
    header.append(name,time); content.append(header,text); row.append(avatar,content); host.append(row);
  }
  if (atBottom) host.scrollTop = host.scrollHeight;
}
function renderDevelopmentEvents(events) {
  let panel = $("developmentActivityPanel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "developmentActivityPanel";
    panel.className = "development-activity-panel";
    const heading = document.createElement("div");
    heading.className = "development-activity-heading";
    const title = document.createElement("strong");
    title.textContent = "Development activity";
    const count = document.createElement("span");
    count.id = "developmentActivityCount";
    heading.append(title, count);
    const list = document.createElement("div");
    list.id = "developmentActivityList";
    list.className = "development-activity-list";
    panel.append(heading, list);
    const timeline = $("timeline");
    timeline.parentNode.insertBefore(panel, timeline);
  }
  const host = $("developmentActivityList");
  const items = (events || []).filter(e => safeText(e.event_type).startsWith("dev_")).slice(-18);
  const signature = items.map(e => `${e.event_id}:${e.event_type}:${e.created_at}`).join("|");
  if (panel.dataset.signature === signature) return;
  panel.dataset.signature = signature;
  $("developmentActivityCount").textContent = `${items.length} events`;
  host.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "development-event-empty";
    empty.textContent = "No development events linked to this session yet.";
    host.append(empty);
    return;
  }
  const labels = {
    task_linked_to_session: "Task linked to this Live Room",
    task_started: "Development task queued",
    agent_thinking: "Claud is reasoning",
    agent_stream_complete: "Model response completed",
    list_workspace_files: "Inspected workspace files",
    read_workspace_file: "Read workspace file",
    read_project_source: "Inspected permitted source",
    write_workspace_file: "Wrote workspace file",
    run_python_compile: "Python syntax validation",
    validate_powershell_syntax: "PowerShell syntax validation",
    run_safe_powershell: "Ran allowlisted PowerShell",
    tool_call_validation_failed: "Tool call rejected by validator",
    task_review_required: "Mentor review required",
    task_blocked: "Development task blocked",
    task_paused: "Development paused",
    task_stopped: "Development stopped",
    task_completed: "Development task completed",
    mentor_feedback: "Mentor feedback recorded"
  };
  for (const event of items) {
    const type = safeText(event.event_type).replace(/^dev_/, "");
    const row = document.createElement("div");
    row.className = "development-event";
    const dot = document.createElement("span");
    dot.className = "development-event-dot " + (type.includes("failed") || type.includes("blocked") ? "warning" : type.includes("completed") ? "success" : "");
    const body = document.createElement("div");
    body.className = "development-event-body";
    const head = document.createElement("div");
    head.className = "development-event-head";
    const name = document.createElement("strong");
    name.textContent = labels[type] || type.replaceAll("_", " ");
    const time = document.createElement("time");
    time.textContent = formatTime(event.created_at);
    const detail = document.createElement("div");
    detail.className = "development-event-detail";
    detail.textContent = safeText(event.detail || "");
    head.append(name, time);
    body.append(head, detail);
    row.append(dot, body);
    host.append(row);
  }
}

function setThinking(events, session, messages) {
  const latest = events.length ? events[events.length-1] : null;
  const latestDev = [...(events || [])].reverse().find(e => safeText(e.event_type).startsWith("dev_"));
  const devType = latestDev ? safeText(latestDev.event_type).replace(/^dev_/, "") : "";
  const devThinking = ["agent_thinking", "agent_stream_started", "agent_model_request_started"].includes(devType);
  const coreThinking = !!latest && latest.event_type === "claud_thinking";
  const isThinking = (coreThinking || devThinking) && session.status === "active";
  $("thinking").classList.toggle("hidden", !isThinking);
  const label = $("thinking").querySelector(".thinking-label");
  if (label) label.textContent = devThinking ? "Claud is developing through the self-development agent" : "Claud is processing through its Core pipeline";
  $("liveBadge").textContent = isThinking ? (devThinking ? "● CLAUD DEVELOPING" : "● CLAUD THINKING") : "● LIVE MONITOR";
  $("liveBadge").style.color = isThinking ? "#93c5fd" : "#34d399";
}
function updateFollowButton() {
  const button = $("followLiveBtn");
  if (!button) return;
  button.textContent = followLive ? "Following active session" : "Follow live session";
  button.classList.toggle("follow-active", followLive);
  button.title = followLive
    ? "Automatically follow the newest active discussion"
    : "Enable automatic follow of the newest active discussion";
}
async function refreshSessions() {
  const data = await api("/api/sessions?limit=50");
  const sessions = data.sessions || [];
  if (followLive) {
    // The API orders sessions by updated_at DESC. Prefer the newest active
    // session so a discussion started after page load becomes visible at once.
    const target = sessions.find(s => s.status === "active") || sessions[0];
    if (target && target.session_id !== selectedId) {
      selectedId = target.session_id;
      currentMessageSignature = "";
    }
  } else if (!selectedId && sessions.length) {
    selectedId = sessions[0].session_id;
  }
  renderSessions(sessions);
  updateFollowButton();
}
async function refreshSelected() {
  if (!selectedId) return;
  const data = await api("/api/session?id=" + encodeURIComponent(selectedId));
  const s = data.session;
  $("topic").textContent = s.topic || "Untitled discussion";
  $("sessionMeta").textContent = `Session ${s.session_id} · Started ${shortDate(s.created_at)} · Core conversation linked`;
  $("exchangeCount").textContent = `${s.turn_count} / ${s.max_turns}`;
  $("messageCount").textContent = (data.messages || []).length;
  $("statusValue").textContent = s.status;
  $("statusValue").style.color = s.status === "active" ? "#34d399" : s.status === "completed" ? "#93c5fd" : "#fda4af";
  const last = data.messages?.[data.messages.length-1];
  $("lastActivity").textContent = last ? formatTime(last.created_at) : "—";
  renderMessages(data.messages || []);
  renderDevelopmentEvents(data.events || []);
  setThinking(data.events || [], s, data.messages || []);
}
async function selectSession(id, manual = false) {
  if (manual) followLive = false;
  selectedId = id;
  currentMessageSignature = "";
  updateFollowButton();
  await refreshSessions();
  await refreshSelected();
}
async function refreshAll() {
  if (paused || refreshInFlight) return;
  refreshInFlight = true;
  try {
    await refreshSessions();
    if (selectedId) await refreshSelected();
    setConnection(true, "Connected");
    $("dbStatus").textContent = "communication.db · read-only · polling";
  } catch (err) {
    setConnection(false, "Database unavailable");
    $("dbStatus").textContent = err.message;
  } finally {
    refreshInFlight = false;
  }
}
$("refreshBtn").addEventListener("click", () => refreshAll());
$("pauseVisualBtn").addEventListener("click", () => {
  paused = !paused;
  $("pauseVisualBtn").textContent = paused ? "Resume updates" : "Pause updates";
  $("liveBadge").textContent = paused ? "● UPDATES PAUSED" : "● LIVE MONITOR";
  if (!paused) refreshAll();
});
$("followLiveBtn").addEventListener("click", () => {
  followLive = !followLive;
  if (followLive) {
    selectedId = null;
    currentMessageSignature = "";
  }
  updateFollowButton();
  refreshAll();
});
(async function boot() {
  try {
    const health = await api("/api/health");
    if (health.status !== "ready") throw new Error("Communication database is not ready");
    await refreshAll();
  } catch (err) {
    setConnection(false, "Not ready");
    $("dbStatus").textContent = err.message;
  }
  updateFollowButton();
  pollTimer = setInterval(refreshAll, 750);
})();
