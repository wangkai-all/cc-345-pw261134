"""持久化层：SQLite 元数据 + 磁盘 blob/分片存储。

关键不变量：
  * 版本内容由客户端 manifest 显式声明（role/sha256/size），绝不按上传时间拼装；
  * 每个版本带 base_version_id，构成版本 DAG，关系核对独立于内容核对；
  * blob 以 sha256 内容寻址，天然去重；同版本重传同角色文件即覆盖该角色指针；
  * 所有改变“会话 / blob 引用 / 清理”的操作共用一把 RLock，
    清理与分片恢复（finalize）互斥，避免互相误删。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from typing import Any

REQUIRED_ROLES = ("form", "image", "note")
# 组装/审核状态机
V_ASSEMBLING = "assembling"
V_COMPLETE = "complete"          # 文件齐了，自动核对已跑（结论可能不通过）
V_UNDER_REVIEW = "under_review"
V_APPROVED = "approved"
V_REJECTED = "rejected"
V_SUPERSEDED = "superseded"      # 同提交有更新版本通过审核
# 核对结论
C_PENDING = "pending"
C_PASS = "pass"
C_FAIL = "fail"
# 上传会话
S_OPEN = "open"
S_FINALIZED = "finalized"
S_ABORTED = "aborted"
S_EXPIRED = "expired"

UPLOAD_CHUNK_SIZE = 256 * 1024


def now_ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class StorageError(Exception):
    """业务错误，携带 HTTP 风格状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Storage:
    def __init__(self, data_dir: str, grace_seconds: int = 1800):
        self.data_dir = os.path.abspath(data_dir)
        self.blobs_dir = os.path.join(self.data_dir, "blobs")
        self.tmp_dir = os.path.join(self.data_dir, "chunks")
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "filing.db")
        self.grace_seconds = grace_seconds
        self.lock = threading.RLock()
        import sqlite3
        # 单一持久连接：所有访问由 self.lock 串行化；autocommit 模式避免
        # “事务中再开连接写 audit_log” 这类 SQLite 写锁自死锁。
        self.conn = sqlite3.connect(self.db_path, timeout=30,
                                    check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS submissions(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions(
                id TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                base_version_id TEXT,
                status TEXT NOT NULL,
                content_check TEXT NOT NULL DEFAULT 'pending',
                relation_check TEXT NOT NULL DEFAULT 'pending',
                content_detail TEXT NOT NULL DEFAULT '',
                relation_detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(submission_id, seq)
            );
            CREATE TABLE IF NOT EXISTS version_files(
                version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                filename TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(version_id, role)
            );
            CREATE TABLE IF NOT EXISTS blobs(
                sha256 TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                ref_count INTEGER NOT NULL DEFAULT 0,
                finalized_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upload_sessions(
                id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT '',
                received_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                lease_expires_at REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            """
            )

    def audit(self, actor: str, action: str, target: str, detail: Any = "") -> None:
        with self.lock:
            c = self.conn
            c.execute(
                "INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                (now_ts(), actor, action, target,
                 detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)),
            )

    # ---------------------------------------------------------- submissions
    def create_submission(self, title: str) -> dict:
        title = (title or "").strip() or "未命名填报"
        sid = new_id("sub")
        with self.lock:
            c = self.conn
            c.execute(
                "INSERT INTO submissions(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (sid, title, now_ts(), now_ts()),
            )
        self.audit("filing-client", "create_submission", sid, {"title": title})
        return self.get_submission(sid)

    def list_submissions(self) -> list[dict]:
        with self.lock:
            c = self.conn
            rows = c.execute(
                "SELECT * FROM submissions ORDER BY created_at DESC"
            ).fetchall()
            return [self._hydrate_submission(c, r) for r in rows]

    def get_submission(self, sid: str) -> dict:
        with self.lock:
            c = self.conn
            row = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise StorageError(404, f"提交不存在: {sid}")
            return self._hydrate_submission(c, row)

    def _hydrate_submission(self, c, row) -> dict:
        versions = c.execute(
            "SELECT * FROM versions WHERE submission_id=? ORDER BY seq", (row["id"],)
        ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "versions": [self._hydrate_version(c, v) for v in versions],
        }

    # ------------------------------------------------------------- versions
    def create_version(self, sid: str, base_version_id: str | None) -> dict:
        with self.lock:
            c = self.conn
            sub = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            if not sub:
                raise StorageError(404, f"提交不存在: {sid}")
            if base_version_id:
                base = c.execute(
                    "SELECT * FROM versions WHERE id=? AND submission_id=?",
                    (base_version_id, sid),
                ).fetchone()
                if not base:
                    raise StorageError(400, "base_version_id 不属于该提交，版本关系不合法")
            nxt = c.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM versions WHERE submission_id=?", (sid,)
            ).fetchone()[0]
            vid = new_id("ver")
            c.execute(
                """INSERT INTO versions(id,submission_id,seq,base_version_id,status,
                   content_check,relation_check,created_at,updated_at)
                   VALUES(?,?,?,?,?, 'pending','pending', ?,?)""",
                (vid, sid, nxt, base_version_id, V_ASSEMBLING, now_ts(), now_ts()),
            )
            c.execute("UPDATE submissions SET updated_at=? WHERE id=?", (now_ts(), sid))
        self.audit("filing-client", "create_version", vid,
                   {"submission_id": sid, "base": base_version_id})
        return self.get_version(vid)

    def get_version(self, vid: str) -> dict:
        with self.lock:
            c = self.conn
            row = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not row:
                raise StorageError(404, f"版本不存在: {vid}")
            return self._hydrate_version(c, row)

    def _hydrate_version(self, c, row) -> dict:
        files = c.execute(
            "SELECT * FROM version_files WHERE version_id=? ORDER BY role", (row["id"],)
        ).fetchall()
        return {
            "id": row["id"],
            "submission_id": row["submission_id"],
            "seq": row["seq"],
            "base_version_id": row["base_version_id"],
            "status": row["status"],
            "content_check": row["content_check"],
            "relation_check": row["relation_check"],
            "content_detail": row["content_detail"],
            "relation_detail": row["relation_detail"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "files": [
                {
                    "role": f["role"],
                    "sha256": f["sha256"],
                    "size": f["size"],
                    "filename": f["filename"],
                    "revoked": bool(f["revoked"]),
                    "updated_at": f["updated_at"],
                }
                for f in files
            ],
        }

    # ------------------------------------------------------------ manifest
    def attach_manifest(self, vid: str, entries: list[dict]) -> dict:
        """声明/更新版本清单。允许 blob 稍后才上传（先传大附件后补表单亦可）。

        entries: [{role, sha256, size, filename, revoked?}]
        已撤销的角色可以只带 revoked=true（沿用父版本指针由关系核对解释）。
        """
        roles = set()
        for e in entries:
            role = e.get("role")
            if role not in REQUIRED_ROLES:
                raise StorageError(400, f"非法文件角色: {role!r}，允许 {REQUIRED_ROLES}")
            if role in roles:
                raise StorageError(400, f"清单中角色 {role} 重复")
            roles.add(role)
            if not e.get("revoked"):
                if not e.get("sha256") or not e.get("filename"):
                    raise StorageError(400, f"角色 {role} 缺少 sha256/filename")
                if int(e.get("size", -1)) < 0:
                    raise StorageError(400, f"角色 {role} size 非法")
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not ver:
                raise StorageError(404, f"版本不存在: {vid}")
            if ver["status"] != V_ASSEMBLING:
                raise StorageError(409, f"版本已 {ver['status']}，清单不可再改（请发新版本）")
            for e in entries:
                role = e["role"]
                revoked = 1 if e.get("revoked") else 0
                if revoked:
                    # 撤销：保留旧指针但置 revoked；若该角色从未声明则直接写墓碑
                    c.execute(
                        """INSERT INTO version_files(version_id,role,sha256,size,filename,revoked,updated_at)
                           VALUES(?,?,?,?,?,1,?)
                           ON CONFLICT(version_id,role) DO UPDATE SET revoked=1, updated_at=excluded.updated_at""",
                        (vid, role, e.get("sha256", ""), int(e.get("size", 0) or 0),
                         e.get("filename", ""), now_ts()),
                    )
                else:
                    c.execute(
                        """INSERT INTO version_files(version_id,role,sha256,size,filename,revoked,updated_at)
                           VALUES(?,?,?,?,?,0,?)
                           ON CONFLICT(version_id,role) DO UPDATE SET
                             sha256=excluded.sha256, size=excluded.size,
                             filename=excluded.filename, revoked=0, updated_at=excluded.updated_at""",
                        (vid, role, e["sha256"], int(e["size"]), e["filename"], now_ts()),
                    )
            c.execute("UPDATE versions SET updated_at=? WHERE id=?", (now_ts(), vid))
        self.audit("filing-client", "attach_manifest", vid, {"roles": sorted(roles)})
        return self._maybe_complete(vid)

    def attach_blob_to_version(self, vid: str, role: str, filename: str,
                               sha256: str, size: int) -> dict:
        """便捷路径：blob 已 finalize 后直接挂到某个版本的角色上（含同版本重传覆盖）。"""
        if role not in REQUIRED_ROLES:
            raise StorageError(400, f"非法文件角色: {role}")
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not ver:
                raise StorageError(404, f"版本不存在: {vid}")
            if ver["status"] != V_ASSEMBLING:
                raise StorageError(409, f"版本已 {ver['status']}，文件不可再改（请发新版本）")
            blob = c.execute("SELECT * FROM blobs WHERE sha256=?", (sha256,)).fetchone()
            if not blob:
                raise StorageError(404, "blob 尚未上传完成，请先完成分片上传")
            c.execute(
                """INSERT INTO version_files(version_id,role,sha256,size,filename,revoked,updated_at)
                   VALUES(?,?,?,?,?,0,?)
                   ON CONFLICT(version_id,role) DO UPDATE SET
                     sha256=excluded.sha256, size=excluded.size,
                     filename=excluded.filename, revoked=0, updated_at=excluded.updated_at""",
                (vid, role, sha256, size, filename, now_ts()),
            )
            c.execute("UPDATE versions SET updated_at=? WHERE id=?", (now_ts(), vid))
        self.audit("filing-client", "attach_blob", vid, {"role": role, "sha256": sha256[:12]})
        return self._maybe_complete(vid)

    def revoke_file(self, vid: str, role: str) -> dict:
        if role not in REQUIRED_ROLES:
            raise StorageError(400, f"非法文件角色: {role}")
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not ver:
                raise StorageError(404, f"版本不存在: {vid}")
            if ver["status"] != V_ASSEMBLING:
                raise StorageError(409, f"版本已 {ver['status']}，不可撤销（请发新版本）")
            c.execute(
                """INSERT INTO version_files(version_id,role,sha256,size,filename,revoked,updated_at)
                   VALUES(?,?, '',0,'',1,?)
                   ON CONFLICT(version_id,role) DO UPDATE SET revoked=1, updated_at=excluded.updated_at""",
                (vid, role, now_ts()),
            )
        self.audit("filing-client", "revoke_file", vid, {"role": role})
        return self._maybe_complete(vid)

    def _maybe_complete(self, vid: str) -> dict:
        """三个角色都有声明（含撤销墓碑）且未撤销角色的 blob 已就位 → 触发自动核对。"""
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if ver["status"] != V_ASSEMBLING:
                return self._hydrate_version(c, ver)
            files = c.execute(
                "SELECT * FROM version_files WHERE version_id=?", (vid,)
            ).fetchall()
            by_role = {f["role"]: f for f in files}
            if not all(r in by_role for r in REQUIRED_ROLES):
                return self._hydrate_version(c, ver)
            for f in files:
                if f["revoked"]:
                    continue
                blob = c.execute(
                    "SELECT 1 FROM blobs WHERE sha256=?", (f["sha256"],)
                ).fetchone()
                if not blob:
                    return self._hydrate_version(c, ver)  # 等迟到的附件
            return self._run_checks(c, vid)

    # ------------------------------------------------------------- checks
    def _run_checks(self, c, vid: str) -> dict:
        """内容核对与关系核对分别执行、分别记录结论。"""
        ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        files = c.execute(
            "SELECT * FROM version_files WHERE version_id=?", (vid,)
        ).fetchall()

        # ---- 内容核对：重算磁盘上每个未撤销 blob 的 sha256，与 manifest 比对
        content_problems: list[str] = []
        for f in files:
            if f["revoked"]:
                continue
            path = self._blob_path(f["sha256"])
            if not os.path.exists(path):
                content_problems.append(f"{f['role']}: 磁盘 blob 缺失")
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(UPLOAD_CHUNK_SIZE), b""):
                    h.update(chunk)
            actual = h.hexdigest()
            if actual != f["sha256"]:
                content_problems.append(
                    f"{f['role']}: sha256 不一致（manifest={f['sha256'][:12]} 实际={actual[:12]}）"
                )
            actual_size = os.path.getsize(path)
            if actual_size != f["size"]:
                content_problems.append(
                    f"{f['role']}: 大小不一致（manifest={f['size']} 实际={actual_size}）"
                )
        content_status = C_PASS if not content_problems else C_FAIL
        content_detail = "全部文件 sha256/大小核对通过" if not content_problems else "；".join(content_problems)

        # ---- 关系核对：版本 DAG、必备角色、撤销必须有父版本对应附件
        relation_problems: list[str] = []
        base_row = None
        if ver["base_version_id"]:
            base_row = c.execute(
                "SELECT * FROM versions WHERE id=?", (ver["base_version_id"],)
            ).fetchone()
            if not base_row:
                relation_problems.append("base_version_id 指向不存在的版本")
        active_roles = {f["role"] for f in files if not f["revoked"]}
        declared_roles = {f["role"] for f in files}
        # 角色有三种状态：有效附件 / 显式撤销墓碑 / 完全未声明。
        # 只有第三种才算“缺失”；撤销由后面的父版本链规则校验。
        missing = [r for r in REQUIRED_ROLES if r not in declared_roles]
        if missing:
            relation_problems.append(f"缺少必备角色且未说明撤销: {','.join(missing)}")
        for f in files:
            if not f["revoked"]:
                continue
            if base_row is None:
                relation_problems.append(f"角色 {f['role']} 在首版被撤销，无父版本附件可沿用")
            else:
                parent = c.execute(
                    "SELECT * FROM version_files WHERE version_id=? AND role=? AND revoked=0",
                    (ver["base_version_id"], f["role"]),
                ).fetchone()
                if not parent:
                    relation_problems.append(
                        f"角色 {f['role']} 撤销但父版本 {ver['base_version_id']} 无有效附件"
                    )
        # 环检测（防御）
        seen, cur = set(), ver["base_version_id"]
        while cur:
            if cur in seen:
                relation_problems.append("版本链存在环")
                break
            seen.add(cur)
            r = c.execute("SELECT base_version_id FROM versions WHERE id=?", (cur,)).fetchone()
            cur = r["base_version_id"] if r else None
        relation_status = C_PASS if not relation_problems else C_FAIL
        relation_detail = "版本链与角色关系核对通过" if not relation_problems else "；".join(relation_problems)

        new_status = V_COMPLETE
        c.execute(
            """UPDATE versions SET status=?, content_check=?, relation_check=?,
               content_detail=?, relation_detail=?, updated_at=? WHERE id=?""",
            (new_status, content_status, relation_status,
             content_detail, relation_detail, now_ts(), vid),
        )
        row = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        self.audit("system", "auto_check", vid,
                   {"content": content_status, "relation": relation_status})
        return self._hydrate_version(c, row)

    def recheck(self, vid: str) -> dict:
        """审核端可手动重跑双核对（例如怀疑磁盘被改动）。"""
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not ver:
                raise StorageError(404, f"版本不存在: {vid}")
            if ver["status"] == V_ASSEMBLING:
                raise StorageError(409, "版本仍在组装，文件未齐")
            return self._run_checks(c, vid)

    # ------------------------------------------------------------- review
    def submit_for_review(self, vid: str) -> dict:
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not ver:
                raise StorageError(404, f"版本不存在: {vid}")
            if ver["status"] == V_ASSEMBLING:
                raise StorageError(409, "版本仍在组装，文件未齐，无法送审")
            if ver["status"] in (V_UNDER_REVIEW, V_APPROVED):
                return self._hydrate_version(c, ver)
            if ver["status"] == V_SUPERSEDED:
                raise StorageError(409, "该版本已被更新版本取代")
            c.execute(
                "UPDATE versions SET status=?, updated_at=? WHERE id=?",
                (V_UNDER_REVIEW, now_ts(), vid),
            )
        self.audit("filing-client", "submit_review", vid)
        return self.get_version(vid)

    def review(self, vid: str, approve: bool, comment: str, actor: str = "reviewer") -> dict:
        with self.lock:
            c = self.conn
            ver = c.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
            if not ver:
                raise StorageError(404, f"版本不存在: {vid}")
            if ver["status"] != V_UNDER_REVIEW:
                raise StorageError(409, f"版本状态为 {ver['status']}，仅审核中版本可审核")
            if approve and (ver["content_check"] != C_PASS or ver["relation_check"] != C_PASS):
                raise StorageError(
                    409,
                    f"核对未全部通过（内容={ver['content_check']} 关系={ver['relation_check']}），禁止通过",
                )
            if approve:
                c.execute(
                    "UPDATE versions SET status=?, updated_at=? WHERE id=?",
                    (V_APPROVED, now_ts(), vid),
                )
                # 同提交旧 approved 版本转 superseded（下游只读最新通过版）
                c.execute(
                    """UPDATE versions SET status=?, updated_at=?
                       WHERE submission_id=? AND status=? AND id<>?""",
                    (V_SUPERSEDED, now_ts(), ver["submission_id"], V_APPROVED, vid),
                )
            else:
                c.execute(
                    "UPDATE versions SET status=?, updated_at=? WHERE id=?",
                    (V_REJECTED, now_ts(), vid),
                )
        self.audit(actor, "approve" if approve else "reject", vid,
                   {"comment": comment})
        return self.get_version(vid)

    # ----------------------------------------------------- upload sessions
    def _session_dir(self, session_id: str) -> str:
        return os.path.join(self.tmp_dir, session_id)

    def _chunk_path(self, session_id: str, index: int) -> str:
        return os.path.join(self._session_dir(session_id), f"{index:08d}.part")

    def _blob_path(self, sha256: str) -> str:
        return os.path.join(self.blobs_dir, sha256)

    def create_upload_session(self, sha256: str, size: int, filename: str,
                              content_type: str, lease_seconds: int) -> dict:
        if not sha256 or len(sha256) != 64:
            raise StorageError(400, "sha256 必须为 64 位十六进制")
        if size < 0:
            raise StorageError(400, "size 非法")
        with self.lock:
            c = self.conn
            # 幂等：同 sha 已有 open 会话则直接复用（断点续传/重试）
            existing = c.execute(
                "SELECT * FROM upload_sessions WHERE sha256=? AND status=?",
                (sha256, S_OPEN),
            ).fetchone()
            if existing:
                self._renew(c, existing["id"], lease_seconds)
                return self.get_upload_session(existing["id"])
            if c.execute("SELECT 1 FROM blobs WHERE sha256=?", (sha256,)).fetchone():
                # blob 已存在：返回一个虚拟的已完成会话，客户端无需再传
                return {"id": None, "already_exists": True, "sha256": sha256,
                        "received_bytes": size, "size": size, "status": S_FINALIZED,
                        "lease_expires_at": None, "chunks": []}
            sid = new_id("upl")
            os.makedirs(self._session_dir(sid), exist_ok=True)
            lease = time.time() + lease_seconds
            c.execute(
                """INSERT INTO upload_sessions(id,sha256,size,filename,content_type,
                   received_bytes,status,lease_expires_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,0,?, ?,?,?)""",
                (sid, sha256, size, filename, content_type, S_OPEN, lease, now_ts(), now_ts()),
            )
        self.audit("filing-client", "create_upload", sid,
                   {"sha256": sha256[:12], "size": size, "filename": filename})
        return self.get_upload_session(sid)

    def _renew(self, c, session_id: str, lease_seconds: int) -> None:
        c.execute(
            "UPDATE upload_sessions SET lease_expires_at=?, updated_at=? WHERE id=?",
            (time.time() + lease_seconds, now_ts(), session_id),
        )

    def get_upload_session(self, session_id: str) -> dict:
        with self.lock:
            c = self.conn
            row = c.execute(
                "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                raise StorageError(404, f"上传会话不存在: {session_id}")
            chunks = []
            d = self._session_dir(session_id)
            if os.path.isdir(d):
                chunks = sorted(
                    int(fn[:-5]) for fn in os.listdir(d) if fn.endswith(".part")
                )
            return {
                "id": row["id"],
                "sha256": row["sha256"],
                "size": row["size"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "received_bytes": row["received_bytes"],
                "status": row["status"],
                "lease_expires_at": row["lease_expires_at"],
                "chunks": chunks,
            }

    def put_chunk(self, session_id: str, index: int, data: bytes,
                  lease_seconds: int) -> dict:
        with self.lock:
            c = self.conn
            row = c.execute(
                "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                raise StorageError(404, f"上传会话不存在: {session_id}")
            if row["status"] != S_OPEN:
                raise StorageError(409, f"会话已 {row['status']}，不能再写分片")
            if time.time() > row["lease_expires_at"]:
                # 租约到期：标记过期并清理，让客户端重新建会话
                self._expire_session(c, row)
                raise StorageError(410, "上传会话租约已过期，请重新发起上传")
            os.makedirs(self._session_dir(session_id), exist_ok=True)
            path = self._chunk_path(session_id, index)
            with open(path, "wb") as fh:
                fh.write(data)
            total = sum(
                os.path.getsize(os.path.join(self._session_dir(session_id), fn))
                for fn in os.listdir(self._session_dir(session_id)) if fn.endswith(".part")
            )
            c.execute(
                "UPDATE upload_sessions SET received_bytes=?, updated_at=? WHERE id=?",
                (total, now_ts(), session_id),
            )
            self._renew(c, session_id, lease_seconds)  # 每次写入续期 → 恢复中的上传不会被清
            return self.get_upload_session(session_id)

    def finalize_upload(self, session_id: str) -> dict:
        with self.lock:
            c = self.conn
            row = c.execute(
                "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                raise StorageError(404, f"上传会话不存在: {session_id}")
            if row["status"] == S_FINALIZED:
                return self.get_upload_session(session_id)
            if row["status"] != S_OPEN:
                raise StorageError(409, f"会话已 {row['status']}，无法完成")
            d = self._session_dir(session_id)
            if not os.path.isdir(d):
                raise StorageError(409, "分片目录丢失")
            part_files = sorted(
                fn for fn in os.listdir(d) if fn.endswith(".part")
            )
            expected = (row["size"] + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
            if len(part_files) != expected:
                raise StorageError(
                    409, f"分片不完整：收到 {len(part_files)}，期望 {expected}"
                )
            # 顺序拼接并实时算 hash
            h = hashlib.sha256()
            total = 0
            tmp_blob = os.path.join(self.tmp_dir, f"{session_id}.assembling")
            with open(tmp_blob, "wb") as out:
                for i, fn in enumerate(part_files):
                    if fn != f"{i:08d}.part":
                        os.remove(tmp_blob)
                        raise StorageError(409, f"分片序号不连续/缺失：期望 {i:08d}.part")
                    with open(os.path.join(d, fn), "rb") as fh:
                        for chunk in iter(lambda: fh.read(UPLOAD_CHUNK_SIZE), b""):
                            out.write(chunk)
                            h.update(chunk)
                            total += len(chunk)
            digest = h.hexdigest()
            if digest != row["sha256"]:
                os.remove(tmp_blob)
                raise StorageError(
                    422, f"内容核对失败：实际 sha256={digest[:12]} 与声明 {row['sha256'][:12]} 不符"
                )
            if total != row["size"]:
                os.remove(tmp_blob)
                raise StorageError(422, f"大小核对失败：实际 {total} 声明 {row['size']}")
            # 内容寻址落盘（已存在则去重）
            final_path = self._blob_path(digest)
            if not os.path.exists(final_path):
                os.replace(tmp_blob, final_path)
            else:
                os.remove(tmp_blob)
            c.execute(
                """INSERT INTO blobs(sha256,size,ref_count,finalized_at,created_at)
                   VALUES(?,?,0,?,?)
                   ON CONFLICT(sha256) DO NOTHING""",
                (digest, total, now_ts(), now_ts()),
            )
            c.execute(
                "UPDATE upload_sessions SET status=?, updated_at=? WHERE id=?",
                (S_FINALIZED, now_ts(), session_id),
            )
            shutil.rmtree(d, ignore_errors=True)
            # blob 迟到补齐：回查所有以 manifest 声明了该 blob、仍在组装的版本，
            # 若三角色已齐则触发自动核对（支持“先传大附件再补表单”与任何到达顺序）。
            pending = [r["id"] for r in c.execute(
                """SELECT DISTINCT vf.version_id AS id FROM version_files vf
                   JOIN versions v ON v.id=vf.version_id
                   WHERE vf.sha256=? AND v.status=?""",
                (digest, V_ASSEMBLING)).fetchall()]
        for vid in pending:
            self._maybe_complete(vid)
        self.audit("system", "finalize_upload", session_id, {"sha256": digest[:12]})
        return self.get_upload_session(session_id)

    def abort_upload(self, session_id: str) -> dict:
        with self.lock:
            c = self.conn
            row = c.execute(
                "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                raise StorageError(404, f"上传会话不存在: {session_id}")
            if row["status"] == S_OPEN:
                c.execute(
                    "UPDATE upload_sessions SET status=?, updated_at=? WHERE id=?",
                    (S_ABORTED, now_ts(), session_id),
                )
                shutil.rmtree(self._session_dir(session_id), ignore_errors=True)
        self.audit("filing-client", "abort_upload", session_id)
        return self.get_upload_session(session_id)

    def _expire_session(self, c, row) -> None:
        c.execute(
            "UPDATE upload_sessions SET status=?, updated_at=? WHERE id=?",
            (S_EXPIRED, now_ts(), row["id"]),
        )
        shutil.rmtree(self._session_dir(row["id"]), ignore_errors=True)

    # ------------------------------------------------------------- cleanup
    def cleanup(self, now: float | None = None) -> dict:
        """清理过期会话与孤儿 blob。

        安全要点：
          * 只处理 lease 已过期的 open 会话 —— 正在恢复（持续写分片/续期）的会话
            lease 始终在未来，绝不会被删；
          * 孤儿 blob 宽限期内保留，支持“先传大附件再补表单”；
          * 与 finalize/put_chunk 共用同一把锁，物理删除不可能与恢复并发。
        """
        now = time.time() if now is None else now
        removed_sessions, removed_blobs, kept_blobs = [], [], []
        with self.lock:
            c = self.conn
            stale = c.execute(
                "SELECT * FROM upload_sessions WHERE status=? AND lease_expires_at<?",
                (S_OPEN, now),
            ).fetchall()
            for row in stale:
                self._expire_session(c, row)
                removed_sessions.append(row["id"])

            blobs = c.execute("SELECT * FROM blobs").fetchall()
            for b in blobs:
                referenced = c.execute(
                    """SELECT 1 FROM version_files vf
                       WHERE vf.sha256=? AND vf.revoked=0 LIMIT 1""",
                    (b["sha256"],),
                ).fetchone()
                if referenced:
                    continue
                # 孤儿判定：只有仍 open 的分片会话代表“正在恢复的上传”，需要保护；
                # finalized 会话的 blob 已独立落盘，不构成保留理由（由宽限期保护）。
                busy = c.execute(
                    "SELECT 1 FROM upload_sessions WHERE sha256=? AND status=? LIMIT 1",
                    (b["sha256"], S_OPEN),
                ).fetchone()
                if busy:
                    continue
                # 宽限期按 DB 中 finalize 时间计算（不按文件 mtime：内容寻址去重时
                # 同一 blob 可能沿用更早的物理文件，mtime 不代表本次登记时间）。
                finalized_ts = b["finalized_at"] or b["created_at"]
                try:
                    finalized_epoch = datetime.datetime.strptime(
                        finalized_ts, "%Y-%m-%dT%H:%M:%S.%fZ"
                    ).replace(tzinfo=datetime.timezone.utc).timestamp()
                except (TypeError, ValueError):
                    finalized_epoch = now
                age = now - finalized_epoch
                if age < self.grace_seconds:
                    kept_blobs.append(b["sha256"])
                    continue
                path = self._blob_path(b["sha256"])
                if os.path.exists(path):
                    os.remove(path)
                c.execute("DELETE FROM blobs WHERE sha256=?", (b["sha256"],))
                removed_blobs.append(b["sha256"])

            # 清掉已无分片目录的终态会话行（保留近期 finalized 以便审计查询，这里仅清 aborted/expired）
            c.execute(
                "DELETE FROM upload_sessions WHERE status IN (?,?)",
                (S_ABORTED, S_EXPIRED),
            )
        report = {
            "ts": now_ts(),
            "expired_sessions": removed_sessions,
            "removed_orphan_blobs": removed_blobs,
            "blobs_within_grace_kept": kept_blobs,
        }
        self.audit("system", "cleanup", "storage", report)
        return report

    # ------------------------------------------------------------- reads
    def stats(self) -> dict:
        with self.lock:
            c = self.conn
            blob_rows = c.execute("SELECT sha256,size FROM blobs").fetchall()
            orphan = 0
            disk = 0
            for b in blob_rows:
                disk += b["size"]
                ref = c.execute(
                    "SELECT 1 FROM version_files WHERE sha256=? AND revoked=0 LIMIT 1",
                    (b["sha256"],),
                ).fetchone()
                busy = c.execute(
                    "SELECT 1 FROM upload_sessions WHERE sha256=? AND status=? LIMIT 1",
                    (b["sha256"], S_OPEN),
                ).fetchone()
                if not ref and not busy:
                    orphan += 1
            return {
                "blobs": len(blob_rows),
                "orphan_blobs": orphan,
                "open_sessions": c.execute(
                    "SELECT COUNT(*) FROM upload_sessions WHERE status=?", (S_OPEN,)
                ).fetchone()[0],
                "expired_sessions": c.execute(
                    "SELECT COUNT(*) FROM upload_sessions WHERE status=?", (S_EXPIRED,)
                ).fetchone()[0],
                "disk_bytes": disk,
                "versions": c.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
                "submissions": c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0],
            }

    def blob_path_for_download(self, sha256: str) -> tuple[str, dict]:
        with self.lock:
            c = self.conn
            row = c.execute("SELECT * FROM blobs WHERE sha256=?", (sha256,)).fetchone()
            if not row:
                raise StorageError(404, "blob 不存在")
            path = self._blob_path(sha256)
            if not os.path.exists(path):
                raise StorageError(410, "blob 物理文件已被清理")
            return path, dict(row)

    def list_audit(self, limit: int = 100) -> list[dict]:
        with self.lock:
            c = self.conn
            rows = c.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def downstream_latest(self) -> list[dict]:
        """下游视图：每个提交仅暴露最新一条 approved 版本（superseded 不可见）。"""
        with self.lock:
            c = self.conn
            subs = c.execute("SELECT * FROM submissions ORDER BY created_at").fetchall()
            out = []
            for s in subs:
                ver = c.execute(
                    "SELECT * FROM versions WHERE submission_id=? AND status=? ORDER BY seq DESC LIMIT 1",
                    (s["id"], V_APPROVED),
                ).fetchone()
                if not ver:
                    continue
                out.append({
                    "submission_id": s["id"],
                    "title": s["title"],
                    "version": self._hydrate_version(c, ver),
                })
            return out
