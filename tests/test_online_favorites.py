#!/usr/bin/env python3
"""在线收藏落本地 + 歌单导入（v1.3.0）测试。

覆盖：S1 三种字段名、S2 导入去重与上限、S3 只撤销来源、S5 createdAt 递增，
以及 S6 坏 JSON 隔离与写盘后收藏页不白屏。

运行：TMPDIR=/dev/shm <venv-python> tests/test_online_favorites.py
"""
from __future__ import annotations

import asyncio
import glob
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-online-fav-"))
FAV_DIR = ROOT / "favorites"
ETC = ROOT / "fnmusic_kugou_pro" / "etc"
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

GUID_A = "a" * 64
GUID_B = "b" * 64
DB = ROOT / "music.db"
con = sqlite3.connect(DB)
con.execute("CREATE TABLE user (guid TEXT, name TEXT)")
con.executemany("INSERT INTO user (guid, name) VALUES (?, ?)", [
    (GUID_A, "alice"),
    (GUID_B, "bob"),
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
    "FNMUSIC_FEED": "0",
    "FNMUSIC_FAV_DIR": str(FAV_DIR),
    "FNMUSIC_ETC_DIR": str(ETC),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    "FNMUSIC_ENV_FILE": str(ETC / ".env"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})

sys.path.insert(0, str(PROXY))
import app  # noqa: E402

app.CONF["fav_dir"] = str(FAV_DIR)
app.CONF["music_db"] = str(DB)
app.CONF["kugou_enabled"] = True
app.CONF["kugou_token"] = "test-token"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def run_case(name: str, fn) -> None:
    try:
        fn()
        check(name, True)
    except AssertionError as e:
        check(name, False, str(e) or "assert")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")


def clear_favorites() -> None:
    if FAV_DIR.exists():
        for p in FAV_DIR.glob("*"):
            if p.is_file():
                p.unlink()


def fav_items(guid: str = GUID_A) -> list[dict]:
    return app.load_online_favorites(guid)


def write_items(items: list[dict], guid: str = GUID_A) -> None:
    path = Path(app.user_fav_path(guid))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


def raw_track(h: str, title: str = "歌") -> dict:
    return {
        "hash": h,
        "name": title,
        "singerinfo": [{"name": "歌手", "id": "1"}],
        "albuminfo": {"name": "专辑", "id": "2"},
        "timelen": 180000,
        "extname": "mp3",
    }


PLAYLISTS = [
    {"id": "pid1", "name": "歌单一", "track_count": 5, "is_feed": False},
    {"id": "pid2", "name": "歌单二", "track_count": 1, "is_feed": False},
]
STATE = {
    "tracks": {
        "pid1": [raw_track("DUPHASH", "重复"), raw_track("h2", "同名"), raw_track("h3", "同名"),
                 raw_track("h4", "四"), raw_track("h5", "五")],
        "pid2": [raw_track("p2h", "另一张")],
    },
    "forward_calls": [],
}


async def fake_probe(request, client):
    return True, GUID_A, None


async def fake_online_info(request, guid):
    return {"id": app.song_id_from_online_guid(guid), "source": "kugou", "title": "在线歌",
            "artist": "歌手", "album": "专辑", "duration_s": 180000, "ext": "mp3"}


async def fake_bundles():
    return [app.build_kugou_playlist_obj({
        "global_collection_id": p["id"], "name": p["name"], "count": p["track_count"],
    }) for p in PLAYLISTS]


async def fake_tracks_full(fastapi_app, guid):
    pid = app.kugou_playlist_id_from_guid(guid)
    items = STATE["tracks"].get(pid, [])
    return {"items": list(items), "total": len(items), "page": 1, "pagesize": 50}


async def fake_forward(request, client):
    STATE["forward_calls"].append(request)
    return app.JSONResponse(content={"code": 0, "msg": "forwarded", "data": None})


app._probe_upstream_auth = fake_probe
app._online_info = fake_online_info
app.fetch_kugou_user_playlist_bundles = fake_bundles
app.fetch_kugou_playlist_tracks_full = fake_tracks_full
app.forward_to_upstream = fake_forward


class FakeUpstreamClient:
    def build_request(self, method, url, headers=None, **kw):
        return httpx.Request(method, "http://upstream" + url, headers=headers)

    async def send(self, req):
        return httpx.Response(200, json={
            "code": 0,
            "msg": "ok",
            "data": {"list": [{"guid": "native-1", "title": "本地收藏"}], "total": 1},
        })


app.get_upstream_client = lambda _app: FakeUpstreamClient()


def make_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def request(method: str, path: str, **kw):
    async with make_client() as client:
        return await client.request(method, path, **kw)


def reset_state() -> None:
    clear_favorites()
    STATE["forward_calls"].clear()
    STATE["tracks"] = {
        "pid1": [raw_track("DUPHASH", "重复"), raw_track("h2", "同名"), raw_track("h3", "同名"),
                 raw_track("h4", "四"), raw_track("h5", "五")],
        "pid2": [raw_track("p2h", "另一张")],
    }


def test_s1_create_fields() -> None:
    async def inner():
        guid = "online:kugou:AbCdEf123456"
        for field in ("trackGuid", "trackGUID", "guid"):
            clear_favorites()
            r = await request("POST", "/music/api/v1/favorite-track/create", json={field: guid})
            assert r.status_code == 200, (field, r.status_code, r.text)
            assert r.json().get("code") == 0, (field, r.text)
            items = fav_items()
            assert len(items) == 1 and items[0].get("guid") == guid, (field, items)
    asyncio.run(inner())


def test_s1_local_guid_forwarded() -> None:
    async def inner():
        clear_favorites()
        STATE["forward_calls"].clear()
        local_guid = "0123456789abcdef0123456789abcdef"
        r = await request("POST", "/music/api/v1/favorite-track/create",
                          json={"trackGuid": local_guid})
        assert r.status_code == 200 and r.json().get("msg") == "forwarded", r.text
        assert len(STATE["forward_calls"]) == 1, STATE["forward_calls"]
        assert fav_items() == [], "本地 guid 不应写入在线收藏"
    asyncio.run(inner())


def test_s1_delete_fields_and_idempotent() -> None:
    async def inner():
        for field in ("trackGuid", "trackGUID", "guid"):
            clear_favorites()
            guid = f"online:kugou:del{field.lower()}"
            write_items([{"guid": guid, "createdAt": 1,
                          "track": app.build_favorite_track_obj(guid, created_at=1)}])
            r = await request("POST", "/music/api/v1/favorite-track/delete", json={field: guid})
            assert r.status_code == 200 and r.json().get("code") == 0, (field, r.text)
            assert fav_items() == [], (field, fav_items())
            r2 = await request("POST", "/music/api/v1/favorite-track/delete", json={field: guid})
            assert r2.status_code == 200 and r2.json().get("code") == 0, (field, r2.text)
            assert fav_items() == [], (field, fav_items())
    asyncio.run(inner())


def test_s1_delete_local_guid_forwarded() -> None:
    async def inner():
        clear_favorites()
        STATE["forward_calls"].clear()
        local_guid = "fedcba9876543210fedcba9876543210"
        r = await request("POST", "/music/api/v1/favorite-track/delete",
                          json={"trackGuid": local_guid})
        assert r.status_code == 200 and r.json().get("msg") == "forwarded", r.text
        assert len(STATE["forward_calls"]) == 1, STATE["forward_calls"]
    asyncio.run(inner())


def test_s2_import_dedupe_counts_and_sort() -> None:
    async def inner():
        reset_state()
        manual_dup = "online:kugou:duphash"
        manual_other = "online:kugou:manualonly"
        write_items([
            {"guid": manual_dup, "createdAt": 10,
             "track": app.build_favorite_track_obj(manual_dup, created_at=10)},
            {"guid": manual_other, "createdAt": 11,
             "track": app.build_favorite_track_obj(manual_other, created_at=11)},
        ])
        r = await request("POST", "/_ext/online_favorites/import",
                          json={"user_key": GUID_A, "playlist_id": "pid1"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert (d["imported"], d["skipped"], d["failed"], d["total"]) == (4, 1, 0, 5), d
        items = fav_items()
        assert len(items) == 6, len(items)
        manual = [it for it in items if it.get("guid") == manual_dup][0]
        assert "source" not in manual, manual
        imported = [it for it in items if it.get("source") == "playlist:pid1"]
        assert len(imported) == 4, imported
        stamps = [int(it["createdAt"]) for it in sorted(imported, key=lambda x: x["createdAt"])]
        assert len(stamps) == len(set(stamps)), stamps
        assert all(b > a for a, b in zip(stamps, stamps[1:])), stamps

        r2 = await request("POST", "/_ext/online_favorites/import",
                           json={"user_key": "alice", "playlist_id": "pid1"})
        d2 = r2.json()
        assert r2.status_code == 200, r2.text
        assert (d2["imported"], d2["skipped"], d2["failed"], d2["total"]) == (0, 5, 0, 5), d2
        assert len(fav_items()) == 6, "重复导入不应新增条目"

        rg = await request("GET", "/_ext/online_favorites", params={"user_key": GUID_A})
        assert rg.status_code == 200, rg.text
        assert len(rg.json().get("items") or []) == 6, rg.text
    asyncio.run(inner())


def test_s2_limit_and_unknowns() -> None:
    async def inner():
        reset_state()
        STATE["tracks"]["pid1"] = [raw_track(f"lim{i}") for i in range(301)]
        r = await request("POST", "/_ext/online_favorites/import",
                          json={"user_key": GUID_A, "playlist_id": "pid1"})
        assert r.status_code == 400, r.text
        assert "分批导入" in r.text, r.text
        r2 = await request("POST", "/_ext/online_favorites/import",
                           json={"user_key": GUID_A, "playlist_id": "nope"})
        assert r2.status_code == 400 and r2.json().get("code") == "unknown_playlist", r2.text
        r3 = await request("POST", "/_ext/online_favorites/import",
                           json={"user_key": "nobody", "playlist_id": "pid1"})
        assert r3.status_code == 400 and r3.json().get("code") == "unknown_user", r3.text
        r4 = await request("POST", "/_ext/online_favorites/import",
                           json={"user_key": GUID_A[:8], "playlist_id": "pid1"})
        assert r4.status_code == 400 and r4.json().get("code") == "unknown_user", r4.text
    asyncio.run(inner())


def test_s2_cross_site_rejected() -> None:
    async def inner():
        reset_state()
        r = await request("POST", "/_ext/online_favorites/import",
                          headers={"Origin": "https://evil.example"},
                          json={"user_key": GUID_A, "playlist_id": "pid1"})
        assert r.status_code == 403, r.text
        assert fav_items() == [], fav_items()
    asyncio.run(inner())


def test_s3_undo_only_source() -> None:
    async def inner():
        reset_state()
        manual = "online:kugou:manual3"
        other = "online:kugou:other3"
        write_items([
            {"guid": manual, "createdAt": 1, "track": app.build_favorite_track_obj(manual, created_at=1)},
            {"guid": other, "createdAt": 2, "source": "playlist:pid2",
             "track": app.build_favorite_track_obj(other, created_at=2)},
        ])
        r = await request("POST", "/_ext/online_favorites/import",
                          json={"user_key": GUID_A, "playlist_id": "pid1"})
        assert r.json()["imported"] == 5, r.text
        rd = await request("DELETE", "/_ext/online_favorites/import",
                           json={"user_key": GUID_A, "playlist_id": "pid1"})
        assert rd.status_code == 200 and rd.json().get("removed") == 5, rd.text
        items = fav_items()
        assert len(items) == 2, items
        assert {it.get("guid") for it in items} == {manual, other}, items
        assert not any(it.get("source") == "playlist:pid1" for it in items), items
        rd2 = await request("DELETE", "/_ext/online_favorites/import",
                            json={"user_key": GUID_A, "playlist_id": "pid1"})
        assert rd2.status_code == 200 and rd2.json().get("removed") == 0, rd2.text
    asyncio.run(inner())


def test_s6_corrupt_json_isolated_and_list_ok() -> None:
    async def inner():
        reset_state()
        path = Path(app.user_fav_path(GUID_A))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{坏内容', encoding="utf-8")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        app.logger.addHandler(handler)
        try:
            assert fav_items() == [], "坏 JSON 必须返回空列表"
            backups = glob.glob(str(path) + ".corrupt-*")
            assert len(backups) == 1, backups
            assert "WARNING" in stream.getvalue() and "JSONDecodeError" in stream.getvalue(), stream.getvalue()
        finally:
            app.logger.removeHandler(handler)

        # 坏文件已被挪走；收藏页仍要返回原生列表，且 total 不被污染。
        assert not path.exists(), path
        r = await request("GET", "/music/api/v1/favorite-track/list")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["data"]["total"] == 1 and len(d["data"]["list"]) == 1, d

        # 再次收藏必须能重新写入健康文件。
        guid = "online:kugou:aftercorrupt"
        rc = await request("POST", "/music/api/v1/favorite-track/create", json={"trackGuid": guid})
        assert rc.status_code == 200 and rc.json().get("code") == 0, rc.text
        assert json.loads(path.read_text(encoding="utf-8"))["items"][0]["guid"] == guid
    asyncio.run(inner())


def test_s6_save_keeps_old_file_on_failure() -> None:
    reset_state()
    write_items([{"guid": "online:kugou:keep", "createdAt": 1,
                  "track": app.build_favorite_track_obj("online:kugou:keep", created_at=1)}])
    before = Path(app.user_fav_path(GUID_A)).read_text(encoding="utf-8")
    old_replace = app.os.replace

    def boom(src, dst):
        raise OSError("disk full")

    app.os.replace = boom
    try:
        ok = app.save_online_favorites(GUID_A, [{"guid": "online:kugou:new"}])
    finally:
        app.os.replace = old_replace
    assert ok is False, ok
    after = Path(app.user_fav_path(GUID_A)).read_text(encoding="utf-8")
    assert after == before, "写失败必须保留上一次有效文件"
    assert not list(Path(app.user_fav_path(GUID_A)).parent.glob("*.tmp")), "临时文件未清理"


def test_s4_settings_controls_static() -> None:
    html = (PKG / "app" / "web" / "settings.html").read_text(encoding="utf-8")
    for token in ("导入歌单到用户收藏", "btn-online-fav-import", "btn-online-fav-undo",
                  "已导入", "跳过", "失败", "撤销"):
        assert token in html, token
    assert "<script src=" not in html and "<link rel=\"stylesheet\"" not in html, "新增了外部资源"
    assert "https://" not in html.split("<script>", 1)[-1], "脚本里新增了外部地址"


print("=" * 72)
print("S1 收藏字段名与删除分支")
for _name, _fn in [
    ("create 认三种字段名", test_s1_create_fields),
    ("create 本地 guid 原样透传", test_s1_local_guid_forwarded),
    ("delete 认三种字段名且幂等", test_s1_delete_fields_and_idempotent),
    ("delete 本地 guid 原样透传", test_s1_delete_local_guid_forwarded),
]:
    run_case(_name, _fn)

print("\nS2 歌单导入")
for _name, _fn in [
    ("hash 去重、计数守恒、可读回", test_s2_import_dedupe_counts_and_sort),
    ("301 首拒绝、未知用户/歌单拒绝", test_s2_limit_and_unknowns),
    ("跨站写入 403", test_s2_cross_site_rejected),
]:
    run_case(_name, _fn)

print("\nS3 撤销导入")
run_case("只撤销指定来源", test_s3_undo_only_source)

print("\nS5/S6 排序与容错")
run_case("坏 JSON 隔离且收藏页 200", test_s6_corrupt_json_isolated_and_list_ok)
run_case("写失败保留旧文件并清理临时文件", test_s6_save_keeps_old_file_on_failure)
run_case("设置页导入控件存在且无外部资源", test_s4_settings_controls_static)

print("\n结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
for _name in FAIL:
    print("  ❌", _name)
sys.exit(1 if FAIL else 0)
