/* 下游读取端：只暴露每个提交最新一条 approved 版本，并在浏览器端独立复核内容。 */
const Downstream = (() => {
  const ROLE_LABEL = { form: "表单", image: "图片", note: "修订说明" };

  async function sha256Hex(buf) {
    const digest = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function verifyOne(fileEl, sha, size) {
    const el = fileEl.querySelector(".verify");
    el.textContent = "正在下载并独立计算 SHA-256…";
    try {
      const resp = await fetch(`/api/blobs/${sha}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const buf = await resp.arrayBuffer();
      const actual = await sha256Hex(buf);
      if (actual === sha && buf.byteLength === size) {
        el.className = "verify verify-ok";
        el.textContent = `✓ 下游独立核对通过（${buf.byteLength} B，SHA-256 一致）`;
      } else {
        el.className = "verify verify-bad";
        el.textContent = `✗ 核对失败：实际=${actual.slice(0, 16)}…`;
      }
    } catch (e) {
      el.className = "verify verify-bad";
      el.textContent = "✗ " + e.message;
    }
  }

  async function load() {
    const box = document.getElementById("downstreamList");
    let items;
    try {
      items = (await Api.get("/api/downstream")).items;
    } catch (e) {
      box.innerHTML = `<div class="placeholder">${e.offline ? "当前断网，下游无法读取（已入库版本仍安全保存在服务端）" : esc(e.message)}</div>`;
      return;
    }
    if (!items.length) {
      box.innerHTML = `<div class="placeholder">还没有完整且审核通过的版本 —— 组装中/审核中/驳回/被取代的版本都不会出现在这里</div>`;
      return;
    }
    box.innerHTML = items.map((it) => {
      const v = it.version;
      return `
      <div class="down-item" data-vid="${v.id}">
        <h3>${esc(it.title)} <span class="badge approved">已入库 v${v.seq}</span></h3>
        <div class="kv">版本 ${v.id} · 父版本 ${v.base_version_id ? v.base_version_id : "无"} · 入库时间 ${fmt(v.updated_at)}</div>
        <div class="down-files">
          ${v.files.map((f) => f.revoked ? `
            <div class="down-file">
              <b>${ROLE_LABEL[f.role]}</b>
              <div class="hint">本版已撤销，沿用父版本附件</div>
            </div>` : `
            <div class="down-file">
              <b>${ROLE_LABEL[f.role]}</b>：${esc(f.filename)}
              <div class="kv">${f.size} B</div>
              <div class="hash kv">${esc(f.sha256.slice(0, 20))}…</div>
              <a href="/api/blobs/${f.sha256}" target="_blank">下载</a>
              <button class="small" data-verify="${f.sha256}" data-size="${f.size}">独立核对</button>
              <div class="verify hint"></div>
            </div>`).join("")}
        </div>
      </div>`;
    }).join("");
    box.querySelectorAll("button[data-verify]").forEach((btn) => {
      btn.onclick = () => verifyOne(btn.closest(".down-file"), btn.dataset.verify, parseInt(btn.dataset.size, 10));
    });
  }

  let timer = null;
  function bind() {
    timer = setInterval(() => { if (!Api.isOffline()) load(); }, 3000);
  }
  return { bind, activate: load };
})();
