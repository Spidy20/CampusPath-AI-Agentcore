const tools = {
  placement: {
    number: "TOOL 01",
    title: "Ask your placement coach",
    chatTitle: "Placement Coach",
    description: "Get direct answers about placement strategy, eligibility, preparation, and company rounds.",
    form: "placementForm",
    endpoint: "/api/placement-doubt",
  },
  career: {
    number: "TOOL 02",
    title: "Design your career roadmap",
    chatTitle: "Career Roadmap",
    description: "Turn your target role, current skills, and available time into a measurable learning plan.",
    form: "careerForm",
    endpoint: "/api/career-roadmap",
  },
  resume: {
    number: "TOOL 03",
    title: "Sharpen your resume",
    chatTitle: "Resume Review",
    description: "Get recruiter-style feedback, ATS improvements, and stronger bullet-point examples.",
    form: "resumeForm",
    endpoint: "/api/resume-review",
  },
  interview: {
    number: "TOOL 04",
    title: "Prepare with purpose",
    chatTitle: "Interview Prep",
    description: "Generate role-specific questions, revision topics, and a focused interview practice plan.",
    form: "interviewForm",
    endpoint: "/api/interview-prep",
  },
};

const tabs = document.querySelectorAll(".tab");
const forms = document.querySelectorAll(".tool-form");
const chatThread = document.querySelector("#chatThread");
const chatPanel = document.querySelector("#chatPanel");
const copyButton = document.querySelector("#copyButton");
const clearButton = document.querySelector("#clearButton");
const themeToggle = document.querySelector("#themeToggle");
const modelId = document.querySelector("#modelId");
const temperature = document.querySelector("#temperature");
const topP = document.querySelector("#topP");
const maxTokens = document.querySelector("#maxTokens");
const histories = Object.fromEntries(Object.keys(tools).map((key) => [key, []]));

let activeTool = "placement";
let latestAnswer = "";
let requestInFlight = false;
const uploaders = {};
const busyUploaders = new Set();

// Per-tool default models (resume workflows use Claude 3.7 Sonnet). The select
// follows the active tab until the user picks a model themselves.
let baseDefaultModel = modelId.value;
const toolModelDefaults = {
  resume: "apac.anthropic.claude-3-7-sonnet-20250219-v1:0",
  interview: "apac.anthropic.claude-3-7-sonnet-20250219-v1:0",
};
let userPinnedModel = false;
modelId.addEventListener("change", () => { userPinnedModel = true; });

function applyToolModelDefault() {
  if (userPinnedModel) return;
  const wanted = toolModelDefaults[activeTool] || baseDefaultModel;
  if ([...modelId.options].some((option) => option.value === wanted)) {
    modelId.value = wanted;
    updateSettingsSummary();
  }
}

function welcomeMarkup(toolName) {
  return `
    <div class="chat-welcome">
      <div class="orbit"><span>✦</span></div>
      <h3>${escapeHtml(tools[toolName].chatTitle)}</h3>
      <p>${escapeHtml(tools[toolName].description)}</p>
    </div>`;
}

function renderHistory() {
  const history = histories[activeTool];
  const lastAssistant = [...history].reverse().find((message) => message.role === "assistant");
  latestAnswer = lastAssistant?.content || "";
  chatThread.innerHTML = history.length
    ? history.map(renderSavedMessage).join("")
    : welcomeMarkup(activeTool);
  copyButton.hidden = !history.some((message) => message.role === "assistant");
  scrollChat();
}

function renderSavedMessage(message) {
  if (message.role === "user") {
    return userBubbleMarkup(message.content);
  }
  if (message.role === "error") {
    return `<div class="message-row assistant-row"><div class="avatar">C</div><div class="message error-message">${escapeHtml(message.content)}</div></div>`;
  }
  return assistantBubbleMarkup(message.content, message.meta);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    if (requestInFlight) return;
    activeTool = tab.dataset.tool;
    const tool = tools[activeTool];
    tabs.forEach((item) => item.classList.toggle("active", item === tab));
    forms.forEach((form) => form.classList.toggle("active", form.id === tool.form));
    document.querySelector("#toolNumber").textContent = tool.number;
    document.querySelector("#toolTitle").textContent = tool.title;
    document.querySelector("#toolDescription").textContent = tool.description;
    document.querySelector("#chatSubtitle").textContent = tool.chatTitle;
    applyToolModelDefault();
    renderHistory();
  });
});

function modelParams() {
  return {
    model_id: modelId.value,
    temperature: Number(temperature.value),
    top_p: Number(topP.value),
    max_tokens: Number(maxTokens.value),
  };
}

function updateSettingsSummary() {
  const modelName = modelId.options[modelId.selectedIndex].text.split(" (")[0];
  document.querySelector("#temperatureValue").textContent = temperature.value;
  document.querySelector("#topPValue").textContent = topP.value;
  document.querySelector("#settingsSummary").textContent =
    `${modelName} · T ${temperature.value} · Top P ${topP.value} · ${maxTokens.value} tokens`;
}

[modelId, temperature, topP, maxTokens].forEach((control) => {
  control.addEventListener("input", updateSettingsSummary);
  control.addEventListener("change", updateSettingsSummary);
});

function formPayload(form) {
  const payload = { ...Object.fromEntries(new FormData(form).entries()), ...modelParams() };
  if (payload.hours_per_week) payload.hours_per_week = Number(payload.hours_per_week);
  if (!payload.interview_date) delete payload.interview_date;
  return payload;
}

function summarizePrompt(toolName, payload) {
  if (toolName === "placement") return payload.question;
  if (toolName === "career") return `Create a ${payload.goal} roadmap for ${payload.degree}, ${payload.year}.`;
  if (toolName === "resume") return `Review my resume for a ${payload.target_role} role.`;
  const dateSuffix = payload.interview_date
    ? ` on ${new Date(`${payload.interview_date}T00:00:00`).toLocaleDateString(undefined, {
        day: "numeric", month: "long", year: "numeric",
      })}`
    : "";
  return `Prepare me for a ${payload.interview_type} ${payload.role} interview${dateSuffix}.`;
}

function showBlockedMessage(toolName, text) {
  activeTool = toolName;
  histories[toolName].push({ role: "error", content: text });
  renderHistory();
}

forms.forEach((form) => {
  // Handle validation ourselves so the submit handler always runs and can
  // always give visible feedback; native validation can block silently.
  form.noValidate = true;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (requestInFlight) return;

    const toolName = Object.keys(tools).find((key) => tools[key].form === form.id);

    // Cross-browser validation with feedback that is impossible to miss:
    // native bubble when supported, plus a message in the chat thread.
    if (!form.checkValidity()) {
      form.reportValidity?.();
      const invalid = form.querySelector(":invalid");
      const fieldLabel = invalid?.closest("label")?.firstChild?.textContent?.trim()
        || invalid?.name || "a required field";
      showBlockedMessage(toolName, `Please fill in "${fieldLabel}" before submitting.`);
      invalid?.focus();
      return;
    }
    if (busyUploaders.size) {
      uploaders[toolName]?.setHint("Please wait — your PDF is still being processed.", true);
      showBlockedMessage(toolName, "Your PDF is still being processed. Try again in a moment.");
      return;
    }
    const submitButton = form.querySelector("button[type='submit']");
    const payload = formPayload(form);
    if (toolName === "resume" && String(payload.resume_text || "").trim().length < 50) {
      uploaders.resume?.setHint("Add at least 50 characters of resume text, or upload a PDF first.", true);
      showBlockedMessage(toolName, "Your resume text needs at least 50 characters. Upload a PDF or paste more content.");
      return;
    }
    if (toolName === "interview" && !String(payload.resume_text || "").trim()) {
      delete payload.resume_text;
    }
    const promptSummary = summarizePrompt(toolName, payload);

    activeTool = toolName;
    uploaders[toolName]?.settle();
    histories[toolName].push({ role: "user", content: promptSummary });
    renderHistory();
    const loadingNode = appendLoadingMessage();
    requestInFlight = true;
    submitButton.disabled = true;
    setTabsDisabled(true);

    if (window.innerWidth < 980) {
      chatPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    try {
      const response = await fetch(tools[toolName].endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        const detail = Array.isArray(data.detail)
          ? data.detail.map((item) => item.msg).join(", ")
          : data.detail;
        throw new Error(detail || "The request could not be completed.");
      }

      clearInterval(loadingNode.stageTimer);
      loadingNode.remove();
      latestAnswer = data.answer;
      const message = { role: "assistant", content: data.answer, meta: data };
      histories[toolName].push(message);
      await streamAssistantMessage(message);
      copyButton.hidden = false;
    } catch (error) {
      clearInterval(loadingNode.stageTimer);
      loadingNode.remove();
      histories[toolName].push({ role: "error", content: error.message });
      renderHistory();
    } finally {
      requestInFlight = false;
      submitButton.disabled = false;
      setTabsDisabled(false);
    }
  });
});

function userBubbleMarkup(content) {
  return `
    <div class="message-row user-row">
      <div class="message user-message">
        <span class="message-label">You</span>
        <p>${escapeHtml(content)}</p>
      </div>
      <div class="avatar user-avatar">Y</div>
    </div>`;
}

function assistantBubbleMarkup(content, meta) {
  return `
    <div class="message-row assistant-row">
      <div class="avatar">C</div>
      <div class="message assistant-message">
        <span class="message-label">CampusPath AI</span>
        <div class="markdown-body">${renderMarkdown(content)}</div>
        ${telemetryMarkup(meta)}
      </div>
    </div>`;
}

function appendLoadingMessage() {
  const node = document.createElement("div");
  node.className = "message-row assistant-row loading-message-row";
  node.innerHTML = `
    <div class="avatar thinking-avatar">C</div>
    <div class="message assistant-message thinking-message">
      <span class="message-label">CampusPath AI</span>
      <div class="thinking-line"><span></span><span></span><span></span><em>Connecting to Amazon Bedrock…</em></div>
    </div>`;
  chatThread.appendChild(node);
  scrollChat();

  const stages = ["Reasoning through your context…", "Building an actionable response…", "Checking the final guidance…"];
  let index = 0;
  node.stageTimer = setInterval(() => {
    const label = node.querySelector("em");
    if (label) label.textContent = stages[index++ % stages.length];
  }, 1400);
  return node;
}

async function streamAssistantMessage(message) {
  const node = document.createElement("div");
  node.className = "message-row assistant-row";
  node.innerHTML = `
    <div class="avatar">C</div>
    <div class="message assistant-message">
      <span class="message-label">CampusPath AI <i class="streaming-dot"></i></span>
      <div class="markdown-body streaming-content"></div>
      <div class="telemetry-slot"></div>
    </div>`;
  chatThread.appendChild(node);

  const output = node.querySelector(".streaming-content");
  const words = message.content.split(/(\s+)/);
  const batchSize = Math.max(1, Math.ceil(words.length / 100));
  let cursor = 0;

  await new Promise((resolve) => {
    const timer = setInterval(() => {
      cursor = Math.min(words.length, cursor + batchSize);
      output.innerHTML = renderMarkdown(words.slice(0, cursor).join(""));
      scrollChat();
      if (cursor >= words.length) {
        clearInterval(timer);
        resolve();
      }
    }, 22);
  });

  node.querySelector(".streaming-dot").remove();
  node.querySelector(".telemetry-slot").innerHTML = telemetryMarkup(message.meta);
  scrollChat();
}

function telemetryMarkup(meta) {
  if (!meta) return "";
  const usage = meta.usage || {};
  const cost = meta.cost || {};
  const params = meta.model_params || {};
  const modelName = shortModelName(params.model_id);
  return `
    <div class="response-meta">
      <div class="meta-primary">
        <span title="${escapeHtml(params.model_id || "")}"><b>◈</b> ${escapeHtml(modelName)}</span>
        <span><b>◷</b> ${formatLatency(meta.latency_ms)}</span>
        <span><b>◇</b> ${formatCost(cost.total_cost)}</span>
      </div>
      <details>
        <summary>Usage details</summary>
        <div class="usage-grid">
          <span><small>Input</small><strong>${usage.input_tokens ?? 0}</strong> tokens</span>
          <span><small>Output</small><strong>${usage.output_tokens ?? 0}</strong> tokens</span>
          <span><small>Total</small><strong>${usage.total_tokens ?? 0}</strong> tokens</span>
          <span><small>Settings</small><strong>T ${params.temperature ?? "—"}</strong> · P ${params.top_p ?? "—"} · ${params.max_tokens ?? "—"} max</span>
        </div>
        <p>${escapeHtml(cost.pricing_note || "Cost is an approximate estimate.")}</p>
      </details>
    </div>`;
}

function renderMarkdown(value) {
  const rendered = window.marked ? marked.parse(value) : escapeHtml(value);
  return window.DOMPurify ? DOMPurify.sanitize(rendered) : escapeHtml(value);
}

function shortModelName(value = "") {
  if (value.includes("nova-2-lite")) return "Nova 2 Lite";
  if (value.includes("nova-micro")) return "Nova Micro";
  if (value.includes("nova-lite")) return "Nova Lite";
  if (value.includes("nova-pro")) return "Nova Pro";
  if (value.includes("claude-3-7-sonnet")) return "Claude 3.7 Sonnet";
  if (value.includes("claude-3-5-sonnet")) return "Claude 3.5 Sonnet";
  if (value.includes("claude-3-haiku")) return "Claude 3 Haiku";
  if (value.includes("claude-haiku")) return "Claude Haiku";
  if (value.includes("claude")) return "Claude Sonnet";
  return value || "Bedrock";
}

function formatLatency(milliseconds) {
  if (!Number.isFinite(Number(milliseconds))) return "—";
  return Number(milliseconds) >= 1000
    ? `${(Number(milliseconds) / 1000).toFixed(2)}s`
    : `${Number(milliseconds).toFixed(0)}ms`;
}

function formatCost(value) {
  if (!Number.isFinite(Number(value))) return "—";
  if (Number(value) === 0) return "$0.000000";
  return `$${Number(value).toFixed(6)}`;
}

function setTabsDisabled(disabled) {
  tabs.forEach((tab) => { tab.disabled = disabled; });
}

function scrollChat() {
  requestAnimationFrame(() => {
    chatThread.scrollTo({ top: chatThread.scrollHeight, behavior: "smooth" });
  });
}

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(latestAnswer);
  copyButton.textContent = "Copied";
  setTimeout(() => { copyButton.textContent = "Copy last"; }, 1500);
});

clearButton.addEventListener("click", () => {
  histories[activeTool] = [];
  latestAnswer = "";
  renderHistory();
});

document.querySelector("#placementForm textarea[name='question']").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    event.currentTarget.form.requestSubmit();
  }
});

const interviewDateInput = document.querySelector("#interviewForm input[name='interview_date']");
if (interviewDateInput) {
  const today = new Date();
  const localToday = [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, "0"),
    String(today.getDate()).padStart(2, "0"),
  ].join("-");
  interviewDateInput.min = localToday;
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("campuspath-theme", theme);
  themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  themeToggle.querySelector(".theme-icon").textContent = theme === "dark" ? "☀" : "◐";
}

themeToggle.addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

async function checkHealth() {
  const status = document.querySelector(".status");
  const text = document.querySelector("#statusText");
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error();
    const data = await response.json();
    status.classList.add("online");
    text.textContent = "AI service online";
    if (data.defaults) {
      if (![...modelId.options].some((option) => option.value === data.defaults.model_id)) {
        modelId.add(new Option(data.defaults.model_id, data.defaults.model_id));
      }
      baseDefaultModel = data.defaults.model_id;
      modelId.value = data.defaults.model_id;
      temperature.value = data.defaults.temperature;
      topP.value = data.defaults.top_p;
      maxTokens.value = data.defaults.max_tokens;
      if (data.route_model_defaults) {
        toolModelDefaults.resume = data.route_model_defaults["resume-review"] || toolModelDefaults.resume;
        toolModelDefaults.interview = data.route_model_defaults["interview-prep"] || toolModelDefaults.interview;
      }
      applyToolModelDefault();
      updateSettingsSummary();
    }
  } catch {
    text.textContent = "Service unavailable";
  }
}

// ---------- Reusable PDF uploader (Resume Review + Interview Prep) ----------

// Stop the browser from opening dropped files in a new tab, no matter where
// on the page the file lands.
["dragover", "drop"].forEach((type) => {
  window.addEventListener(type, (event) => event.preventDefault());
});

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function createPdfUploader(toolName, formId, { requiredChars = 0, reviewLabel = "review", roleField = "" } = {}) {
  const form = document.getElementById(formId);
  const root = form?.querySelector("[data-uploader]");
  if (!form || !root) return null;
  const roleInput = roleField ? form.querySelector(`input[name='${roleField}']`) : null;

  const dropzone = root.querySelector("[data-dropzone]");
  const fileInput = root.querySelector("[data-file-input]");
  const browseButton = root.querySelector("[data-browse]");
  const fileCard = root.querySelector("[data-file-card]");
  const fileNameNode = root.querySelector("[data-file-name]");
  const fileDetails = root.querySelector("[data-file-details]");
  const extractButton = root.querySelector("[data-extract]");
  const removeButton = root.querySelector("[data-remove]");
  const progress = root.querySelector("[data-progress]");
  const progressBar = root.querySelector("[data-progress-bar]");
  const progressLabel = root.querySelector("[data-progress-label]");
  const previewShell = root.querySelector("[data-preview-shell]");
  const preview = root.querySelector("[data-preview]");
  const previewMeta = root.querySelector("[data-preview-meta]");
  const textarea = form.querySelector("textarea[name='resume_text']");
  const hint = form.querySelector("[data-upload-hint]");
  const charCount = form.querySelector("[data-char-count]");
  const submitButton = form.querySelector("button[type='submit']");
  const idleHint = hint?.textContent || "";

  let file = null;
  let previewUrl = "";
  let extracting = false;

  function setHint(message, isError = false) {
    if (!hint) return;
    hint.textContent = message;
    hint.classList.toggle("error", Boolean(isError));
  }

  function updateCharCount() {
    if (!charCount || !textarea) return;
    const length = textarea.value.length;
    charCount.hidden = length === 0;
    charCount.textContent = `${length.toLocaleString()} / 20,000`;
    charCount.classList.toggle("warn", requiredChars > 0 && length > 0 && length < requiredChars);
  }

  function setProgress(step, label, percent, failed = false) {
    progress.hidden = false;
    progress.classList.toggle("failed", failed);
    progress.classList.toggle("complete", step === "ready" && !failed);
    progressLabel.textContent = label;
    progressBar.style.width = `${percent}%`;
    progress.querySelectorAll("li").forEach((item) => {
      const itemStep = item.dataset.step;
      item.classList.toggle("active", itemStep === step);
      item.classList.toggle(
        "done",
        (step === "extract" && itemStep === "upload")
          || (step === "ready" && (itemStep === "upload" || itemStep === "extract")),
      );
    });
  }

  function clearPreviewUrl() {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = "";
    }
  }

  function settle() {
    // Called when the review request starts: the extraction phase is over,
    // so retire the progress indicator and hand focus to the chat panel.
    progress.hidden = true;
    progress.classList.remove("failed", "complete");
  }

  function reset({ keepText = true } = {}) {
    file = null;
    extracting = false;
    busyUploaders.delete(api);
    fileInput.value = "";
    clearPreviewUrl();
    fileCard.hidden = true;
    progress.hidden = true;
    progress.classList.remove("failed", "complete");
    previewShell.hidden = true;
    preview.removeAttribute("src");
    dropzone.classList.remove("is-busy", "has-file");
    extractButton.disabled = false;
    extractButton.textContent = "Extract text";
    if (submitButton) submitButton.disabled = false;
    if (!keepText && textarea) textarea.value = "";
    updateCharCount();
    setHint(idleHint);
  }

  function accept(candidate) {
    if (!candidate || extracting) return;
    const isPdf = candidate.type === "application/pdf" || candidate.name.toLowerCase().endsWith(".pdf");
    if (!isPdf) {
      setHint("Only PDF files are supported for upload.", true);
      return;
    }
    if (candidate.size > 5 * 1024 * 1024) {
      setHint("Resume PDFs must be 5 MB or smaller.", true);
      return;
    }

    file = candidate;
    clearPreviewUrl();
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    previewShell.hidden = false;
    previewMeta.textContent = `${formatBytes(file.size)} · local preview`;
    fileCard.hidden = false;
    fileNameNode.textContent = file.name;
    fileDetails.textContent = `${formatBytes(file.size)} · waiting to extract`;
    dropzone.classList.add("has-file");
    progress.hidden = true;
    progress.classList.remove("failed");
    extractButton.textContent = "Extract text";
    setHint("PDF selected. Extracting selectable text…");
    extract();
  }

  async function extract() {
    if (!file || extracting || requestInFlight) return;

    extracting = true;
    busyUploaders.add(api);
    extractButton.disabled = true;
    extractButton.textContent = "Extracting…";
    if (submitButton) submitButton.disabled = true;
    dropzone.classList.add("is-busy");
    fileDetails.textContent = `${formatBytes(file.size)} · extracting…`;
    setTabsDisabled(true);
    setProgress("upload", "Uploading your PDF securely to S3…", 28);

    const body = new FormData();
    body.append("file", file, file.name);

    const stageTimer = setTimeout(() => {
      setProgress("extract", "Lambda is reading selectable text from your PDF…", 68);
    }, 700);

    try {
      const response = await fetch("/api/resume-extract", { method: "POST", body });
      let data = {};
      try {
        data = await response.json();
      } catch {
        throw new Error("The extraction service returned an unexpected response. Is the backend running?");
      }
      if (!response.ok) {
        const detail = Array.isArray(data.detail)
          ? data.detail.map((item) => item.msg || item).join(", ")
          : data.detail;
        throw new Error(detail || "PDF extraction failed.");
      }

      textarea.value = data.resume_text || "";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      updateCharCount();
      textarea.classList.remove("flash");
      void textarea.offsetWidth; // restart the highlight animation
      textarea.classList.add("flash");

      let roleNote = "";
      if (roleInput && !roleInput.value.trim() && data.suggested_role) {
        roleInput.value = data.suggested_role;
        roleInput.classList.remove("flash");
        void roleInput.offsetWidth;
        roleInput.classList.add("flash");
        roleNote = ` Target role set to “${data.suggested_role}” from your resume — change it if needed.`;
      }

      const chars = (data.character_count || textarea.value.length).toLocaleString();
      fileDetails.textContent = `${data.page_count || 0} page(s) · ${chars} chars extracted`;
      previewMeta.textContent = `${formatBytes(file.size)} · ${data.page_count || 0} page(s) extracted`;
      extractButton.textContent = "Re-extract";
      setProgress("ready", "Extraction complete. Review or edit the text below.", 100);

      const warningText = (data.warnings || []).filter(Boolean).join(" ");
      setHint(
        (warningText
          || `Extracted ${chars} characters in ${formatLatency(data.latency_ms)}. Edit freely, then ${reviewLabel}.`)
          + roleNote,
        Boolean(warningText),
      );
      textarea.scrollIntoView({ behavior: "smooth", block: "nearest" });
      textarea.focus({ preventScroll: true });
    } catch (error) {
      extractButton.textContent = "Retry extraction";
      fileDetails.textContent = `${formatBytes(file.size)} · extraction failed`;
      const offline = error instanceof TypeError;
      setProgress("upload", offline ? "Could not reach the server." : "Extraction failed.", 12, true);
      setHint(
        (offline ? "Could not reach the backend. Check that the server is running. " : "")
          + (error.message || "PDF extraction failed.")
          + " You can retry, or paste your resume text manually below.",
        true,
      );
    } finally {
      clearTimeout(stageTimer);
      extracting = false;
      busyUploaders.delete(api);
      extractButton.disabled = false;
      if (submitButton) submitButton.disabled = false;
      dropzone.classList.remove("is-busy");
      setTabsDisabled(false);
    }
  }

  // The file input sits inside the browse <label>, which opens the picker
  // natively. Ignore those clicks here so we don't trigger the picker twice,
  // and open it programmatically only for clicks on the rest of the zone.
  dropzone.addEventListener("click", (event) => {
    if (event.target === fileInput || event.target.closest("[data-browse]")) return;
    if (!extracting) fileInput.click();
  });
  browseButton.addEventListener("click", (event) => {
    event.stopPropagation();
    if (extracting) event.preventDefault();
  });
  fileInput.addEventListener("click", (event) => event.stopPropagation());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!extracting) fileInput.click();
    }
  });
  ["dragenter", "dragover"].forEach((type) => {
    dropzone.addEventListener(type, () => dropzone.classList.add("drag-active"));
  });
  ["dragleave", "drop"].forEach((type) => {
    dropzone.addEventListener(type, () => dropzone.classList.remove("drag-active"));
  });
  fileInput.addEventListener("change", () => accept(fileInput.files?.[0]));
  extractButton.addEventListener("click", extract);
  removeButton.addEventListener("click", () => reset({ keepText: true }));
  textarea?.addEventListener("input", updateCharCount);

  const api = {
    accept,
    setHint,
    settle,
    get busy() { return extracting; },
  };
  uploaders[toolName] = api;
  return api;
}

createPdfUploader("resume", "resumeForm", {
  requiredChars: 50,
  reviewLabel: "click “Review my resume”",
  roleField: "target_role",
});
createPdfUploader("interview", "interviewForm", {
  reviewLabel: "click “Generate prep pack”",
  roleField: "role",
});

// A file dropped anywhere on the page goes to the active tool's uploader,
// with a page-wide visual cue while dragging.
let windowDragDepth = 0;
window.addEventListener("dragenter", (event) => {
  if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
  windowDragDepth += 1;
  if (uploaders[activeTool]) document.body.classList.add("dragging-file");
});
window.addEventListener("dragleave", () => {
  windowDragDepth = Math.max(0, windowDragDepth - 1);
  if (!windowDragDepth) document.body.classList.remove("dragging-file");
});
window.addEventListener("drop", (event) => {
  windowDragDepth = 0;
  document.body.classList.remove("dragging-file");
  const uploader = uploaders[activeTool];
  const droppedFile = event.dataTransfer?.files?.[0];
  if (uploader && droppedFile) uploader.accept(droppedFile);
});

// Surface unexpected script errors in the chat thread instead of failing
// silently — nothing should ever look like "the button did nothing".
window.addEventListener("error", (event) => {
  try {
    histories[activeTool].push({
      role: "error",
      content: `Something went wrong in the page: ${event.message}. Refresh and try again.`,
    });
    renderHistory();
  } catch { /* rendering is unavailable; nothing more we can do */ }
});

const savedTheme = localStorage.getItem("campuspath-theme");
const preferredTheme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
setTheme(savedTheme || preferredTheme);
updateSettingsSummary();
checkHealth();
