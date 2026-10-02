#!/usr/bin/env python3
"""2.1.7 回归：transcode 401 的取证日志（P0-2）。

背景（真实日志，2026-09-30 装机实例）：

    2026-09-30 18:35:59  POST http://unix/music/api/v1/track/transcode  401
    2026-09-30 22:04:34  POST http://unix/music/api/v1/track/transcode  401
    （22:04 那次之后日志里再无任何请求）

现象：飞牛【音乐】偶尔发 `POST /music/api/v1/track/transcode`，扩展按
「GET 全接管、非 GET 全透传」转给上游，上游回 401，播放没了下文。

定不了案的原因：旧日志只有一行状态码，看不到
    ① 请求体长什么样（App 发的是在线 guid 还是本地 guid？）
    ② 请求头带了哪些鉴权字段
    ③ 上游 401 的正文（`error_code` 才是真因；酷狗上游把业务错误也返回成 5xx）

2.1.7 加了 `forward_to_upstream_diag()`：转发语义**逐字节不变**，只多打日志。

本文件要守住的四件事（缺一条这个补丁就白做或者做坏）：
  A. 观测**不得改变**转发语义 —— 上游收到的 method/path/query/body 与旧版一致，
     客户端收到的 status/body 与上游返回的一致。用 MockTransport 直接对账。
  B. 诊断信息必须**真的进日志** —— 请求体字段、鉴权头长度、上游错误体。
  C. 凭据**绝不进日志** —— cookie / authorization / x-trim-music-temp-token
     只记长度不记值（proxy.log 是 0644，写进去就是泄漏）。
  D. GET 行为**不受影响** —— 在线 guid 的 GET 仍由本地应答，不发上游。

阴性对照（证明测的是行为不是签名）：把 `forward_to_upstream_diag` 的日志行
删掉 / 让 transcode 路由退回 `forward_to_upstream`，A/B/C/D 里立刻有断言失败。

运行：env -u PYTHONPATH /tmp/tv3/bin/python tests/test_transcode_diag.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
APP_PY = PROXY / "app.py"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-transcode-"))
LIB = ROOT / "music_lib"
LIB.mkdir(parents=True, exist_ok=True)

DB = ROOT / "music.db"
_con = sqlite3.connect(DB)
_con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
_con.execute("INSERT INTO shared_library (path) VALUES (?)", (str(LIB),))
_con.commit()
_con.close()

os.environ.update({
    "FNMUSIC_MUSIC_DB": str(DB),
    "FNMUSIC_LIBRARY_DIR": "",
    "FNMUSIC_SAVE_ENABLED": "0",
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "0",
    # 持久目录指到临时目录：本机 /var/apps/fnmusic_kugou_pro/{etc,var,home}
    # 符号链接真实存在，不设就会去写系统的 /vol1/@appconf/...（测试污染 + 假红）。
    "FNMUSIC_ETC_DIR": str(ROOT / "fnmusic_kugou_pro" / "etc"),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROXY))
import app  # noqa: E402

import httpx  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record.getMessage())
        except Exception:
            pass

    def clear(self) -> None:
        self.records.clear()

    def joined(self) -> str:
        return "\n".join(self.records)


_cap = LogCapture()
_log = logging.getLogger("fnmusic_proxy")
_log.addHandler(_cap)
_log.setLevel(logging.DEBUG)

ONLINE_GUID = "online:kugou:" + "A" * 32
LOCAL_GUID = "5F0C2E6B1D9A4C7E8B3F2A6D0C9E1B47"
# 上游 401 时酷狗上游会带 error_code（这就是要抓的东西）
UPSTREAM_401 = json.dumps(
    {"status": 0, "error_code": 20010, "error_msg": "need login", "data": None}
).encode()
TOKEN_VALUE = "b" * 64          # 假装是酷狗 token（真实长度 64）
UPSTREAM_CALLS: list[dict] = []


def _make_upstream(status: int = 401, body: bytes = UPSTREAM_401):
    """假上游：记下收到的请求，按指定状态/正文回应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        UPSTREAM_CALLS.append({
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query.decode() if request.url.query else "",
            "body": request.content,
            "headers": dict(request.headers),
        })
        return httpx.Response(
            status,
            content=body,
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


def _client(status: int = 401, body: bytes = UPSTREAM_401):
    """给 app 装上假上游，返回面向客户端的 ASGI client。

    注意 base_url 必须是 "http://unix"：真实代码里
    `get_upstream_client()` 就是这么建的，路由传的是相对路径
    （`/music/api/v1/track/transcode`）。少了这个 base_url，
    httpx 会因为"相对 URL"直接抛 ValueError —— 那是测试环境的错，
    不是被测代码的错。
    """
    app.app.state.upstream_client = httpx.AsyncClient(
        transport=_make_upstream(status, body), base_url="http://unix",
    )
    transport = httpx.ASGITransport(app=app.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ===========================================================================
print("\n[1] 静态：观测确实接上了，且没有偷偷改 GET 接管策略")
SRC = APP_PY.read_text(encoding="utf-8")
STRIPPED = "\n".join(ln for ln in SRC.splitlines() if not ln.lstrip().startswith("#"))

check("定义了 forward_to_upstream_diag()", "async def forward_to_upstream_diag(" in SRC)
check("forward_to_upstream() 仍在（其余路由照旧用它）", "async def forward_to_upstream(" in SRC)

# 两个 transcode 路由的非 GET 分支必须走 diag 版。
# 切片按**行号**取，不要用 str.find('async def _online_info') 当结束标记 ——
# `_online_info` 定义在 transcode 路由**之前**，那样切出来是空串（白测一场）。
_src_lines = SRC.splitlines()
_i0 = next(n for n, ln in enumerate(_src_lines) if ln.startswith("async def track_transcode_session"))
_i1 = next(n for n, ln in enumerate(_src_lines) if ln.startswith("async def _online_info") and n > _i0)
_tr_block = "\n".join(_src_lines[_i0:_i1])
check("切片取到了 transcode 路由正文（防白测）",
      "track_transcode_session" in _tr_block and "_is_get_request" in _tr_block,
      f"{len(_tr_block)} 字节")
check("transcode 路由的非 GET 分支走 diag 版",
      _tr_block.count("forward_to_upstream_diag(") == 2,
      f"命中 {_tr_block.count('forward_to_upstream_diag(')} 次")
check("transcode-session 有独立 tag", '"transcode-session"' in _tr_block)
check("transcode 有独立 tag", '"transcode"' in _tr_block)
# 非 GET 分支不得再直接调 forward_to_upstream（否则 diag 被绕过）
_bad = re.findall(r"if not _is_get_request\(request\):\s*\n\s*return await forward_to_upstream\(", _tr_block)
check("transcode 非 GET 分支未绕过 diag", not _bad, str(_bad))

check("_diag_auth_summary 只取长度", "len(v)" in SRC and "_diag_auth_summary" in SRC)
# 凭据键必须只用来算长度，不得被格式化进日志
check("凭据键不会被打成值", not re.search(r"logger\.\w+\([^)]*\{[^}]*\b(cookie|authorization)\b[^}]*\}", SRC, re.I))

# 不新增配置键（MEMORY 教训：新键要同时改 CONF/.env 写入/启动回读/文档，漏一处即失效）。
# 只查**新增的开关名**，别查 "FNMUSIC_DIAG" 这个纯字面量 —— 取证段的注释里
# 解释"为什么不加这个开关"时会提到它，那样会误报。
check("未新增诊断开关（诊断固定开启，不留半开的开关）",
      not re.search(r"FNMUSIC_DIAG[A-Z_]*\s*\"\s*\)", SRC)
      and not re.search(r"def _env_\w+\([^)]*FNMUSIC_DIAG", SRC)
      and not re.search(r"_env_\w+\(\s*\"FNMUSIC_DIAG", SRC),
      "出现了真正的 FNMUSIC_DIAG* 读取")
check("诊断段未把 body 原样格式化进日志",
      not re.search(r"logger\.\w+\([^)]*\bbody\b[^)]*%\s*raw\b", SRC)
      and "head={" not in SRC)

# ===========================================================================
print("\n[2] 动态：POST transcode（在线 guid + 带 cookie）—— 语义不变 + 有日志")


async def t_post_online():
    UPSTREAM_CALLS.clear()
    _cap.clear()
    async with _client() as cli:
        r = await cli.post(
            "/music/api/v1/track/transcode",
            json={"guid": ONLINE_GUID},
            headers={"cookie": f"token={TOKEN_VALUE}", "x-trim-music-temp-token": "t" * 16},
        )
    log = _cap.joined()
    print(f"    客户端看到: {r.status_code} {r.content[:80]!r}")
    print(f"    上游收到  : {[(c['method'], c['path']) for c in UPSTREAM_CALLS]}")
    for ln in log.splitlines():
        if "[DIAG]" in ln:
            print(f"    日志      : {ln[:190]}")

    # --- A. 转发语义 ---
    check("A1 请求确已转发到上游（1 次）", len(UPSTREAM_CALLS) == 1, str(len(UPSTREAM_CALLS)))
    if UPSTREAM_CALLS:
        c = UPSTREAM_CALLS[0]
        check("A2 上游收到的 method 是 POST", c["method"] == "POST", c["method"])
        check("A3 上游收到的 path 原样", c["path"] == "/music/api/v1/track/transcode", c["path"])
        check("A4 上游收到的 body 逐字节一致",
              c["body"] == json.dumps({"guid": ONLINE_GUID}, separators=(",", ":")).encode()
              or json.loads(c["body"]) == {"guid": ONLINE_GUID},
              repr(c["body"][:80]))
        check("A5 cookie 透传给上游（GET 分发逻辑没被改坏）",
              f"token={TOKEN_VALUE}" in c["headers"].get("cookie", ""))
    check("A6 客户端状态码 = 上游状态码", r.status_code == 401, str(r.status_code))
    check("A7 客户端正文 = 上游正文（错误体没被吞/改写）", r.content == UPSTREAM_401, repr(r.content[:80]))
    check("A8 客户端 content-type 沿用上游", "application/json" in r.headers.get("content-type", ""))

    # --- B. 诊断信息进日志 ---
    check("B1 有 [DIAG] 行", "[DIAG]" in log)
    check("B2 tag 标明是 transcode 路由", "[DIAG] transcode POST" in log, log[-300:])
    check("B3 记录了请求体字段（keys=guid）", "json keys=['guid']" in log, log[-400:])
    check("B4 判出这是在线的 guid", "is_online=yes" in log, log[-400:])
    check("B5 记录了鉴权头长度", f"cookie:{len(f'token={TOKEN_VALUE}')}" in log, log[-400:])
    check("B6 记录了上游状态码", "upstream_status=401" in log, log[-400:])
    check("B7 记录了上游错误体的 error_code（定案靠它）", "20010" in log, log[-400:])

    # --- C. 凭据不进日志 ---
    check("C1 token 值未落日志", TOKEN_VALUE not in log)
    check("C2 半个 token 也未落日志", TOKEN_VALUE[:32] not in log)
    check("C3 临时令牌值未落日志", "t" * 16 not in log)


asyncio.run(t_post_online())

# ===========================================================================
print("\n[3] 动态：POST transcode（本地 guid）—— 仍照旧透传，且日志判为非在线")


async def t_post_local():
    UPSTREAM_CALLS.clear()
    _cap.clear()
    async with _client(status=502, body=b'{"error_code":152}') as cli:
        r = await cli.post("/music/api/v1/track/transcode", json={"guid": LOCAL_GUID})
    log = _cap.joined()
    print(f"    客户端看到: {r.status_code} {r.content!r}")
    check("上游收到 1 次（本地 guid 的 POST 也透传，语义未变）",
          len(UPSTREAM_CALLS) == 1 and UPSTREAM_CALLS[0]["method"] == "POST", str(len(UPSTREAM_CALLS)))
    check("日志判为非在线 guid", "is_online=no" in log, log[-400:])
    check("透传上游的非 200 状态（502）", r.status_code == 502, str(r.status_code))
    check("上游错误体原样回给客户端", r.content == b'{"error_code":152}', repr(r.content))


asyncio.run(t_post_local())

# ===========================================================================
print("\n[4] 动态：2xx 路径不许被缓冲/改写（音频类大响应只走流式）")


async def t_ok_passthrough():
    UPSTREAM_CALLS.clear()
    _cap.clear()
    payload = b'{"code":0,"msg":"ok"}'
    async with _client(status=200, body=payload) as cli:
        r = await cli.post("/music/api/v1/track/transcode", json={"guid": ONLINE_GUID})
    log = _cap.joined()
    check("2xx 正文原样", r.content == payload, repr(r.content))
    check("2xx 也记了 upstream_status", "upstream_status=200" in log, log[-300:])
    check("2xx 不预读正文（错误体读取只发生在 4xx/5xx）", "body=" not in log.split("upstream_status")[-1])


asyncio.run(t_ok_passthrough())

# ===========================================================================
print("\n[5] 动态：GET transcode（在线 guid）仍由本地应答 —— 没被 diag 带去上游")


async def t_get_online_still_local():
    UPSTREAM_CALLS.clear()
    _cap.clear()
    async with _client() as cli:
        r = await cli.get("/music/api/v1/track/transcode", params={"guid": ONLINE_GUID})
    if r.status_code >= 400:
        print(f"    GET 返回 {r.status_code}: {r.content[:120]!r}")
    check("GET 在线 guid 未发上游（本地接管策略未变）", not UPSTREAM_CALLS, str(len(UPSTREAM_CALLS)))
    check("GET 未触发 diag 日志（只有非 GET 才取证）", "[DIAG]" not in _cap.joined())


asyncio.run(t_get_online_still_local())

# ===========================================================================
print("\n[6] 单元：_diag_body_summary / _diag_auth_summary 的行为")
_d = app._diag_body_summary

desc, guid = _d(json.dumps({"guid": ONLINE_GUID}).encode())
check("JSON 体：识别 keys", desc.startswith("json keys=['guid']"), desc)
check("JSON 体：取出 guid", guid == ONLINE_GUID, guid)

desc, guid = _d(b"guid=" + ONLINE_GUID.encode() + b"&x=1")
check("表单体：识别为 form", desc.startswith("form keys="), desc)
check("表单体：取出 guid", guid == ONLINE_GUID, guid)

desc, guid = _d(b"")
check("空体：判为 empty 且无 guid", desc == "empty" and guid == "", f"{desc}/{guid}")

desc, guid = _d(b"\x00\x01\x02binary-garbage")
check("二进制垃圾不抛异常且无 guid", guid == "" and desc.startswith("other"), desc)

desc, guid = _d(json.dumps({"trackGUID": ONLINE_GUID}).encode())
check("备用字段 trackGUID 也认", guid == ONLINE_GUID, guid)
desc, guid = _d(json.dumps({"coverId": "cover-1"}).encode())
check("备用字段 coverId 也认", guid == "cover-1", guid)

# ===========================================================================
print("\n[7] 静态：诊断代码不引入 shell/写文件等副作用")
# 切出**有界**的诊断段：从 _DIAG_GUID_KEYS 定义到 forward_to_upstream_diag 之后的
# 下一个 top-level 定义。不这么切的话，"诊断段里不许 open(" 会因为整份 app.py
# 遍布 open(（落盘/缓存/标记都各有一处）而恒假 —— 那是白测。
_diag_slice = SRC[SRC.find("_DIAG_GUID_KEYS = (") : SRC.find("async def fetch_upstream_envelope(")]
check("诊断段切片非空（防白测）", len(_diag_slice) > 500, f"{len(_diag_slice)} 字节")
check("诊断段无 subprocess", "subprocess" not in _diag_slice)
check("诊断段无 open( 写文件", "open(" not in _diag_slice)
check("诊断段无 os.system / eval / exec",
      not any(t in _diag_slice for t in ("os.system", "eval(", "exec(")))
check("诊断段确实含 diag 函数体（切片没串味）",
      "forward_to_upstream_diag" in _diag_slice and "_diag_body_summary" in _diag_slice)

# ===========================================================================
print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print(f"临时目录: {ROOT}")
sys.exit(1 if FAIL else 0)
