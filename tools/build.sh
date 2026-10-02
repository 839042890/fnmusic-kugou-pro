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
#      实测旧包外层与内层都带过构建机的真实用户名/组名与 uid/gid
#      （形如 ('<用户名>','<组名>',<uid>,<gid>)）。
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
#
# ③ **权限位**（最容易漏的一格）：tar 会把每个成员的 mode 原样写进归档，
#    而 mode 来自磁盘、磁盘 mode 来自构建时的 **umask**。
#    ⇒ 同一份源码，umask 077 与 umask 022 打出的包 **MD5 不同**。
#    这不是"哈希不好看"而已，有两个实际后果：
#      · 包里 manifest / LICENSE / cmd/* / wizard/* 会带上构建机的 umask 痕迹
#        （实测本机是 700），任何人 `tar tvf` 就能看到；
#      · 若构建机 umask 是 077，`cmd/*` 会变成 700 —— 上游发布的是 755，
#        而 `cmd/main` 里有硬检查 `[ ! -x "$APP_DIR/proxy/run_proxy.sh" ]`，
#        非 root 场景会直接判成"找不到 run_proxy.sh"。
#        **在这里"权限收紧"是隐性故障，不是更安全。**
#    所以打包前先把载荷复制进**暂存目录**、把权限归一成确定值：
#      目录 755 / 文件 644 / 仅 `cmd/*` 与 `app/proxy/run_proxy.sh` 置 755。
#
#    ❌ 别想着用 `tar --mode` 省掉暂存目录，两个坑都实测过：
#       ① 它是**单一** flag，无法对同一归档里的不同文件用不同 mode
#          （数据要 644、脚本要 755）；
#       ② `+X` 是**粘性**语义（仅当已有某位可执行时才补 x），源文件是 600 时
#          **不会**提权 ⇒ `run_proxy.sh` 丢掉可执行位 ⇒ 正好踩上面那个硬检查。
STAGE_ROOT="$(mktemp -d)"
trap 'rm -rf "$STAGE_ROOT"' EXIT

# 把 <源目录> 的内容归一化权限后复制到 <目标目录>
stage() {
    local src="$1" dst="$2"
    mkdir -p "$dst"
    cp -a "$src/." "$dst/"
    find "$dst" -type d -exec chmod 755 {} +
    find "$dst" -type f -exec chmod 644 {} +
}

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

# 归一化权限后再打（见上面 ③ 的说明）。`run_proxy.sh` 是唯一需要可执行位的载荷文件
# —— `cmd/main:26` 有 `[ ! -x "$APP_DIR/proxy/run_proxy.sh" ]` 的硬检查。
STAGE_APP="$STAGE_ROOT/app"
stage app "$STAGE_APP"
chmod 755 "$STAGE_APP/proxy/run_proxy.sh"
tar czf app.tgz "${TARFIX[@]}" -C "$STAGE_APP" .

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
#
# 同样先过暂存层归一化权限：`cmd/*` 是飞牛的入口脚本，必须 755
# （这是上游的发布形态，也是唯一不会因构建机 umask 而漂移的形态）。
STAGE_FPK="$STAGE_ROOT/fpk"
mkdir -p "$STAGE_FPK"
cp -a manifest app.tgz cmd config wizard ICON.PNG ICON_256.PNG LICENSE "$STAGE_FPK"/
find "$STAGE_FPK" -type d -exec chmod 755 {} +
find "$STAGE_FPK" -type f -exec chmod 644 {} +
chmod 755 "$STAGE_FPK"/cmd/*
mkdir -p out
FPK="out/fnmusic_kugou_pro-${VERSION}.fpk"
tar czf "$FPK" "${TARFIX[@]}" -C "$STAGE_FPK" \
    manifest app.tgz cmd config wizard ICON.PNG ICON_256.PNG LICENSE

# 顶层不许出现多出来的目录树（飞牛按平铺结构解包）
EXTRA=$(tar tzf "$FPK" | awk -F/ 'NF>1 && $2=="" {print $1}' | sort -u \
        | grep -vE '^(manifest|app\.tgz|cmd|config|wizard|ICON\.PNG|ICON_256\.PNG|LICENSE)$' || true)
[[ -z "$EXTRA" ]] || { echo "❌ .fpk 顶层有多余条目：$EXTRA"; exit 1; }
echo "▸ fpk     : $FPK ($(stat -c%s "$FPK") 字节)"

# --- 4b) 权限位自查（umask 不得泄漏进包）-------------------------------------
# 这一节是 1.0.0 发布收尾时补的：当时"连跑 3 次 MD5 一致"看着没问题，
# 其实只证明了**同一台机器、同一个 umask** 下稳定；换个环境重打就不同。
# 判据来自"包必须与构建机无关"，所以把权限写成**精确等值断言**，
# 而不是"看起来还行"。
assert_modes() {
    local arch="$1" script_re="$2"
    local bad="" mode name
    while read -r mode name; do
        if [[ "$name" == */ ]]; then                       # 目录：drwxr-xr-x
            [[ "$mode" == "drwxr-xr-x" ]] || bad+="      目录非 755：$mode $name"$'\n'
        elif [[ "$name" =~ $script_re ]]; then             # 脚本：-rwxr-xr-x
            [[ "$mode" == "-rwxr-xr-x" ]] || bad+="      脚本非 755：$mode $name"$'\n'
        else                                               # 其余：-rw-r--r--
            [[ "$mode" == "-rw-r--r--" ]] || bad+="      文件非 644：$mode $name"$'\n'
        fi
    done < <(tar tzvf "$arch" | awk '{print $1, $NF}')
    # 补一条独立判据：包内不得出现 go+w（组/其他人可写）。
    # mode 串第 6 位 = group 的 w，第 9 位 = other 的 w。
    local w
    w=$(tar tzvf "$arch" | awk 'substr($1,6,1)=="w" || substr($1,9,1)=="w" {print "      " $1, $NF}')
    [[ -z "$w" ]] || bad+="      存在 go+w（组/其他人可写）："$'\n'"$w"$'\n'
    if [[ -n "$bad" ]]; then
        echo "❌ $arch 权限位自查未通过（umask 可能泄漏）：" >&2
        printf '%s' "$bad" >&2
        exit 1
    fi
    echo "   $arch 权限位自查通过（目录 755 / 文件 644 / 脚本 755，无 go+w）"
}
assert_modes app.tgz '\./proxy/run_proxy\.sh$'
assert_modes "$FPK" '^cmd/'
echo "▸ fpk     : 权限已归一"

# --- 5) 源码包 src.zip（跨时区可复现）--------------------------------------
# 为什么不用 `git archive --format=zip`：它把 commit 时间**转成"本地时区"**的
# DOS 时间写进 zip 头，于是同一个 commit 在不同 TZ 下打出的 zip 哈希不同
# （实测四时区四个值）。差异 280 字节、全在 zip 头的时间字段，
# 但足以让我们公布的哈希**在异地无法复现** —— 别人会误以为包被改过。
#
# tar 格式不受影响（写 unix epoch），所以用 tar 当中间格式，
# 再由 tools/make_src_zip.py 确定性地重打包为 zip。
#
# ⚠ src.zip 必须**最后**生成：它归档的是 git HEAD 的**已提交**内容，
#   而本脚本会改写 manifest.checksum / app.tgz。所以发布流程是
#   ① 跑 build.sh  → ② git add -A && git commit  → ③ 再跑一次 build.sh
#   让 src.zip 覆盖成"包含最新 commit"的那份（.fpk 的哈希不受影响）。
SRCZIP="out/fnmusic_kugou_pro-${VERSION}-src.zip"
if [[ -d .git ]]; then
    env -u PYTHONPATH "$PY312" tools/make_src_zip.py . "$SRCZIP" \
        "${APP_NAME}-${VERSION}/"
    # 硬判据：同一份源码**重新生成**，换 TZ 必须同哈希。
    #
    # ⚠ 这里有个极易写错的陷阱，差点让我白测一场：
    #   对**同一个已存在的文件**跑 `TZ=X md5sum f` 是毫无意义的 ——
    #   md5sum 不读时区，三次必然相同，断言恒真（测试永远绿，什么也没验）。
    #   必须**在各自 TZ 下重新生成一次**，再比哈希。
    #   工具不同（bash 的 TZ 只影响子进程）也要留意：每个生成都在
    #   `TZ=... python3 ...` 里跑，才是真的换环境。
    _tz_zip_md5() {   # $1=TZ，$2=输出路径；回显该 TZ 下新生成 zip 的 md5
        env -u PYTHONPATH TZ="$1" "$PY312" tools/make_src_zip.py . "$2" \
            "${APP_NAME}-${VERSION}/" >/dev/null || return 1
        md5sum "$2" | cut -d' ' -f1
    }
    _t="$(mktemp -d)"
    _h1="$(_tz_zip_md5 UTC            "$_t/utc.zip")"       || { echo "❌ TZ=UTC 下生成失败"; exit 1; }
    _h2="$(_tz_zip_md5 Asia/Shanghai  "$_t/sha.zip")"       || { echo "❌ TZ=Asia/Shanghai 下生成失败"; exit 1; }
    _h3="$(_tz_zip_md5 America/New_York "$_t/ny.zip")"      || { echo "❌ TZ=America/New_York 下生成失败"; exit 1; }
    _h4="$(_tz_zip_md5 Pacific/Kiritimati "$_t/kir.zip")"   || { echo "❌ TZ=Pacific/Kiritimati 下生成失败"; exit 1; }
    # 与"实际要交付的那份"也必须一致，否则说明交付物与已验证物不是同一个东西
    _h5="$(md5sum "$SRCZIP" | cut -d' ' -f1)"
    rm -rf "$_t"
    if [[ "$_h1" != "$_h2" || "$_h2" != "$_h3" || "$_h3" != "$_h4" ]]; then
        echo "❌ src.zip 跨时区不可复现：" >&2
        printf '     UTC=%s\n     SH=%s\n     NY=%s\n     KIR=%s\n' "$_h1" "$_h2" "$_h3" "$_h4" >&2
        exit 1
    fi
    [[ "$_h5" == "$_h1" ]] || { echo "❌ 交付的 src.zip 与跨 TZ 验证结果不一致"; exit 1; }
    echo "▸ src.zip : $SRCZIP ($(stat -c%s "$SRCZIP") 字节, md5 $_h1)"
    echo "            跨时区已验（UTC/SH/NY/Kiritimati 四时区同哈希）"
else
    echo "▸ src.zip : 跳过（无 .git，通常是从源码 zip 解包的场景）"
    SRCZIP=""
fi

# --- 6) 组装交付目录 out/<app>-<ver>/ ---------------------------------------
# 用户的原话是"我一次性拿走，填上 GitHub/Gitee 链接和下载地址就发布"。
# 所以这里直接组装出**一个可以整体复制的目录**，而不是把文件散在工作区各处
# —— 散着放就会出现"你以为复制全了、其实漏了 checksums.txt"这类事故。
DIST="out/${APP_NAME}-${VERSION}"
rm -rf "$DIST"
mkdir -p "$DIST"
cp "$FPK" "$DIST"/
[[ -n "$SRCZIP" && -f "$SRCZIP" ]] && cp "$SRCZIP" "$DIST"/
for f in README.md CHANGELOG.md FAQ.md SECURITY.md; do
    [[ -f "$f" ]] && cp "$f" "$DIST"/
done

# 校验清单：在交付目录里、按**相对该目录**的文件名生成，
# 这样用户 `cd` 进去直接 `sha256sum -c` 就能用，不会因为路径前缀对不上而报错。
(
    cd "$DIST"
    PUBLISH=("$(basename "$FPK")")
    [[ -n "$SRCZIP" && -f "$(basename "$SRCZIP")" ]] && PUBLISH+=("$(basename "$SRCZIP")")
    for f in README.md CHANGELOG.md FAQ.md SECURITY.md; do
        [[ -f "$f" ]] && PUBLISH+=("$f")
    done

    # 标准机器校验格式：只含**一种**算法长度的行。
    # 为什么不把两种算法写进同一个文件再 `-c`：
    #   `sha256sum -c` 遇到同文件里的 md5 行（32 位十六进制）会报
    #   "improperly formatted"（退出码仍 0，但输出有噪音，容易被误读成失败）。
    #   拆开之后两个命令都是干净的 0 退出。
    : > SHA256SUMS
    : > MD5SUMS
    for f in "${PUBLISH[@]}"; do
        printf "%s  %s\n" "$(sha256sum "$f" | cut -d' ' -f1)" "$f" >> SHA256SUMS
        printf "%s  %s\n" "$(md5sum "$f" | cut -d' ' -f1)" "$f" >> MD5SUMS
    done

    {
        echo "# fnmusic_kugou_pro ${VERSION} 发布校验和"
        echo "# 生成：tools/build.sh（可复现构建）"
        echo "# 用途：发帖 / 发 Release 时公示。"
        echo "#"
        echo "# 机器校验请用同目录的 SHA256SUMS / MD5SUMS（拆分是为了让输出干净）："
        echo "#   sha256sum -c SHA256SUMS"
        echo "#   md5sum    -c MD5SUMS"
        echo "#"
        echo "# 覆盖范围：本目录内的全部对外发布物。"
        echo "# .fpk 的权威校验值另有出处：包内 manifest.checksum = app.tgz 的 md5。"
        echo "# -src.zip 已消除时区依赖：同一 commit 在任何 TZ 下哈希相同。"
        echo
        echo "== SHA256 =="
        cat SHA256SUMS
        echo
        echo "== MD5 =="
        cat MD5SUMS
    } > checksums.txt

    # 自检：条目数对得上 + 引用的文件都在（防循环静默漏项）
    [[ "$(wc -l < SHA256SUMS)" -eq "${#PUBLISH[@]}" ]] \
        || { echo "❌ SHA256SUMS 条目数不符（$(wc -l < SHA256SUMS) != ${#PUBLISH[@]}）"; exit 1; }
    [[ "$(wc -l < MD5SUMS)" -eq "${#PUBLISH[@]}" ]] \
        || { echo "❌ MD5SUMS 条目数不符"; exit 1; }
    while read -r _sum _file; do
        [[ -z "$_sum" ]] && continue
        [[ -f "$_file" ]] || { echo "❌ 校验清单引用了不存在的文件：$_file"; exit 1; }
    done < SHA256SUMS
    # 真跑一遍校验命令，确认它们是**干净的 0 退出**（不是"看起来对"）
    sha256sum -c --status SHA256SUMS || { echo "❌ sha256sum -c SHA256SUMS 未通过"; exit 1; }
    md5sum    -c --status MD5SUMS    || { echo "❌ md5sum -c MD5SUMS 未通过"; exit 1; }
)
DIST_N="$(find "$DIST" -maxdepth 1 -type f | wc -l)"

# 权限归一：**必须显式 chmod**。
# 本平台（飞牛 /vol1，btrfs + trimacl）上，新建文件的权限**等于父目录权限**，
# umask 不参与决策 —— 父目录是 700，新文件就是 700，直接交付会让人取不走。
# 目标形态：目录 755 / 文件 644（与包内归一化规则一致）。
chmod 755 "$DIST"
find "$DIST" -maxdepth 1 -type f -exec chmod 644 {} +
echo "▸ 交付目录: $DIST（$DIST_N 个文件，权限 755/644，-c 自检通过）"

# --- 7) 包卫生自检 ---------------------------------------------------------
# 复用仓库自带的测试，避免"脚本说没问题、测试说有问题"两边打架。
env -u PYTHONPATH "$PY312" tests/test_package_hygiene.py "$FPK" | tail -20

echo
echo "✅ 打包完成：$FPK"
echo "   fpk md5    : $(md5sum "$FPK" | cut -d' ' -f1)"
[[ -n "$SRCZIP" && -f "$SRCZIP" ]] && echo "   src.zip md5: $(md5sum "$SRCZIP" | cut -d' ' -f1)"
echo "   交付目录   : $DIST"
echo
echo "下一步："
echo "  1) 发布前对**解包后的载荷**跑全套 tests/（不是工作树）"
echo "  2) 交付：把整个 $DIST/ 目录复制过去即可（含 checksums.txt）"
echo "     复制后 chmod 644，或直接 rm -f 旧版本对应文件"
