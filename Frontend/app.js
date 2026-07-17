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
  return payload;
}

function summarizePrompt(toolName, payload) {
  if (toolName === "placement") return payload.question;
  if (toolName === "career") return `Create a ${payload.goal} roadmap for ${payload.degree}, ${payload.year}.`;
  if (toolName === "resume") return `Review my resume for a ${payload.target_role} role.`;
  return `Prepare me for a ${payload.interview_type} ${payload.role} interview.`;
}

forms.forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (requestInFlight) return;

    const toolName = Object.keys(tools).find((key) => tools[key].form === form.id);
    const submitButton = form.querySelector("button[type='submit']");
    const payload = formPayload(form);
    const promptSummary = summarizePrompt(toolName, payload);

    activeTool = toolName;
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
      modelId.value = data.defaults.model_id;
      temperature.value = data.defaults.temperature;
      topP.value = data.defaults.top_p;
      maxTokens.value = data.defaults.max_tokens;
      updateSettingsSummary();
    }
  } catch {
    text.textContent = "Service unavailable";
  }
}

const savedTheme = localStorage.getItem("campuspath-theme");
const preferredTheme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
setTheme(savedTheme || preferredTheme);
updateSettingsSummary();
checkHealth();
