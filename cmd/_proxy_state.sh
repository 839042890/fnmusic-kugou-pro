#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 ai2ku (https://github.com/ai2ku/)
# Copyright (C) 2026 星痕在此 (fnmusic_kugou_pro fork, 独立维护)
#
# fnmusic-pro 共享状态工具（被 cmd/main、cmd/stop_callback、cmd/uninstall_init、
# cmd/uninstall_callback **source**，不单独执行）
#
# 为什么要单独一份，而不是各脚本各写一遍：
#   "状态文件放在哪"与"哪个进程算我的"这两个判断，四个入口必须**完全一致**。
#   写四份迟早漂移，而漂移的后果正好是 P-01 那类故障：
#     判活说谎 → 状态显示与实际不符；
#     stop 读不到 PID → 杀不掉 → 孤儿进程与新实例同时挂同一个 socket。
#
# 约定：
#   · 所有函数只做"读 / 判断 / 杀进程"，不 exit，也不依赖调用方的 set -e；
#   · app_dir 一律传**绝对路径**（调用方用 readlink -f 归一，避免符号链接
#     导致 cmdline 比对失配）；
#   · 任何"新建的落盘文件"都由调用方负责 chmod（本平台新建文件权限 = 父目录权限）。

FNMUSIC_APP_NAME="fnmusic_kugou_pro"
FNMUSIC_PKG_META_DIR="/var/apps/${FNMUSIC_APP_NAME}"
# 飞牛音乐本体的 socket（我们接管它）。允许覆盖：测试与特殊部署需要换一个
# 不存在的路径，避免把"本机真实存在的代理"当成自己。
FNMUSIC_TARGET_SOCK="${FNMUSIC_TARGET_SOCK:-/var/run/trim_music.socket}"

# 状态文件名（PID / 主日志 / 运行日志 / 安装目录记录）
FNMUSIC_STATE_PID_NAME="fnmusic-pro.pid"
FNMUSIC_STATE_LOG_NAME="fnmusic-pro-main.log"
FNMUSIC_STATE_RUNLOG_NAME="fnmusic-pro-run.log"
FNMUSIC_STATE_APPDEST_NAME="fnmusic-pro-appdest"

# TRIM_* 是"调用方应用"的上下文。串了的话不能认 —— 否则状态文件（以及 .env）
# 会落到别的应用目录里去。按**整段路径**匹配：注入值可能是
# /vol1/@appdata/<app>（末段是应用名），也可能是 /var/apps/<app>/var
# （应用名在中间），只比末段会误拒后者。
fnmusic_owns_path() {
    case "/${1%/}/" in
        */"${FNMUSIC_APP_NAME}"/*) return 0 ;;
        *) return 1 ;;
    esac
}

# 状态目录：**必须是持久目录**。
# 以前 PID/日志放在 /tmp（重启清空、运行期也可能被清理脚本删掉）。落在那里
# 的后果不是"少个文件"，而是：应用中心读不到 PID 就判「已关闭」（进程其实还在
# 跑），随后 stop 同样读不到 PID → 杀不掉 → 孤儿进程一直占着 socket。
fnmusic_state_dir() {
    local v="${TRIM_PKGVAR:-}"
    if [ -n "$v" ] && fnmusic_owns_path "$v"; then
        printf '%s' "${v%/}"
        return 0
    fi
    if [ -d "${FNMUSIC_PKG_META_DIR}/var" ]; then
        printf '%s' "${FNMUSIC_PKG_META_DIR}/var"
        return 0
    fi
    # 仅非飞牛环境（本地调试）：没有持久目录可用，退回 /tmp。
    printf '%s' "/tmp"
}

# 解析安装目录（含 proxy/run_proxy.sh 的那一层），成功时回显绝对路径。
#   优先级：TRIM_APPDEST > 状态目录里的记录 > /tmp 旧记录 > /var/apps/<app>/target
# 为什么要这么多档：stop_callback / uninstall_init 可能既拿不到 TRIM_APPDEST，
# 也没有历史记录（1.0.0 从来没写过 appdest 文件），而"按 cmdline 归属判定"
# 必须先知道安装目录。漏了兜底就等于"扫描功能白写"。
fnmusic_app_dir() {
    local state_dir="${1:-}" cand rec_state rec_legacy
    rec_state=""
    [ -n "$state_dir" ] && rec_state="$(cat "${state_dir}/${FNMUSIC_STATE_APPDEST_NAME}" 2>/dev/null || true)"
    rec_legacy="$(cat "/tmp/${FNMUSIC_STATE_APPDEST_NAME}" 2>/dev/null || true)"
    for cand in "${TRIM_APPDEST:-}" "$rec_state" "$rec_legacy" \
                "/var/apps/${FNMUSIC_APP_NAME}/target"; do
        [ -n "$cand" ] || continue
        if [ -x "${cand}/app/proxy/run_proxy.sh" ]; then
            printf '%s' "${cand}/app"
            return 0
        fi
        if [ -x "${cand}/proxy/run_proxy.sh" ]; then
            printf '%s' "$cand"
            return 0
        fi
    done
    return 1
}

# 只读 PID 文件内容（纯数字），不做任何存活判断
fnmusic_pid_file_read() {
    local f="${1:-}" pid
    [ -n "$f" ] && [ -f "$f" ] || return 1
    pid="$(tr -dc '0-9' < "$f" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    printf '%s' "$pid"
}

# /proc/<pid>/cmdline 看起来是不是"本应用安装目录下的代理"。
#
# 为什么不能只 kill -0：
#   PID 会被复用。PID 文件陈旧时，那个号可能已经属于**别的程序**，
#   kill -0 照样成功 —— 于是我们既误报"在运行"，后续 stop 还会杀掉无关进程。
#
# 为什么还要看 cwd：
#   run_proxy.sh 启动瞬间（exec 之前）的 cmdline 是 `bash ./proxy/run_proxy.sh`，
#   里面没有绝对路径。但它的工作目录就是安装目录，用 /proc/<pid>/cwd 指认，
#   既能认出来，又不会误伤另一个音源扩展的同名脚本。
fnmusic_pid_is_ours() {
    local pid="${1:-}" app_dir="${2:-}" cmd cwd app_real
    [ -n "$pid" ] && [ -n "$app_dir" ] || return 1
    [ -r "/proc/${pid}/cmdline" ] || return 1
    cmd="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
    [ -n "$cmd" ] || return 1
    app_real="$(readlink -f "$app_dir" 2>/dev/null || printf '%s' "$app_dir")"
    case "$cmd" in
        *"--app-dir ${app_real}/proxy"*|*"--app-dir ${app_dir}/proxy"*) return 0 ;;
        *"proxy/run_proxy.sh"*) ;;
        *) return 1 ;;
    esac
    cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    [ -n "$cwd" ] && [ "$cwd" = "$app_real" ] && return 0
    return 1
}

# 扫描 /proc 找本应用的进程。
#   $1 = app_dir
#   $2 = cmdline 里必须出现的子串（如 proxy/ 或 app_socket_bridge.py）
#   $3 = 可选的"必须排除"的子串（用来把网关桥接器从代理进程里剔除）
fnmusic_scan_pids() {
    local app_dir="${1:-}" needle="${2:-}" exclude="${3:-}"
    local app_real d pid cmd cwd
    [ -n "$app_dir" ] && [ -n "$needle" ] || return 1
    app_real="$(readlink -f "$app_dir" 2>/dev/null || printf '%s' "$app_dir")"
    for d in /proc/[0-9]*; do
        pid="${d#/proc/}"
        [ "$pid" = "$$" ] && continue
        [ -r "${d}/cmdline" ] || continue
        cmd="$(tr '\0' ' ' < "${d}/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            *"$needle"*) ;;
            *) continue ;;
        esac
        if [ -n "$exclude" ]; then
            case "$cmd" in
                *"$exclude"*) continue ;;
            esac
        fi
        # 归属确认：cmdline 带绝对安装目录，或工作目录就是安装目录
        case "$cmd" in
            *"${app_real}/"*) printf '%s\n' "$pid"; continue ;;
        esac
        cwd="$(readlink -f "${d}/cwd" 2>/dev/null || true)"
        [ -n "$cwd" ] && [ "$cwd" = "$app_real" ] && printf '%s\n' "$pid"
    done
    return 0
}

# 本应用的代理进程（uvicorn / run_proxy.sh），排除网关桥接器。
# needle 用 "/proxy" 而不是 "proxy/"：uvicorn 的 cmdline 是
# `--app-dir <APP>/proxy --uds ...`，`proxy` 后面是空格，没有斜杠。
# （这里曾写错一次，症状是"扫盘永远扫不到"，而测试把它抓了出来。）
fnmusic_find_pids() {
    fnmusic_scan_pids "${1:-}" "/proxy" "app_socket_bridge.py"
}

# 本应用的网关桥接器（单独跑在 APP_DIR 下）
fnmusic_find_bridge_pids() {
    fnmusic_scan_pids "${1:-}" "app_socket_bridge.py" ""
}

# 读 PID 文件并校验（存活 + 确实是我们），成功时回显 PID
fnmusic_read_pid() {
    local pid_file="${1:-}" app_dir="${2:-}" pid
    pid="$(fnmusic_pid_file_read "$pid_file")" || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    fnmusic_pid_is_ours "$pid" "$app_dir" || return 1
    printf '%s' "$pid"
}

# TARGET 上的服务是不是**我们的**代理（最后一道兜底）。
# 判据：healthz 同时含 "app":"fnmusic_kugou_pro" / "upstream" / "kugou"。
# 必须带 app 字段 —— 否则同族的另一个音源扩展也会命中，等于认错人。
fnmusic_proxy_at_target() {
    local body
    body="$(curl -s --max-time 6 --unix-socket "$FNMUSIC_TARGET_SOCK" \
                 http://localhost/_ext/healthz 2>/dev/null || true)"
    [ -n "$body" ] || return 1
    case "$body" in
        *"\"app\": \"${FNMUSIC_APP_NAME}\""*|*"\"app\":\"${FNMUSIC_APP_NAME}\""*) ;;
        *) return 1 ;;
    esac
    case "$body" in
        *'"upstream"'*) ;;
        *) return 1 ;;
    esac
    return 0
}

# 判活：① PID 文件（带归属校验）→ ② /proc 按 cmdline 找回并回写 → ③ healthz 指纹
# 命中时设置 FNMUSIC_LAST_PID / FNMUSIC_LAST_SOURCE 供调用方打日志。
fnmusic_is_running() {
    local app_dir="${1:-}" pid_file="${2:-}" pid
    [ -n "$app_dir" ] || return 1
    if [ -n "$pid_file" ]; then
        if pid="$(fnmusic_read_pid "$pid_file" "$app_dir")"; then
            FNMUSIC_LAST_PID="$pid"
            FNMUSIC_LAST_SOURCE="pidfile"
            return 0
        fi
    fi
    if pid="$(fnmusic_find_pids "$app_dir" | head -1)" && [ -n "$pid" ]; then
        FNMUSIC_LAST_PID="$pid"
        FNMUSIC_LAST_SOURCE="cmdline"
        # 自愈：把丢掉的 PID 文件补回去（失败也不影响判活结果）
        if [ -n "$pid_file" ]; then
            printf '%s\n' "$pid" > "$pid_file" 2>/dev/null || true
        fi
        return 0
    fi
    if fnmusic_proxy_at_target; then
        FNMUSIC_LAST_PID=""
        FNMUSIC_LAST_SOURCE="healthz"
        return 0
    fi
    return 1
}

# 停掉本应用的全部进程（代理 + 网关桥接器）。
#   用法：fnmusic_kill_all <app_dir> [pid_file ...]
#   **PID 文件失效也要能杀干净** —— 否则旧进程变孤儿，和新实例抢同一个 socket。
#   只杀"确认属于本应用"的进程：另一个音源扩展的进程不能碰。
fnmusic_kill_all() {
    local app_dir="${1:-}" pid_file pid pids=""
    shift || true
    [ -n "$app_dir" ] || return 0

    for pid_file in "$@"; do
        pid="$(fnmusic_read_pid "$pid_file" "$app_dir" || true)"
        [ -n "$pid" ] || continue
        case " $pids " in
            *" $pid "*) ;;
            *) pids="$pids $pid" ;;
        esac
    done
    while read -r pid; do
        [ -n "$pid" ] || continue
        case " $pids " in
            *" $pid "*) ;;
            *) pids="$pids $pid" ;;
        esac
    done <<EOF
$(fnmusic_find_pids "$app_dir" || true)
EOF
    while read -r pid; do
        [ -n "$pid" ] || continue
        case " $pids " in
            *" $pid "*) ;;
            *) pids="$pids $pid" ;;
        esac
    done <<EOF
$(fnmusic_find_bridge_pids "$app_dir" || true)
EOF

    pids="${pids# }"
    if [ -n "$pids" ]; then
        for pid in $pids; do
            kill "$pid" 2>/dev/null || true
        done
        sleep 2
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        # 再确认一次：还活着的记一笔（调用方有 log() 的话会看到）
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                printf 'fnmusic_kill_all: PID %s 仍然存活\n' "$pid" >&2
            fi
        done
    fi

    for pid_file in "$@"; do
        rm -f "$pid_file" 2>/dev/null || true
    done
    # 旧位置（/tmp）的文件由调用方显式传进来删 —— 这里**不**越权去碰 /tmp：
    # 本函数在测试与"另一个应用"的场景里也会被调用，顺手删 /tmp 会波及
    # 真实安装的那一份状态文件。
    return 0
}
