(function () {
  const THOTH_CONFIG = window.THOTH_CONFIG || {};

  async function postJson(url, data) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {}),
    });
  }

  async function getJson(url) {
    return fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  }

  function toast(message) {
    if (window.quickToast) {
      window.quickToast(message);
    }
  }

  function initDesktopExit() {
    const button = document.getElementById("desktop-exit-btn");
    if (!button || !THOTH_CONFIG.desktopEnabled) return;
    button.addEventListener("click", async () => {
      const ok = window.confirm(
        "Exit Thoth Terminal?\n\nThis will stop the desktop shell, local web server, background worker, and managed IBKR gateway."
      );
      if (!ok) return;
      button.disabled = true;
      button.textContent = "Exiting";
      try {
        const res = await postJson("/api/desktop/action", { action: "quit" });
        if (!res.ok) {
          const payload = await res.json().catch(() => ({}));
          throw new Error(payload.error || `Exit failed with HTTP ${res.status}`);
        }
        toast("Exit requested");
      } catch (err) {
        button.disabled = false;
        button.textContent = "Exit";
        toast(err && err.message ? err.message : "Exit request failed");
      }
    });
  }

  function initDesktopUpdater() {
    if (!THOTH_CONFIG.desktopEnabled) return;
    const banner = document.getElementById("desktop-update-banner");
    const text = document.getElementById("desktop-update-text");
    const updateBtn = document.getElementById("desktop-update-restart");
    const dismissBtn = document.getElementById("desktop-update-dismiss");
    const headerCheckBtn = document.getElementById("desktop-update-check-btn");
    if (!banner || !text || !updateBtn || !dismissBtn) return;

    const DISMISS_KEY = "thoth_desktop_update_dismissed";
    let latestStatus = null;
    let pollTimer = null;

    function setHeaderUpdateButton(status, busy) {
      if (!headerCheckBtn) return;
      headerCheckBtn.disabled = !!busy;
      if (busy) {
        headerCheckBtn.textContent = "Checking";
      } else if (status && (status.error || status.state === "error")) {
        headerCheckBtn.textContent = "Update error";
      } else if (status && status.supported === false) {
        headerCheckBtn.textContent = "Update unavailable";
      } else if (status && status.update_available) {
        headerCheckBtn.textContent = status.can_update ? "Update available" : "Update blocked";
      } else if (status && status.restart_available) {
        headerCheckBtn.textContent = "Restart available";
      } else if (status && status.running) {
        headerCheckBtn.textContent = "Updating";
      } else {
        headerCheckBtn.textContent = "Update";
      }
    }

    function dismissToken(status) {
      if (!status) return "";
      return String(status.restart_token || status.upstream_head || status.head || status.checked_at || "");
    }

    function isDismissed(status) {
      const token = dismissToken(status);
      if (!token) return false;
      try {
        return localStorage.getItem(DISMISS_KEY) === token;
      } catch (_e) {
        return false;
      }
    }

    function setDismissed(status) {
      const token = dismissToken(status);
      try {
        if (token) localStorage.setItem(DISMISS_KEY, token);
      } catch (_e) {}
    }

    function hideBanner() {
      banner.style.display = "none";
    }

    function renderUpdater(status, options) {
      latestStatus = status || null;
      const force = !!(options && options.force);
      setHeaderUpdateButton(status, false);
      if (status && status.running) {
        const state = String(status.state || "").toLowerCase();
        banner.dataset.state = state || "running";
        text.textContent = status.message || (state === "restarting" ? "Restarting Thoth Terminal..." : "Updating Thoth Terminal...");
        updateBtn.disabled = true;
        updateBtn.textContent = state === "restarting" ? "Restarting..." : "Updating...";
        banner.style.display = "flex";
        return;
      }
      if (status && (status.error || status.state === "error" || status.supported === false)) {
        if (!force) {
          hideBanner();
          return;
        }
        banner.dataset.state = "error";
        text.textContent = `Update check failed: ${status.error || status.reason || status.blocked_reason || "updater unavailable"}`;
        updateBtn.disabled = true;
        updateBtn.textContent = "Unavailable";
        banner.style.display = "flex";
        return;
      }
      if (!status || (!status.update_available && !status.restart_available)) {
        hideBanner();
        return;
      }
      if (!force && isDismissed(status)) {
        hideBanner();
        return;
      }
      const behind = Number(status.behind || 0);
      const dirty = Number(status.dirty_count || 0);
      const target = status.upstream_head_short ? ` ${status.upstream_head_short}` : "";
      banner.dataset.state = status.state || (status.restart_available ? "restart_available" : "available");
      if (status.update_available && status.can_update) {
        const dirtyNote = dirty ? ` (${dirty} local change${dirty === 1 ? "" : "s"} present; Git will preserve them or stop)` : "";
        text.textContent = `Update available${behind ? `: ${behind} commit${behind === 1 ? "" : "s"}` : ""}${target}${dirtyNote}`;
        updateBtn.disabled = false;
        updateBtn.textContent = "Update and restart";
      } else if (status.restart_available) {
        const count = Number(status.source_change_count || 0);
        text.textContent = `Local source changes detected${count ? ` in ${count} file${count === 1 ? "" : "s"}` : ""}. Restart Thoth to load them.`;
        updateBtn.disabled = false;
        updateBtn.textContent = "Restart Thoth";
      } else {
        text.textContent = `Update blocked: ${status.blocked_reason || "manual intervention required"}`;
        updateBtn.disabled = true;
        updateBtn.textContent = "Blocked";
      }
      banner.style.display = "flex";
    }

    async function refreshDesktopStatus() {
      const res = await getJson("/api/desktop/status");
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || !payload.ok || !payload.desktop) {
        throw new Error(payload.error || `Desktop status failed with HTTP ${res.status}`);
      }
      const status = payload.desktop.updater || null;
      renderUpdater(status, { force: !!(status && (status.running || status.error || status.state === "error")) });
      return status;
    }

    function pollDesktopStatusUntilIdle() {
      if (pollTimer) window.clearTimeout(pollTimer);
      pollTimer = window.setTimeout(async () => {
        try {
          const status = await refreshDesktopStatus();
          if (status && status.running) {
            pollDesktopStatusUntilIdle();
          }
        } catch (err) {
          renderUpdater(
            {
              state: "error",
              error: err && err.message ? err.message : "Desktop status refresh failed.",
            },
            { force: true }
          );
        }
      }, 1500);
    }

    async function checkForUpdates(options) {
      const manual = !!(options && options.manual);
      if (manual) setHeaderUpdateButton(latestStatus, true);
      try {
        const res = await postJson("/api/desktop/action", { action: "check_update" });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok || !payload.desktop) {
          throw new Error(payload.error || `Update check failed with HTTP ${res.status}`);
        }
        const status = payload.desktop.updater || null;
        renderUpdater(status, options || {});
        if (manual && status && (status.error || status.state === "error" || status.supported === false)) {
          toast(status.error || status.reason || status.blocked_reason || "Desktop updater is unavailable.");
        } else if (manual && (!status || (!status.update_available && !status.restart_available))) {
          if (status && Number(status.dirty_count || 0)) {
            toast("No remote update. Local changes are present, but no restart-only source change was detected.");
          } else {
            toast("No desktop update available.");
          }
        } else if (manual && status && status.update_available && !status.can_update) {
          toast(status.blocked_reason || "Desktop update needs manual attention.");
        }
        return status;
      } catch (err) {
        setHeaderUpdateButton(latestStatus, false);
        if (manual) {
          toast(err && err.message ? err.message : "Update check failed.");
        }
        hideBanner();
        return null;
      }
    }

    updateBtn.addEventListener("click", async () => {
      const restartOnly = !!(latestStatus && latestStatus.restart_available && !latestStatus.update_available);
      if (!latestStatus || (!latestStatus.can_update && !restartOnly)) return;
      const ok = window.confirm(
        restartOnly
          ? "Restart Thoth Terminal?\n\nThis will close and reopen the desktop app so it loads local source changes already on disk."
          : "Update and restart Thoth Terminal?\n\nThis will fast-forward the local checkout, reinstall desktop dependencies, close this window, and reopen the app."
      );
      if (!ok) return;
      updateBtn.disabled = true;
      updateBtn.textContent = restartOnly ? "Restarting..." : "Updating...";
      text.textContent = restartOnly
        ? "Restarting Thoth Terminal..."
        : "Updating local checkout. Thoth will restart when it finishes.";
      banner.dataset.state = restartOnly ? "restarting" : "applying";
      banner.style.display = "flex";
      try {
        const res = await postJson("/api/desktop/action", { action: restartOnly ? "restart_desktop" : "apply_update_restart" });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          throw new Error(payload.error || `${restartOnly ? "Restart" : "Update"} failed with HTTP ${res.status}`);
        }
        if (payload.desktop && payload.desktop.updater) {
          renderUpdater(payload.desktop.updater, { force: true });
          if (payload.desktop.updater.running) {
            pollDesktopStatusUntilIdle();
          }
        }
        text.textContent = restartOnly
          ? "Restart started. Thoth will close and reopen automatically."
          : "Update started. Thoth will close and reopen automatically.";
      } catch (err) {
        updateBtn.disabled = false;
        updateBtn.textContent = restartOnly ? "Restart Thoth" : "Update and restart";
        text.textContent = err && err.message ? err.message : "Update failed.";
        banner.dataset.state = "error";
      }
    });

    dismissBtn.addEventListener("click", () => {
      setDismissed(latestStatus);
      hideBanner();
    });

    if (headerCheckBtn) {
      headerCheckBtn.addEventListener("click", () => {
        checkForUpdates({ force: true, manual: true });
      });
    }

    window.THOTH_DESKTOP_UPDATER = {
      check: () => checkForUpdates({ force: true }),
      refresh: refreshDesktopStatus,
      render: renderUpdater,
      pollUntilIdle: pollDesktopStatusUntilIdle,
    };

    window.setTimeout(() => checkForUpdates({ force: false }), 3500);
    window.setInterval(() => checkForUpdates({ force: false }), 10 * 60 * 1000);
  }

  initDesktopExit();
  initDesktopUpdater();
})();
