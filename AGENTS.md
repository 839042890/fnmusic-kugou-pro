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

## 硬纪律（先看这三条）

1. **只改工单点名的文件。** 工单没列到的文件一律不动；确实需要动，先回问，不要"顺手"改。
2. **不 commit、不 push。** 交付物是工作区的改动 + `git status --short`；提交由主控在验收后做。
3. **测试数字只认自己刚跑出来的原始输出。** 贴命令与输出原文；这个环境里拿不到的数据（真实数据库、线上服务、真机浏览器）标 `TODO`，不许编造。
4. **新增或修改行为的改动，先写失败测试再实现**（按 `tdd` 技能的 red → green 循环）。测试沿用仓库现有 `tests/*.py` 风格：普通 python 脚本 + 断言，直接用解释器跑，**不要引入 pytest 或任何新依赖**。
5. **审批豁免**：工单正文若写明「已批准，直接实现」，则以工单为唯一依据直接开工，不要再单独出一版方案等批准；工单要求你先交方案时（或没写这句时），才走「先出方案 → 等批准」。

## 二、测试怎么跑（没有 pytest，别装）

```sh
PY=/vol1/@appcenter/fnmusic_kugou_pro/.venv-proxy/bin/python3.12
cd <repo>
for t in tests/test_*.py; do $PY "$t"; done
```

- 每个脚本自己打印结果，多数用中文「结果：N 通过 / M 失败」
- 退出码 0 = 通过；非 0 = 有失败项
- **基线（2026-11-22，HEAD 8395542）**：
  - 24 个脚本结果稳定：23 通过 / 1 失败（`test_package_hygiene` 需要已构建的 `.fpk`）
  - `test_persist_dirs` 除 1 项需 `app.tgz`（构建产物）外全通过
  - **`test_pipeline` 结果不稳定，不能当基线**：同一命令重复跑出现 37 通过/6 失败、36 通过/7 失败，
    失败项在自己这次跑里又会 PASS（实测：`生成了同名 .lrc` 时而 PASS 时而 FAIL）。
    判断该脚本只需看「本次失败项是否与你的改动相关」，不要用总数比大小
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

## 已经踩过的坑（别再踩）

- **DOM 的 disabled 对称性**（1.1.1 修的）：`pmUpdateModeUI` 曾用
  `input[type=checkbox]:not(:disabled)` 选元素再设 `disabled = all`。当「全部显示」
  已把真实歌单置为 disabled 后，切到白/黑名单时这些元素选不中 → 永远点不动。
  规则：**进入/退出某个禁用态必须用同一个稳定的选择器**（这里用行类 `.pm-item:not(.disabled)`），
  不要用"当前是否禁用"来筛选要改禁用状态的元素。
  这类 bug 静态读代码看不出来，必须真的点一次（UI 验收不可省）。
