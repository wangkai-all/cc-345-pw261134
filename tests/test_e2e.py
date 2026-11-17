"""端到端测试：真实启动 HTTP 服务子进程，覆盖核心业务不变量。

运行：python3 -m unittest tests.test_e2e -v
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 256 * 1024


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, body=None, raw: bytes | None = None):
        url = self.base + path
        data = None
        headers = {}
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except json.JSONDecodeError:
                return e.code, {"error": payload.decode(errors="replace")}

    def get(self, p): return self.call("GET", p)
    def status(self, p):
        req = urllib.request.Request(self.base + p, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
    def post(self, p, b=None): return self.call("POST", p, body=b or {})
    def put_raw(self, p, data: bytes): return self.call("PUT", p, raw=data)


def make_file(size: int, seed: str) -> tuple[bytes, str]:
    # 生成可重复的伪随机内容
    h = hashlib.sha256(seed.encode()).digest()
    buf = (h * (size // len(h) + 1))[:size]
    return buf, hashlib.sha256(buf).hexdigest()


class E2ETest(unittest.TestCase):
    proc = None
    tmp = None
    api: Api
    port: int

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="filing-e2e-")
        cls.port = free_port()
        env = dict(os.environ,
                   DATA_DIR=cls.tmp,
                   HOST="127.0.0.1", PORT=str(cls.port),
                   LEASE_SECONDS="2", GRACE_SECONDS="2", REAPER_INTERVAL="1")
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "server.app"], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.api = Api(f"http://127.0.0.1:{cls.port}")
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if cls.api.get("/api/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            cls.proc.terminate()
            out = cls.proc.stdout.read() if cls.proc.stdout else ""
            raise RuntimeError("服务启动失败:\n" + out)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ----------------------------------------------------------- helpers
    def upload(self, buf: bytes, sha: str, filename: str, drop_chunks=(),
               content_type="application/octet-stream"):
        api = self.api
        st, sess = api.post("/api/uploads", {
            "sha256": sha, "size": len(buf), "filename": filename,
            "content_type": content_type})
        self.assertEqual(st, 201, sess)
        if sess.get("already_exists"):
            return sess
        sid = sess["id"]
        total = max(1, (len(buf) + CHUNK - 1) // CHUNK)
        for i in range(total):
            if i in drop_chunks:
                continue
            chunk = buf[i * CHUNK:(i + 1) * CHUNK]
            st, r = api.put_raw(f"/api/uploads/{sid}/chunks/{i}", chunk)
            self.assertEqual(st, 200, r)
        return sid

    def finalize(self, sid):
        return self.api.post(f"/api/uploads/{sid}/finalize")

    def new_submission_version(self, title="虚构：青山水库巡查"):
        st, sub = self.api.post("/api/submissions", {"title": title})
        self.assertEqual(st, 201)
        st, ver = self.api.post(
            f"/api/submissions/{sub['id']}/versions", {"base_version_id": None})
        self.assertEqual(st, 201)
        return sub, ver

    def attach_three(self, ver, form_buf, form_sha, img_buf, img_sha, note_buf, note_sha):
        for role, buf, sha, name in [
            ("form", form_buf, form_sha, "form.json"),
            ("image", img_buf, img_sha, "site.png"),
            ("note", note_buf, note_sha, "note.txt"),
        ]:
            sid = self.upload(buf, sha, name)
            if isinstance(sid, str):
                st, f = self.finalize(sid)
                self.assertEqual(st, 200, f)
            st, r = self.api.post(f"/api/versions/{ver['id']}/attach",
                                  {"role": role, "sha256": sha, "size": len(buf), "filename": name})
            self.assertEqual(st, 200, r)

    # ----------------------------------------------------------- tests
    def test_01_full_flow_happy_path_and_downstream(self):
        form_buf, form_sha = make_file(512, "form-v1")
        img_buf, img_sha = make_file(CHUNK * 2 + 37, "img-v1")
        note_buf, note_sha = make_file(256, "note-v1")
        sub, ver = self.new_submission_version("用例1-完整流程")
        self.attach_three(ver, form_buf, form_sha, img_buf, img_sha, note_buf, note_sha)

        st, got = self.api.get(f"/api/versions/{ver['id']}")
        self.assertEqual(got["status"], "complete")
        self.assertEqual(got["content_check"], "pass")
        self.assertEqual(got["relation_check"], "pass")

        # 未审核前下游不可见
        st, down = self.api.get("/api/downstream")
        self.assertFalse(any(it["version"]["id"] == ver["id"] for it in down["items"]))

        st, _ = self.api.post(f"/api/versions/{ver['id']}/review/submit")
        self.assertEqual(st, 200)
        st, got = self.api.post(f"/api/versions/{ver['id']}/review",
                                {"approve": True, "comment": "通过"})
        self.assertEqual(st, 200)
        self.assertEqual(got["status"], "approved")

        st, down = self.api.get("/api/downstream")
        item = next(it for it in down["items"] if it["submission_id"] == sub["id"])
        self.assertEqual(item["version"]["id"], ver["id"])

        # 下载内容与原始一致
        with urllib.request.urlopen(f"{self.api.base}/api/blobs/{img_sha}") as r:
            self.assertEqual(r.read(), img_buf)

    def test_02_resume_after_interrupted_upload(self):
        buf, sha = make_file(CHUNK * 3 + 10, "resume-file")
        st, sess = self.api.post("/api/uploads", {
            "sha256": sha, "size": len(buf), "filename": "big.bin"})
        sid = sess["id"]
        # 只传第 0、2 片（第 1 片丢失，模拟中断）
        self.api.put_raw(f"/api/uploads/{sid}/chunks/0", buf[:CHUNK])
        self.api.put_raw(f"/api/uploads/{sid}/chunks/2", buf[2 * CHUNK:3 * CHUNK])
        st, status = self.api.get(f"/api/uploads/{sid}/status")
        self.assertEqual(set(status["chunks"]), {0, 2})
        # 恢复：补第 1、3 片（续传，不重传已有分片）
        self.api.put_raw(f"/api/uploads/{sid}/chunks/1", buf[CHUNK:2 * CHUNK])
        self.api.put_raw(f"/api/uploads/{sid}/chunks/3", buf[3 * CHUNK:])
        st, fin = self.finalize(sid)
        self.assertEqual(st, 200, fin)
        self.assertEqual(fin["status"], "finalized")

    def test_03_sha_mismatch_rejected(self):
        buf, _ = make_file(1024, "tamper")
        fake_sha = "a" * 64
        sid = self.upload(buf, fake_sha, "tamper.bin")
        st, r = self.finalize(sid)
        self.assertEqual(st, 422)
        self.assertIn("内容核对失败", r["error"])

    def test_04_blob_first_then_form(self):
        # 先传附件（此时没有任何版本引用 → 孤儿 blob）
        buf, sha = make_file(777, "early-blob")
        sid = self.upload(buf, sha, "early.txt")
        self.finalize(sid)
        st, stats = self.api.get("/api/admin/stats")
        self.assertGreaterEqual(stats["orphan_blobs"], 1)
        # 再建版本、补表单并挂上早期 blob
        sub, ver = self.new_submission_version("用例4-先附件后表单")
        st, r = self.api.post(f"/api/versions/{ver['id']}/attach",
                              {"role": "image", "sha256": sha, "size": len(buf), "filename": "early.txt"})
        self.assertEqual(st, 200)
        form_buf, form_sha = make_file(300, "form4")
        note_buf, note_sha = make_file(200, "note4")
        for role, b, s, name in [("form", form_buf, form_sha, "f.json"),
                                 ("note", note_buf, note_sha, "n.txt")]:
            x = self.upload(b, s, name)
            self.finalize(x)
            self.api.post(f"/api/versions/{ver['id']}/attach",
                          {"role": role, "sha256": s, "size": len(b), "filename": name})
        st, got = self.api.get(f"/api/versions/{ver['id']}")
        self.assertEqual(got["status"], "complete")
        self.assertEqual(got["relation_check"], "pass")

    def test_05_orphan_cleanup_and_active_upload_protection(self):
        # 孤儿 blob：宽限期内必须保留（无论后台 reaper 跑了几轮），过宽限期后必删
        buf, sha = make_file(333, "doomed-orphan")
        sid = self.upload(buf, sha, "doomed.bin")
        self.finalize(sid)
        deadline = time.time() + 1.5  # 宽限 2s：这段时间内反复清理都不能删
        while time.time() < deadline:
            st, rep = self.api.post("/api/admin/cleanup")
            self.assertNotIn(sha, rep["removed_orphan_blobs"])
            self.assertEqual(self.api.status(f"/api/blobs/{sha}"), 200)
            time.sleep(0.2)
        # 轮询等待过宽限期后的最终删除（reaper 或手动清理均可）
        deleted = False
        for _ in range(20):
            self.api.post("/api/admin/cleanup")
            if self.api.status(f"/api/blobs/{sha}") == 404:
                deleted = True
                break
            time.sleep(0.3)
        self.assertTrue(deleted, "孤儿 blob 过宽限期后未被清理")

        # 进行中（持续续期）的上传不能被清理误删
        buf2, sha2 = make_file(CHUNK * 2, "protected")
        st, sess = self.api.post("/api/uploads", {
            "sha256": sha2, "size": len(buf2), "filename": "p.bin"})
        psid = sess["id"]
        self.api.put_raw(f"/api/uploads/{psid}/chunks/0", buf2[:CHUNK])  # 续期
        time.sleep(1.2)
        self.api.put_raw(f"/api/uploads/{psid}/chunks/1", buf2[CHUNK:])  # 再次续期
        st, rep = self.api.post("/api/admin/cleanup")
        self.assertNotIn(psid, rep["expired_sessions"])
        st, status = self.api.get(f"/api/uploads/{psid}/status")
        self.assertEqual(status["status"], "open")
        # 完成它，避免租约到期后影响后续统计
        self.finalize(psid)

    def test_06_expired_session_reaped(self):
        buf, sha = make_file(500, "expire-me")
        st, sess = self.api.post("/api/uploads", {
            "sha256": sha, "size": len(buf), "filename": "e.bin"})
        sid = sess["id"]
        time.sleep(2.4)  # 租约 2s；后台 reaper(1s) 或下面的手动清理都可能回收
        st, rep = self.api.post("/api/admin/cleanup")
        # 无论谁先回收，过期会话必须已消失，且不会再被当成有效上传
        self.assertTrue(
            sid in rep["expired_sessions"]
            or self.api.get(f"/api/uploads/{sid}/status")[0] == 404)
        st, _ = self.api.get(f"/api/uploads/{sid}/status")
        self.assertEqual(st, 404)  # 终态会话行已被清

    def test_07_revision_revokes_one_attachment_and_supersede(self):
        form1, fsha1 = make_file(128, "r-form1")
        img1, isha1 = make_file(256, "r-img1")
        note1, nsha1 = make_file(128, "r-note1")
        sub, v1 = self.new_submission_version("用例7-修订撤销")
        self.attach_three(v1, form1, fsha1, img1, isha1, note1, nsha1)
        self.api.post(f"/api/versions/{v1['id']}/review/submit")
        st, approved = self.api.post(f"/api/versions/{v1['id']}/review",
                                     {"approve": True, "comment": "v1 通过"})
        self.assertEqual(approved["status"], "approved")

        # v2：只撤销图片，另两个附件重传新版本内容
        st, v2 = self.api.post(f"/api/submissions/{sub['id']}/versions",
                               {"base_version_id": v1["id"]})
        self.assertEqual(st, 201)
        st, r = self.api.post(f"/api/versions/{v2['id']}/revoke", {"role": "image"})
        self.assertEqual(st, 200, r)
        form2, fsha2 = make_file(128, "r-form2")
        note2, nsha2 = make_file(128, "r-note2")
        for role, b, s, name in [("form", form2, fsha2, "f2.json"),
                                 ("note", note2, nsha2, "n2.txt")]:
            x = self.upload(b, s, name)
            self.finalize(x)
            self.api.post(f"/api/versions/{v2['id']}/attach",
                          {"role": role, "sha256": s, "size": len(b), "filename": name})
        st, got = self.api.get(f"/api/versions/{v2['id']}")
        self.assertEqual(got["status"], "complete")
        self.assertEqual(got["relation_check"], "pass", got["relation_detail"])
        revoked = next(f for f in got["files"] if f["role"] == "image")
        self.assertTrue(revoked["revoked"])

        # 首版撤销无父附件 → 关系核对必须失败
        _, v3 = self.api.post(f"/api/submissions/{sub['id']}/versions",
                              {"base_version_id": None})
        self.api.post(f"/api/versions/{v3['id']}/revoke", {"role": "image"})
        fb, fs = make_file(64, "v3f")
        nb, ns = make_file(64, "v3n")
        for role, b, s, name in [("form", fb, fs, "f"), ("note", nb, ns, "n")]:
            x = self.upload(b, s, name)
            self.finalize(x)
            self.api.post(f"/api/versions/{v3['id']}/attach",
                          {"role": role, "sha256": s, "size": len(b), "filename": name})
        st, got3 = self.api.get(f"/api/versions/{v3['id']}")
        self.assertEqual(got3["relation_check"], "fail")
        # 核对不过禁止通过
        self.api.post(f"/api/versions/{got3['id']}/review/submit")
        st, err = self.api.post(f"/api/versions/{got3['id']}/review",
                                {"approve": True, "comment": "x"})
        self.assertEqual(st, 409)

        # v2 通过 → v1 转 superseded，下游只见 v2
        self.api.post(f"/api/versions/{v2['id']}/review/submit")
        st, appr2 = self.api.post(f"/api/versions/{v2['id']}/review",
                                  {"approve": True, "comment": "v2 通过"})
        self.assertEqual(appr2["status"], "approved")
        st, old = self.api.get(f"/api/versions/{v1['id']}")
        self.assertEqual(old["status"], "superseded")
        st, down = self.api.get("/api/downstream")
        item = next(it for it in down["items"] if it["submission_id"] == sub["id"])
        self.assertEqual(item["version"]["id"], v2["id"])

    def test_08_review_guard_immutability_and_reject(self):
        fb, fs = make_file(64, "g-f")
        ib, is_ = make_file(64, "g-i")
        nb, ns = make_file(64, "g-n")
        _, ver = self.new_submission_version("用例8-审核守卫")
        self.attach_three(ver, fb, fs, ib, is_, nb, ns)
        self.api.post(f"/api/versions/{ver['id']}/review/submit")
        # 审核中不可改清单/文件
        st, err = self.api.post(f"/api/versions/{ver['id']}/revoke", {"role": "image"})
        self.assertEqual(st, 409)
        st, err = self.api.post(f"/api/versions/{ver['id']}/attach",
                                {"role": "image", "sha256": fs, "size": 64, "filename": "x"})
        self.assertEqual(st, 409)
        # 组装中版本不能送审
        _, v2 = self.api.post(
            f"/api/submissions/{ver['submission_id']}/versions",
            {"base_version_id": ver["id"]})
        st, err = self.api.post(f"/api/versions/{v2['id']}/review/submit")
        self.assertEqual(st, 409)
        # 驳回流程
        st, r = self.api.post(f"/api/versions/{ver['id']}/review",
                              {"approve": False, "comment": "资料不清"})
        self.assertEqual(r["status"], "rejected")
        st, down = self.api.get("/api/downstream")
        self.assertFalse(any(it["version"]["id"] == ver["id"] for it in down["items"]))

    def test_09_dedup_and_reattach_same_role(self):
        b1, s1 = make_file(400, "dedup")
        sid = self.upload(b1, s1, "a.bin")
        self.finalize(sid)
        # 同 sha 再传：秒传
        st, sess = self.api.post("/api/uploads", {
            "sha256": s1, "size": len(b1), "filename": "a-copy.bin"})
        self.assertTrue(sess.get("already_exists"))
        # 同版本同角色重传（覆盖指针）
        _, ver = self.new_submission_version("用例9-重传去重")
        self.api.post(f"/api/versions/{ver['id']}/attach",
                      {"role": "image", "sha256": s1, "size": len(b1), "filename": "a.bin"})
        b2, s2 = make_file(400, "dedup-new")
        x = self.upload(b2, s2, "b.bin")
        self.finalize(x)
        st, r = self.api.post(f"/api/versions/{ver['id']}/attach",
                              {"role": "image", "sha256": s2, "size": len(b2), "filename": "b.bin"})
        self.assertEqual(st, 200)
        st, got = self.api.get(f"/api/versions/{ver['id']}")
        img = next(f for f in got["files"] if f["role"] == "image")
        self.assertEqual(img["sha256"], s2)

    def test_10_invalid_relations(self):
        # base 不属于该提交
        _, va = self.new_submission_version("用例10-A")
        _, vb = self.new_submission_version("用例10-B")
        st, err = self.api.post(f"/api/submissions/{va['submission_id']}/versions",
                                {"base_version_id": vb["id"]})
        self.assertEqual(st, 400)
        # 非法角色
        st, err = self.api.post(f"/api/versions/{va['id']}/revoke", {"role": "video"})
        self.assertEqual(st, 400)

    def test_11_manifest_and_recheck_detect_tamper(self):
        fb, fs = make_file(64, "m-f")
        ib, is_ = make_file(64, "m-i")
        nb, ns = make_file(64, "m-n")
        _, ver = self.new_submission_version("用例11-篡改检测")
        # 用 manifest 一次性声明（覆盖 manifest 路径）
        entries = [
            {"role": "form", "sha256": fs, "size": len(fb), "filename": "f"},
            {"role": "image", "sha256": is_, "size": len(ib), "filename": "i"},
            {"role": "note", "sha256": ns, "size": len(nb), "filename": "n"},
        ]
        # blob 未上传时清单可挂但版本保持 assembling
        st, r = self.api.post(f"/api/versions/{ver['id']}/manifest", {"entries": entries})
        self.assertEqual(r["status"], "assembling")
        for b, s in [(fb, fs), (ib, is_), (nb, ns)]:
            x = self.upload(b, s, "f")
            self.finalize(x)
        st, got = self.api.get(f"/api/versions/{ver['id']}")
        self.assertEqual(got["status"], "complete")
        # 物理篡改 blob 后手动 recheck → 内容核对失败
        from server.storage import Storage
        # 通过 HTTP 无法直接拿路径，改用 storage 目录约定
        blob_path = os.path.join(self.tmp, "blobs", fs)
        with open(blob_path, "r+b") as fh:
            fh.write(b"X" * 8)
        st, r = self.api.post(f"/api/versions/{ver['id']}/recheck")
        self.assertEqual(st, 200)
        self.assertEqual(r["content_check"], "fail")
        # 修复后重新核对通过
        with open(blob_path, "wb") as fh:
            fh.write(fb)
        st, r = self.api.post(f"/api/versions/{ver['id']}/recheck")
        self.assertEqual(r["content_check"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
