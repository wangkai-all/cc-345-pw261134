# 离线填报 · 版本核对与审核入库系统

一次离线回传包含**表单、图片、修订说明**三类文件，它们可能分别重传，修订还可能只撤销其中
一个附件。接收端**不能按最近上传时间把文件拼成一版**。本系统从空目录实现三端：

| 端 | 形态 | 职责 |
|---|---|---|
| ① 离线填报端 | 浏览器页面 | 选文件、分片上传、补传/重传、撤销附件、断网发件箱与断点续传 |
| ② 接收服务 + 审核入库端 | Python 标准库 HTTP 服务 + 浏览器页面 | 分片接收、租约与清理、版本关系核对 + 文件内容核对、审核通过/驳回 |
| ③ 下游读取端 | 浏览器页面 | 只能读到「完整且审核通过」的最新版本，并可独立重算 SHA-256 复核 |

> 所有演示资料均为虚构，无第三方运行时依赖（仅 Python 3.11 标准库 + 原生浏览器 API）。

## 1. 快速开始

```bash
./run.sh                      # 或 python3 -m server.app
# 另开一个终端，播种虚构演示数据（可选）
python3 tools/seed_demo.py
# 浏览器打开
open http://127.0.0.1:8000/
```

环境变量：`HOST` / `PORT` / `DATA_DIR` / `LEASE_SECONDS`（分片租约）/
`GRACE_SECONDS`（孤儿 blob 宽限期）/ `REAPER_INTERVAL`（后台清理间隔）。

测试：

```bash
python3 -m unittest tests.test_e2e -v   # 11 个端到端用例，真实启停 HTTP 服务
```

## 2. 核心设计：为什么不会「按时间拼版」

### 2.1 版本内容由显式 manifest 声明，而非上传时间推断

每个版本（`versions` 行）由客户端显式创建并声明 `base_version_id`（构成版本 DAG）。
文件与版本的关系写入 `version_files(version_id, role, sha256, size, filename, revoked)`，
**一个版本某一角色指向哪个 blob 完全由清单决定**，与 blob 的上传先后、上传时间无关：

- 同一版本同角色重传 → 覆盖该角色指针（旧 blob 若无引用则按孤儿规则清理）；
- 三个文件可以任意顺序到达，甚至先传大附件、几天后再补表单；
- 撤销只写 `revoked=1` 墓碑，不删父版本 blob；关系核对会校验父版本确有可沿用的附件。

### 2.2 内容核对与关系核对是两条独立结论

版本三角色齐备且 blob 全部就位时自动触发，审核端也可随时「重新核对」：

- **内容核对（content_check）**：逐个重算磁盘 blob 的 SHA-256 与字节数，和 manifest 比对。
  分片 `finalize` 时已做过一次，这里是面向审核的独立复核（测试 11 演示物理篡改后核对失败）。
- **关系核对（relation_check）**：校验版本链（`base` 必须属于同一提交、无环）、
  三个必备角色都有声明（有效附件或显式撤销墓碑）、撤销在父版本有对应有效附件。

两项分别给出 `pass/fail` 与原因。**审核通过的硬门槛是两项都 `pass`**，否则接口返回 409。

### 2.3 版本状态机与下游可见性

```
assembling ──三角色齐备且blob就位──▶ complete ──送审──▶ under_review
   ▲                                      │              ├─ approve ─▶ approved（下游可见）
   └──────── 仅 assembling 可改清单/附件    │              └─ reject  ─▶ rejected（下游不可见）
                                          └── 任一核对 fail：仍为 complete，但无法通过审核
新版本 approve 后，同一提交旧的 approved 自动转 superseded；下游每个提交只暴露最新 approved。
```

下游接口 `GET /api/downstream` 只返回 `status=approved` 的版本；组装中、审核中、驳回、
被取代的版本一律不可见。页面端「独立核对」按钮会重新下载 blob 并用 Web Crypto 重算
SHA-256，不盲信服务端返回的元数据。

## 3. 分片上传、断网恢复与空间清理

### 3.1 分片 + 租约 + 断点续传

- 文件在浏览器本地用 Web Crypto 算 SHA-256，按 256 KiB 分片 PUT 到
  `/api/uploads/{id}/chunks/{index}`；
- 每写一片自动**续期租约**；`GET .../status` 返回已收分片号，刷新页面/断网恢复后只补传缺失分片；
- `finalize` 时服务端顺序拼接、重算 SHA-256/大小，不符返回 422 且不落盘；
  相同内容已存在时直接秒传（内容寻址去重）；
- 租约过期后再写返回 410，前端自动重建会话重传；`abort` 或过期由清理回收分片目录。

### 3.2 断网演示（页面右上角开关，纯前端拦截 fetch）

- 元数据类操作（新建提交、新版本、挂载、撤销、送审、审核）进入 localStorage **发件箱**，
  恢复网络后按入队顺序自动重放，业务错误（如 409）不会卡死队列；
- 分片上传不进发件箱：断网时暂停并保留服务端已收分片，恢复后续传；
- 可勾选「慢速模式」拉大上传窗口，方便在传输中途切换断网开关。

### 3.3 清理与正在恢复的上传如何互不误删

清理器（后台线程 + `POST /api/admin/cleanup` 手动触发）规则：

1. 只回收 **租约已过期** 的 `open` 会话——持续写分片的上传每次都在续期，租约恒在未来；
2. 无任何版本引用、且无 `open` 会话关联的 blob 才算孤儿；孤儿有 `GRACE_SECONDS` 宽限期
   （保证「先传大附件再补表单」不会被立刻删），过期才物理删除；
3. 所有会话、blob、清理操作共用进程内一把 `RLock` 并走同一条 SQLite 连接，
   清理的物理删除与上传 `finalize` 不可能并发交错。

测试 05 专门验证：宽限期内反复清理 blob 仍在、过期后必删、持续续期的会话绝不被回收。

## 4. 目录结构

```
server/
  storage.py   # SQLite 元数据 + 磁盘 blob/分片：版本 DAG、双核对、上传会话、清理、审计
  app.py       # 标准库 ThreadingHTTPServer：REST 接口 + 静态页面 + 后台清理线程
web/
  index.html   # 三端单页（填报 / 审核入库 / 下游）
  css/styles.css
  js/
    api.js        # fetch 封装、断网拦截、发件箱重放
    uploader.js   # SHA-256、分片、断点续传、租约过期重建
    filing.js     # 填报端
    review.js     # 审核入库端 + 运维统计/审计
    downstream.js # 下游只读视图 + 独立复核
    app.js        # 入口与公共工具
tests/test_e2e.py  # 11 个真实 HTTP 端到端用例
tools/seed_demo.py # 虚构演示数据播种
run.sh
```

## 5. HTTP 接口摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/submissions` | 新建填报提交 |
| GET | `/api/submissions`、`/api/submissions/{id}` | 提交与版本 DAG |
| POST | `/api/submissions/{id}/versions` | 建版本（body 带 `base_version_id`） |
| POST | `/api/versions/{id}/manifest` | 整份清单声明（blob 可后到） |
| POST | `/api/versions/{id}/attach` | 把已上传 blob 挂到某角色（重传即覆盖） |
| POST | `/api/versions/{id}/revoke` | 撤销单角色（写墓碑，关系核对校验父版本） |
| POST | `/api/versions/{id}/recheck` | 重跑内容+关系双核对 |
| POST | `/api/versions/{id}/review/submit` | 送审 |
| POST | `/api/versions/{id}/review` | 审核 `{approve, comment}`，双 pass 才能 approve |
| GET | `/api/versions/{id}` | 版本详情含两项核对结论与清单 |
| POST | `/api/uploads` | 建分片会话（同 sha 幂等复用 / 已存在秒传） |
| PUT | `/api/uploads/{id}/chunks/{i}` | 上传分片（自动续期） |
| GET | `/api/uploads/{id}/status` | 已收分片（断点续传依据） |
| POST | `/api/uploads/{id}/finalize` | 拼接 + SHA-256/大小复核落盘 |
| POST | `/api/uploads/{id}/abort` | 放弃上传 |
| GET | `/api/blobs/{sha256}` | 下载 blob |
| GET | `/api/downstream` | 下游视图：每提交仅最新 approved |
| POST | `/api/admin/cleanup` / GET `/api/admin/stats` | 手动清理 / 存储统计 |
| GET | `/api/audit` | 审计日志与清理器日志 |

## 6. 建议的页面演示路径

1. 播种数据后打开页面，先在**下游端**确认只能看到 v1（已入库），v2 审核中不可见；
2. 填报端新建提交 → 建版本 → 分别为三个角色选择文件（可在「先传大附件」区先传图片，再挂版本）；
3. 开「慢速模式」，传一个大文件，传到一半勾选**模拟断网**：观察分片暂停、发件箱累积；
   取消断网后自动续传、发件箱逐条重放；
4. 基于最新版发新版本，只撤销图片并替换表单/说明，送审；
5. 在审核端观察双核对结论，先试驳回复核失败的版本，再通过 v2——回到下游端确认可见版本切换，
   旧 v1 变为「已被取代」；点「独立核对」让浏览器重算 SHA-256；
6. 审核端手动执行清理，结合存储统计与审计日志观察孤儿 blob 宽限期与过期回收。
