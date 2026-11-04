#!/usr/bin/env python3
"""1.0.7 ③ P-10：空 kugou_url 不再被当成"非法地址"报 400。

现场：设置页刚打开、`loadSettings()` 还没返回时点「保存」，输入框是空的，
提交的是 `kugou_url=`，后端回 400「必须以 http:// 或 https:// 开头」。等一会儿再点就正常。

根因：`_sanitize_kugou_url()` 对空值返回 ""（docstring 写着"调用方视为没改"），
但调用方把 "" 判成了「非法 scheme」。

两道保险都要测：
  1. 后端：空值跳过（等于没改），只有非空且 scheme 不对才 400
  2. 前端：设置读回来之前，三个保存按钮一律 disabled（用户根本点不下去）

运行：python3 tests/test_settings_empty_url.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
WEB = PKG / "app" / "web" / "settings.html"

ROOT = Path(tempfile.mkdtemp(prefix="fnmusic-emptyurl-"))
for _p in ("etc", "var", "home"):
    (ROOT / "fnmusic_kugou_pro" / _p).mkdir(parents=True, exist_ok=True)
ENV_FILE = ROOT / "fnmusic_kugou_pro" / "etc" / ".env"

BASE_URL = "http://127.0.0.1:4000"   # 本机实际配置的端口（8899 是酷狗 API 的官方默认兜底值）
ENV_FILE.write_text(f"FNMUSIC_KUGOU_URL={BASE_URL}\nFNMUSIC_KUGOU_QUALITY=high\n",
                    encoding="utf-8")

os.environ.update({
    "FNMUSIC_ENV_FILE": str(ENV_FILE),
    "FNMUSIC_KUGOU_ENABLED": "1",
    "FNMUSIC_KUGOU_URL": BASE_URL,
    "FNMUSIC_KUGOU_QUALITY": "high",
    "FNMUSIC_UPSTREAM_SOCK": str(ROOT / "no-upstream.socket"),
    "FNMUSIC_ETC_DIR": str(ROOT / "fnmusic_kugou_pro" / "etc"),
    "FNMUSIC_VAR_DIR": str(ROOT / "fnmusic_kugou_pro" / "var"),
    "FNMUSIC_PKG_HOME_DIR": str(ROOT / "fnmusic_kugou_pro" / "home"),
    "FNMUSIC_MUSIC_DB": str(ROOT / "absent.db"),
    "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
})
sys.path.insert(0, str(PROXY))

import httpx         # noqa: E402
import app            # noqa: E402

HTML = WEB.read_text(encoding="utf-8")
APP_PY = (PROXY / "app.py").read_text(encoding="utf-8")

PASS, FAIL = [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


def _reset_env(url: str = BASE_URL) -> None:
    ENV_FILE.write_text(f"FNMUSIC_KUGOU_URL={url}\nFNMUSIC_KUGOU_QUALITY=high\n",
                        encoding="utf-8")
    app.CONF["kugou_url"] = url


def _post(data: dict, path: str = "/app/fnmusic_kugou_pro/_ext/settings"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://test") as c:
            return await c.post(path, data=data)
    return asyncio.run(go())


def _env_url() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("FNMUSIC_KUGOU_URL="):
            return line.split("=", 1)[1]
    return ""


# ------------------------------------------------------------------ 后端
def p01():
    """空字符串：200，且 .env 里的地址一个字都没变。"""
    _reset_env()
    r = _post({"kugou_url": ""})
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert r.json().get("status") == "ok", r.text
    assert _env_url() == BASE_URL, f".env 被改成了 {_env_url()!r}"


def p02():
    """纯空白（用户回车留的空格）：同样跳过。"""
    _reset_env()
    r = _post({"kugou_url": "   \t "})
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert _env_url() == BASE_URL, f".env 被改成了 {_env_url()!r}"


def p03():
    """只带 quality、压根没有 kugou_url 键：200（这是"刚打开页面就点保存"的典型形态）。"""
    _reset_env()
    r = _post({"kugou_quality": "320"})
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert _env_url() == BASE_URL
    assert "FNMUSIC_KUGOU_QUALITY=320" in ENV_FILE.read_text(encoding="utf-8")


def p04():
    """空 URL + 其他字段一起提交：整体成功，URL 不动、其他字段生效。"""
    _reset_env()
    r = _post({"kugou_url": "", "kugou_quality": "128", "kugou_enabled": "1"})
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    text = ENV_FILE.read_text(encoding="utf-8")
    assert "FNMUSIC_KUGOU_QUALITY=128" in text, text
    assert _env_url() == BASE_URL


def p05():
    """非空但 scheme 不对：仍然 400（这道闸不能因为放行空值就松掉）。"""
    _reset_env()
    r = _post({"kugou_url": "ftp://nope"})
    assert r.status_code == 400, f"{r.status_code}: {r.text}"
    assert "http" in r.json().get("message", "").lower(), r.text
    assert _env_url() == BASE_URL, ".env 不该被写入非法地址"


def p06():
    """file:// 同样 400（SSRF 那道闸）。"""
    _reset_env()
    r = _post({"kugou_url": "file:///etc/passwd"})
    assert r.status_code == 400, f"{r.status_code}: {r.text}"
    assert _env_url() == BASE_URL


def p07():
    """合法地址照旧写入 —— 别把"放行空值"做成"谁都不写"。"""
    _reset_env()
    r = _post({"kugou_url": "http://127.0.0.1:4123"})
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert _env_url() == "http://127.0.0.1:4123", _env_url()
    assert app.CONF["kugou_url"] == "http://127.0.0.1:4123", app.CONF.get("kugou_url")


def p08():
    """GET ?save=1 走同一实现，空值也应放行（旧前端兼容路径不能落下）。"""
    _reset_env()
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://test") as c:
            return await c.get("/app/fnmusic_kugou_pro/_ext/settings?save=1&kugou_url=")
    r = asyncio.run(go())
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert _env_url() == BASE_URL


# ------------------------------------------------------------------ 静态
def p09():
    """源码：空判断必须出现在 400 之前（切片 + 防白测断言）。"""
    i = APP_PY.find('if "kugou_url" in form:')
    assert i != -1, "找不到 kugou_url 分支"
    j = APP_PY.find('if "kugou_quality" in form:', i)
    assert j > i, "切片终点无效"
    seg = APP_PY[i:j]
    assert len(seg) > 200, f"切片异常短: {len(seg)}"
    k_empty = seg.find('form["kugou_url"]')
    k_400 = seg.find('status_code=400')
    assert k_empty != -1 and k_400 != -1, (k_empty, k_400)
    assert k_empty < k_400, "空值判断必须在 400 之前"


def p10():
    """前端：三个保存按钮初始 disabled，且都带 id（setSaveEnabled 要按 id 点）。"""
    for bid in ("btn-save-main", "btn-save-archive", "btn-save-advanced"):
        assert f'id="{bid}" disabled' in HTML, f"{bid} 没有初始 disabled"
    assert HTML.count(" disabled onclick=") == 3, HTML.count(" disabled onclick=")


def p11():
    """前端：只有设置读成功后按钮才放出来；失败保持置灰并提示。"""
    i = HTML.find("async function refreshSettings()")
    assert i != -1, "找不到 refreshSettings"
    j = HTML.find("\nasync function ", i + 10)
    seg = HTML[i:j if j > i else i + 900]
    assert len(seg) > 100, f"切片异常短: {len(seg)}"
    assert "setSaveEnabled(true)" in seg, "读成功后没放开按钮"
    assert "setSaveEnabled(false)" in seg, "读失败没保持置灰"
    assert seg.find("renderSettings(d)") < seg.find("setSaveEnabled(true)"), \
        "必须先渲染再放开按钮（否则放开的是一张空表单）"


case("P01 空 URL → 200 且不改 .env", p01)
case("P02 纯空白 → 200 且不改 .env", p02)
case("P03 只带 quality 无 kugou_url → 200", p03)
case("P04 空 URL + 其他字段 → 200 且其他生效", p04)
case("P05 ftp:// → 400（闸门没松）", p05)
case("P06 file:// → 400", p06)
case("P07 合法地址照旧写入", p07)
case("P08 GET ?save=1 空值同样放行", p08)
case("P09 源码：空判断在 400 之前", p09)
case("P10 前端三个保存按钮初始 disabled", p10)
case("P11 前端读成功才放开按钮", p11)

print(f"test_settings_empty_url: PASS={len(PASS)} FAIL={len(FAIL)}")
for name, err in FAIL:
    print(f"  ❌ {name}: {err}")
sys.exit(1 if FAIL else 0)
