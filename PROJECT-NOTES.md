# PROJECT-NOTES

> 本文记录当前工作区的代码事实，不是设计愿景。行号以本次读取的 `094fd3a` 工作区为准；清单文件实际名为 `manifest`，当前 `version = 1.0.9`（仓库没有 `manifest.json`）。不确定处明确标为 TODO。

## 1. 架构与数据流

### 1.1 启动与两座 Unix socket

`cmd/main` 负责拉起两个进程：

- `cmd/main:235-238`：启动 `proxy/run_proxy.sh`，把 PID 写入持久状态目录。
- `cmd/main:240-242`：启动 `proxy/app_socket_bridge.py`，供飞牛统一网关的 `/app/fnmusic_kugou_pro/*` 前缀使用。

`run_proxy.sh` 是“接管式”启动：

- `app/proxy/run_proxy.sh:68-69`：固定目标 socket 为 `/var/run/trim_music.socket`，原音乐端被改名为 `/var/run/trim_music_upstream.socket`。
- `app/proxy/run_proxy.sh:186-202`：先用同一套 Python 环境执行 `import app` 预检，失败就退出，尚未碰 socket。
- `app/proxy/run_proxy.sh:204-227`：预检通过后才执行接管：将目标 socket 改名为 upstream，或复用已存在的 upstream。
- `app/proxy/run_proxy.sh:230-232`：最后启动 uvicorn，并把代理绑定回 `/var/run/trim_music.socket`。

因此实际存在两条入口，而不是所有请求都先过桥接器：

```text
fnOS 音乐 App
  ├─ 主音乐 API ───────────────→ /var/run/trim_music.socket（代理 uvicorn）
  └─ /app/fnmusic_kugou_pro/* ─→ target/fnmusic_kugou_pro.sock（桥接器）
                                      └─→ /var/run/trim_music.socket（同一个代理）

代理（FastAPI）
  ├─ 命中的拦截路由 ─→ kugou_source / httpx ─→ KuGouMusicApi
  │                                     默认 http://127.0.0.1:8899
  └─ 未拦截路由 / 非 GET ─→ httpx(uds=/var/run/trim_music_upstream.socket)
                                      └─→ 原 trim-music（官方源）
```

桥接器的前缀、监听 socket 和上游 socket 定义在 `app/proxy/app_socket_bridge.py:28-31`；它校验 `/app/fnmusic_kugou_pro` 前缀后转发，见 `app/proxy/app_socket_bridge.py:213-247`。桥接器只处理网关前缀，不是主音乐 socket 的唯一入口。

代理侧的官方上游客户端使用 Unix socket 传输：

- `app/proxy/app.py:4176-4181`：`httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"])`。
- `app/proxy/app.py:4195-4223`：`forward_to_upstream()` 原样转发方法、路径、query、请求体和响应流。
- `app/proxy/app.py:9745-9777`：兜底路由；非 GET 直接透传，GET 的少数列表接口会补字段，其余继续透传。
- `app/proxy/app.py:5619-5646`：FastAPI lifespan 创建并关闭官方上游客户端。

### 1.2 搜索与元数据合并

歌曲搜索入口是 `app/proxy/app.py:5739-6020`：

- 关键词来自 `q` / `keyword` / `query`，见 `app/proxy/app.py:2086-2089`。
- 官方搜索与酷狗搜索并行：`app/proxy/app.py:5910-5921` 创建官方请求和酷狗预取任务，随后在 `:5951-5978` 合并。
- 酷狗底层调用 `kugou_source.search()`，见 `kugou_source.py:442-501`，请求 KuGouMusicApi 的 `/search`。
- 结果写入 `_SEARCH_CACHE`，只给 track 用；缓存与分级 TTL 见 `app/proxy/app.py:1236-1241`、`:1284-1365`。

歌手、专辑、歌单三个 meta tab 走 `merged_search_meta()`：

- 统一入口：`app/proxy/app.py:4770-4907`。
- 缓存键必须是 `meta:{tag}:{keyword}`：`app/proxy/app.py:4820`；`_META_CACHE` 的声明与说明在 `:1250-1253`。
- 官方全量请求：`app/proxy/app.py:4736-4767`。
- 并发闸键必须带 `tag`：meta 侧在 `app/proxy/app.py:4883`，track 侧在 `:5996`。

搜索的防抖、世代号和 TTL 都在 `app/proxy/app.py:1284-1365`。关键顺序是：先取本次 `gen`，再等待防抖窗口；见 `:1325-1341`。这样等待期间来了新词时，旧请求仍能在返回前被判为 stale。

### 1.3 在线播放、本地优先与归档

播放入口是 `app/proxy/app.py:6834-6928`，处理顺序如下：

1. 非在线 guid 直接透传官方源（`:6839-6841`）。
2. 先查内存音频缓存（`:6843-6848`）。
3. 对 `online:kugou:*`，若 `local_first` 开启且 marker 指向可播放文件，直接读本地归档（`:6850-6871`）。
4. 本地未命中，才查酷狗元数据并换播放 URL（`:6872-6884`）。
5. 边播边存开启且请求是完整拉取时，走 `stream_archive_response()`；否则走纯播放/内存 tee 路径（`:6899-6923`）。

本地优先的“已存过”判据集中在标记文件：

- `app/proxy/app.py:3360-3412`：marker 路径与默认目录。默认是落盘目录下的 `.fnmusic_save_index`；显式配置 `FNMUSIC_SAVE_MARKER_DIR` 时优先使用显式值。
- `app/proxy/app.py:3415-3436`：`saved_file_path()` 同时检查 marker、目标是否为普通文件、文件是否非空。
- `app/proxy/app.py:3444` 起：`saved_file_playable()` 再加可读性试读，失败则回落 CDN。
- `app/proxy/app.py:6498-6529`：只有客户端把整条流转发干净（`drained and state["complete"]`）才归档；客户端切歌/断开时取消后台 pump，不把半首当完整播放。
- 分块 Range 的覆盖度归档在 `app/proxy/app.py:6612-6662`、`:6665-6710` 和 `:6809-6831`。

归档落点与 marker 的关系，已有回归测试明确为“marker 跟随落盘目录”，不是安装目录；见 `tests/test_marker_dir.py:1-12`、`:58-90`。

## 2. 最容易被改坏的五处代码

| # | 位置 | 为什么危险 / 现有约束 |
|---|---|---|
| 1 | `app/proxy/run_proxy.sh:186`（预检）、`:204`（接管）、`:230`（启动） | 顺序必须是“预检 → 改名接管 → 启动 uvicorn”。若把 `mv` 放回预检前，代理导入失败时官方音乐 socket 已被移走，整个【音乐】会不可用。`cmd/main:247-263` 的启动失败还原、`:289` 的 stop 还原是第二道保险。`tests/test_env_empty.py:205-233` 专门做了静态断言和失败路径检查。 |
| 2 | `app/proxy/app_socket_bridge.py:134-182`，特别是 `:162-182` | 响应必须按 `Content-Length` 收满即回；`HEAD` / `204` / `304` 发完头就结束；多读到的 `rest` 必须先发出去；chunked/无长度才退回 EOF。改回无条件 `recv` 到 EOF 会重新引入 uvicorn keep-alive 的 5 秒卡顿；漏发 `rest` 会截断响应体。`tests/test_bridge_content_length.py` 覆盖这些分支。 |
| 3 | `app/proxy/app.py:4820`、`:4883`、`:5996` | 搜索的缓存/并发闸必须按 `tag` 隔离。`track`、`album`、`artist`、`playlist` 共用 `_SEARCH_INFLIGHT`；若 gate key 去掉 `tag`，并发时 track 会拿到 album 的 Future 和响应。`_META_CACHE` 同理，键必须是 `meta:{tag}:{keyword}`。`tests/test_search_debounce.py:488-551` 用故障注入复现 gate key 缺 tag 的串包，再验证修复。 |
| 4 | `app/proxy/app.py:1325-1341` | 必须在等待防抖之前捕获本次世代号 `gen`。若等 `state["event"]` 之后再读 `state["gen"]`，所有等待者都会读到最新世代，旧请求不再被判 stale，慢返回的旧结果会覆盖新结果并写入缓存。`tests/test_search_debounce.py` 的 a05 / n02 覆盖“旧请求慢返回”。 |
| 5 | `app/proxy/app.py:6843-6871` | 播放顺序必须保持“内存 → 本地 marker → CDN”。`local_first` 分支若挪到 `resolve_kugou_url()` 之后，所有已归档歌曲仍会先访问酷狗，断网/CDN 失效时本地播放失效；若放宽 `saved_file_playable()` 的校验，还可能把坏文件或目录当可播放文件。`tests/test_local_first.py:499-514` 用源码顺序断言锁住该顺序，T24-T28 覆盖命中、关闭开关和文件失效回落。 |

补充：播放归档中“只从客户端真正读干净的流归档”同样是高风险逻辑（`app/proxy/app.py:6498-6529`），虽然不进入上面五条，但它已经被切歌/分块 Range 场景覆盖；改动这一段时应同时看 `tests/test_pipeline.py` 和 `tests/test_save.py`。

## 3. 验证方法

### 3.1 测试脚本与覆盖

仓库约定使用指定 Python，不装 pytest：

```sh
PY=/vol1/@appcenter/fnmusic_kugou_pro/.venv-proxy/bin/python3.12
cd /vol1/1000/code/fnmusic_kugou_pro
PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_search_debounce.py
PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_bridge_content_length.py
PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_env_empty.py
PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_local_first.py
PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_marker_dir.py
PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_save.py
```

对应关系：

| 测试 | 覆盖点 |
|---|---|
| `tests/test_search_debounce.py` | 防抖合并、分级 TTL、翻页不等待、同参并发闸、stale 丢弃、gate key 带 `tag` 的故障注入与修复。 |
| `tests/test_bridge_content_length.py` | 有/无 `Content-Length`、body 前缀 `rest`、HEAD/204/304、1xx、chunked/EOF 回退，以及真实 Unix socket 的端到端耗时。 |
| `tests/test_env_empty.py` | 空值/非法值/空白值环境变量不炸模块；`run_proxy.sh` 预检在接管前；`cmd/main` 启动失败还原 socket。 |
| `tests/test_local_first.py` | marker 与文件一致性、坏文件回落、Range/ETag、流式内存、内存→本地→CDN 顺序、开关关闭后的阴性对照。 |
| `tests/test_marker_dir.py` | 默认 marker 目录在曲库落盘目录下、目录权限、覆盖安装删掉安装目录后仍认得已归档。 |
| `tests/test_save.py` | Range 解析、覆盖度合并、`save_enabled` 阴性对照、`.env` 写入安全、设置回读白名单。 |
| `tests/test_pipeline.py` | 真实 HTTP 路由下的边播边存：首次播放归档、二次播放本地优先、中途断开不归档、分块 Range 覆盖后归档。 |

### 3.2 故障注入与阴性对照

优先使用已有测试自带的注入，不要在真机上手工改坏 socket：

- 搜索 gate 串包：`tests/test_search_debounce.py` 的 t02 临时把 gate key 的 `tag` 剥掉，预期 track 拿到 album 数据；t03 恢复后各拿各的。
- 防抖关闭：把 `FNMUSIC_SEARCH_DEBOUNCE_S=0`，同一输入序列应产生多个请求；测试 n01 已给出该阴性对照。
- stale 旁路：测试 n02 将 `_search_stale` 恒设为 False，预期旧请求返回 5 条并写缓存，从而证明当前代码确实在阻止旧结果覆盖。
- 本地优先关闭：测试 T26 设置 `local_first=False`，预期回落 CDN；T27/T28 覆盖“未存过”和“本地文件被删”。
- 归档关闭：测试 `tests/test_pipeline.py` 第 5 节把 `save_enabled` 置 False，预期播放仍 200、目录无新增归档。

### 3.3 手工复现路径

以下命令只读/验证，不要在故障注入后把 socket 留在异常状态。

1. 桥接入口存活与健康：

```sh
curl -sS --unix-socket /var/apps/fnmusic_kugou_pro/target/fnmusic_kugou_pro.sock \
  http://localhost/app/fnmusic_kugou_pro/_ext/healthz
```

   期望返回 JSON，含 `"gateway": "ok"`；桥接前缀的响应语义可再用 `tests/test_bridge_content_length.py` 的端到端用例确认。注意普通 `curl` 按 `Content-Length` 收完就退出，不能单独用来证明“不会多等 5 秒 EOF”；后者必须看测试里的原始 socket 计时。

2. 搜索与 meta tab 隔离：

```sh
curl -sS --unix-socket /var/run/trim_music.socket \
  'http://localhost/music/api/v1/search/track?q=周杰伦&page=1&size=50'
curl -sS --unix-socket /var/run/trim_music.socket \
  'http://localhost/music/api/v1/search/album?q=周杰伦&page=1&size=50'
curl -sS --unix-socket /var/run/trim_music.socket \
  'http://localhost/music/api/v1/search/artist?q=周杰伦&page=1&size=50'
curl -sS --unix-socket /var/run/trim_music.socket \
  'http://localhost/music/api/v1/search/playlist?q=周杰伦&page=1&size=50'
```

   看 `data.list` 的实体类型是否各自正确，并对照日志中的 `[SEARCH]` / `[SEARCH_ALBUM]` / `[SEARCH_ARTIST]` / `[SEARCH_PLAYLIST]`。并发复现最好直接用 `tests/test_search_debounce.py` 的 t02/t03，因为真实服务不一定能稳定重现竞态。

3. 防抖与陈旧请求：

   在设置页把搜索防抖保持默认 `0.8`，连续快速输入 `周` → `周杰` → `周杰伦`，观察日志只应出现最终词的完整上游合并；把 `FNMUSIC_SEARCH_DEBOUNCE_S` 设为 `0` 后重复同一操作，应看到每个词都发请求。确定性的时间推进与 negative control 以 `tests/test_search_debounce.py` 为准。

4. 本地优先与 marker：

   - 完整播放一首 `online:kugou:*`，确认落盘目录出现音频，且 `$SAVE_DIR/.fnmusic_save_index/` 下生成对应 `.saved` marker。
   - 再次播放同一 guid，日志应出现 `[LOCAL-FIRST] hit`，且不应再向 KuGouMusicApi 请求 `/song/url`。
   - 删除本地音频但保留 marker，或把 `FNMUSIC_LOCAL_FIRST` 设为 `0`，再次播放应回落 CDN；测试 T26-T28 已覆盖。

### 3.4 本次实际运行结果（2026-11-22）

以下为本次实际输出，不是推测：

- `tests/test_search_debounce.py`：通过 11 / 失败 0。
- `tests/test_bridge_content_length.py`：`PASS=17 FAIL=0`。
- `tests/test_env_empty.py`：`PASS=36  FAIL=0`。
- `tests/test_marker_dir.py`：通过 6 / 失败 0。
- `tests/test_local_first.py`：通过 49 / 失败 0。
- `tests/test_save.py`：通过 117 / 失败 0。
- `tests/test_pipeline.py`：通过 37 / 失败 6；失败项为“同名 .lrc”“同名封面图”“title/artist/album 标签”和“第二次播放读的是本地归档文件”。这与 `AGENTS.md` 中“`test_pipeline` 只有第二次播放 e2e 失败”的基线描述不一致；本次未定位是环境依赖还是历史遗留，TODO：正式交付前单独核查并在完整 25 脚本口径下重新报数。

本次没有复跑全部 25 个脚本，因此不能据此声称“22 通过 / 3 失败”的基线仍然成立；如果用于交付，必须按 `AGENTS.md` 的命令跑全量并如实记录。
