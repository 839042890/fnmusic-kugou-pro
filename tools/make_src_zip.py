#!/usr/bin/env python3
"""生成**跨时区可复现**的源码 zip。

为什么需要这个脚本
------------------
`git archive --format=zip` 会把 commit 时间**转成本地时区**的 DOS 时间写进
zip 头，于是同一个 commit 在不同 TZ 下打出的 zip 哈希不同（实测）：

    TZ=Asia/Shanghai      e1f3e3ab9af2daf56b391b6ea9d66db7
    TZ=UTC                ab3a3d412cc5b31a5173e40bc6796e6a
    TZ=America/New_York   4161105410926e6451c9070ac516c6d8
    TZ=Pacific/Kiritimati 9efddab2455a5760ff28c2a1a1ecbb12

差异只有 280 字节，且 **100% 落在 zip 头的 DOS 时间字段**（140 B 本地文件头
+ 140 B 中央目录副本）；两个 zip 解包后**内容逐字节相同**。

危害不在文件本身，而在"可复现"这个承诺：我们公布的源码包哈希，异地的
贡献者**无法复现** —— 甚至可能误以为下载到的包被人动过手脚。所以必须修。

为什么以 tar 为中间格式
----------------------
`git archive --format=tar` 写的是 unix epoch，**不受 TZ 影响**（`.fpk` 因此
对 TZ 免疫 —— 它内层就是 tar+gzip）。所以：拿 tar 当中间格式，再用本脚本
确定性地重新打包成 zip。

确定性从哪来
------------
1. 成员时间固定为 `(1980, 1, 1, 0, 0, 0)` —— zip DOS 时间的最小合法值，
   也是 SOURCE_DATE_EPOCH 惯例上的默认值。
2. `ZipInfo.create_system = 0`（MS-DOS）—— 不写 Unix 扩展字段，避免把构建机
   的 uid/gid/权限细节带进归档。
3. 成员顺序 = `git archive` 的产出顺序（本身确定）。
4. 压缩级别固定为 9。

用法
----
    python3 tools/make_src_zip.py <仓库根> <输出.zip> [顶层前缀/]

`<顶层前缀/>` 就是 zip 内的顶层目录名，约定 `<appname>-<version>/`。
省略时用仓库目录名 + `/`。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

# zip 的 DOS 时间最小合法值（1980-01-01 00:00:00）。
# 选它是因为"小到不可能与真实源码时间混淆"，且是 SOURCE_DATE_EPOCH 的惯例默认。
FIXED_DOS_TIME = (1980, 1, 1, 0, 0, 0)
COMPRESS_LEVEL = 9
# 目标运行时是 3.12；这里显式声明最低要求，免得被更老的解释器跑出不同的字节。
MIN_PY = (3, 8)


def build(repo: Path, out_zip: Path, prefix: str) -> tuple[int, int]:
    """把 git HEAD 的 tar 归档重打包成确定性 zip。返回 (文件数, 目录数)。"""
    tar_bytes = subprocess.run(
        ["git", "archive", "--format=tar", f"--prefix={prefix}", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout

    n_files = n_dirs = 0
    tmp = out_zip.with_name(out_zip.name + ".tmp")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf, \
            zipfile.ZipFile(tmp, "w") as zf:
        for m in tf.getmembers():
            name = m.name
            is_dir = m.isdir()

            if is_dir:
                # ⚠ tarfile **会去掉目录名的尾斜杠**，而 zip 的约定是
                #   「以 / 结尾 = 目录」。不补回去，`app/` 会变成一个 0 字节的
                #   **文件**，解包时炸出一串 "exists but is not directory"。
                if not name.endswith("/"):
                    name += "/"
                data = b""
                n_dirs += 1
            else:
                f = tf.extractfile(m)
                if f is None:  # 符号链接/特殊成员：本项目不该出现，直接报错
                    raise SystemExit(f"❌ tar 成员读不出内容：{name}")
                data = f.read()
                n_files += 1

            zi = zipfile.ZipInfo(name, date_time=FIXED_DOS_TIME)
            zi.create_system = 0  # 0 = MS-DOS，不写 Unix 扩展字段
            zi.external_attr = (0o40755 if is_dir else 0o100644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(
                zi, data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=COMPRESS_LEVEL,
            )

    os.replace(tmp, out_zip)  # 原子替换：别让半截文件冒充成品
    return n_files, n_dirs


def main(argv: list[str]) -> int:
    if sys.version_info[:2] < MIN_PY:
        print(f"❌ 需要 Python >= {'.'.join(map(str, MIN_PY))}", file=sys.stderr)
        return 2
    if len(argv) < 3:
        print(__doc__.strip().split("用法")[-1], file=sys.stderr)
        return 2

    repo = Path(argv[1]).resolve()
    out_zip = Path(argv[2]).resolve()
    prefix = argv[3] if len(argv) > 3 else repo.name + "/"
    if not prefix.endswith("/"):
        prefix += "/"

    if not (repo / ".git").exists():
        print(f"❌ {repo} 不是 git 仓库（需要 git archive）", file=sys.stderr)
        return 2

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    n_files, n_dirs = build(repo, out_zip, prefix)
    print(f"▸ src.zip : {out_zip.name}  {out_zip.stat().st_size} 字节，"
          f"{n_files} 文件 / {n_dirs} 目录，顶层 {prefix}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
