#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 ai2ku (https://github.com/ai2ku/)
# Copyright (C) 2026 星痕在此 (fnmusic_kugou_pro fork, 独立维护)
#
# 本程序是自由软件：你可以按照 GNU 通用公共许可证（第 2 版或更高版本）
# 的条件重新发布和/或修改它。完整许可证见仓库根目录 LICENSE 文件。

"""
fnmusic_kugou_pro.sock 桥接器

飞牛统一网关会把 /app/fnmusic_kugou_pro 前缀的请求转发到 target/fnmusic_kugou_pro.sock。
这个桥接器只做一件事：
  1) 校验前缀
  2) 剥掉 /app/fnmusic_kugou_pro
  3) 原样转发到 /var/run/trim_music.socket
  4) 把响应原样回传
"""

from __future__ import annotations

import logging
import os
import socket
import socketserver
import sys

GATEWAY_PREFIX = "/app/fnmusic_kugou_pro"
GATEWAY_PREFIX_STRICT = "/app/fnmusic_kugou_pro/"
UPSTREAM_SOCK = os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music.socket")
LISTEN_SOCK = os.environ.get("FNMUSIC_APP_SOCK", "/var/apps/fnmusic_kugou_pro/target/fnmusic_kugou_pro.sock")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [kugou.sock] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("kugou.sock")


def _read_request_header(sock: socket.socket) -> tuple[str, dict[str, str], str, str]:
    """读取 HTTP 请求首段；body 由后续转发时按 Content-Length 读取。"""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            raise ConnectionError("client closed before header")
        data += chunk
        if len(data) > 1024 * 1024:
            raise ConnectionError("request header too large")
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    if not lines:
        raise ConnectionError("empty request")
    request_line = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return request_line, headers, request_line.split(" ")[1], rest.decode("latin1", errors="ignore")


def _forward_body_with_pre(sock: socket.socket, content_length: int, out: socket.socket, pre: bytes) -> None:
    """先把已收到的 body 前缀发出去，再补读剩下的到 content_length。"""
    sent = 0
    if pre:
        out.sendall(pre)
        sent = len(pre)
    while sent < content_length:
        chunk = sock.recv(min(65536, content_length - sent))
        if not chunk:
            raise ConnectionError("client closed during body")
        out.sendall(chunk)
        sent += len(chunk)


def _forward_body(sock: socket.socket, content_length: int, out: socket.socket) -> None:
    _forward_body_with_pre(sock, content_length, out, b"")


_RESP_HEAD_END = b"\r\n\r\n"
_RESP_HEAD_MAX = 256 * 1024


def _read_response_head(upstream: socket.socket, pre: bytes = b"") -> tuple[bytes, bytes]:
    """读到响应头结束（\\r\\n\\r\\n），返回 (含结尾的 head, 顺带多读到的 body 前缀)。

    多读出来的字节必须原样交给调用方转出去，丢了就是损坏（body 开头少一段）。
    """
    # pre 是上一轮已读进来、还没消耗的字节（1xx 要连读两个响应头）：
    # 必须先接上再找分隔符，否则会拿已读缓冲去等一段永远不会再来的数据。
    buf = pre
    while _RESP_HEAD_END not in buf:
        chunk = upstream.recv(8192)
        if not chunk:
            raise ConnectionError("upstream closed before response header")
        buf += chunk
        if len(buf) > _RESP_HEAD_MAX:
            raise ConnectionError("response header too large")
    head, _, rest = buf.partition(_RESP_HEAD_END)
    return head + _RESP_HEAD_END, rest


def _parse_response_head(head: bytes) -> tuple[int, dict[str, str]]:
    """从响应头里取 (status, headers)。header 名统一小写，值去首尾空白。"""
    lines = head.decode("latin1", errors="replace").split("\r\n")
    try:
        status = int(lines[0].split(" ")[1])
    except (IndexError, ValueError):
        status = 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return status, headers


def _pump_exact(upstream: socket.socket, out: socket.socket, total: int, pre: bytes) -> None:
    """把恰好 total 字节原样转出去：先发已读到的 pre，再补读剩下的。"""
    sent = 0
    if pre:
        out.sendall(pre)
        sent = len(pre)
    while sent < total:
        chunk = upstream.recv(min(65536, total - sent))
        if not chunk:
            raise ConnectionError("upstream closed before content-length satisfied")
        out.sendall(chunk)
        sent += len(chunk)


def _forward_response(upstream: socket.socket, out: socket.socket, method: str = "GET") -> None:
    """把上游响应转给客户端。

    **历史实现无条件 recv 到 EOF，于是被 uvicorn 的 keep-alive 拖住：**
    响应体 8 毫秒就发完了，但 uvicorn 默认 `--timeout-keep-alive 5`，发完 body 不关连接，
    桥接器一直 recv 到 EOF 才返回 ⇒ 每个经 /app/fnmusic_kugou_pro/* 的请求都白等 5 秒
    （手机端、网关都跟着等）。

    现在先解析响应头，**收满 Content-Length 就返回**，不等对端关连接：
      · 1xx（100 Continue 之类）不是终态 —— 转出去，继续读真正的响应头；
      · HEAD / 204 / 304 按 RFC 7230 §3.3.3 没有 body —— 发完头就收尾；
      · chunked / 无 Content-Length（流式响应）—— 退回原来的 EOF 逻辑，行为不变。
    """
    head, rest = _read_response_head(upstream)
    status, headers = _parse_response_head(head)
    hops = 0  # 上限防对端一直吐 1xx 把这里变成死循环
    while 100 <= status < 200 and hops < 8:
        hops += 1
        out.sendall(head)
        head, rest = _read_response_head(upstream, rest)
        status, headers = _parse_response_head(head)

    out.sendall(head)

    if method.upper() == "HEAD" or status in (204, 304):
        log.debug("<- head-only response (%d), done", status)
        return

    transfer_encoding = (headers.get("transfer-encoding") or "").lower()
    if "chunked" not in transfer_encoding:
        raw_len = headers.get("content-length")
        if raw_len is not None:
            try:
                total = int(raw_len)
            except ValueError:
                total = -1
            if total >= 0:
                _pump_exact(upstream, out, total, rest)
                log.debug("<- content-length %d satisfied, done", total)
                return

    # 无长度 / chunked：保持原行为（等到对端关连接）
    if rest:
        out.sendall(rest)
    while True:
        chunk = upstream.recv(65536)
        if not chunk:
            break
        out.sendall(chunk)


def _copy_remaining(client: socket.socket, upstream: socket.socket, rest: str) -> None:
    if rest:
        upstream.sendall(rest.encode("latin1", errors="replace"))
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        upstream.sendall(chunk)


def _rewrite_host_headers(headers: dict[str, str]) -> None:
    headers["x-forwarded-proto"] = headers.get("x-forwarded-proto", "http")
    headers["x-forwarded-for"] = headers.get("x-forwarded-for", "127.0.0.1")


def _rewrite_request_target(method: str, path: str, headers: dict[str, str], upstream: socket.socket) -> None:
    _rewrite_host_headers(headers)
    headers["host"] = "localhost"
    # 保留完整前缀，FastAPI 侧按 /app/fnmusic_kugou_pro 路由匹配。
    target_path = path if path != GATEWAY_PREFIX else GATEWAY_PREFIX_STRICT
    req_line = f"{method} {target_path} HTTP/1.1\r\n"
    for k, v in headers.items():
        if k in {"host", "content-length", "content-type", "user-agent", "x-trim-userid", "x-trim-username", "x-trim-isadmin", "x-forwarded-for", "x-forwarded-proto", "accept", "accept-language", "cookie"}:
            req_line += f"{k}: {v}\r\n"
    req_line += "\r\n"
    upstream.sendall(req_line.encode("latin1", errors="replace"))


def _handle(client: socket.socket, client_addr, rest_b: bytes) -> None:
    try:
        request_line, headers, path, rest = _read_request_header(client)
        log.debug("recv %s %s from %s", request_line.split()[0], path, client_addr)
        if not path.startswith(GATEWAY_PREFIX):
            client.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        if path == GATEWAY_PREFIX:
            path = GATEWAY_PREFIX_STRICT
        content_length = headers.get("content-length", "0")
        try:
            clen = int(content_length)
        except ValueError:
            clen = 0

        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(UPSTREAM_SOCK)
        except FileNotFoundError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 32\r\n\r\nupstream socket not found")
            log.warning("upstream socket missing: %s", UPSTREAM_SOCK)
            return
        except Exception as e:
            client.sendall((f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(str(e))}\r\n\r\n{e}".encode("utf-8")))
            log.warning("upstream connect failed: %s", e)
            return

        try:
            _rewrite_request_target(request_line.split()[0], path, headers, upstream)
            log.debug("-> forwarding %s %s (clen=%d, rest=%d)",
                     request_line.split()[0], path, clen, len(rest))
            _forward_body_with_pre(client, clen, upstream, rest.encode("latin1", errors="replace"))
            log.debug("-> body sent, waiting response")
            _forward_response(upstream, client, request_line.split()[0])
            log.debug("-> response sent, done")
        finally:
            try:
                upstream.close()
            except OSError:
                pass
    except Exception as e:
        log.warning("handler error: %s", e)
        try:
            client.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def finish_request(self, request, client_address):
        try:
            _handle(request, client_address, b"")
        finally:
            try:
                request.close()
            except Exception:
                pass


def main() -> int:
    if os.path.exists(LISTEN_SOCK):
        try:
            os.unlink(LISTEN_SOCK)
        except OSError:
            pass
    server = ThreadingServer(LISTEN_SOCK, None)
    try:
        os.chmod(LISTEN_SOCK, 0o666)
    except OSError:
        pass
    log.info("listening %s -> %s", LISTEN_SOCK, UPSTREAM_SOCK)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            try:
                os.unlink(LISTEN_SOCK)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
