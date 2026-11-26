/* FormFlow client helpers: 1-click Copy with toast + silent Dashboard live refresh */
(function () {
  "use strict";

  let toastTimer = null;

  function showToast(msg) {
    const toast = document.getElementById("ff-toast");
    if (toast) {
      toast.textContent = msg;
      toast.classList.add("visible");
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove("visible"), 2200);
    }
    const statusEl = document.getElementById("copy-status");
    if (statusEl) {
      statusEl.textContent = msg;
      statusEl.className = "hint";
    }
  }

  function markCopied(btn, label) {
    const orig = btn.getAttribute("data-orig-label") || btn.textContent;
    btn.setAttribute("data-orig-label", orig);
    btn.textContent = "Copied ✓";
    btn.classList.add("copied");
    showToast(label ? `Copied ${label}.` : "Copied to clipboard.");
    setTimeout(() => {
      btn.textContent = orig;
      btn.classList.remove("copied");
    }, 1800);
  }

  function fallbackCopy(text, btn, label) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      if (document.execCommand("copy")) {
        markCopied(btn, label);
      } else {
        showToast("Clipboard unavailable — select and copy manually.");
      }
    } catch (_err) {
      showToast("Clipboard unavailable — select and copy manually.");
    }
    document.body.removeChild(ta);
  }

  function copyValue(text, btn, label) {
    const clean = (text || "").trim();
    if (!clean) {
      showToast("Nothing to copy.");
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(clean).then(
        () => markCopied(btn, label),
        () => fallbackCopy(clean, btn, label)
      );
    } else {
      fallbackCopy(clean, btn, label);
    }
  }

  // Global entry points used by both Fill Ready and inline Case Detail buttons
  window.fallbackCopy = fallbackCopy;
  window.copyField = function copyField(btn, key) {
    const el = document.getElementById("val-" + key);
    if (!el) return;
    const label = btn.getAttribute("data-label") || key;
    copyValue(el.textContent || el.innerText || "", btn, label);
  };

  document.addEventListener("click", (evt) => {
    const btn = evt.target && evt.target.closest ? evt.target.closest("[data-copy-target]") : null;
    if (!btn) return;
    const targetId = btn.getAttribute("data-copy-target");
    const el = targetId ? document.getElementById(targetId) : null;
    if (!el) return;
    copyValue(el.textContent || el.innerText || "", btn, btn.getAttribute("data-label") || "");
  });

  // Silent Dashboard Auto-Refresh (polls non-PII /dashboard/status every 4s when visible)
  function initDashboardLivePoll() {
    const root = document.querySelector("[data-live-dashboard]");
    if (!root) return;
    let lastSig = root.getAttribute("data-live-sig") || "";
    const pill = document.getElementById("live-status-pill");
    const pillText = document.getElementById("live-status-text");

    function updatePill(data) {
      if (!pill || !pillText) return;
      pill.classList.remove("live-busy", "live-warn");
      if (!data.inbox_ok) {
        pill.classList.add("live-warn");
        pillText.textContent = "Inbox folder missing";
      } else if (data.processing > 0) {
        pill.classList.add("live-busy");
        pillText.textContent = `Processing ${data.processing} file${data.processing === 1 ? "" : "s"}…`;
      } else {
        pillText.textContent = "Watching Inbox · Live";
      }
    }

    async function poll() {
      if (document.visibilityState !== "visible") return;
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) {
        return;
      }
      try {
        const res = await fetch("/dashboard/status", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();
        updatePill(data);
        if (lastSig && data.latest_sig && data.latest_sig !== lastSig) {
          window.location.reload();
          return;
        }
        if (data.latest_sig) lastSig = data.latest_sig;
      } catch (_err) {
        // Ignore transient disconnect while restarting server
      }
    }

    setInterval(poll, 4000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDashboardLivePoll);
  } else {
    initDashboardLivePoll();
  }
})();
