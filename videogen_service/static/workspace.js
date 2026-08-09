// The three-zone workspace: projects/skills/assets on the left, everything the
// machine has generated in the middle, the Agent chat on the right. All calls
// go to this service's own /v1 — the models behind them run on this machine.

const projectSelect = document.getElementById("ws-project");
const projectInfo = document.getElementById("ws-project-info");
const skillList = document.getElementById("ws-skills");
const assetGrid = document.getElementById("ws-assets");
const assetsEmpty = document.getElementById("ws-assets-empty");
const outputsGrid = document.getElementById("ws-outputs");
const outputsHint = document.getElementById("ws-outputs-hint");
const chatSelect = document.getElementById("ws-chat-select");
const messagesBox = document.getElementById("ws-messages");
const inputBox = document.getElementById("ws-input");
const sendButton = document.getElementById("ws-send");

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (error) {
      // A non-JSON error body still reports as the status code.
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, type) {
  if (window.AppUI) AppUI.showToast(message, type || "info");
}

// ---- 左侧:项目 / Skill / 资产 -------------------------------------------------

async function loadProjects(selected) {
  let projects;
  try {
    projects = await request("/v1/projects");
  } catch (error) {
    return;
  }
  const keep = selected ?? projectSelect.value;
  while (projectSelect.options.length > 1) projectSelect.remove(1);
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.project_id;
    option.textContent = project.name;
    projectSelect.append(option);
  }
  if (keep && [...projectSelect.options].some((o) => o.value === keep)) {
    projectSelect.value = keep;
  }
  refreshProjectInfo(projects);
}

function refreshProjectInfo(projects) {
  const current = (projects || []).find(
    (project) => project.project_id === projectSelect.value,
  );
  if (!current) {
    projectInfo.textContent = "";
    return;
  }
  projectInfo.replaceChildren();
  const text = document.createElement("span");
  text.textContent = `${current.draft_count} 稿 · ${current.render_count} 渲染 · `;
  const download = document.createElement("a");
  download.href = `/v1/projects/${current.project_id}/export`;
  download.textContent = "导出";
  projectInfo.append(text, download);
}

async function createProject() {
  const name = window.prompt("项目名称：");
  if (!name || !name.trim()) return;
  try {
    const project = await request("/v1/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadProjects(project.project_id);
    toast("项目已创建", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadSkills() {
  let skills;
  try {
    skills = await request("/v1/skills");
  } catch (error) {
    return;
  }
  skillList.replaceChildren();
  if (!skills.length) {
    const item = document.createElement("li");
    item.className = "subtle";
    item.textContent = "skills/ 目录还没有 Skill。";
    skillList.append(item);
    return;
  }
  for (const skill of skills) {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = skill.name;
    item.append(name);
    if (skill.category) item.append(` · ${skill.category}`);
    if (skill.description) item.title = skill.description;
    skillList.append(item);
  }
}

async function loadAssets() {
  let assets;
  try {
    assets = await request("/v1/assets");
  } catch (error) {
    return;
  }
  assetGrid.replaceChildren();
  assetsEmpty.hidden = assets.length > 0;
  for (const asset of assets) {
    const cell = document.createElement("figure");
    cell.className = "ws-asset";
    const img = document.createElement("img");
    img.src = asset.media_url;
    img.alt = asset.name;
    img.loading = "lazy";
    const caption = document.createElement("figcaption");
    caption.textContent = asset.category
      ? `${asset.name}（${asset.category}）`
      : asset.name;
    cell.append(img, caption);
    cell.title = `资产 id: ${asset.asset_id}`;
    assetGrid.append(cell);
  }
}

// ---- 中间:产物区 --------------------------------------------------------------
// jobs(图片/配音)和 renders(视频)合成一条按时间倒序的流。轮询只在有任务
// 在跑时进行,节奏和生成台一致。

let outputsTimer = null;

async function loadOutputs() {
  let jobs = [];
  let renders = [];
  try {
    [jobs, renders] = await Promise.all([
      request("/v1/jobs"),
      request("/v1/renders"),
    ]);
  } catch (error) {
    outputsHint.textContent = `加载失败：${error.message}`;
    return;
  }
  const entries = [
    ...jobs.map((job) => ({ kind: "job", item: job, created: job.created_at })),
    ...renders.map((render) => ({
      kind: "render",
      item: render,
      created: render.created_at,
    })),
  ].sort((a, b) => (a.created < b.created ? 1 : -1));

  outputsGrid.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有产物。右边告诉 Agent 你想做什么。";
    outputsGrid.append(empty);
  }
  for (const entry of entries) {
    outputsGrid.append(
      entry.kind === "job" ? jobCard(entry.item) : renderCard(entry.item),
    );
  }

  const active = entries.some((entry) =>
    ["QUEUED", "RUNNING"].includes(entry.item.status),
  );
  outputsHint.textContent = active ? "有任务在跑，自动刷新中…" : "";
  if (active && outputsTimer === null) {
    outputsTimer = setInterval(loadOutputs, 3000);
  } else if (!active && outputsTimer !== null) {
    clearInterval(outputsTimer);
    outputsTimer = null;
  }
}

function baseCard(title, status) {
  const meta = window.AppUI
    ? AppUI.statusMeta(status)
    : { label: status, tone: "neutral" };
  const card = document.createElement("article");
  card.className = "media-card";
  const top = document.createElement("div");
  top.className = "media-card-top";
  const label = document.createElement("span");
  label.className = "subtle";
  label.textContent = title;
  const badge = document.createElement("span");
  badge.className = `status-badge tone-${meta.tone}`;
  badge.textContent = meta.label;
  top.append(label, badge);
  card.append(top);
  return card;
}

function promptLine(card, text) {
  if (!text) return;
  const prompt = document.createElement("p");
  prompt.className = "prompt";
  prompt.textContent = text;
  card.append(prompt);
}

function errorLine(card, text) {
  if (!text) return;
  const error = document.createElement("p");
  error.className = "notice error";
  error.style.margin = "8px 12px 0";
  error.textContent = text;
  card.append(error);
}

function jobCard(job) {
  const kindLabel =
    { t2i: "图片", tts: "配音", compose: "成片" }[job.kind] || job.kind;
  const card = baseCard(`${kindLabel} · ${job.capability}`, job.status);
  promptLine(
    card,
    job.prompt || job.text || (job.render_id ? `来自渲染 ${job.render_id}` : ""),
  );
  errorLine(card, job.error);
  if (job.status === "DONE" && job.media_url) {
    if (job.kind === "t2i") {
      const img = document.createElement("img");
      img.src = job.media_url;
      img.alt = job.prompt || job.job_id;
      img.className = "preview";
      img.loading = "lazy";
      card.append(img);
    } else if (job.kind === "compose") {
      const video = document.createElement("video");
      video.src = job.media_url;
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.className = "preview";
      card.append(video);
    } else {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.src = job.media_url;
      audio.style.margin = "8px 12px";
      audio.style.width = "calc(100% - 24px)";
      card.append(audio);
    }
  }
  const footer = document.createElement("div");
  footer.className = "media-card-footer";
  const actions = document.createElement("div");
  actions.className = "review-actions";
  if (job.status === "DONE" && job.kind === "t2i") {
    const save = document.createElement("button");
    save.type = "button";
    save.className = "button button-secondary";
    save.textContent = "存为资产";
    save.addEventListener("click", async () => {
      const name = window.prompt("资产名称：", (job.prompt || "").slice(0, 20));
      if (!name || !name.trim()) return;
      try {
        await request(`/v1/jobs/${job.job_id}/save-asset`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim() }),
        });
        toast("已存入资产中心", "success");
        await loadAssets();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    actions.append(save);
  }
  if (job.status === "FAILED") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "button button-primary";
    retry.textContent = "重试";
    retry.addEventListener("click", async () => {
      try {
        await request(`/v1/jobs/${job.job_id}/retry`, { method: "POST" });
        await loadOutputs();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    actions.append(retry);
  }
  if (job.status === "DONE" && job.kind === "compose" && job.media_url) {
    const download = document.createElement("a");
    download.className = "button button-secondary";
    download.href = job.media_url;
    download.download = `${job.job_id}.mp4`;
    download.textContent = "下载成片";
    actions.append(download);
  }
  if (["DONE", "FAILED"].includes(job.status)) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button button-ghost";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      const ok = window.AppUI
        ? await AppUI.confirmDanger({
            title: "确认删除",
            message: "删除这条任务及其产物？",
            confirmLabel: "删除",
          })
        : window.confirm("删除这条任务及其产物？");
      if (!ok) return;
      try {
        await request(`/v1/jobs/${job.job_id}`, { method: "DELETE" });
        await loadOutputs();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    actions.append(remove);
  }
  if (actions.childElementCount) footer.append(actions);
  card.append(footer);
  return card;
}

function renderCard(render) {
  const card = baseCard(`视频 · ${render.mode}`, render.status);
  promptLine(card, render.prompt);
  errorLine(card, render.error);
  if (render.status === "DONE" && render.media_url) {
    const video = document.createElement("video");
    video.src = render.media_url;
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.className = "preview";
    card.append(video);
  }
  const footer = document.createElement("div");
  footer.className = "media-card-footer";
  const actions = document.createElement("div");
  actions.className = "review-actions";
  if (render.status === "DONE" && render.media_url) {
    const download = document.createElement("a");
    download.className = "button button-secondary";
    download.href = render.media_url;
    download.download = `${render.render_id}.mp4`;
    download.textContent = "下载 MP4";
    actions.append(download);
  }
  if (render.status === "FAILED") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "button button-primary";
    retry.textContent = "重试";
    retry.addEventListener("click", async () => {
      try {
        await request(`/v1/renders/${render.render_id}/retry`, {
          method: "POST",
        });
        await loadOutputs();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    actions.append(retry);
  }
  if (actions.childElementCount) footer.append(actions);
  card.append(footer);
  return card;
}

// ---- 右侧:Agent 对话 ----------------------------------------------------------

let sending = false;

async function loadChats(selected) {
  let chats;
  try {
    chats = await request("/v1/chats");
  } catch (error) {
    return;
  }
  const keep = selected ?? chatSelect.value;
  while (chatSelect.options.length > 1) chatSelect.remove(1);
  for (const chat of chats) {
    const option = document.createElement("option");
    option.value = chat.chat_id;
    option.textContent = chat.title;
    chatSelect.append(option);
  }
  if (keep && [...chatSelect.options].some((o) => o.value === keep)) {
    chatSelect.value = keep;
  }
}

async function refreshMessages() {
  const chatId = chatSelect.value;
  if (!chatId) {
    messagesBox.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent =
      "新建一个对话开始创作。Agent 会写分镜、排图片/视频/配音任务，全在本机执行。";
    messagesBox.append(empty);
    return;
  }
  try {
    const record = await request(`/v1/chats/${chatId}`);
    renderMessages(record.messages);
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderMessages(messages) {
  messagesBox.replaceChildren();
  for (const message of messages) {
    for (const node of messageNodes(message)) messagesBox.append(node);
  }
  messagesBox.scrollTop = messagesBox.scrollHeight;
}

function bubble(className, text) {
  const node = document.createElement("div");
  node.className = `ws-msg ${className}`;
  node.textContent = text;
  return node;
}

function messageNodes(message) {
  const nodes = [];
  if (message.role === "user") {
    nodes.push(bubble("ws-msg-user", message.content));
    return nodes;
  }
  if (message.role === "tool") {
    let text = message.content;
    try {
      const payload = JSON.parse(message.content);
      text = payload.error
        ? `出错：${payload.error}`
        : Object.entries(payload)
            .map(([key, value]) =>
              `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`,
            )
            .join(" · ");
    } catch (error) {
      // 非 JSON 的工具输出按原文显示。
    }
    const node = bubble("ws-msg-tool", `↳ ${message.tool_name}: ${text}`);
    node.title = message.content;
    nodes.push(node);
    return nodes;
  }
  // assistant: 文本气泡 + 每个工具调用一行。
  if (message.content) nodes.push(bubble("ws-msg-assistant", message.content));
  for (const call of message.tool_calls || []) {
    const fn = call.function || {};
    const args =
      typeof fn.arguments === "string"
        ? fn.arguments
        : JSON.stringify(fn.arguments || {});
    const node = bubble(
      "ws-msg-tool",
      `🔧 ${fn.name}(${args.length > 120 ? `${args.slice(0, 120)}…` : args})`,
    );
    node.title = args;
    nodes.push(node);
  }
  return nodes;
}

async function createChat() {
  try {
    const chat = await request("/v1/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    await loadChats(chat.chat_id);
    await refreshMessages();
    inputBox.focus();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function sendMessage() {
  if (sending) return;
  const text = inputBox.value.trim();
  if (!text) return;
  let chatId = chatSelect.value;
  try {
    if (!chatId) {
      const chat = await request("/v1/chats", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      chatId = chat.chat_id;
      await loadChats(chatId);
    }
    sending = true;
    sendButton.disabled = true;
    inputBox.value = "";
    // 立即回显用户消息和思考占位,Ollama 一轮可能要几分钟。
    messagesBox.querySelector(":scope > .empty")?.remove();
    messagesBox.append(bubble("ws-msg-user", text));
    const thinking = bubble("ws-msg-assistant ws-msg-thinking", "Agent 思考中…");
    messagesBox.append(thinking);
    messagesBox.scrollTop = messagesBox.scrollHeight;

    const record = await request(`/v1/chats/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    renderMessages(record.messages);
    await loadChats(chatId);
    // Agent 可能刚排了任务,产物区立刻跟上。
    await loadOutputs();
  } catch (error) {
    toast(error.message, "error");
    await refreshMessages();
  } finally {
    sending = false;
    sendButton.disabled = false;
    inputBox.focus();
  }
}

// ---- 装配 ---------------------------------------------------------------------

projectSelect.addEventListener("change", () => loadProjects());
document.getElementById("ws-project-new").addEventListener("click", createProject);
chatSelect.addEventListener("change", refreshMessages);
document.getElementById("ws-chat-new").addEventListener("click", createChat);
sendButton.addEventListener("click", sendMessage);
inputBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});

async function start() {
  await Promise.all([loadProjects(), loadSkills(), loadAssets(), loadChats()]);
  await refreshMessages();
  await loadOutputs();
}

start();
