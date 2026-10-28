#!/usr/bin/env python3
"""install_callback 的「目标 socket 被谁持有」检测测试。

背景：这段逻辑前后踩过两次坑，都是"看着能跑"的类型 ——

1. 旧实现 `ss -xlp | ... | head -1` 只取一个 pid，等于抽盲盒；
   且靠"能不能从 healthz 嗅到 upstream 字段"判断是不是扩展，导致
   **重装自己必然误报**、真被占用时反而可能漏报。
2. 重写时我加过 `tail -n +2` 想去表头，结果 `ss -x` 输出**本来就没有表头**，
   唯一那一行被删掉 → 检测静默失效（永远返回"读不到 pid"）。

所以这里用真进程 + 真 unix socket 测四个身份场景，而不是只做静态断言。

运行：env -u PYTHONPATH <venv python> tests/test_socket_owner.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
INSTALL_CALLBACK = PKG / "cmd" / "install_callback"

PASS, FAIL = [], []


# 只匹配稳定前缀，不写死整句文案：这句话的措辞改过一次，
# 测试跟着改很容易漏，而漏掉的测试比没有测试更糟。
WARN_MARK = "WARN: 音乐接口"

def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL  {name}: {detail}")


ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-sockown-"))
SOCK = str(ROOT / "trim_music.socket")
# 只替换 TARGET_SOCK 定义行，其余检测逻辑保持原样 —— 测的是真脚本。
SCRIPT = ROOT / "install_callback.sh"
src = INSTALL_CALLBACK.read_text(encoding="utf-8")
needle = 'TARGET_SOCK="/var/run/trim_music.socket"'
if needle not in src:
    print(f"!! 找不到 TARGET_SOCK 定义行，脚本结构变了：{needle}")
    sys.exit(2)
SCRIPT.write_text(src.replace(needle, f'TARGET_SOCK="{SOCK}"'), encoding="utf-8")

HOLDER = """
import socket, time
s = socket.socket(socket.AF_UNIX)
s.bind(%r)
s.listen(5)
time.sleep(30)
"""


def run_install(appdest: Path) -> str:
    """跑一次 install_callback，返回日志全文。

    占用检测在 venv 之前，所以不需要完整夹具 —— 后面步骤失败退出也不影响。
    """
    appdest.mkdir(parents=True, exist_ok=True)
    etc = ROOT / "pkg" / "fnmusic_kugou_pro" / "etc"
    var = ROOT / "pkg" / "fnmusic_kugou_pro" / "var"
    home = ROOT / "pkg" / "fnmusic_kugou_pro" / "home"
    for d in (etc, var, home):
        d.mkdir(parents=True, exist_ok=True)
    log = ROOT / "install.log"
    if log.exists():
        log.unlink()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(ROOT),
        "TRIM_APPDEST": str(appdest),
        "TRIM_PKGETC": str(etc),
        "TRIM_PKGVAR": str(var),
        "TRIM_PKGHOME": str(home),
        "TRIM_TEMP_LOGFILE": str(log),
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def hold(cwd: Path):
    """在 cwd 下起一个持有 SOCK 的进程，返回 Popen。

    cwd 决定身份：真脚本按 /proc/<pid>/exe 与 cwd 判断"属于哪个应用目录"。
    exe 是 python 解释器（不在应用目录），所以真正起作用的是 cwd ——
    这也和真机一致（uvicorn 的 exe 是 venv 里的 python，cwd 才是应用目录）。
    """
    cwd.mkdir(parents=True, exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    p = subprocess.Popen([sys.executable, "-c", HOLDER % SOCK], cwd=str(cwd))
    for _ in range(60):
        if os.path.exists(SOCK):
            break
        time.sleep(0.1)
    # 再等一拍，确保 listen 已生效（ss 才看得到 users:(...)）
    time.sleep(0.5)
    return p


print("=" * 70)
print("目标 socket 持有者识别")
print("=" * 70)

try:
    # 1) 本应用自己持有 —— 覆盖安装/重装，必须判"正常"，不能告警
    p = hold(ROOT / "self" / "appcenter" / "fnmusic_kugou_pro" / "proxy")
    try:
        log = run_install(ROOT / "self" / "appdest")
    finally:
        p.terminate()
        p.wait(timeout=10)
    check("自己持有 → 判为正常覆盖安装", "由本应用自己持有" in log, log[-500:])
    check("自己持有 → 不告警", WARN_MARK not in log, log[-500:])

    # 2) 飞牛音乐本体持有 —— 正常
    p = hold(ROOT / "music" / "appcenter" / "trim.music")
    try:
        log = run_install(ROOT / "music" / "appdest")
    finally:
        p.terminate()
        p.wait(timeout=10)
    check("音乐本体持有 → 判为正常", "由飞牛音乐本体持有" in log, log[-500:])
    check("音乐本体持有 → 不告警", WARN_MARK not in log, log[-500:])

    # 3) 其他音源扩展占用 —— 必须告警（这是这段代码存在的理由）
    p = hold(ROOT / "other" / "appcenter" / "fnmusic_ext_kugou")
    try:
        log = run_install(ROOT / "other" / "appdest")
    finally:
        p.terminate()
        p.wait(timeout=10)
    check("其他扩展占用 → 告警", WARN_MARK in log, log[-800:])
    check("告警里带 pid 与路径", "pid=" in log and "fnmusic_ext_kugou" in log, log[-800:])

    # 4) 没有 socket —— 什么都不该说
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    log = run_install(ROOT / "nosock" / "appdest")
    check("无 socket → 不输出占用结论",
          "由本应用自己持有" not in log and "被其他音源扩展占用" not in log, log[-400:])

    # 5) 静态守卫：那两个踩过的坑不许回来
    body = INSTALL_CALLBACK.read_text(encoding="utf-8")
    check("不再用 head -1 抽盲盒", "| head -1 |" not in body or "pid=[0-9]*' | head -1" not in body)
    check("不再靠 healthz 嗅探判断占用", "_ext/healthz" not in body)
    check("不再用 tail -n +2 去表头（ss -x 无表头，会删掉唯一一行）",
          "tail -n +2 | grep -F" not in body)
    check("按 /proc/<pid>/exe 判身份", '/proc/$_p/exe' in body)
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
