#!/usr/bin/env python3
"""2.1.6 回归：超大曲目归档、超时可配置、标记文件权限、归档后释放缓存。

这几条都是「静默失效」类 bug —— 功能看着在、配置项看着生效、日志里却一条
都不打，用户只能看到"放了几首歌一首都没存下来"。所以本文件不只断言行为，
还断言**日志里有痕**：静默失效最难查，留痕是这类修复的一半价值。

覆盖：
  1) >内存缓存上限的曲目，走"调用方直传字节"路径仍能归档（P0-1 核心）
  2) 谁都没把字节送进来时，必须打日志说明，不能再静默 return（P0-1）
  3) 内存缓存的 oversize 分支必须打日志（P0-1）
  4) 归档用的网络超时取自 CONF["save_timeout_s"]，不是写死的字面量（P1）
  5) 标记文件权限显式对齐 0644，不依赖进程 umask / 文件系统继承（P2）
  6) 归档成功后释放内存缓存（P2）

运行：PYTHONPATH= /tmp/tv3/bin/python tests/test_oversize_archive.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-oversize-"))
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
    # 故意不设 FNMUSIC_SAVE_MARKER_DIR：要走的正是"跟随落盘目录自动派生"
    # 那条真实路径（含 adopt_library_dir_perms / adopt_library_perms），
    # 显式指定会把它短路掉，测不到要测的东西。
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "0",
    # 持久目录必须指到临时目录：本机 /var/apps/fnmusic_kugou_pro/{etc,var,home}
    # 符号链接真实存在，不设就会去写系统里的 /vol1/@appconf/... —— 既是测试污染，
    # 又会被权限拒绝成假红。
    "FNMUSIC_ETC_DIR": str(ROOT / "fnmusic_kugou_pro" / "etc"),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROXY))
import app  # noqa: E402

# 不让归档路径去打网络：歌词/封面的取用逻辑不是本文件要测的东西。
app.CONF["save_write_lyric"] = False
app.CONF["save_write_cover"] = False

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


class LogCapture(logging.Handler):
    """抓 fnmusic_proxy 的日志，用于断言"不再静默"。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record.getMessage())
        except Exception:
            pass

    def clear(self) -> None:
        self.records.clear()

    def joined(self) -> str:
        return "\n".join(self.records)


_cap = LogCapture()
_log = logging.getLogger("fnmusic_proxy")
_log.addHandler(_cap)
_log.setLevel(logging.DEBUG)


def _body(n: int, magic: bytes = b"fLaC") -> bytes:
    """造一段带正确文件头的字节，n 为总长。"""
    return magic + b"\x00" * (n - len(magic))


# 用"改小阈值"代替真的造 48 MB 文件：阈值是配置量，逻辑边界与绝对值无关，
# 48 MB 只是默认值。这样测试跑得飞快，也不会在 /tmp 里堆大文件。
app._STREAM_CACHE_MAX_FILE_BYTES = 4096
app._STREAM_CACHE_MAX_BYTES = 1 << 20
app._STREAM_CACHE_MAX_ENTRIES = 8

BIG = 8192          # > 4096，模拟"高音质 FLAC"
SMALL = 2048        # < 4096，模拟"能进缓存的普通曲目"

print("=" * 70)
print("1) 超大曲目：直传字节仍能归档（P0-1 核心）")
print("=" * 70)
app._STREAM_CACHE.clear()
_cap.clear()

g_big = "online:kugou:" + "A" * 32
body_big = _body(BIG)
info_big = {"title": "超大曲目", "artist": "测试歌手", "album": "专辑",
            "ext": "flac", "duration_s": 300}

# 先按"客户端边播边存"的真实顺序：字节只经过调用方，不进内存缓存
dest = asyncio.run(app._finalize_archive_meta(g_big, info_big, body=body_big, ext_hint="flac"))
files = sorted(p.name for p in SAVE_DIR.glob("*.flac")) if SAVE_DIR.exists() else []
check(f">缓存阈值的曲目（{BIG}B > {app._STREAM_CACHE_MAX_FILE_BYTES}B）成功落盘",
      bool(files), f"目录内容={files}")
if files:
    f = SAVE_DIR / files[0]
    check("落盘文件字节数与直传的 body 一致", f.stat().st_size == BIG,
          f"{f.stat().st_size} vs {BIG}")
    check("落盘文件带正确扩展名 .flac", f.suffix == ".flac", f.name)
    check("文件名是「歌手 - 歌名」", f.stem == "测试歌手 - 超大曲目", f.stem)
    check("落盘文件权限 0644（飞牛可扫描）",
          (f.stat().st_mode & 0o777) == 0o644, oct(f.stat().st_mode & 0o777))

# 关键反证：这个 guid 从头到尾没进过内存缓存，说明归档不再依赖缓存
check("该曲目从未进入内存缓存（归档不经缓存这条闸门）",
      g_big not in app._STREAM_CACHE, str(list(app._STREAM_CACHE.keys())))
check("直传路径没有打「无字节可归档」的告警",
      "finalize 无字节可归档" not in _cap.joined(), _cap.joined()[-300:])

print()
print("=" * 70)
print("2) 谁都没送字节进来：必须留日志，不能静默 return（P0-1）")
print("=" * 70)
app._STREAM_CACHE.clear()
_cap.clear()
g_lost = "online:kugou:" + "B" * 32
before = sorted(p.name for p in SAVE_DIR.glob("*.flac")) if SAVE_DIR.exists() else []
asyncio.run(app._finalize_archive_meta(g_lost, info_big))       # 无 body、缓存也空
after = sorted(p.name for p in SAVE_DIR.glob("*.flac")) if SAVE_DIR.exists() else []
check("无字节时不落盘", before == after, f"{before} -> {after}")
check("无字节时留下告警日志（不再静默）",
      "无字节可归档" in _cap.joined(), _cap.joined()[-300:])
check("告警里带上了 guid 便于定位",
      g_lost in _cap.joined(), _cap.joined()[-300:])

print()
print("=" * 70)
print("3) 内存缓存的 oversize 分支必须留日志（P0-1）")
print("=" * 70)
app._STREAM_CACHE.clear()
_cap.clear()
g_cache = "online:kugou:" + "C" * 32
app.remember_stream_audio(g_cache, _body(BIG), "flac")
check("超限不入缓存", g_cache not in app._STREAM_CACHE, str(list(app._STREAM_CACHE.keys())))
check("超限时留有 skip oversize 日志",
      "skip oversize" in _cap.joined(), _cap.joined()[-300:])
check("日志里看得出只影响内存缓存、归档走直传",
      "归档走直传" in _cap.joined(), _cap.joined()[-300:])

_cap.clear()
app.remember_stream_audio(g_cache, _body(SMALL), "flac")
check("阈值内的曲目正常进缓存", g_cache in app._STREAM_CACHE)
check("阈值内不打 oversize 日志", "skip oversize" not in _cap.joined())

print()
print("=" * 70)
print("4) 归档网络超时取自 CONF[save_timeout_s]，不是写死的 60（P1）")
print("=" * 70)
_src = (PROXY / "app.py").read_text(encoding="utf-8")

# 4a) 行为：改了配置，函数返回值必须跟着变
_saved_tmo = app.CONF.get("save_timeout_s")
try:
    app.CONF["save_timeout_s"] = 300.0
    check("默认 300 -> 300.0", app.save_timeout_s() == 300.0, str(app.save_timeout_s()))
    app.CONF["save_timeout_s"] = 45
    check("改成 45 -> 45.0（配置真的生效）", app.save_timeout_s() == 45.0,
          str(app.save_timeout_s()))
    app.CONF["save_timeout_s"] = "60"
    check("字符串 '60' 也能转（.env 回读是字符串）",
          app.save_timeout_s() == 60.0, str(app.save_timeout_s()))
    app.CONF["save_timeout_s"] = 0
    check("0 回退默认 300（不能让超时变成 0 直接全超时）",
          app.save_timeout_s() == 300.0, str(app.save_timeout_s()))
    app.CONF["save_timeout_s"] = -5
    check("负数回退默认 300", app.save_timeout_s() == 300.0, str(app.save_timeout_s()))
    app.CONF["save_timeout_s"] = None
    check("None 回退默认 300", app.save_timeout_s() == 300.0, str(app.save_timeout_s()))
    app.CONF["save_timeout_s"] = "abc"
    check("非法值回退默认 300 且不抛异常", app.save_timeout_s() == 300.0,
          str(app.save_timeout_s()))
finally:
    app.CONF["save_timeout_s"] = _saved_tmo

# 4b) 静态：补拉归档的那个 httpx 客户端不能再出现字面量超时
_m = re.search(r"async def _fetch_full_and_archive.*?(?=\nasync def |\ndef )",
               _src, re.S)
check("找到 _fetch_full_and_archive", _m is not None)
if _m:
    seg = _m.group(0)
    check("补拉归档的 httpx 超时来自 save_timeout_s()",
          "timeout=save_timeout_s()" in seg,
          [l.strip() for l in seg.splitlines() if "AsyncClient(" in l])
    check("补拉归档里已无写死的 timeout=60.0",
          "timeout=60.0" not in seg,
          [l.strip() for l in seg.splitlines() if "timeout=" in l])
check("全包已无 timeout=60.0 字面量", "timeout=60.0" not in _src)

# 4c) save_timeout_s 必须是 CONF 的读取口，且 CONF 里有该键
check("CONF 里定义了 save_timeout_s 键", "save_timeout_s" in app.CONF,
      str(sorted(k for k in app.CONF if "timeout" in k)))
check("save_timeout_s() 读的是 CONF",
      'CONF.get("save_timeout_s")' in _src)
check("回读白名单含 FNMUSIC_SAVE_TIMEOUT_S（否则重启丢设置）",
      '"FNMUSIC_SAVE_TIMEOUT_S"' in _src)
# 防回归：别在读取处写裸 int()/float()(os.environ...)。
# 必须先把注释剥掉 —— 文件里恰好有一行注释在讲"不要这么写"，
# 不剥的话这条断言永远假红，反而会被人当成噪音忽略掉。
_code_lines = []
for _l in _src.splitlines():
    _l = _l.split("#", 1)[0] if not _l.lstrip().startswith("#") else ""
    _code_lines.append(_l)
_code_only = "\n".join(_code_lines)
check("无裸 (int|float)(os.environ...)", not re.search(r"(int|float)\(\s*os\.environ", _code_only),
      str([l.strip() for l in _code_lines
           if re.search(r"(int|float)\(\s*os\.environ", l)]))

print()
print("=" * 70)
print("5) 标记文件权限显式对齐 0644（P2）")
print("=" * 70)
# 飞牛 /vol1 是 btrfs + trimacl：新建文件直接照抄父目录的权限位，
# umask 与 open() 请求的 mode 都被忽略（本机实测 父755->755、父700->700），
# 只有显式 os.chmod 才生效。所以这里必须断言"显式 chmod 过后的结果"。
_marker_dir = Path(app.resolve_marker_dir())
_marker_dir.mkdir(parents=True, exist_ok=True)
check("标记目录权限 0755（目录不能用 0644，否则自己写不进去）",
      (_marker_dir.stat().st_mode & 0o777) == 0o755,
      oct(_marker_dir.stat().st_mode & 0o777))

g_mark = "online:kugou:" + "D" * 32
_marker_path = Path(app._save_marker_path(g_mark))
app.mark_saved(g_mark, str(SAVE_DIR / "dummy.flac"))
check("标记文件已写出", _marker_path.exists(), str(_marker_path))
if _marker_path.exists():
    _mode = _marker_path.stat().st_mode & 0o777
    check("标记文件权限 0644（不带动其他位，与音频/歌词/封面一致）",
          _mode == 0o644, oct(_mode))
    check("标记内容是可读的绝对路径",
          _marker_path.read_text(encoding="utf-8").strip() == str(SAVE_DIR / "dummy.flac"))
    check("过程中没留下 .part 临时文件",
          not list(_marker_dir.glob("*.part")), str(list(_marker_dir.iterdir())))
# 静态佐证：mark_saved 里必须出现 adopt_library_perms
_mm = re.search(r"def mark_saved.*?(?=\ndef )", _src, re.S)
check("mark_saved 里显式调用 adopt_library_perms",
      bool(_mm) and "adopt_library_perms" in _mm.group(0))

print()
print("=" * 70)
print("6) 归档成功后释放内存缓存（P2）")
print("=" * 70)
app._STREAM_CACHE.clear()
_cap.clear()
g_rel = "online:kugou:" + "E" * 32
body_rel = _body(SMALL)
app.remember_stream_audio(g_rel, body_rel, "flac")
check("归档前曲目在缓存里", g_rel in app._STREAM_CACHE)
asyncio.run(app._finalize_archive_meta(g_rel, {"title": "释放测试", "artist": "歌手",
                                              "ext": "flac"}, body=body_rel))
check("归档后缓存已释放（字节已落盘，不必再占 RAM）",
      g_rel not in app._STREAM_CACHE, str(list(app._STREAM_CACHE.keys())))
check("释放动作留有 released 日志", "released" in _cap.joined(), _cap.joined()[-300:])
_rel_files = [p for p in SAVE_DIR.glob("*.flac")] if SAVE_DIR.exists() else []
check("归档本身成功（不是靠失败顺带清掉缓存）", bool(_rel_files),
      str(sorted(p.name for p in _rel_files)))

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
