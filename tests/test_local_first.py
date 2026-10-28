#!/usr/bin/env python3
"""本地优先播放（1.0.2 / F-02）的测试。

覆盖：
  * saved_file_path / already_saved 的一致性（含"标记指向目录"这个旧行为修正）
  * saved_file_playable 的试读回落（坏文件 / 权限拒绝 → 回落 CDN 而不是中断）
  * serve_file_with_range 的 200 / 206 / 后缀 Range / 非法 Range / ETag
  * 大文件必须流式：分块 ≤ 256 KiB，且峰值内存远小于文件大小
  * stream_track 的本地命中不再触网；关掉开关 / 文件被删时回落 CDN
  * 配置面：CONF 默认值、.env 回读、设置页字段

运行：python3 tests/test_local_first.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-localfirst-"))
LIB = ROOT / "music_lib"
LIB.mkdir(parents=True, exist_ok=True)
MARKERS = ROOT / "markers"
MARKERS.mkdir(parents=True, exist_ok=True)

DB = ROOT / "music.db"
con = sqlite3.connect(DB)
con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
con.execute("INSERT INTO shared_library (path) VALUES (?)", (str(LIB),))
con.commit()
con.close()

os.environ.update({
    "FNMUSIC_MUSIC_DB": str(DB),
    "FNMUSIC_LIBRARY_DIR": "",
    "FNMUSIC_SAVE_ENABLED": "1",
    "FNMUSIC_SAVE_DIR": str(LIB / "kugou"),
    "FNMUSIC_SAVE_MARKER_DIR": str(MARKERS),
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "0",
    "FNMUSIC_ETC_DIR": str(ROOT / "fnmusic_kugou_pro" / "etc"),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    # 真机上 /vol1/@appconf/<app>/.env 存在，导入期会覆盖上面的设置 → 假红
    "FNMUSIC_ENV_FILE": str(ROOT / "absent.env"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROXY))
import app  # noqa: E402

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


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 夹具

AUDIO = b"ID3" + bytes(range(256)) * 40          # 10,243 字节
# is_online_guid() 的判据是 startswith("online:")，且 source_from_online_guid()
# 取第 2 段作为源名 —— 所以真实 guid 形如 online:kugou:<id>，测试必须照这个来，
# 否则 stream_track 在第一道闸门就把请求透传给上游，压根进不了本地优先分支。
GUID = "online:kugou:test-local-first"


def write_marker(guid: str, target: str) -> None:
    p = Path(app._save_marker_path(guid))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(target + "\n", encoding="utf-8")


def clear_marker(guid: str) -> None:
    p = Path(app._save_marker_path(guid))
    if p.exists():
        p.unlink()


def make_audio(name: str = "a.mp3", body: bytes = AUDIO) -> str:
    p = ROOT / name
    p.write_bytes(body)
    return str(p)


def collect(resp) -> tuple[int, dict, bytes]:
    """把响应跑完，返回 (status, headers, body)。

    404 / 304 / 416 走的是普通 Response（没有 body_iterator），这里一并兼容，
    否则测 416 会因为"拿不到可迭代体"而假红 —— 那是测试的毛病，不是代码的。
    """
    if not hasattr(resp, "body_iterator"):
        body = resp.body if isinstance(resp.body, bytes) else str(resp.body).encode()
        return resp.status_code, dict(resp.headers), body

    async def _go():
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c if isinstance(c, bytes) else str(c).encode())
        return b"".join(chunks)

    body = run(_go())
    return resp.status_code, dict(resp.headers), body


class FakeReq:
    """stream_track 只用到 method / query_params / headers 三样，不必起真服务。"""

    def __init__(
        self,
        guid: str,
        range_header: str | None = None,
        if_none_match: str | None = None,
    ):
        self.method = "GET"
        self.query_params = {"guid": guid}
        self.headers = {}
        if range_header:
            self.headers["range"] = range_header
        if if_none_match:
            self.headers["if-none-match"] = if_none_match

    @property
    def url(self):
        class _U:
            path = "/music/api/v1/track/stream/x"
            query = ""
        return _U()


# ---------------------------------------------------------------- A. saved_file_path

def t01():
    assert app.saved_file_path("") == "", "空 guid 应返回空串"
case("T01 空 guid → 空串", t01)


def t02():
    clear_marker(GUID)
    assert app.saved_file_path(GUID) == "", "无标记文件应返回空串"
case("T02 无标记文件 → 空串", t02)


def t03():
    write_marker("kugou:gone", str(ROOT / "not-there.mp3"))
    assert app.saved_file_path("kugou:gone") == "", "目标文件不存在应返回空串"
case("T03 标记在但文件没了 → 空串", t03)


def t04():
    d = ROOT / "a_dir"
    d.mkdir(exist_ok=True)
    write_marker("kugou:dir", str(d))
    assert app.saved_file_path("kugou:dir") == "", "标记指向目录必须不算已存"
case("T04 标记指向目录 → 空串（旧行为会误判已存）", t04)


def t05():
    p = ROOT / "empty.mp3"
    p.write_bytes(b"")
    write_marker("kugou:empty", str(p))
    assert app.saved_file_path("kugou:empty") == "", "0 字节文件不算已存"
case("T05 0 字节文件 → 空串", t05)


def t06():
    p = make_audio("good.mp3")
    write_marker("kugou:good", p)
    assert app.saved_file_path("kugou:good") == p, "正常文件应返回路径"
case("T06 正常文件 → 返回路径", t06)


def t07():
    p = make_audio("nl.mp3")
    Path(app._save_marker_path("kugou:nl")).write_text(f"  {p}  \n\n", encoding="utf-8")
    assert app.saved_file_path("kugou:nl") == p, "标记内容应 strip 掉空白"
case("T07 标记含首尾空白 → strip 后可用", t07)


# ---------------------------------------------------------------- B. already_saved 一致性


def t08():
    p = make_audio("as.mp3")
    write_marker("kugou:as", p)
    assert app.already_saved("kugou:as") is True, "应判已存"
case("T08 already_saved 正常文件 → True", t08)


def t09():
    d = ROOT / "as_dir"
    d.mkdir(exist_ok=True)
    write_marker("kugou:asdir", str(d))
    assert app.already_saved("kugou:asdir") is False, "目录不算已存"
case("T09 already_saved 目录 → False", t09)


def t10():
    a = ROOT / "e2.mp3"
    a.write_bytes(b"")
    write_marker("kugou:e2", str(a))
    assert app.already_saved("kugou:e2") is False, "0 字节不算已存"
case("T10 already_saved 0 字节 → False", t10)


# ---------------------------------------------------------------- C. saved_file_playable


def t11():
    p = make_audio("pf.mp3")
    write_marker("kugou:pf", p)
    assert app.saved_file_playable("kugou:pf") == p, "可读文件应返回路径"
case("T11 saved_file_playable 正常 → 路径", t11)


def t12():
    assert app.saved_file_playable("kugou:nothing") == "", "无标记应回落"
case("T12 saved_file_playable 无标记 → 空串", t12)


def t13():
    p = make_audio("noread.mp3")
    os.chmod(p, 0o000)
    write_marker("kugou:noread", p)
    got = app.saved_file_playable("kugou:noread")
    os.chmod(p, 0o644)
    assert got == "", "读不出来时必须回落 CDN，而不是让播放中断"
case("T13 saved_file_playable 不可读 → 空串（回落 CDN）", t13)


# ---------------------------------------------------------------- D. serve_file_with_range


def t14():
    p = make_audio("s14.mp3")
    st, hdr, body = collect(run(app.serve_file_with_range(p, None, "audio/mpeg")))
    assert st == 200, f"无 Range 应 200，实际 {st}"
    assert hdr.get("content-length") == str(len(AUDIO)), hdr.get("content-length")
    assert body == AUDIO, "内容应与文件一致"
case("T14 无 Range → 200 全量", t14)


def t15():
    p = make_audio("s15.mp3")
    st, hdr, body = collect(run(app.serve_file_with_range(p, "bytes=0-9", "audio/mpeg")))
    assert st == 206, f"应 206，实际 {st}"
    assert hdr.get("content-range") == f"bytes 0-9/{len(AUDIO)}", hdr.get("content-range")
    assert len(body) == 10 and body == AUDIO[:10], "前 10 字节"
case("T15 Range 0-9 → 206 + Content-Range", t15)


def t16():
    p = make_audio("s16.mp3")
    st, hdr, body = collect(run(app.serve_file_with_range(p, "bytes=1000-", "audio/mpeg")))
    assert st == 206, st
    assert len(body) == len(AUDIO) - 1000, len(body)
    assert body == AUDIO[1000:], "从 1000 到结尾"
case("T16 开区间 Range → 206 到文件尾", t16)


def t17():
    p = make_audio("s17.mp3")
    st, hdr, body = collect(run(app.serve_file_with_range(p, "bytes=-5", "audio/mpeg")))
    assert st == 206, st
    assert body == AUDIO[-5:], "后缀 Range 取末尾 5 字节"
case("T17 后缀 Range → 206 取末尾", t17)


def t18():
    # 这条以前断言"越界 → 宽容 200 全量"，是错的：RFC 7233 §4.4 要求 416。
    # 危害不只是不规范 —— 播放器拖动进度条时若拿到 200，就会把整个文件从头
    # 再下一遍，几十 MB 的 flac 一下就顶满带宽。
    p = make_audio("s18.mp3")
    st, hdr, body = collect(run(app.serve_file_with_range(p, "bytes=999999-", "audio/mpeg")))
    assert st == 416, f"越界 Range 必须 416，实际 {st}"
    assert hdr.get("content-range") == f"bytes */{len(AUDIO)}", hdr.get("content-range")
    assert body == b"", "416 不应带正文"
case("T18 越界 Range → 416 + Content-Range: bytes */size", t18)


def t19():
    p = make_audio("s19.flac")
    st, hdr, _ = collect(run(app.serve_file_with_range(p, None, "audio/flac")))
    assert hdr.get("content-type") == "audio/flac", hdr.get("content-type")
    assert hdr.get("accept-ranges") == "bytes", "必须声明支持 Range"
case("T19 响应头 Content-Type / Accept-Ranges", t19)


def t20():
    p = make_audio("s20.mp3")
    st, hdr, _ = collect(run(app.serve_file_with_range(p, None, "audio/mpeg")))
    assert hdr.get("etag"), "应有 ETag（拖动/重进页面走 304）"
    assert hdr.get("last-modified"), "应有 Last-Modified"
case("T20 ETag / Last-Modified 存在", t20)


def t21():
    empty = ROOT / "zero.mp3"
    empty.write_bytes(b"")
    resp = run(app.serve_file_with_range(str(empty), None, "audio/mpeg"))
    assert resp.status_code == 404, resp.status_code
case("T21 空文件 → 404 而不是空 200", t21)


def t22():
    resp = run(app.serve_file_with_range(str(ROOT / "nope.mp3"), None, "audio/mpeg"))
    assert resp.status_code == 404, resp.status_code
case("T22 文件不存在 → 404", t22)


def t23():
    big = ROOT / "big.mp3"
    chunk = b"\x11" * (1024 * 1024)
    with open(big, "wb") as f:
        for _ in range(8):
            f.write(chunk)
    size = 8 * 1024 * 1024
    assert big.stat().st_size == size

    async def _go():
        resp = await app.serve_file_with_range(str(big), None, "audio/mpeg")
        tracemalloc.start()
        n, mx = 0, 0
        async for c in resp.body_iterator:
            n += len(c)
            mx = max(mx, len(c))
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return n, mx, peak

    n, mx, peak = run(_go())
    assert n == size, f"总字节 {n} != {size}"
    assert mx <= 256 * 1024, f"单块 {mx} 超过 256 KiB，说明没按块流式"
    assert peak < 4 * 1024 * 1024, f"峰值 {peak} 说明整文件被读进内存了"
case("T23 8 MiB 文件流式读：块 ≤256KiB、峰值 <4MiB", t23)


# ---------------------------------------------------------------- E. stream_track 集成


def t24():
    p = make_audio("st1.mp3")
    write_marker(GUID, p)
    # 让"触网"立刻炸掉：本地命中时不该有任何 CDN / info 请求
    orig_url = app.resolve_kugou_url
    orig_info = app.kugou_source.get_info

    async def boom(*a, **kw):
        raise AssertionError("本地命中却去请求酷狗 CDN")

    app.resolve_kugou_url = boom
    app.kugou_source.get_info = boom
    try:
        resp = run(app.stream_track(FakeReq(GUID)))
    finally:
        app.resolve_kugou_url = orig_url
        app.kugou_source.get_info = orig_info
    st, hdr, body = collect(resp)
    assert st == 200, st
    assert body == AUDIO, "本地文件内容应原样返回"
case("T24 命中本地文件 → 完全不触网", t24)


def t25():
    write_marker(GUID, make_audio("st2.mp3"))
    resp = run(app.stream_track(FakeReq(GUID, "bytes=4-19")))
    st, hdr, body = collect(resp)
    assert st == 206, st
    assert hdr.get("content-range", "").startswith("bytes 4-19/"), hdr.get("content-range")
    assert len(body) == 16, len(body)
case("T25 命中本地 + Range → 206", t25)


def t26():
    old = app.CONF.get("local_first")
    app.CONF["local_first"] = False
    try:
        write_marker(GUID, make_audio("st3.mp3"))
        orig_url = app.resolve_kugou_url
        orig_info = app.kugou_source.get_info
        calls = {"n": 0}

        async def info(_sid):
            return {}

        async def no_url(*a, **kw):
            calls["n"] += 1
            return "", ""

        app.kugou_source.get_info = info
        app.resolve_kugou_url = no_url
        try:
            resp = run(app.stream_track(FakeReq(GUID)))
        finally:
            app.resolve_kugou_url = orig_url
            app.kugou_source.get_info = orig_info
    finally:
        app.CONF["local_first"] = old
    assert calls["n"] == 1, f"开关关闭时应走 CDN 分支，实际调用 {calls['n']} 次"
    assert resp.status_code == 404, resp.status_code
case("T26 local_first=False → 回落到 CDN 分支", t26)


def t27():
    clear_marker(GUID)
    orig_url = app.resolve_kugou_url
    orig_info = app.kugou_source.get_info
    calls = {"n": 0}

    async def info(_sid):
        return {}

    async def no_url(*a, **kw):
        calls["n"] += 1
        return "", ""

    app.kugou_source.get_info = info
    app.resolve_kugou_url = no_url
    try:
        resp = run(app.stream_track(FakeReq(GUID)))
    finally:
        app.resolve_kugou_url = orig_url
        app.kugou_source.get_info = orig_info
    assert calls["n"] == 1, "没存过就必须走 CDN"
    assert resp.status_code == 404, resp.status_code
case("T27 未存过的歌 → 走 CDN", t27)


def t28():
    p = make_audio("st4.mp3")
    write_marker(GUID, p)
    os.unlink(p)          # 用户把文件删了，标记还在
    orig_url = app.resolve_kugou_url
    orig_info = app.kugou_source.get_info
    calls = {"n": 0}

    async def info(_sid):
        return {}

    async def no_url(*a, **kw):
        calls["n"] += 1
        return "", ""

    app.kugou_source.get_info = info
    app.resolve_kugou_url = no_url
    try:
        run(app.stream_track(FakeReq(GUID)))
    finally:
        app.resolve_kugou_url = orig_url
        app.kugou_source.get_info = orig_info
        clear_marker(GUID)
    assert calls["n"] == 1, "本地文件被删后必须回落 CDN，而不是 404"
case("T28 本地文件被删 → 回落 CDN", t28)


# ---------------------------------------------------------------- F. 配置面


def t29():
    assert "local_first" in app.CONF, "CONF 应有 local_first"
    assert isinstance(app.CONF["local_first"], bool), "应是 bool"
case("T29 CONF 含 local_first（bool）", t29)


def t30():
    assert app.CONF["local_first"] is True, "默认应为开启"
case("T30 默认开启", t30)


def t31():
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    i_local = src.find("local_path = saved_file_playable(guid)")
    i_cdn = src.find("await resolve_kugou_url(raw_song_id", i_local if i_local > 0 else 0)
    assert i_local > 0, "找不到本地优先分支"
    assert i_cdn > i_local, "本地优先分支必须出现在 CDN 解析之前"
case("T31 本地分支位于 CDN 解析之前", t31)


def t32():
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    i_mem = src.find("memory_body = get_stream_audio(guid)")
    i_local = src.find("local_path = saved_file_playable(guid)")
    i_cdn = src.find("await resolve_kugou_url(raw_song_id", i_local)
    assert 0 < i_mem < i_local < i_cdn, "顺序必须是 内存 → 本地 → CDN"
case("T32 顺序：内存 → 本地 → CDN", t32)


def t33():
    env = (PROXY / "app.py").read_text(encoding="utf-8")
    assert '"FNMUSIC_LOCAL_FIRST": ("local_first", "bool")' in env, \
        ".env 回读白名单缺 FNMUSIC_LOCAL_FIRST（写了没人读）"
case("T33 .env 回读白名单含 FNMUSIC_LOCAL_FIRST", t33)


def t34():
    html = (PKG / "app" / "web" / "settings.html").read_text(encoding="utf-8")
    assert 'id="local_first"' in html, "设置页缺开关"
    assert "'local_first='+document.getElementById('local_first').value" in html, \
        "设置页保存时没带上 local_first"
    assert "local_first').value=d.local_first?'1':'0'" in html, \
        "设置页加载时没回填 local_first"
case("T34 设置页有开关 + 加载/保存闭环", t34)


def t35():
    """生成方 ↔ 消费方接缝：安装脚本可能写出空值键，消费方不能崩。"""
    import subprocess
    code = (
        "import os,sys;"
        "os.environ['FNMUSIC_LOCAL_FIRST']='';"
        f"sys.path.insert(0,{str(PROXY)!r});"
        "import app;"
        "assert app.CONF['local_first'] is True, app.CONF['local_first'];"
        "print('OK')"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRIM_")}
    env["FNMUSIC_ENV_FILE"] = str(ROOT / "absent2.env")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"空值环境变量导致启动失败：{r.stderr[-600:]}"
    assert "OK" in r.stdout
case("T35 FNMUSIC_LOCAL_FIRST 为空值 → 回落默认开启", t35)


# ---------------------------------------------------------------- F. RFC 7233 / 7232 契合度


def t36():
    p = make_audio("r36.mp3")
    _, hdr0, _ = collect(run(app.serve_file_with_range(p, None, "audio/mpeg")))
    etag = hdr0["etag"]

    resp = run(app.serve_file_with_range(p, None, "audio/mpeg", FakeReq(GUID, if_none_match=etag)))
    assert resp.status_code == 304, resp.status_code
    assert resp.body == b"", "304 不应带正文"
    assert "content-length" not in {k.lower() for k in resp.headers}, "304 不该带 Content-Length"
    assert resp.headers.get("etag") == etag, "304 应回送 ETag"
case("T36 If-None-Match 命中 → 304 + 无正文", t36)


def t37():
    p = make_audio("r37.mp3")
    resp = run(
        app.serve_file_with_range(
            p, None, "audio/mpeg", FakeReq(GUID, if_none_match='"deadbeef-1"')
        )
    )
    assert resp.status_code == 200, f"ETag 不匹配应正常返回 200，实际 {resp.status_code}"
case("T37 If-None-Match 不匹配 → 200", t37)


def t38():
    p = make_audio("r38.mp3")
    _, hdr0, _ = collect(run(app.serve_file_with_range(p, None, "audio/mpeg")))
    etag = hdr0["etag"]
    # RFC 7232 §3.2 弱比较：W/"x" 与 "x" 等价
    resp = run(
        app.serve_file_with_range(p, None, "audio/mpeg", FakeReq(GUID, if_none_match=f"W/{etag}"))
    )
    assert resp.status_code == 304, f"弱 ETag 应视为匹配，实际 {resp.status_code}"
case("T38 W/ 弱 ETag 也匹配 → 304", t38)


def t39():
    p = make_audio("r39.mp3")
    resp = run(app.serve_file_with_range(p, None, "audio/mpeg", FakeReq(GUID, if_none_match="*")))
    assert resp.status_code == 304, f"* 应匹配任何已存在表示，实际 {resp.status_code}"
case("T39 If-None-Match: * → 304", t39)


def t40():
    p = make_audio("r40.mp3")
    st, _, body = collect(
        run(app.serve_file_with_range(p, "bytes=0-1,5-6", "audio/mpeg"))
    )
    # 多段 Range 我们不实现 multipart/byteranges ⇒ 按 §3.1 忽略 Range 回全量，
    # 绝不能"只回第一段还报 206"（那会让客户端以为拿到了它要的两段）。
    assert st == 200, f"多段 Range 应忽略并回 200，实际 {st}"
    assert body == AUDIO
case("T40 多段 Range → 忽略，200 全量", t40)


def t41():
    p = make_audio("r41.mp3")
    st, _, body = collect(run(app.serve_file_with_range(p, "items=0-5", "audio/mpeg")))
    assert st == 200, f"未知 range 单位应忽略，实际 {st}"
    assert body == AUDIO
case("T41 未知 range 单位 → 忽略，200 全量", t41)


def t42():
    p = make_audio("r42.mp3")
    st, hdr, _ = collect(run(app.serve_file_with_range(p, "bytes=-0", "audio/mpeg")))
    assert st == 416, f"bytes=-0 不可满足，应 416，实际 {st}"
    assert hdr.get("content-range") == f"bytes */{len(AUDIO)}"
case("T42 bytes=-0 → 416", t42)


def t43():
    p = make_audio("r43.mp3")
    st, hdr, _ = collect(run(app.serve_file_with_range(p, "bytes=999999-", "audio/mpeg")))
    assert st == 416
    assert hdr.get("accept-ranges") == "bytes", "416 也应声明 Accept-Ranges"
    assert hdr.get("etag"), "416 应带 ETag（便于客户端确认同一个文件）"
case("T43 416 响应头齐备（Accept-Ranges / ETag）", t43)


def t44():
    p = make_audio("r44.mp3")
    st, _, _ = collect(run(app.serve_file_with_range(p, "bytes=20-10", "audio/mpeg")))
    assert st == 416, f"start > end 应 416，实际 {st}"
case("T44 bytes=20-10（start>end）→ 416", t44)


def t45():
    etag = '"abc-1"'
    assert app.etag_matches(etag, etag) is True
    assert app.etag_matches(f'W/{etag}', etag) is True
    assert app.etag_matches(f'"x", {etag}', etag) is True
    assert app.etag_matches('"nope"', etag) is False
    assert app.etag_matches("", etag) is False
    assert app.etag_matches("*", etag) is True
case("T45 etag_matches 各分支", t45)


def t46():
    """本地命中的日志必须带 size 与 ext。"""
    body = b"ID3" + b"L" * 4096
    p = make_audio("r46.mp3", body)
    write_marker(GUID, p)
    captured = []
    orig = app.logger.info

    def spy(fmt, *a, **k):
        try:
            captured.append(fmt % a if a else fmt)
        except Exception:
            captured.append(str(fmt))
        return orig(fmt, *a, **k)

    app.logger.info = spy
    try:
        run(app.stream_track(FakeReq(GUID)))
    finally:
        app.logger.info = orig
        clear_marker(GUID)

    hit = [m for m in captured if "[LOCAL-FIRST] hit" in m]
    assert hit, f"没有 local-first hit 日志：{captured}"
    assert f"size={len(body)}" in hit[0], f"日志缺 size：{hit[0]}"
    assert "ext=mp3" in hit[0], f"日志缺 ext：{hit[0]}"
case("T46 命中日志含 size 与 ext", t46)


# ---------------------------------------------------------------- G. 内存版 / 本地版一致性


def t47():
    resp = app.serve_bytes_with_range(AUDIO, "bytes=999999-", "audio/mpeg")
    assert resp.status_code == 416, f"内存版越界 Range 也应 416，实际 {resp.status_code}"
    assert resp.headers.get("content-range") == f"bytes */{len(AUDIO)}", resp.headers.get("content-range")
case("T47 内存版越界 Range → 416", t47)


def t48():
    """同一组 Range，内存版与本地版的状态码必须逐个相同。

    这正是审计说的"时好时坏"：同一首歌，刚播过（在内存缓存里）发越界 Range
    回 200 全量，落盘后再发却回 416。用户/播放器无法理解，也无法复现。
    """
    p = make_audio("r48.mp3")
    cases = [
        None,
        "bytes=0-9",
        "bytes=10-",
        "bytes=-5",
        "bytes=0-",
        "bytes=999999-",
        "bytes=-0",
        "bytes=20-10",
        "bytes=0-1,5-6",
        "items=0-5",
        "garbage",
    ]
    diff = []
    for rh in cases:
        mem = app.serve_bytes_with_range(AUDIO, rh, "audio/mpeg").status_code
        fil = run(app.serve_file_with_range(p, rh, "audio/mpeg")).status_code
        if mem != fil:
            diff.append(f"{rh!r}: 内存={mem} 本地={fil}")
    assert not diff, "内存版与本地版行为不一致 -> " + "; ".join(diff)
case("T48 内存版与本地版对同一组 Range 状态码逐个一致", t48)


def t49():
    """内存版刻意不做 304：ETag 是内容 md5，每个请求重算一遍比省流量更贵。"""
    import inspect

    params = inspect.signature(app.serve_bytes_with_range).parameters
    assert "request" not in params, "内存版不该接 request，也就不该做 304"
    resp = app.serve_bytes_with_range(AUDIO, None, "audio/mpeg")
    assert resp.status_code == 200
    assert resp.headers.get("etag"), "内存版仍应带 ETag（给客户端缓存用）"
case("T49 内存版不做 304（签名里没有 request）", t49)


# ---------------------------------------------------------------- 汇总

print()
for n in PASS:
    print(f"  PASS  {n}")
for n, e in FAIL:
    print(f"  FAIL  {n}: {e}")
print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
