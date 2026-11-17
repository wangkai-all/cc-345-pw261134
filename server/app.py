"""HTTP 服务：离线填报接收端（Python 标准库实现，无第三方依赖）。

运行：python3 -m server.app
环境变量：
  DATA_DIR           数据目录（默认 ./data）
  HOST/PORT          监听地址（默认 127.0.0.1:8000）
  LEASE_SECONDS      分片上传会话租约（默认 900）
  GRACE_SECONDS      孤儿 blob 清理宽限期（默认 1800）
  REAPER_INTERVAL    后台清理间隔秒（默认 30）
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .storage import Storage, StorageError, UPLOAD_CHUNK_SIZE

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class App:
    def __init__(self, storage: Storage, lease_seconds: int):
        self.storage = storage
        self.lease_seconds = lease_seconds
        self.reaper_log: list[dict] = []

    def start_reaper(self, interval: int) -> None:
        def loop():
            while True:
                time.sleep(interval)
                try:
                    report = self.storage.cleanup()
                    if report["expired_sessions"] or report["removed_orphan_blobs"]:
                        self.reaper_log.append(report)
                        self.reaper_log[:] = self.reaper_log[-20:]
                except Exception as exc:  # 后台线程不能死
                    self.reaper_log.append({"error": str(exc), "ts": time.time()})

        threading.Thread(target=loop, name="reaper", daemon=True).start()


def make_handler(app: App):
    st = app.storage

    class Handler(BaseHTTPRequestHandler):
        server_version = "OfflineFiling/1.0"

        def log_message(self, fmt, *args):  # 安静一点
            pass

        # ------------------------------------------------------------ helpers
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StorageError(400, f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise StorageError(400, "请求体必须是 JSON 对象")
            return data

        def _read_bytes(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                raise StorageError(400, "缺少分片内容")
            return self.rfile.read(length)

        def _err(self, exc: StorageError):
            self._json({"error": exc.message}, exc.status)

        # ------------------------------------------------------------ routing
        def do_GET(self):
            try:
                self._route_get()
            except StorageError as e:
                self._err(e)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"服务器内部错误: {e}"}, 500)

        def do_POST(self):
            try:
                self._route_post()
            except StorageError as e:
                self._err(e)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"服务器内部错误: {e}"}, 500)

        def do_PUT(self):
            try:
                self._route_put()
            except StorageError as e:
                self._err(e)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"服务器内部错误: {e}"}, 500)

        def _route_get(self):
            p = [x for x in urlparse(self.path).path.split("/") if x]
            if p == ["api", "health"]:
                return self._json({"ok": True, "ts": time.time()})
            if p == ["api", "submissions"]:
                return self._json({"submissions": st.list_submissions()})
            if len(p) == 3 and p[:2] == ["api", "submissions"]:
                return self._json(st.get_submission(p[2]))
            if len(p) == 3 and p[:2] == ["api", "versions"]:
                return self._json(st.get_version(p[2]))
            if len(p) == 4 and p[:2] == ["api", "uploads"] and p[3] == "status":
                return self._json(st.get_upload_session(p[2]))
            if len(p) == 3 and p[:2] == ["api", "uploads"]:
                return self._json(st.get_upload_session(p[2]))
            if p == ["api", "downstream"]:
                return self._json({"items": st.downstream_latest()})
            if p == ["api", "audit"]:
                return self._json({"entries": st.list_audit(200), "reaper": app.reaper_log})
            if p == ["api", "admin", "stats"]:
                return self._json(st.stats())
            if len(p) == 3 and p[:2] == ["api", "blobs"]:
                return self._download_blob(p[2])
            return self._static(p)

        def _route_post(self):
            p = [x for x in urlparse(self.path).path.split("/") if x]
            # 分片上传是原始二进制，其余接口读 JSON
            if len(p) == 5 and p[:2] == ["api", "uploads"] and p[3] == "chunks":
                data = self._read_bytes()
                return self._json(st.put_chunk(p[2], int(p[4]), data, app.lease_seconds))
            body = self._read_json()
            if p == ["api", "submissions"]:
                return self._json(st.create_submission(body.get("title", "")), 201)
            if len(p) == 4 and p[:2] == ["api", "submissions"] and p[3] == "versions":
                return self._json(
                    st.create_version(p[2], body.get("base_version_id")), 201)
            if len(p) == 4 and p[:2] == ["api", "versions"] and p[3] == "manifest":
                return self._json(st.attach_manifest(p[2], body.get("entries", [])))
            if len(p) == 4 and p[:2] == ["api", "versions"] and p[3] == "revoke":
                return self._json(st.revoke_file(p[2], body.get("role", "")))
            if len(p) == 4 and p[:2] == ["api", "versions"] and p[3] == "attach":
                return self._json(st.attach_blob_to_version(
                    p[2], body["role"], body["filename"], body["sha256"], int(body["size"])))
            if len(p) == 5 and p[:2] == ["api", "versions"] and p[3] == "review" and p[4] == "submit":
                return self._json(st.submit_for_review(p[2]))
            if len(p) == 4 and p[:2] == ["api", "versions"] and p[3] == "review":
                return self._json(st.review(
                    p[2], bool(body.get("approve")), body.get("comment", ""),
                    body.get("actor", "reviewer")))
            if len(p) == 4 and p[:2] == ["api", "versions"] and p[3] == "recheck":
                return self._json(st.recheck(p[2]))
            if p == ["api", "uploads"]:
                return self._json(st.create_upload_session(
                    body["sha256"], int(body["size"]), body["filename"],
                    body.get("content_type", ""), app.lease_seconds), 201)
            if len(p) == 4 and p[:2] == ["api", "uploads"] and p[3] == "finalize":
                return self._json(st.finalize_upload(p[2]))
            if len(p) == 4 and p[:2] == ["api", "uploads"] and p[3] == "abort":
                return self._json(st.abort_upload(p[2]))
            if p == ["api", "admin", "cleanup"]:
                return self._json(st.cleanup())
            raise StorageError(404, f"未知接口: /{'/'.join(p)}")

        def _route_put(self):
            p = [x for x in urlparse(self.path).path.split("/") if x]
            # PUT /api/uploads/{id}/chunks/{index}
            if len(p) == 5 and p[:2] == ["api", "uploads"] and p[3] == "chunks":
                data = self._read_bytes()
                return self._json(st.put_chunk(p[2], int(p[4]), data, app.lease_seconds))
            if len(p) == 4 and p[:2] == ["api", "versions"] and p[3] == "manifest":
                body = self._read_json()
                return self._json(st.attach_manifest(p[2], body.get("entries", [])))
            raise StorageError(404, f"未知接口: /{'/'.join(p)}")

        # ------------------------------------------------------------ download
        def _download_blob(self, sha256: str):
            path, meta = st.blob_path_for_download(sha256)
            with open(path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Blob-Sha256", sha256)
            self.end_headers()
            self.wfile.write(data)

        # ------------------------------------------------------------ static
        def _static(self, parts: list[str]):
            if parts and parts[0] == "api":
                raise StorageError(404, f"未知接口: /{'/'.join(parts)}")
            rel = os.path.normpath(os.path.join(*parts) if parts else "index.html")
            if rel.startswith("..") or os.path.isabs(rel):
                raise StorageError(403, "非法路径")
            full = os.path.join(WEB_DIR, rel)
            if not os.path.isfile(full):
                full = os.path.join(WEB_DIR, "index.html")  # SPA 回退
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    lease = int(os.environ.get("LEASE_SECONDS", "900"))
    grace = int(os.environ.get("GRACE_SECONDS", "1800"))
    interval = int(os.environ.get("REAPER_INTERVAL", "30"))

    storage = Storage(data_dir, grace_seconds=grace)
    app = App(storage, lease_seconds=lease)
    app.start_reaper(interval)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"离线填报接收端已启动: http://{host}:{port}  数据目录={data_dir}")
    print(f"租约={lease}s 宽限期={grace}s 清理间隔={interval}s 分片大小={UPLOAD_CHUNK_SIZE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
