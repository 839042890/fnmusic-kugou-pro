#!/usr/bin/env python3
"""P-08 设置保存改 POST + P-07 前端「登录已失效」分支。

P-08 背景：settings.html 的三处保存原本用 `jget('/_ext/settings?save=1&...')`
把写操作塞进 GET。后端早就同时提供了 POST，前端却没改。这里锁死三件事：
  1. 三处保存真的走 POST，且 qs 里不再带 save=1
  2. `post()` 对 JSON 响应（含 400 的 `{"status":"error","message":...}`）要能读，
     否则非法 kugou_url 的人话提示会退化成 "HTTP 400"
  3. 后端 `?save=1` 兼容分支**必须保留**（旧前端/书签还在用）—— 这是反向断言

P-07 前端：`/_ext/login/status` 新增 `token_invalid`，设置页要能显示
「登录已失效，请重新扫码」并把扫码按钮放出来，否则用户无法自救。

运行：python3 tests/test_settings_post.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
WEB = PKG / "app" / "web" / "settings.html"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-setpost-"))
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)
ENV_FILE = ROOT / "fnmusic_kugou_pro" / "etc" / ".env"

os.environ.update({
    "FNMUSIC_ENV_FILE": str(ENV_FILE),
    "FNMUSIC_KUGOU_ENABLED": "1",
    "FNMUSIC_KUGOU_URL": "http://127.0.0.1:9",
    "FNMUSIC_KUGOU_QUALITY": "high",
    "FNMUSIC_UPSTREAM_SOCK": str(ROOT / "no-upstream.socket"),
    "FNMUSIC_ETC_DIR": str(ROOT / "fnmusic_kugou_pro" / "etc"),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    "FNMUSIC_MUSIC_DB": str(ROOT / "absent.db"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})
sys.path.insert(0, str(PROXY))

import httpx        # noqa: E402
import app          # noqa: E402
import kugou_source  # noqa: E402

HTML = WEB.read_text(encoding="utf-8")
APP_PY = (PROXY / "app.py").read_text(encoding="utf-8")

PASS, FAIL = [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                             base_url="http://test")


# ---------------------------------------------------------------- 静态
def s01():
    n = HTML.count("post('/_ext/settings', qs)")
    assert n == 3, f"三处保存都应改 POST，实际 {n}"


def s02():
    for bad in ("jget('/_ext/settings?'", "'save=1','save_enabled", "'save=1','fav_dir", "'save=1','kugou_url"):
        assert bad not in HTML, f"残留旧写法: {bad}"


def s03():
    # post() 必须先看 content-type，JSON 响应直接交给调用方（哪怕是 400）
    i = HTML.find("async function post(")
    assert i != -1, "找不到 post() 定义"
    j = HTML.find("async function loadHealth", i)
    assert j > i, "切片终点无效（找不到下一个函数）"
    seg = HTML[i:j]
    assert len(seg) > 150, f"切片异常短: {len(seg)}"
    assert "indexOf('json')" in seg, "post() 缺 JSON 分支"


def s04():
    assert "token_invalid" in HTML, "前端没有 token_invalid 分支"
    assert "login-invalid-hint" in HTML, "缺少「登录已失效」提示元素"
    # 失效时必须把扫码按钮放出来
    seg = HTML[HTML.find("async function loadLogin"):]
    seg = seg[:seg.find("\n")]
    assert "bl.style.display='inline-block'" in seg, "token_invalid 分支没显示扫码按钮"


def s05():
    # 后端 GET ?save=1 兼容分支必须还在（网关前缀 + 直连前缀各一处）
    n = APP_PY.count('request.query_params.get("save") == "1"')
    assert n == 2, f"save=1 兼容分支应保留 2 处，实际 {n}"


def s06():
    seg = APP_PY[APP_PY.find("async def login_status():"):]
    seg = seg[:seg.find("@app.post")]
    assert "probe_login_state" in seg, "login_status 没有探活调用（退回只判 token 存在）"
    assert "token_invalid" in seg, "login_status 没有 token_invalid 返回值"


# ---------------------------------------------------------------- 运行时
def r01():
    async def go():
        async with _client() as c:
            r = await c.post("/app/fnmusic_kugou_pro/_ext/settings",
                             data={"kugou_quality": "320"})
            return r
    r = asyncio.run(go())
    assert r.status_code == 200, r.status_code
    assert r.json().get("status") == "ok", r.text
    text = ENV_FILE.read_text(encoding="utf-8")
    assert "FNMUSIC_KUGOU_QUALITY=320" in text, text


def r02():
    # 旧前端兼容：GET ?save=1 仍要生效
    async def go():
        async with _client() as c:
            return await c.get("/app/fnmusic_kugou_pro/_ext/settings?save=1&kugou_quality=128")
    r = asyncio.run(go())
    assert r.status_code == 200 and r.json().get("status") == "ok", r.text
    assert "FNMUSIC_KUGOU_QUALITY=128" in ENV_FILE.read_text(encoding="utf-8")


def r03():
    # 非法 URL → 400 + JSON 人话（前端 post() 必须能读出来）
    async def go():
        async with _client() as c:
            return await c.post("/app/fnmusic_kugou_pro/_ext/settings",
                                data={"kugou_url": "ftp://nope"})
    r = asyncio.run(go())
    assert r.status_code == 400, r.status_code
    assert "json" in (r.headers.get("content-type") or "").lower(), r.headers
    assert "http" in r.json().get("message", "").lower(), r.text


def r04():
    # 直连前缀的 POST 同样可用
    async def go():
        async with _client() as c:
            return await c.post("/_ext/settings", data={"kugou_enabled": "1"})
    r = asyncio.run(go())
    assert r.status_code == 200 and r.json().get("status") == "ok", r.text


case("S01 三处保存均走 POST", s01)
case("S02 不再用 GET 提交写操作", s02)
case("S03 post() 有 JSON 响应分支", s03)
case("S04 前端有 token_invalid 分支与提示", s04)
case("S05 后端 ?save=1 兼容分支仍在（反向断言）", s05)
case("S06 login_status 真调探活", s06)
case("R01 POST 保存生效并落 .env", r01)
case("R02 GET ?save=1 仍生效（旧前端兼容）", r02)
case("R03 非法 URL 返回 400 + JSON 人话", r03)
case("R04 直连前缀 POST 可用", r04)

print(f"test_settings_post: PASS={len(PASS)} FAIL={len(FAIL)}")
for name, err in FAIL:
    print(f"  ❌ {name}: {err}")
sys.exit(1 if FAIL else 0)
