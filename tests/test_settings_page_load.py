#!/usr/bin/env python3
"""1.0.7 ④：设置页只发一次 `/_ext/settings`，且 favicon 零请求。

现场（1.0.6）：
  - `loadAll()` 串行调 `loadHealth()` → `loadSettings()` → `loadLogin()` → `loadSaveOpts()`，
    其中 `loadSettings()` 与 `loadSaveOpts()` **各发一次** `GET /_ext/settings`（同一次进页面 2 次）
  - 页面没有 favicon ⇒ 浏览器自动请求同源根 `/favicon.ico`，被应用中心接管成 67,646 B 的 HTML

本测试全部是**静态断言**（没有浏览器可用）：读 `app/web/settings.html` 与 `app/proxy/app.py`。
每条断言后面都配一句"切片非空"的守卫，避免切片写错导致恒真/恒假却静默通过。

运行：python3 tests/test_settings_page_load.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
HTML = (PKG / "app" / "web" / "settings.html").read_text(encoding="utf-8")
APP_PY = (PKG / "app" / "proxy" / "app.py").read_text(encoding="utf-8")

PASS, FAIL = [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append((name, e or "assert"))
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))


def slice_fn(name: str, width: int = 1200) -> str:
    """按"函数名 → 下一个顶层 function/async function"切片，并断言切片有效。"""
    i = HTML.find(name)
    assert i != -1, f"找不到 {name}"
    m = re.search(r"\n(?:async )?function ", HTML[i + len(name):])
    j = i + len(name) + m.start() if m else i + width
    seg = HTML[i:j]
    assert len(seg) > 40 and name in seg, f"{name} 切片异常（len={len(seg)}）"
    return seg


# ------------------------------------------------------------------ 请求次数
def l01():
    """GET /_ext/settings 在整页 JS 里只出现 1 次。"""
    n = len(re.findall(r"jget\(\s*'/_ext/settings'", HTML))
    assert n == 1, f"jget('/_ext/settings') 出现 {n} 次，应为 1"


def l02():
    """POST 仍是 3 处（三个保存按钮各一个），别为了合并把写操作也合了。"""
    n = len(re.findall(r"post\(\s*'/_ext/settings'", HTML))
    assert n == 3, f"post('/_ext/settings') 出现 {n} 次，应为 3"


def l03():
    """去掉注释后，`/_ext/settings` 的总出现次数 = 1 GET + 3 POST。"""
    body = re.sub(r"//[^\n]*", "", HTML)
    n = body.count("/_ext/settings")
    assert n == 4, f"`/_ext/settings` 实际引用 {n} 处，应为 4（1 GET + 3 POST）"


def l04():
    """in-flight Promise 去重：有 `_settingsP` 变量、复用它、失败要清空。"""
    assert "let _settingsP=null" in HTML, "没有 _settingsP 声明"
    seg = slice_fn("function fetchSettings")
    assert "force||!_settingsP" in seg, "没有复用已有 Promise"
    assert "_settingsP=jget(" in seg, "没有发起请求"
    assert "_settingsP=null" in seg, "失败后没清空（会把失败的 Promise 永久缓存）"
    assert "return _settingsP" in seg, "没返回共享 Promise"


def l05():
    """两个旧入口都变薄壳、共用同一个请求，且各自不再自己发请求。"""
    for fn in ("async function loadSettings", "async function loadSaveOpts"):
        seg = slice_fn(fn)
        assert "fetchSettings(" in seg, f"{fn} 没走 fetchSettings"
        assert "jget(" not in seg, f"{fn} 自己还在发请求"
        assert len(seg.splitlines()) <= 3, f"{fn} 不是薄壳（{len(seg.splitlines())} 行）"


def l06():
    """"刷新"必须绕过缓存（force=true），并一次渲染两块表单。"""
    seg = slice_fn("async function refreshSettings")
    assert "fetchSettings(true)" in seg, "刷新没有 force，会一直吃缓存"
    assert "renderSettings(d)" in seg and "renderSaveOpts(d)" in seg, "两块表单没共用同一份数据"


def l07():
    """loadAll 并发（Promise.all），且包含 refreshSettings。"""
    seg = slice_fn("async function loadAll")
    assert "Promise.all(" in seg, "loadAll 不是并发"
    assert "refreshSettings()" in seg, "loadAll 没接新的刷新入口"
    assert "loadSettings()" not in seg, "loadAll 还在单独调 loadSettings（会多一次请求）"
    assert "loadSaveOpts()" not in seg, "loadAll 还在单独调 loadSaveOpts（会多一次请求）"


def l08():
    """favicon 是 data-URI（零请求），且页面里不再引用 /favicon.ico。"""
    assert 'rel="icon"' in HTML, "没有 favicon 声明"
    assert "data:image/svg+xml" in HTML, "favicon 不是 data-URI"
    assert "/favicon.ico" not in HTML, "还在引用 /favicon.ico"


def l09():
    """没有为 favicon 新增后端路由（保持零请求的设计意图）。"""
    assert "favicon" not in APP_PY.lower(), "app.py 里出现了 favicon 相关代码"


def l10():
    """设置没读回来之前，三个保存按钮都点不动。"""
    for bid in ("btn-save-main", "btn-save-archive", "btn-save-advanced"):
        assert f'id="{bid}" disabled' in HTML, f"{bid} 缺初始 disabled"
    seg = slice_fn("function setSaveEnabled")
    assert "disabled=!on" in seg, "setSaveEnabled 没真正改 disabled"


def l11():
    """读失败：保持置灰 + 给一句人能看懂的提示（别静默留一张空表单）。"""
    seg = slice_fn("async function refreshSettings")
    assert "setSaveEnabled(false)" in seg, "读失败没保持置灰"
    assert "读取设置失败" in seg, "读失败没有提示文案"


def l12():
    """jget / post 两个工具函数还在（合并请求时别把它们删了）。"""
    assert "function jget" in HTML or "jget=" in HTML, "jget 不见了"
    assert "function post" in HTML or "post=" in HTML, "post 不见了"


case("L01 GET /_ext/settings 只 1 处", l01)
case("L02 POST /_ext/settings 仍 3 处", l02)
case("L03 全页引用总数 = 1 GET + 3 POST", l03)
case("L04 in-flight Promise 去重有效", l04)
case("L05 loadSettings/loadSaveOpts 变薄壳", l05)
case("L06 refreshSettings 强制绕过缓存", l06)
case("L07 loadAll 并发且不重复请求", l07)
case("L08 favicon 是 data-URI 且无 /favicon.ico", l08)
case("L09 app.py 无 favicon 路由", l09)
case("L10 三个保存按钮初始 disabled", l10)
case("L11 读失败保持置灰并提示", l11)
case("L12 jget/post 工具函数仍在", l12)

print(f"test_settings_page_load: PASS={len(PASS)} FAIL={len(FAIL)}")
for name, err in FAIL:
    print(f"  ❌ {name}: {err}")
sys.exit(1 if FAIL else 0)
