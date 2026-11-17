"""演示数据播种（全部资料虚构）：通过真实 HTTP 接口造数据。

用法：先启动服务（python3 -m server.app），再运行
  python3 tools/seed_demo.py [--base http://127.0.0.1:8000]

造出来的场景：
  提交①「青云县水库大坝巡查（虚构）」
    v1 已审核通过并入库（下游可见）
    v2 修订版审核中：撤销图片、表单与修订说明更新
  提交②「临江镇农田水利普查（虚构）」
    v1 组装中：大附件已先传（孤儿 blob，宽限期保护），表单待补
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import urllib.error
import urllib.request
import zlib

CHUNK = 256 * 1024


# ------------------------------------------------------------- 虚构文件生成
def make_form(seq: int) -> bytes:
    data = {
        "填报单位": f"虚构水利巡查第{seq}组",
        "水库名称": "青云县青竹水库（虚构）",
        "巡查日期": "2026-11-15",
        "库水位_m": 231.4 + seq * 0.1,
        "渗流量_L_s": 0.8 + seq * 0.05,
        "坝顶位移_mm": 1.2 + seq * 0.3,
        "隐患描述": "无重大隐患" if seq == 1 else "左岸排水沟有轻微淤积，已记录待清淤",
        "填报人": f"虚构填报员-0{seq}",
    }
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def make_note(seq: int) -> bytes:
    if seq == 1:
        text = "首版上报：表单、现场图片、修订说明三件套齐全，资料均为虚构。"
    else:
        text = (f"第 {seq} 次修订：更新库水位与位移读数；"
                "本版撤销上一版现场图片（镜头污损待重拍），图片沿用关系已在清单中声明。资料虚构。")
    return text.encode("utf-8")


def make_png(seed: str, size: int = 70000) -> bytes:
    """生成合法的最小 PNG（虚构现场示意图），无第三方依赖。"""
    width = height = 64
    raw = bytearray()
    counter = hashlib.sha256(seed.encode()).digest()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            i = (y * width + x) % len(counter)
            raw.extend([counter[i], counter[(i + 7) % 32], counter[(i + 13) % 32], 255])
    # 用重复图案填充到目标体积附近（PNG 扫描行之后追加 tEXt 块保持合法）
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    padding = b"fictional-demo-data " * max(0, (size - len(idat)) // 19)
    text = chunk(b"tEXt", b"comment\x00" + padding)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", idat) + text + chunk(b"IEND", b""))


# ------------------------------------------------------------- HTTP 客户端
class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None, raw=None):
        data, headers = None, {}
        if raw is not None:
            data = raw
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = r.read()
                return r.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def upload(self, buf: bytes, filename: str, ctype: str) -> str:
        sha = hashlib.sha256(buf).hexdigest()
        _, sess = self.call("POST", "/api/uploads",
                            {"sha256": sha, "size": len(buf), "filename": filename, "content_type": ctype})
        if sess.get("already_exists"):
            return sha
        sid = sess["id"]
        total = max(1, (len(buf) + CHUNK - 1) // CHUNK)
        for i in range(total):
            st, r = self.call("PUT", f"/api/uploads/{sid}/chunks/{i}",
                              raw=buf[i * CHUNK:(i + 1) * CHUNK])
            assert st == 200, r
        st, r = self.call("POST", f"/api/uploads/{sid}/finalize")
        assert st == 200, r
        return sha

    def attach(self, vid: str, role: str, buf: bytes, filename: str,
               ctype: str = "application/octet-stream"):
        sha = self.upload(buf, filename, ctype)
        st, r = self.call("POST", f"/api/versions/{vid}/attach",
                          {"role": role, "sha256": sha, "size": len(buf), "filename": filename})
        assert st == 200, r
        return sha


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    cli = Client(args.base)

    # 提交①：v1 通过入库，v2 审核中且撤销图片
    _, sub = cli.call("POST", "/api/submissions", {"title": "青云县水库大坝巡查（虚构）"})
    _, v1 = cli.call("POST", f"/api/submissions/{sub['id']}/versions", {"base_version_id": None})
    form1, img1, note1 = make_form(1), make_png("img-v1", 90000), make_note(1)
    cli.attach(v1["id"], "form", form1, "巡查表单-v1.json", "application/json")
    cli.attach(v1["id"], "image", img1, "现场照片-v1.png", "image/png")
    cli.attach(v1["id"], "note", note1, "修订说明-v1.txt", "text/plain")
    cli.call("POST", f"/api/versions/{v1['id']}/review/submit")
    st, r = cli.call("POST", f"/api/versions/{v1['id']}/review",
                     {"approve": True, "comment": "虚构审核：双核对通过，准予入库。"})
    assert st == 200, r

    _, v2 = cli.call("POST", f"/api/submissions/{sub['id']}/versions",
                     {"base_version_id": v1["id"]})
    st, r = cli.call("POST", f"/api/versions/{v2['id']}/revoke", {"role": "image"})
    assert st == 200, r
    cli.attach(v2["id"], "form", make_form(2), "巡查表单-v2.json", "application/json")
    cli.attach(v2["id"], "note", make_note(2), "修订说明-v2.txt", "text/plain")
    cli.call("POST", f"/api/versions/{v2['id']}/review/submit")

    # 提交②：先传大附件（孤儿 blob），版本仍在组装，等补表单
    _, sub2 = cli.call("POST", "/api/submissions", {"title": "临江镇农田水利普查（虚构）"})
    _, v3 = cli.call("POST", f"/api/submissions/{sub2['id']}/versions", {"base_version_id": None})
    big = make_png("early-big-attachment", 300000)
    sha = cli.upload(big, "普查影像-先行上传.png", "image/png")
    st, r = cli.call("POST", f"/api/versions/{v3['id']}/attach",
                     {"role": "image", "sha256": sha, "size": len(big),
                      "filename": "普查影像-先行上传.png"})
    assert st == 200, r

    # 额外：上传一个尚未挂载任何版本的 blob（真·孤儿），演示宽限期内不被清理
    orphan = make_png("orphan-attachment", 50000)
    orphan_sha = cli.upload(orphan, "暂存附件-未挂载.png", "image/png")

    print("演示数据播种完成（全部虚构）：")
    print(f"  提交① {sub['id']}：v1 已入库，v2 审核中（图片撤销）")
    print(f"  提交② {sub2['id']}：v1 组装中，大附件先行、表单待补")
    print(f"  孤儿 blob {orphan_sha[:12]}…：未挂载，宽限期内保留，过期自动清理")
    print(f"  打开 {args.base}/ 查看三个端的页面")


if __name__ == "__main__":
    main()
