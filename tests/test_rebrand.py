#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 星痕在此 (fnmusic_kugou_pro fork, 独立维护)
"""分叉改名回归测试 —— 扫描「即将打包的载荷」，不是整个工作区。

为什么要区分：工作区里天然含旧名的地方很多（构建说明、分析报告、out/ 里的
旧 fpk），扫工作区会开局就 FAIL、失去信号价值。真正要紧的是**打进 .fpk 的
那份内容**里不能残留旧名。

用法：
    python3 tests/test_rebrand.py            # 直接扫工程目录（对应 app.tgz 的根）
    python3 tests/test_rebrand.py <STAGE>    # 扫解包后的暂存目录
"""

import os
import re
import subprocess
import sys
import struct
from pathlib import Path

# 校验 pyc 必须用**目标机运行时**（3.12）。本测试可能在 3.11 的 venv 里跑，
# 而 3.11 连 3.12 的 pyc 都读不了；source_hash 算法跨版本是一致的，
# 但为了稳妥仍优先用 3.12 算。
_TARGET_PY = next(
    (c for c in ("/var/apps/python312/target/bin/python3.12",) if Path(c).is_file()),
    "",
)

# ---- 扫描根：默认本文件所在工程的 app/ 对应关系 -------------------------
# 工程布局： <root>/app/**  ->  app.tgz 内 ./**      （tar czf app.tgz -C app .）
#           <root>/cmd/**  ->  .fpk 内 cmd/**
#           <root>/manifest, config/, wizard/, ICON*  -> .fpk 顶层
ROOT = Path(__file__).resolve().parent.parent

OLD_NAMES = ("fnmusic_ext_kugou", "fnmusic-ext")

# 这些不出现在 .fpk 里，或属于"必须保留的溯源信息"
IGNORE_DIR_NAMES = {"__pycache__", ".git", "out", ".venv-proxy", ".venv", "tests"}
IGNORE_SUFFIX = (".pyc", ".pyo", ".fpk", ".tgz", ".png", ".jpg", ".jpeg",
                 ".ico", ".PNG", ".gz", ".zip", ".so")
DOC_FILES = {"分析报告.md", "构建说明.md", "说明.md", "README.md", "CHANGELOG.md"}

# ⚠ 白名单精确到「行」，且**只允许 GPL 要求的溯源署名**——不可扩张。
#   每一条都必须能解释成"因为授权要求而必须出现的上游名字"。
ALLOW_PATTERNS = [
    # GPL-2.0 要求的原作者署名行（源码头部 / 说明.md）
    r"Copyright \(C\) 2026 ai2ku \(https://github\.com/ai2ku/\)",
    # 说明.md / manifest changelog 里的来源说明
    r"基于 ai2ku 的 (开源项目 )?`?fnmusic_ext_kugou`?",
    r"上游[：:].{0,20}fnmusic_ext_kugou",
    r"上游 fnmusic_ext_kugou",
    r"与上游 fnmusic_ext_kugou",
    r"旧版 fnmusic_ext_kugou",
    r"卸载旧版 fnmusic_ext_kugou",
    r"fnmusic_ext_kugou（作者 ai2ku",
    r"fnmusic_ext_kugou `?2\.1\.0`?",
    r"基线(版本)? ?2\.1\.0",
    # 旧版升级路径相关注释（解释"为什么删掉迁移逻辑"必须提到旧版本号）
    r"fnmusic_ext_kugou \*\*2\.1\.0\*\*",
    # ── 用户可见的"卸载指引"：必须点名旧应用，否则用户不知道卸哪个 ──
    r"上游 fnmusic_ext_kugou",
    r"旧扩展 fnmusic_ext_kugou",
    r"与上游 fnmusic_ext_kugou",
    r"卸载旧版 fnmusic_ext_kugou",
    r"上游 fnmusic_ext_kugou）占用",
    r"旧版 fnmusic_ext_kugou，再",
    r"fnmusic_ext_kugou 2\.1\.0",
    # 拦截提示语里必须点名旧版，否则用户不知道去卸谁
    r"旧扩展 fnmusic_ext_kugou",
    r"旧版 \*\*fnmusic_ext_kugou\*\*",
    r"`fnmusic_ext_kugou`",
    r"/vol1/@appconf/fnmusic_ext_kugou",
]

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def walk(root: Path):
    """遍历，跳过忽略项。返回 [(Path, relpath)]"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIR_NAMES]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in IGNORE_SUFFIX:
                continue
            if fn in DOC_FILES:
                continue
            out.append(p)
    return out


def allowed(line: str) -> bool:
    return any(re.search(pat, line) for pat in ALLOW_PATTERNS)


# ══════════════════════════════════════════════════════════
print("=" * 60)
print("test_rebrand.py — 分叉改名回归测试")
print(f"扫描根: {ROOT}")
print("=" * 60)

print("\n[1] 载荷内不得残留旧名（白名单外）")
bad = []
for p in walk(ROOT):
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
    for n, line in enumerate(text.splitlines(), 1):
        if any(o in line for o in OLD_NAMES):
            # 旧显示名也一并查
            if not allowed(line):
                bad.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    if "飞牛音乐酷狗扩展" in text:
        for n, line in enumerate(text.splitlines(), 1):
            if "飞牛音乐酷狗扩展" in line and not allowed(line):
                bad.append(f"{p.relative_to(ROOT)}:{n}: [旧显示名] {line.strip()[:100]}")
check("无旧名残留（fnmusic_ext_kugou / fnmusic-ext / 飞牛音乐酷狗扩展）",
      not bad, "\n        ".join(bad[:25]))

print("\n[2] 新名一致性（gatewayPrefix ↔ 路由前缀 ↔ GATEWAY_PREFIX ↔ 启动项）")
APP = ROOT / "app"
ui_cfg = (APP / "ui" / "config").read_text(encoding="utf-8")
app_py = (APP / "proxy" / "app.py").read_text(encoding="utf-8")
bridge = (APP / "proxy" / "app_socket_bridge.py").read_text(encoding="utf-8")
manifest = (ROOT / "manifest").read_text(encoding="utf-8")

NEW = "fnmusic_kugou_pro"
check("app/ui/config gatewayPrefix", f"/app/{NEW}" in ui_cfg)
check("app/ui/config gatewaySocket", f"{NEW}.sock" in ui_cfg)
check("app/ui/config 键名", f"{NEW}.main" in ui_cfg)
check("manifest desktop_applaunchname", f"desktop_applaunchname = {NEW}.main" in manifest)
check("manifest appname", f"appname               = {NEW}" in manifest)
check("app.py 路由前缀", f"/app/{NEW}" in app_py)
check("app.py _APP_NAME", f'_APP_NAME = "{NEW}"' in app_py)
check("bridge GATEWAY_PREFIX", f'GATEWAY_PREFIX = "/app/{NEW}"' in bridge)

print("\n[3] 旧版迁移逻辑必须已删除")
for f, needle in [
    ("cmd/install_callback", "LEGACY_ENV_FILE"),
    ("cmd/install_callback", "legacy_env_has_data"),
    ("cmd/upgrade_callback", "OLD_ENV"),
    ("cmd/upgrade_callback", "迁移"),
]:
    p = ROOT / f
    content = p.read_text(encoding="utf-8") if p.exists() else ""
    # 允许出现在"说明为什么删掉"的注释里，但不允许出现可执行代码
    code_lines = [ln for ln in content.splitlines()
                  if needle in ln and not ln.lstrip().startswith("#")]
    check(f"{f} 无 {needle} 可执行残留", not code_lines,
          "; ".join(code_lines[:3]))

print("\n[4] 拦截逻辑存在（install 告警 + start 拒绝）")
main_sh = (ROOT / "cmd" / "main").read_text(encoding="utf-8")
inst = (ROOT / "cmd" / "install_callback").read_text(encoding="utf-8")
check("cmd/main 有 foreign_proxy_owns_target()", "foreign_proxy_owns_target()" in main_sh)
check("cmd/main start 分支调用拦截", "if foreign_proxy_owns_target; then" in main_sh)
check("cmd/main 有 identify_owner()", "identify_owner()" in main_sh)
check("拦截判断在'清理残留'之前",
      main_sh.find("if foreign_proxy_owns_target; then")
      < main_sh.find('log "清理残留代理 socket"'),
      "顺序反了会导致抢占别人的 socket")
check("cmd/install_callback 有占用告警", "音乐接口已被其他音源扩展占用" in inst)
check("安装期不阻断（告警而非 exit 1）", "WARN" in inst)

print("\n[5] .pyc 必须是 3.12、且与源码同步")
pycache = APP / "proxy" / "__pycache__"
if pycache.is_dir():
    pycs = sorted(pycache.glob("*.pyc"))
    check("存在 .pyc", bool(pycs))
    others = [p.name for p in pycs if "cpython-312" not in p.name]
    check("无 3.11 等其它版本字节码", not others, f"混入: {others}")
    stale = []
    for p in pycs:
        m = re.search(r"^(.*)\.cpython-312\.pyc$", p.name)
        if not m:
            continue
        src = APP / "proxy" / f"{m.group(1)}.py"
        if not src.exists():
            continue
        # ⚠ pyc 头的后 8 字节含义**取决于失效模式（flags 低 2 位）**：
        #   0 timestamp → mtime(4) + 源大小(4)
        #   1/3 hash    → 源哈希(8)，**没有源大小字段**
        # 打包用的是 checked-hash（为了可复现，见 build.sh），
        # 所以老式"读 size 比大小"的判据必须换成"比哈希"。
        # 这里用 3.12 自己算 importlib.util.source_hash 来比对 ——
        # 不用 sha256，那个算法对不上（实测过）。
        raw = p.read_bytes()
        flags = struct.unpack("<I", raw[4:8])[0]
        mode = flags & 0x03
        if mode == 0:
            _mtime, size = struct.unpack("<II", raw[8:16])
            if size != src.stat().st_size:
                stale.append(f"{p.name}(记录{size}≠实际{src.stat().st_size})")
        else:
            stored = raw[8:16].hex()
            r = subprocess.run(
                [_TARGET_PY or sys.executable, "-c",
                 "import importlib.util,sys,pathlib;"
                 "print(importlib.util.source_hash(pathlib.Path(sys.argv[1]).read_bytes()).hex())",
                 str(src)],
                capture_output=True, text=True,
            )
            want = r.stdout.strip()
            if not want or stored != want:
                stale.append(f"{p.name}(哈希{stored}≠源码{want or '未算出'})")
    check("无陈旧 .pyc（pyc 记录的源指纹 == 当前源码）", not stale, "; ".join(stale))
    # 断言 pyc 里残留的旧名**不超过**其源码。
    # 为什么这样判：
    #   · .pyc 会内联 docstring 与字符串字面量（但不含注释），所以
    #     源码里正当出现的"上游 fnmusic_ext_kugou 2.1.0"这类说明
    #     必然也会出现在 pyc 里 —— 那是合理的，不是残留。
    #   · 真正要抓的是**陈旧 pyc**：改名前的字节码会带着大量旧名。
    #     因此只要 pyc 的旧名数 > 源码的旧名数，就说明这个 pyc 不是
    #     由当前源码编出来的（漏编 / 用了旧解释器 / 没清 __pycache__）。
    # 这比"pyc 必须 0 个旧名"更严格 —— 后者会逼我们把正当说明文字也删掉。
    pyc_bad = []
    for p in pycs:
        m = re.search(r"^(.*)\.cpython-312\.pyc$", p.name)
        if not m:
            continue
        src = APP / "proxy" / f"{m.group(1)}.py"
        if not src.exists():
            continue
        sb = src.read_bytes()
        pb = p.read_bytes()
        for token in (b"fnmusic_ext_kugou", b"fnmusic-ext"):
            n_pyc, n_src = pb.count(token), sb.count(token)
            if n_pyc > n_src:
                pyc_bad.append(
                    f"{p.name}:{token.decode()} pyc={n_pyc} > src={n_src}（疑似陈旧字节码）")
    check("打包进包的 .pyc 与源码同源（旧名数不超源码）", not pyc_bad, "; ".join(pyc_bad))
else:
    check("存在 __pycache__（打包前应已用 3.12 编译）", False,
          "未编译：打包前请跑 python3.12 -m compileall -q app/proxy")

print("\n[6] app.tgz 不得包含 .env（覆盖安装铁律）")
import tarfile
# 两种布局都要容：
#   · 工作区  -> ROOT=包根，app.tgz 在 ROOT 或 ROOT/out
#   · 解包验证 -> ROOT=pkg/，此时 app.tgz 已被展开成 pkg/app/，
#                 原始 app.tgz 只剩在 .fpk 里（用 test_package_hygiene 验它）
cands = [ROOT / "out" / "app.tgz", ROOT / "app.tgz", ROOT.parent / "inner.tgz"]
tgz = next((p for p in cands if p.exists()), None)
if tgz is None:
    print("  SKIP  本布局无 app.tgz（解包验证场景；已由 test_package_hygiene 覆盖）")
else:
    with tarfile.open(tgz) as t:
        names = t.getnames()
    envs = [n for n in names if n.endswith(".env") or "/.env" in n]
    check("app.tgz 内无 .env", not envs, f"含: {envs}")
    check("app.tgz 内无 venv", not [n for n in names if ".venv" in n])

print("\n[7] 说明.md 必备内容")
# 说明.md 是**交付物**（跟 .fpk 放一起给用户），不随 app.tgz 分发，
# 所以解包场景下找不到是正常的。
doc = ROOT / "说明.md"
if not doc.exists() and (ROOT / "app").is_dir():
    print("  SKIP  解包场景无 说明.md（交付物，不在 app.tgz 内）")
elif doc.exists():
    d = doc.read_text(encoding="utf-8")
    check("含上游来源声明", "ai2ku" in d and "fnmusic_ext_kugou" in d)
    check("含 GPL-2.0 声明", "GPL-2.0" in d)
    check("含'先卸旧版'流程", "先卸载旧版" in d or "先卸载" in d)
    check("含 FAQ", "FAQ" in d or "常见问题" in d)
    check("含本分支定位", "长期维护起点" in d)
else:
    check("说明.md 存在", False)

print("\n[8] 打包载荷内不得含本机部署事实（可分发产物必须通用）")
# 为什么单列一组：分叉时我曾"好心"把向导默认值/提示改成指向本机 4000，
# 理由是"新装即正确"。这是错的 —— wizard/ 会打进 .fpk 分发给别人，
# 而对方的机器上根本没有 MediaGo，8899 也可能空着或跑着正常的酷狗 API。
# 把本机巧合写成产品事实，既误导别人，也让包不再通用。
# 判据：载荷文件（wizard/ app/ cmd/ config/ manifest）里不得出现本机专用线索。
MACHINE_FACTS = ["MediaGo", "127.0.0.1:4000"]
_scan = []
for _rel in ("wizard", "app/ui", "app/web", "cmd", "config"):
    _d = APP.parent / _rel if _rel.startswith("app") else ROOT / _rel
    if _d.is_dir():
        _scan += [p for p in _d.rglob("*") if p.is_file()]
if (ROOT / "manifest").exists():
    _scan.append(ROOT / "manifest")
_found = []
for _p in _scan:
    if "__pycache__" in str(_p) or _p.suffix in (".pyc", ".png", ".PNG"):
        continue
    try:
        _t = _p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for _n, _ln in enumerate(_t.splitlines(), 1):
        for _fact in MACHINE_FACTS:
            if _fact in _ln:
                _found.append(f"{_p.relative_to(ROOT)}:{_n}: {_ln.strip()[:80]}")
check("载荷内无本机部署事实（MediaGo / 4000）", not _found, "\n".join(_found))

# 反向守卫：说明.md 是交付文档（不随包分发），本机信息**应当**在那
_doc = ROOT / "说明.md"
if _doc.exists():
    _dt = _doc.read_text(encoding="utf-8")
    check("说明.md 里保留本机端口信息（该放本机事实的地方）",
          "127.0.0.1:4000" in _dt)

print("\n" + "=" * 60)
print(f"结果: PASS={PASS}  FAIL={FAIL}")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
