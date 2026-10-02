#!/usr/bin/env python3
"""去重索引目录（save_marker_dir）的落点与"覆盖安装不丢"回归测试。

背景：`save_marker_dir` 默认曾是 `<安装目录>/save_index`。飞牛覆盖安装/升级会
替换 target 目录，标记随之全丢 —— 已归档的歌被当成没存过，重播时会再下一次
并落成「歌手 - 歌名 (2).flac」。修法：默认留空 → 推导到「落盘目录」下的隐藏
子目录，与音频文件同生共死。

本测试必须在**独立子进程**里跑：默认值在 app.py 导入时读环境变量，
同进程里改 os.environ 不会重新生效。

运行：PYTHONPATH= /tmp/testvenv/bin/python tests/test_marker_dir.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PY = sys.executable

# 子进程里要跑的脚本：搭好假环境 → 导入 app → 断言标记落点与存活
CHILD = r'''
import os, sqlite3, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(os.environ["_T_ROOTS"])
LIB = ROOT / "music_lib"
LIB.mkdir(parents=True, exist_ok=True)
ROOT.joinpath("home").mkdir(parents=True, exist_ok=True)

DB = ROOT / "music.db"
c = sqlite3.connect(DB)
c.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
c.execute("INSERT INTO shared_library (path) VALUES (?)", (str(LIB),))
c.commit(); c.close()

os.environ.update({
    "FNMUSIC_MUSIC_DB": str(DB),
    "FNMUSIC_LIBRARY_DIR": "",
    "FNMUSIC_SAVE_ENABLED": "1",
    "FNMUSIC_SAVE_DIR": str(LIB / "kugou"),
    "FNMUSIC_SAVE_WRITE_TAGS": "1",
    "FNMUSIC_SAVE_WRITE_COVER": "0",
    "FNMUSIC_SAVE_WRITE_LYRIC": "0",
    "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
    "FNMUSIC_HOME_DIR": str(ROOT / "home"),
    "FNMUSIC_KUGOU_ENABLED": "0",
})
# 关键：不设置 FNMUSIC_SAVE_MARKER_DIR，走"留空自动推导"的新默认
os.environ.pop("FNMUSIC_SAVE_MARKER_DIR", None)

sys.path.insert(0, os.environ["_T_PROXY"])
import app

ROOTDIR, err = app.resolve_save_dir()
assert ROOTDIR, f"落盘目录不可用: {err}"
mdir = app.resolve_marker_dir()
guid = "online:kugou:" + "a" * 32
mpath = app._save_marker_path(guid)

# 归档一个真实文件 + 写标记
afile = Path(ROOTDIR) / "某歌手 - 某歌名.flac"
afile.write_bytes(b"x" * 2048)
app.mark_saved(guid, str(afile))
saved_before = app.already_saved(guid)

# 模拟飞牛覆盖安装：安装目录（_HOME）整个被替换掉
shutil.rmtree(ROOT / "home", ignore_errors=True)
saved_after = app.already_saved(guid)

mdir_mode = os.stat(mdir).st_mode & 0o777
import json
print(json.dumps({
    "lib": str(LIB),
    "marker_dir": mdir,
    "marker_path": mpath,
    "mode": oct(mdir_mode),
    "saved_before": saved_before,
    "saved_after": saved_after,
    "inside_lib": Path(mpath).is_relative_to(LIB),
}))
'''

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


print("=" * 70)
print("去重索引目录：默认跟随落盘目录，覆盖安装不丢")
print("=" * 70)

roots = Path(tempfile.mkdtemp(prefix="fnmusic-marker-"))
env = dict(os.environ)
env.update({
    "_T_ROOTS": str(roots),
    "_T_PROXY": str(PKG / "app" / "proxy"),
    "PYTHONPATH": "",
    "PYTHONDONTWRITEBYTECODE": "1",
})
r = subprocess.run([PY, "-c", CHILD], capture_output=True, text=True, env=env, cwd=str(PKG))

if r.returncode != 0:
    print(r.stdout.strip())
    print(r.stderr.strip()[-1500:])
    check("子进程执行成功", False, f"exit={r.returncode}")
else:
    import json
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1]
    d = json.loads(line)
    print(f"  落盘目录: {d['lib']}/kugou")
    print(f"  标记目录: {d['marker_dir']}  (权限 {d['mode']})")
    check("默认标记目录不再落在安装目录", "/home/save_index" not in d["marker_dir"], d["marker_dir"])
    check("默认标记目录在落盘目录下（隐藏子目录）",
          d["marker_dir"].endswith(".fnmusic_save_index"), d["marker_dir"])
    check("标记文件在曲库根之内", d["inside_lib"], d["marker_path"])
    # 目录权限必须可写：曾经复用文件的 0644，导致 mkdir 成功但写标记 EACCES
    check("标记目录权限含 w（目录不能用 0644）",
          oct(int(d["mode"], 8)) not in ("0o644", "0o444") and (int(d["mode"], 8) & 0o300) == 0o300,
          d["mode"])
    check("标记写入成功且能读回（写前）", d["saved_before"] is True, str(d["saved_before"]))
    check("删掉安装目录后仍认得已归档（覆盖安装不丢索引）",
          d["saved_after"] is True, str(d["saved_after"]))

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
