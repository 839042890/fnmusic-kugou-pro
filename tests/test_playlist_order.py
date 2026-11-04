#!/usr/bin/env python3
"""1.0.7 ②：「我的歌单」里真实歌单排最前，27 个虚拟歌单排在后面。

背景：F-01 加进来的三个 feed 入口（每日推荐 1 + 排行榜 6 + 精选 20 = 27 个虚拟歌单）
原本拼在用户真实歌单**前面**，把用户自己那 5 个歌单挤到了第二屏。

本文件锁四件事：
  1. 顺序：前 5 项是真实歌单（isKugouFeed 不为真），之后 27 项是虚拟歌单
  2. 数量：5 + 27 = 32，一个不多一个不少 —— 顺序变了，**内容集合不许变**
  3. 标识：guid 前缀（online:kugou:playlist: / online:kugou:feed:）、名称、trackCount 原样传递
  4. 边界：任一侧为空时另一侧仍正常（不因为调顺序把空列表搞成 None 崩掉）

运行：python3 tests/test_playlist_order.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
APP_PY = PROXY / "app.py"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-order-"))
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

os.environ.update({
    "FNMUSIC_ENV_FILE": str(ROOT / "fnmusic_kugou_pro" / "etc" / "absent.env"),
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

SRC = APP_PY.read_text(encoding="utf-8")

PASS, FAIL = [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


def _real(n: int = 5) -> list[dict]:
    return [
        {
            "guid": f"online:kugou:playlist:{1000 + i}",
            "name": f"我的歌单{i + 1}",
            "coverId": f"online:kugou:playlist:{1000 + i}",
            "trackCount": 10 + i,
            "source": "kugou",
        }
        for i in range(n)
    ]


def _feed(n: int = 27) -> list[dict]:
    names = ["每日推荐"] + [f"排行榜{i}" for i in range(1, 7)] + [f"精选歌单{i}" for i in range(1, 21)]
    assert len(names) == 27
    return [
        {
            "guid": f"online:kugou:feed:{'daily' if i == 0 else ('rank' if i <= 6 else 'special')}:{i}",
            "name": names[i],
            "coverId": f"online:kugou:feed:x:{i}",
            "trackCount": i,
            "source": "kugou",
            "isKugouFeed": True,
        }
        for i in range(n)
    ]


def _get_list(real: list[dict], feed: list[dict]) -> tuple[list[dict], dict]:
    """打一次真实的 /music/api/v1/playlist/list，返回 (list, 整个 body)。"""
    orig_bundles = app.fetch_kugou_user_playlist_bundles
    orig_feed = app.fetch_kugou_feed_playlists
    orig_env = app.fetch_upstream_envelope

    async def _bundles():
        return list(real)

    async def _feeds():
        return list(feed)

    async def _upstream(*a, **kw):
        return {"code": 0, "msg": "ok", "data": {"list": [], "total": 0}}

    app.fetch_kugou_user_playlist_bundles = _bundles
    app.fetch_kugou_feed_playlists = _feeds
    app.fetch_upstream_envelope = _upstream
    try:
        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app),
                    base_url="http://test") as c:
                return await c.get("/music/api/v1/playlist/list")
        r = asyncio.run(go())
    finally:
        app.fetch_kugou_user_playlist_bundles = orig_bundles
        app.fetch_kugou_feed_playlists = orig_feed
        app.fetch_upstream_envelope = orig_env
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("code") == 0, body
    data = body.get("data") or {}
    lst = data.get("list") or []
    assert isinstance(lst, list) and lst, f"列表为空: {body}"
    return lst, body


# ------------------------------------------------------------------ 静态
def s01():
    """源码顺序断言（按行号切片，且先证明切片非空 —— 防白测）。"""
    i = SRC.find("feed_playlists = await fetch_kugou_feed_playlists()")
    assert i != -1, "找不到 feed 注入点"
    j = SRC.find("now_ts = int(time.time())", i)
    assert j > i, "切片终点无效"
    seg = SRC[i:j]
    assert len(seg) > 100, f"切片异常短: {len(seg)}"
    assert "list(kugou_playlists or []) + feed_playlists" in seg, "顺序没改成「真实在前」"
    assert "feed_playlists + list(kugou_playlists or [])" not in seg, "旧顺序还在"


def s02():
    """注释也要跟着改 —— 注释写着"排在用户歌单之前"会误导下一个人。"""
    i = SRC.find("（虚拟歌单，排在用户歌单")
    assert i != -1, "找不到 F-01 列表注入处的注释"
    seg = SRC[i:i + 200]
    assert seg.count("之后") >= 1, "注释没改成「之后」"
    assert "排在用户歌单之前" not in seg, "旧注释残留"


# ------------------------------------------------------------------ 运行时
def r01():
    lst, body = _get_list(_real(), _feed())
    assert len(lst) == 32, f"总数应为 32，实际 {len(lst)}"


def r02():
    lst, _ = _get_list(_real(), _feed())
    head = lst[:5]
    assert not any(str(x.get("guid", "")).startswith("online:kugou:feed:") for x in head), \
        f"前 5 个里有虚拟歌单: {[x.get('name') for x in head]}"
    assert [x.get("name") for x in head] == [f"我的歌单{i + 1}" for i in range(5)], \
        f"真实歌单顺序被改动: {[x.get('name') for x in head]}"


def r03():
    lst, _ = _get_list(_real(), _feed())
    tail = lst[5:]
    assert len(tail) == 27, f"虚拟歌单应为 27 个，实际 {len(tail)}"
    # 注意：列表里的 feed 项**不带** isKugouFeed（injected 映射只挑 6 个字段，
    # 1.0.6 起就是这样；flag 只在 detail/batch-detail 侧存在）。所以这里用 guid
    # 前缀判身份 —— 想按 flag 判的话得先补字段，那是另一件事，不在本单。
    bad = [x.get("name") for x in tail
           if not str(x.get("guid", "")).startswith("online:kugou:feed:")]
    assert not bad, f"后 27 个里有非虚拟项: {bad}"


def r04():
    """guid 前缀不许动（前端按前缀分派解析器，改了这批歌单全打不开）。"""
    lst, _ = _get_list(_real(), _feed())
    for x in lst[:5]:
        assert str(x.get("guid", "")).startswith("online:kugou:playlist:"), x
    for x in lst[5:]:
        assert str(x.get("guid", "")).startswith("online:kugou:feed:"), x


def r05():
    """名称与 trackCount 原样传递（顺序只调位置，不许改内容）。"""
    real, feed = _real(), _feed()
    lst, _ = _get_list(real, feed)
    assert [x.get("name") for x in lst] == [x["name"] for x in real + feed], "名称集合/顺序不符"
    assert [int(x.get("trackCount") or 0) for x in lst] == \
        [x["trackCount"] for x in real + feed], "trackCount 被改动"


def r06():
    lst, body = _get_list(_real(), _feed())
    assert (body.get("data") or {}).get("total") == len(lst) == 32, body.get("data", {}).get("total")


def r07():
    """只有虚拟歌单时（用户一个真实歌单都没有）：27 个照常，不崩。"""
    lst, _ = _get_list([], _feed())
    assert len(lst) == 27 and all(str(x.get("guid", "")).startswith("online:kugou:feed:")
                                  for x in lst), len(lst)


def r08():
    """只有真实歌单时（feed 全挂）：5 个照常返回，顺序不变。"""
    lst, _ = _get_list(_real(), [])
    assert len(lst) == 5 and lst[0].get("name") == "我的歌单1", [x.get("name") for x in lst]


case("S01 源码改成 真实在前 + 虚拟在后", s01)
case("S02 注释同步（不再写「之前」）", s02)
case("R01 总数 5+27=32", r01)
case("R02 前 5 个是真实歌单且顺序不变", r02)
case("R03 后 27 个是虚拟歌单", r03)
case("R04 guid 前缀未改动", r04)
case("R05 名称/trackCount 原样", r05)
case("R06 total 与 list 长度一致", r06)
case("R07 只有虚拟歌单时不崩", r07)
case("R08 只有真实歌单时不崩", r08)

print(f"test_playlist_order: PASS={len(PASS)} FAIL={len(FAIL)}")
for name, err in FAIL:
    print(f"  ❌ {name}: {err}")
sys.exit(1 if FAIL else 0)
