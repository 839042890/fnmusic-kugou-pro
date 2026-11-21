# AGENTS.md — fnmusic_kugou_pro

给在本仓库工作的 AI/开发者看的约定。**先读这一页，再动代码。**

## 一、这是什么

fnOS（飞牛 OS）音乐 App 的第三方插件：把酷狗/官方源聚合成 App 能消费的接口。

```
app/proxy/app.py     主程序（单文件，约 9.8k 行）——FastAPI，同时挂内部桥接协议
manifest.json        插件清单（版本号在这里）
tests/*.py           25 个自写测试脚本（不是 pytest）
web/                 设置页等前端资源
```

## 二、测试怎么跑（没有 pytest，别装）

```sh
PY=/vol1/@appcenter/fnmusic_kugou_pro/.venv-proxy/bin/python3.12
cd <repo>
for t in tests/test_*.py; do $PY "$t"; done
```

- 每个脚本自己打印结果，多数用中文「结果：N 通过 / M 失败」
- 退出码 0 = 通过；非 0 = 有失败项
- **基线（2026-11-22，HEAD 8395542）**：22 个脚本通过；3 个失败且都是环境依赖：
  - `test_package_hygiene` 需要已构建的 `.fpk`
  - `test_persist_dirs` 有 1 项需要 `app.tgz`（构建产物）
  - `test_pipeline` 的 e2e「第二次播放读本地归档」在当前环境失败（历史遗留）
- **交付前必须给出与基线同样的数字口径**，不得新增失败

## 三、不许动的地方

```
web/                 前端资源（除非任务明确要求）
manifest.json 版本号  只在发布时改，且要跟 CHANGELOG 同步
tests/ 既有断言       不许为了"跑绿"而放宽
.secrets / .env       不存在于本仓库，别去找
```

## 四、分支与提交

```
main             只接受验收通过的合并
codex/<主题>     每个任务一个分支，改完留在分支上等人审
提交信息          <版本或主题>: <一句话>（中文可）
不要 push        除非任务明确要求发版
```

## 五、已知的架构要点（改之前先看）

```
搜索链路   防抖(FNMUSIC_SEARCH_DEBOUNCE_S，默认 0.8) + 世代号 + 分级 TTL
           并发闸 _SEARCH_INFLIGHT 的 gate_key 必须含 tag，否则 track 会拿到
           playlist 的响应（1.0.9 修的就是这个）
缓存       _META_CACHE key = meta:{tag}:{keyword}（三个 meta tab 互相隔离）
           _SEARCH_CACHE 只给 track 用
播放       guid=online:kugou:... 时先查 marker（边听边存）→ 命中读本地
           未命中才回源；官方 guid 直接读本地文件
存档       /vol1/1000/music/.fnmusic_save_index 下按 guid 存 marker
```

## 六、验收风格（用户要求）

```
· 改动先给方案（改哪些文件/函数、影响什么、怎么验证），批准后再写
· 必须做故障注入（改坏一处 → 复现原 bug → 修好 → 不复现）
· 必须有阴性对照（关掉开关/参数为 0 时行为不回归）
· 报数只认实跑输出，不写"应该""预计"
```

## 七、构建与发布

```
版本号改 manifest.json + CHANGELOG.md
打包走 fpk（应用中心升级包），交付目录 /vol1/1000/下载/fnmusic_kugou_pro-<ver>/
交付物含 MD5SUMS / SHA256SUMS，且要能独立重打出相同的包
不要直接 cp 到运行目录 —— 那会让线上代码与安装记录不一致
```
