#!/usr/bin/env python3
"""P-09：6 处「status/error_code 混读」判据收紧后的行为护栏。

背景（hermes 2026-11-04 实测）：
  正常回包 = {"status":1,"error_code":0}
  失败回包 = {"status":0,"error_code":20017/20010/20018}
旧写法 ``body.get("status", body.get("error_code"))`` 里 status 键恒存在 →
error_code 永远读不到 → ``status=0`` 被判成成功，失败回包一路走到"解析 data"，
表现为**静默空列表**（用户看不出为什么空）。

本文件锁三件事：
  1. 失败回包在这 6 处一律走"空壳"分支（形状不变、不抛异常、不透错误态给前端）
  2. 日志里能看到 status/error_code 原值（这是 P-09 唯一可见收益）
  3. 成功回包 / 旧信封（status=200）照旧有数据 —— 防"收紧过头"

运行：python3 tests/test_envelope_p09.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-p09-"))
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

os.environ.update({
    "FNMUSIC_ENV_FILE": str(ROOT / "absent.env"),
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

PASS, FAIL = [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------- 夹具
# 形态来自 hermes 2026-11-04 在 127.0.0.1:4000 的实测回包
VALID = {"status": 1, "error_code": 0,
         "data": {"lists": [{"albumid": 1, "albumname": "A"}], "total": 1,
                  "songs": [{"hash": "H1", "name": "S1", "timelen": 200000}],
                  "list": [{"albumid": 1, "albumname": "A"}]},
         "total": 1}
INVALID = {"status": 0, "error_code": 20017, "data": None}   # 凭证不可用
NO_PARAM = {"status": 0, "error_code": 20010, "data": None}  # 缺参数
LEGACY_OK = {"status": 200, "error_code": 0,
             "data": {"lists": [{"albumid": 2, "albumname": "B"}], "total": 1}}

LOGS = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)[:200]

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        return _Resp(self._payload)


def with_payload(payload, coro_fn):
    orig = kugou_source._client
    kugou_source._client = lambda: _Client(payload)
    try:
        return asyncio.run(coro_fn())
    finally:
        kugou_source._client = orig


# ---------------------------------------------------------------- 6 处
def e01():
    r = with_payload(INVALID, lambda: kugou_source.search_albums_raw("周杰伦"))
    assert r["lists"] == [] and r["total"] == 0, r


def e02():
    r = with_payload(INVALID, lambda: kugou_source.get_album_songs("123"))
    assert r["items"] == [] and r["total"] == 0, r


def e03():
    r = with_payload(INVALID, lambda: kugou_source.get_album_detail("123"))
    assert r == {}, r


def e04():
    r = with_payload(INVALID, lambda: kugou_source.get_artist_albums("6539"))
    assert r["items"] == [] and r["total"] == 0, r


def e05():
    r = with_payload(INVALID, lambda: app.fetch_kugou_artist_search(None, "周杰伦"))
    assert r["code"] == 0 and r["data"]["list"] == [], r


def e06():
    r = with_payload(INVALID, lambda: app.fetch_kugou_playlist_search(None, "周杰伦"))
    assert r["code"] == 0 and r["data"]["list"] == [], r


def e07():
    # 20010（缺参数）同样判失败
    r = with_payload(NO_PARAM, lambda: kugou_source.search_albums_raw("x"))
    assert r["lists"] == [], r


def e08():
    # 唯一可见收益：日志能看出「为什么空」
    LOGS.clear()
    with_payload(INVALID, lambda: kugou_source.search_albums_raw("周杰伦"))
    joined = "\n".join(LOGS)
    assert "status=0" in joined and "error_code=20017" in joined, joined[-500:]


def e09():
    # 防过度收紧：正常信封照旧出数据
    r = with_payload(VALID, lambda: kugou_source.search_albums_raw("周杰伦"))
    assert len(r["lists"]) == 1 and r["total"] == 1, r


def e10():
    # 防过度收紧：旧口径 status=200 + error_code=0 仍算成功
    r = with_payload(LEGACY_OK, lambda: kugou_source.search_albums_raw("周杰伦"))
    assert len(r["lists"]) == 1, r


def e11():
    """端到端：酷狗侧失败时，/music/api/v1/search/artist 必须仍是 HTTP 200 + 空列表。

    官方上游在测试环境里不可达，这里把 _upstream_search_envelope 打成 None
    （= 官方侧失败），正好走「用酷狗结果兜底」那条路。
    """
    orig_c = kugou_source._client
    orig_u = app._upstream_search_envelope
    kugou_source._client = lambda: _Client(INVALID)

    async def _no_upstream(*a, **kw):
        return None

    app._upstream_search_envelope = _no_upstream
    try:
        async def inner():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app),
                    base_url="http://test") as c:
                return await c.get("/music/api/v1/search/artist?q=%E5%91%A8&page=1&size=24")
        r = asyncio.run(inner())
    finally:
        kugou_source._client = orig_c
        app._upstream_search_envelope = orig_u
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("code") == 0, body
    assert body.get("data", {}).get("list") == [], body


_handlers_installed = False


def _install_handler():
    global _handlers_installed
    if not _handlers_installed:
        logging.getLogger("kugou_source").addHandler(_Capture())
        _handlers_installed = True


_install_handler()

case("E01 search_albums_raw 失败回包 → 空 lists", e01)
case("E02 get_album_songs 失败回包 → 空 items", e02)
case("E03 get_album_detail 失败回包 → {}", e03)
case("E04 get_artist_albums 失败回包 → 空 items", e04)
case("E05 fetch_kugou_artist_search → code 0 + 空 list", e05)
case("E06 fetch_kugou_playlist_search → code 0 + 空 list", e06)
case("E07 error_code=20010 同样判失败", e07)
case("E08 日志含 status/error_code 原值", e08)
case("E09 正常信封照旧出数据", e09)
case("E10 旧信封 status=200 仍算成功", e10)
case("E11 端到端 /search/artist 仍 200 + 空列表（不是 500）", e11)

print(f"test_envelope_p09: PASS={len(PASS)} FAIL={len(FAIL)}")
for name, err in FAIL:
    print(f"  ❌ {name}: {err}")
sys.exit(1 if FAIL else 0)
