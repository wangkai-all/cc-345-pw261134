/* API 封装：在线请求 + 模拟断网 + 离线发件箱（只排队元数据类操作）。
 * 分片上传是二进制流式操作，断网时由 uploader 暂停并在恢复后续传，不进 outbox。
 */
const Api = (() => {
  let offline = false;
  const OUTBOX_KEY = "offline-filing-outbox-v1";
  const listeners = [];

  function isOffline() { return offline || !navigator.onLine; }
  function setOffline(v) {
    offline = !!v;
    emit();
    if (!isOffline()) flushOutbox().catch(() => {});
  }
  function onChange(fn) { listeners.push(fn); }
  function emit() { listeners.forEach((fn) => fn(isOffline())); }
  window.addEventListener("online", () => { emit(); flushOutbox().catch(() => {}); });
  window.addEventListener("offline", emit);

  class OfflineError extends Error {
    constructor(msg) { super(msg); this.offline = true; }
  }

  async function request(method, path, body, isBinary = false) {
    if (isOffline()) throw new OfflineError("当前处于断网状态，请求已被本地拦截");
    const opts = { method, headers: {} };
    if (body !== undefined && body !== null) {
      if (isBinary) { opts.body = body; }
      else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    }
    const resp = await fetch(path, opts);
    const text = await resp.text();
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
    }
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  // ----------------------------------------------------------- outbox
  function getOutbox() {
    try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]"); }
    catch { return []; }
  }
  function saveOutbox(list) {
    localStorage.setItem(OUTBOX_KEY, JSON.stringify(list));
    window.dispatchEvent(new CustomEvent("outbox-changed"));
  }
  function enqueue(op) {
    const list = getOutbox();
    list.push({ id: crypto.randomUUID(), queuedAt: new Date().toISOString(), ...op });
    saveOutbox(list);
  }
  function removeOutbox(id) {
    saveOutbox(getOutbox().filter((x) => x.id !== id));
  }

  let flushing = false;
  async function flushOutbox() {
    if (flushing || isOffline()) return;
    flushing = true;
    try {
      // 按入队顺序逐条重放；失败则保留，等下一次在线事件
      let list = getOutbox();
      for (const item of [...list]) {
        if (isOffline()) break;
        try {
          await request(item.method, item.path, item.body);
          removeOutbox(item.id);
        } catch (e) {
          if (e.offline) break;
          // 业务错误（如 409）：标记失败并移出，避免毒消息卡死队列
          item.lastError = e.message;
          item.failed = true;
          const cur = getOutbox().map((x) => (x.id === item.id ? item : x));
          saveOutbox(cur);
          removeOutbox(item.id);
          window.dispatchEvent(new CustomEvent("outbox-failed", { detail: item }));
        }
      }
    } finally {
      flushing = false;
      window.dispatchEvent(new CustomEvent("outbox-changed"));
    }
  }

  // 离线安全的变更操作：断网自动入队，恢复后重放
  async function mutating(method, path, body) {
    if (isOffline()) {
      enqueue({ method, path, body });
      return { queued: true };
    }
    return request(method, path, body);
  }

  return {
    isOffline, setOffline, onChange, OfflineError, request, mutating,
    getOutbox, flushOutbox,
    get: (p) => request("GET", p),
    post: (p, b) => mutating("POST", p, b),
    put: (p, b) => mutating("PUT", p, b),
    binary: (method, p, bytes) => request(method, p, bytes, true),
  };
})();
