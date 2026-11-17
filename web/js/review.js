/* 接收服务 / 审核入库端视图 */
const Review = (() => {
  let selectedVerId = null;
  let filter = "under_review";

  function statusBadge(s) {
    const map = {
      assembling: "组装中", complete: "待送审", under_review: "审核中",
      approved: "已通过", rejected: "已驳回", superseded: "已被取代",
    };
    return `<span class="badge ${s}">${map[s] || s}</span>`;
  }
  function checkBadge(s) {
    const map = { pass: "通过", fail: "未通过", pending: "待核对" };
    return `<span class="badge ${s}">${map[s] || s}</span>`;
  }

  async function loadList() {
    const { submissions } = await Api.get("/api/submissions");
    const ul = document.getElementById("reviewList");
    const items = [];
    for (const s of submissions) {
      for (const v of s.versions) items.push({ s, v });
    }
    const shown = filter === "all" ? items : items.filter((x) => x.v.status === filter);
    if (!shown.length) {
      ul.innerHTML = `<li class="placeholder">没有匹配状态的版本</li>`;
      return;
    }
    ul.innerHTML = shown.map(({ s, v }) => `
      <li data-vid="${v.id}" class="${v.id === selectedVerId ? "active" : ""}">
        <div><b>${esc(s.title)}</b> · v${v.seq} ${statusBadge(v.status)}</div>
        <div class="meta">
          内容${checkBadge(v.content_check)} 关系${checkBadge(v.relation_check)}
          · ${fmt(v.updated_at)}
        </div>
      </li>`).join("");
    ul.querySelectorAll("li[data-vid]").forEach((li) => {
      li.onclick = () => { selectedVerId = li.dataset.vid; loadDetail(); loadList(); };
    });
  }

  async function loadDetail() {
    const box = document.getElementById("reviewDetail");
    if (!selectedVerId) {
      box.innerHTML = `<div class="placeholder">选择左侧版本查看双核对结果</div>`;
      return;
    }
    const v = await Api.get(`/api/versions/${selectedVerId}`);
    const editable = v.status === "under_review";
    box.innerHTML = `
      <div class="kv" style="margin-bottom:8px">
        提交 <b>${v.submission_id}</b> · 版本 <b>v${v.seq}</b>（${v.id}）<br>
        父版本 <b>${v.base_version_id || "无（首版）"}</b> · 当前状态 ${statusBadge(v.status)}
      </div>
      <div class="check-box ${v.content_check}">
        <h3>① 内容核对（重算磁盘 SHA-256 与 manifest 比对）${checkBadge(v.content_check)}</h3>
        <p>${esc(v.content_detail || "—")}</p>
      </div>
      <div class="check-box ${v.relation_check}">
        <h3>② 版本关系核对（DAG / 必备角色 / 撤销合法性）${checkBadge(v.relation_check)}</h3>
        <p>${esc(v.relation_detail || "—")}</p>
      </div>
      <h3 style="font-size:13px">版本文件清单（按 manifest 声明，非按上传时间拼装）</h3>
      ${v.files.map((f) => `
        <div class="file-row ${f.revoked ? "revoked" : ""}">
          <span class="role">${({ form: "表单", image: "图片", note: "说明" })[f.role]}</span>
          <div class="info">
            <div><b>${esc(f.filename)}</b> ${f.revoked ? '<span class="badge fail">本版撤销</span>' : ""}</div>
            <div class="hash">sha256:${esc(f.sha256 || "—")} · ${f.size} B</div>
          </div>
          ${!f.revoked ? `<a class="small" href="/api/blobs/${f.sha256}" target="_blank">查看原件</a>` : ""}
        </div>`).join("")}
      <div class="pill-actions" style="margin-top:12px">
        <button class="small" id="btnRecheck">重新执行双核对</button>
        ${editable ? `<button class="small primary" id="btnApprove">审核通过并入库</button>
          <button class="small danger" id="btnReject">驳回</button>` : ""}
        ${v.status === "complete" ? `<button class="small primary" id="btnForceSubmit">代为送审</button>` : ""}
      </div>
      <p class="hint" style="margin-top:8px">
        规则：仅「审核中」且内容、关系双通过的版本可通过；通过后旧通过版自动转为「已被取代」，下游只能读到新版。
      </p>`;
    document.getElementById("btnRecheck").onclick = async () => {
      try { await Api.post(`/api/versions/${v.id}/recheck`, {}); toast("已重新核对"); await loadDetail(); }
      catch (e) { toast(e.message, "err"); }
    };
    const force = document.getElementById("btnForceSubmit");
    if (force) force.onclick = async () => {
      await Api.post(`/api/versions/${v.id}/review/submit`, {});
      await loadDetail(); loadList();
    };
    const approve = document.getElementById("btnApprove");
    if (approve) approve.onclick = () => openReviewModal(v, true);
    const reject = document.getElementById("btnReject");
    if (reject) reject.onclick = () => openReviewModal(v, false);
  }

  function openReviewModal(v, approve) {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `
      <div class="modal">
        <h3>${approve ? "审核通过并入库" : "驳回该版本"} · v${v.seq}</h3>
        <textarea id="modalComment" placeholder="审核意见（虚构）">${approve ? "双核对通过，资料齐全，准予入库。" : "请补充修订说明后重新提交。"}</textarea>
        <div class="actions">
          <button id="modalCancel">取消</button>
          <button class="${approve ? "primary" : "danger"}" id="modalOk">${approve ? "确认通过" : "确认驳回"}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    backdrop.querySelector("#modalCancel").onclick = () => backdrop.remove();
    backdrop.querySelector("#modalOk").onclick = async () => {
      try {
        await Api.post(`/api/versions/${v.id}/review`, {
          approve, comment: backdrop.querySelector("#modalComment").value,
        });
        backdrop.remove();
        toast(approve ? "已通过入库，下游可见" : "已驳回");
        await loadDetail(); loadList(); loadStats(); loadAudit();
      } catch (e) { toast(e.message, "err"); }
    };
  }

  async function loadStats() {
    try {
      const data = await Api.get("/api/admin/stats");
      const box = document.getElementById("storageStats");
      box.innerHTML = `
        <div class="stat"><b>${data.blobs}</b><span>blob 总数</span></div>
        <div class="stat"><b>${data.orphan_blobs}</b><span>孤儿 blob（宽限内保留）</span></div>
        <div class="stat"><b>${data.open_sessions}</b><span>进行中上传会话</span></div>
        <div class="stat"><b>${data.expired_sessions}</b><span>过期会话</span></div>
        <div class="stat"><b>${(data.disk_bytes / 1024).toFixed(1)} KB</b><span>blob 占用</span></div>`;
    } catch (e) {
      if (!e.offline) toast(e.message, "err");
    }
  }

  async function loadAudit() {
    try {
      const data = await Api.get("/api/audit");
      const box = document.getElementById("auditLog");
      const entries = (data.entries || []).slice(0, 60).map((e) => `
        <div class="entry">${fmt(e.ts)} <b>${esc(e.actor)}</b> ${esc(e.action)}
          → ${esc(e.target)} ${e.detail ? "· " + esc(e.detail) : ""}</div>`).join("");
      const reaper = (data.reaper || []).map((r) => `
        <div class="entry" style="color:var(--amber)">🧹 ${fmt(r.ts)} 清理：过期会话 ${r.expired_sessions.length}
          个，删除孤儿 blob ${r.removed_orphan_blobs.length} 个</div>`).join("");
      box.innerHTML = reaper + entries || `<div class="placeholder">暂无日志</div>`;
    } catch (e) { /* 离线时静默 */ }
  }

  function bind() {
    document.getElementById("reviewFilter").onchange = (e) => { filter = e.target.value; loadList(); };
    document.getElementById("btnReviewRefresh").onclick = () => { loadList(); loadDetail(); loadStats(); loadAudit(); };
    document.getElementById("btnRefreshStats").onclick = loadStats;
    document.getElementById("btnCleanup").onclick = async () => {
      try {
        const r = await Api.post("/api/admin/cleanup", {});
        toast(`清理完成：过期会话 ${r.expired_sessions.length}，孤儿 blob ${r.removed_orphan_blobs.length}`);
        loadStats(); loadAudit();
      } catch (e) { toast(e.message, "err"); }
    };
    setInterval(() => {
      if (Api.isOffline()) return;
      loadList();
      if (selectedVerId) loadDetail();
      loadStats(); loadAudit();
    }, 3000);
  }

  return {
    bind,
    activate: async () => {
      try { await loadList(); await loadDetail(); await loadStats(); await loadAudit(); }
      catch (e) { if (!e.offline) toast(e.message, "err"); }
    },
  };
})();
