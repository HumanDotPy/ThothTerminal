const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function absUrl(href) {
  if (!href) return null;
  if (href.startsWith("http://") || href.startsWith("https://")) return href;
  if (href.startsWith("/")) return "https://x.com" + href;
  return href;
}

function isTweetReady(article) {
  if (!article) return false;
  const hasStatus = article.querySelector('a[href*="/status/"]');
  const hasText = article.querySelector('[data-testid="tweetText"]');
  const hasTime = article.querySelector("time");
  return !!hasStatus && (!!hasText || !!hasTime);
}

function detectPageIssue() {
  if (document.querySelector('input[name="text"], input[name="password"]')) {
    return "login_required";
  }
  const loginBtn = document.querySelector('[data-testid="loginButton"], a[href*="/login"]');
  if (loginBtn) return "login_required";
  const bodyText = document.body ? document.body.innerText || "" : "";
  if (/Something went wrong|Try again|rate limit|temporarily unavailable/i.test(bodyText)) {
    return "page_error";
  }
  return null;
}

async function waitForTweets(maxMs = 25000) {
  const start = Date.now();
  while (Date.now() - start < maxMs) {
    const issue = detectPageIssue();
    if (issue) return { ok: false, reason: issue };
    const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    if (articles.some(isTweetReady)) return { ok: true };
    await sleep(400);
  }
  return { ok: false, reason: "timeout_waiting_for_tweets" };
}

function extractLinks(root) {
  const out = [];
  const seen = new Set();
  const links = root.querySelectorAll("a[href]");
  links.forEach((a) => {
    const href = absUrl(a.getAttribute("href"));
    if (!href || !href.startsWith("http")) return;
    if (href.includes("x.com/") || href.includes("twitter.com/")) {
      if (href.includes("/status/")) return;
      return;
    }
    if (!seen.has(href)) {
      seen.add(href);
      out.push(href);
    }
  });
  return out;
}

function extractMedia(root) {
  const out = [];
  const seen = new Set();
  const imgs = root.querySelectorAll("img");
  imgs.forEach((img) => {
    const src = (img.getAttribute("src") || "").trim();
    if (!src) return;
    if (src.includes("pbs.twimg.com/media") || src.includes("twimg.com/media")) {
      if (!seen.has(src)) {
        seen.add(src);
        out.push(src);
      }
    }
  });
  const styled = root.querySelectorAll("[style]");
  styled.forEach((el) => {
    const style = String(el.getAttribute("style") || "");
    const idx = style.toLowerCase().indexOf("url(");
    if (idx === -1) return;
    const after = style.slice(idx + 4);
    const end = after.indexOf(")");
    if (end === -1) return;
    let src = after.slice(0, end).trim();
    if ((src.startsWith("'") && src.endsWith("'")) || (src.startsWith("\"") && src.endsWith("\""))) {
      src = src.slice(1, -1);
    }
    if (!src) return;
    if (src.includes("pbs.twimg.com/media") || src.includes("twimg.com/media")) {
      if (!seen.has(src)) {
        seen.add(src);
        out.push(src);
      }
    }
  });
  return out;
}

function extractHandle(root) {
  const links = root.querySelectorAll("a");
  for (const a of links) {
    const txt = (a.innerText || "").trim();
    if (txt.startsWith("@") && txt.length > 1) {
      return txt.replace(/^@/, "");
    }
  }
  return null;
}

function extractAuthor(root) {
  const el = root.querySelector('div[data-testid="User-Name"] span');
  if (!el) return null;
  const txt = (el.innerText || "").trim();
  return txt || null;
}

function extractReplyToHandles(root) {
  let text = "";
  const replyBlock = root.querySelector('[data-testid="reply"]');
  if (replyBlock) {
    text = (replyBlock.innerText || "").trim();
  } else {
    const candidates = Array.from(root.querySelectorAll("span, div"));
    const hit = candidates.find((el) => {
      const t = (el.innerText || "").trim();
      return t.startsWith("Replying to") || t.includes("Replying to");
    });
    if (hit) text = (hit.innerText || "").trim();
  }
  const handles = new Set();
  if (replyBlock) {
    const links = replyBlock.querySelectorAll("a");
    links.forEach((a) => {
      const txt = (a.innerText || "").trim();
      if (txt.startsWith("@")) handles.add(txt.slice(1));
    });
  }
  const matches = text.match(/@([A-Za-z0-9_]+)/g) || [];
  matches.forEach((m) => handles.add(m.slice(1)));
  return Array.from(handles);
}

function extractTexts(root) {
  const out = [];
  const els = root.querySelectorAll('[data-testid="tweetText"]');
  els.forEach((el) => {
    const txt = (el.innerText || "").trim();
    if (txt) out.push(txt);
  });
  return out;
}

function extractStatusUrl(root) {
  const links = root.querySelectorAll('a[href*="/status/"]');
  for (const a of links) {
    const href = a.getAttribute("href") || "";
    if (href.includes("/status/")) {
      return absUrl(href);
    }
  }
  return null;
}

function handleFromStatusUrl(url) {
  if (!url) return null;
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    if (parts.length >= 3 && parts[1] === "status") {
      const handle = parts[0];
      return handle || null;
    }
  } catch {
    return null;
  }
  return null;
}

function extractQuotedUrl(root) {
  const q = root.querySelector('div[aria-label="Quoted Tweet"]');
  if (!q) return null;
  const link = q.querySelector('a[href*="/status/"]');
  if (link) return absUrl(link.getAttribute("href"));
  return null;
}

function extractQuotedData(article) {
  const q = article.querySelector('div[aria-label="Quoted Tweet"]');
  if (!q) return null;
  const qRoot = q.querySelector('article[data-testid="tweet"]') || q;
  const url = extractStatusUrl(qRoot);
  const texts = extractTexts(qRoot);
  const text = texts[0] || "";
  const author = extractAuthor(qRoot);
  const handle = extractHandle(qRoot);
  const links = extractLinks(qRoot);
  const media = extractMedia(qRoot);
  return {
    url,
    text,
    author,
    handle,
    links,
    media,
  };
}

function findThreadExpanders() {
  const nodes = [];
  const byTestId = Array.from(document.querySelectorAll('[data-testid="showMoreReplies"], [data-testid="showMore"], [data-testid="showMoreButton"]'));
  nodes.push(...byTestId);
  const candidates = Array.from(document.querySelectorAll('button, a[role="button"], div[role="button"]'));
  candidates.forEach((el) => {
    const text = (el.innerText || "").trim().toLowerCase();
    if (!text) return;
    if (text.includes("reply") || text.includes("replies") || text.includes("thread")) {
      nodes.push(el);
    }
  });
  const uniq = [];
  const seen = new Set();
  nodes.forEach((el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    uniq.push(el);
  });
  return uniq;
}

async function expandThread(maxRounds = 4) {
  for (let round = 0; round < maxRounds; round++) {
    const expanders = findThreadExpanders();
    if (!expanders.length) return;
    let clicked = 0;
    expanders.forEach((el) => {
      try {
        el.click();
        clicked += 1;
      } catch (_e) {}
    });
    if (!clicked) return;
    await sleep(700);
  }
}

function parseTweet(article) {
  try {
    const url = extractStatusUrl(article);
    if (!url) return null;
    let tweetId = null;
    const idx = url.indexOf("/status/");
    if (idx >= 0) {
      tweetId = url.slice(idx + 8).split("?")[0];
    }
    const texts = extractTexts(article);
    const text = texts[0] || "";
    const quotedText = texts.length > 1 ? texts[1] : null;
    const quotedUrl = extractQuotedUrl(article);
    const quoted = extractQuotedData(article);
    const author = extractAuthor(article);
    let handle = extractHandle(article);
    if (!handle) {
      handle = handleFromStatusUrl(url);
    }
    const replyTo = extractReplyToHandles(article);
    const timeEl = article.querySelector("time");
    const createdAt = timeEl ? timeEl.getAttribute("datetime") : null;
    const links = extractLinks(article);
    const media = extractMedia(article);
    const isSelfReply =
      replyTo.length > 0 && handle && replyTo.every((h) => h === handle);
    return {
      url,
      tweet_id: tweetId,
      text,
      quoted_text: quoted && quoted.text ? quoted.text : quotedText,
      quoted_url: quotedUrl,
      quoted,
      author,
      handle,
      reply_to: replyTo,
      is_self_reply: isSelfReply,
      created_at: createdAt,
      links,
      media,
    };
  } catch (_err) {
    return null;
  }
}

function topIds(limit) {
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const out = [];
  for (const article of articles) {
    const data = parseTweet(article);
    if (!data || !data.url) continue;
    out.push(data.tweet_id || data.url);
    if (out.length >= limit) break;
  }
  return out;
}

function countWords(text) {
  if (!text) return 0;
  const matches = String(text).trim().match(/[A-Za-z0-9_]+/g);
  return matches ? matches.length : 0;
}

function perTweetDelayMs(text, baseMs, perCharMs, maxMs, wpm) {
  const len = (text || "").length;
  const words = countWords(text);
  let readMs = 0;
  const speed = Number(wpm || 0);
  if (speed > 0 && words > 0) {
    readMs = Math.floor((words * 60000) / speed);
  }
  const charMs = Math.max(0, Math.floor(len * (perCharMs || 0)));
  const dwell = Math.max(0, baseMs || 0, readMs, charMs);
  return Math.min(maxMs || dwell, dwell);
}

async function extractUntil(opts) {
  const ready = await waitForTweets();
  if (!ready.ok) throw new Error(ready.reason || "no_tweets");
  const knownIds = Array.isArray(opts.knownIds) ? opts.knownIds.map(String) : [];
  const known = new Set(knownIds);
  const stopAfter = Math.max(1, Number(opts.stopAfterSeenStreak || 2));
  const seedLimit = Math.max(0, Number(opts.seedLimit || 0));
  const cacheSize = Math.max(5, Number(opts.cacheSize || 50));
  const maxLoops = Math.max(1, Number(opts.maxLoops || 40));
  const scrollDelay = Math.max(300, Number(opts.scrollDelayMs || 900));
  const baseDelay = Math.max(0, Number(opts.tweetDwellBaseMs || 120));
  const perCharMs = Math.max(0, Number(opts.tweetDwellPerCharMs || 2));
  const maxDelay = Math.max(0, Number(opts.tweetDwellMaxMs || 1200));
  const wpm = Number(opts.tweetDwellWpm || 0);
  const maxNoProgress = 3;

  const items = [];
  const processed = new Set();
  const lastByHandle = new Map();
  const pendingReplies = new Map();
  let seenStreak = 0;
  let noProgress = 0;

  for (let loop = 0; loop < maxLoops; loop++) {
    const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    let newFound = 0;
    for (const article of articles) {
      const data = parseTweet(article);
      if (!data || !data.url) continue;
      if (processed.has(data.url)) continue;
      processed.add(data.url);
      newFound += 1;

      if (data.reply_to && data.reply_to.length > 0 && !data.is_self_reply) {
        continue;
      }

      if (data.is_self_reply && data.handle) {
        const parent = lastByHandle.get(data.handle);
        if (parent) {
          if (!Array.isArray(parent.thread)) parent.thread = [];
          parent.thread.push(data);
          const delay = perTweetDelayMs(data.text, baseDelay, perCharMs, maxDelay, wpm);
          if (delay) await sleep(delay);
          continue;
        }
        const queue = pendingReplies.get(data.handle) || [];
        queue.push(data);
        pendingReplies.set(data.handle, queue);
        const delay = perTweetDelayMs(data.text, baseDelay, perCharMs, maxDelay, wpm);
        if (delay) await sleep(delay);
        continue;
      }

      if (seedLimit > 0) {
        items.push(data);
        if (data.handle) {
          lastByHandle.set(data.handle, data);
          const pending = pendingReplies.get(data.handle);
          if (pending && pending.length) {
            if (!Array.isArray(data.thread)) data.thread = [];
            data.thread.push(...pending);
            pendingReplies.delete(data.handle);
          }
        }
        const delay = perTweetDelayMs(data.text, baseDelay, perCharMs, maxDelay);
        if (delay) await sleep(delay);
        if (items.length >= seedLimit) {
          return { items, latestIds: topIds(cacheSize), stopped: true, reason: "seed_limit" };
        }
        continue;
      }

      const key = data.tweet_id || data.url;
      const isKnown = known.has(key) || known.has(data.url);
      if (isKnown) {
        seenStreak += 1;
      } else {
        seenStreak = 0;
        items.push(data);
        if (data.handle) {
          lastByHandle.set(data.handle, data);
          const pending = pendingReplies.get(data.handle);
          if (pending && pending.length) {
            if (!Array.isArray(data.thread)) data.thread = [];
            data.thread.push(...pending);
            pendingReplies.delete(data.handle);
          }
        }
      }

      const delay = perTweetDelayMs(data.text, baseDelay, perCharMs, maxDelay, wpm);
      if (delay) await sleep(delay);

      if (seenStreak >= stopAfter) {
        return { items, latestIds: topIds(cacheSize), stopped: true, reason: "seen_streak" };
      }
    }

    if (newFound === 0) {
      noProgress += 1;
    } else {
      noProgress = 0;
    }
    if (noProgress >= maxNoProgress) break;

    window.scrollBy(0, 2400);
    await sleep(scrollDelay);
  }

  return { items, latestIds: topIds(cacheSize), stopped: false, reason: "exhausted" };
}

async function extractList(opts) {
  const ready = await waitForTweets();
  if (!ready.ok) throw new Error(ready.reason || "no_tweets");
  const scrolls = Math.max(1, Number(opts.scrolls || 4));
  const maxItems = Math.max(1, Number(opts.maxItems || 20));

  for (let i = 0; i < scrolls; i++) {
    window.scrollBy(0, 2400);
    await sleep(900);
  }

  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const out = [];
  const seen = new Set();
  const lastByHandle = new Map();
  const pendingReplies = new Map();
  for (const article of articles) {
    const data = parseTweet(article);
    if (!data || !data.url || seen.has(data.url)) continue;
    seen.add(data.url);
    if (data.reply_to && data.reply_to.length > 0 && !data.is_self_reply) {
      continue;
    }
    if (data.is_self_reply && data.handle) {
      const parent = lastByHandle.get(data.handle);
      if (parent) {
        if (!Array.isArray(parent.thread)) parent.thread = [];
        parent.thread.push(data);
        continue;
      }
      const queue = pendingReplies.get(data.handle) || [];
      queue.push(data);
      pendingReplies.set(data.handle, queue);
      continue;
    }
    out.push(data);
    if (data.handle) {
      lastByHandle.set(data.handle, data);
      const pending = pendingReplies.get(data.handle);
      if (pending && pending.length) {
        if (!Array.isArray(data.thread)) data.thread = [];
        data.thread.push(...pending);
        pendingReplies.delete(data.handle);
      }
    }
    if (out.length >= maxItems) break;
  }
  return out;
}

async function extractThread(opts) {
  const ready = await waitForTweets();
  if (!ready.ok) throw new Error(ready.reason || "no_tweets");
  const maxReplies = Math.max(0, Number(opts.maxThreadReplies || 6));
  await expandThread(4);
  for (let i = 0; i < 2; i++) {
    window.scrollBy(0, 1600);
    await sleep(700);
    await expandThread(2);
  }
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  if (!articles.length) return { main: null, thread: [] };

  const main = parseTweet(articles[0]);
  if (!main) return { main: null, thread: [] };

  const thread = [];
  for (const article of articles) {
    const data = parseTweet(article);
    if (!data || data.url === main.url) continue;
    if (main.handle && data.handle && data.handle !== main.handle) continue;
    thread.push(data);
    if (maxReplies && thread.length >= maxReplies) break;
  }
  return { main, thread };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || !msg.action) return;
  if (msg.action === "ping") {
    sendResponse({ ok: true, version: "0.1.9" });
    return;
  }
  if (msg.action === "waitReady") {
    const timeoutMs = Math.max(5000, Number(msg.timeoutMs || 15000));
    waitForTweets(timeoutMs)
      .then((res) => sendResponse({ ok: !!res.ok, reason: res.reason || "" }))
      .catch((err) => sendResponse({ ok: false, reason: String(err) }));
    return true;
  }
  if (msg.action === "extractList") {
    extractList(msg).then((items) => sendResponse({ ok: true, items })).catch((err) => {
      sendResponse({ ok: false, error: String(err) });
    });
    return true;
  }
  if (msg.action === "extractThread") {
    extractThread(msg).then((data) => sendResponse({ ok: true, data })).catch((err) => {
      sendResponse({ ok: false, error: String(err) });
    });
    return true;
  }
  if (msg.action === "extractUntil") {
    extractUntil(msg).then((data) => sendResponse({ ok: true, data })).catch((err) => {
      sendResponse({ ok: false, error: String(err) });
    });
    return true;
  }
  if (msg.action === "getLatestIds") {
    try {
      const limit = Math.max(5, Number(msg.cacheSize || 50));
      const latestIds = topIds(limit);
      sendResponse({ ok: true, latestIds });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
    return true;
  }
});
