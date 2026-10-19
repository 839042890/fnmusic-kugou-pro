#!/usr/bin/env python3
"""边播边存补丁的测试。

覆盖：格式嗅探、文件名清洗、落点校验（含路径穿越/符号链接逃逸）、
端到端归档（落盘+标签+歌词+封面+标记+去重）、.env 写入的安全性与权限。

运行：PYTHONPATH= /tmp/testvenv/bin/python tests/test_save.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-test-"))
LIB = ROOT / "music_lib"          # 模拟飞牛共享曲库根
LIB.mkdir(parents=True, exist_ok=True)
OUTSIDE = ROOT / "outside"        # 曲库之外
OUTSIDE.mkdir(parents=True, exist_ok=True)

# 假 music.db：shared_library.path 指向我们的库根
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
    "FNMUSIC_SAVE_WRITE_TAGS": "1",
    "FNMUSIC_SAVE_WRITE_COVER": "1",
    "FNMUSIC_SAVE_WRITE_LYRIC": "1",
    "FNMUSIC_SAVE_MARKER_DIR": str(ROOT / "markers"),
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "0",
    # 持久目录指向临时目录。不设的话 _ETC_DIR 会解析到真实的
    # /var/apps/fnmusic_kugou_pro/etc（本机存在这个符号链接），测试就会
    # 去写系统里的 /vol1/@appconf/... —— 既污染机器又因权限失败而假红。
    "FNMUSIC_ETC_DIR": str(ROOT / "fnmusic_kugou_pro" / "etc"),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    # 宿主可能注入 TRIM_PKGETC 等（调用方应用的上下文，可能指向别的应用），
    # 清掉以免盖过上面的临时目录。
    # P-02：把 .env 钉到不存在的文件。真机 /vol1/@appconf/<app>/.env 存在时，
    # app.py 导入期会读它并覆盖上面设的变量 → 测试在真机"假红"。
    "FNMUSIC_ENV_FILE": str(ROOT / "absent.env"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROXY))
import app  # noqa: E402

# .env 的实际落点由 app.resolve_env_file() 决定（配置目录优先），不要写死路径。
ENV_FILE = app.resolve_env_file(create=True)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def make_audio(fmt: str, path: Path) -> bytes:
    """用 ffmpeg 生成真实音频，返回字节。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    elif fmt == "flac":
        cmd += ["-c:a", "flac"]
    elif fmt == "ogg":
        cmd += ["-c:a", "libvorbis"]
    elif fmt == "wav":
        cmd += ["-c:a", "pcm_s16le"]
    elif fmt == "m4a":
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + [str(path)], check=True)
    return path.read_bytes()


print("=" * 70)
print("1) 格式嗅探 sniff_audio_ext")
print("=" * 70)
samples = {}
for fmt in ("mp3", "flac", "ogg", "wav", "m4a"):
    p = ROOT / f"s.{fmt}"
    data = make_audio(fmt, p)
    samples[fmt] = data
    got = app.sniff_audio_ext(data, "mp3")
    check(f"嗅探 {fmt} -> {fmt}", got == fmt, f"实际 {got}")
check("空字节回退 fallback", app.sniff_audio_ext(b"", "mp3") == "mp3")
check("垃圾字节回退 fallback", app.sniff_audio_ext(b"notaudio" * 4, "mp3") == "mp3")

print()
print("=" * 70)
print("2) 文件名清洗：中文保留、危险字符替换、长度截断")
print("=" * 70)
base = app.library_basename("夜曲", "周杰伦")
check("中文正常命名", base == "周杰伦 - 夜曲", base)
check("斜杠被替换", "/" not in app.library_basename("a/b", "c\\d"))
# 穿越的实际防线是「文件名里不能有分隔符」——没有 "/" 就无法跨目录。
# 剩下几个孤立的 "." 是无害的（会在最终目录内生成名为 `.._.._x` 的普通文件）。
evil = app._ascii_safe("../../../../tmp/pwned")
check("_ascii_safe 消除路径分隔符（无法跨目录）",
      "/" not in evil and "\\" not in evil and evil not in ("", ".", ".."), evil)
check("_ascii_safe 空串兜底", app._ascii_safe("") == "_")
check("_ascii_safe 长度上限", len(app._ascii_safe("あ" * 500, limit=80)) <= 80)
check("control char 被清", "\n" not in app._ascii_safe("a\nb") and "\x00" not in app._ascii_safe("a\x00b"))

print()
print("=" * 70)
print("3) 落点校验 resolve_save_dir（穿越 / 越界 / 符号链接）")
print("=" * 70)
d, err = app.resolve_save_dir()
check("合法子目录通过", d == str(LIB / "kugou") and not err, f"{d} {err}")

app.CONF["save_dir"] = str(LIB)
d, err = app.resolve_save_dir()
check("库根自身通过", d == str(LIB) and not err, f"{d} {err}")

app.CONF["save_dir"] = str(OUTSIDE)
d, err = app.resolve_save_dir()
check("库外目录被拒", d == "" and err, f"d={d} err={err[:60]}")

app.CONF["save_dir"] = str(LIB / ".." / "outside")
d, err = app.resolve_save_dir()
check(".. 穿越被拒", d == "" and err, f"d={d} err={err[:60]}")

app.CONF["save_dir"] = "/etc"
d, err = app.resolve_save_dir()
check("系统目录被拒", d == "" and err, f"d={d}")

# 符号链接逃逸：库内建链接指向库外
link = LIB / "escape"
try:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(OUTSIDE)
    app.CONF["save_dir"] = str(link)
    d, err = app.resolve_save_dir()
    check("符号链接逃逸被拒", d == "" and err, f"d={d} err={err[:60]}")
    link.unlink()
except OSError as e:
    print(f"  SKIP 符号链接测试（{e}）")

app.CONF["save_dir"] = str(LIB / "kugou")

print()
print("=" * 70)
print("4) 端到端归档 archive_online_track_blocking")
print("=" * 70)
guid = "online:kugou:" + "a" * 32
info = {"title": "测试歌曲", "artist": "测试歌手", "album": "测试专辑",
        "ext": "mp3", "duration_s": 1, "release_date": "2024-05-01"}
lyric = "[00:01.00]测试歌词\n[00:02.00]第二行"
cover = samples["mp3"]  # 用任意字节充当封面
dest = app.archive_online_track_blocking(guid, samples["flac"], info, lyric, cover)
print(f"  归档结果: {dest}")
check("返回了落盘路径", bool(dest))
check("文件存在且非空", dest and os.path.exists(dest) and os.path.getsize(dest) > 0)
check("扩展名按内容纠正为 flac（声明是 mp3）", dest.endswith(".flac"), dest)
check("文件名是「歌手 - 歌名」", "测试歌手 - 测试歌曲" in (dest or ""), dest)
check("落在配置的库内目录", (dest or "").startswith(str(LIB / "kugou")), dest)

lrc = os.path.splitext(dest)[0] + ".lrc"
check("同名 .lrc 已生成", os.path.exists(lrc))
if os.path.exists(lrc):
    check(".lrc 内容正确", "测试歌词" in Path(lrc).read_text(encoding="utf-8"))

img = os.path.splitext(dest)[0] + ".jpg"
check("同名封面图已生成", os.path.exists(img) or os.path.exists(os.path.splitext(dest)[0] + ".png"),
      str(list(Path(os.path.dirname(dest)).glob("*"))))

# 权限：应与父目录属主一致、对 others 可读（飞牛要扫）
st = os.stat(dest)
check("权限 0644（飞牛可扫）", (st.st_mode & 0o777) == 0o644, oct(st.st_mode & 0o777))

print()
print("=" * 70)
print("5) 标签写入（mutagen 读取验证）")
print("=" * 70)
try:
    from mutagen import File as MF
    audio = MF(dest, easy=True)
    tags = audio.tags if audio else None
    def g(k):
        v = tags.get(k) if tags else None
        return str(v[0]) if isinstance(v, (list, tuple)) and v else str(v or "")
    check("title 标签写入", g("title") == "测试歌曲", g("title"))
    check("artist 标签写入", g("artist") == "测试歌手", g("artist"))
    check("album 标签写入", g("album") == "测试专辑", g("album"))
    raw = MF(dest)
    has_pic = bool(getattr(raw, "pictures", None))
    check("flac 内嵌封面", has_pic, f"pictures={len(getattr(raw,'pictures',[]) or [])}")
except Exception as e:
    check("mutagen 校验标签", False, repr(e))

print()
print("=" * 70)
print("6) 去重：同一 guid 不重复归档")
print("=" * 70)
check("already_saved 已置位", app.already_saved(guid))


def audio_files(d):
    exts = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".opus")
    return sorted(p.name for p in Path(d).iterdir() if p.suffix.lower() in exts)


before = audio_files(LIB / "kugou")
dest2 = app.archive_online_track_blocking(guid, samples["flac"], info, lyric, cover)
after = audio_files(LIB / "kugou")
check("重复归档返回空", dest2 == "", dest2)
check("音频文件数未增加", before == after, f"{before} -> {after}")

# 标记指向的文件被删后，应允许重新归档
os.unlink(dest)
check("目标文件删除后 already_saved 变 False", not app.already_saved(guid))
dest3 = app.archive_online_track_blocking(guid, samples["flac"], info, lyric, cover)
check("可重新归档", bool(dest3) and os.path.exists(dest3), dest3)

print()
print("=" * 70)
print("7) 同名不同曲去重：unique_library_path 追加序号")
print("=" * 70)
# unique_library_path 按 (目录, 名字, 扩展名) 判重，不同扩展名互不冲突。
# 要触发序号必须同一 basename + 同一扩展名，所以这里用同格式的另一个 guid。
guid_b = "online:kugou:" + "b" * 32
destb = app.archive_online_track_blocking(guid_b, samples["flac"], info, "", b"")
check("同名同格式第二个文件加了序号", bool(destb) and "(2)" in destb, destb)
check("原文件仍在", os.path.exists(dest3))
guid_c = "online:kugou:" + "c" * 32
destc = app.archive_online_track_blocking(guid_c, samples["flac"], info, "", b"")
check("第三个文件序号为 (3)", bool(destc) and "(3)" in destc, destc)

print()
print("=" * 70)
print("8) 关闭开关 / 超大文件 / 空数据 不落盘")
print("=" * 70)
app.CONF["save_enabled"] = False
g_off = "online:kugou:" + "c" * 32
r = app.archive_online_track_blocking(g_off, samples["mp3"], info, "", b"")
check("关闭时（含代码层开关）不落盘", r == "", r)
app.CONF["save_enabled"] = True

app.CONF["save_max_bytes"] = 100
g_big = "online:kugou:" + "d" * 32
r = app.archive_online_track_blocking(g_big, samples["flac"], info, "", b"")
check("超过体积上限被跳过", r == "", r)
app.CONF["save_max_bytes"] = 512 * 1024 * 1024

r = app.archive_online_track_blocking("online:kugou:" + "e" * 32, b"", info, "", b"")
check("空 body 被跳过", r == "")

print()
print("=" * 70)
print("9) .env 写入安全性（换行注入 / 权限 / 值清洗）")
print("=" * 70)
payload = "http://127.0.0.1:8899\nPLACEHOLDER_CMD\n"
clean = app._sanitize_env_value(payload)
check("换行被消除", "\n" not in clean and "\r" not in clean, repr(clean))
check("注入载荷不再是独立命令行", "PLACEHOLDER_CMD" not in clean.split(" ") or "\n" not in clean)
check("_sanitize 挡 NUL", "\x00" not in app._sanitize_env_value("a\x00b"))
# kugou_url 会成为 httpx 的 base_url，只放行 http(s)
check("kugou_url 放行 http", app._sanitize_kugou_url("http://10.0.0.9:8899") == "http://10.0.0.9:8899")
check("kugou_url 放行 https", app._sanitize_kugou_url("https://a.example/b") == "https://a.example/b")
check("kugou_url 拒绝 file://", app._sanitize_kugou_url("file:///etc/passwd") == "")
check("kugou_url 拒绝 ftp://", app._sanitize_kugou_url("ftp://x/y") == "")
check("kugou_url 拒绝无 scheme", app._sanitize_kugou_url("127.0.0.1:8899") == "")
check("kugou_url 换行注入也一并清掉",
      "\n" not in app._sanitize_kugou_url("http://a\nb"))

envfile = ENV_FILE
envfile_bak = None
if envfile.exists():
    envfile_bak = envfile.read_bytes()
try:
    ok = app.write_env_updates({"FNMUSIC_TEST_ONE": "v1\nrm -rf /tmp/x", "FNMUSIC_TEST_TWO": "v2"})
    check("write_env_updates 成功", ok)
    text = envfile.read_text(encoding="utf-8")
    check("写入内容无换行注入", "rm -rf" not in text or "\nrm -rf" not in text, repr(text[-120:]))
    check(".env 里的可疑行不存在", "FNMUSIC_TEST_ONE=v1 rm -rf /tmp/x" in text, repr(text[-160:]))
    mode = envfile.stat().st_mode & 0o777
    check(".env 权限为 0600（含酷狗 token）", mode == 0o600, oct(mode))
    ok2 = app.write_env_updates({"FNMUSIC_TEST_ONE": "", "FNMUSIC_TEST_TWO": ""})
    check("二次写入仍 0600", ok2 and (envfile.stat().st_mode & 0o777) == 0o600)
finally:
    if envfile_bak is not None:
        envfile.write_bytes(envfile_bak)
        os.chmod(envfile, 0o600)

print()
print("=" * 70)
print("10) Range 解析：单段闭/开区间、多段、后缀式")
print("=" * 70)
check("bytes=0- → (0, None)", app.parse_range_header("bytes=0-") == (0, None))
check("bytes=0-1023 → (0, 1023)", app.parse_range_header("bytes=0-1023") == (0, 1023))
check("bytes=524288-1048575 → (524288, 1048575)",
      app.parse_range_header("bytes=524288-1048575") == (524288, 1048575))
check("bytes=100- → (100, None)", app.parse_range_header("bytes=100-") == (100, None))
check("大小写无关", app.parse_range_header("Bytes=0-9") == (0, 9))
check("空格容忍", app.parse_range_header("  bytes = 5 - 9 ") == (5, 9))
check("多段返回 None（宁可少存不可错存）",
      app.parse_range_header("bytes=0-9,20-29") is None)
check("后缀式 bytes=-500 返回 None（不是从头读）",
      app.parse_range_header("bytes=-500") is None)
check("空值返回 None", app.parse_range_header(None) is None)
check("垃圾串返回 None", app.parse_range_header("bytes=abc") is None)

print()
print("=" * 70)
print("11) 归档候选判定 should_consider_archive")
print("=" * 70)
check("无 Range → 候选", app.should_consider_archive(None) is True)
check("bytes=0- → 候选", app.should_consider_archive("bytes=0-") is True)
check("bytes=0-1048575 → 候选", app.should_consider_archive("bytes=0-1048575") is True)
check("bytes=524288-1048575 → 候选（顺序读取第 K 块）",
      app.should_consider_archive("bytes=524288-1048575") is True)
check("bytes=524288- → 候选", app.should_consider_archive("bytes=524288-") is True)
check("多段 → 不候选", app.should_consider_archive("bytes=0-9,20-29") is False)
check("bytes=-500（后缀式）→ 不候选", app.should_consider_archive("bytes=-500") is False)

print()
print("=" * 70)
print("12) 覆盖度：顺序分块读完算完整；只听半首不算")
print("=" * 70)
TOTAL = 1_000_000


def _cov():
    c = app.ArchiveCoverage()
    c.note_total(TOTAL)
    return c


c = _cov()
check("空覆盖不算完整", c.is_complete() is False)
c.add(0, 99_999)
check("只读第一块不算完整", c.is_complete() is False, f"covered={c.covered_bytes()}")
step = 65536
pos = 0
while pos < TOTAL:
    c.add(pos, min(pos + step - 1, TOTAL - 1))
    pos += step
check("顺序分块读满整曲算完整", c.is_complete() is True,
      f"covered={c.covered_bytes()} spans={len(c.spans)}")
check("合并成单段 [0, TOTAL-1]", c.spans == [(0, TOTAL - 1)], str(c.spans))
check("从 0 连续覆盖 = 全长", c.covered_from_zero() == TOTAL)

c2 = _cov()
c2.add(TOTAL // 2, TOTAL - 1)
check("只听后半段不算完整（不能误归档）", c2.is_complete() is False,
      f"covered={c2.covered_bytes()}")
check("但前缀覆盖为 0", c2.covered_from_zero() == 0)

c3 = _cov()
for a, b in [(0, 999), (4000, 4999), (1000, 3999), (5000, TOTAL - 1)]:
    c3.add(a, b)
check("乱序但拼齐也算完整", c3.is_complete() is True, str(c3.spans[:4]))

c4 = _cov()
c4.add(0, 499_999)
c4.add(0, 499_999)
check("重复读同一段不虚增", c4.covered_bytes() == 500_000, str(c4.covered_bytes()))
check("重复读仍未覆盖全曲", c4.is_complete() is False)

c5 = _cov()
c5.add(0, TOTAL - 1 - 4096)
check("末尾缺 4KB（探测未覆盖）仍认完整", c5.is_complete() is True,
      f"covered={c5.covered_bytes()}")
c6 = _cov()
c6.add(0, TOTAL // 2)
check("缺一半不认完整", c6.is_complete() is False)

print()
print("=" * 70)
print("13) 送达字节数决定覆盖区间（切歌不得被算成已读）")
print("=" * 70)
_env_bak = os.environ.get("FNMUSIC_SAVE_ENABLED")
os.environ["FNMUSIC_SAVE_ENABLED"] = "1"
app.CONF["save_enabled"] = True
_save_bak = app.CONF.get("save_enabled")
try:
    app._ARCHIVE_COVERAGE.clear()
    g = "online:kugou:" + "9" * 32
    hdrs = {"content-range": f"bytes 0-{TOTAL - 1}/{TOTAL}",
            "content-length": str(TOTAL)}
    app.note_client_read(g, None, hdrs, 1000)
    cov = app._ARCHIVE_COVERAGE[g]
    check("声明整段但只送达 1000B → 只记 1000B", cov.covered_bytes() == 1000,
          str(cov.spans))
    check("未满 → 不算完整", cov.is_complete() is False)

    app._ARCHIVE_COVERAGE.clear()
    g2 = "online:kugou:" + "8" * 32
    for start in range(0, TOTAL, 100_000):
        end = min(start + 99_999, TOTAL - 1)
        app.note_client_read(
            g2,
            f"bytes={start}-{end}",
            {"content-range": f"bytes {start}-{end}/{TOTAL}",
             "content-length": str(end - start + 1)},
            end - start + 1,
        )
    cov2 = app._ARCHIVE_COVERAGE[g2]
    check("分块累加至覆盖全曲 → 完整", cov2.is_complete() is True,
          f"covered={cov2.covered_bytes()}")

    app._ARCHIVE_COVERAGE.clear()
    g3 = "online:kugou:" + "7" * 32
    app.note_client_read(g3, None, hdrs, 0)
    check("delivered=0 不建覆盖", g3 not in app._ARCHIVE_COVERAGE)

    app.CONF["save_enabled"] = False
    app._ARCHIVE_COVERAGE.clear()
    app.note_client_read(g, None, hdrs, 1000)
    check("关闭开关时不记覆盖", g not in app._ARCHIVE_COVERAGE)
finally:
    app.CONF["save_enabled"] = _save_bak
    if _env_bak is not None:
        os.environ["FNMUSIC_SAVE_ENABLED"] = _env_bak
    app._ARCHIVE_COVERAGE.clear()

print()
print("=" * 70)
print("10) run_proxy.sh 不再 source .env（root 注入防线）")
print("=" * 70)
sh = (PKG / "app" / "proxy" / "run_proxy.sh").read_text(encoding="utf-8")
check("run_proxy.sh 已无 `. .env` / source .env", "set -a" not in sh and '. "${BASE_DIR}/.env"' not in sh)
check("run_proxy.sh 改为逐行 export", 'export "${_key}=${_val}"' in sh)

# 抽出脚本里的加载块，原样跑一遍注入样本
block_start = sh.index("# 加载 .env")
block_end = sh.index('echo "[run_proxy] .env 已按 KEY=VALUE 逐行加载')
block = sh[block_start:block_end]
# 上面的切片在 if 块结束前就断了（fi 在后面的 echo 之后），这里补回 if 的收尾。
# 注意 while 的 `done < ...` 已包含在 block 里，不要再补一次。
block += "fi\n"
fake_base = ROOT / "fakebase"
fake_base.mkdir(exist_ok=True)
sentinel = ROOT / "INJECTION_SENTINEL"
if sentinel.exists():
    sentinel.unlink()
(fake_base / ".env").write_text(
    f"FNMUSIC_KUGOU_URL=http://127.0.0.1:8899\n"
    f"EVIL=1; touch {sentinel}\n"
    f"FNMUSIC_SAVE_ENABLED=1\n"
    f"`touch {sentinel}`\n"
    f"BAD $(touch {sentinel})\n"
    f"9BAD=x\n"
    f"OK_KEY=hello world\n",
    encoding="utf-8",
)
runner = ROOT / "runner.sh"
# 加载块引用 ${ETC_DIR} 与 ${BASE_DIR}（脚本前面定义的）。测试直接跑切片，
# 得把这两个变量补上，否则 set -u 会直接判未定义变量而退出。
runner.write_text("#!/bin/bash\nset -euo pipefail\n"
                  + f"BASE_DIR={fake_base}\nETC_DIR={fake_base}\n"
                  + block +
                  '\necho "OK_KEY=${OK_KEY:-<unset>}"\necho "FNMUSIC_SAVE_ENABLED=${FNMUSIC_SAVE_ENABLED:-<unset>}"\n'
                  'echo "EVIL=${EVIL:-<unset>}"\necho "BAD=${BAD:-<unset>}"\n',
                  encoding="utf-8")
os.chmod(runner, 0o755)
r = subprocess.run(["bash", str(runner)], capture_output=True, text=True)
out = r.stdout + r.stderr
print("  " + out.replace("\n", "\n  ").strip())
check("脚本执行成功", r.returncode == 0, out)
check("注入命令未被执行（无 sentinel 文件）", not sentinel.exists())
check("正常键值被正确加载", "OK_KEY=hello world" in out, out)
check("布尔键加载", "FNMUSIC_SAVE_ENABLED=1" in out, out)
# export 不是 eval：带 shell 元字符的值只是被当字面量存进变量，不执行。
check("含元字符的值按字面量保存", "EVIL=1; touch" in out, out)
check("非法 key（含空格 / 数字开头）被跳过", "BAD=<unset>" in out, out)

print()
print("=" * 70)
print("11) 设置回读：网页保存过的项，重启后必须还在（不能悄悄恢复默认）")
print("=" * 70)
# 这一条防的是"写进 .env 却没人读"的静默回退：设置页保存成功、进程一重启
# 又变回默认值，用户会以为设置根本没生效。
envfile = ENV_FILE
envfile_bak = envfile.read_bytes() if envfile.exists() else None
try:
    ok = app.write_env_updates({
        "FNMUSIC_SAVE_ENABLED": "1",
        "FNMUSIC_SAVE_DIR": str(LIB / "saved"),
        "FNMUSIC_LIBRARY_DIR": str(LIB),
        "FNMUSIC_SAVE_WRITE_TAGS": "1",
        "FNMUSIC_SAVE_WRITE_COVER": "0",
        "FNMUSIC_SAVE_WRITE_LYRIC": "1",
        "FNMUSIC_SAVE_MARKER_DIR": str(LIB / "saved" / ".idx"),
    })
    check("写入设置成功", ok)
    # 复刻启动时的回读逻辑：读 .env → 按白名单映射回 CONF
    reload_map = {
        "FNMUSIC_LIBRARY_DIR": ("library_dir", "str"),
        "FNMUSIC_SAVE_ENABLED": ("save_enabled", "bool"),
        "FNMUSIC_SAVE_DIR": ("save_dir", "str"),
        "FNMUSIC_SAVE_WRITE_TAGS": ("save_write_tags", "bool"),
        "FNMUSIC_SAVE_WRITE_COVER": ("save_write_cover", "bool"),
        "FNMUSIC_SAVE_WRITE_LYRIC": ("save_write_lyric", "bool"),
        "FNMUSIC_SAVE_MARKER_DIR": ("save_marker_dir", "str"),
    }
    loaded = {}
    for line in envfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            loaded[k.strip()] = v.strip()
    for env_k, (ck, t) in reload_map.items():
        check(f"回读白名单覆盖 {env_k}", env_k in loaded or env_k == "FNMUSIC_SAVE_WRITE_TAGS",
              f"白名单缺 {env_k}")
    check("落盘目录能回读到", loaded.get("FNMUSIC_SAVE_DIR") == str(LIB / "saved"),
          repr(loaded.get("FNMUSIC_SAVE_DIR")))
    check("开关能回读到", loaded.get("FNMUSIC_SAVE_ENABLED") == "1")
    check("去重索引目录能回读到", loaded.get("FNMUSIC_SAVE_MARKER_DIR") == str(LIB / "saved" / ".idx"),
          repr(loaded.get("FNMUSIC_SAVE_MARKER_DIR")))
    # 关键：白名单映射表本身必须包含这些键，否则启动回读会漏
    src = (PROXY / "app.py").read_text(encoding="utf-8")
    for env_k in ("FNMUSIC_LIBRARY_DIR", "FNMUSIC_SAVE_ENABLED", "FNMUSIC_SAVE_DIR",
                  "FNMUSIC_SAVE_WRITE_TAGS", "FNMUSIC_SAVE_WRITE_COVER",
                  "FNMUSIC_SAVE_WRITE_LYRIC", "FNMUSIC_SAVE_MARKER_DIR"):
        check(f"_KUGOU_KEY_MAP 含 {env_k}（否则重启丢设置）",
              f'"{env_k}"' in src, f"{env_k} 未在回读白名单里")
finally:
    if envfile_bak is not None:
        envfile.write_bytes(envfile_bak)
        os.chmod(envfile, 0o600)

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
