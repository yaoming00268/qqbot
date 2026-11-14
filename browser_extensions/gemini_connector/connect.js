/* The one-time token stays in the fragment and is never sent in a page request. */
(() => {
  if (location.hostname !== "127.0.0.1" || location.pathname !== "/gemini-browser-link" ||
      !document.querySelector('[data-qqbot-gemini-link="1"]')) return;
  const token = location.hash.slice(1);
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return;
  const status = document.getElementById("status");
  status.textContent = "正在连接当前浏览器的 Gemini 登录态…";
  chrome.runtime.sendMessage({type: "qqbot.gemini.connect", endpoint: location.origin, token}, response => {
    status.textContent = chrome.runtime.lastError ? "连接扩展未响应，请刷新本页。" :
      (response?.message || "未能连接，请先登录 Gemini 再刷新本页。");
  });
})();
