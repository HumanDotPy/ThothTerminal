(function () {
  function isAbortError(error) {
    return error && error.name === "AbortError";
  }

  const THOTH_CONFIG = window.THOTH_CONFIG || {};
  const UI_PREFS_KEY = "thoth_ui_prefs";
  const PORTFOLIO_TARGETS_KEY = "thoth_portfolio_targets_v1";
  const UI_PREF_DEFAULTS = {
    tone: "measured",
    intensity: "signal",
    density: "balanced",
  };
  const DEFAULT_PORTFOLIO_TARGETS = [
    { id: "2026-q2", label: "Q2 2026", due: "2026-06-30", value: 50000 },
    { id: "2026-q3", label: "Q3 2026", due: "2026-09-30", value: 60000 },
    { id: "2026-eoy-blue-sky", label: "EOY 2026 Blue Sky", due: "2026-12-31", value: 70000 },
    { id: "2027-q2", label: "Q2 2027", due: "2027-06-30", value: 100000 },
  ];

  function initClientPresencePolicy() {
    if (window.ThothClientPresence && typeof window.ThothClientPresence.read === "function") {
      return window.ThothClientPresence;
    }
    const activeMs = Math.max(5000, Number(THOTH_CONFIG.clientPresenceActiveS || 30) * 1000);
    const recentMs = Math.max(activeMs, Number(THOTH_CONFIG.clientPresenceRecentS || 300) * 1000);
    let lastActiveAt = Date.now();
    let lastState = "";

    function classify() {
      if (document.visibilityState && document.visibilityState !== "visible") return "hidden";
      if (!document.hasFocus()) return "idle";
      const age = Date.now() - lastActiveAt;
      if (age <= activeMs) return "active";
      if (age <= recentMs) return "recent";
      return "idle";
    }

    function read() {
      return {
        state: classify(),
        last_active_at: new Date(lastActiveAt).toISOString(),
        idle_ms: Math.max(0, Date.now() - lastActiveAt),
      };
    }

    function headers(extra) {
      const base = Object.assign({}, extra || {});
      base["X-Thoth-Client-Presence"] = classify();
      return base;
    }

    function emitIfChanged() {
      const state = classify();
      if (state === lastState) return;
      const previous = lastState || null;
      lastState = state;
      window.dispatchEvent(new CustomEvent("thoth:presencechange", { detail: { state, previous, presence: read() } }));
    }

    function markActive() {
      lastActiveAt = Date.now();
      emitIfChanged();
    }

    ["mousemove", "mousedown", "keydown", "scroll", "touchstart", "pointerdown"].forEach((evt) => {
      window.addEventListener(evt, markActive, { passive: true });
    });
    document.addEventListener("visibilitychange", emitIfChanged);
    window.addEventListener("focus", markActive);
    window.addEventListener("blur", emitIfChanged);
    window.setInterval(emitIfChanged, Math.min(60000, Math.max(5000, activeMs)));
    emitIfChanged();

    window.ThothClientPresence = { read, state: classify, headers, markActive };
    return window.ThothClientPresence;
  }

  const clientPresence = initClientPresencePolicy();

  function cloneDefaultPortfolioTargets() {
    return DEFAULT_PORTFOLIO_TARGETS.map((row) => Object.assign({}, row));
  }

  function normalizePortfolioTarget(row, index) {
    const fallback = DEFAULT_PORTFOLIO_TARGETS[index] || DEFAULT_PORTFOLIO_TARGETS[DEFAULT_PORTFOLIO_TARGETS.length - 1];
    const value = Number(row && row.value);
    return {
      id: String((row && row.id) || fallback.id || `target-${index + 1}`),
      label: String((row && row.label) || fallback.label || `Target ${index + 1}`),
      due: String((row && row.due) || fallback.due || ""),
      value: Number.isFinite(value) && value > 0 ? value : Number(fallback.value || 0),
    };
  }

  function readPortfolioTargets() {
    try {
      const raw = localStorage.getItem(PORTFOLIO_TARGETS_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (Array.isArray(parsed) && parsed.length) {
        return parsed.map((row, index) => normalizePortfolioTarget(row, index)).filter((row) => row.value > 0);
      }
    } catch (_e) {}
    return cloneDefaultPortfolioTargets();
  }

  function writePortfolioTargets(targets) {
    const normalized = Array.isArray(targets)
      ? targets.map((row, index) => normalizePortfolioTarget(row, index)).filter((row) => row.value > 0)
      : cloneDefaultPortfolioTargets();
    try {
      localStorage.setItem(PORTFOLIO_TARGETS_KEY, JSON.stringify(normalized));
    } catch (_e) {}
    return normalized;
  }

  function formatMoneyCompact(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return "$0";
    return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }

  function parseDueDate(target) {
    const due = Date.parse(`${target && target.due ? target.due : ""}T23:59:59`);
    return Number.isFinite(due) ? due : Number.POSITIVE_INFINITY;
  }

  function activePortfolioTarget(targets, now = new Date()) {
    const sorted = (Array.isArray(targets) && targets.length ? targets : cloneDefaultPortfolioTargets())
      .map((row, index) => normalizePortfolioTarget(row, index))
      .sort((a, b) => parseDueDate(a) - parseDueDate(b));
    const nowMs = now instanceof Date ? now.getTime() : Date.now();
    const activeIndex = sorted.findIndex((row) => parseDueDate(row) >= nowMs);
    const index = activeIndex >= 0 ? activeIndex : sorted.length - 1;
    return {
      target: sorted[index],
      previous: index > 0 ? sorted[index - 1] : null,
      index,
      targets: sorted,
    };
  }

  function basePortfolioMood(metrics) {
    const day = Number(metrics && metrics.daily_pnl_pct != null ? metrics.daily_pnl_pct : 0);
    if (!Number.isFinite(day)) return "neutral";
    if (day >= 5) return "manic";
    if (day >= 2) return "ecstatic";
    if (day >= 0.5) return "happy";
    if (day <= -5) return "miserable";
    if (day <= -2) return "depressed";
    if (day <= -0.5) return "sad";
    return "neutral";
  }

  function positiveMoodAtLeast(current, desired) {
    const rank = { neutral: 0, happy: 1, ecstatic: 2, manic: 3, victory: 4 };
    const currentRank = rank[current] || 0;
    const desiredRank = rank[desired] || 0;
    return desiredRank > currentRank ? desired : current;
  }

  function evaluatePortfolioTarget(metrics, options = {}) {
    const active = activePortfolioTarget(options.targets || readPortfolioTargets(), options.now || new Date());
    const target = active.target;
    const basis = options.basis && typeof options.basis === "object" ? options.basis : {};
    const nlv = Number(metrics && metrics.net_liquidation);
    const realized = Number(metrics && metrics.realized_pnl);
    const unrealized = Number(metrics && metrics.unrealized_pnl);
    const startingEquity = Number(basis.starting_equity);
    const netContributions = Number(basis.net_contributions || 0);
    const positionCostBasis = Number(metrics && metrics.estimated_position_cost_basis);
    const hasNlv = Number.isFinite(nlv) && nlv > 0;
    const hasGainProxy = Number.isFinite(realized) || Number.isFinite(unrealized);
    const contributionAdjustedGain = hasNlv && Number.isFinite(startingEquity) && startingEquity > 0
      ? nlv - startingEquity - (Number.isFinite(netContributions) ? netContributions : 0)
      : null;
    const positionBasisGain = hasNlv && Number.isFinite(positionCostBasis) && positionCostBasis > 0
      ? nlv - positionCostBasis
      : null;
    const pnlGain = (Number.isFinite(realized) ? realized : 0) + (Number.isFinite(unrealized) ? unrealized : 0);
    let gainProxy = hasGainProxy ? pnlGain : null;
    let gainSource = hasGainProxy ? "realized plus unrealized P&L" : "";
    if (positionBasisGain != null) {
      gainProxy = positionBasisGain;
      gainSource = "estimated position cost basis";
    }
    if (contributionAdjustedGain != null) {
      gainProxy = contributionAdjustedGain;
      gainSource = "starting equity less net contributions";
    }
    const targetValue = Number(target && target.value ? target.value : 0);
    const progress = hasNlv && targetValue > 0 ? nlv / targetValue : 0;
    const distance = hasNlv && targetValue > 0 ? targetValue - nlv : null;
    const previousValue = Number(active.previous && active.previous.value ? active.previous.value : 0);
    const rungStart = previousValue > 0 && previousValue < targetValue ? previousValue : 0;
    const rungProgress = hasNlv && targetValue > rungStart ? (nlv - rungStart) / (targetValue - rungStart) : progress;
    const estimatedCostBasis = gainProxy != null && hasNlv ? nlv - gainProxy : null;
    const organicProgress = gainProxy != null && targetValue > 0 ? gainProxy / targetValue : null;
    return {
      target,
      previous: active.previous,
      index: active.index,
      targets: active.targets,
      netLiquidation: hasNlv ? nlv : null,
      progress,
      rungProgress,
      distance,
      estimatedGain: gainProxy,
      estimatedCostBasis,
      gainSource,
      organicProgress,
      progressLabel: hasNlv ? `${Math.max(0, progress * 100).toFixed(progress >= 1 ? 0 : 1)}%` : "WAITING",
      distanceLabel: distance == null ? "NO SNAPSHOT" : distance <= 0 ? `${formatMoneyCompact(Math.abs(distance))} over` : `${formatMoneyCompact(distance)} left`,
      organicLabel: organicProgress == null ? "GAIN MIX UNKNOWN" : `${Math.max(0, organicProgress * 100).toFixed(1)}% gain-led`,
    };
  }

  function evaluatePortfolioMood(metrics, options = {}) {
    if (!options.connected) {
      return {
        mood: "neutral",
        baseMood: "neutral",
        targetState: evaluatePortfolioTarget({}, options),
        reasons: ["Portfolio target ladder is ready; waiting for a live account snapshot."],
      };
    }
    const targetState = evaluatePortfolioTarget(metrics || {}, options);
    const baseMood = basePortfolioMood(metrics || {});
    const day = Number(metrics && metrics.daily_pnl_pct != null ? metrics.daily_pnl_pct : 0);
    let mood = baseMood;
    const progress = Number(targetState.progress || 0);
    const rungProgress = Number(targetState.rungProgress || 0);
    const organic = Number(targetState.organicProgress || 0);

    if (progress >= 1 && day >= -2) {
      mood = "victory";
    } else if (progress >= 0.97 && day >= -0.5) {
      mood = positiveMoodAtLeast(mood, organic >= 0.05 ? "ecstatic" : "happy");
    } else if ((progress >= 0.9 || rungProgress >= 0.8) && day >= -0.5) {
      mood = positiveMoodAtLeast(mood, "happy");
    } else if (organic >= 0.08 && day >= -0.25) {
      mood = positiveMoodAtLeast(mood, "happy");
    }

    const reasons = [
      `${targetState.target.label}: ${targetState.progressLabel} of ${formatMoneyCompact(targetState.target.value)}.`,
      targetState.distance == null ? "Live NLV is unavailable." : `${targetState.distanceLabel} to the active target.`,
    ];
    if (targetState.organicProgress != null) {
      reasons.push(`${targetState.organicLabel} using ${targetState.gainSource || "available portfolio data"} as a cost-basis proxy.`);
    }

    return { mood, baseMood, targetState, reasons };
  }

  window.ThothPortfolioMood = {
    key: PORTFOLIO_TARGETS_KEY,
    defaults: DEFAULT_PORTFOLIO_TARGETS,
    readTargets: readPortfolioTargets,
    writeTargets: writePortfolioTargets,
    activeTarget: activePortfolioTarget,
    evaluateTarget: evaluatePortfolioTarget,
    evaluate: evaluatePortfolioMood,
    moodFromPerformance: basePortfolioMood,
    money: formatMoneyCompact,
  };

  function sanitizeUiPrefs(raw) {
    const prefs = Object.assign({}, UI_PREF_DEFAULTS, raw || {});
    prefs.tone = ["measured", "evangelion"].includes(prefs.tone) ? prefs.tone : UI_PREF_DEFAULTS.tone;
    prefs.intensity = ["quiet", "signal", "alarm"].includes(prefs.intensity) ? prefs.intensity : UI_PREF_DEFAULTS.intensity;
    prefs.density = ["balanced", "dense"].includes(prefs.density) ? prefs.density : UI_PREF_DEFAULTS.density;
    return prefs;
  }

  function applyUiPrefs(raw) {
    const prefs = sanitizeUiPrefs(raw);
    const root = document.documentElement;
    root.dataset.uiTone = prefs.tone;
    root.dataset.uiIntensity = prefs.intensity;
    root.dataset.uiDensity = prefs.density;
    return prefs;
  }

  function readUiPrefs() {
    try {
      const raw = localStorage.getItem(UI_PREFS_KEY);
      return applyUiPrefs(raw ? JSON.parse(raw) : UI_PREF_DEFAULTS);
    } catch (_e) {
      return applyUiPrefs(UI_PREF_DEFAULTS);
    }
  }

  function writeUiPrefs(nextPrefs) {
    const prefs = applyUiPrefs(nextPrefs);
    try {
      localStorage.setItem(UI_PREFS_KEY, JSON.stringify(prefs));
    } catch (_e) {}
    return prefs;
  }

  window.THOTH_UI_PREFS = {
    defaults: Object.assign({}, UI_PREF_DEFAULTS),
    read: readUiPrefs,
    write: writeUiPrefs,
    apply: applyUiPrefs,
  };

  readUiPrefs();

  function quickToast(message) {
    const toastContainer = document.getElementById("toast-container");
    if (!toastContainer || !message) return;
    const el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = `<strong>Thoth</strong> · ${message}`;
    toastContainer.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }
  window.quickToast = quickToast;

  function initGlobalMoodRefresh() {
    const moodRuntime = window.ThothMoodState;
    if (!moodRuntime || !moodRuntime.write || document.body.classList.contains("route-home")) return;

    const REFRESH_MS = 60 * 1000;
    const PREVIEW_HOLD_MS = 5 * 60 * 1000;
    const PORTFOLIO_CACHE_KEY = "thoth_home_portfolio_background_v1";
    const PORTFOLIO_FETCH_LOCK_KEY = "thoth_home_portfolio_background_lock_v1";
    const PORTFOLIO_CACHE_TTL_MS = 45 * 1000;
    const PORTFOLIO_FETCH_LOCK_MS = 25 * 1000;

    function hasLivePortfolio(status, state, metrics) {
      return Boolean(status && status.connected && (state?.account_id || Number(metrics?.net_liquidation || 0) > 0));
    }

    function moodFromPerformance(metrics) {
      return window.ThothPortfolioMood?.moodFromPerformance
        ? window.ThothPortfolioMood.moodFromPerformance(metrics)
        : "neutral";
    }

    function previewIsFresh(state) {
      if (!state || state.source !== "preview") return false;
      const updatedAt = Date.parse(state.updatedAt || "");
      return Number.isFinite(updatedAt) && Date.now() - updatedAt < PREVIEW_HOLD_MS;
    }

    function readCachedPortfolio() {
      try {
        const raw = localStorage.getItem(PORTFOLIO_CACHE_KEY);
        const cached = raw ? JSON.parse(raw) : null;
        if (!cached || !cached.saved_at || !cached.payload) return null;
        if (Date.now() - Number(cached.saved_at) > PORTFOLIO_CACHE_TTL_MS) return null;
        return cached.payload;
      } catch (_e) {
        return null;
      }
    }

    function writeCachedPortfolio(payload) {
      try {
        localStorage.setItem(PORTFOLIO_CACHE_KEY, JSON.stringify({ saved_at: Date.now(), payload }));
      } catch (_e) {}
    }

    function fetchLockIsFresh() {
      try {
        return Number(localStorage.getItem(PORTFOLIO_FETCH_LOCK_KEY) || "0") > Date.now();
      } catch (_e) {
        return false;
      }
    }

    function acquireFetchLock() {
      const token = Date.now() + PORTFOLIO_FETCH_LOCK_MS;
      try {
        localStorage.setItem(PORTFOLIO_FETCH_LOCK_KEY, String(token));
      } catch (_e) {}
      return String(token);
    }

    function releaseFetchLock(token) {
      try {
        if (localStorage.getItem(PORTFOLIO_FETCH_LOCK_KEY) === token) {
          localStorage.removeItem(PORTFOLIO_FETCH_LOCK_KEY);
        }
      } catch (_e) {}
    }

    function applyPortfolioPayload(data, current) {
      const status = data.ibkr_status || {};
      const mood = data.portfolio_mood || {};
      const state = data.portfolio_state || {};
      const targetProfile = data.portfolio_targets || {};
      const metrics = state.metrics || mood.metrics || {};
      const connected = hasLivePortfolio(status, state, metrics);
      if (!connected) return current;
      const evaluation = window.ThothPortfolioMood?.evaluate
        ? window.ThothPortfolioMood.evaluate(metrics, { connected, targets: targetProfile.targets, basis: targetProfile.basis })
        : { mood: connected ? moodFromPerformance(metrics) : "neutral", targetState: null };
      const displayMood = evaluation.mood || current.mood || "neutral";
      moodRuntime.write({
        mood: displayMood,
        character: current.character || "omori",
        source: connected ? "portfolio" : "default",
        theme: moodRuntime.fallbackThemes?.[displayMood],
        backgroundImage: moodRuntime.fallbackBackgrounds?.[displayMood],
        portfolioTarget: evaluation.targetState || null,
      });
    }

    async function refreshMood() {
      const current = moodRuntime.read ? moodRuntime.read() : {};
      if (previewIsFresh(current)) return;

      const cached = readCachedPortfolio();
      if (cached) {
        applyPortfolioPayload(cached, current);
        return;
      }
      if (fetchLockIsFresh()) return;
      const lockToken = acquireFetchLock();
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 20000);
      try {
        const resp = await fetch("/api/home/portfolio?live=false&include_market=false", {
          headers: clientPresence.headers({ Accept: "application/json" }),
          signal: controller.signal,
        });
        if (!resp.ok) return;
        const data = await resp.json();
        writeCachedPortfolio(data);
        applyPortfolioPayload(data, current);
      } catch (_e) {
      } finally {
        window.clearTimeout(timeout);
        releaseFetchLock(lockToken);
      }
    }

    window.setTimeout(refreshMood, 8000);
    window.setInterval(() => {
      if (document.visibilityState !== "hidden") refreshMood();
    }, REFRESH_MS);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshMood();
    });
    window.addEventListener("storage", (event) => {
      if (event.key !== PORTFOLIO_CACHE_KEY || document.visibilityState === "hidden") return;
      const current = moodRuntime.read ? moodRuntime.read() : {};
      if (previewIsFresh(current)) return;
      const cached = readCachedPortfolio();
      if (cached) applyPortfolioPayload(cached, current);
    });
  }
  initGlobalMoodRefresh();

  function formatLocalTimestamp(raw) {
    if (!raw) return null;
    let iso = String(raw).trim();
    if (!iso) return null;
    iso = iso.replace(" ", "T");
    if (!/(?:[zZ]|[+-]\d\d:?\d\d)$/.test(iso)) {
      iso = `${iso}Z`;
    }
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return null;
    const now = new Date();
    const opts = {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
      hour12: true
    };
    if (dt.getFullYear() !== now.getFullYear()) {
      opts.year = "numeric";
    }
    return dt.toLocaleString("en-US", opts);
  }

  function localizeTimestamps(root) {
    const scope = root || document;
    const nodes = scope.querySelectorAll("[data-utc]");
    if (!nodes.length) return;
    nodes.forEach((el) => {
      const raw = el.getAttribute("data-utc");
      const formatted = formatLocalTimestamp(raw);
      if (!formatted) return;
      el.textContent = formatted;
      el.setAttribute("title", formatted);
    });
  }

  // Global search + keyboard help
  (function () {
    const globalInput = document.getElementById("global-search-input");
    const overlay = document.getElementById("global-search-overlay");
    const closeBtn = document.getElementById("global-search-close");
    const queryInput = document.getElementById("global-search-query");
    const results = document.getElementById("global-search-results");
    const kindWrap = document.getElementById("global-search-kinds");
    const helpOverlay = document.getElementById("keyboard-help-overlay");
    const helpClose = document.getElementById("keyboard-help-close");

    if (!overlay || !queryInput || !results) return;

    let activeKind = "all";
    let timer = null;
    let queryId = 0;
    let searchAbort = null;

    function abortSearch() {
      if (searchAbort) {
        searchAbort.abort();
        searchAbort = null;
      }
    }

    function setKind(kind) {
      activeKind = kind;
      if (kindWrap) {
        Array.from(kindWrap.querySelectorAll(".filter-chip")).forEach((btn) => {
          btn.classList.toggle("active", btn.getAttribute("data-kind") === kind);
        });
      }
      runSearch();
    }

    function openOverlay(seed) {
      overlay.style.display = "flex";
      window.__THOTH_OVERLAY_OPEN = true;
      if (seed) queryInput.value = seed;
      queryInput.focus();
      runSearch();
    }

    function closeOverlay() {
      abortSearch();
      overlay.style.display = "none";
      window.__THOTH_OVERLAY_OPEN = false;
    }

    function openHelp() {
      if (!helpOverlay) return;
      helpOverlay.style.display = "flex";
      window.__THOTH_OVERLAY_OPEN = true;
    }

    function closeHelp() {
      if (!helpOverlay) return;
      helpOverlay.style.display = "none";
      window.__THOTH_OVERLAY_OPEN = false;
    }

    function renderResults(items) {
      results.innerHTML = "";
      if (!items.length) {
        results.innerHTML = `<div style="padding:12px;color:var(--muted);font-family:var(--mono);font-size:12px;">No results.</div>`;
        return;
      }
      items.forEach((it) => {
        const row = document.createElement("div");
        row.className = "search-item";
        row.innerHTML = `<div style="display:flex;justify-content:space-between;gap:12px;"><span>${it.label || it.id}</span><span class="pill">${it.kind || ""}</span></div>`;
        row.addEventListener("click", () => {
          if (it.href) window.location.href = it.href;
        });
        results.appendChild(row);
      });
    }

    async function runSearch() {
      clearTimeout(timer);
      const q = (queryInput.value || "").trim();
      if (!q) {
        abortSearch();
        results.innerHTML = "";
        return;
      }
      timer = setTimeout(async () => {
        const myId = ++queryId;
        abortSearch();
        const controller = new AbortController();
        searchAbort = controller;
        try {
          localStorage.setItem("thoth_global_search_q", q);
        } catch (_e) {}
        try {
          const res = await fetch(`/api/search?kind=${encodeURIComponent(activeKind)}&q=${encodeURIComponent(q)}&limit=18`, {
            headers: clientPresence.headers({ Accept: "application/json" }),
            signal: controller.signal,
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = await res.json();
          if (myId !== queryId) return;
          const items = Array.isArray(json.items) ? json.items : [];
          renderResults(items);
        } catch (error) {
          if (!isAbortError(error) && myId === queryId) results.innerHTML = "";
        } finally {
          if (searchAbort === controller) searchAbort = null;
        }
      }, 120);
    }

    if (globalInput) {
      globalInput.addEventListener("focus", () => openOverlay(globalInput.value || ""));
      globalInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          openOverlay(globalInput.value || "");
        }
      });
      try {
        const last = localStorage.getItem("thoth_global_search_q");
        if (last) globalInput.value = last;
      } catch (_e) {}
    }

    if (kindWrap) {
      kindWrap.addEventListener("click", (e) => {
        const btn = e.target.closest(".filter-chip");
        if (!btn) return;
        setKind(btn.getAttribute("data-kind") || "all");
      });
    }

    queryInput.addEventListener("input", runSearch);
    if (closeBtn) closeBtn.addEventListener("click", closeOverlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeOverlay();
    });

    if (helpClose) helpClose.addEventListener("click", closeHelp);
    if (helpOverlay) {
      helpOverlay.addEventListener("click", (e) => {
        if (e.target === helpOverlay) closeHelp();
      });
    }

    document.addEventListener("keydown", (e) => {
      const tag = (e.target && e.target.tagName ? e.target.tagName.toLowerCase() : "");
      const inField = ["input", "textarea", "select"].includes(tag);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openOverlay(globalInput ? globalInput.value : "");
      } else if (e.key === "?" && !(e.metaKey || e.ctrlKey) && !inField) {
        e.preventDefault();
        openHelp();
      } else if (e.key === "Escape") {
        if (overlay.style.display !== "none") closeOverlay();
        if (helpOverlay && helpOverlay.style.display !== "none") closeHelp();
      }
    });
  })();

  localizeTimestamps(document);

  // In-app notifications (ingest updates) with new items banner
  (function () {
    const pollS = Number(THOTH_CONFIG.notificationPollS || 20);
    if (!pollS || pollS <= 0) return;
    const toastContainer = document.getElementById("toast-container");
    if (!toastContainer) return;

    // New items banner elements
    const newItemsBanner = document.getElementById("new-items-banner");
    const newItemsCount = document.getElementById("new-items-count");
    const newItemsLoad = document.getElementById("new-items-load");
    const newItemsDismiss = document.getElementById("new-items-dismiss");
    let pendingNewItems = 0;

    function readStoredLastIdRaw() {
      try {
        return localStorage.getItem("thoth_last_ingest_id");
      } catch (_e) {
        return null;
      }
    }

    function readLastId() {
      const parsed = Number(readStoredLastIdRaw() || "0");
      return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    }

    // Seed only on first browser visit. A stale stored id means the user has unseen updates.
    const serverLatestId = Number(THOTH_CONFIG.latestIngestId || 0);
    const storedLastIdRaw = readStoredLastIdRaw();
    const hasStoredLastId = storedLastIdRaw !== null;
    let lastId = readLastId();
    if (serverLatestId > 0 && !hasStoredLastId) {
      lastId = serverLatestId;
      try {
        localStorage.setItem("thoth_last_ingest_id", String(lastId));
      } catch (_e) {}
    }
    const initialCatchupDue = hasStoredLastId && serverLatestId > lastId;

    window.addEventListener("storage", (e) => {
      if (e.key === "thoth_last_ingest_id" && e.newValue) {
        const v = Number(e.newValue || "0");
        if (!Number.isNaN(v)) lastId = Math.max(lastId, v);
      }
    });

    let lastSoundAt = 0;
    let lastToastAt = 0;
    const soundRateMs = Number(THOTH_CONFIG.notificationSoundRateS || 60) * 1000;
    const toastRateMs = Number(THOTH_CONFIG.notificationToastRateS || 25) * 1000;
    const dedupeWindowMs = Number(THOTH_CONFIG.notificationDedupeWindowS || 12) * 1000;
    const requireIdle = !!THOTH_CONFIG.notificationRequireIdle;
    const idleMs = Number(THOTH_CONFIG.notificationIdleS || 18) * 1000;

    let lastActiveAt = Date.now();
    function markActive() {
      lastActiveAt = Date.now();
    }
    ["mousemove", "mousedown", "keydown", "scroll", "touchstart"].forEach((evt) => {
      window.addEventListener(evt, markActive, { passive: true });
    });

    function shouldNotifyNow() {
      if (!requireIdle) return true;
      if (document.visibilityState === "hidden") return true;
      if (!document.hasFocus()) return true;
      return Date.now() - lastActiveAt > idleMs;
    }

    function bumpInbox(count) {
      const inbox = document.querySelector(".feed-tab");
      if (!inbox) return;
      const countEl = inbox.querySelector(".count");
      if (!countEl) return;
      const cur = parseInt(countEl.textContent || "0", 10);
      if (!Number.isNaN(cur)) countEl.textContent = String(cur + count);
    }

    function showNewItemsBanner(count) {
      if (!newItemsBanner || !newItemsCount) return;
      pendingNewItems += count;
      newItemsCount.textContent = String(pendingNewItems);
      newItemsBanner.style.display = "flex";
    }

    function hideNewItemsBanner() {
      if (!newItemsBanner) return;
      newItemsBanner.style.display = "none";
      pendingNewItems = 0;
    }

    // Banner handlers
    if (newItemsLoad) {
      newItemsLoad.addEventListener("click", async () => {
        // Try live append first, fall back to reload
        const feedDocs = document.getElementById("feed-docs");
        if (!feedDocs) {
          window.location.reload();
          return;
        }
        
        // Get current filters from data attributes
        const triage = feedDocs.dataset.view || "NEW";
        const firstDoc = feedDocs.querySelector(".doc[data-doc-id]");
        const afterId = firstDoc ? parseInt(firstDoc.dataset.docId, 10) : 0;
        
        try {
          const params = new URLSearchParams({
            after_id: String(afterId),
            triage: triage,
            limit: "50"
          });
          const res = await fetch(`/api/feed/recent?${params}`, {
            headers: clientPresence.headers({ Accept: "application/json" }),
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = await res.json();
          const items = Array.isArray(json.items) ? json.items : [];
          
          if (items.length === 0) {
            hideNewItemsBanner();
            return;
          }
          
          // Prepend new items to the feed
          items.reverse().forEach((item) => {
            const docEl = createDocElement(item);
            if (feedDocs.firstChild) {
              feedDocs.insertBefore(docEl, feedDocs.firstChild);
            } else {
              feedDocs.appendChild(docEl);
            }
            // Animate entry
            docEl.style.opacity = "0";
            docEl.style.transform = "translateY(-10px)";
            requestAnimationFrame(() => {
              docEl.style.transition = "opacity .3s ease, transform .3s ease";
              docEl.style.opacity = "1";
              docEl.style.transform = "translateY(0)";
            });
          });
          
          hideNewItemsBanner();
          quickToast(`${items.length} new items loaded`);
        } catch (_e) {
          // Fall back to reload on error
          window.location.reload();
        }
      });
    }
    if (newItemsDismiss) {
      newItemsDismiss.addEventListener("click", hideNewItemsBanner);
    }
    
    // Create a document element for live insertion
    function createDocElement(item) {
      const div = document.createElement("div");
      div.className = "doc doc-clickable";
      div.dataset.docId = String(item.id);
      div.dataset.triage = item.triage_state || "NEW";
      div.dataset.saved = item.saved ? "1" : "0";
      div.dataset.href = `/docs/${item.id}`;
      
      const thumbHtml = item.thumb_path
        ? `<img class="thumb" src="/objects/${item.thumb_path}" alt="" loading="lazy" />`
        : item.thumb_url
        ? `<img class="thumb" src="${item.thumb_url}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
        : `<div class="thumb placeholder"></div>`;
      
      const langPill = item.language ? `<span class="pill">${item.language}</span>` : "";
      const transPill = item.has_translation ? `<span class="pill">EN ✓</span>` : "";
      const savedPill = item.saved ? `<span class="pill">saved</span>` : "";
      const domainPills = (item.domains || []).map(d => `<span class="pill">${d}</span>`).join("");
      const sourceUrl = item.url ? String(item.url) : "";
      const sourceLink = sourceUrl
        ? `<a class="btn" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">Source</a>`
        : "";
      
      div.innerHTML = `
        <div><input type="checkbox" data-doc-id="${item.id}" /></div>
        <div class="doc-thumb">${thumbHtml}</div>
        <div>
          <div class="meta">
            <span>${item.source_name}</span>
            <span>·</span>
            <span data-utc="${item.published_at || ''}">${formatLocalTimestamp(item.published_at) || ''}</span>
            ${langPill}
            ${transPill}
            <span class="pill">${item.triage_state}</span>
            ${savedPill}
          </div>
          <div class="doc-actions-row">
            ${sourceLink}
            <details class="doc-menu">
              <summary class="btn" title="Actions">Actions</summary>
              <div class="doc-menu-panel">
                <button class="btn doc-action" type="button" data-action="note" data-doc-id="${item.id}">New note</button>
                <button class="btn doc-action" type="button" data-action="thesis" data-doc-id="${item.id}">Seed thesis</button>
                <button class="btn doc-action" type="button" data-action="evidence" data-doc-id="${item.id}">Attach evidence</button>
                <button class="btn doc-triage" type="button" data-doc-action="save">Save</button>
                <button class="btn doc-triage" type="button" data-doc-action="actioned">Actioned</button>
                <button class="btn doc-triage" type="button" data-doc-action="ignore">Ignore</button>
                <button class="btn doc-triage" type="button" data-doc-action="reset_to_new">Reset</button>
              </div>
            </details>
          </div>
          <h3><a href="/docs/${item.id}">${escapeHtml(item.title)}</a></h3>
          <div class="excerpt">${escapeHtml(item.excerpt)}${item.excerpt.length >= 420 ? '…' : ''}</div>
          ${domainPills ? `<div class="domains">${domainPills}</div>` : ''}
        </div>
      `;
      
      // Make clickable
      div.addEventListener("click", (e) => {
        const target = e.target;
        if (!target) return;
        if (target.closest("a, button, input, select, textarea, summary, details")) return;
        const href = div.dataset.href;
        if (!href) return;
        if (e.metaKey || e.ctrlKey) {
          window.open(href, "_blank", "noopener");
          return;
        }
        window.location.href = href;
      });
      
      return div;
    }
    
    function escapeHtml(text) {
      const div = document.createElement("div");
      div.textContent = text || "";
      return div.innerHTML;
    }

    function shouldDedupe(message) {
      if (!dedupeWindowMs || dedupeWindowMs <= 0) return false;
      try {
        const lastTs = Number(localStorage.getItem("thoth_last_toast_at") || "0");
        const lastMsg = localStorage.getItem("thoth_last_toast_msg") || "";
        if (Date.now() - lastTs < dedupeWindowMs && message === lastMsg) {
          return true;
        }
      } catch (_e) {}
      return false;
    }

    function noteToast(message) {
      try {
        localStorage.setItem("thoth_last_toast_at", String(Date.now()));
        localStorage.setItem("thoth_last_toast_msg", String(message || ""));
      } catch (_e) {}
    }

    function showToast(message) {
      const now = Date.now();
      if (toastRateMs && now - lastToastAt < toastRateMs) return;
      if (shouldDedupe(message)) return;
      lastToastAt = now;
      noteToast(message);
      const el = document.createElement("div");
      el.className = "toast";
      el.innerHTML = `<strong>Thoth</strong> · ${message}`;
      toastContainer.appendChild(el);
      setTimeout(() => {
        el.remove();
      }, 5000);
    }

    function playSound() {
      const now = Date.now();
      if (now - lastSoundAt < soundRateMs) return;
      lastSoundAt = now;
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = 660;
        gain.gain.value = 0.03;
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.12);
      } catch (_e) {}
    }

    const visiblePollMs = Math.max(1000, pollS * 1000);
    const idlePollMs = Math.max(visiblePollMs * 4, 120000);
    const offlinePollMs = Math.max(visiblePollMs * 12, 300000);
    let polling = false;
    let pollTimer = null;

    function clearPollTimer() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function schedulePoll(delayMs) {
      clearPollTimer();
      if (document.visibilityState && document.visibilityState !== "visible") {
        return;
      }
      const presenceState = clientPresence.state ? clientPresence.state() : "active";
      const waitMs = navigator && navigator.onLine === false
        ? offlinePollMs
        : presenceState === "idle"
        ? Math.max(idlePollMs, Number(delayMs || visiblePollMs))
        : Math.max(1000, Number(delayMs || visiblePollMs));
      pollTimer = setTimeout(() => {
        pollTimer = null;
        void poll();
      }, waitMs);
    }

    async function poll() {
      clearPollTimer();
      if (polling) return;
      if (document.visibilityState && document.visibilityState !== "visible") return;
      if (navigator && navigator.onLine === false) {
        schedulePoll(offlinePollMs);
        return;
      }
      polling = true;
      try {
        const sharedLast = readLastId();
        if (sharedLast > lastId) lastId = sharedLast;

        const res = await fetch(`/api/ingest/updates?after_id=${encodeURIComponent(lastId)}`, {
          headers: clientPresence.headers({ Accept: "application/json" }),
        });
        if (!res.ok) return;
        const json = await res.json();
        const items = Array.isArray(json.items) ? json.items : [];
        if (items.length === 0) return;

        let maxId = lastId;
        let totalNew = 0;
        let totalUpdated = 0;
        let anyToast = false;
        let anySound = false;
        const messages = [];

        items.forEach((it) => {
          if (typeof it.id === "number") maxId = Math.max(maxId, it.id);
          const notify = it.notify || {};
          const newCount = Number(notify.new || it.items_new || 0);
          const updatedCount = Number(notify.updated || it.items_updated || 0);
          const shouldToast = notify.toast !== undefined ? !!notify.toast : true;
          const shouldSound = !!notify.sound;
          if (newCount > 0) {
            totalNew += newCount;
            totalUpdated += updatedCount;
            if (shouldToast) anyToast = true;
            if (shouldSound) anySound = true;
            if (notify.message) messages.push(String(notify.message));
          }
        });

        lastId = maxId;
        try {
          localStorage.setItem("thoth_last_ingest_id", String(lastId));
        } catch (_e) {}

        if (totalNew > 0) {
          bumpInbox(totalNew);
          // Show the new items banner on feed page if viewing NEW items
          const isFeedPage = window.location.pathname === "/feed";
          const feedDocs = document.getElementById("feed-docs");
          const currentView = feedDocs ? (feedDocs.dataset.view || "NEW").toUpperCase() : "NEW";
          const currentSaved = feedDocs ? feedDocs.dataset.saved === "1" : false;
          
          // Only show banner if user is viewing a tab that would show new items
          const shouldShowBanner = isFeedPage && (currentView === "NEW" || currentView === "ALL") && !currentSaved;
          
          if (shouldShowBanner) {
            showNewItemsBanner(totalNew);
          }
          
          if (shouldNotifyNow()) {
            if (anyToast) {
              // Customize message based on current view
              let msg;
              if (messages.length === 1) {
                msg = messages[0];
              } else if (shouldShowBanner) {
                msg = `${totalNew} new items in your inbox`;
              } else {
                msg = `Ingest complete: ${totalNew} new, ${totalUpdated} updated`;
              }
              showToast(msg);
            }
            if (anySound) {
              playSound();
            }
          }
        }
      } catch (_e) {} finally {
        polling = false;
        schedulePoll(visiblePollMs);
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        schedulePoll(window.location.pathname === "/feed" ? 2500 : Math.max(30000, visiblePollMs));
        return;
      }
      clearPollTimer();
    });
    window.addEventListener("online", () => {
      if (document.visibilityState === "visible") {
        schedulePoll(window.location.pathname === "/feed" ? 2500 : Math.max(30000, visiblePollMs));
      }
    });
    window.addEventListener("offline", clearPollTimer);
    window.addEventListener("thoth:presencechange", (event) => {
      const state = event && event.detail ? event.detail.state : "";
      if (state === "active" || state === "recent") {
        schedulePoll(window.location.pathname === "/feed" ? 2500 : Math.max(30000, visiblePollMs));
      } else if (state === "idle") {
        schedulePoll(idlePollMs);
      }
    });

    const isFeedPage = window.location.pathname === "/feed";
    schedulePoll(
      initialCatchupDue
        ? (isFeedPage ? 2500 : 5000)
        : (isFeedPage ? Math.max(10000, visiblePollMs) : Math.max(90000, visiblePollMs * 3))
    );
  })();

  // Mobile nav + filters toggles
  (function () {
    const navToggle = document.getElementById("nav-toggle");
    const navOverlay = document.getElementById("nav-overlay");
    const nav = document.querySelector(".nav");
    if (navToggle) {
      navToggle.addEventListener("click", () => {
        document.body.classList.toggle("nav-open");
      });
    }
    if (navOverlay) {
      navOverlay.addEventListener("click", () => {
        document.body.classList.remove("nav-open");
      });
    }
    if (nav) {
      nav.addEventListener("click", (e) => {
        const target = e.target;
        if (target && target.closest("a")) {
          document.body.classList.remove("nav-open");
        }
      });
    }

    const burgerBtn = document.getElementById("burger-btn");
    const burgerOverlay = document.getElementById("burger-overlay");
    const burgerMenu = document.getElementById("burger-menu");
    if (burgerMenu) {
      burgerMenu.hidden = !document.body.classList.contains("burger-open");
    }
    if (burgerBtn) {
      burgerBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        document.body.classList.toggle("burger-open");
        if (burgerMenu) {
          burgerMenu.hidden = !document.body.classList.contains("burger-open");
        }
      });
    }
    if (burgerOverlay) {
      burgerOverlay.addEventListener("click", () => {
        document.body.classList.remove("burger-open");
        if (burgerMenu) burgerMenu.hidden = true;
      });
    }
    if (burgerMenu) {
      burgerMenu.addEventListener("click", (e) => {
        const target = e.target;
        if (target && target.closest("a")) {
          document.body.classList.remove("burger-open");
          burgerMenu.hidden = true;
        }
      });
    }
    document.addEventListener("click", (e) => {
      if (document.body.classList.contains("burger-open")) {
        const target = e.target;
        if (burgerMenu && burgerMenu.contains(target)) return;
        if (burgerBtn && burgerBtn.contains(target)) return;
        document.body.classList.remove("burger-open");
        if (burgerMenu) burgerMenu.hidden = true;
      }
    });

    const filtersToggle = document.getElementById("filters-toggle");
    const filtersClose = document.getElementById("filters-close");
    const filtersOverlay = document.getElementById("filters-overlay");
    if (filtersToggle) {
      filtersToggle.addEventListener("click", () => {
        document.body.classList.add("show-filters");
      });
    }
    if (filtersClose) {
      filtersClose.addEventListener("click", () => {
        document.body.classList.remove("show-filters");
      });
    }
    if (filtersOverlay) {
      filtersOverlay.addEventListener("click", () => {
        document.body.classList.remove("show-filters");
      });
    }
  })();

  // PWA install + service worker
  (function () {
    if (THOTH_CONFIG.desktopEnabled) return;
    const banner = document.getElementById("pwa-install-banner");
    const installBtn = document.getElementById("pwa-install-btn");
    const dismissBtn = document.getElementById("pwa-install-dismiss");
    const copyEl = banner ? banner.querySelector(".pwa-copy span") : null;
    let deferredPrompt = null;
    const isIOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent || "");

    function isStandalone() {
      return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone;
    }

    function showBanner() {
      if (!banner) return;
      if (!window.matchMedia("(max-width: 820px)").matches) return;
      if (isStandalone()) return;
      const dismissed = localStorage.getItem("thoth_pwa_dismissed");
      if (dismissed === "1") return;
      if (isIOS && copyEl) {
        copyEl.textContent = "Tap Share, then Add to Home Screen.";
        if (installBtn) installBtn.style.display = "none";
      }
      banner.style.display = "flex";
    }

    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      deferredPrompt = e;
      showBanner();
    });

    if (installBtn) {
      installBtn.addEventListener("click", async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        try {
          await deferredPrompt.userChoice;
        } catch (_e) {}
        deferredPrompt = null;
        if (banner) banner.style.display = "none";
      });
    }

    if (dismissBtn) {
      dismissBtn.addEventListener("click", () => {
        localStorage.setItem("thoth_pwa_dismissed", "1");
        if (banner) banner.style.display = "none";
      });
    }

    if (!deferredPrompt) {
      showBanner();
    }

    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/service-worker.js").catch(() => {});
      });
    }
  })();
})();
