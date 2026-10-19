#!/usr/bin/env python3
"""P-02 回归：`FNMUSIC_ENV_FILE` 显式覆盖 .env（读与写必须是同一个路径）。

背景（P-02，真机已确认）
    在已安装本应用的机器上跑测试，`test_pipeline` 有 6 项失败（写标签/封面/歌词）。
    根因：app.py 导入期会加载 /vol1/@appconf/<app>/.env 并**覆盖** os.environ，
    把测试自己设的 FNMUSIC_SAVE_WRITE_TAGS / _COVER / _LYRIC 压成 0。
    干净环境 457 项全绿，真机"假红" —— 最容易把人带偏的那类失败。

修法：app.py 支持 FNMUSIC_ENV_FILE（绝对路径）显式指定 .env；读写共用
    `resolve_env_file()`，所以覆盖点只有一处。

本测试**带阴性对照**：
    · 本文件里"覆盖文件生效"的断言，在旧代码（不认 FNMUSIC_ENV_FILE）下必然
      失败 —— 我们先断言"环境变量 FNMUSIC_SAVE_WRITE_TAGS=1 时，
      覆盖文件里的 0 赢了"，再断言"不给覆盖文件时，1 赢了"。
      两条一起才证明：生效的是**那个文件**，而不是别的什么。

跑法：env -u PYTHONPATH python3 tests/test_env_override.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
APP_PY = PROXY / "app.py"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    suffix = f"   [{detail}]" if detail and not cond else ""
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{suffix}")


ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-env-"))
ETC = ROOT / "fnmusic_kugou_pro" / "etc"
VAR = ROOT / "fnmusic_kugou_pro" / "var"
HOME = ROOT / "fnmusic_kugou_pro" / "home"
LIBDIR = ROOT / "music_lib"
for _p in (ETC, VAR, HOME, LIBDIR):
    _p.mkdir(parents=True, exist_ok=True)

DB = ROOT / "music.db"
_con = sqlite3.connect(DB)
_con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
_con.execute("INSERT INTO shared_library (path) VALUES (?)", (str(LIBDIR),))
_con.commit()
_con.close()

# 模拟"用户真配置"：显式把两个开关关掉
CFG = ROOT / "explicit" / ".env"
CFG.parent.mkdir(parents=True, exist_ok=True)
CFG.write_text(
    "FNMUSIC_KUGOU_ENABLED=0\n"
    "FNMUSIC_SAVE_WRITE_TAGS=0\n"
    "FNMUSIC_SAVE_WRITE_LYRIC=0\n",
    encoding="utf-8",
)

CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["_T_PROXY"])
import app
print("@@JSON@@" + json.dumps({
    "write_tags": app.CONF["save_write_tags"],
    "write_lyric": app.CONF["save_write_lyric"],
    "env_file": str(app._ENV_FILE),
    "resolved": str(app.resolve_env_file()),
}))
'''


def run_child(**env_over: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FNMUSIC_", "TRIM_"))}
    env.update({
        "_T_PROXY": str(PROXY),
        "PYTHONPATH": "",
        "FNMUSIC_MUSIC_DB": str(DB),
        "FNMUSIC_LIBRARY_DIR": "",
        "FNMUSIC_SAVE_ENABLED": "1",
        "FNMUSIC_SAVE_DIR": str(LIBDIR / "online"),
        # 环境变量这一侧故意给"开"，用来和覆盖文件里的"关"对照
        "FNMUSIC_SAVE_WRITE_TAGS": "1",
        "FNMUSIC_SAVE_WRITE_LYRIC": "1",
        "FNMUSIC_CACHE_DIR": str(ROOT / "cache"),
        "FNMUSIC_HOME_DIR": str(ROOT / "home"),
        "FNMUSIC_KUGOU_ENABLED": "1",
        "FNMUSIC_ETC_DIR": str(ETC),
        "FNMUSIC_VAR_DIR": str(VAR),
        "FNMUSIC_PKG_HOME_DIR": str(HOME),
        "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
        "FNMUSIC_ENV_FILE": "",
    })
    env.update(env_over)
    r = subprocess.run([sys.executable, "-c", CHILD], capture_output=True,
                       text=True, env=env, timeout=120)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@JSON@@")]
    if not line:
        return {"_err": (r.stdout + r.stderr)[-1200:]}
    return json.loads(line[-1][len("@@JSON@@"):])


print("=" * 70)
print("1) 静态：读与写必须走同一个解析函数")
print("=" * 70)
src = APP_PY.read_text(encoding="utf-8")
check("app.py 认 FNMUSIC_ENV_FILE", "FNMUSIC_ENV_FILE" in src)
_weu = src[src.index("def write_env_updates"):]
_weu = _weu[:_weu.index("\ndef ", 1)] if "\ndef " in _weu[1:] else _weu
check("切片非空且确实是 write_env_updates（防白测）",
      len(_weu) > 300 and "resolve_env_file" in _weu, str(len(_weu)))
check("写盘入口统一走 resolve_env_file(create=True)",
      "resolve_env_file(create=True)" in _weu, _weu[:200])
check("非绝对路径被忽略（有告警分支）", "不是绝对路径" in src)

print()
print("=" * 70)
print("2) 动态：覆盖文件生效（阴性对照：旧代码在这里必定失败）")
print("=" * 70)
d = run_child(FNMUSIC_ENV_FILE=str(CFG))
check("子进程导入成功", "_err" not in d, str(d.get("_err", ""))[:600])
if "_err" not in d:
    check("覆盖文件里的 0 压过了环境变量的 1（写标签）", d["write_tags"] is False, str(d["write_tags"]))
    check("覆盖文件里的 0 压过了环境变量的 1（写歌词）", d["write_lyric"] is False, str(d["write_lyric"]))
    check("_ENV_FILE 就是覆盖路径", Path(d["env_file"]) == CFG, d["env_file"])
    check("resolve_env_file() 与 _ENV_FILE 一致（读写同一处）",
          Path(d["resolved"]) == Path(d["env_file"]), f'{d["resolved"]} vs {d["env_file"]}')

print()
print("=" * 70)
print("3) 动态：不给覆盖文件时，环境变量说了算（证明上面赢的是那个文件）")
print("=" * 70)
d2 = run_child()
check("子进程导入成功", "_err" not in d2, str(d2.get("_err", ""))[:600])
if "_err" not in d2:
    check("无覆盖文件 → 环境变量的 1 生效（写标签）", d2["write_tags"] is True, str(d2["write_tags"]))
    check("默认 .env 落在配置目录", Path(d2["env_file"]) == ETC / ".env", d2["env_file"])

print()
print("=" * 70)
print("4) 动态：相对路径被忽略（不允许靠 cwd 意外命中）")
print("=" * 70)
d3 = run_child(FNMUSIC_ENV_FILE="relative/.env")
check("子进程导入成功", "_err" not in d3, str(d3.get("_err", ""))[:600])
if "_err" not in d3:
    check("相对路径不生效，仍用配置目录", Path(d3["env_file"]) == ETC / ".env", d3["env_file"])
    check("环境变量的 1 未被覆盖", d3["write_tags"] is True, str(d3["write_tags"]))

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
