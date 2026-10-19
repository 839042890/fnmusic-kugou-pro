#!/usr/bin/env python3
"""v2.1.3：可变状态迁出安装目录（持久化）回归测试。

背景（同一个 bug 的第三处）：
飞牛的安装目录（target / `/vol1/@appcenter/<app>`）是**程序目录**，覆盖安装会
被整体替换。凡是"跑起来才产生"的东西放进去，升级一次就归零。历史事故：
  1. save_index/ 去重标记被清 → 听过的歌重下 → 存成「歌名 (2).flac」(2.1.2 修)
  2. .env 被包内空模板覆盖 → 酷狗登录态丢失 → "搜索突然搜不到歌"   (2.1.3 修)
  3. fav_dir（在线收藏）在安装目录内 → 收藏列表清零               (2.1.3 修)

飞牛给的三处持久目录（install_init 已建好，/var/apps/<app>/<sub> 是符号链接）：
  TRIM_PKGETC  → etc  → /vol1/@appconf/<app>   配置（.env）
  TRIM_PKGVAR  → var  → /vol1/@appdata/<app>   运行数据（缓存、索引）
  TRIM_PKGHOME → home → /vol1/@apphome/<app>   用户数据（在线收藏）

覆盖：路径解析与归属校验、.env 读写落点、启动期迁移、配置回读白名单
（含 int/float 强制转换）、fav_dir 不在安装目录、app.tgz 内不含 .env。

运行：PYTHONPATH= /tmp/testvenv/bin/python tests/test_persist_dirs.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PROXY = PKG / "app" / "proxy"
PY = sys.executable

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 子进程脚本：路径解析必须在导入期完成，同进程改 os.environ 不生效
# ---------------------------------------------------------------------------
RESOLVE_CHILD = r'''
import json, logging, os, sys
from pathlib import Path
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
src = Path(os.environ["_T_APP"]).read_text(encoding="utf-8")
start = src.index("_HOME = os.environ.get")
end = src.index("CONF = {")
ns = {"os": os, "Path": Path, "__file__": os.environ["_T_APP"],
      "logger": logging.getLogger("probe")}
exec(src[start:end], ns)
print("@@JSON@@" + json.dumps({
    "etc": ns["_ETC_DIR"], "var": ns["_VAR_DIR"], "home": ns["_PKG_HOME_DIR"],
    "env_file": str(ns["_ENV_FILE"]),
    "install": ns["_INSTALL_DIR"],
}))
'''


def resolve(extra_env: dict[str, str]) -> tuple[dict, str]:
    env = dict(os.environ)
    env.update({
        "_T_APP": str(PROXY / "app.py"),
        "PYTHONPATH": "",
        # 先把宿主可能注入的值清干净，再由 extra_env 决定
        "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
        "FNMUSIC_ETC_DIR": "", "FNMUSIC_VAR_DIR": "", "FNMUSIC_PKG_HOME_DIR": "",
        # P-02：显式 .env 覆盖也必须清掉，否则真机设过它会改写下面的 env_file 断言
        "FNMUSIC_ENV_FILE": "",
    })
    env.update(extra_env)
    r = subprocess.run([PY, "-c", RESOLVE_CHILD], capture_output=True, text=True, env=env)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@JSON@@")]
    if not line:
        return {}, (r.stdout + r.stderr)[-800:]
    return json.loads(line[-1][len("@@JSON@@"):]), r.stderr


print("=" * 70)
print("1) 路径解析：TRIM_* 归属校验（串到别的应用必须拒绝）")
print("=" * 70)
# 场景：TRIM_PKGETC 等是"调用方应用"的上下文。若由别的应用带进来的
# 环境启动（或变量串了），值会指向**另一个应用**的目录
# （下面用 someapp 作占位）。盲信就会把含酷狗 token 的 .env 写进
# 别的应用目录 —— 功能故障 + 凭据泄漏。
d, err = resolve({
    "TRIM_PKGETC": "/vol1/@appconf/someapp",
    "TRIM_PKGVAR": "/vol1/@appdata/someapp",
    "TRIM_PKGHOME": "/vol1/@apphome/someapp",
})
check("子进程解析成功", bool(d), err)
if d:
    # ⚠ 断言不能写成 '"someapp" not in d["etc"]'：那个字符串在本测试里
    # 只是一个**测试夹具名**，换个名字断言就失去意义（等于测"字符串在不在"，
    # 而不是测"归属校验有没有生效"），而"拒绝后回退"在应用尚未安装时
    # 会落到安装目录。要判的是**是否被拒绝**，就该看
    # 「拒绝的 WARNING 有没有打」+「取值没被那个外部应用的值带走」。
    check("外应用的 TRIM_PKGETC 被拒绝（打了拒绝日志）",
          "看起来不属于本应用" in err and "TRIM_PKGETC" in err, err[-300:])
    check("外应用的 TRIM_PKGVAR 被拒绝（打了拒绝日志）",
          "TRIM_PKGVAR" in err, err[-300:])
    check("外应用的 TRIM_PKGHOME 被拒绝（打了拒绝日志）",
          "TRIM_PKGHOME" in err, err[-300:])
    check("取值没有采用被拒的外部路径",
          d["etc"] != "/vol1/@appconf/someapp"
          and d["var"] != "/vol1/@appdata/someapp"
          and d["home"] != "/vol1/@apphome/someapp",
          f'{d["etc"]} | {d["var"]} | {d["home"]}')
    # 回退目标：已安装时是 /var/apps/<app>/<sub>；未安装时回落到安装目录
    # （_INSTALL_DIR，即 <pkg>/app）。两种都正常，只要**落在本应用自己范围内**，
    # 而不是被那个被拒的外部应用路径带走。
    check("回退到本应用自己的目录（/var/apps 链接或安装目录）",
          d["etc"] in ("/var/apps/fnmusic_kugou_pro/etc", str(PKG / "app")),
          d["etc"])

print()
print("=" * 70)
print("2) 路径解析：正确注入时按注入值走")
print("=" * 70)
d2, err2 = resolve({
    "TRIM_PKGETC": "/vol1/@appconf/fnmusic_kugou_pro",
    "TRIM_PKGVAR": "/vol1/@appdata/fnmusic_kugou_pro",
    "TRIM_PKGHOME": "/vol1/@apphome/fnmusic_kugou_pro",
})
check("解析成功", bool(d2), err2)
if d2:
    check("etc 用注入值", d2["etc"] == "/vol1/@appconf/fnmusic_kugou_pro", d2["etc"])
    check("var 用注入值", d2["var"] == "/vol1/@appdata/fnmusic_kugou_pro", d2["var"])
    check("home 用注入值", d2["home"] == "/vol1/@apphome/fnmusic_kugou_pro", d2["home"])
    check(".env 落在 etc 目录", d2["env_file"] == "/vol1/@appconf/fnmusic_kugou_pro/.env",
          d2["env_file"])
    # 【1.0.0 分叉】原来这里断言"存在指向安装目录的旧位置（迁移源）"，
    # 迁移逻辑删除后该属性已不存在，改为断言**没有**这个概念。
    check("无旧位置属性（迁移源已删）", "legacy" not in d2, str(sorted(d2)))

print()
print("=" * 70)
print("3) run_proxy.sh 导出的别名优先（且自身也过归属校验）")
print("=" * 70)
d3, err3 = resolve({
    "FNMUSIC_ETC_DIR": "/vol1/@appconf/fnmusic_kugou_pro",
    "FNMUSIC_VAR_DIR": "/vol1/@appdata/fnmusic_kugou_pro",
    "FNMUSIC_PKG_HOME_DIR": "/vol1/@apphome/fnmusic_kugou_pro",
    # 故意同时串一个 TRIM_*：别名应当胜出
    "TRIM_PKGETC": "/vol1/@appconf/someapp",
})
check("解析成功", bool(d3), err3)
if d3:
    check("别名压过被串的 TRIM_PKGETC",
          d3["etc"] == "/vol1/@appconf/fnmusic_kugou_pro", d3["etc"])

d4, err4 = resolve({"FNMUSIC_ETC_DIR": "/vol1/@appconf/someapp"})
# 同 1)：断言不能靠"路径里没有 someapp"——那是夹具名，不是判据。
check("别名串了也被拒绝（打了拒绝日志）",
      "看起来不属于本应用" in err4 and "FNMUSIC_ETC_DIR" in err4, err4[-300:])
check("别名串了不采用那个外部路径",
      bool(d4) and d4["etc"] != "/vol1/@appconf/someapp",
      d4.get("etc", err4))

print()
print("=" * 70)
print("4) app.tgz 里不能有 .env（根治：否则空模板先盖掉凭证）")
print("=" * 70)
# 飞牛解包 app.tgz **先于** callback 执行。包里若带 .env，那份全空模板会在
# upgrade_callback 有机会迁移之前就把安装目录里的旧凭证覆盖掉 → 救不回来。
# 真机上 app.tgz 解包后即被删除，所以按三种位置找，找到哪个都算数：
#   1. 打包前的构建目录      <pkg>/app.tgz
#   2. 手动解包 .fpk 的结果  <pkg>/../app.tgz
#   3. 直接读 .fpk 里的那份  <pkg>/../*.fpk
def _tgz_names(path: Path) -> list[str] | None:
    try:
        with tarfile.open(path, "r:gz") as tf:
            return [n.lstrip("./") for n in tf.getnames()]
    except Exception:
        return None


names: list[str] | None = None
src = ""
for cand in (PKG / "app.tgz", PKG.parent / "app.tgz"):
    if cand.exists():
        names = _tgz_names(cand)
        src = str(cand)
        break
if names is None:
    # .fpk 里嵌套一个 app.tgz，要把内层取出来再读。
    # 只认 manifest 里 appname 与 version 都对得上的包 —— /tmp 里可能躺着
    # 早先版本留下的旧 .fpk，按文件名 glob 会读到错的包，测试就假绿了。
    import io
    want_ver = ""
    mf = PKG / "manifest"
    if mf.exists():
        m = re.search(r"^version\s*=\s*(\S+)", mf.read_text(encoding="utf-8"), re.M)
        want_ver = m.group(1) if m else ""
    cands = sorted(PKG.parent.glob("*.fpk"))
    if want_ver:
        cands = [p for p in cands if want_ver in p.name] or cands
    for fpk in cands:
        try:
            with tarfile.open(fpk, "r:gz") as outer:
                mtext = outer.extractfile("manifest").read().decode("utf-8", "replace")
                if f"version               = {want_ver}" not in mtext and mtext:
                    continue
                blob = outer.extractfile("app.tgz")
                if blob is None:
                    continue
                with tarfile.open(fileobj=io.BytesIO(blob.read()), mode="r:gz") as inner:
                    names = [n.lstrip("./") for n in inner.getnames()]
            src = f"{fpk}!app.tgz"
            break
        except Exception:
            continue

if names is None:
    check("能定位到 app.tgz（本项须在构建产物上跑）", False,
          f"{PKG}/app.tgz、{PKG.parent}/app.tgz、{PKG.parent}/*.fpk 都没找到")
else:
    envs = [n for n in names if n == ".env" or n.endswith("/.env")]
    check("app.tgz 内不含 .env", not envs, f"{src}: {envs}")
    check("app.tgz 内含 proxy/app.py（确认拿到的是真包）",
          any(n.endswith("proxy/app.py") for n in names), src)

print()
print("=" * 70)
print("5) .env 读回白名单：int/float 必须强制转换")
print("=" * 70)
# 这几项曾"写得进、读不回"：.env 里有值但没有任何回读路径，网页端改完
# 重启就"恢复"成默认值。补白名单后必须保证类型正确（否则 int 比较恒不等）。
READBACK_CHILD = r'''
import json, os, sys
os.environ.update({
    "FNMUSIC_HOME_DIR": os.environ["_T_INSTALL_HOME"],
    "FNMUSIC_KUGOU_ENABLED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
})
sys.path.insert(0, os.environ["_T_PROXY"])
import app
print("@@JSON@@" + json.dumps({
    "env_file": str(app.resolve_env_file()),
    "save_max_bytes": app.CONF.get("save_max_bytes"),
    "save_timeout_s": app.CONF.get("save_timeout_s"),
    "kugou_search_timeout": app.CONF.get("kugou_search_timeout"),
    "fav_dir": app.CONF.get("fav_dir"),
    "save_marker_dir": app.CONF.get("save_marker_dir"),
    "types": {
        "save_max_bytes": type(app.CONF.get("save_max_bytes")).__name__,
        "save_timeout_s": type(app.CONF.get("save_timeout_s")).__name__,
        "kugou_search_timeout": type(app.CONF.get("kugou_search_timeout")).__name__,
    },
}))
'''
APP = "fnmusic_kugou_pro"


def mkpkgroot(prefix: str) -> tuple[Path, Path, Path, Path, Path]:
    """造一个形状跟飞牛一致的应用根：<root>/<app>/{etc,var,home,app}。

    必须让路径里出现**应用名这一段**：归属校验按整段匹配，否则会把我们的
    临时目录当成"别的应用"而拒掉，测试就测不到真正要测的东西。
    """
    root = Path(tempfile.mkdtemp(prefix=prefix)) / APP
    etc, var, home, inst = root / "etc", root / "var", root / "home", root / "app"
    for p in (etc, var, home, inst):
        p.mkdir(parents=True, exist_ok=True)
    return root, etc, var, home, inst


def child_env(etc: Path, var: Path, home: Path, inst: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "_T_ETC": str(etc), "_T_VAR": str(var), "_T_HOME": str(home),
        "_T_INSTALL_HOME": str(inst),
        "_T_PROXY": str(PROXY), "PYTHONPATH": "",
        "PYTHONDONTWRITEBYTECODE": "1",
        # 别让宿主注入的值漏进来（app.py 会优先认它们）
        "TRIM_PKGETC": "", "TRIM_PKGVAR": "", "TRIM_PKGHOME": "",
        "FNMUSIC_ETC_DIR": "", "FNMUSIC_VAR_DIR": "", "FNMUSIC_PKG_HOME_DIR": "",
        # P-02：显式 .env 覆盖也必须清掉，否则真机设过它会改写下面的 env_file 断言
        "FNMUSIC_ENV_FILE": "",
    })
    env.update({
        "FNMUSIC_ETC_DIR": str(etc), "FNMUSIC_VAR_DIR": str(var),
        "FNMUSIC_PKG_HOME_DIR": str(home),
    })
    return env


def run_child(src: str, env: dict[str, str]) -> tuple[dict | None, str]:
    r = subprocess.run([PY, "-c", src], capture_output=True, text=True,
                       env=env, cwd=str(PKG))
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@JSON@@")]
    if not line:
        return None, (r.stdout + r.stderr)[-900:]
    return json.loads(line[-1][len("@@JSON@@"):]), r.stderr


_root5, etc5, var5, home5, inst5 = mkpkgroot("fnmusic-persist-")
(etc5 / ".env").write_text(
    "FNMUSIC_SAVE_MAX_BYTES=1048576\n"          # int
    "FNMUSIC_SAVE_TIMEOUT_S=42.5\n"             # float
    "FNMUSIC_KUGOU_SEARCH_TIMEOUT=9.5\n"        # float
    "FNMUSIC_FAV_DIR=/vol1/music/my_favs\n"     # str
    "FNMUSIC_SAVE_MARKER_DIR=/vol1/music/idx\n" # str
    "FNMUSIC_SAVE_TIMEOUT_S_BAD=notafloat\n"
    "FNMUSIC_KUGOU_TOKEN=\n",
    encoding="utf-8",
)
got, err5 = run_child(READBACK_CHILD, child_env(etc5, var5, home5, inst5))
if got is None:
    check("回读子进程执行成功", False, err5)
else:
    check("回读子进程执行成功", True)
    check("读的就是配置目录那份 .env", got["env_file"] == str(etc5 / ".env"),
          got["env_file"])
    check("save_max_bytes 回读为 int 且值正确",
          got["types"]["save_max_bytes"] == "int" and got["save_max_bytes"] == 1048576,
          f'{got["types"]["save_max_bytes"]}={got["save_max_bytes"]}')
    check("save_timeout_s 回读为 float 且值正确",
          got["types"]["save_timeout_s"] == "float" and got["save_timeout_s"] == 42.5,
          f'{got["types"]["save_timeout_s"]}={got["save_timeout_s"]}')
    check("kugou_search_timeout 回读为 float 且值正确",
          got["types"]["kugou_search_timeout"] == "float" and got["kugou_search_timeout"] == 9.5,
          f'{got["types"]["kugou_search_timeout"]}={got["kugou_search_timeout"]}')
    check("fav_dir 回读", got["fav_dir"] == "/vol1/music/my_favs", str(got["fav_dir"]))
    check("save_marker_dir 回读", got["save_marker_dir"] == "/vol1/music/idx",
          str(got["save_marker_dir"]))

print()
print("=" * 70)
print("6) 【1.0.0 分叉】安装目录的 .env 必须被忽略（迁移逻辑已删除）")
print("=" * 70)
LEGACY_TXT = (
    "FNMUSIC_KUGOU_TOKEN=abc123token\n"
    "FNMUSIC_KUGOU_USERID=1234567890\n"
    "FNMUSIC_KUGOU_DFID=\n"
    "FNMUSIC_SAVE_DIR=/vol1/music/saved\n"
)
print()
print("=" * 70)
print("6) 【1.0.0 分叉】安装目录的 .env 必须被忽略（迁移逻辑已删除）")
print("=" * 70)
# 依据：以 2.1.0 为基线，.env 本就只在配置目录；不存在"安装目录旧 .env"。
# 删除迁移后要求更严：即便安装目录真有一份 .env，也必须**不读它** ——
# 因为安装目录是可被覆盖安装整体替换的程序目录，读它 = 把可变状态放错地方。
IGNORE_CHILD = r'''
import json, os, sys
from pathlib import Path
os.environ.update({
    "FNMUSIC_HOME_DIR": os.environ["_T_INSTALL_HOME"],
    "FNMUSIC_KUGOU_ENABLED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
})
sys.path.insert(0, os.environ["_T_PROXY"])
import app
legacy = Path(os.environ["_T_INSTALL_HOME"]) / ".env"
legacy.parent.mkdir(parents=True, exist_ok=True)
legacy.write_text(os.environ["_T_LEGACY_CONTENT"], encoding="utf-8")
print("@@JSON@@" + json.dumps({
    "has_migrate_fn": hasattr(app, "_migrate_env_to_conf_dir"),
    "has_legacy_attr": hasattr(app, "_LEGACY_ENV_FILE"),
    "resolved": str(app.resolve_env_file()),
    "etc": os.environ["_T_ETC"],
    "legacy_path": str(legacy),
}))
'''
LEGACY_TXT = (
    "FNMUSIC_KUGOU_TOKEN=abc123token\n"
    "FNMUSIC_KUGOU_USERID=1234567890\n"
    "FNMUSIC_KUGOU_DFID=\n"
    "FNMUSIC_SAVE_DIR=/vol1/music/saved\n"
)
_root6, etc6, var6, home6, inst6 = mkpkgroot("fnmusic-migrate-")
env6 = child_env(etc6, var6, home6, inst6)
env6["_T_LEGACY_CONTENT"] = LEGACY_TXT
m, err6 = run_child(IGNORE_CHILD, env6)
if m is None:
    check("分叉守卫子进程执行成功", False, err6)
else:
    check("分叉守卫子进程执行成功", True)
    check("app 已无 _migrate_env_to_conf_dir（迁移函数已删）",
          m["has_migrate_fn"] is False, str(m["has_migrate_fn"]))
    check("app 已无 _LEGACY_ENV_FILE（旧位置概念已删）",
          m["has_legacy_attr"] is False, str(m["has_legacy_attr"]))
    check("resolve_env_file 始终指向配置目录（不因安装目录有 .env 而回退）",
          m["resolved"] == str(Path(m["etc"]) / ".env"), m["resolved"])

print()
print("=" * 70)
print("7) 包内空模板（8 个全空 FNMUSIC_KUGOU_*）依然不会被当成配置")
print("=" * 70)
EMPTY_TXT = "".join(f"FNMUSIC_KUGOU_{k}=\n" for k in
                    ("TOKEN", "USERID", "DFID", "T1", "MID", "GUID", "DEV", "MAC"))
_root7, etc7, var7, home7, inst7 = mkpkgroot("fnmusic-empty-")
env7 = child_env(etc7, var7, home7, inst7)
env7["_T_LEGACY_CONTENT"] = EMPTY_TXT
m7, err7 = run_child(IGNORE_CHILD, env7)
if m7 is None:
    check("空模板子进程执行成功", False, err7)
else:
    check("空模板子进程执行成功", True)
    check("空模板不被读取（resolve 仍指配置目录）",
          m7["resolved"] == str(Path(m7["etc"]) / ".env"), m7["resolved"])
    check("空模板不产生配置文件", not (Path(m7["etc"]) / ".env").exists())

print()
print("=" * 70)
print("8) fav_dir 绝不能落在安装目录（在线收藏会随升级清零）")
print("=" * 70)
FAV_CHILD = r'''
import json, os, sys
os.environ.update({
    "FNMUSIC_HOME_DIR": os.environ["_T_INSTALL_HOME"],
    "FNMUSIC_KUGOU_ENABLED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
})
sys.path.insert(0, os.environ["_T_PROXY"])
import app
print("@@JSON@@" + json.dumps({
    "conf_fav_dir": app.CONF.get("fav_dir", ""),
    "user_fav_path": app.user_fav_path("user-1234567890"),
    "install_home": os.environ["_T_INSTALL_HOME"],
    "marker_dir": app.resolve_marker_dir(),
    "cache_dir": app.CONF.get("cache_dir", ""),
}))
'''
_root8, etc8, var8, home8, inst8 = mkpkgroot("fnmusic-fav-")
(etc8 / ".env").write_text("FNMUSIC_KUGOU_ENABLED=0\n", encoding="utf-8")
f, err8 = run_child(FAV_CHILD, child_env(etc8, var8, home8, inst8))
if f is None:
    check("fav_dir 子进程执行成功", False, err8)
else:
    check("fav_dir 子进程执行成功", True)
    check("fav_dir 默认不含安装目录路径",
          str(inst8) not in f["user_fav_path"], f["user_fav_path"])
    check("fav_dir 默认在持久 home 目录下",
          f["user_fav_path"].startswith(str(home8)), f["user_fav_path"])
    check("标记目录兜底不在安装目录",
          str(inst8) not in f["marker_dir"], f["marker_dir"])
    check("缓存目录在持久 var 目录下",
          str(f["cache_dir"]).startswith(str(var8)), str(f["cache_dir"]))

print()
print("=" * 70)
print("9) 脚本层：TRIM_* 归属校验 + .env 落 ETC_DIR（install/upgrade/main/run_proxy）")
print("=" * 70)
# 静态校验：四个脚本都必须有归属校验（漏一个就是一条把 token 写进别人目录的路）
GUARD = "不属于"
for rel in ("cmd/main", "cmd/install_callback", "cmd/upgrade_callback",
            "app/proxy/run_proxy.sh"):
    src = (PKG / rel).read_text(encoding="utf-8")
    check(f"{rel} 有 TRIM_* 归属校验", GUARD in src)

inst_src = (PKG / "cmd" / "install_callback").read_text(encoding="utf-8")
check("install_callback 的 .env 落 ETC_DIR", 'ENV_FILE="$ETC_DIR/.env"' in inst_src)
# 【1.0.0 分叉】以下两条由"迁移存在"反转为"迁移已删除"的回归守卫。
# 依据：本分支以上游 2.1.0 为基线，无旧位置迁移义务。
inst_code = "\n".join(
    ln for ln in inst_src.splitlines() if not ln.lstrip().startswith("#")
)
check("install_callback 无旧位置迁移源（分叉已删）",
      'LEGACY_ENV_FILE="$APP_DIR/.env"' not in inst_code)
check("install_callback 无 legacy_env_has_data（分叉已删）",
      "legacy_env_has_data" not in inst_code)

up_src = (PKG / "cmd" / "upgrade_callback").read_text(encoding="utf-8")
up_code = "\n".join(
    ln for ln in up_src.splitlines() if not ln.lstrip().startswith("#")
)
check("upgrade_callback 无 .env 迁移（分叉已删）", 'NEW_ENV="$ETC_DIR/.env"' not in up_code)
check("upgrade_callback 无在线收藏迁移（分叉已删）", "online_favorites" not in up_code)

app_src = (PROXY / "app.py").read_text(encoding="utf-8")
check("app.py 无 .env 启动期迁移（分叉已删）",
      "_migrate_env_to_conf_dir" not in app_src)
check("app.py 无安装目录旧 .env 回退（分叉已删）",
      "_LEGACY_ENV_FILE" not in app_src)

rp_src = (PKG / "app/proxy/run_proxy.sh").read_text(encoding="utf-8")
check("run_proxy.sh 优先读配置目录的 .env", 'ENV_FILE="${ETC_DIR}/.env"' in rp_src)
check("run_proxy.sh 缓存不在安装目录", 'CACHE_DIR="${BASE_DIR}/cache"' not in rp_src)
check("run_proxy.sh 不 source .env（防 root 注入）", "\n    . \"" not in rp_src)
check("run_proxy.sh 导出持久目录别名",
      "FNMUSIC_ETC_DIR" in rp_src and "FNMUSIC_VAR_DIR" in rp_src)

main_src = (PKG / "cmd" / "main").read_text(encoding="utf-8")
check("cmd/main 解析持久目录并 export",
      "resolve_pkg_dir" in main_src and "export TRIM_PKGETC" in main_src)

print()
print("=" * 70)
print("10) 白名单完整性：.env 里写的 key 必须能回读")
print("=" * 70)
app_src = (PROXY / "app.py").read_text(encoding="utf-8")
# install_callback 写进 .env 的键
written = set(re.findall(r'echo "(FNMUSIC_[A-Z0-9_]+)=', inst_src))
# app.py 回读白名单里的键
block = app_src[app_src.index("_KUGOU_KEY_MAP = {"):]
block = block[:block.index("}")]
readable = set(re.findall(r'"(FNMUSIC_[A-Z0-9_]+)":', block))
# 这几项不需要回读：路径别名（run_proxy.sh 用，不经 app.py）
NO_READBACK = {"FNMUSIC_ETC_DIR", "FNMUSIC_VAR_DIR", "FNMUSIC_PKG_HOME_DIR"}
missing = sorted(written - readable - NO_READBACK)
check("install_callback 写的键都能回读（无'写得进读不回'）", not missing, str(missing))

# 反向：白名单里的键也必须是脚本会写的（防止拼错 key 名）
unknown = sorted(readable - written)
check("白名单没有拼错的键", not unknown, str(unknown))

print()
print("=" * 70)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)

sys.exit(1 if FAIL else 0)
