/* 分片上传器：
 *  - Web Crypto 本地计算 SHA-256，服务端 finalize 时重算复核；
 *  - 先查会话已收分片实现断点续传（刷新页面/中途断网均可恢复）；
 *  - 断网期间自动暂停等待，恢复后只传缺失分片；
 *  - 会话租约过期(410)时自动重建会话重传；
 *  - 支持“先传大附件”：完成后登记为待挂载 blob。
 */
const Uploader = (() => {
  const CHUNK = 256 * 1024;
  const earlyBlobs = new Map(); // sha -> 登记信息
  const changeListeners = [];

  function onChange(fn) { changeListeners.push(fn); }
  function emit() { changeListeners.forEach((fn) => fn()); }

  function delay(ms) { return new Promise((r) => setTimeout(r, ms)); }
  async function waitOnline() {
    if (!Api.isOffline()) return;
    await new Promise((resolve) => {
      const timer = setInterval(() => {
        if (!Api.isOffline()) { clearInterval(timer); resolve(); }
      }, 400);
    });
  }

  async function sha256Hex(buf) {
    const digest = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function registerEarly(entry) {
    earlyBlobs.set(entry.sha256, entry);
    emit();
  }
  function listEarly() { return [...earlyBlobs.values()]; }
  function removeEarly(sha) { earlyBlobs.delete(sha); emit(); }

  async function createSession(sha, size, filename, contentType) {
    return Api.request("POST", "/api/uploads", { sha256: sha, size, filename, content_type: contentType });
  }

  /**
   * 上传一个 File，带进度回调。
   * onProgress({phase, percent, message})
   * phase: hashing | waiting-offline | uploading | finalizing | done | error
   */
  async function uploadFile(file, { onProgress, slowMs = 0 } = {}) {
    onProgress?.({ phase: "hashing", percent: 0, message: "本地计算 SHA-256…" });
    const buf = await file.arrayBuffer();
    const sha = await sha256Hex(buf);
    const total = Math.max(1, Math.ceil(file.size / CHUNK));

    onProgress?.({ phase: "waiting-offline", percent: 0, message: "等待网络…" });
    await waitOnline();

    let session = await createSession(sha, file.size, file.name, file.type || "application/octet-stream");
    if (session.already_exists) {
      onProgress?.({ phase: "done", percent: 100, message: "服务端已存在相同 blob，秒传完成" });
      return { sha256: sha, size: file.size, filename: file.name, deduplicated: true };
    }

    let doneChunks = new Set((await Api.request("GET", `/api/uploads/${session.id}/status`)).chunks || []);

    for (let i = 0; i < total; i++) {
      if (doneChunks.has(i)) continue;
      if (Api.isOffline()) {
        onProgress?.({ phase: "waiting-offline", percent: Math.round((doneChunks.size / total) * 100),
          message: "断网暂停中，已传分片保留在服务端…" });
        await waitOnline();
      }
      const chunk = buf.slice(i * CHUNK, Math.min((i + 1) * CHUNK, buf.byteLength));
      try {
        const st = await Api.binary("PUT", `/api/uploads/${session.id}/chunks/${i}`, chunk);
        doneChunks = new Set(st.chunks || []);
      } catch (e) {
        if (/410|租约/.test(e.message)) {
          // 租约过期：重建会话从头来（分片目录已被服务端清理）
          session = await createSession(sha, file.size, file.name, file.type || "application/octet-stream");
          doneChunks = new Set();
          i = -1;
          continue;
        }
        throw e;
      }
      onProgress?.({
        phase: "uploading",
        percent: Math.round((doneChunks.size / total) * 100),
        message: `分片 ${Math.min(doneChunks.size, total)}/${total}`,
      });
      if (slowMs > 0) await delay(slowMs);
    }

    onProgress?.({ phase: "finalizing", percent: 99, message: "服务端拼接并复核内容…" });
    await waitOnline();
    await Api.request("POST", `/api/uploads/${session.id}/finalize`);
    onProgress?.({ phase: "done", percent: 100, message: "上传完成，内容核对通过" });
    return { sha256: sha, size: file.size, filename: file.name };
  }

  /** 完整流程：上传并（可选）挂到某版本某角色；未指定版本则进“先传附件”区。 */
  async function uploadAndRegister(file, opts = {}) {
    const entry = {
      sha256: null, size: file.size, filename: file.name,
      status: "uploading", percent: 0, message: "", role: opts.role || null,
      versionId: opts.versionId || null,
    };
    const key = crypto.randomUUID();
    entry.key = key;
    earlyBlobs.set(key, entry);
    emit();
    try {
      const result = await uploadFile(file, {
        slowMs: opts.slowMs || 0,
        onProgress: (p) => {
          entry.percent = p.percent; entry.message = p.message; entry.status = p.phase;
          emit();
        },
      });
      entry.sha256 = result.sha256;
      entry.status = "ready";
      entry.percent = 100;
      entry.message = result.deduplicated ? "秒传（内容去重）" : "待挂载";
      // 去重时 Map 里可能已有同 sha 旧条目，合并
      for (const [k, v] of earlyBlobs) {
        if (k !== key && v.sha256 === entry.sha256) { earlyBlobs.delete(k); break; }
      }
      earlyBlobs.set(entry.sha256, entry);
      earlyBlobs.delete(key);
      emit();
      if (opts.versionId && opts.role) {
        await Api.post(`/api/versions/${opts.versionId}/attach`, {
          role: opts.role, sha256: entry.sha256, size: entry.size, filename: entry.filename,
        });
        entry.status = "mounted"; entry.message = `已挂载到 ${opts.role}`;
        emit();
      }
      return entry;
    } catch (e) {
      entry.status = "error";
      entry.message = e.message;
      emit();
      throw e;
    }
  }

  return { uploadFile, uploadAndRegister, listEarly, removeEarly, onChange, registerEarly };
})();
