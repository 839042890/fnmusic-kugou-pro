#!/usr/bin/env python3
"""v2.1.4：空值环境变量导致启动崩溃的回归测试。

事故复盘（2.1.3，真实发生，用户装机后【音乐】整站不可用）：
    ValueError: could not convert string to float: ''
    File ".../proxy/app.py", line 189, in <module>
        "save_timeout_s": float(os.environ.get("FNMUSIC_SAVE_TIMEOUT_S", "300")),

链条：
  1. 2.1.3 把「用户在向导里没填」的可选项写进 .env 时，写成了
     `FNMUSIC_SAVE_TIMEOUT_S=`（**键在、值为空**）。同批还有
     FNMUSIC_SAVE_MAX_BYTES / FNMUSIC_FAV_DIR。
     且 2.1.3 新增的配置回读白名单让这 3 个键**第一次真正被读取**——
     在此之前它们是死键，重来不读，所以从没炸过。
  2. run_proxy.sh 按 KEY=VALUE 逐行 export，空值也照 export，
     于是 os.environ 里存在该键、值为空串。
  3. os.environ.get(KEY, "300") 的默认值**只在键不存在时**生效，
     空串照样返回 ""。float("") → ValueError。
  4. 异常发生在**模块导入期**（CONF 是模块级字典），uvicorn 起不来。
  5. 致命处：run_proxy.sh 是「先接管 socket（mv trim_music.socket →
     trim_music_upstream.socket）再启动 uvicorn」。接管成功、启动失败，
     TARGET 上空无一物 → 整个【音乐】不可用，且不重启【音乐】无法恢复。

修复（三道防线）：
  A. app.py 引入 _env_str/_env_bool/_env_int/_env_float，**空串与纯空白一律
     视为"未设置"**，回落默认值，永不抛异常。全部数值/布尔配置改走它们。
  B. run_proxy.sh 把 socket 接管挪到预检**之后**：先用同一解释器 `import app`
     验活，失败则一个字节都不动音乐端（失败要失败得干净）。
  C. cmd/main 在代理启动失败时调 restore_socket()，把音乐端还回去。

本测试覆盖 A/B/C 三处。
运行：env -u PYTHONPATH python3 tests/test_env_empty.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
APP_PY = PROXY / "app.py"
RUN_PROXY = PROXY / "run_proxy.sh"
CMD_MAIN = PKG / "cmd" / "main"
PY = sys.executable

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


APP_SRC = APP_PY.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# 1) 静态检查：不允许再出现裸的类型转换读取环境变量
# ---------------------------------------------------------------------------
print("\n[1] 静态：禁止裸 int()/float()(os.environ.get(...))")
STRIP_COMMENTS = "\n".join(
    line for line in APP_SRC.splitlines() if not line.lstrip().startswith("#")
)
bare = re.findall(r"(?:int|float)\(\s*os\.environ\.get\(", STRIP_COMMENTS)
check("app.py 无裸数值转换（全部走 _env_* 助手）", not bare, f"仍有 {len(bare)} 处")

# 助手函数自身存在
for fn in ("_env_str", "_env_flag", "_env_int", "_env_float"):
    check(f"app.py 定义 {fn}()", f"def {fn}(" in APP_SRC)

# 2.1.4 引入过一处函数重名：读取侧 _env_bool(key, default)->bool 与设置保存侧
# _env_bool(value)->str 同名，后者把前者覆盖掉。功能侥幸没坏，但语义混乱。
# 现在两侧分别叫 _env_flag / _bool_to_digit，且同名定义必须唯一。
_defs = re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)\(", APP_SRC, re.MULTILINE)
_dup = sorted({n for n in _defs if _defs.count(n) > 1})
check("app.py 无重名函数定义", not _dup, f"重名：{_dup}")
check("读取侧叫 _env_flag（不再撞名）", "def _env_flag(" in APP_SRC)
check("写入侧叫 _bool_to_digit（不再撞名）", "def _bool_to_digit(" in APP_SRC)
check("不存在旧名 _env_bool 定义", "def _env_bool(" not in APP_SRC)

# 关键：三个"事故键"必须走助手（调用可能跨行，按窗口匹配）
for key in ("FNMUSIC_SAVE_TIMEOUT_S", "FNMUSIC_SAVE_MAX_BYTES", "FNMUSIC_FAV_DIR"):
    hit = False
    for i, line in enumerate(APP_SRC.splitlines()):
        if key in line:
            window = "\n".join(APP_SRC.splitlines()[max(0, i - 2) : i + 3])
            if "_env_" in window:
                hit = True
                break
    check(f"{key} 经 _env_* 读取", hit, "未见 _env_ 调用")

# ---------------------------------------------------------------------------
# 2) 动态：以"键在、值为空"的 .env 形状导入 app —— 这正是 2.1.3 崩溃的输入
# ---------------------------------------------------------------------------
print("\n[2] 动态：空值 env 下导入 app 不崩，且回落默认值")

# install_callback 实际会写出的形状（含空值键）
EMPTY_ENV = {
    "FNMUSIC_KUGOU_ENABLED": "1",
    "FNMUSIC_KUGOU_URL": "http://127.0.0.1:4000",
    "FNMUSIC_KUGOU_QUALITY": "high",
    "FNMUSIC_KUGOU_SEARCH_TIMEOUT": "15",
    # ↓ 向导没填 → 空值（事故元凶）
    "FNMUSIC_KUGOU_TOKEN": "",
    "FNMUSIC_LIBRARY_DIR": "",
    "FNMUSIC_SAVE_ENABLED": "0",
    "FNMUSIC_SAVE_DIR": "",
    "FNMUSIC_SAVE_MAX_BYTES": "",
    "FNMUSIC_SAVE_TIMEOUT_S": "",
    "FNMUSIC_SAVE_MARKER_DIR": "",
    "FNMUSIC_FAV_DIR": "",
    # 下面这些键 2.1.3 起也会出现在 .env 里，一并覆盖
    "FNMUSIC_ONLINE_LIMIT": "",
    "FNMUSIC_SEARCH_TIMEOUT": "",
    "FNMUSIC_KUGOU_STEP": "",
    "FNMUSIC_META_FULL_POLL_PAGE_TIMEOUT": "",
    "FNMUSIC_LATE_PAGE_WAIT_S": "",
    "FNMUSIC_STREAM_CACHE_ENTRIES": "",
    "FNMUSIC_COVER_PROTECT_BYTES": "",
    "FNMUSIC_KUGOU_FULL_PAGESIZE": "",
}

CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["_T_PROXY"])
import app
keys = ["save_timeout_s", "save_max_bytes", "fav_dir", "online_limit",
        "search_timeout", "kugou_step", "save_enabled", "kugou_search_timeout",
        "meta_full_poll_page_timeout", "late_page_wait_s"]
print("@@JSON@@" + json.dumps({k: repr(app.CONF.get(k)) for k in keys}))
print("@@OK@@")
'''

env = {k: v for k, v in os.environ.items() if not k.startswith("FNMUSIC_")}
env.update(EMPTY_ENV)
env["_T_PROXY"] = str(PROXY)
# P-02：本用例要验证的是"安装脚本产出的空值 .env 长什么样"，
# 真机上 /vol1/@appconf/<app>/.env 一旦存在，app.py 导入期就会读它并覆盖
# EMPTY_ENV —— 测的就不是这里构造的形状了。钉到不存在的文件。
env["FNMUSIC_ENV_FILE"] = str(Path(tempfile.mkdtemp(prefix="fnmusic-env-")) / "absent.env")
env.pop("PYTHONPATH", None)

proc = subprocess.run(
    [PY, "-c", CHILD], capture_output=True, text=True, env=env, cwd=str(PROXY), timeout=120
)
out = proc.stdout + proc.stderr
imported = "@@OK@@" in proc.stdout
check("空值 env 下 import app 成功（2.1.3 在此崩溃）", imported, out.strip().splitlines()[-1] if out.strip() else "无输出")
check("不再出现 ValueError: could not convert", "could not convert string to float" not in out)

if imported:
    import json

    payload = proc.stdout.split("@@JSON@@")[1].split("\n")[0]
    conf = json.loads(payload)
    check("save_timeout_s 回落 300.0", conf["save_timeout_s"] == "300.0", conf["save_timeout_s"])
    check("save_max_bytes 回落 512MB", conf["save_max_bytes"] == str(512 * 1024 * 1024), conf["save_max_bytes"])
    check("online_limit 回落 30", conf["online_limit"] == "30", conf["online_limit"])
    check("search_timeout 回落 15.0", conf["search_timeout"] == "15.0", conf["search_timeout"])
    check("kugou_step 回落 50", conf["kugou_step"] == "50", conf["kugou_step"])
    check("kugou_search_timeout 回落 15.0", conf["kugou_search_timeout"] == "15.0", conf["kugou_search_timeout"])
    check("fav_dir 非空且不在安装目录内", conf["fav_dir"].strip("'") not in ("", "None") and "@appcenter" not in conf["fav_dir"], conf["fav_dir"])

# 非法值（非空但解析不了）也要回落，而不是崩
print("\n[3] 动态：非法值（'abc'）回落默认值且不崩")
BAD = dict(EMPTY_ENV)
BAD.update({"FNMUSIC_SAVE_TIMEOUT_S": "abc", "FNMUSIC_SAVE_MAX_BYTES": "xyz",
            "FNMUSIC_FAV_DIR": "/should/not/be/used"})
env2 = {k: v for k, v in os.environ.items() if not k.startswith("FNMUSIC_")}
env2.update(BAD)
env2["_T_PROXY"] = str(PROXY)
env2.pop("PYTHONPATH", None)
p2 = subprocess.run([PY, "-c", CHILD], capture_output=True, text=True, env=env2, cwd=str(PROXY), timeout=120)
check("非法数值不崩、回落默认", "@@OK@@" in p2.stdout, (p2.stdout + p2.stderr).strip().splitlines()[-1:])
if "@@OK@@" in p2.stdout:
    import json as _json

    c2 = _json.loads(p2.stdout.split("@@JSON@@")[1].split("\n")[0])
    check("非法 save_timeout_s → 300.0", c2["save_timeout_s"] == "300.0", c2["save_timeout_s"])
    check("非法 save_max_bytes → 512MB", c2["save_max_bytes"] == str(512 * 1024 * 1024), c2["save_max_bytes"])
    check("非法(非空) fav_dir 仍被尊重", c2["fav_dir"] == "'/should/not/be/used'", c2["fav_dir"])

# 纯空白也要当"未设置"
print("\n[4] 动态：纯空白值视为未设置")
WS = dict(EMPTY_ENV)
WS.update({"FNMUSIC_SAVE_TIMEOUT_S": "   ", "FNMUSIC_ONLINE_LIMIT": "\t"})
env3 = {k: v for k, v in os.environ.items() if not k.startswith("FNMUSIC_")}
env3.update(WS)
env3["_T_PROXY"] = str(PROXY)
env3.pop("PYTHONPATH", None)
p3 = subprocess.run([PY, "-c", CHILD], capture_output=True, text=True, env=env3, cwd=str(PROXY), timeout=120)
check("空白值不崩", "@@OK@@" in p3.stdout, (p3.stdout + p3.stderr).strip().splitlines()[-1:])
if "@@OK@@" in p3.stdout:
    import json as _json

    c3 = _json.loads(p3.stdout.split("@@JSON@@")[1].split("\n")[0])
    check("空白 save_timeout_s → 300.0", c3["save_timeout_s"] == "300.0", c3["save_timeout_s"])
    check("空白 online_limit → 30", c3["online_limit"] == "30", c3["online_limit"])

# ---------------------------------------------------------------------------
# 5) run_proxy.sh：必须"先预检、后接管"
# ---------------------------------------------------------------------------
print("\n[5] run_proxy.sh：预检在 socket 接管之前")
sh = RUN_PROXY.read_text(encoding="utf-8")
i_pre = sh.find("import app')")
i_takeover = sh.find('mv "${TARGET_SOCK}" "${UPSTREAM_SOCK}"')
check("存在预检 import app", i_pre != -1, "未找到预检")
check("存在 socket 接管", i_takeover != -1, "未找到 mv 接管")
if i_pre != -1 and i_takeover != -1:
    check("预检位置在接管之前", i_pre < i_takeover, f"pre={i_pre} takeover={i_takeover}")
check("预检清空 PYTHONPATH（env -u PYTHONPATH）", "env -u PYTHONPATH" in sh)
check("预检失败即 exit 1（放弃接管）", re.search(r"预检失败.*\n(?:.*\n){0,4}.*exit 1", sh) is not None or "放弃接管" in sh)

# 早期那段"接管"不应再出现在预检之前
early = sh[:i_pre] if i_pre != -1 else sh
check("预检之前无 socket 接管动作", 'mv "${TARGET_SOCK}"' not in early, "接管仍在预检前")

# ---------------------------------------------------------------------------
# 6) cmd/main：启动失败要还原音乐端
# ---------------------------------------------------------------------------
print("\n[6] cmd/main：代理启动失败时还原音乐 socket")
m = CMD_MAIN.read_text(encoding="utf-8")
check("cmd/main 定义 restore_socket", "restore_socket()" in m)
# 在 start 分支的失败路径里调用
fail_block = m[m.find("代理进程死亡") : m.find("代理进程死亡") + 900]
check("启动失败路径调用 restore_socket", "restore_socket" in fail_block, fail_block[:200] if "restore_socket" not in fail_block else "")

# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过 ✓")
