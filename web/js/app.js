/* 入口：标签切换、断网开关、公共工具、初始化 */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleString("zh-CN", { hour12: false });
}
function toast(msg, kind = "ok") {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 3200);
}

document.addEventListener("DOMContentLoaded", () => {
  // 标签页
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".tabpanel").forEach((x) => x.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
      if (tab.dataset.tab === "review") Review.activate();
      if (tab.dataset.tab === "downstream") Downstream.activate();
    };
  });

  // 断网开关
  const toggle = document.getElementById("offlineToggle");
  const state = document.getElementById("netState");
  function paintNet(off) {
    state.textContent = off ? "● 离线（请求本地拦截）" : "● 在线";
    state.className = `net ${off ? "offline" : "online"}`;
  }
  toggle.onchange = () => {
    Api.setOffline(toggle.checked);
    paintNet(Api.isOffline());
    if (!Api.isOffline()) {
      toast("网络恢复：发件箱重放 + 分片断点续传自动进行");
      Filing.load();
    } else {
      toast("已进入模拟断网：变更进入发件箱，上传自动暂停", "err");
    }
  };
  Api.onChange(paintNet);

  // 初始化三个视图
  Filing.bind();
  Review.bind();
  Downstream.bind();
  Filing.load();
});
