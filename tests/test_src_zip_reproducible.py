#!/usr/bin/env python3
"""src.zip 可复现性：不许随本地时区变化。

背景（第 4 层可复现性缺陷）
--------------------------
`git archive --format=zip` 会把 commit 时间**转成本地时区**的 DOS 时间写进
zip 头（本地文件头 + 中央目录各一份）。于是同一 commit、只改 `TZ`：

    TZ=Asia/Shanghai  -> e1f3e3ab9af2daf56b391b6ea9d66db7
    TZ=UTC            -> ab3a3d412cc5b31a5173e40bc6796e6a

⇒ 我们在 `checksums.txt` 里公布的 src.zip 哈希**只在一个时区里成立**，
别人换台机器重建就"对不上"，会被误读成"包被动过手脚"。

注意 `.fpk` **不受影响**：`--format=tar` 写的是 unix epoch，三时区同哈希。
这一层只影响 `src.zip`。

修法：`tools/make_src_zip.py` —— `git archive --format=tar` 拿字节，
再用 Python 重打包为 zip，成员时间戳固定、`create_system=0`(MS-DOS)
因此不写 Unix 扩展字段。

本测试的四组断言
----------------
  [A] 四时区跑交付脚本 -> 同一哈希（要防的性质）
  [B] 与 `git archive --format=zip` 内容等价（成员名 + 逐文件 sha256）
      —— 换实现不能顺手把内容换了
  [C] **阴性对照**：直接 `git archive --format=zip` 在四时区下**必须不同**
      —— 否则说明这个测试根本测不出 TZ 依赖（比如 TZ 没生效、
      或 git 版本行为变了），[A] 的"通过"就是假绿。
  [D] 交付目录里的那份 src.zip 与四时区验证结果一致
      —— 光有 [A] 只能证明"脚本稳定"，证明不了"要交给用户的那个文件"
      就是刚验证过的那份（out/ 里可能是旧脚本的残留）。

[C] 与 [D] 是关键：只断言"新方法稳定"的测试，在 TZ 环境失效、
或交付物过期时会**静默恒真**。

用法：env -u PYTHONPATH python3 tests/test_src_zip_reproducible.py
依赖：git（仓库根必须带 .git）；从源码 zip 解包（无 .git）时自动 SKIP。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPT = REPO / "tools" / "make_src_zip.py"
APP_NAME = "fnmusic_kugou_pro"

# 版本号**从 manifest 读**，不要写死。
# 踩过：1.0.1 打包后本测试仍用 "1.0.0/" 作前缀去重新生成，
# 于是交付物与"验证值"永远不相等 —— 假失败，而且看起来像打包坏了。
VERSION = ""
_mf = REPO / "manifest"
if _mf.is_file():
    for _ln in _mf.read_text(encoding="utf-8", errors="ignore").splitlines():
        if _ln.strip().startswith("version"):
            VERSION = _ln.split("=", 1)[1].strip()
            break
if not VERSION:
    print("FAIL：读不到 manifest 里的 version")
    sys.exit(1)
PREFIX = f"{APP_NAME}-{VERSION}/"

TZS = ["UTC", "Asia/Shanghai", "America/New_York", "Pacific/Kiritimati"]

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


if not REPO.is_dir():
    print("找不到仓库根：", REPO)
    sys.exit(1)

# 从源码 zip 解包出来的载荷没有 .git，无法做 git archive —— 正常跳过。
if not (REPO / ".git").exists():
    print("SKIP：此载荷没有 .git（源码 zip 解包场景），跳过 src.zip 可复现性测试。")
    sys.exit(0)

if not SCRIPT.is_file():
    print(f"FAIL：找不到生成脚本 {SCRIPT}")
    sys.exit(1)

print("=" * 70)
print(f"src.zip 可复现性：{REPO}")
print("=" * 70)

work = Path(tempfile.mkdtemp(prefix="srczip-"))


def run_script(tz: str, out: Path) -> str:
    """跑交付脚本，返回 md5。"""
    env = dict(os.environ, TZ=tz)
    env.pop("PYTHONPATH", None)  # 宿主注入的 PYTHONPATH 会盖过解释器环境
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(REPO), str(out), PREFIX],
        capture_output=True, text=True, env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"脚本失败（TZ={tz}）：\n{r.stdout}\n{r.stderr}")
    return hashlib.md5(out.read_bytes()).hexdigest()


def git_archive_zip(tz: str, out: Path) -> str:
    """直接用 git 原生 zip（用来做对照）。"""
    env = dict(os.environ, TZ=tz)
    with out.open("wb") as fh:
        r = subprocess.run(
            ["git", "archive", "--format=zip", f"--prefix={PREFIX}", "HEAD"],
            cwd=REPO, stdout=fh, stderr=subprocess.PIPE, env=env,
        )
    if r.returncode != 0:
        raise RuntimeError(f"git archive 失败（TZ={tz}）：{r.stderr.decode()}")
    return hashlib.md5(out.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
print("\n[A] 交付脚本跨 TZ：必须同一哈希")
script_hashes = {}
for tz in TZS:
    h = run_script(tz, work / f"new-{tz.replace('/', '_')}.zip")
    script_hashes[tz] = h
    print(f"      TZ={tz:<22} -> {h}")
check(f"四时区生成同一哈希（{TZS[0]} 为准）",
      len(set(script_hashes.values())) == 1,
      str(script_hashes))

print("\n[B] 与 git 原生 zip 内容等价（换实现不能换内容）")


def digest(zp: Path) -> dict[str, str]:
    with zipfile.ZipFile(zp) as z:
        return {i.filename: hashlib.sha256(z.read(i)).hexdigest() for i in z.infolist()}


native = work / "native.zip"
git_archive_zip("Asia/Shanghai", native)
new_zip = work / f"new-{TZS[1].replace('/', '_')}.zip"
a, b = digest(native), digest(new_zip)
check("成员数量与名字集合一致", set(a) == set(b), f"{len(a)} vs {len(b)}")
diff = [k for k in a if a.get(k) != b.get(k)]
check("逐成员内容 sha256 全等", not diff, str(diff[:5]))

print("\n[C] 阴性对照：git 原生 zip 必须**随 TZ 变**")
native_hashes = {}
for tz in TZS:
    native_hashes[tz] = git_archive_zip(tz, work / f"nat-{tz.replace('/', '_')}.zip")
print(f"      {len(set(native_hashes.values()))} 个不同哈希 / {len(TZS)} 个时区")
# 若这里只有 1 个哈希，说明"TZ 依赖"这个性质在本环境下测不到，
# [A] 的稳定就可能是假绿（例如 TZ 变量没被 git 读到）。
check("原生 zip 确实随 TZ 变（否则本测试无法证伪）",
      len(set(native_hashes.values())) > 1,
      str(native_hashes))

print("\n[D] 交付物与已验证物必须是同一个东西")
# 只有 [A] 证明"脚本稳定"，还不足以说明**要交给用户的那个文件**也是稳定的
# —— 万一 out/ 里躺着的是上次用旧脚本生成的残留，[A] 照样全绿。
# 所以要显式比对交付目录里的那份。
# 只看**当前版本**那一份。out/ 里可能还躺着旧版本的目录，
# 它们的 prefix 不同（版本号不同），本来就不该与本轮验证同哈希 ——
# 把旧版本也算进来只会得到一条与"可复现性"无关的假失败。
_delivered = REPO / "out" / f"{APP_NAME}-{VERSION}" / f"{APP_NAME}-{VERSION}-src.zip"
if not _delivered.is_file():
    print(f"      （{_delivered} 不存在，跳过 —— 先跑一次 tools/build.sh）")
else:
    _dh = hashlib.md5(_delivered.read_bytes()).hexdigest()
    check(f"交付的 {_delivered.name} 与四时区验证结果一致",
          _dh == script_hashes[TZS[0]],
          f"交付={_dh} 验证={script_hashes[TZS[0]]}")

shutil.rmtree(work, ignore_errors=True)

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
