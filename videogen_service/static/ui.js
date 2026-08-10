// Shared UI helpers for the videogen console.
// Trimmed copy of videotube's ui.js: only what the generation page uses —
// the dashboard, review workspace and publish helpers stayed in that project.
// Exposed only as window.AppUI so the page script keeps its own globals.

(function () {
  "use strict";

  const STATUS_LABELS = {
    QUEUED: "排队中",
    RUNNING: "生成中",
    DONE: "已完成",
    FAILED: "生成失败",
  };

  function statusMeta(status) {
    const raw = String(status || "");
    return {
      label: STATUS_LABELS[raw] || raw || "未知",
      tone:
        raw === "DONE"
          ? "success"
          : raw === "FAILED"
            ? "danger"
            : raw === "RUNNING" || raw === "QUEUED"
              ? "info"
              : "neutral",
      raw,
    };
  }

  function formatRelativeTime(input) {
    if (!input) return "—";
    const date = input instanceof Date ? input : new Date(input);
    if (Number.isNaN(date.getTime())) return String(input);
    const delta = date.getTime() - Date.now();
    const abs = Math.abs(delta);
    const rtf =
      typeof Intl !== "undefined" && Intl.RelativeTimeFormat
        ? new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" })
        : null;
    const units = [
      ["year", 365 * 24 * 3600 * 1000],
      ["month", 30 * 24 * 3600 * 1000],
      ["day", 24 * 3600 * 1000],
      ["hour", 3600 * 1000],
      ["minute", 60 * 1000],
      ["second", 1000],
    ];
    for (const [unit, ms] of units) {
      if (abs >= ms || unit === "second") {
        const value = Math.round(delta / ms);
        if (rtf) return rtf.format(value, unit);
        return `${Math.abs(value)}${
          {
            year: "年",
            month: "个月",
            day: "天",
            hour: "小时",
            minute: "分钟",
            second: "秒",
          }[unit]
        }${delta < 0 ? "前" : "后"}`;
      }
    }
    return date.toLocaleString();
  }

  function ensureToastHost() {
    let host = document.getElementById("app-toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "app-toast-host";
      host.className = "toast-host";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    return host;
  }

  function showToast(message, type) {
    const host = ensureToastHost();
    const toast = document.createElement("div");
    toast.className = `toast toast-${type || "info"}`;
    toast.textContent = message;
    host.appendChild(toast);
    window.setTimeout(() => {
      toast.classList.add("is-leaving");
      window.setTimeout(() => toast.remove(), 180);
    }, 2800);
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  // Promise-based confirm using a shared <dialog>. Resolves true/false.
  function confirmDanger(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      let dialog = document.getElementById("app-confirm-dialog");
      if (!dialog) {
        dialog = document.createElement("dialog");
        dialog.id = "app-confirm-dialog";
        dialog.className = "dialog";
        dialog.innerHTML = `
          <form method="dialog" class="dialog-body">
            <h3 class="dialog-title" data-confirm-title>确认</h3>
            <p class="dialog-message" data-confirm-message></p>
            <div class="dialog-actions">
              <button type="submit" value="cancel" class="button button-secondary">取消</button>
              <button type="submit" value="confirm" class="button button-danger is-solid" data-confirm-ok>确认</button>
            </div>
          </form>`;
        document.body.appendChild(dialog);
        dialog.addEventListener("close", () => {
          const result = dialog.returnValue === "confirm";
          const pending = dialog._pendingResolve;
          dialog._pendingResolve = null;
          if (pending) pending(result);
        });
      }
      dialog.querySelector("[data-confirm-title]").textContent =
        opts.title || "确认操作";
      dialog.querySelector("[data-confirm-message]").textContent =
        opts.message || "";
      dialog.querySelector("[data-confirm-ok]").textContent =
        opts.confirmLabel || "确认";
      dialog._pendingResolve = resolve;
      openDialog(dialog);
    });
  }

  // The console now talks to the render service directly, so its health pills
  // read this service's own /health instead of a control plane's proxy.
  async function initSystemHealth() {
    const serviceEl = document.querySelector("[data-health-service]");
    const scriptEl = document.querySelector("[data-health-script]");
    const setState = (el, ok, label, warn) => {
      if (!el) return;
      el.textContent = label;
      el.classList.remove("is-ok", "is-warn", "is-off");
      el.classList.add(ok ? "is-ok" : warn ? "is-warn" : "is-off");
    };
    try {
      const response = await fetch("/health");
      if (!response.ok) throw new Error("health failed");
      const body = await response.json();
      const modes = (body.modes || []).length;
      setState(serviceEl, true, `渲染服务在线 · ${modes} 种模式`, false);
    } catch (error) {
      setState(serviceEl, false, "渲染服务离线", true);
    }
    try {
      const response = await fetch("/v1/scripts/config");
      if (!response.ok) throw new Error("script config failed");
      const body = await response.json();
      if (body.enabled) setState(scriptEl, true, `字幕总结 · ${body.model}`, false);
      else setState(scriptEl, false, "字幕总结已关闭", true);
    } catch (error) {
      setState(scriptEl, false, "字幕总结未知", true);
    }
  }

  function setMobileNavOpen(shell, toggle, backdrop, open) {
    shell.classList.toggle("nav-open", open);
    document.body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (backdrop) {
      backdrop.hidden = !open;
      backdrop.setAttribute("aria-hidden", String(!open));
    }
  }

  function initMobileNav() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const shell = document.querySelector(".app-shell");
    if (!toggle || !shell) return;

    // Real backdrop element (pseudo-elements cannot receive reliable clicks).
    let backdrop = document.querySelector(".nav-backdrop");
    if (!backdrop) {
      backdrop = document.createElement("button");
      backdrop.type = "button";
      backdrop.className = "nav-backdrop";
      backdrop.setAttribute("aria-label", "关闭导航");
      backdrop.hidden = true;
      document.body.appendChild(backdrop);
    }

    const close = () => setMobileNavOpen(shell, toggle, backdrop, false);
    const open = () => setMobileNavOpen(shell, toggle, backdrop, true);

    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      if (shell.classList.contains("nav-open")) close();
      else open();
    });
    backdrop.addEventListener("click", close);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && shell.classList.contains("nav-open")) {
        close();
        toggle.focus();
      }
    });
  }

  window.AppUI = {
    statusMeta,
    formatRelativeTime,
    showToast,
    openDialog,
    closeDialog,
    confirmDanger,
    initSystemHealth,
    initMobileNav,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initMobileNav();
      initSystemHealth();
    });
  } else {
    initMobileNav();
    initSystemHealth();
  }
})();
