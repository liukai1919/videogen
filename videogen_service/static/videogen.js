// Drives the MiniMax H3 generation bench.
// Moved over from videotube: the page now talks to this service's own /v1 seam
// instead of a control plane, so job ids are minted here and the B 站 publish
// flow stayed behind in that project.
// Generations take minutes, so the form only queues a render and the list polls.

let fps = 24;
let lengthStep = 17;
let lengthOffset = 5;

// Mirrors videogen_service.renderer.SHOT_HEADER: "[0s-2s] 描述" or "[3s] 描述".
let shotHeader =
  /^\[\s*(\d+(?:\.\d+)?)\s*s?\s*(?:[-–—~～]\s*(\d+(?:\.\d+)?)\s*s?\s*)?\]\s*(.*)$/;

const form = document.getElementById("generate-form");
const modeSelect = document.getElementById("mode");
const frameFields = document.getElementById("frame-fields");
const lastFrameField = document.getElementById("last-frame-field");
const lengthHint = document.getElementById("length-hint");
const storyHint = document.getElementById("story-hint");
const storySummary = document.getElementById("story-summary");
const secondsInput = document.getElementById("seconds");
const secondsLabel = document.getElementById("seconds-label");
const promptInput = document.getElementById("prompt");
// Filled from /health so adding a storyboard mode to the config does not need a
// matching edit here.
let timelineModes = new Set();
const composeNotice = document.getElementById("compose-notice");
const generateButton = document.getElementById("generate-button");
const jobList = document.getElementById("job-list");

const projectSelect = document.getElementById("project-select");
const projectNewButton = document.getElementById("project-new");
const projectDrafts = document.getElementById("project-drafts");
const draftList = document.getElementById("draft-list");
const sourceSkill = document.getElementById("source-skill");
const pipelinePanel = document.getElementById("pipeline-panel");
const pipelineStatus = document.getElementById("pipeline-status");
const pipelineNotice = document.getElementById("pipeline-notice");
const pipelineActions = document.getElementById("pipeline-actions");

const sourceBlock = document.getElementById("source-block");
const sourceUrl = document.getElementById("source-url");
const sourceTarget = document.getElementById("source-target");
const sourceMaxShots = document.getElementById("source-max-shots");
const sourceGuidance = document.getElementById("source-guidance");
const sourceButton = document.getElementById("source-button");
const sourceNotice = document.getElementById("source-notice");
const sourceResult = document.getElementById("source-result");
const sourceTitle = document.getElementById("source-title");
const sourceMeta = document.getElementById("source-meta");
const sourceSummary = document.getElementById("source-summary");
const narrationBlock = document.getElementById("narration-block");
const narrationList = document.getElementById("narration-list");

const frameUrls = { first: null, last: null };

// Mirrors the renderer's align_length_frames so the operator sees the real
// duration before spending ten minutes of GPU time on it.
function alignedSeconds(seconds) {
  const requested = Math.max(lengthOffset, Math.round(seconds * fps));
  const steps = Math.max(0, Math.ceil((requested - lengthOffset) / lengthStep));
  const frames = steps * lengthStep + lengthOffset;
  return { frames, seconds: frames / fps };
}

// Mirrors parse_storyboard closely enough to price the job; the server parses it
// again and its answer is the one that runs.
function parseStoryboard(prompt, fallbackSeconds) {
  const shots = [];
  for (const line of prompt.split("\n")) {
    const match = shotHeader.exec(line.trim());
    if (!match) continue;
    const [, start, end] = match;
    shots.push(end === undefined ? Number(start) : Number(end) - Number(start));
  }
  return shots.length ? shots : [fallbackSeconds];
}

function refreshLengthHint() {
  const requested = Number(secondsInput.value || 0);
  if (timelineModes.has(modeSelect.value)) {
    const shots = parseStoryboard(promptInput.value, requested);
    const frames = shots.reduce((sum, s) => sum + alignedSeconds(s).frames, 0);
    storySummary.textContent =
      `当前 ${shots.length} 段，合计 ${frames} 帧（${(frames / fps).toFixed(2)} 秒），` +
      "每段单独占用一次显存。";
    lengthHint.textContent = `分镜模式下方的时长填什么都不算数，实际长度按分镜累加。`;
    return;
  }
  const { frames, seconds } = alignedSeconds(requested);
  lengthHint.textContent =
    `H3 的长度按 17k+5 帧向上取整，实际会生成 ${frames} 帧（${seconds.toFixed(2)} 秒）`;
}

function revokeFrameUrl(key) {
  if (frameUrls[key]) {
    URL.revokeObjectURL(frameUrls[key]);
    frameUrls[key] = null;
  }
}

function bindFramePreview(inputId, key, previewId, imgId, nameId) {
  const input = document.getElementById(inputId);
  const preview = document.getElementById(previewId);
  const img = document.getElementById(imgId);
  const name = document.getElementById(nameId);
  input.addEventListener("change", () => {
    revokeFrameUrl(key);
    const file = input.files && input.files[0];
    if (!file) {
      preview.hidden = true;
      img.removeAttribute("src");
      name.textContent = "";
      return;
    }
    frameUrls[key] = URL.createObjectURL(file);
    img.src = frameUrls[key];
    name.textContent = file.name;
    preview.hidden = false;
  });
}

function refreshModeFields() {
  const mode = modeSelect.value;
  // A storyboard mode reaches the Director node, which has no image input at
  // all, so it takes no reference frame and sets its own duration.
  const storyboard = timelineModes.has(mode);
  frameFields.hidden = mode === "t2v" || storyboard;
  lastFrameField.hidden = mode !== "flf2v";
  document.getElementById("first-frame").required = mode !== "t2v" && !storyboard;
  document.getElementById("last-frame").required = mode === "flf2v";
  storyHint.hidden = !storyboard;
  secondsInput.readOnly = storyboard;
  secondsLabel.textContent = storyboard ? "时长（由分镜决定）" : "时长（秒）";
  if (mode === "t2v" || storyboard) {
    document.getElementById("first-frame").value = "";
    document.getElementById("last-frame").value = "";
    assetSelects.first.value = "";
    assetSelects.last.value = "";
    revokeFrameUrl("first");
    revokeFrameUrl("last");
    document.getElementById("first-frame-preview").hidden = true;
    document.getElementById("last-frame-preview").hidden = true;
  } else if (mode === "i2v") {
    document.getElementById("last-frame").value = "";
    assetSelects.last.value = "";
    revokeFrameUrl("last");
    document.getElementById("last-frame-preview").hidden = true;
  }
}

function notify(element, text, kind) {
  element.textContent = text;
  element.className = kind ? `notice ${kind}` : "";
}

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (error) {
      // A non-JSON error body is still worth reporting as the status code.
    }
    throw new Error(detail);
  }
  // DELETE answers 204 with no body, which response.json() would choke on.
  if (response.status === 204) return null;
  return response.json();
}

function modeLabel(mode) {
  return (
    { t2v: "文生视频", i2v: "图生视频", flf2v: "首尾帧", story: "分镜长片" }[
      mode
    ] || mode
  );
}

// render_id is ours to mint now that no control plane hands one out. It has to
// match ^[A-Za-z0-9_-]{1,80}$ and stay unique across reloads.
function newRenderId() {
  const stamp = new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14);
  const salt = Math.random().toString(36).slice(2, 8);
  return `vg_${stamp}_${salt}`;
}

function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function progressBlock() {
  const wrap = document.createElement("div");
  wrap.className = "vg-progress is-indeterminate";
  const track = document.createElement("div");
  track.className = "vg-progress-track";
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  const fill = document.createElement("div");
  fill.className = "vg-progress-fill";
  track.append(fill);
  const meta = document.createElement("div");
  meta.className = "vg-progress-meta";
  const label = document.createElement("span");
  label.className = "vg-progress-label";
  const value = document.createElement("span");
  value.className = "vg-progress-value";
  meta.append(label, value);
  wrap.append(track, meta);
  return wrap;
}

// Called on every poll, including for cards that were not rebuilt: the bar is
// the one thing that changes constantly, and re-rendering the card to move it
// would tear down a playing video and any open menu underneath it.
function applyProgress(card, job) {
  const wrap = card.querySelector(".vg-progress");
  if (!wrap) return;
  const track = wrap.querySelector(".vg-progress-track");
  const fill = wrap.querySelector(".vg-progress-fill");
  const label = wrap.querySelector(".vg-progress-label");
  const value = wrap.querySelector(".vg-progress-value");
  const elapsed =
    typeof job.elapsed_seconds === "number"
      ? `已用 ${formatClock(job.elapsed_seconds)}`
      : "";

  if (job.status === "QUEUED") {
    wrap.classList.add("is-indeterminate");
    track.removeAttribute("aria-valuenow");
    fill.style.width = "";
    label.textContent = "排队中，等前一条让出显卡";
    value.textContent = "";
    return;
  }
  if (!job.progress) {
    // Running, but ComfyUI has not reported a step yet: 21GB of weights are
    // still going onto the card, which is the longest silent part of the job.
    wrap.classList.add("is-indeterminate");
    track.removeAttribute("aria-valuenow");
    fill.style.width = "";
    label.textContent = "准备中，正在加载模型";
    value.textContent = elapsed;
    return;
  }

  const percent = Math.round(job.progress.percent * 100);
  wrap.classList.remove("is-indeterminate");
  fill.style.width = `${percent}%`;
  track.setAttribute("aria-valuenow", String(percent));
  const steps = `${job.progress.step}/${job.progress.total_steps} 步`;
  label.textContent =
    job.progress.pass_total > 1
      ? `第 ${job.progress.pass_index}/${job.progress.pass_total} 段 · ${steps}`
      : steps;
  value.textContent = elapsed ? `${percent}% · ${elapsed}` : `${percent}%`;
}

function renderJob(job) {
  const meta = window.AppUI
    ? AppUI.statusMeta(job.status)
    : { label: job.status, tone: "neutral" };

  const card = document.createElement("article");
  card.className = "media-card";
  card.dataset.jobId = job.render_id;

  const top = document.createElement("div");
  top.className = "media-card-top";

  const left = document.createElement("div");
  left.className = "media-card-meta";

  const pick = document.createElement("input");
  pick.type = "checkbox";
  pick.className = "pick";
  pick.dataset.jobId = job.render_id;
  // An in-flight job cannot be deleted, so it must not be selectable either.
  pick.disabled = job.status === "RUNNING" || job.status === "QUEUED";
  pick.addEventListener("change", refreshBulkBar);
  left.append(pick);

  const status = document.createElement("span");
  status.className = `status-badge tone-${meta.tone}`;
  status.textContent = meta.label;
  left.append(status);

  top.append(left);

  const busy = job.status === "RUNNING" || job.status === "QUEUED";
  if (!busy) {
    const more = document.createElement("div");
    more.className = "menu";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "button button-ghost";
    toggle.setAttribute("aria-label", "更多操作");
    toggle.textContent = "⋯";
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      document.querySelectorAll(".menu.is-open").forEach((item) => {
        if (item !== more) item.classList.remove("is-open");
      });
      more.classList.toggle("is-open");
    });
    const panel = document.createElement("div");
    panel.className = "menu-panel";
    const reuse = document.createElement("button");
    reuse.type = "button";
    reuse.className = "menu-item";
    reuse.textContent = "回填到表单";
    reuse.addEventListener("click", () => {
      more.classList.remove("is-open");
      reuseJob(job);
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "menu-item";
    remove.textContent = "删除";
    remove.addEventListener("click", () => {
      more.classList.remove("is-open");
      deleteJob(job, card);
    });
    panel.append(reuse);
    for (const [flag, slot, label] of [
      [job.has_first_frame, "first", "首帧存为资产"],
      [job.has_last_frame, "last", "尾帧存为资产"],
    ]) {
      if (!flag) continue;
      const save = document.createElement("button");
      save.type = "button";
      save.className = "menu-item";
      save.textContent = label;
      save.addEventListener("click", () => {
        more.classList.remove("is-open");
        saveFrameAsset(job, slot);
      });
      panel.append(save);
    }
    panel.append(remove);
    more.append(toggle, panel);
    top.append(more);
  }
  card.append(top);

  const prompt = document.createElement("p");
  prompt.className = "prompt";
  prompt.textContent = job.prompt;
  card.append(prompt);

  if ((job.prompt || "").length > 80) {
    const togglePrompt = document.createElement("button");
    togglePrompt.type = "button";
    togglePrompt.className = "prompt-toggle";
    togglePrompt.textContent = "展开";
    togglePrompt.addEventListener("click", () => {
      const open = prompt.classList.toggle("is-expanded");
      togglePrompt.textContent = open ? "收起" : "展开";
    });
    card.append(togglePrompt);
  }

  const sub = document.createElement("div");
  sub.className = "subtle";
  sub.style.margin = "6px 12px 0";
  const created = window.AppUI
    ? AppUI.formatRelativeTime(job.created_at)
    : job.created_at || "";
  sub.textContent = `${modeLabel(job.mode)} · ${job.width}×${job.height} · ${Number(job.seconds).toFixed(2)}s · ${created}`;
  card.append(sub);

  if (job.status === "QUEUED" || job.status === "RUNNING") {
    const skeleton = document.createElement("div");
    skeleton.className = "media-skeleton";
    skeleton.setAttribute("aria-hidden", "true");
    card.append(skeleton, progressBlock());
  }

  if (job.error) {
    const error = document.createElement("p");
    error.className = "notice error";
    error.style.margin = "8px 12px 0";
    error.textContent = job.error;
    card.append(error);
  }

  if (job.status === "DONE" && job.media_url) {
    const video = document.createElement("video");
    video.src = job.media_url;
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.className = "preview";
    if (job.width && job.height) {
      video.style.aspectRatio = `${job.width} / ${job.height}`;
    }
    card.append(video);
  }

  const footer = document.createElement("div");
  footer.className = "media-card-footer";
  const actions = document.createElement("div");
  actions.className = "review-actions";

  if (job.status === "FAILED") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "button button-primary";
    retry.textContent = "重试";
    retry.addEventListener("click", () => retryJob(job.render_id, retry));
    actions.append(retry);
  }
  if (job.status === "DONE" && job.media_url) {
    const download = document.createElement("a");
    download.className = "button button-secondary";
    download.href = job.media_url;
    download.download = `${job.render_id}.mp4`;
    download.textContent = "下载 MP4";
    actions.append(download);
  }
  if (actions.childElementCount) footer.append(actions);
  card.append(footer);
  applyProgress(card, job);
  return card;
}

// Reference frames live on the service side and are not handed back, so a
// re-run of an image mode still needs the operator to pick the still again.
function reuseJob(job) {
  modeSelect.value = job.mode;
  promptInput.value = job.prompt;
  document.getElementById("width").value = job.width;
  document.getElementById("height").value = job.height;
  secondsInput.value = job.seconds;
  refreshModeFields();
  refreshLengthHint();
  syncRatioButtons();
  promptInput.focus();
  if (job.has_first_frame || job.has_last_frame) {
    notify(composeNotice, "参考图没有回填，请重新选一张", "");
  }
}

async function confirmDelete(message) {
  if (window.AppUI && AppUI.confirmDanger) {
    return AppUI.confirmDanger({
      title: "确认删除",
      message,
      confirmLabel: "确认删除",
    });
  }
  return window.confirm(message);
}

async function removeJob(renderId) {
  await request(`/v1/renders/${renderId}`, { method: "DELETE" });
  rows.get(renderId)?.element.remove();
  rows.delete(renderId);
}

async function deleteJob(job) {
  const ok = await confirmDelete("删除这条任务？视频文件一并删掉，不可恢复。");
  if (!ok) return;
  try {
    await removeJob(job.render_id);
    await refreshJobs();
  } catch (error) {
    notify(composeNotice, error.message, "error");
  }
}

async function deleteSelected() {
  const ids = selectedJobIds();
  if (!ids.length) return;
  const ok = await confirmDelete(
    `删除选中的 ${ids.length} 条任务？视频文件一并删掉，不可恢复。`,
  );
  if (!ok) return;
  const failed = [];
  for (const renderId of ids) {
    try {
      await removeJob(renderId);
    } catch (error) {
      failed.push(`${renderId}: ${error.message}`);
    }
  }
  await refreshJobs();
  if (failed.length) notify(composeNotice, failed.join("；"), "error");
}

function selectedJobIds() {
  return [...jobList.querySelectorAll(".pick:checked")].map(
    (box) => box.dataset.jobId,
  );
}

function refreshBulkBar() {
  const total = jobList.querySelectorAll(".pick").length;
  const picked = selectedJobIds().length;
  const bar = document.getElementById("bulk-bar");
  bar.hidden = total === 0;
  document.getElementById("selected-count").textContent = picked
    ? `已选 ${picked} 条`
    : "未选中";
  document.getElementById("bulk-delete").disabled = picked === 0;
  const all = document.getElementById("select-all");
  all.checked = total > 0 && picked === total;
  all.indeterminate = picked > 0 && picked < total;
}

async function retryJob(renderId, button) {
  button.disabled = true;
  try {
    await request(`/v1/renders/${renderId}/retry`, { method: "POST" });
    await refreshJobs();
  } catch (error) {
    button.disabled = false;
    notify(composeNotice, error.message, "error");
  }
}

// Rebuilding every row on each poll would tear down the <video> the operator is
// watching, so rows are diffed instead and only replaced when something they
// actually display has changed.
const rows = new Map();

function jobSignature(job) {
  return JSON.stringify([job.status, job.error, job.media_url, job.prompt]);
}

async function refreshJobs() {
  let jobs;
  try {
    jobs = await request("/v1/renders");
  } catch (error) {
    if (!rows.size) {
      jobList.replaceChildren();
      const failed = document.createElement("div");
      failed.className = "empty";
      failed.textContent = `加载失败：${error.message}`;
      jobList.append(failed);
    }
    return;
  }

  // Drop the placeholder empty node when we start rendering real cards.
  if (jobs.length) {
    const placeholder = jobList.querySelector(":scope > .empty");
    if (placeholder) placeholder.remove();
  }

  const seen = new Set();
  for (const job of jobs) {
    seen.add(job.render_id);
    const signature = jobSignature(job);
    const existing = rows.get(job.render_id);
    if (existing && existing.signature === signature) {
      // Progress deliberately stays out of the signature: it moves on every
      // poll, and rebuilding the card for it would be the one update that
      // guarantees a card is never stable while a job runs.
      applyProgress(existing.element, job);
      continue;
    }
    const element = renderJob(job);
    if (existing) {
      const oldVideo = existing.element.querySelector("video");
      const wasPlaying = oldVideo && !oldVideo.paused;
      const currentTime = oldVideo ? oldVideo.currentTime : 0;
      existing.element.replaceWith(element);
      if (wasPlaying) {
        const nextVideo = element.querySelector("video");
        if (nextVideo) {
          nextVideo.currentTime = currentTime;
          nextVideo.play().catch(() => {});
        }
      }
    } else {
      jobList.append(element);
    }
    rows.set(job.render_id, { element, signature });
  }

  for (const [renderId, row] of rows) {
    if (seen.has(renderId)) continue;
    row.element.remove();
    rows.delete(renderId);
  }

  if (!jobs.length) {
    jobList.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有生成任务";
    jobList.append(empty);
  }
  refreshBulkBar();
  schedulePoll(jobs);
}

// Nothing changes on its own once every job is finished, so the timer only runs
// while something is actually in flight.
let pollTimer = null;
let pollInterval = null;

function schedulePoll(jobs) {
  const active = jobs.some(
    (job) => job.status === "QUEUED" || job.status === "RUNNING",
  );
  // A moving bar wants a faster clock than a queue that only changes when a job
  // finishes, so a pure queue stays on the slow one.
  const interval = jobs.some((job) => job.status === "RUNNING") ? 2000 : 5000;
  if (active && pollTimer !== null && pollInterval === interval) return;
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
    pollInterval = null;
  }
  if (active) {
    pollInterval = interval;
    pollTimer = setInterval(refreshJobs, interval);
  }
}

function syncRatioButtons() {
  const width = Number(document.getElementById("width").value);
  const height = Number(document.getElementById("height").value);
  document.querySelectorAll("[data-ratio]").forEach((button) => {
    const active =
      Number(button.dataset.w) === width && Number(button.dataset.h) === height;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

// ---- 资产中心 ----------------------------------------------------------------
// 具名的可复用参考图,住在 work/.assets/。提交渲染时服务端把资产字节拷进渲染
// 目录,所以之后删资产不影响任何历史任务。

const assetSelects = {
  first: document.getElementById("first-frame-asset"),
  last: document.getElementById("last-frame-asset"),
};
const assetList = document.getElementById("asset-list");
const assetNotice = document.getElementById("asset-notice");

let assetIndex = new Map();

function assetLabel(asset) {
  return asset.category ? `${asset.name}（${asset.category}）` : asset.name;
}

function renderAssetOptions(select, assets) {
  const keep = select.value;
  while (select.options.length > 1) select.remove(1);
  for (const asset of assets) {
    const option = document.createElement("option");
    option.value = asset.asset_id;
    option.textContent = assetLabel(asset);
    select.append(option);
  }
  if (keep && [...select.options].some((o) => o.value === keep)) {
    select.value = keep;
  }
}

function renderAssetRows(assets) {
  assetList.replaceChildren();
  for (const asset of assets) {
    const item = document.createElement("li");
    const row = document.createElement("div");
    row.className = "asset-row";
    const thumb = document.createElement("img");
    thumb.className = "asset-thumb";
    thumb.src = asset.media_url;
    thumb.alt = asset.name;
    thumb.loading = "lazy";
    const label = document.createElement("span");
    label.textContent = assetLabel(asset);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button button-ghost asset-delete";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      const ok = await confirmDelete(
        `删除资产「${asset.name}」？已提交的渲染不受影响。`,
      );
      if (!ok) return;
      try {
        await request(`/v1/assets/${asset.asset_id}`, { method: "DELETE" });
        await loadAssets();
      } catch (error) {
        notify(assetNotice, error.message, "error");
      }
    });
    row.append(thumb, label, remove);
    item.append(row);
    assetList.append(item);
  }
}

async function loadAssets() {
  let assets;
  try {
    assets = await request("/v1/assets");
  } catch (error) {
    return; // 资产列表挂了不拦渲染,下拉里没得选而已。
  }
  assetIndex = new Map(assets.map((asset) => [asset.asset_id, asset]));
  renderAssetOptions(assetSelects.first, assets);
  renderAssetOptions(assetSelects.last, assets);
  renderAssetRows(assets);
}

// 选中资产且没有另传文件时,预览直接指向资产图。
function showAssetPreview(key) {
  const select = assetSelects[key];
  const input = document.getElementById(key === "first" ? "first-frame" : "last-frame");
  const preview = document.getElementById(`${key}-frame-preview`);
  const img = document.getElementById(`${key}-frame-img`);
  const name = document.getElementById(`${key}-frame-name`);
  if (input.files && input.files[0]) return; // 上传文件优先,预览保持文件的
  const asset = assetIndex.get(select.value);
  if (!asset) {
    preview.hidden = true;
    img.removeAttribute("src");
    name.textContent = "";
    return;
  }
  img.src = asset.media_url;
  name.textContent = `资产：${asset.name}`;
  preview.hidden = false;
}

async function uploadAsset() {
  const name = document.getElementById("asset-name").value.trim();
  const file = document.getElementById("asset-file").files[0];
  if (!name || !file) {
    notify(assetNotice, "名称和图片都要填", "error");
    return;
  }
  const body = new FormData();
  body.set("name", name);
  body.set("category", document.getElementById("asset-category").value.trim());
  body.set("file", file);
  try {
    await request("/v1/assets", { method: "POST", body });
    document.getElementById("asset-name").value = "";
    document.getElementById("asset-file").value = "";
    notify(assetNotice, "已存入资产中心", "ok");
    await loadAssets();
  } catch (error) {
    notify(assetNotice, error.message, "error");
  }
}

async function saveFrameAsset(job, slot) {
  const name = window.prompt(
    "资产名称：",
    `${modeLabel(job.mode)}参考图 ${job.render_id.slice(-6)}`,
  );
  if (!name || !name.trim()) return;
  try {
    await request("/v1/assets/from-render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        render_id: job.render_id,
        slot,
        name: name.trim(),
      }),
    });
    if (window.AppUI) AppUI.showToast("参考帧已存入资产中心", "success");
    await loadAssets();
  } catch (error) {
    notify(composeNotice, error.message, "error");
  }
}

// ---- Memory 偏好 -------------------------------------------------------------
// 用户显式要求记住的长期偏好,住在 work/memory.json,每次起稿注入总结 Prompt。

const memoryList = document.getElementById("memory-list");
const memoryNotice = document.getElementById("memory-notice");
const memoryText = document.getElementById("memory-text");

async function loadMemory() {
  let entries;
  try {
    entries = await request("/v1/memory");
  } catch (error) {
    return;
  }
  memoryList.replaceChildren();
  for (const entry of entries) {
    const item = document.createElement("li");
    const row = document.createElement("div");
    row.className = "draft-row";
    const label = document.createElement("span");
    label.textContent = entry.text;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button button-ghost draft-fill";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      try {
        await request(`/v1/memory/${entry.entry_id}`, { method: "DELETE" });
        await loadMemory();
      } catch (error) {
        notify(memoryNotice, error.message, "error");
      }
    });
    row.append(label, remove);
    item.append(row);
    memoryList.append(item);
  }
}

async function addMemory() {
  const text = memoryText.value.trim();
  if (!text) {
    notify(memoryNotice, "先写一条偏好", "error");
    return;
  }
  try {
    await request("/v1/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    memoryText.value = "";
    notify(memoryNotice, "已记住，之后每次起稿都会带上", "ok");
    await loadMemory();
  } catch (error) {
    notify(memoryNotice, error.message, "error");
  }
}

// ---- 项目与 Skill -----------------------------------------------------------
// 项目把脚本草稿和渲染归档到一起（work/.projects/），Skill 是 skills/ 里的具名
// 创作规范。两者都是可选项：不选时页面行为和从前完全一样。

async function loadSkills() {
  let skills;
  try {
    skills = await request("/v1/skills");
  } catch (error) {
    return; // Skill 列表挂了不拦生成，只是下拉里没得选。
  }
  for (const skill of skills) {
    const option = document.createElement("option");
    option.value = skill.skill_id;
    option.textContent = skill.category
      ? `${skill.name}（${skill.category}）`
      : skill.name;
    if (skill.description) option.title = skill.description;
    sourceSkill.append(option);
  }
}

async function loadProjects(selectedId) {
  let projects;
  try {
    projects = await request("/v1/projects");
  } catch (error) {
    return;
  }
  const keep = selectedId ?? projectSelect.value;
  while (projectSelect.options.length > 1) projectSelect.remove(1);
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.project_id;
    option.textContent =
      `${project.name}（${project.draft_count} 稿 · ${project.render_count} 渲染）`;
    projectSelect.append(option);
  }
  if (keep && [...projectSelect.options].some((o) => o.value === keep)) {
    projectSelect.value = keep;
  }
}

async function refreshDrafts() {
  const projectId = projectSelect.value;
  if (!projectId) {
    projectDrafts.hidden = true;
    draftList.replaceChildren();
    return;
  }
  let project;
  try {
    project = await request(`/v1/projects/${projectId}`);
  } catch (error) {
    projectDrafts.hidden = true;
    return;
  }
  draftList.replaceChildren();
  // 新草稿排前面，回填按钮把整份脚本重新写进表单。
  for (const draft of [...project.drafts].reverse()) {
    const item = document.createElement("li");
    const row = document.createElement("div");
    row.className = "draft-row";
    const label = document.createElement("span");
    const created = window.AppUI
      ? AppUI.formatRelativeTime(draft.created_at)
      : draft.created_at;
    label.textContent =
      `#${draft.draft_id} ${draft.result.title} · ${draft.result.seconds.toFixed(0)}s · ${created}`;
    const fill = document.createElement("button");
    fill.type = "button";
    fill.className = "button button-ghost draft-fill";
    fill.textContent = "回填";
    fill.addEventListener("click", () => {
      fillFromScript(draft.result);
      notify(sourceNotice, `草稿 #${draft.draft_id} 已填进表单`, "ok");
    });
    row.append(label, fill);
    item.append(row);
    draftList.append(item);
  }
  projectDrafts.hidden = project.drafts.length === 0;
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
    pipelineFilledDraft = null;
    await refreshDrafts();
    await refreshPipeline();
    if (window.AppUI) AppUI.showToast("项目已创建", "success");
  } catch (error) {
    notify(composeNotice, error.message, "error");
  }
}

// 渲染排队成功后把 render_id 挂进当前项目;挂失败不影响已排队的渲染。
async function linkRenderToProject(renderId) {
  const projectId = projectSelect.value;
  if (!projectId) return;
  try {
    await request(`/v1/projects/${projectId}/renders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ render_id: renderId }),
    });
    await loadProjects(projectId);
  } catch (error) {
    notify(composeNotice, `已排队，但归档到项目失败：${error.message}`, "error");
  }
}

// ---- 自动流水线 --------------------------------------------------------------
// 起稿到渲染的编排全在本机:yt-dlp 取字幕、Ollama 出脚本、人工审阅是硬关卡、
// 批准后进 ComfyUI 渲染队列。脚本好了流水线会停在 AWAITING_REVIEW 等人。

const PIPELINE_LABELS = {
  QUEUED: "排队等脚本…",
  SCRIPTING: "正在取字幕并让 Ollama 总结，通常要一两分钟…",
  AWAITING_REVIEW: "脚本草稿已就绪并填进了表单——改到满意后点「批准并渲染」。",
  RENDERING: "已批准，渲染中；进度看右侧任务列表。",
  DONE: "流水线完成，视频在右侧任务列表里。",
  REJECTED: "草稿已驳回。清除后可以换个要求重新起稿。",
  FAILED: "流水线失败。",
};

let pipelineTimer = null;
// 记住已回填过的草稿,轮询时不反复覆盖审阅人正在改的提示词。
let pipelineFilledDraft = null;

function stopPipelinePoll() {
  if (pipelineTimer !== null) {
    clearInterval(pipelineTimer);
    pipelineTimer = null;
  }
}

function schedulePipelinePoll(run) {
  const active =
    run && ["QUEUED", "SCRIPTING", "RENDERING"].includes(run.status);
  if (!active) {
    stopPipelinePoll();
    return;
  }
  if (pipelineTimer === null) {
    pipelineTimer = setInterval(refreshPipeline, 3000);
  }
}

function pipelineButton(label, kind, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${kind}`;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

async function pipelineCall(path, options, after) {
  try {
    await request(path, options);
    notify(pipelineNotice, "", "");
    if (after) await after();
    await refreshPipeline();
  } catch (error) {
    notify(pipelineNotice, error.message, "error");
  }
}

async function startPipeline() {
  const projectId = projectSelect.value;
  const url = sourceUrl.value.trim();
  if (!url) {
    notify(pipelineNotice, "先在上面的「从 YouTube 网址起稿」里贴链接", "error");
    return;
  }
  await pipelineCall(`/v1/projects/${projectId}/pipeline`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      target_seconds: Number(sourceTarget.value) || null,
      max_shots: Number(sourceMaxShots.value) || null,
      guidance: sourceGuidance.value.trim() || null,
      skill: sourceSkill.value || null,
      width: Number(document.getElementById("width").value) || 864,
      height: Number(document.getElementById("height").value) || 480,
    }),
  });
}

async function approvePipeline(run) {
  const body = {};
  const edited = promptInput.value.trim();
  // 审阅人在主表单里改分镜;和草稿不一样才算改过,原样批准就渲染存档稿。
  if (run.draft && edited && edited !== run.draft.result.prompt) {
    body.prompt = edited;
  }
  await pipelineCall(
    `/v1/projects/${projectSelect.value}/pipeline/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    async () => {
      await loadProjects(projectSelect.value);
      await refreshJobs();
    },
  );
}

async function rejectPipeline() {
  const reason = window.prompt("驳回原因（可留空）：") ?? "";
  await pipelineCall(`/v1/projects/${projectSelect.value}/pipeline/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason.trim() || null }),
  });
}

function renderPipeline(run) {
  pipelineActions.replaceChildren();
  if (!run) {
    pipelineStatus.textContent =
      "没有进行中的流水线。贴好链接后一键起稿：脚本生成后会停下等你审阅，批准才进显卡。";
    pipelineActions.append(
      pipelineButton("一键起稿 → 审阅 → 渲染", "button-primary", startPipeline),
    );
    stopPipelinePoll();
    return;
  }
  let text = PIPELINE_LABELS[run.status] || run.status;
  if (run.status === "FAILED" && run.error) text += `原因：${run.error}`;
  if (run.status === "REJECTED" && run.review && run.review.reason) {
    text += `原因：${run.review.reason}`;
  }
  pipelineStatus.textContent = text;

  const projectId = projectSelect.value;
  if (run.status === "AWAITING_REVIEW") {
    if (run.draft && pipelineFilledDraft !== run.draft.draft_id) {
      fillFromScript(run.draft.result);
      pipelineFilledDraft = run.draft.draft_id;
    }
    pipelineActions.append(
      pipelineButton("批准并渲染", "button-primary", () => approvePipeline(run)),
      pipelineButton("驳回", "button-danger", rejectPipeline),
    );
  } else if (run.status === "FAILED") {
    pipelineActions.append(
      pipelineButton("重试", "button-primary", () =>
        pipelineCall(`/v1/projects/${projectId}/pipeline/retry`, {
          method: "POST",
        }),
      ),
      pipelineButton("清除", "button-secondary", () =>
        pipelineCall(`/v1/projects/${projectId}/pipeline`, {
          method: "DELETE",
        }),
      ),
    );
  } else if (run.status === "DONE" || run.status === "REJECTED") {
    pipelineActions.append(
      pipelineButton("清除，准备下一条", "button-secondary", () =>
        pipelineCall(`/v1/projects/${projectId}/pipeline`, {
          method: "DELETE",
        }),
      ),
    );
  } else if (run.status === "QUEUED" || run.status === "SCRIPTING") {
    pipelineActions.append(
      pipelineButton("取消", "button-secondary", () =>
        pipelineCall(`/v1/projects/${projectId}/pipeline`, {
          method: "DELETE",
        }),
      ),
    );
  }
  schedulePipelinePoll(run);
}

async function refreshPipeline() {
  const projectId = projectSelect.value;
  if (!projectId) {
    pipelinePanel.hidden = true;
    stopPipelinePoll();
    return;
  }
  pipelinePanel.hidden = false;
  let run = null;
  try {
    run = await request(`/v1/projects/${projectId}/pipeline`);
  } catch (error) {
    // 404 就是还没起过流水线;别的错误也先按"没有"展示,下一轮再试。
  }
  const wasRendering = run && run.status === "RENDERING";
  renderPipeline(run);
  if (run && run.status === "DONE") {
    await loadProjects(projectId);
    await refreshDrafts();
  }
  if (wasRendering) await refreshJobs();
}

// ---- YouTube 网址 → 分镜脚本 ------------------------------------------------

function fillFromScript(script) {
  // The service already aligned the storyboard, so the returned prompt and
  // duration are exactly what a render of it would produce.
  if (script.mode) modeSelect.value = script.mode;
  promptInput.value = script.prompt;
  secondsInput.value = script.seconds.toFixed(2);
  refreshModeFields();
  refreshLengthHint();

  sourceTitle.textContent = script.title;
  const source = script.source;
  if (source.kind === "document") {
    sourceMeta.textContent =
      `${source.filename} · 本地文档 ${source.chars} 字` +
      `${source.truncated ? "（已截断）" : ""}`;
  } else {
    const kind = source.automatic_captions ? "自动字幕" : "人工字幕";
    sourceMeta.textContent =
      `${source.title} · ${kind}（${source.subtitle_language}）· ` +
      `原片 ${Math.round(source.duration_seconds / 60)} 分钟 · ` +
      `字幕 ${source.transcript_chars} 字${source.transcript_truncated ? "（已截断）" : ""}`;
  }
  sourceSummary.textContent = script.summary;

  narrationList.replaceChildren();
  const narrations = script.shots.filter((shot) => shot.narration);
  for (const shot of narrations) {
    const item = document.createElement("li");
    item.textContent = shot.narration;
    narrationList.append(item);
  }
  narrationBlock.hidden = narrations.length === 0;
  sourceResult.hidden = false;
}

async function createScript() {
  const url = sourceUrl.value.trim();
  if (!url) {
    notify(sourceNotice, "先贴一个 YouTube 链接", "error");
    return;
  }
  sourceButton.disabled = true;
  notify(sourceNotice, "正在取字幕并总结，通常要一两分钟…", "");
  try {
    const script = await request("/v1/scripts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        target_seconds: Number(sourceTarget.value) || null,
        max_shots: Number(sourceMaxShots.value) || null,
        guidance: sourceGuidance.value.trim() || null,
        skill: sourceSkill.value || null,
        project_id: projectSelect.value || null,
      }),
    });
    fillFromScript(script);
    notify(sourceNotice, "分镜已填进下面的提示词，改完再点生成", "ok");
    if (window.AppUI) AppUI.showToast("分镜脚本已生成", "success");
    if (projectSelect.value) {
      await loadProjects(projectSelect.value);
      await refreshDrafts();
    }
  } catch (error) {
    notify(sourceNotice, error.message, "error");
  } finally {
    sourceButton.disabled = false;
  }
}

// ---- 本地文档 → 分镜脚本 ------------------------------------------------------

async function createScriptFromDocument() {
  const input = document.getElementById("source-doc");
  const file = input.files && input.files[0];
  if (!file) {
    notify(sourceNotice, "先选一个 PDF / Word / 文本文件", "error");
    return;
  }
  const docButton = document.getElementById("source-doc-button");
  docButton.disabled = true;
  notify(sourceNotice, "正在本机解析文档并总结，通常要一两分钟…", "");
  try {
    const body = new FormData();
    body.set("file", file);
    if (Number(sourceTarget.value)) body.set("target_seconds", sourceTarget.value);
    if (Number(sourceMaxShots.value)) body.set("max_shots", sourceMaxShots.value);
    if (sourceGuidance.value.trim()) body.set("guidance", sourceGuidance.value.trim());
    if (sourceSkill.value) body.set("skill", sourceSkill.value);
    if (projectSelect.value) body.set("project_id", projectSelect.value);
    const script = await request("/v1/scripts/document", { method: "POST", body });
    fillFromScript(script);
    notify(sourceNotice, "分镜已填进下面的提示词，改完再点生成", "ok");
    if (window.AppUI) AppUI.showToast("分镜脚本已生成", "success");
    if (projectSelect.value) {
      await loadProjects(projectSelect.value);
      await refreshDrafts();
    }
  } catch (error) {
    notify(sourceNotice, error.message, "error");
  } finally {
    docButton.disabled = false;
  }
}

// ---- 提交渲染 ---------------------------------------------------------------

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  generateButton.disabled = true;
  notify(composeNotice, "正在排队…", "");
  try {
    // Reference frames ride along as multipart so the browser never has to
    // base64 a full-resolution still into JSON.
    const body = new FormData(form);
    const renderId = newRenderId();
    body.set("render_id", renderId);
    // An empty number input posts "", which the service reads as a bad int.
    if (!String(body.get("seed") || "").trim()) body.delete("seed");
    for (const key of ["first_frame", "last_frame"]) {
      const file = body.get(key);
      if (file && file.size === 0 && !file.name) body.delete(key);
    }
    // 隐藏或空着的资产选择不跟着提交:模式不吃参考图时残留的选中项会被
    // 服务端当成真参考图拒掉。
    for (const key of ["first_frame_asset", "last_frame_asset"]) {
      if (!String(body.get(key) || "").trim()) body.delete(key);
    }
    if (frameFields.hidden) {
      body.delete("first_frame_asset");
      body.delete("last_frame_asset");
    } else if (lastFrameField.hidden) {
      body.delete("last_frame_asset");
    }
    await request("/v1/renders", { method: "POST", body });
    notify(composeNotice, "已排队，生成期间显卡会被独占", "ok");
    await linkRenderToProject(renderId);
    await refreshJobs();
  } catch (error) {
    notify(composeNotice, error.message, "error");
  } finally {
    generateButton.disabled = false;
  }
});

sourceButton.addEventListener("click", createScript);
document
  .getElementById("source-doc-button")
  .addEventListener("click", createScriptFromDocument);
document.getElementById("project-export").addEventListener("click", () => {
  const projectId = projectSelect.value;
  if (!projectId) {
    notify(composeNotice, "先选一个项目再导出", "error");
    return;
  }
  // zip 里是成片 MP4、每稿的分镜文本和按渲染时间轴对齐的 SRT 字幕,
  // 剪映/DaVinci/Premiere 都能直接吃。
  const anchor = document.createElement("a");
  anchor.href = `/v1/projects/${projectId}/export`;
  anchor.download = "";
  anchor.click();
});
document.getElementById("asset-upload").addEventListener("click", uploadAsset);
document.getElementById("memory-add").addEventListener("click", addMemory);
memoryText.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addMemory();
  }
});
assetSelects.first.addEventListener("change", () => showAssetPreview("first"));
assetSelects.last.addEventListener("change", () => showAssetPreview("last"));
projectNewButton.addEventListener("click", createProject);
projectSelect.addEventListener("change", async () => {
  pipelineFilledDraft = null;
  notify(pipelineNotice, "", "");
  await refreshDrafts();
  await refreshPipeline();
});
sourceUrl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    createScript();
  }
});

modeSelect.addEventListener("change", () => {
  refreshModeFields();
  refreshLengthHint();
});
secondsInput.addEventListener("input", refreshLengthHint);
promptInput.addEventListener("input", refreshLengthHint);
document.getElementById("width").addEventListener("input", syncRatioButtons);
document.getElementById("height").addEventListener("input", syncRatioButtons);
document.getElementById("bulk-delete").addEventListener("click", deleteSelected);
document.getElementById("select-all").addEventListener("change", (event) => {
  for (const box of jobList.querySelectorAll(".pick")) {
    if (!box.disabled) box.checked = event.target.checked;
  }
  refreshBulkBar();
});

document.querySelectorAll("[data-ratio]").forEach((button) => {
  button.addEventListener("click", () => {
    document.getElementById("width").value = button.dataset.w;
    document.getElementById("height").value = button.dataset.h;
    syncRatioButtons();
  });
});

bindFramePreview(
  "first-frame",
  "first",
  "first-frame-preview",
  "first-frame-img",
  "first-frame-name",
);
bindFramePreview(
  "last-frame",
  "last",
  "last-frame-preview",
  "last-frame-img",
  "last-frame-name",
);

document.addEventListener("click", (event) => {
  if (!event.target.closest(".menu")) {
    document.querySelectorAll(".menu.is-open").forEach((item) => {
      item.classList.remove("is-open");
    });
  }
});

async function start() {
  try {
    const health = await request("/health");
    fps = Number(health.fps || fps);
    lengthStep = Number(health.length_step || lengthStep);
    lengthOffset = Number(health.length_offset || lengthOffset);
    if (health.shot_header_pattern) {
      shotHeader = new RegExp(health.shot_header_pattern);
    }
    if (health.min_seconds) secondsInput.min = String(health.min_seconds);
    if (health.max_seconds) secondsInput.max = String(health.max_seconds);
    timelineModes = new Set(health.timeline_modes || []);
    // 分镜长片 needs a custom node the other modes do not, so an install that
    // skipped it drops the mode from config — and then from this list, rather
    // than letting the operator queue a job that cannot run.
    const configured = health.modes || [];
    if (configured.length) {
      for (const option of modeSelect.options) {
        option.hidden = !configured.includes(option.value);
      }
      // Starting on 分镜长片 when it exists: that is what a YouTube script fills.
      if (configured.includes("story")) modeSelect.value = "story";
    }
  } catch (error) {
    notify(composeNotice, error.message, "error");
  }
  try {
    const script = await request("/v1/scripts/config");
    sourceTarget.value = String(script.target_seconds);
    sourceMaxShots.value = String(script.max_shots);
    sourceMaxShots.max = String(script.max_shots);
    if (!script.enabled) {
      sourceButton.disabled = true;
      sourceBlock.open = false;
      notify(sourceNotice, "字幕总结在 config.yaml 里是关的（script.enabled）", "error");
    }
  } catch (error) {
    sourceButton.disabled = true;
    notify(sourceNotice, error.message, "error");
  }
  refreshModeFields();
  refreshLengthHint();
  syncRatioButtons();
  await Promise.all([loadSkills(), loadProjects(), loadAssets(), loadMemory()]);
  await refreshPipeline();
  // refreshJobs starts its own timer, and only while a job is in flight.
  await refreshJobs();
}

start();
