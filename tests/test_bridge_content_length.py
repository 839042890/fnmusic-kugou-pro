#!/usr/bin/env python3
"""1.0.7 ①：桥接器按 Content-Length 收尾，不再陪 uvicorn keep-alive 干等 5 秒。

背景（审查方实测）：所有经 `/app/fnmusic_kugou_pro/*` 的请求，响应体 8 毫秒就发完，
连接却要 5.009 秒才 EOF。根因是 `_forward_response()` 死等对端关闭，而 uvicorn 默认
`--timeout-keep-alive 5`，响应体发完后不关连接。

本文件锁四件事：
  1. 有 Content-Length：收满就返回，**不等 EOF**（这是主因，必须 <0.5s）
  2. 无 Content-Length / chunked：仍退回原来的 EOF 逻辑，且字节一个不能少
  3. 204 / 304 / HEAD：按 RFC 7230 §3.3.3 根本没有 body，同样不许等
  4. 1xx 不是终态：转出去后继续读真正的响应头（否则 100-continue 会把真响应吞掉）

两层验证：
  · 单元层：socketpair 直接喂字节给 `_forward_response()`，可控、快
  · 端到端：真起一个桥接器子进程 + 假上游 unix server，从客户端侧量 EOF 时间

运行：python3 tests/test_bridge_content_length.py
"""
from __future__ import annotations

import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
BRIDGE_PY = PROXY / "app_socket_bridge.py"

sys.path.insert(0, str(PROXY))
import app_socket_bridge as bridge  # noqa: E402

PASS, FAIL = [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


def _cl_response(body: bytes, extra_headers: bytes = b"") -> bytes:
    return (b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + extra_headers +
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


# ------------------------------------------------------------------ 单元层
def _unit_forward(fixture: bytes, method: str = "GET", hold: float = 5.0,
                  chunks: list[bytes] | None = None, delay: float = 0.0):
    """把 fixture 喂给 _forward_response，返回 (客户端收到的字节, 耗时, 异常)。"""
    up, up_peer = socket.socketpair()
    out, out_peer = socket.socketpair()
    got = bytearray()

    def reader():
        while True:
            try:
                b = out_peer.recv(65536)
            except OSError:
                break
            if not b:
                break
            got.extend(b)

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()

    def feeder():
        try:
            if chunks:
                for c in chunks:
                    up_peer.sendall(c)
                    time.sleep(delay)
            else:
                up_peer.sendall(fixture)
            # 关键：发完**不关连接**，像 uvicorn keep-alive 那样挂着
            time.sleep(hold)
        except OSError:
            pass
        finally:
            try:
                up_peer.close()
            except OSError:
                pass

    ft = threading.Thread(target=feeder, daemon=True)
    ft.start()

    err = None
    t0 = time.monotonic()
    try:
        bridge._forward_response(up, out, method)
    except BaseException as e:  # noqa: BLE001
        err = e
    elapsed = time.monotonic() - t0

    out.close()
    rt.join(2.0)
    up.close()
    out_peer.close()
    return bytes(got), elapsed, err


SMALL = b'{"status":"ok","data":{"kugou_url":"http://127.0.0.1:4000"}}'


def c01():
    """有 Content-Length：必须秒回，且字节完全一致。"""
    resp = _cl_response(SMALL)
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {len(got)} != {len(resp)}"
    assert elapsed < 0.5, f"仍在等 EOF：{elapsed:.3f}s（应 <0.5s）"


def c02():
    """响应头与 body 在同一次 sendall 里（rest 里带 body 前缀）也要一致。"""
    body = b"x" * 7000
    resp = _cl_response(body)
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {len(got)} != {len(resp)}"
    assert elapsed < 0.5, f"{elapsed:.3f}s"


def c03():
    """body 分多次、带间隔到达 —— 多读少读都算错。"""
    body = bytes(range(256)) * 40  # 10240 B
    head = (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
    chunks = [head + body[:100], body[100:5000], body[5000:]]
    got, elapsed, err = _unit_forward(b"", hold=5.0, chunks=chunks, delay=0.02)
    assert err is None, f"抛异常: {err!r}"
    assert got == head + body, f"字节不一致: {len(got)} != {len(head) + len(body)}"
    assert elapsed < 1.0, f"{elapsed:.3f}s"


def c04():
    """5 MiB body：不许截断、不许卡住（对应归档/大 JSON 场景）。"""
    body = os.urandom(5 * 1024 * 1024)
    resp = _cl_response(body)
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert len(got) == len(resp), f"长度不符: {len(got)} != {len(resp)}"
    assert got == resp, "大 body 字节不一致"
    assert elapsed < 2.0, f"{elapsed:.3f}s"


def c05():
    """206 部分内容（带 Content-Range）+ Content-Length：同样按长度收尾。"""
    body = b"RIFF" + os.urandom(4096)
    resp = _cl_response(body, extra_headers=b"Content-Range: bytes 0-4099/9999999\r\n"
                                           b"Accept-Ranges: bytes\r\n")
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, "206 响应字节不一致"
    assert elapsed < 0.5, f"{elapsed:.3f}s"


def c06():
    """chunked：没有 Content-Length，退回 EOF 逻辑，但必须收全。"""
    body = b'{"ok":true}'
    resp = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n")
    got, elapsed, err = _unit_forward(resp, hold=0.3)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"chunked 字节不一致: {len(got)} != {len(resp)}"


def c07():
    """204 无 body：按规范立刻收尾，不许等 5 秒。"""
    resp = b"HTTP/1.1 204 No Content\r\n\r\n"
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {got!r}"
    assert elapsed < 0.5, f"204 仍在等 EOF：{elapsed:.3f}s"


def c08():
    """304 无 body（条件请求命中的常见回包）：同样立刻收尾。"""
    resp = b"HTTP/1.1 304 Not Modified\r\nETag: \"abc\"\r\n\r\n"
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {got!r}"
    assert elapsed < 0.5, f"304 仍在等 EOF：{elapsed:.3f}s"


def c09():
    """HEAD：请求方法决定没有 body，即使带了 Content-Length 也不许等。"""
    resp = b"HTTP/1.1 200 OK\r\nContent-Length: 1234\r\n\r\n"
    got, elapsed, err = _unit_forward(resp, method="HEAD", hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {got!r}"
    assert elapsed < 0.5, f"HEAD 仍在等 EOF：{elapsed:.3f}s"


def c10():
    """1xx 不是终态：100 Continue 之后跟真响应，两段都要转出去。"""
    resp = (b"HTTP/1.1 100 Continue\r\n\r\n"
            + _cl_response(SMALL))
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"1xx 后真响应丢了: {len(got)} != {len(resp)}"
    assert elapsed < 0.5, f"{elapsed:.3f}s"


def c11():
    """Content-Length: 0：只有头，立刻收尾。"""
    resp = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    got, elapsed, err = _unit_forward(resp, hold=5.0)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {got!r}"
    assert elapsed < 0.5, f"{elapsed:.3f}s"


def c12():
    """无 Content-Length、非 chunked（不规范但对端可能这么发）：仍要收全。"""
    resp = b"HTTP/1.1 200 OK\r\nX-Whatever: 1\r\n\r\nlegacy-body"
    got, elapsed, err = _unit_forward(resp, hold=0.3)
    assert err is None, f"抛异常: {err!r}"
    assert got == resp, f"字节不一致: {len(got)} != {len(resp)}"


def c13():
    """源码级：`_forward_response` 里不许再出现"无条件 recv 到 EOF"的老写法。"""
    src = BRIDGE_PY.read_text(encoding="utf-8")
    i = src.find("def _forward_response(")
    assert i != -1, "找不到 _forward_response"
    j = src.find("\ndef ", i + 10)
    seg = src[i:j if j > i else len(src)]
    assert len(seg) > 400, f"切片异常短（可能切错位置）: {len(seg)}"
    assert "_pump_exact" in seg, "缺少按 Content-Length 收尾的调用"
    assert "content-length" in seg.lower(), "切片里没读到 Content-Length"
    # 老写法（while True: recv -> if not chunk: break 直接当唯一出口）不许单独存在：
    # 现在只有 chunked / 无长度时才允许走这段
    assert seg.count("while True") <= 1, f"EOF 循环出现了 {seg.count('while True')} 次"


# ------------------------------------------------------------------ 端到端
class _FakeUpstream(socketserver.ThreadingUnixStreamServer):
    """假上游：收一个请求，回一段固定字节，然后像 uvicorn 一样挂着不关。"""

    daemon_threads = True

    def __init__(self, path, fixture, hold):
        self.fixture = fixture
        self.hold = hold
        super().__init__(path, _FakeUpstreamHandler)


class _FakeUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        data = b""
        while b"\r\n\r\n" not in data:
            c = self.request.recv(8192)
            if not c:
                return
            data += c
        try:
            self.request.sendall(srv.fixture)
            time.sleep(srv.hold)  # 不关连接
        except OSError:
            pass


def _probe_app_socket(app_sock, path="/app/fnmusic_kugou_pro/_ext/settings"):
    """像审查方那样用**原始 socket** 量：响应体收全耗时 + 连接 EOF 耗时。"""
    t0 = time.monotonic()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20.0)
    s.connect(str(app_sock))
    s.sendall(f"GET {path} HTTP/1.1\r\nhost: localhost\r\naccept: */*\r\n\r\n".encode())
    data, t_body = b"", None
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        if t_body is None:
            data += chunk
            if b"\r\n\r\n" in data:
                head, _, body = data.partition(b"\r\n\r\n")
                marker = b"content-length:"
                low = head.lower()
                if marker in low:
                    try:
                        n = int(low.split(marker, 1)[1].split(b"\r\n", 1)[0].strip())
                    except ValueError:
                        n = -1
                    if n >= 0 and len(body) >= n:
                        t_body = time.monotonic() - t0
    t_eof = time.monotonic() - t0
    s.close()
    return data, t_body, t_eof


def _e2e(fixture, hold=5.0, method="GET", expect_cl=True):
    """起假上游 + 真桥接器子进程，返回 (客户端收到的字节, body 耗时, EOF 耗时)。"""
    root = Path(tempfile.mkdtemp(prefix="fnmbridge-"))
    up_sock = root / "up.sock"
    app_sock = root / "app.sock"
    srv = _FakeUpstream(str(up_sock), fixture, hold)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({"FNMUSIC_UPSTREAM_SOCK": str(up_sock), "FNMUSIC_APP_SOCK": str(app_sock)})
    proc = subprocess.Popen([sys.executable, str(BRIDGE_PY)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if app_sock.exists():
                break
            if proc.poll() is not None:
                raise AssertionError(f"桥接器进程提前退出，rc={proc.returncode}")
            time.sleep(0.02)
        else:
            raise AssertionError("桥接器 10 秒内没起来")
        time.sleep(0.05)
        data, t_body, t_eof = _probe_app_socket(app_sock)
        return data, t_body, t_eof
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()
        srv.server_close()
        for p in (up_sock, app_sock):
            try:
                p.unlink()
            except OSError:
                pass


def e01():
    """端到端（这就是审查方的验收姿势）：有 Content-Length 时 EOF 必须 <0.1s。"""
    resp = _cl_response(SMALL)
    data, t_body, t_eof = _e2e(resp, hold=5.0)
    assert t_body is not None, "响应体没收全"
    assert t_eof < 0.1, f"连接 EOF 仍要 {t_eof:.3f}s（应 <0.1s；改前是 5.0s）"
    # 客户端收到的应当是"转出去的原始响应"，只差被 bridge 重写过的请求头不影响响应
    assert data == resp, f"端到端字节不一致: {len(data)} != {len(resp)}"


def e02():
    """端到大 body（2 MiB）+ 保持连接：仍然 <0.5s 收尾且字节一致。"""
    body = os.urandom(2 * 1024 * 1024)
    resp = _cl_response(body)
    data, t_body, t_eof = _e2e(resp, hold=5.0)
    assert data == resp, f"字节不一致: {len(data)} != {len(resp)}"
    assert t_eof < 0.5, f"{t_eof:.3f}s"


def e03():
    """端到端 chunked：退回 EOF 逻辑，但内容必须完整（不回归流式响应）。"""
    body = b'{"list":[]}'
    resp = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n")
    data, t_body, t_eof = _e2e(resp, hold=0.3)
    assert data == resp, f"chunked 端到端字节不一致: {len(data)} != {len(resp)}"


def e04():
    """端到端 304：立刻收尾（本地优先的条件请求会用到）。"""
    resp = b"HTTP/1.1 304 Not Modified\r\nETag: \"x\"\r\n\r\n"
    data, t_body, t_eof = _e2e(resp, hold=5.0)
    assert data == resp, f"字节不一致: {data!r}"
    assert t_eof < 0.1, f"304 仍要 {t_eof:.3f}s"


case("C01 Content-Length 响应秒回（不再等 EOF）", c01)
case("C02 头与 body 同时在 rest 里也一致", c02)
case("C03 body 分片到达仍逐字节一致", c03)
case("C04 5MiB body 不截断不卡住", c04)
case("C05 206 部分内容按长度收尾", c05)
case("C06 chunked 退回 EOF 且收全", c06)
case("C07 204 无 body 不等待", c07)
case("C08 304 无 body 不等待", c08)
case("C09 HEAD 不等待", c09)
case("C10 1xx 后跟真响应不丢", c10)
case("C11 Content-Length: 0 立刻收尾", c11)
case("C12 无长度非 chunked 仍收全", c12)
case("C13 源码里不再有无条件 recv 到 EOF 当唯一出口", c13)
case("E01 端到端 EOF <0.1s（审查方验收姿势）", e01)
case("E02 端到端 2MiB 且收尾 <0.5s", e02)
case("E03 端到端 chunked 不回归", e03)
case("E04 端到端 304 秒回", e04)

print(f"test_bridge_content_length: PASS={len(PASS)} FAIL={len(FAIL)}")
for name, err in FAIL:
    print(f"  ❌ {name}: {err}")
sys.exit(1 if FAIL else 0)
