#!/usr/bin/env python3
"""歌单映射（F-03）测试：按飞牛本地用户过滤「我的歌单」。

覆盖：
  1. 工具层：三种 mode / 非法 mode / ids 非数组 / id 类型混用 / 空规则
  2. 加载层：不存在 / 语法错（首次 vs 有上次有效）/ 结构不可用 / mtime 热加载
  3. 用户解析：Authorization 指纹、探测异常、shared、guid→用户名、读库失败、列名自适应
  4. 规则优先级：用户名 → guid → default
  5. 四个入口：列表 / detail / batch-detail / 歌曲列表（**第四个最容易漏**）
  6. 异常隔离：拉歌单失败、识别用户失败都不许 500
  7. 未配置时零开销（不额外打 /user/me）
  8. 阴性对照：把过滤去掉，命中用例必须变红（证明测的是行为不是签名）
  9. 静态断言：四个入口共用同一判定函数；哈希不进日志

运行：python3 tests/test_playlist_map.py
（必须用装了 httpx 的解释器，例如 app 自带的 .venv-proxy）
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-plmap-"))
CONF = ROOT / "fnmusic_kugou_pro" / "etc"
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)
CONF.mkdir(parents=True, exist_ok=True)

DB = ROOT / "music.db"
DB_ALT = ROOT / "music-alt.db"


def _mk_db(path: Path, guid_col: str, name_col: str, rows: list[tuple[str, str]]) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(f"CREATE TABLE user ({guid_col} TEXT, {name_col} TEXT)")
        con.executemany(f"INSERT INTO user ({guid_col}, {name_col}) VALUES (?, ?)", rows)
        con.commit()
    finally:
        con.close()


GUID_A = "a" * 64          # 飞牛用户 A
GUID_B = "b" * 64          # 飞牛用户 B
_mk_db(DB, "guid", "name", [(GUID_A, "alice"), (GUID_B, "bob")])
_mk_db(DB_ALT, "guid", "username", [(GUID_A, "alice-alt")])

# 某些飞牛版本 name 存在但留空，真实显示名在 name_latin_full
DB_LATIN = ROOT / "music_latin.db"
con = sqlite3.connect(DB_LATIN)
con.execute("CREATE TABLE user (guid TEXT, name TEXT, name_latin_full TEXT, name_latin TEXT)")
con.executemany("INSERT INTO user VALUES (?,?,?,?)", [
    (GUID_A, "", "alice-latin", "ignored"),
    (GUID_B, "bob", "bob-latin", ""),      # name 非空 ⇒ 必须优先用它
    ("c" * 64, "", "carol-latin", ""),     # name 空 ⇒ 用 latin_full
])
con.commit()
con.close()

os.environ.update({
    "FNMUSIC_MUSIC_DB": str(DB),
    "FNMUSIC_LIBRARY_DIR": "",
    "FNMUSIC_SAVE_ENABLED": "0",
    "FNMUSIC_SAVE_DIR": str(ROOT / "lib"),
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "1",
    "FNMUSIC_ETC_DIR": str(CONF),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    # 必须显式指到临时路径：真机上 /vol1/@appconf/<app>/.env 存在，
    # 导入期会覆盖这里设的值 → 假红（1.0.3 排障时踩过）。
    "FNMUSIC_ENV_FILE": str(CONF / ".env"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})

sys.path.insert(0, str(PROXY))
import app  # noqa: E402

MAP_FILE = CONF / "playlist_map.json"
PASS, FAIL = [], []


def ok(name: str) -> None:
    PASS.append(name)


def bad(name: str, err) -> None:
    FAIL.append((name, err))


def case(name: str, fn) -> None:
    try:
        fn()
        ok(name)
    except AssertionError as e:
        bad(name, e or "assert")
    except Exception as e:  # noqa: BLE001
        bad(name, f"{type(e).__name__}: {e}")


def reset() -> None:
    """清掉三个缓存，让下一次调用真的去读文件/读库。"""
    app._PM_CACHE.update({"mtime": None, "data": None, "checked": 0.0})
    app._UC_CACHE.clear()
    app._GUID_NAME_CACHE.update({"ts": 0.0, "map": {}})


def write_map(obj) -> None:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    MAP_FILE.write_text(text, encoding="utf-8")
    reset()


def rm_map() -> None:
    try:
        MAP_FILE.unlink()
    except OSError:
        pass
    reset()


# ---------- 假上游 ----------
PID_A1, PID_A2, PID_A3 = "collection_1", "collection_2", "collection_3"
G1 = app.kugou_playlist_guid(PID_A1, "歌单1")
G2 = app.kugou_playlist_guid(PID_A2, "歌单2")
G3 = app.kugou_playlist_guid(PID_A3, "歌单3")

STATE = {
    "authed": True,
    "guid": GUID_A,
    "bundles": [],
    "detail_obj_calls": [],        # 记录"回源酷狗 detail"被调用了几次
    "list_calls": 0,
    "probe_calls": 0,
    "raise_bundles": False,
    "raise_probe": False,
}


def _mk_bundles() -> list[dict]:
    return [
        app.build_kugou_playlist_obj({"global_collection_id": PID_A1, "name": "歌单1", "count": 3}),
        app.build_kugou_playlist_obj({"global_collection_id": PID_A2, "name": "歌单2", "count": 5}),
        app.build_kugou_playlist_obj({"global_collection_id": PID_A3, "name": "歌单3", "count": 7}),
    ]


async def fake_probe(request, client):
    STATE["probe_calls"] += 1
    if STATE["raise_probe"]:
        raise RuntimeError("probe boom")
    return STATE["authed"], STATE["guid"], None


async def fake_bundles():
    STATE["list_calls"] += 1
    if STATE["raise_bundles"]:
        raise RuntimeError("kugou boom")
    return [dict(b) for b in STATE["bundles"]]


async def fake_envelope(request, client):
    return {"code": 0, "msg": "ok", "data": {"list": []}}


async def fake_detail_obj(request, guid):
    STATE["detail_obj_calls"].append(guid)
    return {"guid": guid, "name": "回源来的", "trackCount": 0, "source": "kugou"}


async def fake_tracks(request, guid, page=1, size=50):
    return {"items": [{"hash": "c" * 32, "name": "歌"}], "total": 7}


async def fake_tracks_full(fastapi_app, guid):
    return await fake_tracks(None, guid)


app._probe_upstream_auth = fake_probe
app.fetch_kugou_user_playlist_bundles = fake_bundles
app.fetch_upstream_envelope = fake_envelope
app._fetch_kugou_playlist_detail_obj = fake_detail_obj
app.fetch_kugou_playlist_tracks = fake_tracks
app.fetch_kugou_playlist_tracks_full = fake_tracks_full


def reset_state(**kw) -> None:
    STATE.update({
        "authed": True,
        "guid": GUID_A,
        "bundles": _mk_bundles(),
        "detail_obj_calls": [],
        "list_calls": 0,
        "probe_calls": 0,
        "raise_bundles": False,
        "raise_probe": False,
    })
    STATE.update(kw)
    reset()


async def get(client, path):
    return await client.get(path)


def names_of(payload) -> list[str]:
    return [it.get("name") for it in (payload.get("data") or {}).get("list") or []]


# ==================== 1. 工具层 ====================
def t_rules() -> None:
    allr = {"mode": "all", "ids": frozenset()}
    assert app.playlist_allow(allr, "x") and app.playlist_allow(allr, "")
    wl = {"mode": "whitelist", "ids": frozenset({"1", "2"})}
    assert app.playlist_allow(wl, "1") and app.playlist_allow(wl, "2")
    assert not app.playlist_allow(wl, "3")
    bl = {"mode": "blacklist", "ids": frozenset({"1"})}
    assert not app.playlist_allow(bl, "1")
    assert app.playlist_allow(bl, "2")


def t_norm_rule_valid() -> None:
    r = app._pm_norm_rule({"mode": "blacklist", "ids": ["1", 2, " 3 ", ""]}, "t")
    assert r == {"mode": "blacklist", "ids": frozenset({"1", "2", "3"})}, r


def t_norm_rule_bad_mode() -> None:
    assert app._pm_norm_rule({"mode": "deny", "ids": ["1"]}, "t") is None


def t_norm_rule_ids_not_list() -> None:
    # whitelist + ids 不是数组 ⇒ 视为"没配好"⇒ 无效（放行），不能静默屏蔽全部
    assert app._pm_norm_rule({"mode": "whitelist", "ids": "1,2"}, "t") is None
    # blacklist + ids 不是数组 ⇒ 空黑名单 = 什么都不屏蔽，有效
    r = app._pm_norm_rule({"mode": "blacklist", "ids": "1,2"}, "t")
    assert r == {"mode": "blacklist", "ids": frozenset()}, r


def t_norm_rule_empty_ids() -> None:
    assert app._pm_norm_rule({"mode": "whitelist", "ids": []}, "t") is None
    assert app._pm_norm_rule({"mode": "all"}, "t") == {"mode": "all", "ids": frozenset()}


def t_norm_default_fallback() -> None:
    cfg = app._pm_normalize({"users": {"alice": {"mode": "all"}}})
    assert cfg["default"]["mode"] == "all", cfg
    assert "alice" in cfg["users"]


def t_norm_users_not_dict() -> None:
    assert app._pm_normalize({"users": ["alice"]}) is None


# ==================== 2. 加载层 ====================
def t_load_missing() -> None:
    rm_map()
    cfg = app.load_playlist_map()
    assert cfg["users"] == {}, cfg
    assert app.playlist_map_active() is False


def t_load_broken_first() -> None:
    rm_map()
    write_map("{ not json")
    cfg = app.load_playlist_map()          # 不许抛
    assert cfg["users"] == {}, cfg
    assert app.playlist_map_active() is False


def t_load_broken_keeps_last() -> None:
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    assert app.playlist_map_active() is True
    cfg1 = app.load_playlist_map()
    assert "alice" in cfg1["users"]
    # 文件被写坏：必须保留上次有效配置，既不全放行也不全拦截
    MAP_FILE.write_text("{ broken", encoding="utf-8")
    app._PM_CACHE["checked"] = 0.0
    cfg2 = app.load_playlist_map()
    assert "alice" in cfg2["users"], cfg2
    assert app.playlist_rule_for(GUID_A, "alice")[0]["mode"] == "blacklist"


def t_load_users_not_dict_keeps_last() -> None:
    write_map({"users": {"alice": {"mode": "all"}}})
    app.load_playlist_map()
    # 直接改文件、只清 stat TTL —— 不能用 write_map，它会把缓存也清掉，
    # 那样测的就不是"保留上次有效"而是"重新解析"了。
    MAP_FILE.write_text(json.dumps({"users": "oops"}), encoding="utf-8")
    app._PM_CACHE["checked"] = 0.0
    cfg = app.load_playlist_map()
    assert "alice" in cfg["users"], cfg


def t_load_hot_reload() -> None:
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    assert app.load_playlist_map()["users"]["alice"]["mode"] == "blacklist"
    write_map({"users": {"alice": {"mode": "whitelist", "ids": [PID_A1]}}})
    assert app.load_playlist_map()["users"]["alice"]["mode"] == "whitelist"


def t_load_file_removed() -> None:
    write_map({"users": {"alice": {"mode": "all"}}})
    app.load_playlist_map()
    rm_map()
    cfg = app.load_playlist_map()
    assert cfg["users"] == {}, cfg


# ==================== 3. 用户解析 ====================
def t_fingerprint_distinct() -> None:
    from types import SimpleNamespace

    class R:
        def __init__(self, h):
            self.headers = h
            self.client = None

    a1 = app._auth_fingerprint(R({"authorization": "Bearer AAA"}))
    a2 = app._auth_fingerprint(R({"Authorization": "Bearer AAA"}))
    b1 = app._auth_fingerprint(R({"authorization": "Bearer BBB"}))
    assert a1 == a2 and a1 != b1
    assert "AAA" not in a1 and len(a1) <= 16


async def t_user_ok(client) -> None:
    reset_state()
    guid, name = await app.resolve_playlist_user(_dummy_req(), object())
    assert guid == GUID_A and name == "alice", (guid, name)


async def t_user_shared(client) -> None:
    reset_state(guid="shared")
    guid, name = await app.resolve_playlist_user(_dummy_req(), object())
    assert (guid, name) == ("", ""), (guid, name)


async def t_user_probe_raises(client) -> None:
    reset_state(raise_probe=True)
    guid, name = await app.resolve_playlist_user(_dummy_req(), object())
    assert (guid, name) == ("", ""), (guid, name)


async def t_user_db_fail_degrades(client) -> None:
    reset_state()
    old = app.CONF["music_db"]
    app.CONF["music_db"] = str(ROOT / "nope.db")
    try:
        guid, name = await app.resolve_playlist_user(_dummy_req(), object())
    finally:
        app.CONF["music_db"] = old
    # 读不到库不等于认不出人 —— 仍返回 guid，只是名字为空（退化按 guid 匹配）
    assert guid == GUID_A and name == "", (guid, name)


async def t_user_db_latin_full(client) -> None:
    """name 留空的版本：必须用 name_latin_full 补上，而不是退化成"没有名字"。

    只挑"存在的那一列"不够 —— name 列存在但值是空串时，要按优先级取第一个非空。
    """
    reset_state()
    old = app.CONF["music_db"]
    app.CONF["music_db"] = str(DB_LATIN)
    app._GUID_NAME_CACHE.update({"ts": 0.0, "map": {}})
    try:
        m = app._user_table_map()
    finally:
        app.CONF["music_db"] = old
        app._GUID_NAME_CACHE.update({"ts": 0.0, "map": {}})
    assert m.get(GUID_A) == "alice-latin", m
    assert m.get("c" * 64) == "carol-latin", m
    assert m.get(GUID_B) == "bob", f"name 非空时必须优先 name，实际 {m.get(GUID_B)}"


async def t_user_db_alt_column(client) -> None:
    reset_state()
    old = app.CONF["music_db"]
    app.CONF["music_db"] = str(DB_ALT)
    app._GUID_NAME_CACHE.update({"ts": 0.0, "map": {}})
    try:
        guid, name = await app.resolve_playlist_user(_dummy_req(), object())
    finally:
        app.CONF["music_db"] = old
        app._GUID_NAME_CACHE.update({"ts": 0.0, "map": {}})
    assert name == "alice-alt", name


def _dummy_req():
    class R:
        headers = {"authorization": "Bearer test-token-xyz"}
        client = None
        method = "GET"
        query_params: dict = {}
        app = None

    return R()


# ==================== 4. 规则优先级 ====================
def t_priority_name_first() -> None:
    write_map({"users": {
        "alice": {"mode": "whitelist", "ids": [PID_A1]},
        GUID_A: {"mode": "whitelist", "ids": [PID_A2]},
    }})
    rule, hit = app.playlist_rule_for(GUID_A, "alice")
    assert rule["ids"] == frozenset({PID_A1}), rule
    assert hit.startswith("user:"), hit


def t_priority_guid_fallback() -> None:
    write_map({"users": {GUID_A: {"mode": "whitelist", "ids": [PID_A2]}}})
    rule, hit = app.playlist_rule_for(GUID_A, "renamed-user")
    assert rule["ids"] == frozenset({PID_A2}), rule
    assert "guid" in hit, hit


def t_priority_default() -> None:
    write_map({"users": {"someone": {"mode": "all"}},
               "default": {"mode": "blacklist", "ids": [PID_A3]}})
    rule, hit = app.playlist_rule_for(GUID_A, "alice")
    assert hit == "default", hit
    assert rule["mode"] == "blacklist"


# ==================== 5. 四个入口 ====================
async def e_list_no_config(client) -> None:
    reset_state()
    rm_map()
    r = await client.get("/music/api/v1/playlist/list")
    assert r.status_code == 200, r.status_code
    assert len(names_of(r.json())) == 3, r.json()
    # 零开销：没配规则就不该去识别用户
    assert STATE["probe_calls"] == 0, STATE["probe_calls"]


async def e_list_whitelist(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "whitelist", "ids": [PID_A1]}}})
    r = await client.get("/music/api/v1/playlist/list")
    assert names_of(r.json()) == ["歌单1"], names_of(r.json())


async def e_list_blacklist(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get("/music/api/v1/playlist/list")
    assert names_of(r.json()) == ["歌单1", "歌单3"], names_of(r.json())


async def e_list_two_users(client) -> None:
    """A/B 双用户规则不同 —— 同一份配置下各自看到各自的，不许串号。"""
    write_map({
        "users": {
            "alice": {"mode": "whitelist", "ids": [PID_A1]},
            "bob": {"mode": "whitelist", "ids": [PID_A2, PID_A3]},
        },
    })
    reset_state()
    r = await client.get("/music/api/v1/playlist/list",
                         headers={"authorization": "Bearer token-A"})
    assert names_of(r.json()) == ["歌单1"], names_of(r.json())

    reset_state(guid=GUID_B)
    r = await client.get("/music/api/v1/playlist/list",
                         headers={"authorization": "Bearer token-B"})
    assert names_of(r.json()) == ["歌单2", "歌单3"], names_of(r.json())


async def e_list_default_only(client) -> None:
    """只配 default、不配 users 也必须生效。

    这是实际捅过的 bug：playlist_map_active 只看 users ⇒ 只写 default 时
    整个功能静默失效（配置看着对、日志一条不打、歌单一个不少）。
    """
    reset_state()
    write_map({"default": {"mode": "blacklist", "ids": [PID_A2]}})
    r = await client.get("/music/api/v1/playlist/list")
    assert names_of(r.json()) == ["歌单1", "歌单3"], names_of(r.json())


async def e_list_bundles_raise(client) -> None:
    reset_state(raise_bundles=True)
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get("/music/api/v1/playlist/list")
    assert r.status_code == 200, r.status_code          # 不许 500
    assert names_of(r.json()) == [], r.json()


async def e_list_probe_raise(client) -> None:
    """识别用户失败 ⇒ 走 default，不该影响列表本身。"""
    reset_state(raise_probe=True)
    write_map({"default": {"mode": "blacklist", "ids": [PID_A2]}})
    r = await client.get("/music/api/v1/playlist/list")
    assert r.status_code == 200, r.status_code
    assert names_of(r.json()) == ["歌单1", "歌单3"], names_of(r.json())


async def e_detail_blocked(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get(f"/music/api/v1/playlist/detail?guid={G2}")
    payload = r.json()
    assert r.status_code == 200, r.status_code
    assert payload["code"] == 0, payload
    assert payload["data"] == {}, payload           # 同构空结构，不是 null、不是 404
    # 关键：被屏蔽时绝不能回源酷狗 detail（否则换个接口照样能取到）
    assert STATE["detail_obj_calls"] == [], STATE["detail_obj_calls"]


async def e_detail_allowed(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get(f"/music/api/v1/playlist/detail?guid={G1}")
    payload = r.json()
    assert payload["data"].get("name") == "歌单1", payload


async def e_batch_mixed(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get(f"/music/api/v1/playlist/batch-detail?guids={G1},{G2},{G3}")
    got = [it.get("guid") for it in r.json()["data"]["list"]]
    assert G1 in got and G3 in got, got
    assert G2 not in got, got
    # 被屏蔽的那个不许回源
    assert G2 not in STATE["detail_obj_calls"], STATE["detail_obj_calls"]


async def e_batch_all_blocked(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "whitelist", "ids": [PID_A1]}}})
    r = await client.get(f"/music/api/v1/playlist/batch-detail?guids={G2},{G3}")
    assert r.json()["data"]["list"] == [], r.json()
    assert STATE["detail_obj_calls"] == [], STATE["detail_obj_calls"]


async def e_track_list_blocked(client) -> None:
    """第四个入口：歌单内歌曲。不拦的话 size=-1 一次拉完。"""
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={G2}&size=-1")
    payload = r.json()
    assert r.status_code == 200, r.status_code
    assert payload["data"]["list"] == [], payload
    assert payload["data"]["total"] == 0, payload


async def e_track_list_allowed(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={G1}&size=-1")
    payload = r.json()
    assert payload["data"]["total"] == 7, payload


async def e_diag_endpoint(client) -> None:
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    r = await client.get("/_ext/playlist_map")
    payload = r.json()
    assert payload["active"] is True, payload
    assert payload["users"]["alice"]["mode"] == "blacklist", payload
    assert payload["current"]["user"] == "alice", payload
    raw = r.text
    assert "test-token" not in raw and "Bearer" not in raw, "诊断端点泄漏了 Authorization"
    assert GUID_A not in raw, "诊断端点给了完整 guid"


# ==================== 8. 阴性对照 ====================
async def n_negative_control(client) -> None:
    """把过滤函数换成恒等（= 逻辑被注掉），命中用例必须变红。

    证明前面的断言测的是"过滤行为"，不是函数签名恰好长得对。
    注意：负向对照里 detail / track-list 走的是各自的 allow 判定，
    不受 filter_playlist_bundles 影响 —— 所以这里只对照"列表"这一入口，
    另外两个入口用下面的 n_negative_allow 对照。
    """
    reset_state()
    write_map({"users": {"alice": {"mode": "whitelist", "ids": [PID_A1]}}})
    orig = app.filter_playlist_bundles
    app.filter_playlist_bundles = lambda bundles, g, n: bundles   # 故意不滤
    try:
        r = await client.get("/music/api/v1/playlist/list")
        got = names_of(r.json())
    finally:
        app.filter_playlist_bundles = orig
    assert got == ["歌单1", "歌单2", "歌单3"], (
        f"阴性对照失败：过滤被去掉后仍只有 {got}，说明正经用例可能没测到过滤"
    )


async def n_negative_allow(client) -> None:
    """同一个对照，作用在 detail / track-list 共用的 playlist_allow 上。"""
    reset_state()
    write_map({"users": {"alice": {"mode": "blacklist", "ids": [PID_A2]}}})
    orig = app.playlist_allow
    app.playlist_allow = lambda rule, pid: True
    try:
        r1 = await client.get(f"/music/api/v1/playlist/detail?guid={G2}")
        d = r1.json()["data"]
        r2 = await client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={G2}")
        t = r2.json()["data"]
    finally:
        app.playlist_allow = orig
    assert d != {}, "阴性对照失败：detail 判定没被对照到"
    assert t.get("total") == 7, "阴性对照失败：歌曲列表判定没被对照到"


# ==================== 9. 静态断言 ====================
def t_static_shared_judge() -> None:
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    lines = src.split("\n")

    def body(name: str) -> str:
        start = None
        for i, l in enumerate(lines):
            if l.startswith(f"async def {name}("):
                start = i
                break
        assert start is not None, f"找不到 {name}"
        end = start + 1
        while end < len(lines) and not lines[end].startswith("@app."):
            end += 1
        seg = "\n".join(lines[start:end])
        # 防白测：切片必须非空且含函数名（切成空串会让下面的断言恒真）
        assert len(seg) > 200 and f"async def {name}(" in seg, f"{name} 切片异常"
        return seg

    for fn in ("playlist_detail", "playlist_batch_detail", "playlist_track_list"):
        seg = body(fn)
        assert "playlist_map_active()" in seg, f"{fn} 没接判定"
        assert "playlist_rule_for(" in seg, f"{fn} 没走统一规则解析"
        assert "playlist_allow(" in seg, f"{fn} 没走统一放行判定"

    seg = body("playlist_list")
    assert "playlist_map_active()" in seg, "playlist_list 没接判定"
    # 列表入口必须走统一的 bundles 过滤（内部再调 playlist_rule_for / playlist_allow）
    assert "filter_playlist_bundles(" in seg, "playlist_list 没走统一过滤"

    fb = src[src.find("def filter_playlist_bundles("):src.find("def playlist_blocked_detail_payload(")]
    assert "playlist_rule_for(" in fb and "playlist_allow(" in fb, "过滤函数内部没复用统一判定"


def t_static_hash_not_logged() -> None:
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(src.split("\n"), 1):
        if "logger." not in line:
            continue
        low = line.lower()
        # 日志行里不许出现缓存键 / 指纹 / token 变量
        if "fingerprint" in low or "_auth_key" in low:
            offenders.append((i, line.strip()))
        if "{key}" in line or ", key)" in line:
            offenders.append((i, line.strip()))
    assert not offenders, f"疑似把缓存键写进日志：{offenders}"


def t_static_no_unbounded_gather() -> None:
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    assert "return_exceptions=True" in src, "gather 没开异常隔离"
    seg = src[src.find("if playlist_map_active():\n        # 并发"):]
    seg = seg[:1200]
    assert "asyncio.gather(" in seg and "return_exceptions=True" in seg, "列表入口的 gather 未做异常隔离"


# ==================== main ====================
async def main() -> None:
    import httpx

    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("=" * 72)
        print("1) 判定工具层")
        print("=" * 72)
        case("playlist_allow 三种 mode", t_rules)
        case("规则归一：合法项 / id 类型混用", t_norm_rule_valid)
        case("规则归一：非法 mode → 该项无效", t_norm_rule_bad_mode)
        case("规则归一：ids 非数组", t_norm_rule_ids_not_list)
        case("规则归一：空 ids", t_norm_rule_empty_ids)
        case("规则归一：default 缺省回落 all", t_norm_default_fallback)
        case("规则归一：users 不是对象 → 整体不可用", t_norm_users_not_dict)

        print("\n" + "=" * 72)
        print("2) 配置加载（含损坏时保留上次有效）")
        print("=" * 72)
        case("无配置文件 = 不过滤", t_load_missing)
        case("首次就语法错 → 空配置且不抛", t_load_broken_first)
        case("语法错 + 有上次有效 → 保留上次有效", t_load_broken_keeps_last)
        case("结构不可用 + 有上次有效 → 保留", t_load_users_not_dict_keeps_last)
        case("mtime 变化热加载", t_load_hot_reload)
        case("文件被删 → 回落空配置", t_load_file_removed)

        print("\n" + "=" * 72)
        print("3) 用户识别")
        print("=" * 72)
        case("指纹：同头同值、不同值不同、不落明文", t_fingerprint_distinct)
        await _acase("识别正常用户", t_user_ok, client)
        await _acase("guid=shared → 视为未登录", t_user_shared, client)
        await _acase("探测抛异常 → 未登录", t_user_probe_raises, client)
        await _acase("读库失败 → 退化用 guid", t_user_db_fail_degrades, client)
        await _acase("user 表列名不同（username）也能读", t_user_db_alt_column, client)
        await _acase("user 表 name 留空 → 用 name_latin_full", t_user_db_latin_full, client)

        print("\n" + "=" * 72)
        print("4) 规则优先级 用户名 → guid → default")
        print("=" * 72)
        case("用户名命中优先于 guid", t_priority_name_first)
        case("用户名不命中 → 用 guid", t_priority_guid_fallback)
        case("都不命中 → default", t_priority_default)

        print("\n" + "=" * 72)
        print("5) 四个入口")
        print("=" * 72)
        await _acase("列表：未配置不过滤 + 零额外请求", e_list_no_config, client)
        await _acase("列表：whitelist", e_list_whitelist, client)
        await _acase("列表：blacklist", e_list_blacklist, client)
        await _acase("列表：A/B 双用户不串号", e_list_two_users, client)
        await _acase("列表：只配 default 也生效", e_list_default_only, client)
        await _acase("列表：拉歌单失败不 500", e_list_bundles_raise, client)
        await _acase("列表：识别用户失败走 default", e_list_probe_raise, client)
        await _acase("detail：被屏蔽 → 同构空结构且不回源", e_detail_blocked, client)
        await _acase("detail：未屏蔽 → 正常", e_detail_allowed, client)
        await _acase("batch-detail：混合 guid 只剔被屏蔽", e_batch_mixed, client)
        await _acase("batch-detail：全被屏蔽不回源", e_batch_all_blocked, client)
        await _acase("歌曲列表：被屏蔽 → list:[] total:0", e_track_list_blocked, client)
        await _acase("歌曲列表：未屏蔽 → 正常", e_track_list_allowed, client)
        await _acase("诊断端点：不泄漏 Authorization / 完整 guid", e_diag_endpoint, client)

        print("\n" + "=" * 72)
        print("8) 阴性对照（过滤被注掉时必须变红）")
        print("=" * 72)
        await _acase("对照：列表过滤", n_negative_control, client)
        await _acase("对照：detail / 歌曲列表判定", n_negative_allow, client)

        print("\n" + "=" * 72)
        print("9) 静态断言")
        print("=" * 72)
        case("四个入口共用同一判定函数", t_static_shared_judge)
        case("Authorization 指纹不进日志", t_static_hash_not_logged)
        case("gather 异常隔离", t_static_no_unbounded_gather)

    print("\n" + "=" * 72)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for name, err in FAIL:
        print(f"  ✗ {name}: {err}")
    print("=" * 72)
    if FAIL:
        raise SystemExit(1)


async def _acase(name: str, fn, client) -> None:
    try:
        await fn(client)
        ok(name)
    except AssertionError as e:
        bad(name, e or "assert")
    except Exception as e:  # noqa: BLE001
        bad(name, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
