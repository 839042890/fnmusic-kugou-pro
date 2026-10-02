#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 ai2ku (https://github.com/ai2ku/)
# Copyright (C) 2026 星痕在此 (fnmusic_kugou_pro fork, 独立维护)
set -euo pipefail

# ==============================================================================
# fnmusic-pro Proxy 启动入口 (以 root 权限由 systemd 调用)
# 架构：Unix Socket 接管
#
# 幂等接管：
# - TARGET 是 trim-music → mv 到 UPSTREAM，再绑代理
# - UPSTREAM 已是 trim-music（systemd 重启场景）→ 只替换 TARGET 上的代理 socket
# 禁止 rm 掉仍在 accept 的 trim-music inode。
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ===== 持久化目录 =====
# BASE_DIR 是"程序目录"（/vol1/@appcenter/<app>，飞牛的 target），覆盖安装会被
# 整个替换。可变状态一律放飞牛的持久目录，否则升级一次就归零：
#   etc  → /vol1/@appconf/<app>   配置（.env、登录凭证）
#   var  → /vol1/@appdata/<app>   运行数据（封面缓存、去重索引）
#   home → /vol1/@apphome/<app>   用户数据（在线收藏）
# 坑1：绝不能用 "${BASE_DIR}/../etc" 去推。BASE_DIR 是真实路径
#      /vol1/@appcenter/<app>，它的 ../etc 是 /vol1/@appcenter/etc（不存在）。
#      必须走 /var/apps/<app>/etc 这个飞牛建的符号链接。
# 坑2：优先级 环境变量 > 已知的 /var/apps 链接 > 安装目录内旧位置。最后一档
#      只为在非飞牛环境（本地调试）下不崩，正常装机不会走到。
# 坑3：TRIM_PKGETC 等是"调用方应用"的上下文。若被别的应用带进来的环境启动
#      （或变量串了），照用就会把 .env（含登录 token）写进别的应用目录。
#      所以只在路径里出现本应用名时才认。
APP_NAME="fnmusic_kugou_pro"
PKG_DIR="/var/apps/${APP_NAME}"
pick_dir() {
    local _var="$1" _sub="$2" _v
    _v="${!_var:-}"
    if [ -n "$_v" ]; then
        # 按"整段路径"匹配：注入值可能是 /vol1/@appconf/<app>（末段是应用
        # 名），也可能是 /var/apps/<app>/etc（应用名在中间）。
        case "/$_v/" in
            */"$APP_NAME"/*) printf '%s' "$_v"; return ;;
            *) echo "[run_proxy] 忽略 ${_var}=${_v}（不属于 ${APP_NAME}）" ;;
        esac
    fi
    [ -d "${PKG_DIR}/${_sub}" ] && { printf '%s' "${PKG_DIR}/${_sub}"; return; }
    printf '%s' "${BASE_DIR}/${_sub}"
}
ETC_DIR="$(pick_dir TRIM_PKGETC etc)"
VAR_DIR="$(pick_dir TRIM_PKGVAR var)"
PKG_HOME_DIR="$(pick_dir TRIM_PKGHOME home)"
mkdir -p "$ETC_DIR" "$VAR_DIR" "$PKG_HOME_DIR" 2>/dev/null || true
export FNMUSIC_ETC_DIR="$ETC_DIR"
export FNMUSIC_VAR_DIR="$VAR_DIR"
export FNMUSIC_PKG_HOME_DIR="$PKG_HOME_DIR"
# 日志落在安装目录自带的 log/ 下（程序以 root 运行，可直接写）。
# 写不进安装目录时退回 /tmp，避免启动脚本直接死掉。
LOG_FILE="${FNMUSIC_RUN_LOG:-${BASE_DIR}/log/proxy.log}"
if ! mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null; then
    LOG_FILE="/tmp/fnmusic-pro-run.log"
fi
# 超过 10MB 滚动一次，避免安装目录被日志撑爆。
if [ -f "$LOG_FILE" ] && [ "$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 10485760 ]; then
    mv -f "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null || true
fi

TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
# 封面/封面ID 缓存也属运行数据 → 落持久 var 目录，别放安装目录（升级即清、
# 白白重下一遍封面）。
CACHE_DIR="${VAR_DIR}/cache"

probe_sock() {
    local sock="$1"
    if [ ! -S "${sock}" ]; then
        echo "none"
        return
    fi
    local health
    health="$(curl -s --max-time 2 --unix-socket "${sock}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${health}" | grep -q '"upstream"'; then
        echo "proxy"
        return
    fi
    local search
    search="$(curl -s --max-time 2 --unix-socket "${sock}" "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
    if echo "${search}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        echo "trim"
        return
    fi
    echo "stale"
}

echo "[run_proxy] 等待 trim-music 在 ${TARGET_SOCK} 或 ${UPSTREAM_SOCK} 上就绪..."
exec >> "$LOG_FILE" 2>&1
echo "===== run_proxy 启动 @ $(date '+%Y-%m-%d %H:%M:%S') ====="
echo "BASE_DIR=${BASE_DIR}"
FOUND_TRIM=0
for i in $(seq 1 60); do
    t_probe="$(probe_sock "${TARGET_SOCK}")"
    u_probe="$(probe_sock "${UPSTREAM_SOCK}")"
    if [ "${t_probe}" = "trim" ] || [ "${u_probe}" = "trim" ]; then
        FOUND_TRIM=1
        break
    fi
    sleep 1
done

if [ "${FOUND_TRIM}" -ne 1 ]; then
    echo "[run_proxy] 错误: 未探测到存活的 trim-music socket（TARGET=${t_probe} UPSTREAM=${u_probe}）"
    echo "[run_proxy] 请在应用商店确认【音乐】应用处于【运行中】状态"
    exit 1
fi

t_probe="$(probe_sock "${TARGET_SOCK}")"
u_probe="$(probe_sock "${UPSTREAM_SOCK}")"
echo "[run_proxy] probe TARGET=${t_probe} UPSTREAM=${u_probe}"

if [ "${t_probe}" != "trim" ] && [ "${u_probe}" != "trim" ]; then
    echo "[run_proxy] 错误: 无 trim-music 可接管" >&2
    exit 1
fi

# 注意：socket 接管（mv TARGET -> UPSTREAM）**不在这里做**，见文件末尾。
# 2.1.3 事故：接管在前、uvicorn 在后，代理一旦启动失败（当时是空值 env 导致
# 导入期 ValueError），TARGET 已被 mv 走却没有新服务接手 —— 于是整个【音乐】
# 不可用，且不重启【音乐】无法恢复。改为「先预检、再接管」，预检失败时
# 一个字节都不动音乐端。

mkdir -p "${CACHE_DIR}"

export FNMUSIC_HOME="${BASE_DIR}"
# 注意：这里**不**导出 FNMUSIC_HOME_DIR。该变量在 app.py 里表示"安装目录内的
# app/home"（老版本的收藏落点，只当兜底用）。把它指到持久目录会造成同一变量
# 两处含义，容易在看日志时判断错位置。用户数据目录另有 FNMUSIC_PKG_HOME_DIR。
export FNMUSIC_UPSTREAM_SOCK="${UPSTREAM_SOCK}"
export FNMUSIC_CACHE_DIR="${CACHE_DIR}"
export PYTHONUNBUFFERED=1

# 加载 .env：逐行解析 KEY=VALUE 后显式 export：仅接受合法变量名，
# 值必须是单行且不含控制字符，其余行一律忽略。
# 位置：优先配置目录；那里还没有而安装目录有一份**非空**的旧文件时，
# 继续用旧位置（app.py 启动后会自动迁移，见 _migrate_env_to_conf_dir）。
# 空模板文件不算 —— 包内自带一份 8 个全空的 FNMUSIC_KUGOU_*=。
ENV_FILE="${ETC_DIR}/.env"
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "${BASE_DIR}/.env" ] && grep -qvE '^[A-Za-z_][A-Za-z0-9_]*=$' "${BASE_DIR}/.env" 2>/dev/null; then
        ENV_FILE="${BASE_DIR}/.env"
        echo "[run_proxy] 注意: .env 仍在安装目录，将在启动后迁移到 ${ETC_DIR}"
    fi
fi
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r _line || [ -n "${_line}" ]; do
        # 去空白与行尾 CR（Windows 传过来的文件）
        _line="${_line%$'\r'}"
        case "${_line}" in
            ''|\#*) continue ;;
        esac
        case "${_line}" in
            *=*) ;;
            *) continue ;;
        esac
        _key="${_line%%=*}"
        _val="${_line#*=}"
        # key 必须是合法 shell 变量名，否则跳过（挡掉 `x;cmd` 之类）
        case "${_key}" in
            *[!A-Za-z0-9_]*) continue ;;
            [0-9]*) continue ;;
        esac
        # 值里若含换行/回车不可能（read 按行切分）；这里再挡控制字符
        case "${_val}" in
            *[[:cntrl:]]*) continue ;;
        esac
        export "${_key}=${_val}"
    done < "$ENV_FILE"
    echo "[run_proxy] .env 已按 KEY=VALUE 逐行加载（未 source）: ${ENV_FILE}"
fi

UVICORN="${BASE_DIR}/.venv-proxy/bin/uvicorn"
if [ ! -x "${UVICORN}" ]; then
    echo "[run_proxy] 错误: 未找到 uvicorn: ${UVICORN}"
    exit 1
fi

# ===== 预检：先在"不碰音乐端"的前提下把 app 导入一遍 =====
# 用与正式启动完全相同的 python 与工作目录，只做 import（不监听 socket）。
# 目的是把"配置写错导致模块导入期抛异常"这类致命故障挡在接管之前：
# 预检失败 = 音乐端毫发无损，只是扩展没起来。
# 注意要清掉 PYTHONPATH：宿主注入的 PYTHONPATH 会盖过 venv 里的包，
# 报出 pydantic_core._pydantic_core 之类的假故障。
PYBIN="${BASE_DIR}/.venv-proxy/bin/python"
if [ -x "${PYBIN}" ]; then
    if ! (cd "${BASE_DIR}/proxy" && env -u PYTHONPATH "${PYBIN}" -c 'import app') >/dev/null 2>&1; then
        echo "[run_proxy] 预检失败：app 模块导入报错，放弃接管（音乐端未受影响）。详情："
        (cd "${BASE_DIR}/proxy" && env -u PYTHONPATH "${PYBIN}" -c 'import app') 2>&1 | tail -25 | sed 's/^/[run_proxy]   /'
        exit 1
    fi
    echo "[run_proxy] 预检通过（app 可导入）"
else
    echo "[run_proxy] 警告: 未找到 ${PYBIN}，跳过预检"
fi

# ===== 预检通过后才接管 socket =====
t_probe="$(probe_sock "${TARGET_SOCK}")"
u_probe="$(probe_sock "${UPSTREAM_SOCK}")"
if [ "${t_probe}" = "trim" ]; then
    if [ "${u_probe}" != "none" ] && [ "${u_probe}" != "trim" ]; then
        echo "[run_proxy] 清理非 trim 的 upstream 残留 (${u_probe})..."
        rm -f "${UPSTREAM_SOCK}"
    fi
    if [ "${u_probe}" = "trim" ]; then
        echo "[run_proxy] TARGET 与 UPSTREAM 都是 trim-music，保留 UPSTREAM，移除 TARGET 后接管"
        rm -f "${TARGET_SOCK}"
    else
        echo "[run_proxy] 执行 socket 接管: ${TARGET_SOCK} -> ${UPSTREAM_SOCK}"
        mv "${TARGET_SOCK}" "${UPSTREAM_SOCK}"
    fi
elif [ "${u_probe}" = "trim" ]; then
    echo "[run_proxy] UPSTREAM 已是 trim-music，仅替换代理 socket"
    if [ "${t_probe}" != "none" ]; then
        rm -f "${TARGET_SOCK}"
    fi
else
    echo "[run_proxy] 错误: 无 trim-music 可接管（可能在预检期间退出）" >&2
    exit 1
fi

echo "[run_proxy] 启动 uvicorn 代理服务绑定至 ${TARGET_SOCK}..."
exec "${UVICORN}" app:app \
    --app-dir "${BASE_DIR}/proxy" \
    --uds "${TARGET_SOCK}"
