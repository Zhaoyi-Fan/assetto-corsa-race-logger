# assetto-corsa-race-logger（中文说明）

**"神装 AC 版 Warcraft Logs"：全场录制（CSP Lua 游戏内插件）+ 离线分析工具链，把每一场
比赛变成一份自包含、可分享、可交互的 HTML 报告——事故检测、原因归因、圈速分析、可拖动
2D 回放。English: [README.md](README.md)**

---

## 项目目的

离线锦标赛（对 AI）有一个"赛后复盘"难题：比赛出了乱子，游戏不给任何答案——T1 到底谁碰了
谁？那台 AI 为什么连续三圈在同一个弯打转？你的冲出是路肩弹跳、冷胎、脏气流，还是纯粹自己
失误？回放看完就没了，遥测软件只盯着玩家，另外 15 台车没人记录。

MMO 团本圈多年前就用 Warcraft Logs 解决了这个问题：全程记录、离线分析、一个链接分享。
本项目把同样的思路搬进 Assetto Corsa：

1. **永远在录**。游戏内轻量插件持续采样**每一台车**的完整物理状态（离线 AI 跑的是完整
   本地物理，数据和玩家一样全），外加所有离散事件——碰撞、过线、进出站、复位、旗语、
   起步反应时间；V1.4 起还记录每台车的混动能量状态（MGU-K 功率、电量、每圈部署/回收、
   直道模式与超车模式的使用）。
2. **离线分析**。Python 流水线检测失控（打转/大滑移/推头冲出/甩尾冲出/困住），跨车合并成
   "事故 episode"，为每一起构建带证据的归因链。
3. **一个文件分享**。产物是零外部依赖的单个 HTML——丢进 Discord，任何人打开就能拖回放、
   看归因。界面中英双语（右上角切换）。

为 VRC Formula Alpha 2025 离线联赛而生（阈值也在该联赛真实比赛上标定），适用于任何车与
赛道内容。

## 在线示例

**[▶ 打开一份真实比赛报告](https://zhaoyi-fan.github.io/assetto-corsa-race-logger/examples/spa-race.report.html)** —
斯帕 15 车 5 圈联赛（Formula Alpha 2025）：全场静止发车的起步反应时间、8 次车对车接触、
7 张事故卡、3 个 DNF。回放、圈速图、归因——全部在这一个自包含 HTML 里，就是工具的原始产物。

生成它的原始 log 在 [`examples/vrclog_spa_race_example.zip`](examples/vrclog_spa_race_example.zip)
（解压后 19 MB）。自己复现：

```
unzip examples/vrclog_spa_race_example.zip -d examples
python analyzer/vrclog_report.py examples/vrclog_20260803_231630_spa-layout_f1_2025_race_r13.txt
```

（复现需要本机装有 spa `layout_f1_2025` 赛道 mod——分析器要读它的 AI line；只看示例报告
则什么都不需要。）

**[▶ 打开赛季面板](https://zhaoyi-fan.github.io/assetto-corsa-race-logger/examples/season/)** —
7 场联赛、7 条赛道（蒙特利尔、摩纳哥、斯帕、匈格罗林、银石、英特拉格斯、赞德沃特），由
`season_report.py` 聚合：赛历、车手事故榜、跨场失控热点弯。赛历每一行都能打开那场的完整
报告，每份报告的标题栏都有返回面板的按钮（`--season-link`）；整套文件在
[`examples/season/`](examples/season/)。

## 组成

| 组件 | 作用 |
|---|---|
| `app/vrc_race_logger` | CSP Lua 插件：15 Hz 每车物理流、1 Hz 状态流、天气流、事件流，以及（V1.4）只在变化时写的 10 Hz 能量流 + 部署/回收/直道模式/超车模式事件 + 每圈能量小结。分块写盘防崩溃。只读——不改物理不改内容文件，联赛安全。16 车约 2 MB/分钟。 |
| `analyzer/vrclog_report.py` | 一条命令：log → `*.report.html`（约 3.5 MB，约 1 秒生成）。 |
| `analyzer/season_report.py` | 整个 logs 目录聚合成赛季面板：赛历、车手事故榜、跨场热点弯、每赛道 AI vs 玩家的能量部署。 |
| `docs/log_format_spec.md` | 完整日志格式契约（schema 1 与 schema 2），逐字段说明。 |

能量数据有两层来源：每台车都通过 CSP 暴露电量、部署输入和输出档位；物理脚本发布了 CAN
通道表的车还能给出 MGU-K 功率 kW、每圈 MJ 计数、功率上限、部署策略和直道模式锁存。第一
个适配的是 VRC Formula Alpha 2026 Pro（通道按名字在运行时解析，不写死索引）；AI 车暴露的
数据和玩家完全一样，所以一份比赛日志现在能看出 AI 的部署策略到底在做什么。

### 报告内容

* **总览** — 结果、最快圈、起步反应时间（绿灯→首次位移，最快加亮、抢跑标红）、进站表
  （圈号/静止时长/换胎）、位置变化图、失控热点弯。
* **圈速** — 每人圈速走势（进出站圈与超限圈特殊标记）、对头名累计差（按圈）、净圈统计
  （均值、σ），图例点击显隐。
* **驾驶分析** — MoTeC 式距离域圈对比，参考圈三档可选（全场最快/自己最快/任意车手任意圈）：
  速度 + 油门/刹车曲线（弯角色带背景）、累计 Δt 曲线（看时间丢在**哪里**）、逐弯表格
  （刹车点、入弯/apex 速度、回油门点、弯中耗时、跨圈刹点一致性 σ）、驾驶风格指标（滑行、
  全油门、trail braking、滑移超限占比）、G-G 图、全圈微分段热图。点击任意图表直接跳到
  回放对应时刻。**点击弯角表任意行**展开整场风格面板（v1.4）：你 vs 全场中位数，汇总
  全部干净通过——刹车起点/峰值/松刹点、油门承诺点与中断次数、后轮打滑与侧滑时长、
  路肩使用、逐车手明细、速度/踏板/滑移中位曲线，并把该弯生效中的 `ai_hints` 区间直接
  画在图上。同一引擎也可命令行独立使用（`analyzer/corner_style.py`）：喂两个 log 就能
  逐弯以米为单位量出一次 AI hint 修改的效果。
* **时间轴** — 每车手一条泳道：事故点（点击跳回放）、进站块、DNF 标记、黄旗带、底部
  抓地/降雨条。
* **事故卡片** — 每起 episode 一张卡：严重度、每车叙事链（`与 X 接触 → 打转 → 冲出赛道 →
  困住 12s → 退赛`）、按置信度排序的证据、速度/刹车 spark 图、一键跳转回放。
* **能量**（v1.6，需要 logger V1.4+ 的日志）— 每车部署表（每圈部署/回收 MJ、过线电量、
  最低电量、整场电量漂移、峰值 kW、直道模式次数与秒数、超车模式次数、功率受限时长、
  Vmax、策略档），以 AI 中位数作基线；沿圈功率剖面图（选中车手 vs AI 合并的按位置 kW
  中位数，电量走右轴），底下叠该布局自己的区文件——直道模式区、超车模式检测线/起点、
  功率削减区、功率重置区、备用功率曲线区；每圈能量表；能量事件时间轴（每车的部署/回收/
  翼片打开/超车模式，点击跳回放）。logger 1.4 的日志里，某圈总量比上一圈低时，这一圈的
  部署/回收 MJ 会重复上一圈的值（1.4.1 已修）；分析器用能量流重建这些圈，旧日志一样准确。
* **回放** — 2D 俯视图（真实赛道彩带）、全车朝向+拖尾、跟车视角、实时排位含差距、每轮
  遥测（ndSlip、路面、输入、β）+ 实时滚动输入曲线、进度条下方全程油门/刹车带、圈刻度
  进度条、1–32× 播放。滚轮/双指缩放、拖动平移、支持触屏。有能量数据时（v1.6）：遥测
  面板加 MGU-K 功率条、电量条和直道模式/超车模式/受限标签，滚动曲线加 kW 线，实时排位
  加每车电量，彩带叠电量线和翼片打开标记，地图上部署/回收的车带蓝/绿光晕、直道模式翼片
  打开带黄圈、超车模式带紫圈。

### 归因证据（自动生成，带置信度）

`contact`（追尾/并排接触）、`wall`（撞墙，区分诱因/后果）、`kerb`（路肩+垂直 G 尖峰）、
`offline`（严重偏线）、`dirty_air`（长时间贴前车过弯）、`cold_tyres`/`worn_tyres`/
`flat_spot`（胎况）、`prior_incident`（60 秒内有前科）、`avoidance`（黄旗下避让事故现场）、
`rain`/`grip`（环境）、`ai_line`（同弯 3+ 起独立 AI 失控 → AI 走线嫌疑）、`no_external`
（无外因——极限失误）。卡死 AI 被游戏回收进站的退赛会按其事件签名识别为"卡死回收退赛"。

## 快速开始

### 1. 装录制端（游戏内插件）

需要 Assetto Corsa + 较新的 [Custom Shaders Patch](https://acstuff.club/patch/)（支持 Lua app）。

1. 把 `app/vrc_race_logger` 复制到 `<AC 根目录>\apps\lua\`。
2. 游戏内从 app 任务栏添加 **VRC Race Logger**（Lua apps 分类）。
3. 完成——默认自动录 race 和 qualify（practice 可选开），窗口开不开都在录。窗口里能看
   状态、采样计数，还有手动 *Finalize & save now* 按钮。

日志写到 `<AC 根目录>\logs\vrclog_<时间戳>_<赛道>_<session>.txt`。比赛中数据流式写进
`.parts` 目录、结束时合并；游戏崩溃的话下次启动自动抢救。超过 256 MB 的 session 保留为
`.parts` 目录（分析器直接支持）。

### 2. 装分析端

```
pip install numpy        # 唯一依赖（Python 3.10+）
```

### 3. 生成报告

```
python analyzer/vrclog_report.py "<AC 根目录>\logs\vrclog_20260709_..._race.txt"
```

报告输出在 log 旁边（`<log>.report.html`），现代浏览器直接打开（Chrome 80+ / Firefox 113+ /
Safari 16.4+，回放二进制用 zlib 压缩、浏览器端 `DecompressionStream` 解压）。

参数：`--out 输出路径`、`--ac-root AC 根目录`（默认读 `AC_ROOT` 环境变量，否则自动探测
Steam 路径）、`--ai fast_lane.ai 路径`、`--corners 弯名 json`、`--season-link 赛季页链接`
（报告页头加返回赛季面板的按钮）。

### 4. 赛季面板（可选）

```
python analyzer/season_report.py "<AC 根目录>\logs" --out season.html
```

接受目录/通配符/单个 log/`.parts`；默认跳过 5 分钟以下的残段（`--min-minutes` 可调）；
赛历行自动链接到旁边的单场报告——单场报告先用 `--season-link season.html` 生成，两边就能
互相跳转。

### 5. 多份日志的能量部署对比（A/B 实验）

```
python analyzer/energy_compare.py logA.txt logB.txt logC.txt --labels A,B,C --out compare.md --html compare.html
```

把每份日志里所有有 CAN 数据的 AI 车的所有圈合并，输出一张 markdown 表：每圈部署/回收 MJ、
过线与最低 SoC、峰值 kW、部署请求被功率上限压到 0 的比例、每圈部署/直道模式/受限秒数、
Vmax、圈速、STRAT 与 PU 模式——各臂并排并给出相对第一份日志的差值；再按该布局自己的
区文件逐区列出 AI 在圈内哪里放电；加 `--html` 输出一页叠加各臂 AI kW / SoC 剖面的图。
专为"AI 为什么不部署"的实验准备：改一个 AI 设置、跑同一场、对比。不需要赛道模型。

## 新赛道：弯名配置

首次在未知赛道运行会从 AI 线曲率自动检测弯段、按 T1..Tn 编号，并生成可编辑骨架
`analyzer/corners/<track>-<layout>.json`。把 `n`/`name` 改成官方编号/弯名再重跑，报告里的
所有标签就会升级（完整范例见 `corners/spa-layout_f1_2025.json`；随库自带 21 条已整理的
赛道）。弯名只影响标签美观——检测/归因/回放不依赖它。

## 阈值调校

所有检测阈值集中在 `analyzer/thresholds.py`（带注释）：滑移角界限、出界驻留时长、接触
分组窗口、证据窗口、严重度门槛。出厂值在真实 16 车联赛上标定；换车种误报就改这里重跑，
一秒钟出新报告。

## 开发与测试

```
python analyzer/tests/test_pipeline.py
```

端到端合成测试：程序生成的体育场赛道（v7 `fast_lane.ai` 二进制）+ 剧本化 log（追尾→打转→
冲出→困住→回收退赛、倒挡诱饵、撞墙、黄旗、缺 tick 车、零数据车），先按 schema 1 断言解析
对齐、检测、归因与报告渲染，再把同一场比赛写成带剧本化能量遥测的 schema 2 日志（一台 CAN
车、一台只有原生字段的车、直道模式/超车/功率区）核对能量分析、剖面和能量标签，外加逐帧
重现 logger 1.4 每圈计数串到下一圈的夹具、核对分析器的修复，共 97 项。

技术细节：报告=模板+JSON+base64(zlib(回放二进制))，无 CDN 无跟踪，`file://` 可开；回放流
40 字节/样本/车 @15 Hz（含 MGU-K kW、电量和能量模式标志位）；所有来自 log 的字符串（车手/车/赛道名）入 DOM 前经 HTML 转义——
分享出去的报告会在别人浏览器里渲染，mod 内容不可作为可信标记。日志与报告内含你 session
里出现的车手名，分享时留意。

## 已知边界

* 阈值按 race 标定；排位/练习 log 可用（CLI 会提示、结果按最快圈排序、race 语义证据关闭），
  出场圈可能过触发。
* CSP 的圈有效性标志在离线 race session 不可靠，已刻意忽略。
* 0% 机械损伤的联赛没有损伤通道证据，用 `prior_incident` 启发式补偿。
* 回放排位榜的差距是距离/速度估算，非官方计时。

## 许可

MIT — 见 [LICENSE](LICENSE)。
