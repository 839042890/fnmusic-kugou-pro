#!/usr/bin/env python3
"""端到端验证「边播边存」真实链路（不是纯函数，而是走真正的 HTTP 路由）。

链路：
    浏览器 GET /music/api/v1/track/stream?guid=online:kugou:<hash>
      -> app.stream_track()
      -> kugou_source.get_info() / resolve_url()   （打假 KuGouMusicApi）
      -> app.stream_archive_response()             （边转发边攒字节）
      -> app._finalize_archive_meta()              （补歌词/封面）
      -> app.archive_online_track_async()          （落盘）
      -> 曲库目录里出现音频 + .lrc + 封面

用 httpx.ASGITransport 直接驱动 ASGI app，不需要真的 uvicorn/nginx/socket；
假 KuGouMusicApi 用同一个 event loop 里的本地 HTTP 服务（或直接 monkeypatch
kugou_source 的底层请求）来提供 /privilege/lite、/song/url、/search/lyric。

PYTHONPATH= /tmp/testvenv/bin/python tests/test_pipeline.py
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-e2e-"))
LIB = ROOT / "music_lib"
LIB.mkdir(parents=True, exist_ok=True)

DB = ROOT / "music.db"
_con = sqlite3.connect(DB)
_con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
_con.execute("INSERT INTO shared_library (path) VALUES (?)", (str(LIB),))
_con.commit()
_con.close()

SAVE_DIR = LIB / "online"
os.environ.update({
    "FNMUSIC_MUSIC_DB": str(DB),
    "FNMUSIC_LIBRARY_DIR": "",
    "FNMUSIC_SAVE_ENABLED": "1",
    "FNMUSIC_SAVE_DIR": str(SAVE_DIR),
    "FNMUSIC_SAVE_WRITE_TAGS": "1",
    "FNMUSIC_SAVE_WRITE_COVER": "1",
    "FNMUSIC_SAVE_WRITE_LYRIC": "1",
    "FNMUSIC_SAVE_MARKER_DIR": str(ROOT / "markers"),
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "1",
    "FNMUSIC_APP_MODE": "",
    # P-02：真机上有 /vol1/@appconf/<app>/.env，app.py 导入期会读它并覆盖
    # 上面这些变量（写标签/封面/歌词被压成 0）→ 6 项"假红"。钉到不存在的文件。
    "FNMUSIC_ENV_FILE": str(ROOT / "absent.env"),
})

sys.path.insert(0, str(PROXY))

# ---------------------------------------------------------------- 假音源素材
SONG_HASH = "a" * 32
TITLE = "晴天"
ARTIST = "周杰伦"
ALBUM = "叶惠美"
LYRIC_TEXT = "[00:01.00]故事的小黄花\n[00:05.00]从出生那年就飘着\n"

AUDIO = ROOT / "src.flac"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:a", "flac", str(AUDIO)], check=True)
AUDIO_BYTES = AUDIO.read_bytes()

COVER = ROOT / "cover.jpg"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=orange:s=300x300:d=1",
                "-frames:v", "1", str(COVER)], check=True)
COVER_BYTES = COVER.read_bytes()


class FakeKugou(BaseHTTPRequestHandler):
    """最小 KuGouMusicApi：只实现本链路会打到的三个接口。"""

    def log_message(self, *a):  # 静音
        pass

    def _json(self, obj, code=200):
        import json
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/privilege/lite":
            if q.get("hash", [""])[0] != SONG_HASH:
                return self._json({"status": 0}, 200)
            # 字段对齐酷狗真实回参：name(不是 songname) + singerinfo[] + albuminfo{}
            # （kugou_source.track_item_to_raw 走的就是这条路径）
            return self._json({
                "status": 1,
                "data": {
                    "hash": SONG_HASH,
                    "name": TITLE,
                    "singerinfo": [{"name": ARTIST, "id": 123}],
                    "albuminfo": {"name": ALBUM, "id": 456},
                    "timelen": 2000,
                    "extname": "flac",
                    "size": len(AUDIO_BYTES),
                    "bitrate": 1411,
                    "cover": f"http://127.0.0.1:{PORT}/cover.jpg",
                    "info": {"image": f"http://127.0.0.1:{PORT}/cover.jpg",
                             "extname": "flac", "timelen": "2000"},
                    "relate_goods": [{"quality": "flac", "suffix": "flac",
                                      "filesize": len(AUDIO_BYTES), "bitrate": 1411}],
                },
            })
        if u.path == "/lyric":
            return self._json({"status": 1, "decodeContent": LYRIC_TEXT})
        if u.path == "/song/url":
            return self._json({
                "status": 1,
                "url": [f"http://127.0.0.1:{PORT}/audio.flac"],
                "extName": "flac",
                "fileSize": len(AUDIO_BYTES),
            })
        if u.path == "/search/lyric":
            return self._json({
                "status": 1,
                "candidates": [{"id": 1, "accesskey": "k", "lyric": LYRIC_TEXT}],
            })
        if u.path == "/login/qr/key":
            return self._json({"status": 1, "data": {"qrcode": "x"}})
        if u.path == "/cover.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(COVER_BYTES)))
            self.end_headers()
            self.wfile.write(COVER_BYTES)
            return
        if u.path == "/audio.flac":
            # 像真 CDN 一样支持 Range：否则 seek 请求会被当成 200 全量返回，
            # 测不出"Range 请求不被归档"。
            rng = self.headers.get("Range") or self.headers.get("range")
            total = len(AUDIO_BYTES)
            if rng and rng.startswith("bytes="):
                spec = rng[len("bytes="):].split(",")[0].strip()
                a, _, b = spec.partition("-")
                start = int(a) if a else 0
                end = int(b) if b else total - 1
                end = min(end, total - 1)
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                part = AUDIO_BYTES[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "audio/flac")
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(len(part)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(part)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/flac")
            self.send_header("Content-Length", str(total))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(AUDIO_BYTES)
            return
        return self._json({"status": 0}, 404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeKugou)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["FNMUSIC_KUGOU_URL"] = f"http://127.0.0.1:{PORT}"

import app  # noqa: E402
import kugou_source  # noqa: E402

kugou_source.set_config({"kugou_url": f"http://127.0.0.1:{PORT}", "kugou_enabled": True})
app.CONF["kugou_url"] = f"http://127.0.0.1:{PORT}"
app.CONF["kugou_enabled"] = True

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def _swap_track(raw: bytes, song_hash: str) -> None:
    """把假 KuGou 换成另一首曲目：改模块级全局名并推进 kugou_source 索引。

    main() 里的局部变量 AUDIO_BYTES 是读值快照，重绑定模块全局不影响它，
    所以这里只用显式赋值，不用 global。
    """
    g = globals()
    g["AUDIO_BYTES"] = raw
    g["SONG_HASH"] = song_hash
    kugou_source._search_index[song_hash] = {
        "id": f"kugou:{song_hash}", "hash": song_hash,
        "title": "长曲", "artist": "某歌手", "album": "某专辑",
        "duration_s": 180, "ext": "flac", "source": "kugou",
        "file_size": len(raw), "bitrate": 1411,
        "union_cover": f"http://127.0.0.1:{PORT}/cover.jpg",
        "_matched_tier": {"quality": "flac", "suffix": "flac",
                          "bitrate": 1411, "size": len(raw)},
    }


async def main():
    import httpx

    guid = f"online:kugou:{SONG_HASH}"
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("=" * 70)
        print("1) 完整播放 -> 边播边存归档（真实路由 /music/api/v1/track/stream）")
        print("=" * 70)
        r = await client.get(f"/music/api/v1/track/stream?guid={guid}")
        check("stream 返回 200", r.status_code == 200, f"{r.status_code}")
        got = r.content
        check("客户端收到的音频与原文件字节一致", got == AUDIO_BYTES,
              f"{len(got)} vs {len(AUDIO_BYTES)}")
        check("响应带 Content-Type audio/flac", "audio/flac" in r.headers.get("content-type", ""),
              r.headers.get("content-type", ""))

        # 归档是后台 task，等它落盘（最多 15s）
        dests = []
        for _ in range(150):
            await asyncio.sleep(0.1)
            dests = [p for p in SAVE_DIR.glob("*.flac")] if SAVE_DIR.exists() else []
            if dests:
                break
        check("曲库里出现了归档音频", bool(dests),
              str(list(SAVE_DIR.iterdir())) if SAVE_DIR.exists() else "目录不存在")
        if not dests:
            return
        dest = dests[0]
        print(f"  归档文件: {dest}")

        print()
        print("=" * 70)
        print("2) 文件名 / 歌词 / 封面 sidecar")
        print("=" * 70)
        check("文件名为「歌手 - 歌名」", dest.stem == f"{ARTIST} - {TITLE}", dest.name)
        check("落盘目录就是配置的目录", dest.parent == SAVE_DIR, str(dest.parent))
        lrc = dest.with_suffix(".lrc")
        check("生成了同名 .lrc", lrc.exists())
        if lrc.exists():
            check(".lrc 内容与音源一致", "故事的小黄花" in lrc.read_text(encoding="utf-8"))
        imgs = [p for p in SAVE_DIR.glob("*.jpg")] + [p for p in SAVE_DIR.glob("*.png")]
        check("生成了同名封面图", bool(imgs), str(list(SAVE_DIR.iterdir())))

        print()
        print("=" * 70)
        print("3) 标签（用 mutagen 独立读回验证）")
        print("=" * 70)
        from mutagen import File as MF
        audio = MF(str(dest), easy=True)
        tags = audio.tags if audio else None
        check("title 标签", bool(tags) and str(tags.get("title", [""])[0]) == TITLE,
              str(dict(tags or {})))
        check("artist 标签", bool(tags) and str(tags.get("artist", [""])[0]) == ARTIST)
        check("album 标签", bool(tags) and str(tags.get("album", [""])[0]) == ALBUM)
        raw = MF(str(dest))
        check("flac 内嵌了封面", bool(getattr(raw, "pictures", None)),
              str(raw.pictures if raw else None))
        check("音频可正常解码（时长 > 0）",
              bool(raw and getattr(raw.info, "length", 0) > 0),
              str(getattr(raw.info, "length", None)) if raw else "None")

        print()
        print("=" * 70)
        print("4) 再次完整播放：不重复归档")
        print("=" * 70)
        before = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        r2 = await client.get(f"/music/api/v1/track/stream?guid={guid}")
        # F-02（1.0.2）起第二次播放会命中「本地优先」，读的是磁盘上那份归档
        # 文件；归档时写过标签，字节与原 CDN 流不同（本用例里长度恰好相同，
        # 但内容不等）。所以不能再拿 AUDIO_BYTES 比，改比磁盘上那份文件 ——
        # 这同时也正面证明了确实走了本地优先，没有再去请求酷狗。
        archived = sorted(SAVE_DIR.glob("*.flac"))
        check("第二次播放仍正常返回", r2.status_code == 200 and len(r2.content) > 0)
        check(
            "第二次播放读的是本地归档文件（本地优先）",
            bool(archived) and r2.content == archived[0].read_bytes(),
            f"archived={len(archived)}",
        )
        await asyncio.sleep(3)
        after = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        check("没有产生重复文件", before == after, f"{before} -> {after}")

        print()
        print("=" * 70)
        print("5) 关闭开关后不再归档")
        print("=" * 70)
        app.CONF["save_enabled"] = False
        guid2 = f"online:kugou:{'b' * 32}"
        r3 = await client.get(f"/music/api/v1/track/stream?guid={guid2}")
        check("关闭时播放不受影响", r3.status_code == 200, str(r3.status_code))
        await asyncio.sleep(2.5)
        flacs = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        check("关闭后没有新增归档", flacs == after, f"{after} -> {flacs}")

        # ============================================================
        # 7) 中途切歌：只读一小部分就断开，不能把半首歌当整曲归档
        # ============================================================
        print()
        print("=" * 70)
        print("7) 中途切歌/拖动：只读一部分就断开，不应归档半首歌")
        print("=" * 70)
        app.CONF["save_enabled"] = True
        # 换一首长音频，才有"只读一半"的空间。FakeKugou 读的就是 AUDIO_BYTES /
        # SONG_HASH 这两个模块级全局名，用 _swap_track 重绑定即可让它换曲。
        big_path = ROOT / "big.flac"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=180",
                        "-c:a", "flac", str(big_path)], check=True)
        big_raw = big_path.read_bytes()
        _swap_track(big_raw, "f" * 32)
        big_guid = f"online:kugou:{SONG_HASH}"
        before_big = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        # 这一段必须走真实 socket：ASGITransport 会把整个 ASGI app 跑完才把
        # 响应交给客户端，客户端"中途 break"根本传不回服务端，模拟不出切歌。
        got, _ = await _play_over_real_socket(big_guid, big_raw, stop_after_ratio=0.10)
        check("长曲 stream 返回 200", got is not None, "真实 socket 请求失败")
        if got is not None:
            print(f"  只读了 {got}/{len(big_raw)} 字节（{100*got/len(big_raw):.1f}%）后断开")
        await asyncio.sleep(3.0)                  # 等后台 pump 收尾
        after_big = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        newly = sorted(set(after_big) - set(before_big))
        if newly:
            f = SAVE_DIR / newly[0]
            print(f"  ⚠ 新增归档 {newly[0]} 大小={f.stat().st_size} 原始={len(big_raw)}")
        check("中途断开未归档半首歌", newly == [], str(newly))

        # ============================================================
        # 7b) 真实 socket 完整播放：仍要正常归档（防止上一条修过头）
        # ============================================================
        print()
        print("=" * 70)
        print("7b) 真实 socket 下完整读完 -> 照常归档")
        print("=" * 70)
        before_full = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        got_full, body_full = await _play_over_real_socket(big_guid, big_raw)
        check("真实 socket 完整播放读到全部字节",
              got_full == len(big_raw), f"{got_full} vs {len(big_raw)}")
        check("真实 socket 收到的字节与原文件一致", body_full == big_raw)
        for _ in range(60):                       # 归档是后台任务，轮询等落盘
            await asyncio.sleep(0.5)
            if set(p.name for p in SAVE_DIR.glob("*.flac")) - set(before_full):
                break
        full_new = sorted(set(p.name for p in SAVE_DIR.glob("*.flac")) - set(before_full))
        check("真实 socket 完整播放后完成归档", full_new != [], "未归档")
        if full_new:
            f = SAVE_DIR / full_new[0]
            print(f"  归档 {full_new[0]} 大小={f.stat().st_size} 原始={len(big_raw)}")
            check("归档文件大小与原文件一致", f.stat().st_size == len(big_raw))
            # 不能直接比整个文件字节：写标签/内嵌封面会把 FLAC 的 metadata 块
            # 改掉。真正该保证的是"音频帧一字节没变"，用解码后的 PCM 比对。
            check("归档音频帧与原文件一致（解码 PCM 比对）",
                  _decoded_pcm(f) == _decoded_pcm(big_path))
        after_big = sorted(p.name for p in SAVE_DIR.glob("*.flac"))

        # ============================================================
        # 8) Range 请求（拖动进度条 / seek）不归档
        # ============================================================
        print()
        print("=" * 70)
        print("8) Range 请求（拖动进度条 / seek）不归档")
        print("=" * 70)
        r = await client.get(f"/music/api/v1/track/stream?guid={big_guid}",
                             headers={"Range": "bytes=0-1023"})
        check("Range 请求返回 206", r.status_code == 206, str(r.status_code))
        check("Range 请求只回请求的那一段", len(r.content) == 1024, str(len(r.content)))
        await asyncio.sleep(2.0)
        check("Range 请求不归档",
              sorted(p.name for p in SAVE_DIR.glob("*.flac")) == after_big)
        # 再验一次：内存缓存命中路径（第二次请求同一 guid）也不该归档
        r2 = await client.get(f"/music/api/v1/track/stream?guid={big_guid}",
                              headers={"Range": "bytes=0-1023"})
        check("Range 请求（缓存命中路径）返回 206", r2.status_code == 206, str(r2.status_code))
        await asyncio.sleep(1.5)
        check("Range 请求（缓存命中路径）不归档",
              sorted(p.name for p in SAVE_DIR.glob("*.flac")) == after_big)

        # ============================================================
        # 9) 分块 Range 顺序读完 -> 归档（v2.1.5 修复的正是这条）
        #    飞牛【音乐】播放器是"分块随机读取"引擎，永远发
        #    Range: bytes=<start>-<end>，从不发 bytes=0-。2.1.4 及以前用
        #    should_cache 判定"只有无 Range 或 bytes=0- 才归档"，于是
        #    边播边存一条都不触发（用户实测"放了两首歌没存下来"）。
        # ============================================================
        print()
        print("=" * 70)
        print("9) 分块 Range 顺序读完 -> 归档（本次修复的核心回归）")
        print("=" * 70)
        big2_path = ROOT / "chunked.flac"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=520:duration=150",
                        "-c:a", "flac", str(big2_path)], check=True)
        big2_raw = big2_path.read_bytes()
        _swap_track(big2_raw, "d" * 32)
        ck_guid = f"online:kugou:{SONG_HASH}"
        total = len(big2_raw)
        print(f"  曲目大小 {total} 字节")
        before_ck = sorted(p.name for p in SAVE_DIR.glob("*.flac"))

        # 9a) 只读前 40% 的分块：绝不能归档（用户可能听到一半就切歌）
        n_chunks = 8
        step = total // n_chunks
        boundaries = [(i * step, min((i + 1) * step - 1, total - 1))
                      for i in range(n_chunks)]
        for start, end in boundaries[:3]:
            rr = await client.get(f"/music/api/v1/track/stream?guid={ck_guid}",
                                  headers={"Range": f"bytes={start}-{end}"})
            check(f"分块 bytes={start}-{end} 返回 206", rr.status_code == 206,
                  str(rr.status_code))
        await asyncio.sleep(2.5)
        after_partial = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        check("只读前 3/8 块：不归档（没听完不能存）",
              not (set(after_partial) - set(before_ck)),
              str(sorted(set(after_partial) - set(before_ck))))

        # 9b) 接着把剩下的块读完：覆盖整曲 -> 必须归档
        for start, end in boundaries[3:]:
            rr = await client.get(f"/music/api/v1/track/stream?guid={ck_guid}",
                                  headers={"Range": f"bytes={start}-{end}"})
            check(f"分块 bytes={start}-{end} 返回 206", rr.status_code == 206,
                  str(rr.status_code))
        got_new = []
        for _ in range(80):                        # 补拉+落盘是后台任务
            await asyncio.sleep(0.5)
            got_new = sorted(set(p.name for p in SAVE_DIR.glob("*.flac")) - set(before_ck))
            if got_new:
                break
        check("分块读完整曲 -> 归档（v2.1.4 此处一条都不存）", got_new != [],
              f"未归档；目录={sorted(p.name for p in SAVE_DIR.glob('*.flac'))}")
        if got_new:
            f = SAVE_DIR / got_new[0]
            print(f"  归档 {got_new[0]} 大小={f.stat().st_size} 原始={total}")
            check("分块路径归档的文件完整（大小一致）",
                  f.stat().st_size == total, f"{f.stat().st_size} vs {total}")
            check("分块路径归档的音频帧与原文件一致（解码 PCM 比对）",
                  _decoded_pcm(f) == _decoded_pcm(big2_path))

        # 9c) 同一首已归档后再分块读：不重复落盘
        after_ck = sorted(p.name for p in SAVE_DIR.glob("*.flac"))
        for start, end in boundaries[:2]:
            await client.get(f"/music/api/v1/track/stream?guid={ck_guid}",
                             headers={"Range": f"bytes={start}-{end}"})
        await asyncio.sleep(2.0)
        check("已归档曲目再分块读：不重复落盘",
              sorted(p.name for p in SAVE_DIR.glob("*.flac")) == after_ck)


def _decoded_pcm(path: Path) -> bytes:
    """解码成裸 PCM 再比，绕过容器元数据差异。"""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2",
         "-ar", "44100", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    return out


async def _play_over_real_socket(
    guid: str, raw: bytes, stop_after_ratio: float | None = None
) -> tuple[int | None, bytes]:
    """用真实 HTTP socket 播放，返回 (读到的字节数, 收到的字节)。

    stop_after_ratio 给定时，读到该比例就断开（模拟切歌/拖动）；None 表示读完。

    为什么非得起 uvicorn：ASGITransport 是把整个 ASGI app 跑完、拿到完整
    响应体之后才返回给客户端的，客户端侧 break 无法反压到服务端，也就测不出
    "客户端断开 → 后台 pump 该被取消"。真实 socket 才会触发 Starlette 关闭
    StreamingResponse 生成器，从而走到 _stream() 的 finally。
    """
    import httpx
    import uvicorn

    conf = uvicorn.Config(app.app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(conf)
    sock = conf.bind_socket()
    port = sock.getsockname()[1]
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)

    got = 0
    buf = bytearray()
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            async with c.stream(
                "GET", f"http://127.0.0.1:{port}/music/api/v1/track/stream?guid={guid}"
            ) as resp:
                if resp.status_code != 200:
                    return None, b""
                async for chunk in resp.aiter_bytes(8192):
                    got += len(chunk)
                    buf += chunk
                    if stop_after_ratio is not None and got > len(raw) * stop_after_ratio:
                        break
    finally:
        server.should_exit = True
        for _ in range(50):
            if not t.is_alive():
                break
            await asyncio.sleep(0.1)
    return got, bytes(buf)


try:
    asyncio.run(main())
finally:
    srv.shutdown()

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
