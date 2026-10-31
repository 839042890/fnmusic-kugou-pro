#!/usr/bin/env python3
"""F-01 歌单类内容源（每日推荐 / 排行榜 / 精选歌单）+ 时长的单位约定。

覆盖：
  A. 单位（最容易回归的一条）
     * /playlist/track/all 的 timelen 是**毫秒** → track_item_to_raw 原样透出
     * 每日推荐的 time_length 是**秒** → daily_item_to_raw 必须 ×1000
     * 排行榜 audio_info.duration_* 是**毫秒**
     * 三源输出都落在 100000–1200000（1.7–20 分钟）
     * 阴性对照：把任一处当"秒"处理，区间断言必须变红
  B. 三个 adapter 的字段映射（三源形状完全不同，不能共用）
  C. 按配置音质选 hash_128/320/flac（不分档就会永远只放 128k）
  D. feed guid 编解码；且不得被 playlist 的"按 : 切第一段"解析污染
  E. 列表注入 / detail / batch-detail / track-list 四个入口
  F. 失败隔离：任一组上游挂掉，其余组照常
  G. 开关：kugou_feed=0 → 回到原行为（零额外条目）
  H. 静态：feed 取数链**没有**逐首调 /song/url 或 /privilege/lite 做预检
  I. size=-1 全量翻页不重复、不死循环

运行：python3 tests/test_feed_sources.py
（需要装了 httpx 的解释器，例如 app 自带的 .venv-proxy）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-feed-"))
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

os.environ.update({
    # 真机 /vol1/@appconf/<app>/.env 存在 → 导入期会覆盖设置，必须指到不存在的文件
    "FNMUSIC_ENV_FILE": str(ROOT / "absent.env"),
    "FNMUSIC_KUGOU_ENABLED": "1",
    "FNMUSIC_FEED": "1",
    "FNMUSIC_KUGOU_URL": "http://127.0.0.1:9",   # 测试里禁止真连酷狗
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


def acase(name, coro):
    case(name, lambda: asyncio.run(coro))


async def _acase(name, fn):
    try:
        await fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------- 真实样本
# 以下四段是 2026-10-31 从 127.0.0.1:4000 实采的回包片段（字段名/单位均为实测值）。

SPECIAL_ITEM = {          # /playlist/track/all  data.songs[]
    "hash": "21D6FDD4A0B93C6E5A8DB0C5D0B0E1F2",
    "name": "张芸京 - 偏爱",
    "singerinfo": [{"name": "张芸京", "id": "0"}],
    "albuminfo": {"name": "偏爱", "id": "1234567"},
    "timelen": 212898,     # 毫秒 = 3:32.898
    "extname": "mp3",
    "size": 3406990,
    "bitrate": 320,
    "cover": "http://imge.kugou.com/stdmusic/{size}/20200101/abc.jpg",
    "privilege": 0,
    "feetype": 0,
    "info": {"timelen": None},
}

DAILY_ITEM = {            # /everyday/recommend  data.song_list[]
    "songname": "闹够了没有",
    "ori_audio_name": "闹够了没有",
    "author_name": "赖伟锋",
    "album_name": "男朋女友",
    "album_id": "971482",
    "album_audio_id": "32141918",
    "time_length": 238,           # **秒** = 3:58
    "timelength_320": 238,        # 也是秒
    "hash": "6A203E11C11A6B2A1A77E47CB359E9FB",
    "hash_128": "6A203E11C11A6B2A1A77E47CB359E9FB",
    "hash_320": "24D1E742369A2DFDD16BC309187869F3",
    "hash_flac": "CDC735D02864C27F218698DB0FF82E92",
    "filesize_128": 3820347,
    "filesize_320": 9550567,
    "filesize_flac": 25786023,
    "sizable_cover": "http://imge.kugou.com/stdmusic/{size}/20250207/x.jpg",
    "bitrate": 128,
    "extname": "mp3",
    "fail_process": 4,
}

RANK_ITEM = {             # /rank/audio  data.songlist[]
    "songname": "海阔天空",
    "author_name": "BEYOND",
    "album_id": 973001,
    "album_audio_id": 32155307,
    "audio_info": {
        "hash_128": "C41E80A18D1448FA47086372999C7F43",
        "hash_320": "0D04734E75820BEAE8AA202E758931DB",
        "hash_flac": "EF79AF82F05AA5242AB2AAA22CA7DE78",
        "duration_128": 324858,
        "duration_320": 324884,
        "duration_flac": 324825,      # 毫秒
        "filesize_flac": 32879211,
        "extname": "mp3",
    },
    "album_info": {
        "album_name": "乐与怒",
        "sizable_cover": "http://imge.kugou.com/stdmusic/{size}/20240428/y.jpg",
    },
    "trans_param": {"union_cover": "http://imge.kugou.com/stdmusic/{size}/20240428/y.jpg"},
    "privilege_download": {"privilege": 10, "privilege_flac": 10},
}

DUR_MIN, DUR_MAX = 100_000, 1_200_000


def _with_quality(q, fn):
    old = kugou_source.CFG.get("kugou_quality")
    kugou_source.CFG["kugou_quality"] = q
    try:
        return fn()
    finally:
        kugou_source.CFG["kugou_quality"] = old


# ---------------------------------------------------------------- A. 单位
def t01():
    raw = kugou_source.track_item_to_raw(SPECIAL_ITEM)
    assert raw, "精选 adapter 返回空"
    assert raw["duration_s"] == 212898, f"timelen 必须是毫秒原值，实际 {raw['duration_s']}"


def t02():
    raw = kugou_source.daily_item_to_raw(DAILY_ITEM)
    assert raw, "每日推荐 adapter 返回空"
    assert raw["duration_s"] == 238000, \
        f"time_length 是秒，必须 ×1000=238000，实际 {raw['duration_s']}"


def t03():
    raw = kugou_source.rank_item_to_raw(RANK_ITEM)
    assert raw, "排行榜 adapter 返回空"
    assert raw["duration_s"] == 324825, f"duration_flac 是毫秒，实际 {raw['duration_s']}"


def t04():
    got = {
        "special": kugou_source.track_item_to_raw(SPECIAL_ITEM)["duration_s"],
        "daily": kugou_source.daily_item_to_raw(DAILY_ITEM)["duration_s"],
        "rank": kugou_source.rank_item_to_raw(RANK_ITEM)["duration_s"],
    }
    bad = {k: v for k, v in got.items() if not (DUR_MIN <= v <= DUR_MAX)}
    assert not bad, f"duration 超出 100000–1200000 毫秒区间：{bad}"


def t05():
    """阴性对照：把毫秒当秒（÷1000）或把秒当毫秒，区间断言必须变红。"""
    wrong_ms_as_s = 212898 / 1000          # 有人"修"成除 1000 的样子
    wrong_s_as_ms = 238                    # 有人漏了 ×1000
    assert not (DUR_MIN <= wrong_ms_as_s <= DUR_MAX), "区间断言抓不到 ÷1000 的回归"
    assert not (DUR_MIN <= wrong_s_as_ms <= DUR_MAX), "区间断言抓不到漏 ×1000 的回归"


# ---------------------------------------------------------------- B. 字段映射
def t06():
    raw = kugou_source.daily_item_to_raw(DAILY_ITEM)
    assert raw["title"] == "闹够了没有", raw["title"]
    assert raw["artist"] == "赖伟锋", raw["artist"]
    assert raw["album"] == "男朋女友", raw["album"]
    assert raw["album_id"] == "971482", raw["album_id"]
    assert raw["file_size"] == 25786023, raw["file_size"]
    assert "{size}" in raw["cover_url"], "封面要保留 {size} 占位符给 _fill_cover_size"
    assert raw["id"] == "kugou:CDC735D02864C27F218698DB0FF82E92", raw["id"]


def t07():
    raw = kugou_source.rank_item_to_raw(RANK_ITEM)
    assert raw["title"] == "海阔天空", raw["title"]
    assert raw["artist"] == "BEYOND", raw["artist"]
    assert raw["album"] == "乐与怒", raw["album"]
    assert raw["album_id"] == "973001", raw["album_id"]
    assert "{size}" in raw["cover_url"], raw["cover_url"]
    assert raw["ext"] == "flac", raw["ext"]


def t08():
    raw = kugou_source.track_item_to_raw(SPECIAL_ITEM)
    assert raw["title"] == "偏爱", f"要剥掉 '歌手 - ' 前缀，实际 {raw['title']}"
    assert raw["artist"] == "张芸京"
    assert raw["album"] == "偏爱"
    assert raw["ext"] == "mp3", raw["ext"]


def t09():
    """坏输入不许抛：三源 adapter 对 None / {} / 缺字段都要返回 {}。"""
    for fn in (kugou_source.daily_item_to_raw, kugou_source.rank_item_to_raw,
               kugou_source.track_item_to_raw):
        for junk in (None, {}, {"foo": 1}, {"hash_flac": ""}, []):
            out = fn(junk) if not isinstance(junk, list) else fn(junk)
            assert out == {}, f"{fn.__name__}({junk!r}) 应返回空 dict，实际 {out!r}"


# ---------------------------------------------------------------- C. 音质档位
def t10():
    for q, expect in (("high", "flac"), ("flac", "flac"), ("320", "320"), ("128", "128")):
        raw = _with_quality(q, lambda: kugou_source.daily_item_to_raw(DAILY_ITEM))
        assert f"hash_{expect}" in raw["id"].upper().replace("KUGOU:", "") or raw, "空"
        want = {"flac": "CDC735D02864C27F218698DB0FF82E92",
                "320": "24D1E742369A2DFDD16BC309187869F3",
                "128": "6A203E11C11A6B2A1A77E47CB359E9FB"}[expect]
        assert raw["hash"] == want, f"quality={q} 应取 hash_{expect}，实际 {raw['hash']}"


def t11():
    """配置了不存在的档位 → 回落到可用档，不许出空 hash。"""
    raw = _with_quality("ultra-hires", lambda: kugou_source.daily_item_to_raw(DAILY_ITEM))
    assert raw and raw["hash"], "未知音质必须回落而不是返回空"


def t12():
    """仅 128 可用（flac/320 为空）时也要能出声。"""
    it = dict(DAILY_ITEM, hash_flac="", hash_320="")
    raw = kugou_source.daily_item_to_raw(it)
    assert raw["hash"] == DAILY_ITEM["hash_128"], raw["hash"]
    assert raw["ext"] == "mp3"


# ---------------------------------------------------------------- D. guid
def t13():
    g = app.kugou_feed_guid("rank", "84235")
    assert g == "online:kugou:feed:rank:84235", g
    assert app.is_kugou_feed_guid(g)
    assert app.parse_kugou_feed_guid(g) == ("rank", "84235")
    d = app.kugou_feed_guid("daily")
    assert d == "online:kugou:feed:daily", d
    assert app.parse_kugou_feed_guid(d) == ("daily", "")
    s = app.kugou_feed_guid("special", "collection_3_2270230357_254_0")
    assert app.parse_kugou_feed_guid(s) == ("special", "collection_3_2270230357_254_0"), s


def t14():
    """坑#5 的回归护栏：feed guid 不能被 playlist 那套解析吃掉。"""
    g = app.kugou_feed_guid("rank", "84235")
    assert not app.is_kugou_playlist_guid(g), "feed guid 不许命中 playlist 前缀"
    assert app.kugou_playlist_id_from_guid(g) == "", "feed guid 若走 playlist 解析会被切成空/截断"


def t15():
    for bad in ("", None, "online:kugou:feed:", "online:kugou:feed:bogus:x",
                "online:kugou:feed:rank", "online:kugou:playlist:abc"):
        assert app.parse_kugou_feed_guid(bad) == ("", ""), f"{bad!r} 应解析失败"


# ---------------------------------------------------------------- E. 入口
class _Stub:
    """替换 kugou_source 的取数函数，记录调用次数，绝不联网。"""

    def __init__(self, ranks=None, specials=None, tracks=None, fail_rank=False):
        self.ranks = ranks if ranks is not None else [
            {"rankid": "84235", "name": "名品堂", "cover": "http://c/{size}/a.jpg"},
            {"rankid": "33165", "name": "粤语金曲榜", "cover": "http://c/{size}/b.jpg"},
        ]
        self.specials = specials if specials is not None else [
            {"ident": f"collection_3_{i}_1_0", "name": f"精选{i}", "cover": "http://c/{size}/s.jpg"}
            for i in range(25)
        ]
        self.tracks = tracks if tracks is not None else {"items": [], "total": 0}
        self.fail_rank = fail_rank
        self.calls = {"rank_top": 0, "top_playlist": 0, "rank_audio": 0, "collection": 0}
        self._saved = {}

    def __enter__(self):
        for name, val in (("fetch_rank_top", self._rank_top),
                          ("fetch_top_playlists", self._top_playlist),
                          ("fetch_rank_audio", self._rank_audio),
                          ("fetch_collection_tracks", self._collection),
                          ("fetch_daily_recommend", self._daily)):
            self._saved[name] = getattr(kugou_source, name)
            setattr(kugou_source, name, val)
        return self

    def __exit__(self, *exc):
        for name, val in self._saved.items():
            setattr(kugou_source, name, val)
        return False

    async def _rank_top(self):
        self.calls["rank_top"] += 1
        if self.fail_rank:
            raise RuntimeError("boom")
        return list(self.ranks)

    async def _top_playlist(self, page=1, size=40):
        self.calls["top_playlist"] += 1
        return {"items": list(self.specials), "page": page, "pagesize": size, "has_next": True}

    async def _rank_audio(self, rankid, page=1, size=100):
        self.calls["rank_audio"] += 1
        return dict(self.tracks, page=page, pagesize=size)

    async def _collection(self, gcid, page=1, size=100):
        self.calls["collection"] += 1
        return dict(self.tracks, page=page, pagesize=size)

    async def _daily(self, page=1, size=30):
        return {"items": [kugou_source.daily_item_to_raw(DAILY_ITEM)],
                "total": 1, "creation_date": "20261031"}


async def t16():
    with _Stub() as st:
        out = await app.fetch_kugou_feed_playlists()
    kinds = [app.parse_kugou_feed_guid(p["guid"])[0] for p in out]
    assert kinds.count("daily") == 1, kinds
    assert kinds.count("rank") == 2, kinds
    assert kinds.count("special") == app._FEED_SPECIAL_LIMIT, \
        f"精选要截到 {app._FEED_SPECIAL_LIMIT} 条，实际 {kinds.count('special')}"
    assert len({p["guid"] for p in out}) == len(out), "guid 有重复"
    assert st.calls["rank_top"] == 1 and st.calls["top_playlist"] == 1
    names = [p["name"] for p in out]
    assert "每日推荐" in names and any(n.startswith("排行榜 · ") for n in names), names


async def t17():
    """失败隔离：rank/top 挂掉，每日推荐与精选仍要在。"""
    with _Stub(fail_rank=True) as st:
        out = await app.fetch_kugou_feed_playlists()
    kinds = [app.parse_kugou_feed_guid(p["guid"])[0] for p in out]
    assert kinds.count("daily") == 1, "rank/top 失败不该把每日推荐弄没"
    assert kinds.count("special") == app._FEED_SPECIAL_LIMIT
    assert kinds.count("rank") == 0


async def t18():
    """开关：kugou_feed=0 → 零条目、零上游请求（阴性对照）。"""
    old = app.CONF.get("kugou_feed")
    app.CONF["kugou_feed"] = False
    try:
        with _Stub() as st:
            out = await app.fetch_kugou_feed_playlists()
    finally:
        app.CONF["kugou_feed"] = old
    assert out == [], f"关掉后不许再注入任何虚拟歌单，实际 {len(out)} 条"
    assert st.calls["rank_top"] == 0 and st.calls["top_playlist"] == 0, "关掉后不许打上游"


async def t19():
    """kugou_enabled=0 同样不该注入（与既有总开关语义一致）。"""
    old = app.CONF.get("kugou_enabled")
    app.CONF["kugou_enabled"] = False
    try:
        out = await app.fetch_kugou_feed_playlists()
    finally:
        app.CONF["kugou_enabled"] = old
    assert out == [], out


def t20():
    """detail 对象：名字优先取列表期缓存的真名，取不到才回落 ident。"""
    g = app.kugou_feed_guid("rank", "84235")
    obj = app.kugou_feed_playlist_obj(g)
    assert obj and obj["guid"] == g
    assert obj["name"], "名字不能空"
    assert app.kugou_feed_playlist_obj("online:kugou:feed:bogus:x") is None


# ---------------------------------------------------------------- F. 出歌
async def t21():
    """track-list 的取数与 build_online_track 的结果（guid/duration/codec）。"""
    tracks = {"items": [kugou_source.rank_item_to_raw(RANK_ITEM)], "total": 1}
    with _Stub(tracks=tracks):
        payload = await app.fetch_kugou_feed_tracks(
            app.kugou_feed_guid("rank", "84235"), page=1, size=50)
    assert payload["total"] == 1 and len(payload["items"]) == 1
    online = app.build_online_track(payload["items"][0])
    assert online["guid"] == "online:kugou:EF79AF82F05AA5242AB2AAA22CA7DE78", online["guid"]
    assert online["duration"] == 324825, online["duration"]
    assert online["audioSpec"]["duration"] == 324825
    assert online["audioSpec"]["codec"] == "flac", online["audioSpec"]


async def t22():
    """size=-1 全量：两页拼齐，且不多翻一次。"""
    page_calls = []

    async def fake(guid, page=1, size=50):
        page_calls.append(page)
        if page == 1:
            return {"items": [{"id": f"kugou:{(i):032X}"} for i in range(100)], "total": 150}
        return {"items": [{"id": f"kugou:{(i):032X}"} for i in range(100, 150)], "total": 150}

    g = app.kugou_feed_guid("rank", "84235")
    saved = app.fetch_kugou_feed_tracks
    app.fetch_kugou_feed_tracks = fake
    try:
        got = await app.fetch_kugou_feed_tracks_full(g)
    finally:
        app.fetch_kugou_feed_tracks = saved
    assert len(got["items"]) == 150, len(got["items"])
    assert got["total"] == 150
    assert page_calls == [1, 2], f"多翻或漏翻：{page_calls}"


async def t23():
    """daily 全量不分页（接口本来就 30 首）。"""
    with _Stub():
        got = await app.fetch_kugou_feed_tracks_full(app.kugou_feed_guid("daily"))
    assert got["total"] == 1 and got["items"], got


async def t31():
    """精选歌单总数必须取 data.count。

    上游 /playlist/track/all 把总数放在 data.count（实测 126），没有 data.total。
    只读 total 会让每页的 total 退化成"本页条数"，连带 size=-1 的全量循环
    在第一页就以为拉完（126 首只出 100 首）—— 真机已复现。
    """
    songs = [{"hash": f"{i:032X}", "name": f"song{i}", "timelen": 200000} for i in range(5)]

    def body(extra):
        data = {"songs": songs}
        data.update(extra)
        return {"status": 1, "data": data}

    saved = kugou_source._feed_get
    try:
        async def fake_count(path, params, tag):
            return body({"count": 126})

        kugou_source._feed_get = fake_count
        got = await kugou_source.fetch_collection_tracks("collection_3_1_1_0", page=2, size=5)
        assert got["total"] == 126, f"没读 data.count：{got.get('total')}"
        assert len(got["items"]) == 5, len(got["items"])

        # 阴性对照：没有 count 时仍要认 total
        async def fake_total(path, params, tag):
            return body({"total": 77})

        kugou_source._feed_get = fake_total
        got = await kugou_source.fetch_collection_tracks("collection_3_1_1_0", page=1, size=5)
        assert got["total"] == 77, got.get("total")

        # 两个都没有：退化成本页条数（不抛、不为 0）
        async def fake_none(path, params, tag):
            return body({})

        kugou_source._feed_get = fake_none
        got = await kugou_source.fetch_collection_tracks("collection_3_1_1_0", page=1, size=5)
        assert got["total"] == 5, got.get("total")
    finally:
        kugou_source._feed_get = saved


# ------------------------------------------- H. F-03 歌单映射 × feed 隔离
def _pm_write(cfg: dict) -> None:
    Path(app.playlist_map_path()).write_text(json.dumps(cfg), encoding="utf-8")
    app._PM_CACHE.update({"data": None, "mtime": None, "checked": 0})


def _pm_reset() -> None:
    try:
        Path(app.playlist_map_path()).unlink()
    except FileNotFoundError:
        pass
    app._PM_CACHE.update({"data": None, "mtime": None, "checked": 0})


def _feed_bundles() -> list[dict]:
    """1 个每日推荐 + 6 个排行榜 + 20 个精选 = 27 条（与真机注入数一致）。"""
    out = [{"guid": app.kugou_feed_guid("daily"), "name": "每日推荐"}]
    out += [{"guid": app.kugou_feed_guid("rank", r), "name": f"排行榜 · {r}"}
            for r in ("84235", "85432", "33165", "59896", "33163", "35811")]
    out += [{"guid": app.kugou_feed_guid("special", f"collection_3_{i}_1_0"), "name": f"精选{i}"}
            for i in range(20)]
    return out


def _playlist_bundles() -> list[dict]:
    return [{"guid": f"online:kugou:playlist:collection_3_{i}_9_0", "name": f"歌单{i}"}
            for i in (1, 2, 3)]


def _pm_cfg(mode: str, ids: list[str]) -> dict:
    rule = {"mode": mode, "ids": ids}
    return {"users": {"tester": dict(rule)}, "default": dict(rule)}


async def t32():
    """三种模式 × feed 存活矩阵：all / whitelist / blacklist 都必须 27/27。

    缺陷 1：filter_playlist_bundles 拿 kugou_playlist_id_from_guid 取 pid，
    feed guid 拿到空串 → whitelist 下必然全删。修法是 feed 直接放行、不进判定。
    """
    feeds = _feed_bundles()
    want = [b["guid"] for b in feeds]
    got = {}
    try:
        for mode in ("all", "whitelist", "blacklist"):
            _pm_write(_pm_cfg(mode, ["99999999"]))
            kept = app.filter_playlist_bundles(list(feeds), "tester", "tester")
            got[mode] = len(kept)
            assert [b["guid"] for b in kept] == want, \
                f"{mode}: 存活 {len(kept)}/{len(feeds)}（期望 {len(feeds)}，且顺序不变）"
    finally:
        _pm_reset()
    assert got == {"all": 27, "whitelist": 27, "blacklist": 27}, got


async def t33():
    """同一配置下用户歌单照旧受白/黑名单约束（防"修 feed 顺手把 F-03 弄废"）。"""
    feeds = _feed_bundles()
    pls = _playlist_bundles()
    feed_guids = [b["guid"] for b in feeds]
    keep_pid = "collection_3_2_9_0"
    try:
        # 白名单：只留命中那 1 条
        _pm_write(_pm_cfg("whitelist", [keep_pid]))
        kept = app.filter_playlist_bundles(feeds + pls, "tester", "tester")
        assert [b["guid"] for b in kept] == feed_guids + [f"online:kugou:playlist:{keep_pid}"], \
            "白名单没有正确过滤用户歌单"

        # 黑名单：删掉命中那 1 条
        _pm_write(_pm_cfg("blacklist", [keep_pid]))
        kept = app.filter_playlist_bundles(feeds + pls, "tester", "tester")
        assert [b["guid"] for b in kept] == feed_guids + [
            "online:kugou:playlist:collection_3_1_9_0",
            "online:kugou:playlist:collection_3_3_9_0",
        ], "黑名单没有正确过滤用户歌单"

        # all：一条都不动
        _pm_write(_pm_cfg("all", [keep_pid]))
        kept = app.filter_playlist_bundles(feeds + pls, "tester", "tester")
        assert len(kept) == len(feeds) + len(pls), len(kept)
    finally:
        _pm_reset()


async def t34():
    """顺序固化：whitelist 生效时，另外三个入口的 feed 也不许被拦。

    这三处都是"feed 分支在 F-03 判断之前"，一旦有人把分支挪后就会拦掉整组。
    """
    try:
        _pm_write(_pm_cfg("whitelist", ["99999999"]))
        g = app.kugou_feed_guid("daily")
        async with await _client() as c:
            r = await c.get("/music/api/v1/playlist/detail", params={"guid": g})
            b = r.json()
            assert b["code"] == 0 and b["data"].get("guid") == g, f"detail 被拦：{b}"
            r = await c.get("/music/api/v1/playlist/batch-detail", params={"guids": g})
            b = r.json()
            assert [x["guid"] for x in b["data"]["list"]] == [g], f"batch-detail 被拦：{b}"

        tracks = {"items": [kugou_source.rank_item_to_raw(RANK_ITEM)], "total": 1}
        with _Stub(tracks=tracks):
            async with await _client() as c:
                r = await c.get("/music/api/v1/track/playlist-detail/list",
                                params={"playlistGUID": app.kugou_feed_guid("rank", "84235"),
                                        "size": "-1"})
        b = r.json()
        assert b["data"]["list"], f"track-list 被拦：{b}"
    finally:
        _pm_reset()


async def t35():
    """日志可读性：drop= 只放 pid（不含 guid/空串），并带 feed= 与 total=。"""
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs: list[str] = []

        def emit(self, rec):
            self.msgs.append(rec.getMessage())

    cap = _Cap()
    root = logging.getLogger()
    root.addHandler(cap)
    try:
        # 正常情形：3 条用户歌单，白名单只留 1 条 → filtered 2/3 ... feed=27 total=30
        _pm_write(_pm_cfg("whitelist", ["collection_3_2_9_0"]))
        app.filter_playlist_bundles(_feed_bundles() + _playlist_bundles(), "tester", "tester")
        line = next((m for m in cap.msgs if "[PLAYLIST_MAP] filtered" in m), "")
        assert line, f"没打过滤日志：{cap.msgs[-3:]}"
        assert "filtered 2/3 " in line, f"分母没排除 feed：{line}"
        assert "feed=27" in line and "total=30" in line, f"缺 feed/total 字段：{line}"
        drop_seg = line.split("drop=", 1)[1].split(" feed=", 1)[0]
        assert drop_seg == "collection_3_1_9_0,collection_3_3_9_0", f"drop 不是纯 pid：{drop_seg}"
        assert "<" not in drop_seg and "online:" not in drop_seg, f"drop 混进了 guid：{drop_seg}"
        assert "drop_badguid" not in line, f"正常情况不该有 drop_badguid：{line}"

        # 脏数据：解析不出 id 的 guid 要单独记 + 单独 warning
        cap.msgs.clear()
        dirty = list(_feed_bundles()) + [{"guid": "online:kugou:playlist:", "name": "坏歌单"}]
        app.filter_playlist_bundles(dirty, "tester", "tester")
        line = next((m for m in cap.msgs if "[PLAYLIST_MAP] filtered" in m), "")
        assert "drop_badguid=online:kugou:playlist:" in line, f"脏 guid 没单独记：{line}"
        assert "filtered 0/1 " in line, f"脏数据分母不对：{line}"
        assert any("无法解析出 id" in m for m in cap.msgs), "脏 guid 没打 warning"
    finally:
        root.removeHandler(cap)
        _pm_reset()


# ---------------------------------------------------------------- G. 路由
async def _client():
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                             base_url="http://test")


async def t24():
    async with await _client() as c:
        r = await c.get("/music/api/v1/playlist/detail",
                        params={"guid": app.kugou_feed_guid("daily")})
        assert r.status_code == 200, r.status_code
        body = r.json()
        assert body["code"] == 0 and body["data"]["guid"] == "online:kugou:feed:daily", body


async def t25():
    g1, g2 = app.kugou_feed_guid("daily"), app.kugou_feed_guid("rank", "33165")
    async with await _client() as c:
        r = await c.get("/music/api/v1/playlist/batch-detail", params={"guids": f"{g1},{g2}"})
        body = r.json()
        got = [x["guid"] for x in body["data"]["list"]]
        assert got == [g1, g2], f"批量详情丢了 feed 条目：{got}"


async def t26():
    tracks = {"items": [kugou_source.rank_item_to_raw(RANK_ITEM)], "total": 1}
    g = app.kugou_feed_guid("rank", "84235")
    with _Stub(tracks=tracks):
        async with await _client() as c:
            r = await c.get("/music/api/v1/track/playlist-detail/list",
                            params={"playlistGUID": g, "size": "-1"})
    body = r.json()
    assert body["code"] == 0, body
    lst = body["data"]["list"]
    assert len(lst) == 1, lst
    assert lst[0]["duration"] == 324825, lst[0]["duration"]
    assert lst[0]["guid"].startswith("online:kugou:")


def t27():
    """静态：四个入口都真的接了 feed（用行号切片，防切片为空的假绿）。"""
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    targets = {
        "playlist_list": "async def playlist_list(",
        "playlist_detail": "async def playlist_detail(",
        "playlist_batch_detail": "async def playlist_batch_detail(",
        "playlist_track_list": "async def playlist_track_list(",
    }
    marks = {}
    for name, needle in targets.items():
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(needle)), None)
        assert idx is not None, f"找不到 {needle}"
        marks[name] = idx
    order = sorted(marks.items(), key=lambda kv: kv[1])
    for pos, (name, start) in enumerate(order):
        end = order[pos + 1][1] if pos + 1 < len(order) else len(lines)
        seg = "\n".join(lines[start:end])
        assert len(seg) > 200, f"{name} 切片过短，断言等于没写"
        assert ("is_kugou_feed_guid" in seg or "fetch_kugou_feed_playlists" in seg), f"{name} 没接 feed 入口"


def t28():
    """静态：feed 取数链不许逐首预检（会多打 N 次上游 + 触发风控）。"""
    src = (PROXY / "kugou_source.py").read_text(encoding="utf-8")
    start = src.find("# F-01 歌单类内容源")
    assert start > 0, "找不到 F-01 段落"
    seg = src[start:]
    assert len(seg) > 3000, "F-01 切片过短，断言等于没写"
    for bad in ("/song/url", "/privilege/lite", "/song/privilege"):
        assert bad not in seg, f"feed 取数链里出现了逐首预检调用 {bad}"


def t29():
    """静态：每日推荐必须显式 ×1000（唯一一处秒转毫秒）。"""
    src = (PROXY / "kugou_source.py").read_text(encoding="utf-8")
    start = src.find("def daily_item_to_raw(")
    assert start > 0, "找不到 daily_item_to_raw"
    seg = src[start:start + 3000]
    assert len(seg) > 500, "切片过短"
    assert "1000" in seg, "time_length 是秒，daily_item_to_raw 里必须有 ×1000"


def t30():
    """静态：track_item_to_raw 里不许出现 / 1000（那是被驳回的"修复"）。"""
    src = (PROXY / "kugou_source.py").read_text(encoding="utf-8")
    start = src.find("def track_item_to_raw(")
    end = src.find("\ndef _pick_relate_quality_fields(")
    assert start > 0 and end > start, "切片边界不对"
    seg = src[start:end]
    assert len(seg) > 500, "切片过短，断言等于没写"
    assert "/ 1000" not in seg and "/1000" not in seg, \
        "timelen 是毫秒，这里不许再做 ÷1000"


# ---------------------------------------------------------------- main
async def main_async():
    print("=" * 72)
    print("A. 时长单位（毫秒为主，每日推荐是秒）")
    print("=" * 72)
    case("T01 精选 timelen 原样透出（毫秒）", t01)
    case("T02 每日推荐 time_length ×1000", t02)
    case("T03 排行榜 duration_flac 原样（毫秒）", t03)
    case("T04 三源 duration 都落在 100000–1200000", t04)
    case("T05 阴性对照：单位搞错时区间断言必红", t05)

    print("\n" + "=" * 72)
    print("B. 三个 adapter 的字段映射")
    print("=" * 72)
    case("T06 每日推荐字段（songname/author_name/time_length/sizable_cover）", t06)
    case("T07 排行榜字段（audio_info/album_info 嵌套）", t07)
    case("T08 精选歌单字段（剥歌手前缀）", t08)
    case("T09 坏输入不抛异常", t09)

    print("\n" + "=" * 72)
    print("C. 按配置音质取 hash")
    print("=" * 72)
    case("T10 high/flac/320/128 各取对应档", t10)
    case("T11 未知音质回落而非空", t11)
    case("T12 只有 128 时也能出声", t12)

    print("\n" + "=" * 72)
    print("D. feed guid 编解码")
    print("=" * 72)
    case("T13 daily/rank/special 编解码", t13)
    case("T14 feed guid 不被 playlist 解析污染", t14)
    case("T15 非法 guid 一律解析失败", t15)

    print("\n" + "=" * 72)
    print("E. 列表注入 / 开关")
    print("=" * 72)
    await _acase("T16 注入 daily + rank + 截断的 special", t16)
    await _acase("T17 rank/top 失败不影响其余组", t17)
    await _acase("T18 kugou_feed=0 → 零条目零请求", t18)
    await _acase("T19 kugou_enabled=0 → 零条目", t19)
    case("T20 detail 对象字段", t20)

    print("\n" + "=" * 72)
    print("F. 出歌与全量翻页")
    print("=" * 72)
    await _acase("T21 rank 出歌 → build_online_track", t21)
    await _acase("T22 size=-1 翻页拼齐且不多翻", t22)
    await _acase("T23 daily 全量不分页", t23)
    await _acase("T31 精选总数取 data.count", t31)

    print("\n" + "=" * 72)
    print("H. F-03 歌单映射 × feed 隔离（缺陷 1）")
    print("=" * 72)
    await _acase("T32 三模式 feed 存活矩阵 27/27", t32)
    await _acase("T33 用户歌单白/黑名单照旧生效", t33)
    await _acase("T34 whitelist 下另三入口不拦 feed", t34)
    await _acase("T35 drop 日志字段可机器切分", t35)

    print("\n" + "=" * 72)
    print("G. 路由 + 静态断言")
    print("=" * 72)
    await _acase("T24 /playlist/detail 认 feed guid", t24)
    await _acase("T25 /playlist/batch-detail 认 feed guid", t25)
    await _acase("T26 /track/playlist-detail/list 出歌", t26)
    case("T27 四个入口都接了 feed", t27)
    case("T28 feed 链无逐首预检", t28)
    case("T29 daily 必须 ×1000", t29)
    case("T30 track_item_to_raw 不许 ÷1000", t30)


asyncio.run(main_async())

print()
for n in PASS:
    print(f"  PASS  {n}")
for n, e in FAIL:
    print(f"  FAIL  {n}: {e}")
print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
