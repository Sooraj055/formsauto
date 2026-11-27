/* FormRush Mobile-First Frontend Controller (zero personal data stored in source) */

const STAGES = [
  { key: "link_received", label: "Link received" },
  { key: "form_opened", label: "Form opened" },
  { key: "questions_detected", label: "Questions detected" },
  { key: "answers_matched", label: "Answers matched" },
  { key: "form_filled", label: "Form filled" },
  { key: "validation_completed", label: "Validation completed" },
  { key: "submission_initiated", label: "Submission initiated" },
  { key: "submission_confirmed", label: "Submission confirmed" },
];

let authToken = localStorage.getItem("formrush_token") || "";
let activeRunId = null;
let eventSource = null;
let pollTimer = null;
let lastUrlSubmitted = "";

const $ = (id) => document.getElementById(id);

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (authToken) headers["X-FormRush-Token"] = authToken;
  return headers;
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  if (res.status === 401) {
    showPinModal();
    throw new Error("Unlock required.");
  }
  return res;
}

function showPinModal() {
  $("pinModal").classList.remove("hidden");
  $("pinInput").focus();
}

function hidePinModal() {
  $("pinModal").classList.add("hidden");
  $("pinError").classList.add("hidden");
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// --- Navigation Tabs ---
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "historyTab") loadHistory();
    if (btn.dataset.tab === "profileTab") loadProfile();
  });
});

// --- Health & Auth ---
async function initApp() {
  try {
    const [healthRes, authRes] = await Promise.all([
      fetch("/api/health"),
      fetch("/api/auth/status", { headers: authHeaders() }),
    ]);
    const health = await healthRes.json();
    const auth = await authRes.json();

    const badge = $("engineBadge");
    if (health.browser_ready) {
      badge.textContent = "Browser Ready";
      badge.className = "pill pill-green";
    } else {
      badge.textContent = "Warming Up";
      badge.className = "pill pill-yellow";
    }

    if (auth.pin_required && !auth.unlocked) {
      showPinModal();
    } else {
      await Promise.all([loadProfile(), loadHistory()]);
    }
  } catch {
    $("engineBadge").textContent = "Offline";
    $("engineBadge").className = "pill pill-red";
  }
}

$("pinUnlockForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("pinInput").value.trim();
  const res = await fetch("/api/auth/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin }),
  });
  if (!res.ok) {
    $("pinError").textContent = "Incorrect PIN.";
    $("pinError").classList.remove("hidden");
    return;
  }
  const data = await res.json();
  authToken = data.token;
  localStorage.setItem("formrush_token", authToken);
  $("pinInput").value = "";
  hidePinModal();
  await Promise.all([loadProfile(), loadHistory()]);
});

// --- Paste from Clipboard ---
$("pasteBtn").addEventListener("click", async () => {
  $("urlError").classList.add("hidden");
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      $("formUrlInput").value = text.trim();
    }
  } catch {
    $("formUrlInput").focus();
    $("urlError").textContent = "Tap inside the box and paste manually (clipboard permission denied).";
    $("urlError").classList.remove("hidden");
  }
});

// --- Run Automation ---
$("startRunForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("formUrlInput").value.trim();
  await launchRun(url, false);
});

async function launchRun(url, forceResubmit) {
  $("urlError").classList.add("hidden");
  $("resultBanner").classList.add("hidden");
  $("pausePanel").classList.add("hidden");
  if (!url) {
    $("urlError").textContent = "Please paste a Google Forms link first.";
    $("urlError").classList.remove("hidden");
    return;
  }

  lastUrlSubmitted = url;
  $("startBtn").disabled = true;
  $("startBtn").textContent = "Running...";

  try {
    const res = await apiFetch("/api/runs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, force_resubmit: forceResubmit }),
    });
    const data = await res.json();
    if (!res.ok) {
      $("urlError").textContent = data.detail || "Unable to start form run.";
      $("urlError").classList.remove("hidden");
      resetStartButton();
      return;
    }
    activeRunId = data.run_id;
    renderRunSummary(data);

    if (data.status === "duplicate_blocked") {
      resetStartButton();
      return;
    }
    subscribeRunEvents(data.run_id);
  } catch (err) {
    $("urlError").textContent = err.message || "Network error.";
    $("urlError").classList.remove("hidden");
    resetStartButton();
  }
}

function resetStartButton() {
  $("startBtn").disabled = false;
  $("startBtn").textContent = "Analyze / Start";
}

function closeEventStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function subscribeRunEvents(runId) {
  closeEventStream();
  const qs = authToken ? `?token=${encodeURIComponent(authToken)}` : "";
  eventSource = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events${qs}`);

  eventSource.onmessage = (evt) => {
    try {
      const summary = JSON.parse(evt.data);
      renderRunSummary(summary);
      if (isTerminal(summary.status)) {
        closeEventStream();
        resetStartButton();
        loadHistory();
      }
    } catch {
      // ignore keepalive/malformed
    }
  };

  eventSource.onerror = () => {
    // Fallback polling in case mobile network interrupts SSE
    if (!pollTimer) {
      pollTimer = setInterval(async () => {
        try {
          const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}`);
          if (!res.ok) return;
          const summary = await res.json();
          renderRunSummary(summary);
          if (isTerminal(summary.status)) {
            closeEventStream();
            resetStartButton();
            loadHistory();
          }
        } catch {
          // keep trying briefly
        }
      }, 800);
    }
  };
}

function isTerminal(status) {
  return ["submitted", "blocked", "failed", "uncertain_timeout", "duplicate_blocked"].includes(status);
}

function renderRunSummary(summary) {
  $("analysisSection").classList.remove("hidden");
  $("elapsedBadge").classList.remove("hidden");
  $("elapsedBadge").textContent = `${summary.elapsed_ms} ms`;
  $("runFormTitle").textContent = summary.form_title || "Form Analysis";

  $("mTotal").textContent = summary.total_questions;
  $("mMatched").textContent = summary.matched_count;
  $("mFilled").textContent = summary.filled_count;
  $("mMissing").textContent = summary.missing_required_count;
  $("mOptional").textContent = summary.optional_unanswered_count;

  const statusPill = $("runStatusPill");
  const statusMap = {
    running: ["Running", "pill-neutral"],
    paused: ["Needs Input", "pill-yellow"],
    submitted: ["Submitted", "pill-green"],
    blocked: ["Blocked", "pill-red"],
    failed: ["Failed", "pill-red"],
    uncertain_timeout: ["Uncertain", "pill-yellow"],
    duplicate_blocked: ["Duplicate Guard", "pill-yellow"],
  };
  const [label, cls] = statusMap[summary.status] || [summary.status, "pill-neutral"];
  statusPill.textContent = label;
  statusPill.className = `pill ${cls}`;

  // Timeline
  const timeline = $("stageTimeline");
  timeline.innerHTML = "";
  const completedSet = new Set(summary.completed_stages || []);
  STAGES.forEach((st) => {
    const li = document.createElement("li");
    const isDone = completedSet.has(st.key) || (summary.status === "submitted" && st.key === "submission_confirmed");
    const isActive = !isDone && summary.current_stage === st.key;
    li.className = `stage-item${isDone ? " done" : ""}${isActive ? " active" : ""}`;
    li.innerHTML = `<span class="stage-dot"></span><span>${escapeHtml(st.label)}</span>`;
    timeline.appendChild(li);
  });

  // Questions list
  const qList = $("questionsList");
  qList.innerHTML = "";
  (summary.questions || []).forEach((q) => {
    const div = document.createElement("div");
    div.className = `q-card ${q.status}`;
    const badgeCls =
      q.status === "matched" ? "pill-green" : q.status === "uncertain" ? "pill-yellow" : "pill-red";
    const badgeTxt =
      q.status === "matched"
        ? q.filled
          ? "Filled"
          : "Matched"
        : q.status === "uncertain"
        ? "Attention"
        : "Missing";
    const ansDisplay = Array.isArray(q.proposed_answer)
      ? q.proposed_answer.join(", ")
      : q.proposed_answer || "—";

    div.innerHTML = `
      <div class="q-top">
        <span class="q-title">${escapeHtml(q.title)}${q.required ? " *" : ""}</span>
        <span class="pill ${badgeCls}">${badgeTxt}</span>
      </div>
      <div class="q-meta">${escapeHtml(q.question_type.replace("_", " "))} • ${escapeHtml(q.reason || "")}</div>
      <div class="q-answer">Answer: <strong>${escapeHtml(ansDisplay)}</strong></div>
    `;
    qList.appendChild(div);
  });

  // Paused unresolved questions UI
  if (summary.status === "paused") {
    renderPausePanel(summary.unresolved_questions || []);
  } else {
    $("pausePanel").classList.add("hidden");
  }

  // Banner for final/special states
  renderBanner(summary);
}

function renderBanner(summary) {
  const banner = $("resultBanner");
  const title = $("bannerTitle");
  const tag = $("bannerTag");
  const msg = $("bannerMessage");
  const conf = $("bannerConfirmation");
  const actions = $("bannerActions");

  banner.className = "card banner hidden";
  conf.classList.add("hidden");
  actions.classList.add("hidden");
  actions.innerHTML = "";

  if (summary.status === "submitted") {
    banner.className = "card banner success";
    title.textContent = "Submission Confirmed";
    tag.textContent = `${summary.elapsed_ms} ms`;
    tag.className = "pill pill-green";
    msg.textContent = summary.message;
    if (summary.confirmation_text) {
      conf.textContent = summary.confirmation_text;
      conf.classList.remove("hidden");
    }
    banner.classList.remove("hidden");
  } else if (summary.status === "duplicate_blocked") {
    banner.className = "card banner warn";
    title.textContent = "Duplicate Protection";
    tag.textContent = "Blocked";
    tag.className = "pill pill-yellow";
    msg.textContent = summary.message;
    const prevStatus = summary.duplicate_warning?.previous_status;
    if (prevStatus === "submitted") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-secondary btn-sm";
      btn.textContent = "Allow Resubmit Anyway";
      btn.onclick = () => launchRun(lastUrlSubmitted || summary.form_url, true);
      actions.appendChild(btn);
    }
    const openLink = document.createElement("a");
    openLink.href = summary.form_url;
    openLink.target = "_blank";
    openLink.rel = "noopener noreferrer";
    openLink.className = "btn btn-ghost btn-sm";
    openLink.textContent = "Open Form Manually";
    actions.appendChild(openLink);
    actions.classList.remove("hidden");
    banner.classList.remove("hidden");
  } else if (["blocked", "failed", "uncertain_timeout"].includes(summary.status)) {
    banner.className = `card banner ${summary.status === "uncertain_timeout" ? "warn" : "error"}`;
    title.textContent =
      summary.status === "blocked"
        ? "Manual Action Required"
        : summary.status === "uncertain_timeout"
        ? "Submission Uncertain"
        : "Run Stopped";
    tag.textContent = summary.status.replace("_", " ");
    tag.className = `pill ${summary.status === "uncertain_timeout" ? "pill-yellow" : "pill-red"}`;
    msg.textContent = summary.message;

    const openLink = document.createElement("a");
    openLink.href = summary.form_url;
    openLink.target = "_blank";
    openLink.rel = "noopener noreferrer";
    openLink.className = "btn btn-secondary btn-sm";
    openLink.textContent = "Open Form in Browser";
    actions.appendChild(openLink);
    actions.classList.remove("hidden");
    banner.classList.remove("hidden");
  }
}

function renderPausePanel(unresolved) {
  const panel = $("pausePanel");
  const container = $("unresolvedList");
  container.innerHTML = "";

  unresolved.forEach((q) => {
    const box = document.createElement("div");
    box.className = "unresolved-item";
    box.dataset.qid = q.question_id;
    box.dataset.qtype = q.question_type;

    const header = document.createElement("div");
    header.innerHTML = `<strong>${escapeHtml(q.title)} *</strong><p class="muted">${escapeHtml(q.reason)}</p>`;
    box.appendChild(header);

    if (q.candidate_fields && q.candidate_fields.length > 0) {
      const hintWrap = document.createElement("div");
      hintWrap.className = "choice-pills";
      q.candidate_fields.forEach((cf) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "choice-pill";
        chip.textContent = `Use ${cf.field_label}`;
        chip.onclick = () => {
          const inp = box.querySelector("[data-answer-input]");
          if (inp) inp.value = cf.value || "";
        };
        hintWrap.appendChild(chip);
      });
      box.appendChild(hintWrap);
    }

    if (q.question_type === "multiple_choice" && q.options.length > 0) {
      const hiddenInp = document.createElement("input");
      hiddenInp.type = "hidden";
      hiddenInp.setAttribute("data-answer-input", "1");
      box.appendChild(hiddenInp);

      const pills = document.createElement("div");
      pills.className = "choice-pills";
      q.options.forEach((opt) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "choice-pill";
        b.textContent = opt;
        b.onclick = () => {
          pills.querySelectorAll(".choice-pill").forEach((p) => p.classList.remove("selected"));
          b.classList.add("selected");
          hiddenInp.value = opt;
        };
        pills.appendChild(b);
      });
      box.appendChild(pills);
    } else if (q.question_type === "dropdown" && q.options.length > 0) {
      const sel = document.createElement("select");
      sel.setAttribute("data-answer-input", "1");
      sel.required = true;
      const def = document.createElement("option");
      def.value = "";
      def.textContent = "Select an option...";
      sel.appendChild(def);
      q.options.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        sel.appendChild(o);
      });
      box.appendChild(sel);
    } else if (q.question_type === "checkbox" && q.options.length > 0) {
      const cbWrap = document.createElement("div");
      cbWrap.className = "stack";
      q.options.forEach((opt) => {
        const lbl = document.createElement("label");
        lbl.className = "save-toggle";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = opt;
        cb.setAttribute("data-checkbox-opt", "1");
        lbl.appendChild(cb);
        lbl.appendChild(document.createTextNode(opt));
        cbWrap.appendChild(lbl);
      });
      box.appendChild(cbWrap);
    } else if (q.question_type === "paragraph") {
      const ta = document.createElement("textarea");
      ta.rows = 3;
      ta.required = true;
      ta.placeholder = "Enter your answer...";
      ta.setAttribute("data-answer-input", "1");
      box.appendChild(ta);
    } else {
      const inp = document.createElement("input");
      inp.type = "text";
      inp.required = true;
      inp.placeholder = "Enter your answer...";
      inp.setAttribute("data-answer-input", "1");
      box.appendChild(inp);
    }

    const saveRow = document.createElement("label");
    saveRow.className = "save-toggle";
    saveRow.innerHTML = `<input type="checkbox" data-save-profile="1" /> Save this answer to profile for future forms`;
    box.appendChild(saveRow);

    container.appendChild(box);
  });

  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("resumeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!activeRunId) return;

  const items = [];
  document.querySelectorAll("#unresolvedList .unresolved-item").forEach((box) => {
    const qid = box.dataset.qid;
    const qtype = box.dataset.qtype;
    const save = !!box.querySelector("[data-save-profile]")?.checked;
    let answer = "";
    if (qtype === "checkbox") {
      answer = Array.from(box.querySelectorAll("[data-checkbox-opt]:checked")).map((c) => c.value);
      if (answer.length === 0) return;
    } else {
      answer = (box.querySelector("[data-answer-input]")?.value || "").trim();
      if (!answer) return;
    }
    items.push({ question_id: qid, answer, save_to_profile: save });
  });

  if (items.length === 0) return;

  $("resumeSubmitBtn").disabled = true;
  $("resumeSubmitBtn").textContent = "Submitting...";
  try {
    const res = await apiFetch(`/api/runs/${encodeURIComponent(activeRunId)}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: items }),
    });
    const summary = await res.json();
    renderRunSummary(summary);
    if (isTerminal(summary.status)) {
      closeEventStream();
      resetStartButton();
      loadHistory();
    }
  } finally {
    $("resumeSubmitBtn").disabled = false;
    $("resumeSubmitBtn").textContent = "Fill & Submit";
  }
});

$("cancelRunBtn").addEventListener("click", async () => {
  if (!activeRunId) return;
  await apiFetch(`/api/runs/${encodeURIComponent(activeRunId)}/cancel`, { method: "POST" });
  $("pausePanel").classList.add("hidden");
  closeEventStream();
  resetStartButton();
});

// --- Profile Management ---
async function loadProfile() {
  try {
    const res = await apiFetch("/api/profile");
    if (!res.ok) return;
    const p = await res.json();
    $("pFullName").value = p.full_name || "";
    $("pMobile").value = p.mobile_number || "";
    $("pEmail").value = p.email || "";
    $("pTickets").value = p.preferred_ticket_quantity ?? 4;
    $("pCity").value = p.place_city || "";
    $("pAddress").value = p.address_locality || "";
    $("pPin").value = p.pin_code || "";
    renderCustomFields(p.custom_fields || []);
  } catch {
    // wait for unlock
  }
}

function renderCustomFields(fields) {
  const container = $("customFieldsContainer");
  container.innerHTML = "";
  fields.forEach((f) => addCustomFieldRow(f.label, f.value, f.key));
}

function addCustomFieldRow(label = "", value = "", key = "") {
  const row = document.createElement("div");
  row.className = "custom-field-row";
  row.dataset.key = key;
  row.innerHTML = `
    <input type="text" class="cf-label" placeholder="Field label (e.g. Gender)" value="${escapeHtml(label)}" />
    <input type="text" class="cf-value" placeholder="Saved value" value="${escapeHtml(value)}" />
    <button type="button" class="btn btn-ghost btn-sm cf-del">Remove</button>
  `;
  row.querySelector(".cf-del").onclick = () => row.remove();
  $("customFieldsContainer").appendChild(row);
}

$("addCustomFieldBtn").addEventListener("click", () => addCustomFieldRow());

$("profileForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const custom_fields = [];
  document.querySelectorAll("#customFieldsContainer .custom-field-row").forEach((row, idx) => {
    const label = row.querySelector(".cf-label").value.trim();
    const value = row.querySelector(".cf-value").value.trim();
    if (!label) return;
    const key =
      row.dataset.key ||
      label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") ||
      `custom_${idx}`;
    custom_fields.push({ key, label, value, aliases: [label.toLowerCase()] });
  });

  const payload = {
    full_name: $("pFullName").value.trim(),
    mobile_number: $("pMobile").value.trim(),
    email: $("pEmail").value.trim(),
    preferred_ticket_quantity: Number($("pTickets").value),
    place_city: $("pCity").value.trim(),
    address_locality: $("pAddress").value.trim(),
    pin_code: $("pPin").value.trim(),
    custom_fields,
  };

  const res = await apiFetch("/api/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const msg = $("profileMsg");
  if (res.ok) {
    msg.textContent = "Encrypted profile saved.";
    msg.className = "status-msg";
  } else {
    msg.textContent = "Failed to save profile.";
    msg.className = "error-text";
  }
  msg.classList.remove("hidden");
  setTimeout(() => msg.classList.add("hidden"), 3000);
});

$("pinConfigForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("newPinInput").value.trim();
  const res = await apiFetch("/api/auth/pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin: pin || null }),
  });
  const data = await res.json();
  const msg = $("pinConfigMsg");
  if (res.ok) {
    if (data.token) {
      authToken = data.token;
      localStorage.setItem("formrush_token", authToken);
    } else {
      authToken = "";
      localStorage.removeItem("formrush_token");
    }
    $("newPinInput").value = "";
    msg.textContent = data.pin_required ? "Access PIN enabled." : "Access PIN cleared.";
    msg.className = "status-msg";
  } else {
    msg.textContent = data.detail || "Could not update PIN.";
    msg.className = "error-text";
  }
  msg.classList.remove("hidden");
});

// --- History ---
async function loadHistory() {
  try {
    const res = await apiFetch("/api/history?limit=15");
    if (!res.ok) return;
    const entries = await res.json();
    const list = $("historyList");
    if (!entries.length) {
      list.innerHTML = `<p class="muted">No registration attempts yet.</p>`;
      return;
    }
    list.innerHTML = "";
    entries.forEach((h) => {
      const div = document.createElement("div");
      div.className = "history-item";
      const cls =
        h.status === "submitted"
          ? "pill-green"
          : h.status === "paused" || h.status === "uncertain_timeout"
          ? "pill-yellow"
          : "pill-red";
      const ts = new Date(h.updated_at).toLocaleString();
      div.innerHTML = `
        <div class="row-between">
          <strong>${escapeHtml(h.form_title || h.canonical_key)}</strong>
          <span class="pill ${cls}">${escapeHtml(h.status)}</span>
        </div>
        <div class="muted">${escapeHtml(h.message)}</div>
        <div class="q-meta">${h.filled_count}/${h.total_questions} filled • ${h.elapsed_ms} ms • ${escapeHtml(ts)}</div>
      `;
      list.appendChild(div);
    });
  } catch {
    // ignore until unlocked
  }
}

$("refreshHistoryBtn").addEventListener("click", loadHistory);

initApp();
