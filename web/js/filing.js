/* 离线填报端视图 */
const Filing = (() => {
  let submissions = [];
  let curSubId = null;
  let curVerId = null;

  const ROLE_LABEL = { form: "表单", image: "图片", note: "修订说明" };

  function curSub() { return submissions.find((s) => s.id === curSubId) || null; }
  function curVer() {
    const s = curSub();
    return s ? s.versions.find((v) => v.id === curVerId) || null : null;
  }

  async function load() {
    const data = await Api.get("/api/submissions");
    submissions = data.submissions || [];
    if (curSubId && !submissions.find((s) => s.id === curSubId)) {
      curSubId = null; curVerId = null;
    }
    render();
  }

  // ------------------------------------------------------------ renders
  function render() {
    renderSubList();
    renderGraph();
    renderVersion();
    renderEarly();
    renderOutbox();
    document.getElementById("curSubHint").textContent = curSub() ? curSub().title : "未选择填报";
    document.getElementById("btnNewVersion").disabled = !curSub();
    document.getElementById("btnSubmitReview").disabled = !curVer() || curVer().status !== "complete";
  }

  function renderSubList() {
    const ul = document.getElementById("subList");
    if (!submissions.length) {
      ul.innerHTML = `<li class="placeholder">暂无填报，先新建一个（全部资料为虚构）</li>`;
      return;
    }
    ul.innerHTML = submissions.map((s) => `
      <li data-id="${s.id}" class="${s.id === curSubId ? "active" : ""}">
        <div><b>${esc(s.title)}</b> <span class="kv">#${s.id}</span></div>
        <div class="meta">${s.versions.length} 个版本 · 创建 ${fmt(s.created_at)}</div>
      </li>`).join("");
    ul.querySelectorAll("li[data-id]").forEach((li) => {
      li.onclick = () => {
        curSubId = li.dataset.id;
        const s = curSub();
        curVerId = s && s.versions.length ? s.versions[s.versions.length - 1].id : null;
        render();
      };
    });
  }

  function renderGraph() {
    const box = document.getElementById("versionGraph");
    const s = curSub();
    if (!s || !s.versions.length) { box.innerHTML = `<span class="hint">尚无版本</span>`; return; }
    box.innerHTML = s.versions.map((v, i) => `
      ${i ? '<span class="arrow">→</span>' : ""}
      <span class="node ${v.id === curVerId ? "active" : ""}" data-vid="${v.id}">
        v${v.seq} ${statusBadge(v.status)}
      </span>`).join("");
    box.querySelectorAll(".node").forEach((n) => {
      n.onclick = () => { curVerId = n.dataset.vid; render(); };
    });
  }

  function renderVersion() {
    const box = document.getElementById("versionDetail");
    const v = curVer();
    if (!v) {
      box.innerHTML = `<div class="placeholder">${curSub() ? "该填报还没有版本，点击「发新版本」" : "请先选择或新建填报"}</div>`;
      return;
    }
    const editable = v.status === "assembling";
    const filesByRole = Object.fromEntries(v.files.map((f) => [f.role, f]));
    const rows = ["form", "image", "note"].map((role) => {
      const f = filesByRole[role];
      if (!f) {
        return `<div class="file-row">
          <span class="role">${ROLE_LABEL[role]}</span>
          <div class="info"><span class="hint">未声明</span></div>
          ${editable ? fileInput(role) : ""}
        </div>`;
      }
      return `<div class="file-row ${f.revoked ? "revoked" : ""}">
        <span class="role">${ROLE_LABEL[role]}</span>
        <div class="info">
          <div><b>${esc(f.filename)}</b> ${f.revoked ? '<span class="badge fail">本版撤销</span>' : ""}
            <span class="kv">${f.size} 字节</span></div>
          <div class="hash">sha256:${esc(f.sha256 || "—")}</div>
        </div>
        <div class="pill-actions">
          ${editable ? fileInput(role, "补传/重传") : ""}
          ${editable ? `<button class="small danger" data-revoke="${role}">撤销该附件</button>` : ""}
          ${!f.revoked ? `<a class="small" href="/api/blobs/${f.sha256}" target="_blank">下载核对</a>` : ""}
        </div>
      </div>`;
    }).join("");

    box.innerHTML = `
      <div class="kv" style="margin-bottom:8px">
        版本 <b>v${v.seq}</b>（${v.id}） · 父版本 <b>${v.base_version_id ? v.base_version_id : "无（首版）"}</b>
      </div>
      <div class="check-box ${v.content_check}">
        <h3>内容核对 ${checkBadge(v.content_check)}</h3>
        <p>${esc(v.content_detail || "尚未核对")}</p>
      </div>
      <div class="check-box ${v.relation_check}">
        <h3>版本关系核对 ${checkBadge(v.relation_check)}</h3>
        <p>${esc(v.relation_detail || "尚未核对")}</p>
      </div>
      ${rows}
      <div class="kv" style="margin-top:6px">更新于 ${fmt(v.updated_at)}</div>`;

    box.querySelectorAll("input[data-role]").forEach((inp) => {
      inp.onchange = () => uploadForRole(inp.dataset.role, inp.files[0]);
    });
    box.querySelectorAll("button[data-revoke]").forEach((btn) => {
      btn.onclick = () => revoke(btn.dataset.revoke);
    });
  }

  function fileInput(role, label = "选择文件") {
    return `<label class="small" style="border:1px solid var(--line);border-radius:8px;padding:4px 9px;cursor:pointer">
      ${label}<input type="file" data-role="${role}" hidden></label>`;
  }

  function renderEarly() {
    const ul = document.getElementById("earlyBlobs");
    const items = Uploader.listEarly();
    if (!items.length) {
      ul.innerHTML = `<li class="placeholder">尚未上传附件</li>`;
      return;
    }
    ul.innerHTML = items.map((b) => `
      <li data-sha="${esc(b.sha256 || b.key)}">
        <div><b>${esc(b.filename)}</b>
          <span class="badge ${b.status === "ready" || b.status === "mounted" ? "pass" : b.status === "error" ? "fail" : "pending"}">
            ${b.status === "ready" ? "待挂载" : b.status === "mounted" ? "已挂载" : b.status === "error" ? "失败" : b.status}
          </span>
          ${b.role ? `<span class="kv">角色:${ROLE_LABEL[b.role]}</span>` : ""}
        </div>
        <div class="kv">${b.size} 字节 · ${esc(b.message || "")}</div>
        ${b.sha256 ? `<div class="hash kv">sha256:${esc(b.sha256.slice(0, 24))}…</div>` : ""}
        <div class="progress"><div style="width:${b.percent || 0}%"></div></div>
        <div class="pill-actions" style="margin-top:6px">
          ${b.sha256 && curVer() && curVer().status === "assembling"
            ? ["form", "image", "note"].map((r) =>
              `<button class="small" data-mount="${b.sha256}" data-role="${r}">挂到${ROLE_LABEL[r]}</button>`).join("") : ""}
          ${b.sha256 ? `<button class="small danger" data-discard="${b.sha256}">放弃（等服务端宽限期清理）</button>` : ""}
        </div>
      </li>`).join("");
    ul.querySelectorAll("button[data-mount]").forEach((btn) => {
      btn.onclick = async () => {
        const b = items.find((x) => x.sha256 === btn.dataset.mount);
        try {
          await Api.post(`/api/versions/${curVerId}/attach`, {
            role: btn.dataset.role, sha256: b.sha256, size: b.size, filename: b.filename,
          });
          b.role = btn.dataset.role; b.status = "mounted"; b.message = `已挂载到 ${ROLE_LABEL[btn.dataset.role]}`;
          toast("已挂载到当前版本");
          await load();
        } catch (e) { toast(e.message, "err"); }
      };
    });
    ul.querySelectorAll("button[data-discard]").forEach((btn) => {
      btn.onclick = () => { Uploader.removeEarly(btn.dataset.discard); renderEarly(); };
    });
  }

  function renderOutbox() {
    const ul = document.getElementById("outboxList");
    const items = Api.getOutbox();
    if (!items.length) {
      ul.innerHTML = `<li class="placeholder">发件箱为空</li>`;
      return;
    }
    ul.innerHTML = items.map((o) => `
      <li><b>${o.method}</b> ${esc(o.path)}
        <div class="meta hint">排队于 ${fmt(o.queuedAt)}${o.failed ? " · 失败:" + esc(o.lastError || "") : ""}</div>
      </li>`).join("");
  }

  // ------------------------------------------------------------ actions
  async function uploadForRole(role, file) {
    if (!file || !curVerId) return;
    const slow = document.getElementById("slowMode").checked;
    const slowMs = parseInt(document.getElementById("slowMs").value || "0", 10);
    try {
      // uploadAndRegister 会立即在页面登记条目；断网时内部 waitOnline 自动暂停，
      // 恢复后续传并挂载，无需调用方提前拦截。
      const wasOffline = Api.isOffline();
      await Uploader.uploadAndRegister(file, {
        role, versionId: curVerId, slowMs: slow ? slowMs : 0,
      });
      toast(wasOffline
        ? `${ROLE_LABEL[role]} 已在恢复网络后自动上传并挂载`
        : `${ROLE_LABEL[role]} 上传并挂载成功`);
      await load();
    } catch (e) {
      toast(e.message, "err");
    }
  }

  async function revoke(role) {
    if (!curVerId) return;
    if (!confirm(`确认在本版本撤销「${ROLE_LABEL[role]}」？\n（仅记录撤销关系，父版本附件仍保留，下游读父版指针）`)) return;
    try {
      await Api.post(`/api/versions/${curVerId}/revoke`, { role });
      toast(`已撤销 ${ROLE_LABEL[role]}`);
      await load();
    } catch (e) { toast(e.message, "err"); }
  }

  function bind() {
    document.getElementById("btnNewSub").onclick = async () => {
      const title = document.getElementById("newSubTitle").value.trim();
      if (!title) return toast("请填写填报标题", "err");
      try {
        const r = await Api.post("/api/submissions", { title });
        if (r.queued) { toast("断网：新建请求已进入发件箱，恢复后自动创建"); }
        else { curSubId = r.id; document.getElementById("newSubTitle").value = ""; }
        await safeLoad();
      } catch (e) { toast(e.message, "err"); }
    };
    document.getElementById("btnRefreshSubs").onclick = safeLoad;

    document.getElementById("btnNewVersion").onclick = async () => {
      const s = curSub();
      if (!s) return;
      const base = s.versions.length ? s.versions[s.versions.length - 1].id : null;
      try {
        const r = await Api.post(`/api/submissions/${curSubId}/versions`, { base_version_id: base });
        if (!r.queued) { curVerId = r.id; toast(`已创建 v${r.seq}`); }
        else toast("断网：新版本请求已排队");
        await safeLoad();
      } catch (e) { toast(e.message, "err"); }
    };

    document.getElementById("btnSubmitReview").onclick = async () => {
      try {
        await Api.post(`/api/versions/${curVerId}/review/submit`, {});
        toast("已送审");
        await safeLoad();
      } catch (e) { toast(e.message, "err"); }
    };

    document.getElementById("earlyFile").onchange = async (ev) => {
      const file = ev.target.files[0];
      if (!file) return;
      const slow = document.getElementById("slowMode").checked;
      const slowMs = parseInt(document.getElementById("slowMs").value || "0", 10);
      try {
        await Uploader.uploadAndRegister(file, { slowMs: slow ? slowMs : 0 });
        toast("附件上传完成，可在文件齐后补表单/挂载");
      } catch (e) { toast(e.message, "err"); }
      ev.target.value = "";
    };

    window.addEventListener("outbox-changed", renderOutbox);
    window.addEventListener("outbox-failed", (e) => toast(`发件箱重放失败: ${e.detail.lastError}`, "err"));
    Uploader.onChange(renderEarly);
  }

  async function safeLoad() {
    try { await load(); } catch (e) {
      if (!e.offline) toast(e.message, "err");
      else render();
    }
  }

  return { bind, load: safeLoad, render };
})();
