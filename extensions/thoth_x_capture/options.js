const DEFAULTS = {
  thothUrl: "http://127.0.0.1:8750",
  listUrl: "https://x.com/i/lists/2000395136614724040",
  enabled: true,
  pollMinMinutes: 30,
  pollMaxMinutes: 60,
  activeWindowStart: "07:00",
  activeWindowEnd: "01:00",
  activeWindowTz: "America/New_York",
  includeThread: true,
  maxThreadReplies: 2,
  seenStreakStop: 2,
  initialBackfillCount: 100,
  batchSize: 10,
  tweetDwellBaseMs: 120,
  tweetDwellPerCharMs: 2,
  tweetDwellMaxMs: 1200,
  keepTabOnError: false,
  foregroundCapture: false,
  autoActivateMs: 1200,
  autoPoke: true,
  allowFocusSteal: false,
  captureWindowMode: "window",
  captureWindowMinimized: true,
  readingWpm: 200,
  lastRunAt: "",
  lastRunTrigger: "",
  lastStatus: "",
  lastError: ""
};

const $ = (id) => document.getElementById(id);

async function loadSettings() {
  const settings = await chrome.storage.local.get(DEFAULTS);
  $("enabled").checked = !!settings.enabled;
  $("thothUrl").value = settings.thothUrl || "";
  $("listUrl").value = settings.listUrl || "";
  $("pollMinMinutes").value = settings.pollMinMinutes || "";
  $("pollMaxMinutes").value = settings.pollMaxMinutes || "";
  $("activeWindowStart").value = settings.activeWindowStart || "";
  $("activeWindowEnd").value = settings.activeWindowEnd || "";
  $("activeWindowTz").value = settings.activeWindowTz || "";
  $("includeThread").checked = !!settings.includeThread;
  $("maxThreadReplies").value = settings.maxThreadReplies || "";
  $("seenStreakStop").value = settings.seenStreakStop || "";
  $("initialBackfillCount").value = settings.initialBackfillCount ?? "";
  $("batchSize").value = settings.batchSize ?? "";
  $("tweetDwellBaseMs").value = settings.tweetDwellBaseMs ?? "";
  $("tweetDwellPerCharMs").value = settings.tweetDwellPerCharMs ?? "";
  $("tweetDwellMaxMs").value = settings.tweetDwellMaxMs ?? "";
  $("keepTabOnError").checked = !!settings.keepTabOnError;
  $("foregroundCapture").checked = !!settings.foregroundCapture;
  $("autoActivateMs").value = settings.autoActivateMs ?? "";
  $("autoPoke").checked = settings.autoPoke !== false;
  $("allowFocusSteal").checked = !!settings.allowFocusSteal;
  $("captureWindowMode").value = settings.captureWindowMode || "tab";
  $("captureWindowMinimized").checked = settings.captureWindowMinimized !== false;
  $("readingWpm").value = settings.readingWpm ?? "";
  if ($("lastRunAt")) {
    $("lastRunAt").textContent = settings.lastRunAt
      ? new Date(settings.lastRunAt).toLocaleString("en-US")
      : "—";
  }
  if ($("lastRunTrigger")) {
    $("lastRunTrigger").textContent = settings.lastRunTrigger || "—";
  }

  const status = settings.lastStatus || "";
  const err = settings.lastError || "";
  if (status) {
    setStatus(status + (err ? ` (${err})` : ""));
  }
}

function parseIntField(id, fallback) {
  const raw = $(id).value.trim();
  if (!raw) return fallback;
  const val = Number(raw);
  return Number.isFinite(val) ? val : fallback;
}

function updateNextRun() {
  const el = $("nextRunAt");
  if (!el || !chrome.alarms || !chrome.alarms.get) return;
  chrome.alarms.get("capture", (alarm) => {
    if (chrome.runtime.lastError) {
      el.textContent = "—";
      return;
    }
    if (!alarm || !alarm.scheduledTime) {
      el.textContent = "—";
      return;
    }
    el.textContent = new Date(alarm.scheduledTime).toLocaleString("en-US");
  });
}

async function saveSettings() {
  const data = {
    enabled: $("enabled").checked,
    thothUrl: $("thothUrl").value.trim(),
    listUrl: $("listUrl").value.trim(),
    pollMinMinutes: parseIntField("pollMinMinutes", DEFAULTS.pollMinMinutes),
    pollMaxMinutes: parseIntField("pollMaxMinutes", DEFAULTS.pollMaxMinutes),
    activeWindowStart: $("activeWindowStart").value.trim(),
    activeWindowEnd: $("activeWindowEnd").value.trim(),
    activeWindowTz: $("activeWindowTz").value.trim(),
    includeThread: $("includeThread").checked,
    maxThreadReplies: parseIntField("maxThreadReplies", DEFAULTS.maxThreadReplies),
    seenStreakStop: parseIntField("seenStreakStop", DEFAULTS.seenStreakStop),
    initialBackfillCount: parseIntField("initialBackfillCount", DEFAULTS.initialBackfillCount),
    batchSize: parseIntField("batchSize", DEFAULTS.batchSize),
    tweetDwellBaseMs: parseIntField("tweetDwellBaseMs", DEFAULTS.tweetDwellBaseMs),
    tweetDwellPerCharMs: parseIntField("tweetDwellPerCharMs", DEFAULTS.tweetDwellPerCharMs),
    tweetDwellMaxMs: parseIntField("tweetDwellMaxMs", DEFAULTS.tweetDwellMaxMs),
    keepTabOnError: $("keepTabOnError").checked,
    foregroundCapture: $("foregroundCapture").checked,
    autoActivateMs: parseIntField("autoActivateMs", DEFAULTS.autoActivateMs),
    autoPoke: $("autoPoke").checked,
    allowFocusSteal: $("allowFocusSteal").checked,
    captureWindowMode: $("captureWindowMode").value || "tab",
    captureWindowMinimized: $("captureWindowMinimized").checked,
    readingWpm: parseIntField("readingWpm", DEFAULTS.readingWpm)
  };
  await chrome.storage.local.set(data);
  chrome.runtime.sendMessage({ action: "settingsUpdated" });
  return data;
}

function originPatternForUrl(url) {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}/*`;
  } catch {
    return null;
  }
}

async function requestPermissionForUrl(url) {
  const pattern = originPatternForUrl(url);
  if (!pattern) return false;
  try {
    const has = await chrome.permissions.contains({ origins: [pattern] });
    if (has) return true;
    return await chrome.permissions.request({ origins: [pattern] });
  } catch {
    return false;
  }
}

function setStatus(msg) {
  $("status").textContent = msg || "";
  if (msg) {
    setTimeout(() => { $("status").textContent = ""; }, 3000);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  try {
    await loadSettings();
  } catch (err) {
    setStatus(`Options failed to load: ${err && err.message ? err.message : err}`);
  }
  updateNextRun();

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    const status = changes.lastStatus ? changes.lastStatus.newValue : null;
    const err = changes.lastError ? changes.lastError.newValue : null;
    if (status) {
      setStatus(status + (err ? ` (${err})` : ""));
    }
    if (changes.lastRunAt || changes.lastRunTrigger) {
      loadSettings().catch(() => {});
    }
  });
  setInterval(updateNextRun, 5000);

  $("saveBtn").addEventListener("click", async () => {
    const data = await saveSettings();
    const ok = await requestPermissionForUrl(data.thothUrl);
    if (!ok) {
      const pattern = originPatternForUrl(data.thothUrl) || data.thothUrl;
      setStatus(`Permission not granted for ${pattern}`);
      return;
    }
    setStatus("Saved.");
  });

  $("grantBtn").addEventListener("click", async () => {
    const url = $("thothUrl").value.trim();
    const ok = await requestPermissionForUrl(url);
    const pattern = originPatternForUrl(url) || url;
    setStatus(ok ? "Permission granted." : `Permission not granted for ${pattern}`);
  });

  $("runNowBtn").addEventListener("click", () => {
    chrome.runtime.sendMessage({ action: "runNow" }, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) {
        setStatus(`Run failed: ${err.message || err}`);
        return;
      }
      if (!resp || !resp.ok) {
        setStatus(`Run failed: ${(resp && resp.error) || "unknown error"}`);
        return;
      }
      setStatus("Run started.");
    });
  });

  $("backfillBtn").addEventListener("click", () => {
    const count = parseIntField("initialBackfillCount", DEFAULTS.initialBackfillCount);
    chrome.runtime.sendMessage({ action: "runBackfill", count }, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) {
        setStatus(`Backfill failed: ${err.message || err}`);
        return;
      }
      if (!resp || !resp.ok) {
        setStatus(`Backfill failed: ${(resp && resp.error) || "unknown error"}`);
        return;
      }
      setStatus(`Backfill started (${count}).`);
    });
  });
});
