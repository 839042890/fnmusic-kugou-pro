"""1.0.8 搜索卡顿修复的验收测试。

覆盖工单 hermes id=1795077539-1 的四组验收 + 四条阴性对照。

验收：
  a01  连打三字（周→周杰→周杰伦）→ 酷狗 1 / 官方 1（防抖合并）
  a02  0 结果 10s 内走缓存、11s 后重发（空结果短 TTL）
  a03  完整结果 299s 命中、301s 重发（完整结果长 TTL）
  a04a 翻页 page=2 不等防抖（elapsed < debounce）
  a04b 同 keyword 不同 page 不串 Future（gate key 含 page/size）
  a05  旧请求慢返回不覆盖新结果（stale 丢弃）

阴性对照（每条：改坏 → 观测回归 → 还原）：
  n01  debounce=0 → 三字连打请求数 = 3
  n02  _search_stale 恒 False → 旧结果覆盖新结果（A 返回 5 条）
  n03  _search_ttl 一刀切 300 → 空结果 11s 不重发（kugou 仍 1 次）
  n04  并发闸旁路 → 同 keyword 双并发官方请求 = 2

报数口径：酷狗请求数 = fetch_kugou_search 调用次数；官方请求数 =
upstream_client.send 调用次数。测试里 kugou_search_limit=50 / step=50，
每次搜索恰好 1 页（1 次酷狗请求），两者数值可直接比较。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PROXY = BASE / "app" / "proxy"
APP_PY = PROXY / "app.py"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-sdb-"))
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
import app           # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, str(e) or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------- 全局计数与夹具
KUGOU_CALLS: list[tuple[str, int]] = []   # (keyword, page)
OFFICIAL_CALLS: list[str] = []            # 官方请求 path

_ORIG_CLOCK = app._SEARCH_CLOCK
_ORIG_STALE = app._search_stale
_ORIG_TTL = app._search_ttl
_ORIG_FETCH_KUGOU = app.fetch_kugou_search


def _song_items(n: int) -> list[dict]:
    return [{"id": f"kugou:{i:032X}", "name": f"歌{i}", "artist": "星痕"} for i in range(n)]


def _official_envelope(items: list | None = None, total: int | None = None) -> dict:
    lst = items or []
    return {"code": 0, "data": {"list": lst, "total": total if total is not None else len(lst)}}


def _install_official(payload: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        OFFICIAL_CALLS.append(request.url.path)
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})
    app.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix")


def _install_kugou(fn) -> None:
    async def wrapper(keyword, limit, page=1):
        KUGOU_CALLS.append((keyword, page))
        result = fn(keyword, page)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    app.fetch_kugou_search = wrapper


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test")


def reset() -> None:
    KUGOU_CALLS.clear()
    OFFICIAL_CALLS.clear()
    app._SEARCH_CACHE.clear()
    app._META_CACHE.clear()
    app._SEARCH_INFLIGHT.clear()
    app._DEBOUNCE_STATE.clear()
    app._SEARCH_CLOCK = _ORIG_CLOCK
    app._search_stale = _ORIG_STALE
    app._search_ttl = _ORIG_TTL
    app.fetch_kugou_search = _ORIG_FETCH_KUGOU


def _set_search_conf(debounce: float) -> None:
    app.CONF["search_debounce_s"] = debounce
    app.CONF["kugou_search_limit"] = 50
    app.CONF["kugou_step"] = 50


def _search(keyword: str, page: int = 1, size: int = 50) -> str:
    return f"/music/api/v1/search/track?q={keyword}&page={page}&size={size}"


# ---------------------------------------------------------------- 验收
def a01():
    _set_search_conf(0.5)
    reset()
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": _song_items(3), "total": 3})

    async def go():
        async with _client() as c:
            r1, r2, r3 = await asyncio.gather(
                c.get(_search("周")), c.get(_search("周杰")), c.get(_search("周杰伦")),
            )
            return r1, r2, r3
    r1, r2, r3 = asyncio.run(go())
    assert len(KUGOU_CALLS) == 1, f"酷狗应只发 1 次，实际 {len(KUGOU_CALLS)}: {KUGOU_CALLS}"
    assert len(OFFICIAL_CALLS) == 1, f"官方应只发 1 次，实际 {len(OFFICIAL_CALLS)}: {OFFICIAL_CALLS}"
    assert KUGOU_CALLS[0][0] == "周杰伦", f"最终应发最后一个词，实际 {KUGOU_CALLS[0]}"
    assert len(r3.json()["data"]["list"]) == 3, "最后一个词应返回 3 条"
    assert len(r1.json()["data"]["list"]) == 0 and len(r2.json()["data"]["list"]) == 0, \
        "前两个词应被判定 stale 返回空信封"


def a02():
    _set_search_conf(0.0)
    reset()
    clock = {"t": 1000.0}
    app._SEARCH_CLOCK = lambda: clock["t"]
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": [], "total": 0})

    async def go():
        async with _client() as c:
            await c.get(_search("空词"))
            clock["t"] += 9
            await c.get(_search("空词"))
            clock["t"] += 2  # 累计 11s
            await c.get(_search("空词"))
    asyncio.run(go())
    assert len(KUGOU_CALLS) == 2, \
        f"空结果 10s 内走缓存、11s 重发，酷狗应发 2 次，实际 {len(KUGOU_CALLS)}: {KUGOU_CALLS}"


def a03():
    _set_search_conf(0.0)
    reset()
    clock = {"t": 1000.0}
    app._SEARCH_CLOCK = lambda: clock["t"]
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": _song_items(50), "total": 50})  # 50>=cap → complete

    async def go():
        async with _client() as c:
            await c.get(_search("周杰伦"))
            clock["t"] += 299
            await c.get(_search("周杰伦"))
            clock["t"] += 2  # 累计 301s
            await c.get(_search("周杰伦"))
    asyncio.run(go())
    assert len(KUGOU_CALLS) == 2, \
        f"完整结果 299s 命中、301s 重发，酷狗应发 2 次，实际 {len(KUGOU_CALLS)}: {KUGOU_CALLS}"


def a04a():
    _set_search_conf(0.5)
    reset()
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": [], "total": 0})

    async def go():
        async with _client() as c:
            t0 = time.perf_counter()
            await c.get(_search("周杰伦", page=1))
            t1 = time.perf_counter()
            await c.get(_search("周杰伦", page=2))
            t2 = time.perf_counter()
            return t1 - t0, t2 - t1
    d1, d2 = asyncio.run(go())
    assert d1 >= 0.4, f"page=1 首查应等防抖（>=0.4s），实际 {d1:.3f}s"
    assert d2 < 0.4, f"翻页 page=2 不应等防抖（<0.4s），实际 {d2:.3f}s"


def a04b():
    _set_search_conf(0.0)
    reset()
    official = _official_envelope([{"guid": f"official-{i}"} for i in range(100)], total=100)
    _install_official(official)
    # 注意：酷狗必须返回条目，否则 merge_online_tracks 的「无酷狗结果直接
    # 返回官方原文」分支会让官方 100 条原样返回（不切片），page=1/page=2 会
    # 相同。带上酷狗条目后走切片路径，才能验证「翻页不串 Future」。
    _install_kugou(lambda kw, page: {"items": _song_items(5), "total": 5})

    async def go():
        async with _client() as c:
            r1, r2 = await asyncio.gather(
                c.get(_search("周杰伦", page=1, size=50)),
                c.get(_search("周杰伦", page=2, size=50)),
            )
            return r1, r2
    r1, r2 = asyncio.run(go())
    assert len(OFFICIAL_CALLS) == 2, \
        f"同 keyword 不同 page 应各发官方请求（gate key 含 page），实际 {len(OFFICIAL_CALLS)}"
    l1 = r1.json()["data"]["list"]
    l2 = r2.json()["data"]["list"]
    assert [x["guid"] for x in l1] != [x["guid"] for x in l2], "page=1 与 page=2 切片应不同（不串 Future）"
    assert len(l1) == 50 and len(l2) == 50, f"各页应 50 条，实际 {len(l1)}/{len(l2)}"


def a04c():
    """并发闸去重：同 (scope, keyword, page, size) 双并发 → 官方只发 1 次。"""
    _set_search_conf(0.0)
    reset()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        OFFICIAL_CALLS.append(request.url.path)
        await asyncio.sleep(0.1)  # 让第一个请求卡住，第二个命中并发闸复用 Future
        return httpx.Response(200, json=_official_envelope(), headers={"content-type": "application/json"})
    app.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow_handler), base_url="http://unix")
    _install_kugou(lambda kw, page: {"items": _song_items(3), "total": 3})

    async def go():
        async with _client() as c:
            r1, r2 = await asyncio.gather(
                c.get(_search("周杰伦")), c.get(_search("周杰伦")),
            )
            return r1, r2
    r1, r2 = asyncio.run(go())
    assert len(OFFICIAL_CALLS) == 1, \
        f"同参双并发应命中并发闸（官方只发 1 次），实际 {len(OFFICIAL_CALLS)}"
    assert r1.json()["data"]["list"] == r2.json()["data"]["list"], "复用 Future 应返回相同结果"


def a05():
    _set_search_conf(0.0)
    reset()
    _install_official(_official_envelope())
    release_a: asyncio.Event | None = None

    async def kugou_fn(kw, limit, page=1):
        KUGOU_CALLS.append((kw, page))
        if kw == "周杰" and release_a is not None:
            await release_a.wait()  # 旧请求慢返回
        return {"items": _song_items(5), "total": 5}
    app.fetch_kugou_search = kugou_fn

    async def go():
        nonlocal release_a
        release_a = asyncio.Event()
        async with _client() as c:
            ta = asyncio.create_task(c.get(_search("周杰")))
            await asyncio.sleep(0.02)              # 让旧请求先卡在酷狗
            rb = await c.get(_search("周杰伦"))     # 新请求先完成
            release_a.set()                        # 放行旧请求
            ra = await ta
            return ra, rb
    ra, rb = asyncio.run(go())
    assert len(ra.json()["data"]["list"]) == 0, \
        f"旧请求应被 stale 丢弃（返回空信封），实际 {len(ra.json()['data']['list'])} 条"
    assert len(rb.json()["data"]["list"]) == 5, "新请求应正常返回 5 条"
    assert "total" not in app._SEARCH_CACHE.get("周杰", {}), "旧请求不应把结果写进缓存"


# ---------------------------------------------------------------- 阴性对照
def n01():
    """debounce=0 → 三字连打请求数 = 3（证明防抖在合并）。"""
    _set_search_conf(0.0)
    reset()
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": _song_items(3), "total": 3})

    async def go():
        async with _client() as c:
            await asyncio.gather(
                c.get(_search("周")), c.get(_search("周杰")), c.get(_search("周杰伦")),
            )
    asyncio.run(go())
    print(f"  [阴性①] debounce=0 → 酷狗请求数={len(KUGOU_CALLS)} 官方请求数={len(OFFICIAL_CALLS)}")
    assert len(KUGOU_CALLS) == 3, f"debounce=0 应发 3 次酷狗，实际 {len(KUGOU_CALLS)}"
    assert len(OFFICIAL_CALLS) == 3, f"debounce=0 应发 3 次官方，实际 {len(OFFICIAL_CALLS)}"


def n02():
    """_search_stale 恒 False → 旧结果覆盖新结果（A 返回 5 条而非 0）。"""
    _set_search_conf(0.0)
    reset()
    _install_official(_official_envelope())
    release_a: asyncio.Event | None = None

    async def kugou_fn(kw, limit, page=1):
        if kw == "周杰" and release_a is not None:
            await release_a.wait()
        return {"items": _song_items(5), "total": 5}
    app.fetch_kugou_search = kugou_fn
    app._search_stale = lambda scope, gen: False   # 改坏：恒不 stale

    async def go():
        nonlocal release_a
        release_a = asyncio.Event()
        async with _client() as c:
            ta = asyncio.create_task(c.get(_search("周杰")))
            await asyncio.sleep(0.02)
            await c.get(_search("周杰伦"))
            release_a.set()
            ra = await ta
            return ra
    ra = asyncio.run(go())
    print(f"  [阴性②] _search_stale 恒 False → 旧请求返回 {len(ra.json()['data']['list'])} 条（覆盖新结果）")
    assert len(ra.json()["data"]["list"]) == 5, \
        f"stale 被旁路后旧请求应返回 5 条（bug 复现），实际 {len(ra.json()['data']['list'])}"
    assert "total" in app._SEARCH_CACHE.get("周杰", {}), "stale 被旁路后旧请求应写缓存"


def n03():
    """_search_ttl 一刀切 300 → 空结果 11s 不重发（kugou 仍 1 次）。"""
    _set_search_conf(0.0)
    reset()
    clock = {"t": 1000.0}
    app._SEARCH_CLOCK = lambda: clock["t"]
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": [], "total": 0})
    app._search_ttl = lambda entry: 300.0          # 改坏：一刀切 300

    async def go():
        async with _client() as c:
            await c.get(_search("空词"))
            clock["t"] += 11
            await c.get(_search("空词"))
    asyncio.run(go())
    print(f"  [阴性③] _search_ttl 一刀切 300 → 空结果 11s 后酷狗仍只发 {len(KUGOU_CALLS)} 次（不重发）")
    assert len(KUGOU_CALLS) == 1, f"一刀切 300 时 11s 不应重发（bug 复现），实际 {len(KUGOU_CALLS)}"


def n04():
    """并发闸旁路 → 同 keyword 双并发官方请求 = 2。"""
    _set_search_conf(0.0)
    reset()
    _install_official(_official_envelope())
    _install_kugou(lambda kw, page: {"items": [], "total": 0})
    class _NoGate(dict):
        """get 恒 miss、set 不生效：真正旁路并发闸（否则 search_track 内写入
        的 Future 仍会被后续同 key 请求 get 到，闸就还在起作用）。"""
        def get(self, key, default=None):
            return None
        def __setitem__(self, key, value):
            pass
    app._SEARCH_INFLIGHT = _NoGate()                # 改坏：旁路并发闸

    async def go():
        async with _client() as c:
            await asyncio.gather(
                c.get(_search("周杰伦")), c.get(_search("周杰伦")),
            )
    asyncio.run(go())
    print(f"  [阴性④] 并发闸旁路 → 同 keyword 双并发官方请求数={len(OFFICIAL_CALLS)}")
    assert len(OFFICIAL_CALLS) == 2, f"旁路闸后双并发应发 2 次官方，实际 {len(OFFICIAL_CALLS)}"


# ---------------------------------------------------------------- 运行
print("\n[1] 验收（应全绿）")
case("a01 防抖合并三字 → 1/1", a01)
case("a02 空结果 10s/11s 分级 TTL", a02)
case("a03 完整结果 299s/301s", a03)
case("a04a 翻页 page=2 不等防抖", a04a)
case("a04b gate key 含 page/size（不串 Future）", a04b)
case("a04c 并发闸去重同参双并发 → 1 次官方", a04c)
case("a05 旧请求 stale 丢弃（不覆盖新结果）", a05)

print("\n[2] 阴性对照（应复现 bug 行为）")
n01()
n02()
n03()
n04()

print("\n[3] 汇总")
print(f"  通过 {len(PASS)} / 失败 {len(FAIL)}")
for name in PASS:
    print(f"    ✅ {name}")
for name, err in FAIL:
    print(f"    ❌ {name}: {err}")

sys.exit(1 if FAIL else 0)
