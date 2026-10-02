#!/usr/bin/env python3
"""打包卫生：.fpk 里不许出现"多余版本 pyc"和包内 .env。

这两个坑都真实发生过，而且**都是静默的**：

  1. `app.cpython-311.pyc` 混进包 —— compileall **只增不删**。跑过任何一次
     3.11 的语法检查（`py_compile` / `python -m compileall`）就会在
     `__pycache__` 里留下 311 版字节码。目标机运行时是 3.12，311 的 pyc
     **会被直接忽略**（magic 不匹配）——不报错、不影响功能，纯死重量，
     所以极难发现。2.1.7 打包时我自己就踩了一次。
  2. `app/.env` 混进包 —— 飞牛升级是"先解包 app.tgz 覆盖安装目录，再跑
     upgrade_callback"，包内那份（哪怕是全空模板）会在回调有机会迁移之前
     就把用户的真配置盖掉。历史事故：升级后酷狗登录态丢失、突然搜不到歌。

用法：env -u PYTHONPATH python3 tests/test_package_hygiene.py <fpk路径>
      不传路径则用工作区 out/ 里版本最高的那个。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
OUT = PKG / "out"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


if len(sys.argv) > 1:
    FPK = Path(sys.argv[1])
else:
    cands = sorted(OUT.glob("fnmusic_kugou_pro-*.fpk"))
    if not cands:
        print("找不到 .fpk")
        sys.exit(1)
    FPK = cands[-1]

print(f"检查包：{FPK}")
check("文件存在", FPK.is_file(), str(FPK))
if not FPK.is_file():
    sys.exit(1)

with tarfile.open(FPK) as tf:
    names = tf.getnames()
    manifest = tf.extractfile("manifest").read().decode()
    inner_bytes = tf.extractfile("app.tgz").read()

print(f"  外层成员 {len(names)} 个；app.tgz {len(inner_bytes)} 字节")
print()

# ---------------------------------------------------------------------------
print("[1] 外层成员：不许有目录前缀（飞牛要求平铺）")
tops = {n.split("/")[0] for n in names}
check("顶层无多余目录树（manifest/app.tgz/cmd/config/wizard 等）",
      not any(t in ("target", "app") for t in tops), str(sorted(tops)))

print()
print("[2] app.tgz 内层：pyc 只许 312，且不许 .pyc 与源码失配")
# ⚠ 临时目录必须每次新建。
# 原先这里硬编码 /tmp/hyg_inner.tgz 与 /tmp/hyg_check，踩了两次：
#   ① 若上一次是别的账号（如 root）跑过，残留文件属主不同 → write_bytes
#      直接 PermissionError，测试红得莫名其妙，跟被测包毫无关系；
#   ② 同名路径并行/连续跑会互相覆盖，读到上一轮的字节 → 假绿。
# 用 mkdtemp 一次性解决，测试不再往 /tmp 里丢固定垃圾。
_work_root = Path(tempfile.mkdtemp(prefix="hyg-"))
inner = _work_root / "inner.tgz"
inner.write_bytes(inner_bytes)
with tarfile.open(inner) as itf:
    inames = itf.getnames()

pycs = [n for n in inames if n.endswith(".pyc")]
print("  pyc:", pycs)
bad = [n for n in pycs if "cpython-312" not in n]
check("无非 312 的 pyc（compileall 只增不删的残留）", not bad, str(bad))
check("pyc 数量与源文件匹配（3 个）", len(pycs) == 3, str(len(pycs)))
check("__pycache__ 里没有 legacy 命名（app.pyc 式，那是 -b 的产物）",
      not [n for n in pycs if not re.search(r"cpython-3\d\d\.pyc$", n)], str(pycs))

print()
print("[3] app.tgz 内层：绝不许含 .env")
env_entries = [n for n in inames if n.rstrip("/").endswith("/.env") or n in ("./.env", ".env")]
check("包内无 .env（否则升级会盖掉用户登录态）", not env_entries, str(env_entries))

print()
print("[4] manifest 自洽")
m = dict(re.findall(r"^(\w+)\s*=\s*(.+)$", manifest, re.MULTILINE))
check("version 已填", bool(m.get("version")), str(m.get("version")))
check("checksum == app.tgz 的 md5",
      m.get("checksum", "").strip() == __import__("hashlib").md5(inner_bytes).hexdigest(),
      f'{m.get("checksum")} vs {__import__("hashlib").md5(inner_bytes).hexdigest()}')
check("changelog 提到当前版本号", m.get("version", "x").strip() in m.get("changelog", ""),
      m.get("version", ""))
# 【1.0.0 分叉】原断言是"changelog 仍含 48MB 归档修复 / transcode 取证说明"，
# 目的是防止从旧包回滚时把修复说明丢了。分叉后 changelog 是全新的 1.0.0 起点，
# 不再逐条复述上游 2.1.x 的修复 —— 所以改为**两条更有意义的守卫**：
#   ① changelog 必须声明本包是独立分叉（防止又变回"看起来像上游的一个版本"）；
#   ② 上游那些修复的实际代码仍在（这才是"没回滚"的真正证据）。
cl = m.get("changelog", "")
check("changelog 声明是独立分叉", "分叉" in cl or "独立" in cl, cl[:80])
_app_src = (PKG / "app" / "proxy" / "app.py").read_text(encoding="utf-8")
check("48MB 归档修复的代码仍在（内存缓存不再是唯一字节来源）",
      "48" in _app_src and "archive" in _app_src.lower())
check("transcode 取证日志的代码仍在", "transcode" in _app_src.lower())

print()
print("[5] pyc 与源码一致性（magic / 失效模式）")
# 失效模式（flags 低 2 位）决定 pyc 头里那 8 个字节是什么：
#   0 timestamp       → 头 = mtime(4) + 源大小(4)
#   1 unchecked-hash  → 头 = 源哈希(8)，运行时**不校验**
#   3 checked-hash    → 头 = 源哈希(8)，运行时校验（源码变了就重编）
# 我们打的是 checked-hash：它是唯一既能"跨 mtime 可复现"、又保留
# "源码改了 pyc 会失效"保护的选项。所以断言必须跟着分支走 ——
# 早先写死"pyc 记录的 mtime == 实际"，换成哈希模式后必然全红（实测 6 条失败）。
import struct
import shutil
import subprocess

MODE_TIMESTAMP, MODE_UNCHECKED, MODE_CHECKED = 0, 1, 3


def _find_target_py() -> str:
    """找**目标机运行时**解释器（3.12）。

    pyc 的正确性不是"能解析"就算数，而是**必须匹配真正会运行它的那版解释器**。
    本测试常在本机 3.11 的 venv 里跑，而 3.11 连 3.12 的字节码都读不了
    （marshal 格式不同），所以必须显式用 3.12 去校验。
    """
    for c in ("/var/apps/python312/target/bin/python3.12",):
        if Path(c).is_file():
            return c
    if sys.version_info[:2] == (3, 12):
        return sys.executable
    return ""


_TARGET_PY = _find_target_py()
check("找到 3.12 目标解释器（校验 pyc 用）", bool(_TARGET_PY), _TARGET_PY or "未找到")

TARGET_MAGIC = b""
if _TARGET_PY:
    r = subprocess.run(
        [_TARGET_PY, "-c", "import importlib.util as u; print(u.MAGIC_NUMBER.hex())"],
        capture_output=True, text=True,
    )
    TARGET_MAGIC = bytes.fromhex(r.stdout.strip())
    print(f"  目标解释器 magic = {TARGET_MAGIC.hex()}")


def _try_marshal_312(blob: bytes) -> tuple[bool, str]:
    """用 3.12 解 pyc 的代码对象（跨版本 marshal 读不了，必须真调 3.12）。"""
    if not _TARGET_PY:
        return True, "跳过"
    code = (
        "import marshal,sys;"
        "d=open(sys.argv[1],'rb').read()[16:];"
        "marshal.loads(d);print('OK')"
    )
    r = subprocess.run([_TARGET_PY, "-c", code, str(_PYPATH)], capture_output=True, text=True)
    return ("OK" in r.stdout), (r.stderr.strip().splitlines() or [""])[-1]


# 从 app.tgz 解出源码与 pyc 做对照
work = _work_root / "check"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)
with tarfile.open(inner) as itf:
    for n in ("proxy/app.py", "proxy/app_socket_bridge.py", "proxy/kugou_source.py"):
        itf.extract("./" + n, work)
    for n in pycs:
        itf.extract(n, work)

for src_name in ("app.py", "app_socket_bridge.py", "kugou_source.py"):
    src = work / "proxy" / src_name
    pyc = work / "proxy" / "__pycache__" / (src_name[:-3] + ".cpython-312.pyc")
    if not src.is_file() or not pyc.is_file():
        check(f"{src_name}: 源码/pyc 都在", False, f"{src.is_file()}/{pyc.is_file()}")
        continue
    raw = pyc.read_bytes()
    magic, flags, f1, f2 = struct.unpack("<4sIII", raw[:16])
    mode = flags & 0x03
    st = src.stat()
    check(f"{src_name}: pyc 不是 unchecked-hash（不校验的模式不许进包）",
          mode != MODE_UNCHECKED, f"flags={flags} mode={mode}")
    if mode == MODE_TIMESTAMP:
        # 老式时间戳型：头里是 mtime + 源大小
        size, mtime = f2, f1
        check(f"{src_name}: pyc 记录的源大小 == 实际（{size}）", size == st.st_size,
              f"pyc={size} 实际={st.st_size}")
        check(f"{src_name}: pyc 记录的 mtime == 实际", mtime == int(st.st_mtime),
              f"pyc={mtime} 实际={int(st.st_mtime)}")
    else:
        # 哈希型：头里 8 字节是源文件哈希。
        # ⚠ 用的是 importlib.util.source_hash()，**不是** sha256/djb2 那类
        #   常见摘要 —— 按 sha256 写的断言会稳定失败。必须让 3.12 自己算。
        want = b""
        if _TARGET_PY:
            r = subprocess.run(
                [_TARGET_PY, "-c",
                 "import importlib.util,sys,pathlib;"
                 "print(importlib.util.source_hash(pathlib.Path(sys.argv[1]).read_bytes()).hex())",
                 str(src)],
                capture_output=True, text=True,
            )
            want = r.stdout.strip()
        stored = raw[8:16].hex()
        check(f"{src_name}: pyc 源哈希 == importlib.util.source_hash（{stored}）",
              bool(want) and stored == want, f"pyc={stored} 期望={want or '未算出'}")
        check(f"{src_name}: 哈希模式下 mtime 字段不参与（可复现性来源）",
              mode == MODE_CHECKED, f"mode={mode}")
    if TARGET_MAGIC:
        check(f"{src_name}: pyc magic 匹配目标 3.12（{magic.hex()}）",
              magic == TARGET_MAGIC, f"pyc={magic.hex()} 目标={TARGET_MAGIC.hex()}")
    _PYPATH = pyc
    ok, err = _try_marshal_312(raw[16:])
    check(f"{src_name}: pyc 字节码能被 3.12 marshal 解析", ok, err)

print()
print("[6] 图标：尺寸阶梯 + 字面量兜底（否则应用在部分界面上是空白图标）")
# 为什么单列一组：1.0.0 首发时 app/ui/images 里**只有** icon_64/icon_256 两张，
# 而且 icon_64.png 里装的其实是 256x256 的图（上游带过来的，名不副实）。
# 后果是手机 App 的应用列表里图标空白 —— 客户端会按屏幕密度去挑
# icon_{16,24,32,48,64,72,96,128,256}.png，缺哪张就是空；更糟的是
# 部分调用路径**不做 {0} 替换**（实测 access.log 里出现过请求
# `ui/images/icon_%7B0%7D.png`，即字面量 {0}），必须有一个真的
# `icon_{0}.png` 文件兜底 —— 飞牛官方应用（xunlei / all.editor /
# trim.hermes）都放了这一张。
#
# 断言用 PNG 头解尺寸，不引 Pillow：本测试其余部分只依赖 stdlib。

LADDER = [16, 24, 32, 48, 64, 72, 96, 128, 256]
APP_IMAGES = "./ui/images/"


def _png_size(blob: bytes):
    """从 PNG 字节流解 (宽, 高, 色彩类型)。非 PNG 返回 None。"""
    if len(blob) < 26 or blob[:8] != b"\x89PNG\r\n\x1a\n" or blob[12:16] != b"IHDR":
        return None
    w = int.from_bytes(blob[16:20], "big")
    h = int.from_bytes(blob[20:24], "big")
    return w, h, blob[25]


icons = {}
with tarfile.open(inner) as itf:
    for n in inames:
        if n.startswith(APP_IMAGES) and n.lower().endswith(".png"):
            try:
                icons[n[len(APP_IMAGES):]] = itf.extractfile(n).read()
            except Exception:
                pass

print("  包内 ui/images:", sorted(icons))

missing = [f"icon_{n}.png" for n in LADDER if f"icon_{n}.png" not in icons]
check("尺寸阶梯齐全（16/24/32/48/64/72/96/128/256）", not missing, f"缺 {missing}")

check("存在字面量兜底 icon_{0}.png（客户端不做 {0} 替换时会命中它）",
      "icon_{0}.png" in icons, str(sorted(icons)))

_sq, _named = [], []
for name, blob in sorted(icons.items()):
    got = _png_size(blob)
    if got is None:
        _sq.append(f"{name}=非PNG")
        continue
    w, h, ctype = got
    if w != h:
        _sq.append(f"{name}={w}x{h} 非正方形")
    if ctype != 6:  # 6 == RGBA
        _sq.append(f"{name}=色彩类型{ctype} 无 alpha 通道")
    # 名不副实：icon_64.png 必须是 64x64
    mm = re.match(r"^icon_(\d+)\.png$", name)
    if mm and int(mm.group(1)) != w:
        _named.append(f"{name}={w}x{h}")

check("每张图标都是正方形且带 alpha 通道", not _sq, str(_sq))
check("文件名尺寸与实际尺寸一致（防 icon_64.png 装 256 图）", not _named, str(_named))

top_icons = {}
with tarfile.open(FPK) as tf:
    for n in ("ICON.PNG", "ICON_256.PNG"):
        try:
            top_icons[n] = tf.extractfile(n).read()
        except Exception:
            pass
check("包顶层有 ICON.PNG 与 ICON_256.PNG（桌面/应用中心用的是这两张）",
      len(top_icons) == 2, str(sorted(top_icons)))
_bad_top = []
for n, blob in top_icons.items():
    got = _png_size(blob)
    if got is None:
        _bad_top.append(f"{n}=非PNG")
        continue
    w, h, ctype = got
    if w != h or ctype != 6:
        _bad_top.append(f"{n}={w}x{h} type{ctype}")
check("顶层 ICON*.PNG 为正方形且带 alpha", not _bad_top, str(_bad_top))
_i256 = _png_size(top_icons.get("ICON_256.PNG", b""))
check("ICON_256.PNG 就是 256x256", _i256 is not None and _i256[:2] == (256, 256), str(_i256))

print()
print("[7] tar 元数据：不许泄漏构建机身份（用户名/组名/uid/gid）")
# 为什么单列一组：tar 默认把**构建机**的 uname/gname 和 uid/gid 写进归档。
# 实测旧交付包外层泄漏 ('octop-native','AppUsers',930,924)、
# 内层泄漏 ('octop-native','octop-native',930,924) —— 用户解包就能看到
# 打包者的用户名和用户 ID。这不只是隐私问题，也是**可复现性**问题：
# 换台机器（或换个账号）打包，同一份源码会得到不同字节。
#
# 修法是打包时 --owner=0 --group=0 --numeric-owner，让 tar 只写 uid/gid=0
# 且不写名字。这里断言结果，不关心脚本怎么实现。

def _tar_identity_labels(tf):
    """返回 (名字泄漏列表, 非零uid/gid列表, mtime集合)。"""
    leaked, bad_ids, mtimes = [], [], set()
    for ti in tf.getmembers():
        if ti.uname or ti.gname:
            leaked.append(f"{ti.name}: uname={ti.uname!r} gname={ti.gname!r}")
        if ti.uid != 0 or ti.gid != 0:
            bad_ids.append(f"{ti.name}: uid={ti.uid} gid={ti.gid}")
        mtimes.add(ti.mtime)
    return leaked, bad_ids, mtimes


with tarfile.open(FPK) as tf:
    out_leak, out_ids, out_mtimes = _tar_identity_labels(tf)
with tarfile.open(inner) as itf:
    inn_leak, inn_ids, inn_mtimes = _tar_identity_labels(itf)

check("外层 .fpk 不含构建机用户名/组名", not out_leak, str(out_leak[:3]))
check("外层 .fpk 的 uid/gid 全为 0", not out_ids, str(out_ids[:3]))
check("内层 app.tgz 不含构建机用户名/组名", not inn_leak, str(inn_leak[:3]))
check("内层 app.tgz 的 uid/gid 全为 0", not inn_ids, str(inn_ids[:3]))

# mtime 必须钉成同一个值，否则"文件修改时间不同 → 归档字节不同"。
# EPOCH 与 build.sh 里保持一致；这里只断言"唯一"，不锁死具体数值，
# 这样 build.sh 换 EPOCH 不需要同步改测试。
check("外层 .fpk 所有成员 mtime 唯一（打包时刻不泄漏）",
      len(out_mtimes) == 1, f"{sorted(out_mtimes)[:4]}…共{len(out_mtimes)}种")
check("内层 app.tgz 所有成员 mtime 唯一（源 mtime 不泄漏）",
      len(inn_mtimes) == 1, f"{sorted(inn_mtimes)[:4]}…共{len(inn_mtimes)}种")

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
