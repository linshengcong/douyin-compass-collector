# 抖音电商罗盘榜单采集器

这是一个本地 macOS/Windows 工程：使用独立 Chrome Profile 保存人工登录态，通过真实 Chrome 页面点击、滚动和响应监听采集商品榜单。榜单采集不使用 Cookie 重放 HTTP 接口。淘宝可在独立 Profile 内额外备份会话 Cookie，用于浏览器重启时恢复；通知和部署查询仍使用各自的网络客户端。

默认任务为个护家清实时榜，只循环 `industry_id=5` 下的全部三级分类，页面选择实时榜（`date_type=1`），品牌不限（`brand_type=-1`）、price_bin=不限。支持指定分类列表及 `all_level1` 自动发现；设置 `category_scope.industry_id` 时限定行业，未设置时发现所有行业。排除“全部”，忽略四级及更深节点。任务、分类和分页全部串行执行，页面操作间隔为 0.5～1 秒，超时后有限恢复重试；平台连续三次返回 `11001` 时安全停止本批。

> 当前代码已接通动态分类发现、完整分页、正式发布、Scheduler、PySide6 GUI 和钉钉汇总。仓库自动化测试不等于真实账号、真实 Webhook、LaunchAgent 或第二台 Mac 的外部验收；这些操作仍需人工执行。

## 1. 当前能力

- 手动登录和登录态持久化；
- 手动正式采集、`--dry-run`、`--force`；
- SQLite + Alembic、CSV、原始响应和 Manifest；
- JSONL 日志、失败截图和脱敏诊断材料；
- APScheduler 北京时间定时运行、同日宽限补采、跨天 `missed`；
- PySide6 本地控制台、实时进度、安全日志和 Chrome 生命周期控制；
- GUI 启动/停止 Scheduler、打开 CSV 和输出目录；
- 钉钉签名 Webhook 批次汇总，GUI 展示发送状态；
- 用户级 `launchd` 安装、卸载和状态脚本。

当前已注册罗盘和淘宝页面适配器。淘宝可通过独立配置从 CLI、GUI 运行；分类/排行解析、存储迁移、CSV、调度接入和网站平台切换已实现。淘宝两个目标分类已通过真实账号完整采集与发布验收，全根任务仍在进行，默认调度任务暂不启用。

## 快捷命令

仅保留11个入口：help、install、start、run、login、status、clean、schedule、notify、web、check。除 `make`、`make help`、`make install`、`make start` 外必须指定 `PLATFORM=tt|tb`；tt为抖音，tb为淘宝。缺少平台、未知平台和无效动作在执行前拒绝。

```bash
make                              # 帮助
make help                         # 帮助
make install                      # 安装锁定依赖
make start                        # 同时启动两平台独立GUI，立即强制采集并通知
make run PLATFORM=tt              # 抖音：GUI立即强制采集，结束后通知
make run PLATFORM=tb              # 淘宝：GUI立即强制采集，结束后通知
make login PLATFORM=tb            # 与采集相同Profile登录
make status PLATFORM=tb           # 只查看淘宝批次
make clean PLATFORM=tb ACTION=data   # 仅清理淘宝本地数据
make clean PLATFORM=tb ACTION=login  # 仅清理淘宝登录态
make schedule PLATFORM=tb         # 前台调度，Ctrl-C停止
make schedule PLATFORM=tb ACTION=check     # 服务配置无副作用检查
make schedule PLATFORM=tb ACTION=install   # 安装并启动所选平台服务
make schedule PLATFORM=tb ACTION=status    # 查看所选平台服务
make schedule PLATFORM=tb ACTION=uninstall # 卸载所选平台服务
make notify PLATFORM=tb           # 真实发送淘宝钉钉测试消息
make web PLATFORM=tb              # 本地网站开发，首屏淘宝
make web PLATFORM=tb ACTION=build # 构建首屏为淘宝的网站
make check PLATFORM=tb            # 后端/前端测试及所选平台服务检查
make check PLATFORM=tb ACTION=test # 仅完整后端测试
```

`run` 默认 `GUI=yes START=yes MODE=force NOTIFY=yes`。可选参数保持明确语义：

```bash
make run PLATFORM=tb START=no       # 打开GUI，等待手动开始
make run PLATFORM=tb GUI=no         # 终端立即采集
make run PLATFORM=tb MODE=normal    # 已有正式发布就跳过，否则新批次完整重采
make run PLATFORM=tb MODE=dry-run NOTIFY=no # 试采，不发布正式商品/CSV，不通知
make start START=no NOTIFY=no       # 同时打开两个GUI，等待手动开始
```

`normal` 不是补录，已发布的部分成功结果也会跳过；分类级补录见TODO。`force` 每次创建新批次，新排名属于新版本，旧版本保留。`START=no` 只允许与 `GUI=yes` 一起使用。显式模式在GUI中锁定，避免普通采集被改成强制采集。

`start` 并行执行两个独立的 `run`，各自使用 `TT_CONFIG`、`TB_CONFIG` 和平台默认任务，公共运行参数同时传给两边。终端等待两个窗口各自结束；关闭或启动失败一个平台不会停止另一个，两边结束后返回失败状态（如有）。

运行中关闭GUI只需确认一次：本窗口的采集、Chrome及自有Scheduler收尾后自动退出，另一平台继续运行。正在执行的请求会在完成或超时后响应中止；退出保留登录Profile，不发布本次未完成数据。

登录、采集、状态、清理及调度共用所选平台配置。tt默认 `config/tasks.yaml`；tb在本机验收配置存在时使用 `runtime/acceptance/taobao/2026-10-01/full-root-config.yaml`，保持已登录Profile和数据库，否则使用可分发的 `config/taobao.yaml` 全量配置。可通过 `CONFIG=...` 覆盖，但平台及TASK归属仍校验。运行解释器统一为 `.venv/bin/python`，可通过 `PYTHON=...` 覆盖。

`NOTIFY=yes|no` 控制本次采集/调度/通知测试，不修改.env；Webhooks及加签密钥继续从本机.env读取。后台服务仅存布尔开关，不保存凭证。网页分别使用 `TT_WEB_DATA_INDEX_URL`、`TB_WEB_DATA_INDEX_URL`，可显式覆盖公开索引；公开站未设置初始平台时仍默认抖音。

`clean ACTION=data` 删除所选平台的数据库批次及级联分类/商品/店铺/raw索引，并清理对应的本地raw、CSV、失败材料和网页暂存。数据库文件、另一平台数据、共享JSONL、Profile、配置、凭证和备份保留；CLI清理必须显式提供 --platform。清理需要同时获得本平台采集和Scheduler锁，本次调整不会实际清理已有数据。

`schedule` 默认前台调度，只处理所选平台配置中启用的任务；人工 `run` 的force默认不改变Scheduler的幂等规则。服务标签分别为 `com.zhuanz1.douyin-compass-collector.compass` 和 `com.zhuanz1.douyin-compass-collector.taobao`。运行锁已按平台拆分，同一runtime可同时运行两平台的GUI、采集和Scheduler；同平台仍只允许一个实例。不自动迁移或卸载旧服务。`check` 默认运行完整后端和前端测试，再渲染所选平台服务配置，不安装服务。

两平台可以在两个终端同时启动：

```bash
make run PLATFORM=tb
make run PLATFORM=tt
```

每个平台内部仍串行采集。GUI、采集和调度分别使用 `runtime/locks/<platform>/` 下的锁；日志和控制文件分别写入 `runtime/logs/<platform>/`、`runtime/controls/<platform>/`。停止或关闭一个窗口不操作另一个平台。Chrome保留检查期仍持有本平台采集锁。

多平台配置必须在 `platforms.<id>.database_path` 声明独立数据库，单平台旧配置可继续使用顶层 `database.path`。规范化后的数据库或Profile路径重复会拒绝启动；数据库通过 `runtime_platform` 元数据登记唯一归属，跨平台复用及混合历史数据库均拒绝，不自动拆分或清理。迁移只在升级前备份，原数据库、CSV和登录态路径保留。

升级前关闭旧版本GUI、采集和Scheduler。检测到旧全局锁仍被占用时，新版本提示 `legacy_gui`、`legacy_collection` 或 `legacy_scheduler` 并拒绝启动，不终止旧进程。旧CLI按明确平台、任务所属平台或唯一启用平台解析；多平台歧义需要 `--platform`，清理始终必填该参数。

实际回归、并行试采、发布通知结果及GUI验收限制见[双平台独立运行验收](docs/双平台独立运行验收.md)。

下面仍保留完整CLI，方便排查具体执行链路。

## 2. 环境要求

- macOS 或 Windows 10/11；
- 安装在系统标准位置的 Google Chrome 正式版（应用不内置 Chrome，不能用 Edge 或 Chromium 代替）；
- Python 3.12；
- [uv](https://docs.astral.sh/uv/)；
- 可访问抖音电商罗盘的账号。

进入工程并安装锁定依赖：

```bash
cd /Users/Zhuanz1/Documents/douyin-compass-collector
uv sync --frozen
```

检查 CLI：

```bash
uv run --frozen python -m compass_collector --help
```

## 3. 配置

主配置位于 `config/tasks.yaml`，GUI 只读展示平台、Profile、任务范围及执行状态，不提供配置编辑器。未知平台、缺失主任务、重复任务 ID、重复分类组合和无效配置在启动 Chrome 前报错；行业归属错误或不存在的分类，在当次分类发现阶段使整个任务失败。

- `browser`：共享 Chrome 参数；
- `platforms.<id>.database_path`：多平台独立数据库；
- `platforms.<id>.profile_dir`：每个平台独立 Profile；罗盘复用 `runtime/browser-profile`；
- `collection`：页面动作超时、响应超时、采集间隔、有限重试及人工等待；
- `tasks`：任务 ID、平台、启停、名称、每日时间、分类、筛选与日期；
- `publication.web_primary_task_id`：唯一可更新现有网站根索引的主任务；
- 数据库、保留策略和调度仍在各自配置节管理。

指定分类按配置顺序执行，名称从本次平台分类树解析：

```yaml
category_scope:
  mode: selected
  targets:
    - industry_id: "5"
      category_id: "1000004647,1000004649"
```

默认的个护家清自动发现模式：

```yaml
category_scope:
  mode: all_level1
  industry_id: "5"
```

自动发现模式不填写 `targets`，可用 `industry_id` 限定行业；删除该字段才遍历所有行业。指定列表的行业仍填写在各 target 中。罗盘三级类目的请求组合为二级分类 ID 与三级分类 ID，平台逻辑负责这种映射，只选择到三级。支持 `brand_type=-1`（不限）、`0`（非知名品牌），价格支持 `不限` 与 `10001-?`（严格大于一万元）；自定义价格通过真实页面对话框设置，无法确认条件时任务报错，不降级为不限。

每次任务保存分类树快照，完整请求全部页。手动任务遇到登录或验证可人工恢复，默认 180 秒；定时任务结束并进入通知链路。恢复会重建分类和筛选条件，回到尚未持久化页；已完成页不重复入库。任务冻结北京时间业务日期，跨天、停止或恢复后响应契约不一致时安全结束。

当前 cron 只支持每日固定时间，例如 `0 14 * * *`；调度保留同日宽限与跨天不补实时榜单规则。

## 4. 首次登录

```bash
uv run --frozen python -m compass_collector login
```

在打开的独立 Chrome 中完成登录。检查完成后回到终端按 Enter，程序会正常关闭 Chrome。该 Profile 位于 `runtime/browser-profile/`，不要与日常 Chrome Profile 混用。

## 5. GUI 手动运行

日常调试推荐直接打开空闲控制台：

```bash
make run PLATFORM=tt START=no
```

控制台支持：

- 选择正式采集或试运行；
- 实时查看分类总数、分类路径、分类序号、分页进度和脱敏日志；
- 协作式中止采集；
- 成功、失败或中止后保留 Chrome，检查完成后由按钮关闭；
- 启动和优雅停止 GUI 自己创建的 Scheduler；
- 只读识别终端或 launchd 启动的外部 Scheduler；
- 打开本次或最近已发布 CSV，以及 `runtime/exports/`。
- 查看当前或最近批次的钉钉发送状态。
- 在采集和 Scheduler 均停止时清除本地采集数据。

`make run PLATFORM=tt` 默认打开GUI立即强制采集并通知；`MODE=normal`、`MODE=dry-run`显式切换模式，`GUI=no`回退终端，`START=no`打开空闲GUI。

GUI 关闭时不会留下自己启动的 Scheduler 或 Chrome。运行中的采集会先确认，再在页面动作和响应等待的短检查点协作式中止。

## 6. 终端手动运行

显式添加 `--no-gui` 可回退终端模式。正式采集会保留完整审计，并发布正式商品记录和 CSV：

```bash
uv run --frozen python -m compass_collector run \
  --task compass_household_cleaning_realtime \
  --no-gui
```

同一计划时间已有成功版本时默认跳过。强制创建新版本：

```bash
uv run --frozen python -m compass_collector run \
  --task compass_household_cleaning_realtime \
  --force \
  --no-gui
```

试运行同样保存分类树快照并采集配置范围内的动态分类，保留 `collection_batches`、`category_runs`、`raw_responses`、Manifest 和 gzip 原始响应；它不写正式商品记录或 CSV，也不分配版本，`published_at` 始终为空：

```bash
uv run --frozen python -m compass_collector run \
  --task compass_household_cleaning_realtime \
  --dry-run \
  --no-gui
```

手动 `run` 成功或失败后都会保留 Chrome，检查完成后按 Enter 关闭。

查看最近状态：

```bash
uv run --frozen python -m compass_collector status
```

## 7. 开发期清除本地数据

GUI 底部的“清除本地采集数据”会先显示不可恢复确认。按钮只在本平台采集、保留的 Chrome 和 Scheduler 都停止时可用。

终端调试可执行：

```bash
make clean PLATFORM=tt ACTION=data
```

或使用带显式确认参数的完整命令：

```bash
uv run --frozen python -m compass_collector clear-data --platform compass --yes
```

会删除所选平台的数据库批次及级联数据、CSV、原始响应、失败材料和网页暂存；保留数据库文件、平台归属元数据、另一平台数据及日志。也会保留 `runtime/browser-profile/`、`runtime/locks/`、`.env`、`config/`、备份和 runtime 中其他未知文件。删除目标被严格限制在当前工程 `runtime/` 内；如果数据库配置到该边界之外，清理会在删除任何文件前拒绝执行。

## 8. 前台 Scheduler

```bash
uv run --frozen python -m compass_collector scheduler
```

Scheduler 在前台常驻，按 Ctrl-C 正常停止。它不会等待 Enter；每个批次结束后自动关闭本次 Chrome。

GUI 中也可以启动 Scheduler。GUI 只允许停止自己创建的子进程；发现终端或 launchd Scheduler 时只显示“外部 Scheduler 运行中”，不会终止它。停止 GUI Scheduler 时，正在执行的批次会完成后再退出；“中止本次采集”是单独的二次确认操作。

Scheduler 到期时若 Chrome 正被登录或手动采集占用，本次任务记录为 `skipped_busy`，不排队、不重试。

同一计划时间已有非 dry-run 的 `success`、`partial_success`、`failed`、`auth_required`、`missed` 等终态时，Scheduler 都不会自动重试。dry-run 终态不占用正式计划；失败后只能等待下一次计划执行，或由人工运行命令补跑。

## 9. launchd 守护

仓库只提供脚本，不会在安装依赖或测试时自动注册系统服务。

先执行无副作用校验：

```bash
./scripts/install_launchd.sh --dry-run
```

明确需要后台守护后，由当前 Mac 用户主动安装：

```bash
./scripts/install_launchd.sh
```

查看状态：

```bash
./scripts/status_launchd.sh
```

卸载并停止：

```bash
./scripts/uninstall_launchd.sh
```

LaunchAgent 标识为 `com.zhuanz1.douyin-compass-collector`，安装位置为 `~/Library/LaunchAgents/`。它登录后启动 Scheduler，并只在 Scheduler 异常退出时拉起。启动参数使用 uv 的绝对路径、工程绝对路径和 `--frozen`，不会在后台更新依赖。

launchd 标准输出和错误输出写入 `/dev/null`；业务运行状态统一查看 `runtime/logs/YYYY-MM-DD.jsonl`。如果服务反复退出，先执行状态脚本查看最后退出状态，再在终端手动运行 Scheduler 获取安全错误摘要。

## 10. 运行产物

```text
runtime/
├── browser-profile/    # 登录凭证，敏感
├── data/collector.db   # SQLite 审计与正式数据
├── exports/            # CSV，按日期/task_id 隔离并长期保留
├── raw/                # gzip 原始响应，默认 30 天
├── logs/               # JSONL，默认 10 天
├── locks/              # GUI、Scheduler、采集 advisory lock 元数据
└── artifacts/          # 失败截图和诊断材料，默认 10 天
```

`runtime/` 已整体加入 `.gitignore`。完整响应只保存在本机 `runtime/raw/`，仓库中的 Fixture 只是脱敏契约样本。

正式 CSV 路径为 `runtime/exports/<platform>/<YYYY-MM-DD>/<task_id>/<中文文件名>.csv`。即使两个任务使用相同展示名和计划时间，也不会互相覆盖；通知仍只展示中文文件名。

CSV 固定为 8 列：`分类、排名、商品缩略图、商品、店铺名称、用户支付金额、成交件数、首次上榜`。先按分类接口发现顺序输出，再按分类内排名输出；只包含成功完成的三级分类。

GUI 日志直接消费同一份安全事件；JSONL 仍是唯一持久日志。启动 GUI 时只恢复最近采集批次的最后 500 条事件。

## 11. 安全边界

- 不把 Cookie 值、Token、Webhook、签名密钥或完整请求头写入仓库；
- 不输出完整接口 URL 或认证异常原文；
- HTTP 失败正文最多保存 1 MiB，只进入本机失败材料；
- Chrome Profile 包含登录凭证，不提交、不普通复制、不通过聊天传输；
- `launchd` plist 不包含业务凭证；
- GUI 不展示响应正文、Cookie、Token、请求头或原始异常文本；
- 钉钉凭证只存放在已忽略的本机 `.env` 或更高优先级的系统环境变量中。

## 12. 备份、恢复与故障处理

- [备份与恢复](docs/备份与恢复.md)
- [故障处理](docs/故障处理.md)
- [完整工程方案](docs/工程方案.md)

## 13. 新 Mac 交付检查清单

本仓库当前没有在第二台 Mac 上实际验收。迁移时应逐项执行：

1. 安装 Chrome、Python 3.12 和 uv；
2. 克隆仓库并运行 `uv sync --frozen`；
3. 检查 `config/tasks.yaml`；
4. 执行 `login` 并人工登录；
5. 从 `.env.example` 创建 `.env`，填入当前有效凭证并执行 `make notify PLATFORM=tt`；
6. 执行 `make run PLATFORM=tt START=no`，检查单窗口、最近日志、通知和 Scheduler 状态；
7. 执行一次 GUI `dry-run`，核对动态三级分类数量、完整分页、SQLite/raw 审计和批次汇总；
8. 执行 GUI 正式 `run`，核对 `published_at`、中文 8 列 CSV、打开文件和关闭 Chrome；
9. 前台启动 Scheduler 并用 Ctrl-C 停止；
10. 先执行 launchd `--dry-run`；
11. 获得明确授权后再安装 LaunchAgent；
12. 验证登录后启动、状态查询和卸载。

## 14. 后续 TODO

- 云主机和 systemd；
- 重试策略与 Scheduler 逻辑后续重新梳理；
- [ ] 分类级补录：针对失败或缺失分类，从第一页完整重采，按本次接口排名生成新版本；保留原版本及分类来源、采集时间，不按商品或缺失页插入旧榜单。版本合并与发布规则、排名变化验收待设计；
- 以 SQLite 权威状态重建 Manifest 和 raw 索引的崩溃恢复；
- 以更多平台的页面和字段契约验证适配器扩展；
- 多主机独立运行与监控；
- 其他榜单 Adapter。

当前 `normal` 模式仍为：同一任务、同一计划时间已有正式发布结果（含部分成功）则跳过；否则创建新批次完整重采。分类级补录尚未实现，不作为 `normal` 的现有行为。

## 浏览器改造与历史兼容

调用链为 CLI / GUI / Scheduler → 任务配置及幂等锁 → 平台适配器 → 分类发现与校验 → 页面响应采集 → 分页审计与完整性校验 → SQLite / CSV 协调发布 → OSS、网站及通知。

`platforms/contracts.py` 规定 `open_session`、`discover_scopes`、`collect_scope`、`close`。共享编排不持有页面对象、不解析罗盘响应和请求参数；罗盘导航、选择器、日期匹配、错误码及原始指标换算集中在适配器。共享金额为实际人民币元 `CNY`、件数为实际件 `count`，导出器只负责展示。

Alembic `0005_platform_capture` 增加平台标识、任务配置快照、通用分类路径、平台元数据和安全请求参数，迁移旧金额/件数单位。升级已有数据库前使用 SQLite backup 保存完整已提交状态，包括 WAL。旧记录归属罗盘，无法还原的请求范围标记为未知，不套用新配置。历史 raw、CSV、Manifest 保持原路径可读；新 raw、artifacts、exports 按平台和任务隔离，保留策略同时兼容旧日期目录和新平台目录。

网站不可变快照与 `latest.json` 位于 `<public_prefix>/<platform>/<task_id>/`。只有配置主任务更新兼容根 `<public_prefix>/latest.json`，其他任务不覆盖当前网页。现有网页字段和展示保持兼容。

真实 Chrome 的可控页面回归单独运行：`RUN_BROWSER_TESTS=1 uv run --frozen python -m pytest tests/test_compass_browser.py`。普通自动化测试、可控浏览器测试和真实平台验收分别记录，见 `docs/浏览器改造验收.md`。

## 淘宝接入与网站平台切换（开发中）

四阶段范围、验收条件和剩余真实验证见 `docs/淘宝平台开发执行方案.md`。阶段记录放在 `runtime/acceptance/taobao/`；受控测试中的合成数据不代表真实账号采集结果。

淘宝采集编排已实现响应归属、20条保持、有限恢复、跨午夜和共享 raw → SQLite → Manifest → CSV 链路；完整链路测试覆盖空榜、单页、多页、部分成功与全部失败。同类淘宝业务错误连续三个分类发生时停止批次，网络错误或成功分类会重置计数。平台工厂已接入 `TaobaoBrowserControls`，CLI、GUI 可使用独立 PoC 配置启动。2026-10-01 已完成香薰蜡烛与香薰精油两分类各15页、合计600条的真实 dry-run 和正式本地数据库/CSV验收；全根245分类的完整终态仍待验收。无法匹配控件时明确失败并保存本地截图。默认 `config/tasks.yaml` 仍仅运行抖音。

淘宝独立 Profile 登录和两个目标分类的手动试采：

淘宝全量采集统一使用 `make run PLATFORM=tb`，登录使用 `make login PLATFORM=tb`，状态使用 `make status PLATFORM=tb`。本机优先使用已认证的验收配置；其他机器使用 `config/taobao.yaml` 并在该独立Profile重新登录。两分类PoC可用 `make run PLATFORM=tb CONFIG=config/taobao-poc.yaml MODE=dry-run NOTIFY=no`，此时范围和Profile都由PoC配置决定。

主配置 `config/tasks.yaml` 中的淘宝任务仍为 `enabled: false`，不自动加入抖音调度。独立全量配置声明根 `50025705`、实时、20条和全部三级分类；通过显式tb入口启动，新增入口不代表全根真实验收已通过。

淘宝平台配置启用 `webdriver_compatibility: true`，在第一次导航及子 frame 页面脚本之前覆盖 `Navigator.prototype.webdriver` 的 getter，与已成功人工登录的工作树方式一致，不新增 `navigator` 实例属性。抖音默认关闭此兼容方式。该设置不保证其他自动化信号不可见，也不迁移另一工作树的登录态；相同相对 Profile 路径在不同工作树中实际是两个目录。

淘宝同时开启 `persist_session_cookies: true`：首次导航前恢复原生 Profile 未恢复的会话 Cookie，正常关闭前原子更新 `<Profile>/.session-cookies.json`，退出登录后也更新为空状态。该文件包含明文登录凭证，POSIX 权限为 `0600`，与已忽略的 Profile 一起只留在本机，不进入 raw、数据库、CSV、通知或公开发布；Windows 用户应继续使用当前账号私有的 Profile 目录。持久 Cookie、Local Storage 仍由 Chrome 自身保存，抖音默认不额外备份。需要在开启后正常登录并由程序关闭一次才能建立备份；意外强制结束不保证保存最后状态，网站使登录态失效后仍需人工登录。本地合成登录的跨重启/退出检查，以及2026-10-01同一真实淘宝 Profile 的连续重启和两个目标分类 dry-run 已通过；不保证网站登录态永久有效。

页面导航结束后，先在动作超时预算内等待排行榜控件；人工登录落在商家首页时，等待可见“市场”入口后返回排行榜一次。仅在页面仍需登录时进入人工等待，避免页面尚未加载就提示认证失效。真实分类菜单采用 `.item-cate` 与 `.common-picker-menu` 三列结构：悬停一级、二级名称，再点击三级名称；不使用 Ant Cascader 选择器。

真实单页榜（例如香氛贴11条）不显示活动页码；控制器仅在接口确认总数不超过20、且请求是第一页时允许页码控件缺失，同时仍确认20条模式。多页榜继续逐页确认活动页码与实际请求身份。

多页榜收到匹配响应后，如果活动页码确认失败，会丢弃该次响应，在 `collection.network_retry_attempts` 限制内重新进入当前分类并翻到尚未保存的页；已经保存的页不会重复写入。恢复后仍需确认分类、20条模式、请求页码和分类总数，超过恢复次数则终止并保留浏览器供检查。

`login` 命令保持窗口直到在终端按 Enter，正常关闭时保存登录态。`run` 的窗口在任务结束或失败后按 `browser.keep_open_after_manual_run` 控制是否保留；手动 PoC 默认 `true`，检查结束后按 Enter 或通过 GUI 关闭。该选项不用于无人值守 Scheduler；自动验收配置可显式设为 `false`，有限诊断脚本也会在取证完成后释放浏览器。淘宝长分类名会在菜单文本中缩写，控制器在对应第三列使用完整 `title` 精确匹配，并拒绝多个同名目标。

分类点击返回超时不一定表示点击未生效。适配器只在已识别的分类点击超时后继续等待同一动作代次的完整匹配响应；响应通过分类、日期、页码、20条和可见页面状态校验后才接受。没有匹配响应时保留原步骤、异常类型及截图，按配置次数重建页面，达到上限后停止。不会在原页面盲目重复点击。淘宝分类内重复商品不算失败，按原排名位置保留，不去重；条数、完整分页和排名连续性仍严格校验。罗盘继续保持原商品唯一性要求。

```bash
uv run --frozen python -m compass_collector login --config config/taobao-poc.yaml --platform taobao
DINGTALK_ENABLED=false uv run --frozen python -m compass_collector run --config config/taobao-poc.yaml --task taobao_household_cleaning_realtime --no-gui --dry-run
```

PoC 配置的数据库为 `runtime/data/taobao-poc.db`，登录目录为 `runtime/taobao-browser-profile`，不复用内置浏览器的登录。首次需在程序打开的 Chrome 中完成正常登录。`--dry-run` 用于采集验收，不发布正式商品数据；命令中的 `DINGTALK_ENABLED=false` 仅为本次进程禁用汇总通知，不修改 `.env`；不要用 PoC 配置启动 Scheduler。两目标分类必须由当次真实分类树解析成功，控件与完整请求参数必须同时通过验证。

网站默认抖音，桌面和移动均可切换淘宝。切换重置关键词、分类、数值条件、排序、分页及移动展开。淘宝展示支付买家数、访客数原始区间和商品接口提供的跳转链接；筛选和排序按人数下界计算，未知指标保持空值，排序时置后。淘宝没有首次上榜字段，因此隐藏该筛选与标记。接口商品、店铺名称中的 HTML 实体仅在前端解码一次供文本展示和搜索；原始响应、数据库、CSV 和公开快照保留来源原文。

原 `VITE_DATA_INDEX_URL` 继续指向抖音公开索引。淘宝使用 `VITE_TAOBAO_DATA_INDEX_URL`，例如 `<公开前缀>/taobao/taobao_household_cleaning_realtime/latest.json`。本地可配置在仓库根 `.env`，GitHub Pages 构建读取同名 Repository variables；不能填写 Cookie、token 或私有签名下载链接。未配置淘宝地址时界面显示提示，仍可切回抖音。

新公开快照版本为 3，包含平台、任务、批次、采集开始/结束时间；前端继续兼容抖音版本 1、2，历史缺图片使用占位。数据库 `0006_taobao_metrics` 在保留抖音历史值的基础上增加淘宝独立指标与链接。已有数据库升级前继续备份；有淘宝扩展数据时，迁移拒绝直接降级，避免静默丢失字段。

验证命令：

```bash
.venv/bin/python -m pytest -q
npm ci --prefix web
npm test --prefix web
npm run build --prefix web
```

前端状态测试使用 Node 自带测试运行器、现有 TypeScript 编译器及 jsdom，不启动用户浏览器。它覆盖加载/错误/空榜、未配置地址、平台切换重置和晚到请求；布局、滚动实际效果与线上部署需另外验收。

本地网页验收可使用 `scripts/taobao_web_preview.py`。脚本只写入 runtime 独立目录并绑定 `127.0.0.1`，不读取 Profile、完整原始响应或 OSS 凭据。默认生成合成数据：构建时将两平台 VITE 索引分别指向 `http://127.0.0.1:5175/data/compass/compass_preview/latest.json` 和 `http://127.0.0.1:5175/data/taobao/taobao_preview/latest.json`，复制 `web/dist/` 到脚本的 `--root`，再运行 `PYTHONPATH=src .venv/bin/python scripts/taobao_web_preview.py --serve`。合成产物不能用来更新正式公开网站。

对照真实已发布CSV时，成对传入 `--compass-manifest <抖音Manifest路径>` 与 `--taobao-manifest <淘宝Manifest路径>`，并指定独立 `--root`。脚本要求每个平台各一个成功或部分成功的正式批次，读取Manifest引用的CSV，保留实际批次、任务、分类计数和采集窗口；localhost索引中的任务ID也改为实际任务ID，构建配置须对应调整。该模式仍只生成本地预览，不上传OSS。2026-10-01 的真实600条淘宝、5000条抖音桌面/移动浏览器验收已通过；同一淘宝批次已实际发布到自己的 OSS 索引，[线上网站](https://linshengcong.github.io/) 的桌面/移动切换、实际图片、跳转链接、指标和采集窗口已验证。证据在本地 runtime 验收目录，具体批次范围与全根验收分别记录。

空榜验收添加 `--taobao-state empty`，加载失败验收添加 `--taobao-state error`（仅本地淘宝索引返回503）。未配置验收使用明确为空的 `VITE_TAOBAO_DATA_INDEX_URL` 构建。为各场景指定独立 `--root` 保存构建和截图；验收结束后恢复标准构建，不把localhost地址带入发布产物。
