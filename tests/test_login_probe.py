#!/usr/bin/env python3
"""P-07 登录态探活 + P-09 回包信封判定。

覆盖：
  A. envelope_state 判据（**先 error_code 后 status**）
     * ec=0 & st=1 → valid
     * ec in (20010,20017,20018) → invalid
     * **st=0 + ec=20017 → invalid**（历史写法会判成 valid，本文件最关键的用例）
     * st=0 + ec=0 / st=0 + ec=9999 → unknown（兜底分支，不许掉进 valid）
     * st=200 + ec=0 → valid（兼容旧信封）；{} → valid；非 dict → unknown
  B. probe_login_state 四态与缓存（600s / 30s / skipped / invalidate）
  C. /_ext/login/status 四态输出（token_invalid 只在 invalid 时出现）
  D. unknown 命中时日志里有 status / error_code 原值
  E. 静态：login_status 必须调探活（防回退成只判 bool(token)）

运行：python3 tests/test_login_probe.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-probe-"))
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

CALLS: list = []


class _Resp:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _Client:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        CALLS.append((a, kw))
        if self._exc:
            raise self._exc
        return self._resp


class _Clock:
    """替换 kugou_source.time，用来快进缓存 TTL。"""

    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t

    def time(self):
        return 1760000000.0 + self.t

    def advance(self, d):
        self.t += d


_ORIG_CLIENT = kugou_source._client
_ORIG_TIME = kugou_source.time


def with_probe(resp=None, exc=None):
    CALLS.clear()
    kugou_source._client = lambda: _Client(resp=resp, exc=exc)   # noqa: E731
    clk = _Clock()
    kugou_source.time = clk
    kugou_source.invalidate_login_probe()
    return clk


def restore():
    kugou_source._client = _ORIG_CLIENT
    kugou_source.time = _ORIG_TIME
    kugou_source.invalidate_login_probe()


def body(st=1, ec=0):
    return {"status": st, "error_code": ec, "errmsg": "", "data": []}


def run(coro):
    return asyncio.run(coro)


def _probe(payload=None, status_code=200, bad_json=False, exc=None):
    with_probe(resp=_Resp(status_code=status_code, payload=payload, bad_json=bad_json), exc=exc)
    try:
        return run(kugou_source.probe_login_state())
    finally:
        restore()


# ---------------------------------------------------------------- A. 判据

def t01():
    assert kugou_source.envelope_state(body(1, 0)) == "valid"


def t02():
    for ec in (20017, 20018, 20010):
        assert kugou_source.envelope_state(body(0, ec)) == "invalid", f"ec={ec}"


def t03():
    # 历史写法 body.get("status", body.get("error_code")) 会在这里判成 valid
    assert kugou_source.envelope_state(
        {"status": 0, "error_code": 20017, "errmsg": "need login"}) == "invalid"


def t04():
    assert kugou_source.envelope_state({"status": 0, "error_code": 0}) == "unknown"


def t05():
    assert kugou_source.envelope_state({"status": 0, "error_code": 9999}) == "unknown"


def t06():
    assert kugou_source.envelope_state({"status": 200, "error_code": 0}) == "valid"


def t07():
    assert kugou_source.envelope_state(["not", "a", "dict"]) == "unknown"


def t08():
    # 空回包 → unknown：别把网关错误页/空响应当成功
    assert kugou_source.envelope_state({}) == "unknown"


def t08b():
    # 兼容旧口径只认 st in (1,200)，键缺失不再放行
    assert kugou_source.envelope_state({"error_code": 0}) == "unknown"
    assert kugou_source.envelope_state({"status": None, "error_code": 0}) == "unknown"


case("T01 ec=0&st=1 → valid", t01)
case("T02 ec=20017/20018/20010 → invalid", t02)
case("T03 st=0 + ec=20017 → invalid（历史写法会判 valid）", t03)
case("T04 st=0 + ec=0 → unknown（兜底分支不许掉进 valid）", t04)
case("T05 st=0 + ec=9999 → unknown", t05)
case("T06 st=200 + ec=0 → valid（兼容旧信封）", t06)
case("T07 非 dict → unknown", t07)
case("T08 空信封 {} → unknown（空回包不许当成功）", t08)
case("T08b ec=0 但 status 键缺失/None → unknown（不再兼容 None）", t08b)


# ---------------------------------------------------------------- B. 探活

def t09():
    r = _probe(body(1, 0))
    assert r["state"] == "valid" and r["status"] == 1 and r["error_code"] == 0
    assert r["cached"] is False, r


def t10():
    r = _probe(body(0, 20017))
    assert r["state"] == "invalid" and r["error_code"] == 20017, r


def t11():
    assert _probe(body(0, 20010))["state"] == "invalid"


def t12():
    r = _probe({"status": 0, "error_code": 20017}, status_code=500)
    assert r["state"] == "unknown", r
    assert "http=500" in r["detail"], r


def t13():
    r = _probe(exc=TimeoutError("timeout"))
    assert r["state"] == "unknown", r
    assert "TimeoutError" in r["detail"], r


def t14():
    r = _probe(bad_json=True)
    assert r["state"] == "unknown" and r["detail"].startswith("bad"), r


def _cache_case(payload, advance, expect_cached):
    with_probe(resp=_Resp(payload=payload))
    clk = kugou_source.time
    try:
        run(kugou_source.probe_login_state())
        clk.advance(advance)
        second = run(kugou_source.probe_login_state())
        assert second["cached"] is expect_cached, f"cached={second['cached']}"
        want = 1 if expect_cached else 2
        assert len(CALLS) == want, f"上游请求数 {len(CALLS)} 期望 {want}"
    finally:
        restore()


def t15():
    _cache_case(body(1, 0), 599, True)


def t16():
    _cache_case(body(1, 0), 601, False)


def t17():
    _cache_case({"status": 0, "error_code": 9999}, 29, True)


def t18():
    _cache_case({"status": 0, "error_code": 9999}, 31, False)


def t19():
    with_probe(resp=_Resp(payload=body(0, 20017)))
    try:
        run(kugou_source.probe_login_state())
        assert len(CALLS) == 1, CALLS
        kugou_source.invalidate_login_probe()
        run(kugou_source.probe_login_state())
        assert len(CALLS) == 2, f"失效后应重新探活，实际 {len(CALLS)}"
    finally:
        restore()


def t20():
    with_probe(resp=_Resp(payload=body(1, 0)))
    old = kugou_source.CFG.get("kugou_enabled", True)
    kugou_source.CFG["kugou_enabled"] = False
    try:
        r = run(kugou_source.probe_login_state())
        assert r["state"] == "skipped", r
        assert not CALLS, f"禁用时不该请求上游，实际 {len(CALLS)} 次"
    finally:
        kugou_source.CFG["kugou_enabled"] = old
        restore()


case("T09 valid 回包 → state=valid", t09)
case("T10 invalid 回包 → invalid 且带回原始码", t10)
case("T11 20010 → invalid", t11)
case("T12 HTTP 500 → unknown（不许当失效）", t12)
case("T13 超时/连不上 → unknown", t13)
case("T14 JSON 坏 → unknown", t14)
case("T15 valid 缓存 599s 内命中（只打 1 次）", t15)
case("T16 valid 缓存 601s 后重新探活", t16)
case("T17 unknown 缓存 30s 内命中", t17)
case("T18 unknown 缓存 31s 后重新探活（短缓存）", t18)
case("T19 invalidate_login_probe() 后重新探活", t19)
case("T20 kugou_enabled=0 → skipped 且零请求", t20)


# ---------------------------------------------------------------- C. login_status

def _status(resp=None, exc=None, token="T" * 64, userid="1710799835"):
    old_tok, old_uid = app.CONF["kugou_token"], app.CONF["kugou_userid"]
    with_probe(resp=resp, exc=exc)
    app.CONF["kugou_token"], app.CONF["kugou_userid"] = token, userid
    try:
        r = run(app.login_status())
        return json.loads(r.body)
    finally:
        app.CONF["kugou_token"], app.CONF["kugou_userid"] = old_tok, old_uid
        restore()


def t21():
    d = _status(token="", userid="")
    assert d["status"] == "not_logged_in", d
    assert not CALLS, f"没 token 不该探活，实际 {len(CALLS)} 次"


def t22():
    d = _status(resp=_Resp(payload=body(1, 0)))
    assert d["status"] == "logged_in" and d["token_state"] == "valid", d


def t23():
    d = _status(resp=_Resp(payload=body(0, 20017)))
    assert d["status"] == "token_invalid", d
    assert d["message"] == "登录已失效，请重新扫码", d
    assert d["userid"] == "1710799835" and d["has_token"] is True, d
    assert d["probe_error_code"] == 20017, d


def t24():
    d = _status(resp=_Resp(payload=body(0, 9999)))
    assert d["status"] == "logged_in" and d["token_state"] == "unknown", d


def t25():
    d = _status(exc=TimeoutError("timeout"))
    assert d["status"] == "logged_in" and d["token_state"] == "unknown", d


case("T21 无 token → not_logged_in 且零请求", t21)
case("T22 valid → logged_in + token_state=valid", t22)
case("T23 invalid → token_invalid + 文案 + 保留 userid", t23)
case("T24 unknown → 仍是 logged_in（不得误报失效）", t24)
case("T25 探活抛异常 → logged_in + unknown", t25)


# ---------------------------------------------------------------- D. 日志

class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def t26():
    h = _Cap()
    lg = logging.getLogger("kugou_source")
    lv = lg.level
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        with_probe(resp=_Resp(payload={"status": 0, "error_code": 9999}))
        try:
            run(kugou_source.probe_login_state())
        finally:
            restore()
    finally:
        lg.removeHandler(h)
        lg.setLevel(lv)
    hit = [l for l in h.lines if "login_probe unknown" in l]
    assert hit, f"没有 unknown 日志：{h.lines}"
    line = hit[-1]
    assert "status=0" in line and "error_code=9999" in line, line


case("T26 unknown 日志含 status/error_code 原值", t26)


# ---------------------------------------------------------------- E. 静态

def t27():
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    assert "probe_login_state()" in src, "login_status 里找不到探活调用"
    assert "token_invalid" in src, "login_status 里没有 token_invalid 分支"


case("T27 login_status 必须走探活（防回退成 bool(token)）", t27)


# ---------------------------------------------------------------- 汇总

print(f"test_login_probe: PASS={len(PASS)} FAIL={len(FAIL)}")
for n, e in FAIL:
    print(f"  \u274c {n}: {e}")
sys.exit(1 if FAIL else 0)
