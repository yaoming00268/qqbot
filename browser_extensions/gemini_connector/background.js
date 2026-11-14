const COOKIE_NAMES = ["__Secure-1PSID", "__Secure-1PSIDTS"];
const COOKIE_DOMAINS = new Set(["google.com", "gemini.google.com"]);

function validLink(link) {
  try {
    const url = new URL(link.endpoint);
    return url.origin === link.endpoint && url.protocol === "http:" && url.hostname === "127.0.0.1" &&
      Number(url.port) >= 1024 && Number(url.port) <= 65535 && /^[A-Za-z0-9_-]{43}$/.test(link.token);
  } catch (_) { return false; }
}

async function synchronize(link, handshake = false) {
  if (!validLink(link)) return {ok: false, message: "请从 QQBot 重新打开连接页。"};
  if (!/^[A-Za-z0-9_-]{16,80}$/.test(link.clientId))
    return {ok: false, message: "请从 QQBot 重新打开连接页。"};
  const cookies = [];
  for (const name of COOKIE_NAMES) {
    const cookie = await chrome.cookies.get({url: "https://gemini.google.com/", name, storeId: link.storeId});
    if (cookie && COOKIE_DOMAINS.has(cookie.domain.replace(/^\./, "")) && !cookie.partitionKey) {
      cookies.push({name, value: cookie.value, domain: cookie.domain, path: cookie.path,
        expirationDate: cookie.expirationDate || 0});
    }
  }
  if (!cookies.some(cookie => cookie.name === COOKIE_NAMES[0]))
    return {ok: false, message: "请先在此浏览器配置登录 gemini.google.com，然后刷新本页。"};
  const response = await fetch(link.endpoint + "/connect", {
    method: "POST", headers: {"Content-Type": "application/json"}, credentials: "omit",
    signal: AbortSignal.timeout(5000),
    body: JSON.stringify({token: link.token, cookies, client_id: link.clientId, handshake,
      browser: /Edg\//.test(navigator.userAgent) ? "edge" : "chrome"})
  });
  const result = await response.json();
  // Only a connection token is stored by the extension, never cookie values.
  return {ok: Boolean(response.ok && result.ok), message: result.message};
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type !== "qqbot.gemini.connect" || sender.id !== chrome.runtime.id ||
      !validLink(message)) return false;
  let url;
  try { url = new URL(sender.url); } catch (_) { return false; }
  if (url.origin !== message.endpoint || url.pathname !== "/gemini-browser-link" ||
      url.hash !== "#" + message.token || !sender.tab || sender.tab.incognito) return false;
  (async () => {
    const stores = await chrome.cookies.getAllCookieStores();
    const store = stores.find(item => item.tabIds.includes(sender.tab.id));
    if (!store) throw new Error("Cookie store unavailable");
    const saved = await chrome.storage.local.get("qqbotClientId");
    const clientId = saved.qqbotClientId || crypto.randomUUID();
    const link = {endpoint: message.endpoint, token: message.token, storeId: store.id, clientId};
    const result = await synchronize(link, true);
    if (result.ok) {
      await chrome.storage.local.set({qqbotGemini: link, qqbotClientId: clientId});
      await chrome.alarms.create("qqbot-gemini-sync", {periodInMinutes: 1});
    }
    respond(result);
  })().catch(() => respond({ok: false, message: "QQBot 连接暂不可用，请从媒体页重新打开连接页。"}));
  return true;
});

let refreshTask = null, refreshPending = false;
async function refresh() {
  refreshPending = true;
  if (!refreshTask) {
    refreshTask = (async () => {
      while (refreshPending) {
        refreshPending = false;
        try {
          const {qqbotGemini} = await chrome.storage.local.get("qqbotGemini");
          if (qqbotGemini) await synchronize(qqbotGemini);
        } catch (_) { /* QQBot may be closed; only login sync is retried. */ }
      }
    })().finally(() => { refreshTask = null; });
  }
  return refreshTask;
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "qqbot-gemini-sync") refresh();
});
chrome.cookies.onChanged.addListener(change => {
  if (!change.removed && COOKIE_NAMES.includes(change.cookie.name) &&
      COOKIE_DOMAINS.has(change.cookie.domain.replace(/^\./, ""))) refresh();
});
chrome.runtime.onStartup.addListener(async () => {
  const {qqbotGemini} = await chrome.storage.local.get("qqbotGemini");
  if (qqbotGemini) {
    await chrome.alarms.create("qqbot-gemini-sync", {periodInMinutes: 1});
    refresh();
  }
});
