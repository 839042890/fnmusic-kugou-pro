#!/usr/bin/env python3
"""cmd/install_callback 生成 .env 的行为测试。

覆盖（都是实际踩过的坑）：
1. 向导里填的 FNMUSIC_* 以同名环境变量传入，必须写进 .env，不能被默认值覆盖。
2. 重装/覆盖安装时不能把用户已存的 token、落盘目录清空。
3. 值里带换行/控制字符不能破坏 .env 行结构。
4. v2.1.3 起 .env 写进**持久配置目录**（TRIM_PKGETC → @appconf/<app>），
   不再落安装目录；写成功后清掉安装目录里的旧文件。

关于布局：归属校验要求路径里出现"应用名"这一段，所以假的 APPDEST 必须放在
形如 <tmp>/fnmusic_kugou_pro/... 的路径下；同时**必须显式设置** TRIM_PKG*，
否则本机存在 /var/apps/fnmusic_kugou_pro/etc 符号链接，脚本会真的往系统
配置目录写（这个测试跑在非 root 下会因此失败）。

运行：PYTHONPATH= /tmp/tv3/bin/python tests/test_install_callback.py
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
INSTALL = PKG / "cmd" / "install_callback"
APP = "fnmusic_kugou_pro"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


class Layout:
    """形状与飞牛一致的应用根：<root>/<app>/{app,etc,var,home}。"""

    def __init__(self, prefix: str):
        self.root = Path(tempfile.mkdtemp(prefix=prefix)) / APP
        self.appdest = self.root / "app"          # 安装目录 target
        self.etc = self.root / "etc"              # → @appconf/<app>
        self.var = self.root / "var"              # → @appdata/<app>
        self.home = self.root / "home"            # → @apphome/<app>
        for p in (self.appdest, self.etc, self.var, self.home):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def env_file(self) -> Path:
        return self.etc / ".env"

    @property
    def legacy_env_file(self) -> Path:
        return self.appdest / ".env"

    def cleanup(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)


def run_install(lay: Layout, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """在假布局上跑 install_callback（venv 用空壳文件骗过依赖安装）。"""
    proxy = lay.appdest / "proxy"
    proxy.mkdir(parents=True, exist_ok=True)
    (proxy / "requirements.txt").write_text("")
    venv_bin = lay.appdest / ".venv-proxy" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    for name in ("pip", "uvicorn", "python3"):
        p = venv_bin / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)

    env = dict(os.environ)
    env["TRIM_APPDEST"] = str(lay.appdest)
    env["TRIM_TEMP_LOGFILE"] = str(lay.appdest / "install.log")
    # 显式注入持久目录：不设的话本机 /var/apps/<app>/etc 链接会被认到，
    # 脚本就往真实系统目录写了。
    env["TRIM_PKGETC"] = str(lay.etc)
    env["TRIM_PKGVAR"] = str(lay.var)
    env["TRIM_PKGHOME"] = str(lay.home)
    # 让 install_callback 找到的"python3.12"是我们能控制的空壳
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(INSTALL)], env=env,
                          capture_output=True, text=True)


def read_env(lay: Layout) -> dict[str, str]:
    out = {}
    p = lay.env_file
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def main() -> None:
    # ---------------------------------------------------------------- 1
    print("=" * 70)
    print("1) 向导填的值必须写进 .env（不能被默认值覆盖）+ 落到配置目录")
    print("=" * 70)
    L = Layout("fnmusic-inst-")
    r = run_install(L, {
        "FNMUSIC_KUGOU_URL": "http://10.0.0.9:8899",
        "FNMUSIC_KUGOU_QUALITY": "320",
        "FNMUSIC_LIBRARY_DIR": "/vol1/music",
    })
    check("install_callback 退出码 0", r.returncode == 0, f"{r.returncode} {r.stderr[-300:]}")
    env = read_env(L)
    check(".env 落在配置目录（不在安装目录）", L.env_file.is_file(), str(L.env_file))
    check("安装目录里没有留下 .env", not L.legacy_env_file.exists(),
          str(L.legacy_env_file))
    check("向导的 URL 保留了", env.get("FNMUSIC_KUGOU_URL") == "http://10.0.0.9:8899",
          env.get("FNMUSIC_KUGOU_URL", ""))
    check("向导的音质保留了", env.get("FNMUSIC_KUGOU_QUALITY") == "320",
          env.get("FNMUSIC_KUGOU_QUALITY", ""))
    check("向导的曲库目录保留了", env.get("FNMUSIC_LIBRARY_DIR") == "/vol1/music",
          env.get("FNMUSIC_LIBRARY_DIR", ""))
    check(".env 权限是 600", stat.S_IMODE(L.env_file.stat().st_mode) == 0o600,
          oct(stat.S_IMODE(L.env_file.stat().st_mode)))
    check("配置目录权限是 700", stat.S_IMODE(L.etc.stat().st_mode) == 0o700,
          oct(stat.S_IMODE(L.etc.stat().st_mode)))
    check("边播边存默认关闭", env.get("FNMUSIC_SAVE_ENABLED") == "0",
          env.get("FNMUSIC_SAVE_ENABLED", ""))
    check("归档内容默认全开",
          (env.get("FNMUSIC_SAVE_WRITE_TAGS"), env.get("FNMUSIC_SAVE_WRITE_COVER"),
           env.get("FNMUSIC_SAVE_WRITE_LYRIC")) == ("1", "1", "1"))
    check("收藏目录默认为空（让 app 端推导到持久 home，见 test_persist_dirs 第 8 节）",
          env.get("FNMUSIC_FAV_DIR") == "", repr(env.get("FNMUSIC_FAV_DIR")))
    check("收藏目录不指向安装目录", str(L.appdest) not in env.get("FNMUSIC_FAV_DIR", ""),
          env.get("FNMUSIC_FAV_DIR", ""))
    # .env 里不允许出现重复键：读回时以后者为准，排障时极易看错
    keys = [ln.split("=", 1)[0] for ln in L.env_file.read_text().splitlines() if "=" in ln]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    check(".env 没有重复键", not dupes, str(dupes))

    # ---------------------------------------------------------------- 2
    print()
    print("=" * 70)
    print("2) 重装/覆盖安装：已有 token 与落盘设置不能被清空")
    print("=" * 70)
    p = L.env_file
    txt = p.read_text()
    txt = txt.replace("FNMUSIC_KUGOU_TOKEN=", "FNMUSIC_KUGOU_TOKEN=SECRET123")
    txt = txt.replace("FNMUSIC_KUGOU_USERID=", "FNMUSIC_KUGOU_USERID=77777")
    txt = txt.replace("FNMUSIC_SAVE_ENABLED=0", "FNMUSIC_SAVE_ENABLED=1")
    txt = txt.replace("FNMUSIC_SAVE_DIR=", "FNMUSIC_SAVE_DIR=/vol1/music/saved")
    p.write_text(txt)

    # 重装时向导没填（不导出同名环境变量）
    r = run_install(L)
    check("重装退出码 0", r.returncode == 0, f"{r.returncode} {r.stderr[-300:]}")
    env = read_env(L)
    check("token 没被清空", env.get("FNMUSIC_KUGOU_TOKEN") == "SECRET123",
          env.get("FNMUSIC_KUGOU_TOKEN", ""))
    check("userid 没被清空", env.get("FNMUSIC_KUGOU_USERID") == "77777",
          env.get("FNMUSIC_KUGOU_USERID", ""))
    check("边播边存开关没被重置", env.get("FNMUSIC_SAVE_ENABLED") == "1",
          env.get("FNMUSIC_SAVE_ENABLED", ""))
    check("落盘目录没被清空", env.get("FNMUSIC_SAVE_DIR") == "/vol1/music/saved",
          env.get("FNMUSIC_SAVE_DIR", ""))
    check("重装后 URL 仍是上次的值", env.get("FNMUSIC_KUGOU_URL") == "http://10.0.0.9:8899",
          env.get("FNMUSIC_KUGOU_URL", ""))

    # ---------------------------------------------------------------- 2b
    print()
    print("=" * 70)
    print("2b) 【1.0.0 分叉】已删除旧版迁移 —— 安装目录的 .env 不该被迁移")
    print("=" * 70)
    # 依据：本分支以上游 2.1.0 为基线，2.1.0 起 .env 就在配置目录，
    # 不存在"安装目录里的旧 .env"这种历史上需要救援的状态。
    # 这个用例是**回归守卫**：防止将来有人"好心"把迁移逻辑加回来。
    L2 = Layout("fnmusic-inst2-")
    L2.legacy_env_file.write_text(
        "FNMUSIC_KUGOU_TOKEN=OLDTOKEN999\n"
        "FNMUSIC_KUGOU_USERID=1234567890\n"
        "FNMUSIC_SAVE_DIR=/vol1/music/old\n",
        encoding="utf-8",
    )
    r = run_install(L2)
    check("退出码 0（不因该文件报错）", r.returncode == 0, f"{r.returncode} {r.stderr[-300:]}")
    env2 = read_env(L2)
    check("安装目录的旧 token 未被迁移进配置目录",
          env2.get("FNMUSIC_KUGOU_TOKEN") != "OLDTOKEN999",
          repr(env2.get("FNMUSIC_KUGOU_TOKEN")))
    check("安装目录的旧落盘目录未被迁移",
          env2.get("FNMUSIC_SAVE_DIR") != "/vol1/music/old",
          repr(env2.get("FNMUSIC_SAVE_DIR")))
    check("配置目录 .env 由向导默认值生成（不是迁移来的）",
          env2.get("FNMUSIC_KUGOU_URL") != "",
          repr(env2.get("FNMUSIC_KUGOU_URL")))

    # ---------------------------------------------------------------- 2c
    print()
    print("=" * 70)
    print("2c) 【1.0.0 分叉】升级回调不再做任何迁移")
    print("=" * 70)
    up = (PKG / "cmd" / "upgrade_callback").read_text(encoding="utf-8")
    up_code = "\n".join(
        ln for ln in up.splitlines()
        if not ln.lstrip().startswith("#")
    )
    check("upgrade_callback 无 .env 迁移代码", "OLD_ENV" not in up_code and "NEW_ENV" not in up_code)
    check("upgrade_callback 无在线收藏迁移代码",
          "online_favorites" not in up_code)
    check("upgrade_callback 仅做语法检查（不阻断）", "py_compile" in up_code)

    # ---------------------------------------------------------------- 3
    print()
    print("=" * 70)
    print("3) 环境变量里塞换行/控制字符，不能破坏 .env 行结构")
    print("=" * 70)
    L4 = Layout("fnmusic-inst4-")
    r = run_install(L4, {
        "FNMUSIC_KUGOU_URL": "http://1.2.3.4:8899\nFNMUSIC_SAVE_ENABLED=99\nTOUCHED=1",
    })
    check("注入场景退出码 0", r.returncode == 0, f"{r.returncode} {r.stderr[-300:]}")
    raw = L4.env_file.read_text()
    env = read_env(L4)
    check("换行被剔除，没有多出键", "TOUCHED" not in env, str(sorted(env)))
    # 期望键数直接从安装脚本里数（echo "FNMUSIC_X=..." 的数量），
    # 这样加/删配置项时这个断言会自动跟上，不用手改数字。
    import re as _re
    script = (PKG / "cmd" / "install_callback").read_text(encoding="utf-8")
    expected_keys = len(set(_re.findall(r'echo "(FNMUSIC_[A-Z0-9_]+)=', script)))
    check(".env 行数没被撑开", len(raw.splitlines()) == expected_keys,
          f"{len(raw.splitlines())} != {expected_keys}")
    check("注入后仍是预期键集合", len(env) == expected_keys,
          f"{len(env)} != {expected_keys}")
    check("注入没能改写开关", env.get("FNMUSIC_SAVE_ENABLED") == "0",
          env.get("FNMUSIC_SAVE_ENABLED", ""))

    # ---------------------------------------------------------------- 4
    print()
    print("=" * 70)
    print("4) 归属校验函数 pick_pkg_dir：TRIM_* 串到别的应用必须拒绝")
    print("=" * 70)
    # 不直接跑 install_callback：TRIM_PKGETC 被拒后它会退回
    # /var/apps/<app>/etc（**真实系统目录**），在非 root 下写不进去、
    # 还会污染机器。改为把脚本里的 pick_pkg_dir 抠出来单独喂输入 ——
    # 这正是要测的那个纯函数。
    script = INSTALL.read_text(encoding="utf-8")
    fn = script[script.index("pick_pkg_dir() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    # 用一个**不存在**的链接路径当第二档，这样"被拒"的用例必然落到兜底，
    # 断言才能明确区分"接受了注入值"和"退到兜底"。
    probe = f'''
PKG_APP_NAME="{APP}"
LOGF="/dev/null"
{fn}
printf '%s\\n' "$(pick_pkg_dir TRIM_PKGETC /nonexistent/pkg/{APP}/etc /fallback)"
'''
    checks = [
        # (注入值, 期望, 说明)
        ("/vol1/@appconf/someapp", "/fallback",
         "外应用（末段是别的应用名）被拒"),
        ("/vol1/@appdata/someapp/etc", "/fallback",
         "外应用（应用名在中间）被拒"),
        ("", "/fallback",
         "空值 → 走链接/兜底"),
        ("/vol1/@appconf/fnmusic_kugou_pro", "/vol1/@appconf/fnmusic_kugou_pro",
         "本应用（末段是应用名）被接受"),
        ("/var/apps/fnmusic_kugou_pro/etc", "/var/apps/fnmusic_kugou_pro/etc",
         "本应用（应用名在中间）被接受"),
        ("/vol1/@appconf/fnmusic_kugou_pro_extra", "/fallback",
         "仅前缀相似但不是整段 → 仍被拒"),
    ]
    for val, want, desc in checks:
        env = dict(os.environ)
        env["TRIM_PKGETC"] = val
        r = subprocess.run(["bash", "-c", probe], env=env,
                           capture_output=True, text=True)
        got = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        check(desc, got == want, f"得到 {got!r}，期望 {want!r}")

    # ---------------------------------------------------------------- 5
    print()
    print("=" * 70)
    print("5) P-05 残留：平台回放旧表单值，绝不能覆盖用户已有配置")
    print("=" * 70)
    # 真实事故（1.0.2 真机升级到 1.0.2 时）：飞牛把**上次安装提交的表单值**
    # 原样回放给 install_callback。用户 .env 里是 high，回放值是 128
    # （1.0.1 时代留下的旧输入），而新包的 initValue 已经是 high ——
    # 旧逻辑「向导值 != initValue ⇒ 用户主动改过 ⇒ USE 向导值」于是把
    # 用户的 high 静默覆盖成 128，音质降级且无任何提示。
    #
    # 判据改为：.env 里**已有该键就一律保留**，向导值只在键不存在时生效。
    # 另给一个显式逃生口 FNMUSIC_ADOPT_WIZARD=1。

    def seed_and_run(wizard: dict[str, str], pre: dict[str, str] | None):
        """造一个"已有 .env"的布局（或空布局），带指定向导环境变量跑一遍。"""
        lay = Layout("fnmusic-replay-")
        if pre:
            lay.env_file.write_text(
                "".join(f"{k}={v}\n" for k, v in pre.items()), encoding="utf-8")
            lay.env_file.chmod(0o600)
        # 从外部 shell 继承来的 FNMUSIC_ADOPT_WIZARD 会干扰用例，显式钉死默认值
        wiz = {"FNMUSIC_ADOPT_WIZARD": "0"}
        wiz.update(wizard)
        return lay, run_install(lay, wiz)

    seeded = {
        "FNMUSIC_KUGOU_QUALITY": "high",
        "FNMUSIC_KUGOU_URL": "http://127.0.0.1:4000",
        "FNMUSIC_KUGOU_TOKEN": "TOKEN-保留我",
        "FNMUSIC_SAVE_WRITE_TAGS": "0",
    }

    # ① 平台回放旧表单值 ⇒ 一律保留（这就是那次事故的回归测试）
    L5, r5 = seed_and_run({"FNMUSIC_KUGOU_QUALITY": "128"}, seeded)
    e5 = read_env(L5)
    check("回放旧表单值：退出码 0", r5.returncode == 0, f"{r5.returncode} {r5.stderr[-300:]}")
    check("回放旧表单值：音质仍是 high（事故回归）",
          e5.get("FNMUSIC_KUGOU_QUALITY") == "high", repr(e5.get("FNMUSIC_KUGOU_QUALITY")))
    check("回放旧表单值：URL 仍是用户配的 4000",
          e5.get("FNMUSIC_KUGOU_URL") == "http://127.0.0.1:4000",
          repr(e5.get("FNMUSIC_KUGOU_URL")))
    check("回放旧表单值：token 未丢",
          e5.get("FNMUSIC_KUGOU_TOKEN") == "TOKEN-保留我", repr(e5.get("FNMUSIC_KUGOU_TOKEN")))
    check("回放旧表单值：用户关掉的开关没被打开",
          e5.get("FNMUSIC_SAVE_WRITE_TAGS") == "0", repr(e5.get("FNMUSIC_SAVE_WRITE_TAGS")))
    check("回放旧表单值：stderr 有 KEEP 提示（用户在安装界面看得见）",
          "KEEP FNMUSIC_KUGOU_QUALITY" in r5.stderr, r5.stderr[-400:])
    check("回放旧表单值：stdout 为空（提示绝不能写 stdout —— env_scalar 的返回值就是 stdout）",
          r5.stdout.strip() == "", repr(r5.stdout[:300]))
    check("本次新增的配置项仍能落地（.env 本无 LOCAL_FIRST → 写默认 1）",
          e5.get("FNMUSIC_LOCAL_FIRST") == "1", repr(e5.get("FNMUSIC_LOCAL_FIRST")))

    # ② 逃生口：显式要求采用向导值
    L5b, r5b = seed_and_run({"FNMUSIC_KUGOU_QUALITY": "128",
                             "FNMUSIC_ADOPT_WIZARD": "1"}, seeded)
    e5b = read_env(L5b)
    check("逃生口 FNMUSIC_ADOPT_WIZARD=1：音质采用向导值 128",
          e5b.get("FNMUSIC_KUGOU_QUALITY") == "128", repr(e5b.get("FNMUSIC_KUGOU_QUALITY")))
    check("逃生口：stderr 有 ADOPT 提示",
          "ADOPT FNMUSIC_KUGOU_QUALITY" in r5b.stderr, r5b.stderr[-400:])
    check("逃生口：未传向导值的键仍保留原值（token 不丢）",
          e5b.get("FNMUSIC_KUGOU_TOKEN") == "TOKEN-保留我", repr(e5b.get("FNMUSIC_KUGOU_TOKEN")))

    # ③ 逃生口 + 向导没传该字段 ⇒ 绝不能写成空值（比覆盖更糟）
    L5c, r5c = seed_and_run({"FNMUSIC_ADOPT_WIZARD": "1"}, seeded)
    e5c = read_env(L5c)
    check("逃生口但向导未传该字段：值保持 high（不写成空）",
          e5c.get("FNMUSIC_KUGOU_QUALITY") == "high", repr(e5c.get("FNMUSIC_KUGOU_QUALITY")))
    check("逃生口但向导未传该字段：URL 也保持",
          e5c.get("FNMUSIC_KUGOU_URL") == "http://127.0.0.1:4000",
          repr(e5c.get("FNMUSIC_KUGOU_URL")))

    # ④ 首次安装（.env 不存在）⇒ 向导值必须生效，这条**不能**被改坏
    L5d, r5d = seed_and_run({"FNMUSIC_KUGOU_QUALITY": "high"}, None)
    e5d = read_env(L5d)
    check("首次安装（无 .env）：向导音质生效", e5d.get("FNMUSIC_KUGOU_QUALITY") == "high",
          repr(e5d.get("FNMUSIC_KUGOU_QUALITY")))
    L5e, r5e = seed_and_run({"FNMUSIC_KUGOU_QUALITY": "320"}, None)
    check("首次安装（无 .env）：非默认向导值也生效",
          read_env(L5e).get("FNMUSIC_KUGOU_QUALITY") == "320",
          repr(read_env(L5e).get("FNMUSIC_KUGOU_QUALITY")))

    for obj in (L, L2, L4, L5, L5b, L5c, L5d, L5e):
        obj.cleanup()


try:
    main()
finally:
    print()
    print("=" * 70)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 70)
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)

sys.exit(1 if FAIL else 0)
