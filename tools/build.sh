#!/usr/bin/env bash
# ============================================================================
#  fnmusic_kugou_pro — 打包脚本
#
#  用法：
#      tools/build.sh              # 用 manifest 里的 version
#      tools/build.sh 1.0.0        # 强制指定版本号（会写回 manifest 的 version）
#
#  产出：
#      out/fnmusic_kugou_pro-<version>.fpk
#
#  设计原则：**每一步都自查，发现问题就在打坏包之前退出。**
#  这个脚本里几乎每条断言都对应一次真实踩过的坑，注释里写了原因，
#  请别"简化"掉。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_NAME="$(awk -F= '/^appname[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' manifest)"
[[ "$APP_NAME" == "fnmusic_kugou_pro" ]] || { echo "❌ manifest 的 appname 不是 fnmusic_kugou_pro（$APP_NAME）"; exit 1; }

if [[ $# -ge 1 ]]; then
    VERSION="$1"
    sed -i "s|^version\([[:space:]]*\)=.*|version\1= ${VERSION}|" manifest
else
    VERSION="$(awk -F= '/^version[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' manifest)"
fi
[[ -n "$VERSION" ]] || { echo "❌ 读不到 version"; exit 1; }

# --- 找 3.12 解释器 ---------------------------------------------------------
# 必须是**运行时那版**。3.11 编出来的 pyc 在 3.12 下会被直接忽略（magic 不匹配），
# 纯死重量且不报错 —— 属于最难发现的那类问题。
PY312=""
for c in /var/apps/python312/target/bin/python3.12 \
         /usr/local/bin/python3.12 \
         "$(command -v python3.12 2>/dev/null || true)"; do
    if [[ -n "$c" && -x "$c" ]] && "$c" -c 'import sys; assert sys.version_info[:2]==(3,12)' 2>/dev/null; then
        PY312="$c"; break
    fi
done
if [[ -z "$PY312" ]]; then
    echo "❌ 找不到 Python 3.12。运行时要的是 3.12，不能用 3.11 顶替。"
    echo "   已尝试：/var/apps/python312/target/bin/python3.12、/usr/local/bin/python3.12、PATH 里的 python3.12"
    exit 1
fi
echo "▸ python  : $PY312 ($("$PY312" -V 2>&1))"
echo "▸ version : $VERSION"

# --- 0) 可复现性：把 tar 元数据与文件时间钉死 ---------------------------------
# 同一份源码打两次包，MD5 必须一致 —— 否则用户拿到的哈希无法用于校验，
# 也无法判断"我下的这个包是不是官方那个"。
#
# 两个泄漏源，必须同时堵：
#   ① tar 会把构建机的 uname/gname/uid/gid 和**每个文件各自的 mtime** 写进归档。
#      实测旧包外层泄漏 ('octop-native','AppUsers',930,924)、
#      内层泄漏 ('octop-native','octop-native',930,924)。
#   ② pyc 默认是**时间戳型**（flags=0），把源文件 mtime 编进字节码 ——
#      即使 tar 修好了，源码 mtime 一变 pyc 就变，包还是不稳定。
#
# EPOCH 只是"一个固定的过去时刻"，不承载任何语义；选 2024-01-01 是图个好认。
EPOCH=1704067200
TARFIX=(--sort=name --owner=0 --group=0 --numeric-owner --mtime="@${EPOCH}")
# 说明：`--mtime` 会覆盖**所有成员**的 mtime，所以不需要预先 touch 源文件；
# `--owner=0 --group=0 --numeric-owner` 则负责不写构建机的用户名与 uid/gid。
# （早先版本额外做了一遍 `find app -exec touch`，实测在本机文件系统上
#  `touch app.tgz` 会 "Operation not permitted"，且本来就多余 —— 已删。）

# --- 1) 清干净再编 pyc ------------------------------------------------------
# compileall **只增不删**：目录里若有上次 3.11 留下的 pyc，会被一起打进包。
# 所以先 rm -rf 整个 __pycache__，而不是依赖 compileall。
rm -rf app/proxy/__pycache__ app/__pycache__

# 关键：传**相对路径**。compileall 会把 co_filename 原样记下，
# 传绝对路径 → pyc 里嵌着构建机的家目录（这类绝对路径曾经真的漏进过包里）。
#
# --invalidation-mode checked-hash：
#   默认的 timestamp 模式把**源文件 mtime** 写进 pyc 头 → 源码 mtime 不同，
#   字节码就不同，包永远不可复现。checked-hash 改成存源文件的哈希（8 字节），
#   与 mtime 无关，因此确定。
#
# 为什么是 checked-hash 而不是 unchecked-hash：
#   两者都能做到跨 mtime 稳定，区别在运行时**要不要校验**。
#   unchecked 完全不查 —— 源文件被改而 pyc 没重编时会**拿旧字节码糊弄过去**，
#   是安全隐患。checked 会校验哈希，源不匹配就重编（或报错），保护还在。
#   哈希算法是 importlib.util.source_hash()，**不是** sha256 ——
#   一开始按 sha256 写，实测对不上才纠正过来。
"$PY312" -m compileall -q --invalidation-mode checked-hash app/proxy

PYCS=$(find app/proxy/__pycache__ -name '*.pyc' | sort)
[[ -n "$PYCS" ]] || { echo "❌ 没编出 pyc"; exit 1; }
BAD=$(echo "$PYCS" | grep -v 'cpython-312\.pyc$' || true)
[[ -z "$BAD" ]] || { echo "❌ 出现非 312 的 pyc：$BAD"; exit 1; }
# co_filename 自查：编出来的字节码里不许含构建机的绝对路径。
#
# ⚠ 两个设计要点，都踩过：
#
# 1) 不用 `strings` 做检查。精简的 Linux（含飞牛本身）常常没装 binutils，
#    `strings` 不存在；配上 `set -e`，检查会**静默失效**（断言恒不触发，
#    比不写还危险）。用解释器自己读字节流最稳。
#
# 2) **不许在脚本里硬编码构建机特有的名字**（用户名、家目录、agent 目录名）。
#    否则"检查泄露"的这段代码本身就成了新的泄露点 —— 谁会想到去读一遍
#    打包脚本的字符串常量呢。这里改成**运行时算出仓库自身路径**再去比对，
#    脚本因此对所有构建环境通用。
"$PY312" - "$ROOT" <<'PY'
import marshal, pathlib, sys

repo = pathlib.Path(sys.argv[1]).resolve()
bad = []
for p in sorted(pathlib.Path("app/proxy/__pycache__").glob("*.pyc")):
    raw = p.read_bytes()
    text = raw.decode("latin-1")
    # 通用家目录前缀（跨平台），以及**本仓库自己的绝对路径**
    for probe in ("/home/", "/Users/", "/root/", str(repo)):
        if probe in text:
            bad.append(f"{p.name}: 命中构建机路径 {probe!r}")
    try:
        fn = marshal.loads(raw[16:]).co_filename
        if fn.startswith("/") or "\\" in fn:
            bad.append(f"{p.name}: co_filename 是绝对路径 {fn!r}")
    except Exception as e:  # noqa: BLE001
        bad.append(f"{p.name}: marshal 解析失败 {e}")
if bad:
    print("❌ pyc 自查未通过：", file=sys.stderr)
    for line in bad:
        print("   " + line, file=sys.stderr)
    sys.exit(1)
print("   pyc co_filename 自查通过（均为相对路径、无构建机路径）")
PY
echo "▸ pyc     : $(echo "$PYCS" | wc -l) 个，全 312，无绝对路径"

# --- 2) 打 app.tgz（绝不含 .env）--------------------------------------------
# .env 一旦进包，飞牛升级是「先解包覆盖安装目录、再跑 upgrade_callback」，
# 它会在回调有机会迁移之前就把用户的真凭证盖成空模板。
# 历史事故：升级后酷狗登录态丢失，表现为"突然搜不到歌"。
rm -f app/.env
[[ ! -e app/.env ]] || { echo "❌ app/.env 删不掉，中止"; exit 1; }
tar czf app.tgz "${TARFIX[@]}" -C app .

if tar tzf app.tgz | grep -qxE '\./\.env|\.env'; then
    echo "❌ app.tgz 里混进了 .env"; exit 1
fi
if tar tzf app.tgz | grep -q '__pycache__/.*\.pyc$'; then
    BADP=$(tar tzf app.tgz | grep '__pycache__/.*\.pyc$' | grep -v 'cpython-312')
    [[ -z "$BADP" ]] || { echo "❌ 包内有非 312 pyc：$BADP"; exit 1; }
fi
echo "▸ app.tgz : $(stat -c%s app.tgz) 字节，无 .env"

# --- 3) 回填 checksum（飞牛用 md5）------------------------------------------
CHECK="$(md5sum app.tgz | cut -d' ' -f1)"
sed -i "s|^checksum\([[:space:]]*\)=.*|checksum\1= $CHECK|" manifest
# 回来验一次，防止 sed 没匹配上（比如键名拼错）而静默沿用旧值
GOT="$(awk -F= '/^checksum[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' manifest)"
[[ "$GOT" == "$CHECK" ]] || { echo "❌ checksum 回填失败（$GOT != $CHECK）"; exit 1; }
echo "▸ checksum: $CHECK"

# --- 4) 打 .fpk -------------------------------------------------------------
# 顶层必须平铺：manifest / app.tgz / cmd / config / wizard / 图标 / LICENSE。
# tests/ 不进包（那是开发用的，不是运行时要的东西）。
mkdir -p out
FPK="out/fnmusic_kugou_pro-${VERSION}.fpk"
tar czf "$FPK" "${TARFIX[@]}" manifest app.tgz cmd config wizard \
    ICON.PNG ICON_256.PNG LICENSE

# 顶层不许出现多出来的目录树（飞牛按平铺结构解包）
EXTRA=$(tar tzf "$FPK" | awk -F/ 'NF>1 && $2=="" {print $1}' | sort -u \
        | grep -vE '^(manifest|app\.tgz|cmd|config|wizard|ICON\.PNG|ICON_256\.PNG|LICENSE)$' || true)
[[ -z "$EXTRA" ]] || { echo "❌ .fpk 顶层有多余条目：$EXTRA"; exit 1; }
echo "▸ fpk     : $FPK ($(stat -c%s "$FPK") 字节)"

# --- 5) 包卫生自检 ---------------------------------------------------------
# 复用仓库自带的测试，避免"脚本说没问题、测试说有问题"两边打架。
env -u PYTHONPATH "$PY312" tests/test_package_hygiene.py "$FPK" | tail -20

echo
echo "✅ 打包完成：$FPK"
echo "   md5: $(md5sum "$FPK" | cut -d' ' -f1)"
echo
echo "下一步（建议）："
echo "  1) 对**解包后的载荷**跑全套 tests/（不是工作树）"
echo "  2) 交付：把 $FPK 复制到你的共享目录并 chmod 644"
