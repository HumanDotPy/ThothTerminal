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
  initialized: false,
  lastSeenIds: [],
  cacheSize: 50,
  maxLoops: 40,
  scrollDelayMs: 900,
  tweetDwellBaseMs: 120,
  tweetDwellPerCharMs: 2,
  tweetDwellMaxMs: 1200,
  readingWpm: 200,
  batchSize: 10,
  keepTabOnError: false,
  foregroundCapture: false,
  autoActivateMs: 1200,
  autoPoke: true,
  allowFocusSteal: false,
  captureWindowMode: "window",
  captureWindowMinimized: true,
  lastRunAt: "",
  lastRunTrigger: "",
  lastStatus: "",
  lastError: ""
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function getSettings() {
  const data = await chrome.storage.local.get(DEFAULTS);
  return data;
}

async function ensureDefaults() {
  const current = await chrome.storage.local.get(null);
  const update = {};
  for (const [key, val] of Object.entries(DEFAULTS)) {
    if (!(key in current)) update[key] = val;
  }
  if (Object.keys(update).length) {
    await chrome.storage.local.set(update);
  }
}

async function setStatus(status, error = "") {
  await chrome.storage.local.set({ lastStatus: status, lastError: error });
}

function originPatternForUrl(url) {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}/*`;
  } catch {
    return null;
  }
}

async function ensureHostPermission(baseUrl, request = false) {
  const pattern = originPatternForUrl(baseUrl);
  if (!pattern) return false;
  try {
    const has = await chrome.permissions.contains({ origins: [pattern] });
    if (has) return true;
    if (!request) return false;
    return await chrome.permissions.request({ origins: [pattern] });
  } catch {
    return false;
  }
}

function timeStrToMinutes(value) {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  const parts = raw.split(":");
  if (parts.length < 2) return null;
  const h = Number(parts[0]);
  const m = Number(parts[1]);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return null;
  if (h < 0 || h > 23 || m < 0 || m > 59) return null;
  return h * 60 + m;
}

function nowInTz(tz) {
  try {
    const now = new Date();
    const localized = new Date(now.toLocaleString("en-US", { timeZone: tz }));
    return localized;
  } catch {
    return new Date();
  }
}

function isWithinWindow(settings) {
  const startMin = timeStrToMinutes(settings.activeWindowStart);
  const endMin = timeStrToMinutes(settings.activeWindowEnd);
  const tz = settings.activeWindowTz;
  if (startMin === null || endMin === null || !tz) return true;
  const now = nowInTz(tz);
  const nowMin = now.getHours() * 60 + now.getMinutes();
  if (startMin < endMin) {
    return nowMin >= startMin && nowMin < endMin;
  }
  return nowMin >= startMin || nowMin < endMin;
}

function minutesUntilWindowStart(settings) {
  const startMin = timeStrToMinutes(settings.activeWindowStart);
  const endMin = timeStrToMinutes(settings.activeWindowEnd);
  const tz = settings.activeWindowTz;
  if (startMin === null || endMin === null || !tz) return 0;

  const now = nowInTz(tz);
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const inWindow = isWithinWindow(settings);
  if (inWindow) return 0;

  if (startMin < endMin) {
    if (nowMin < startMin) return startMin - nowMin;
    return 24 * 60 - nowMin + startMin;
  }
  if (nowMin < startMin) return startMin - nowMin;
  return 24 * 60 - nowMin + startMin;
}

function randBetween(min, max) {
  if (min === max) return min;
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

async function scheduleNextRun() {
  const settings = await getSettings();
  await chrome.alarms.clear("capture");
  if (!settings.enabled) return;

  const delayToWindow = minutesUntilWindowStart(settings);
  if (delayToWindow > 0) {
    chrome.alarms.create("capture", { delayInMinutes: Math.max(1, delayToWindow) });
    return;
  }

  const minM = Math.max(1, Number(settings.pollMinMinutes || 30));
  const maxM = Math.max(minM, Number(settings.pollMaxMinutes || minM));
  const delay = randBetween(minM, maxM);
  chrome.alarms.create("capture", { delayInMinutes: delay });
}

function waitForTabComplete(tabId, timeoutMs = 90000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("timeout"));
    }, timeoutMs);

    function listener(id, info) {
      if (id !== tabId) return;
      if (info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }

    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function pokeTab(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const el = document.body || document.documentElement;
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const x = Math.max(1, Math.min(rect.width - 2, rect.width / 2));
        const y = Math.max(1, Math.min(rect.height - 2, rect.height / 2));
        const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y };
        el.dispatchEvent(new MouseEvent("mousemove", opts));
        el.dispatchEvent(new MouseEvent("mousedown", opts));
        el.dispatchEvent(new MouseEvent("mouseup", opts));
        el.dispatchEvent(new MouseEvent("click", opts));
      }
    });
  } catch {
    // ignore
  }
}

async function pulseActivate(tabId, ms) {
  const waitMs = Math.max(0, Number(ms || 0));
  if (!waitMs) return;
  let prevTab = null;
  try {
    const activeTabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (activeTabs && activeTabs.length) prevTab = activeTabs[0];
  } catch {
    prevTab = null;
  }
  try {
    await chrome.tabs.update(tabId, { active: true });
  } catch {
    return;
  }
  await sleep(waitMs);
  if (prevTab && prevTab.id && prevTab.id !== tabId) {
    try {
      await chrome.tabs.update(prevTab.id, { active: true });
    } catch {
      // ignore
    }
  }
}

async function maybeActivateAndPoke(tabId, settings, foreground) {
  const shouldPoke = settings.autoPoke !== false;
  const activationMs = Math.max(0, Number(settings.autoActivateMs || 0));
  const mode = settings.captureWindowMode || "tab";
  const canActivate = mode === "tab" && !!settings.allowFocusSteal;
  if (activationMs > 0 && !foreground && canActivate) {
    await pulseActivate(tabId, activationMs);
  }
  if (shouldPoke) {
    await pokeTab(tabId);
  }
}

async function waitForTweetReadyInTab(tabId, settings, foreground, timeoutMs = 60000) {
  const start = Date.now();
  let lastPoke = 0;
  while (Date.now() - start < timeoutMs) {
    try {
      await ensureContentScript(tabId);
      if (Date.now() - lastPoke > 7000) {
        await maybeActivateAndPoke(tabId, settings, foreground);
        lastPoke = Date.now();
      }
      const resp = await sendMessage(tabId, { action: "ping" });
      if (resp && resp.ok) {
        const ready = await sendMessage(tabId, { action: "waitReady", timeoutMs: 8000 });
        if (ready && ready.ok) return true;
      }
    } catch {
      // retry until timeout
    }
    await sleep(1200);
  }
  return false;
}

function sendMessage(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(err);
        return;
      }
      resolve(resp);
    });
  });
}

async function ensureContentScript(tabId) {
  try {
    const pong = await sendMessage(tabId, { action: "ping" });
    if (pong && pong.ok) return;
  } catch {
    // fall through to inject
  }

  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"]
  });
}

async function openCaptureTarget(url, settings, foreground = false) {
  const mode = settings.captureWindowMode || "tab";
  const useWindow = (mode === "window" || mode === "stealth") && !foreground;
  if (useWindow) {
    const state = settings.captureWindowMinimized ? "minimized" : "normal";
    const isStealth = mode === "stealth";
    const win = await chrome.windows.create({
      url,
      focused: false,
      type: "popup",
      state,
      left: isStealth ? -10000 : undefined,
      top: isStealth ? -10000 : undefined,
      width: 900,
      height: 900
    });
    if (isStealth) {
      try {
        await chrome.windows.update(win.id, {
          focused: false,
          state,
          left: -10000,
          top: -10000,
          width: 900,
          height: 900
        });
      } catch {
        // ignore
      }
    }
    const tab = win.tabs && win.tabs.length ? win.tabs[0] : null;
    return { tabId: tab ? tab.id : null, windowId: win.id, ownsWindow: true };
  }
  const tab = await chrome.tabs.create({ url, active: !!foreground });
  return { tabId: tab.id, windowId: tab.windowId, ownsWindow: false };
}

async function closeTarget(target) {
  if (!target) return;
  if (target.ownsWindow && target.windowId) {
    try {
      await chrome.windows.remove(target.windowId);
    } catch {
      // ignore
    }
    return;
  }
  if (target.tabId) {
    try {
      await chrome.tabs.remove(target.tabId);
    } catch {
      // ignore
    }
  }
}

async function getLatestIds(settings, foreground = false) {
  const target = await openCaptureTarget(settings.listUrl, settings, foreground);
  let hadError = false;
  try {
    if (!target.tabId) throw new Error("capture_tab_missing");
    await waitForTabComplete(target.tabId);
    await sleep(1500);
    await maybeActivateAndPoke(target.tabId, settings, foreground);
    const ready = await waitForTweetReadyInTab(target.tabId, settings, foreground, 60000);
    if (!ready) {
      hadError = true;
      await setStatus("Capture failed", "timeout_waiting_for_tweets");
      return [];
    }
    await ensureContentScript(target.tabId);
    const resp = await sendMessage(target.tabId, {
      action: "getLatestIds",
      cacheSize: settings.cacheSize
    });
    if (!resp || !resp.ok) {
      if (resp && resp.error) {
        await setStatus("Capture failed", resp.error);
      }
      hadError = true;
      return [];
    }
    return resp.latestIds || [];
  } catch (err) {
    hadError = true;
    await setStatus("Capture failed", String(err));
    return [];
  } finally {
    if (target.tabId && !(settings.keepTabOnError && hadError)) await closeTarget(target);
  }
}

async function extractUntil(settings, opts, foreground = false) {
  const target = await openCaptureTarget(settings.listUrl, settings, foreground);
  let hadError = false;
  try {
    if (!target.tabId) throw new Error("capture_tab_missing");
    await waitForTabComplete(target.tabId);
    await sleep(1500);
    await maybeActivateAndPoke(target.tabId, settings, foreground);
    const ready = await waitForTweetReadyInTab(target.tabId, settings, foreground, 60000);
    if (!ready) {
      hadError = true;
      await setStatus("Capture failed", "timeout_waiting_for_tweets");
      return { items: [], latestIds: [] };
    }
    await ensureContentScript(target.tabId);
    const resp = await sendMessage(target.tabId, {
      action: "extractUntil",
      knownIds: opts.knownIds || [],
      stopAfterSeenStreak: opts.stopAfterSeenStreak || settings.seenStreakStop,
      seedLimit: opts.seedLimit || 0,
      cacheSize: settings.cacheSize,
      maxLoops: settings.maxLoops,
      scrollDelayMs: settings.scrollDelayMs,
      tweetDwellBaseMs: settings.tweetDwellBaseMs,
      tweetDwellPerCharMs: settings.tweetDwellPerCharMs,
      tweetDwellMaxMs: settings.tweetDwellMaxMs,
      tweetDwellWpm: settings.readingWpm
    });
    if (!resp || !resp.ok) {
      if (resp && resp.error) {
        await setStatus("Capture failed", resp.error);
      }
      hadError = true;
      return { items: [], latestIds: [] };
    }
    return resp.data || { items: [], latestIds: [] };
  } catch (err) {
    hadError = true;
    await setStatus("Capture failed", String(err));
    return { items: [], latestIds: [] };
  } finally {
    if (target.tabId && !(settings.keepTabOnError && hadError)) await closeTarget(target);
  }
}

function mergeThread(base, detail) {
  if (!detail || !detail.main) return { ...base, thread: [] };
  const main = detail.main;
  const thread = Array.isArray(detail.thread) ? detail.thread : [];
  const merged = { ...base, ...main, thread: thread.slice() };
  if (!merged.quoted_text && base.quoted_text) merged.quoted_text = base.quoted_text;
  if (!merged.quoted_url && base.quoted_url) merged.quoted_url = base.quoted_url;
  if (!merged.links || !merged.links.length) merged.links = base.links || [];
  if (!merged.media || !merged.media.length) merged.media = base.media || [];
  merged.thread = normalizeThread(merged.thread);
  return merged;
}

function tweetSortKey(item) {
  if (!item || typeof item !== "object") return null;
  if (item.created_at) {
    const dt = new Date(item.created_at);
    if (!Number.isNaN(dt.getTime())) return dt.getTime();
  }
  const raw = item.tweet_id || item.id || "";
  const num = Number(String(raw).replace(/\D/g, ""));
  if (Number.isFinite(num) && num > 0) return num;
  if (item.url && item.url.includes("/status/")) {
    const idx = item.url.indexOf("/status/");
    if (idx >= 0) {
      const tail = item.url.slice(idx + 8).split("?")[0];
      const n2 = Number(String(tail).replace(/\D/g, ""));
      if (Number.isFinite(n2) && n2 > 0) return n2;
    }
  }
  return null;
}

function normalizeThread(thread) {
  if (!Array.isArray(thread) || thread.length === 0) return [];
  const seen = new Set();
  const filtered = [];
  thread.forEach((item) => {
    if (!item || !item.url) return;
    const key = item.tweet_id || item.url;
    if (seen.has(key)) return;
    seen.add(key);
    filtered.push(item);
  });
  filtered.sort((a, b) => {
    const ka = tweetSortKey(a);
    const kb = tweetSortKey(b);
    if (ka == null && kb == null) return 0;
    if (ka == null) return -1;
    if (kb == null) return 1;
    return ka - kb;
  });
  return filtered;
}

async function extractThreadForTweet(url, settings, foreground = false) {
  const target = await openCaptureTarget(url, settings, foreground);
  try {
    if (!target.tabId) throw new Error("capture_tab_missing");
    await waitForTabComplete(target.tabId);
    await sleep(1200);
    await maybeActivateAndPoke(target.tabId, settings, foreground);
    await waitForTweetReadyInTab(target.tabId, settings, foreground, 60000);
    await ensureContentScript(target.tabId);
    const resp = await sendMessage(target.tabId, {
      action: "extractThread",
      maxThreadReplies: settings.maxThreadReplies
    });
    if (!resp || !resp.ok) return null;
    return resp.data || null;
  } finally {
    if (target.tabId) await closeTarget(target);
  }
}

async function sendToThoth(items, settings) {
  if (!items || !items.length) return { ok: true, ingested: 0 };
  const base = (settings.thothUrl || "").replace(/\/$/, "");
  const permitted = await ensureHostPermission(base);
  if (!permitted) {
    const pattern = originPatternForUrl(base) || base;
    throw new Error(`permission_required:${pattern}`);
  }
  const endpoint = base + "/api/capture/tweets";
  const body = JSON.stringify({ items });
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`Thoth responded ${resp.status} ${text}`.trim());
  }
  try {
    return await resp.json();
  } catch {
    return { ok: true, ingested: items.length };
  }
}

async function sendInBatches(items, settings, statusPrefix) {
  if (!items || !items.length) return 0;
  const size = Math.max(1, Number(settings.batchSize || items.length));
  let total = 0;
  for (let i = 0; i < items.length; i += size) {
    const batch = items.slice(i, i + size);
    const res = await sendToThoth(batch, settings);
    const ingested = res && typeof res.ingested === "number" ? res.ingested : batch.length;
    total += ingested;
    const done = Math.min(i + batch.length, items.length);
    await setStatus(`${statusPrefix} ${done}/${items.length}`, "");
    await sleep(200);
  }
  return total;
}

async function runCapture(opts = {}) {
  const foregroundOverride = !!opts.foreground;
  const backfillCount = Math.max(0, Number(opts.backfillCount || 0));
  const force = !!opts.force;
  const report = !!opts.report;
  const trigger = opts.trigger || (foregroundOverride ? "manual" : "scheduled");
  try {
    await chrome.storage.local.set({ lastRunAt: new Date().toISOString(), lastRunTrigger: trigger });
  } catch {
    // ignore
  }
  const settings = await getSettings();
  if (!settings.enabled && !force) {
    await setStatus("Capture disabled.", "");
    await scheduleNextRun();
    if (report) throw new Error("capture_disabled");
    return;
  }
  if (!isWithinWindow(settings) && !force) {
    await setStatus("Outside active window.", "");
    await scheduleNextRun();
    if (report) throw new Error("outside_active_window");
    return;
  }

  const permitted = await ensureHostPermission(settings.thothUrl);
  if (!permitted) {
    const pattern = originPatternForUrl(settings.thothUrl) || settings.thothUrl;
    await setStatus("Permission required", pattern);
    await scheduleNextRun();
    if (report) throw new Error(`permission_required:${pattern}`);
    return;
  }

  const useForeground = foregroundOverride || !!settings.foregroundCapture;
  let items = [];
  let latestIds = [];
  let previousTab = null;

  if (useForeground) {
    try {
      const activeTabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (activeTabs && activeTabs.length) previousTab = activeTabs[0];
    } catch {
      previousTab = null;
    }
  }

  try {
    if (backfillCount > 0) {
      await setStatus(`Backfill ${backfillCount}...`, "");
      const resp = await extractUntil(settings, { knownIds: [], seedLimit: backfillCount }, useForeground);
      items = resp.items || [];
      latestIds = resp.latestIds || [];
      if (items.length) {
        const ingested = await sendInBatches(items, settings, "Backfill");
        await setStatus(`Backfill captured ${ingested} tweet(s).`, "");
      } else {
        await setStatus("Backfill found no tweets.", "");
      }
      if (latestIds && latestIds.length) {
        await chrome.storage.local.set({ lastSeenIds: latestIds });
      }
      await scheduleNextRun();
      return;
    }

    if (!settings.initialized) {
      await setStatus("Initializing...", "");
      if (settings.initialBackfillCount && settings.initialBackfillCount > 0) {
        const resp = await extractUntil(settings, {
          knownIds: [],
          seedLimit: settings.initialBackfillCount
        }, useForeground);
        items = resp.items || [];
        latestIds = resp.latestIds || [];
        if (items.length) {
          await sendInBatches(items, settings, "Backfill");
        }
      } else {
        latestIds = await getLatestIds(settings, useForeground);
      }

      await chrome.storage.local.set({
        initialized: true,
        lastSeenIds: latestIds
      });

      await setStatus("Initialized.", "");
      await scheduleNextRun();
      return;
    }

    await setStatus("Capturing...", "");
    const resp = await extractUntil(settings, {
      knownIds: settings.lastSeenIds || [],
      stopAfterSeenStreak: settings.seenStreakStop
    }, useForeground);
    items = resp.items || [];
    latestIds = resp.latestIds || [];

    if (settings.includeThread) {
      const enriched = [];
      for (const item of items) {
        if (!item || !item.url) continue;
        try {
          const detail = await extractThreadForTweet(item.url, settings, useForeground);
          const merged = mergeThread(item, detail);
          enriched.push(merged);
        } catch {
          enriched.push(item);
        }
        await sleep(350 + Math.floor(Math.random() * 450));
      }
      items = enriched;
    }

    items = items.map((item) => {
      if (!item || !Array.isArray(item.thread)) return item;
      return { ...item, thread: normalizeThread(item.thread) };
    });

    if (items.length) {
      const ingested = await sendInBatches(items, settings, "Captured");
      await setStatus(`Captured ${ingested} tweet(s).`, "");
    } else {
      await setStatus("No new tweets.", "");
    }

    if (latestIds && latestIds.length) {
      await chrome.storage.local.set({ lastSeenIds: latestIds });
    }
  } catch (err) {
    await setStatus("Capture failed", String(err));
  }

  await scheduleNextRun();

  if (useForeground && previousTab && previousTab.id) {
    try {
      await chrome.tabs.update(previousTab.id, { active: true });
    } catch {
      // ignore
    }
  }
}

async function captureActiveTab(tab) {
  if (!tab || !tab.id) return;
  const settings = await getSettings();
  if (!settings.enabled) return;

  const url = tab.url || "";
  let items = [];
  if (url.includes("/status/")) {
    try {
      await ensureContentScript(tab.id);
      const resp = await sendMessage(tab.id, {
        action: "extractThread",
        maxThreadReplies: settings.maxThreadReplies
      });
      if (resp && resp.ok && resp.data && resp.data.main) {
        items = [mergeThread(resp.data.main, resp.data)];
      }
    } catch {
      await setStatus("Manual capture failed", "content script unavailable");
    }
  } else {
    try {
      await ensureContentScript(tab.id);
      const resp = await sendMessage(tab.id, {
        action: "extractList",
        scrolls: 1,
        maxItems: 5
      });
      if (resp && resp.ok) items = resp.items || [];
    } catch {
      await setStatus("Manual capture failed", "content script unavailable");
    }
  }

  if (items.length) {
    try {
      const ingested = await sendInBatches(items, settings, "Captured");
      await setStatus(`Captured ${ingested} tweet(s).`, "");
    } catch (err) {
      await setStatus("Manual capture failed", String(err));
    }
  } else {
    await setStatus("No tweets found.", "");
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await ensureDefaults();
  await scheduleNextRun();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureDefaults();
  await scheduleNextRun();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "capture") {
    await runCapture({ foreground: false, trigger: "alarm" });
  }
});

chrome.action.onClicked.addListener(async (tab) => {
  await captureActiveTab(tab);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || !msg.action) return;
  if (msg.action === "settingsUpdated") {
    scheduleNextRun().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "runNow") {
    runCapture({ foreground: true, force: true, report: true, trigger: "manual" }).then(() => sendResponse({ ok: true })).catch((err) => {
      sendResponse({ ok: false, error: String(err) });
    });
    return true;
  }
  if (msg.action === "runBackfill") {
    runCapture({ foreground: true, force: true, report: true, backfillCount: msg.count, trigger: "manual" }).then(() => sendResponse({ ok: true })).catch((err) => {
      sendResponse({ ok: false, error: String(err) });
    });
    return true;
  }
});
