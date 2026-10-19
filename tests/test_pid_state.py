#!/usr/bin/env python3
"""P-01 回归：状态目录 / 判活 / 按 cmdline 认领进程 / 清理。

背景（P-01，真机已确认）
    1.0.0 把 PID 与日志放在 /tmp。这些文件一旦被清掉（重启、或任何清理脚本）：
      ① 应用中心读不到 PID → 判「已关闭」，而进程其实还在服务 —— 状态说谎；
      ② stop 同样读不到 PID → 杀不掉旧进程 → 它变成孤儿，
         与新实例同时挂同一个 socket，白占内存。
    修法：状态文件落 TRIM_PKGVAR；判活改成「PID 文件（带归属校验）
    → /proc 扫盘找回 → healthz 指纹」三级兜底；stop 两级双杀。

本测试两层：
    静态 —— 四个入口都 source 共享实现；状态文件不再硬编码 /tmp；bash -n 全过
    动态 —— 用 `exec -a` 造一个 cmdline 伪装的"代理进程"，验证
             (a) PID 文件丢了也能找回来并回写
             (b) PID 被复用时不会误认（更不会误杀无关进程）
             (c) kill 只打自己人，不碰另一个应用的进程
             (d) TRIM_PKGVAR 串到别的应用时拒绝使用
             (e) 网关桥接器被单独识别（不混进"代理进程"里）

跑法：env -u PYTHONPATH python3 tests/test_pid_state.py
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
LIB = PKG / "cmd" / "_proxy_state.sh"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    suffix = f"   [{detail}]" if detail and not cond else ""
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{suffix}")


def bash(script: str, **env_over: str) -> subprocess.CompletedProcess:
    """在干净的 STATE 环境下跑一段 bash（不碰真机的 socket / 状态文件）。"""
    env = dict(os.environ)
    env.update({
        "TRIM_PKGVAR": "",
        "TRIM_APPDEST": "",
        # 关键：指向不存在的路径。构建机上真实存在 /var/run/trim_music.socket
        # 且本应用可能正在跑 —— 不隔离的话"进程被杀光"这一步会被 healthz
        # 兜底判成"仍在运行"，测试就变成依赖机器状态。
        "FNMUSIC_TARGET_SOCK": str(ROOT / "no-such.sock"),
    })
    env.update(env_over)
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env, timeout=60)


def lib(cmdline: str, **env_over: str) -> subprocess.CompletedProcess:
    return bash(f". {shlex.quote(str(LIB))}\n{cmdline}", **env_over)


ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-pid-"))
# 安装目录结构：<ROOT>/install/app/proxy/run_proxy.sh
APP = ROOT / "install" / "app"
(APP / "proxy").mkdir(parents=True, exist_ok=True)
(APP / "proxy" / "run_proxy.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
(APP / "proxy" / "run_proxy.sh").chmod(0o755)
VAR = ROOT / "fnmusic_kugou_pro" / "var"
VAR.mkdir(parents=True, exist_ok=True)
# 另一个"音源扩展"：它的进程绝不能被我们认领/杀掉
OTHER = ROOT / "other_app"
(OTHER / "proxy").mkdir(parents=True, exist_ok=True)
(OTHER / "proxy" / "run_proxy.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
(OTHER / "proxy" / "run_proxy.sh").chmod(0o755)

print("=" * 70)
print("1) 静态：四个入口共用同一份状态实现，状态文件不再写 /tmp")
print("=" * 70)
for rel in ("cmd/main", "cmd/stop_callback", "cmd/uninstall_init",
            "cmd/uninstall_callback"):
    src = (PKG / rel).read_text(encoding="utf-8")
    check(f"{rel} 引用 _proxy_state.sh", "_proxy_state.sh" in src)

main_src = (PKG / "cmd" / "main").read_text(encoding="utf-8")
check("cmd/main 的 PID 文件落在持久状态目录",
      'PID_FILE="${STATE_DIR}/' in main_src)
check("cmd/main 不再把 PID 文件硬编码到 /tmp",
      not [ln for ln in main_src.splitlines()
           if ln.strip().startswith('PID_FILE="/tmp/')])
check("cmd/main 定义了旧位置兼容（只读）",
      'LEGACY_PID_FILE="/tmp/' in main_src and
      'LEGACY_APPDEST_FILE="/tmp/' in main_src)
# /tmp/fnmusic* 只允许出现在"注释 / 旧位置兼容"这两类行里
_bad = []
for _ln in main_src.splitlines():
    if "/tmp/fnmusic-pro" not in _ln:
        continue
    _s = _ln.strip()
    if _s.startswith("#") or "LEGACY" in _ln or "旧位置" in _ln:
        continue
    _bad.append(_s)
check("cmd/main 里 /tmp/fnmusic* 仅在旧位置兼容处出现", not _bad, str(_bad[:3]))

for rel in ("cmd/_proxy_state.sh", "cmd/main", "cmd/stop_callback",
            "cmd/uninstall_init", "cmd/uninstall_callback"):
    r = subprocess.run(["bash", "-n", str(PKG / rel)], capture_output=True, text=True)
    check(f"{rel} 语法检查（bash -n）", r.returncode == 0, r.stderr.strip()[:300])

print()
print("=" * 70)
print("2) 动态：状态目录解析（含归属校验）")
print("=" * 70)
r = lib("fnmusic_state_dir", TRIM_PKGVAR=str(VAR))
check("TRIM_PKGVAR 属于本应用 → 直接采用",
      r.stdout.strip() == str(VAR), r.stdout.strip())

r = lib("fnmusic_state_dir", TRIM_PKGVAR=str(ROOT / "otherapp" / "var"))
check("TRIM_PKGVAR 指向别的应用 → 拒绝（不得落到别处）",
      "otherapp" not in r.stdout, r.stdout.strip())

(VAR / "fnmusic-pro-appdest").write_text(str(APP.parent) + "\n", encoding="utf-8")
r = lib(f"fnmusic_app_dir {shlex.quote(str(VAR))}")
check("安装目录可从状态目录里的记录解析", r.stdout.strip() == str(APP), r.stdout.strip())
r = lib(f"fnmusic_app_dir {shlex.quote(str(VAR))}",
        TRIM_APPDEST=str(APP.parent))
check("TRIM_APPDEST 优先于记录", r.stdout.strip() == str(APP), r.stdout.strip())

print()
print("=" * 70)
print("3) 动态：造两个'代理进程'+一个桥接器，验证认领与清理")
print("=" * 70)


def spawn(app_dir: Path, argv0: str) -> subprocess.Popen:
    """造一个 cmdline 伪装的进程（/proc/<pid>/cmdline 里就是 argv0）。

    用 `exec -a` 而不是改名二进制：目标机没有 gdb/调试器，
    而 argv[0] 足够了 —— 判活逻辑读的就是 cmdline。
    """
    return subprocess.Popen(
        ["bash", "-c", f"exec -a {shlex.quote(argv0)} sleep 300"],
        cwd=str(app_dir), start_new_session=True,
    )


procs = []
try:
    proxy = spawn(APP, f"uvicorn app:app --app-dir {APP}/proxy --uds /tmp/x.sock")
    procs.append(proxy)
    other = spawn(OTHER, f"uvicorn app:app --app-dir {OTHER}/proxy --uds /tmp/y.sock")
    procs.append(other)
    # 桥接器：cmdline 里没有绝对路径，只能靠 /proc/<pid>/cwd 认领
    bridge = spawn(APP, "python3 ./proxy/app_socket_bridge.py")
    procs.append(bridge)
    time.sleep(0.4)

    r = lib(f"fnmusic_find_pids {shlex.quote(str(APP))}")
    found = r.stdout.split()
    check("扫盘认出本应用的代理进程", str(proxy.pid) in found, str(found))
    check("扫盘把网关桥接器排除在外", str(bridge.pid) not in found, str(found))
    check("扫盘不认别的应用的进程", str(other.pid) not in found, str(found))

    r = lib(f"fnmusic_find_bridge_pids {shlex.quote(str(APP))}")
    check("桥接器由单独的扫描函数认出", str(bridge.pid) in r.stdout.split(), r.stdout)

    pidfile = VAR / "fnmusic-pro.pid"
    probe = (f'if fnmusic_is_running {shlex.quote(str(APP))} '
             f'{shlex.quote(str(pidfile))}; then '
             f'echo "YES ${{FNMUSIC_LAST_PID}} ${{FNMUSIC_LAST_SOURCE}}"; '
             f'else echo "NO"; fi')
    r = lib(probe)
    check("PID 文件不存在时仍判为运行（扫盘找回）", r.stdout.startswith("YES"), r.stdout)
    check("找回的 PID 正确、来源标为 cmdline",
          r.stdout.split()[1:] == [str(proxy.pid), "cmdline"], r.stdout)
    check("PID 文件被自动回写（自愈）",
          pidfile.exists() and pidfile.read_text().strip() == str(proxy.pid),
          pidfile.read_text() if pidfile.exists() else "缺失")

    # PID 复用：文件里写的是"活着但无关"的进程
    pidfile.write_text(str(other.pid) + "\n", encoding="utf-8")
    r = lib(probe)
    check("PID 被复用时不会误认（仍认出真代理）",
          r.stdout.startswith("YES") and r.stdout.split()[1] == str(proxy.pid), r.stdout)
    check("误认的 PID 被纠正回写",
          pidfile.read_text().strip() == str(proxy.pid), pidfile.read_text())

    # kill：只打自己人
    r = lib(f"fnmusic_kill_all {shlex.quote(str(APP))} {shlex.quote(str(pidfile))}")
    time.sleep(0.5)
    try:
        proxy.wait(timeout=3)
        proxy_dead = True
    except subprocess.TimeoutExpired:
        proxy_dead = False
    try:
        bridge.wait(timeout=3)
        bridge_dead = True
    except subprocess.TimeoutExpired:
        bridge_dead = False
    check("kill_all 停掉代理进程", proxy_dead)
    check("kill_all 停掉网关桥接器", bridge_dead)
    check("kill_all 不碰别的应用的进程", other.poll() is None)
    check("kill_all 清掉 PID 文件", not pidfile.exists())

    r = lib(probe)
    check("杀干净后判活为假（healthz 也不认错人）", r.stdout.strip() == "NO", r.stdout)
finally:
    for p in procs:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
