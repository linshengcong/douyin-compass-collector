# 抖音电商罗盘榜单采集器

这是一个本地 Mac 工程：使用独立 Chrome Profile 保存人工登录态，Playwright 读取白名单内认证状态，`httpx` 请求抖音电商罗盘商品榜单。所有模式都会保留 SQLite、Manifest 和 gzip 原始响应审计；正式模式额外发布商品数据与 CSV。

当前业务范围为分类接口动态返回的所有一级分类及其全部三级分类：排除“全部”，忽略四级及更深节点。最多两个一级分类并行，单个三级分类的分页最多四线程预取；全局最多八个在途 HTTP 请求，所有请求启动共用随机 0.01～0.03 秒间隔，不实现任何自动重试。

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

当前不包含自动重试、云主机部署、页面点击采集或其他榜单类型。

## 快捷命令

```bash
make help              # 查看全部命令
make install           # 安装锁定依赖
make login             # 人工登录
make login                         # 打开独立 Chrome，人工登录
make app                           # 打开空闲 GUI 控制台
make run                           # GUI 正式采集
make run MODE=dry-run              # GUI 试运行
make run MODE=force                # GUI 强制创建新版本
make run GUI=no                    # 终端正式采集
make run MODE=dry-run GUI=no       # 终端试运行
make notify-test                   # 真实发送一条钉钉测试消息
make clear-data                    # 清除采集数据，保留 Chrome 登录态
make status                        # 查看最近运行状态
make scheduler                     # 前台启动 Scheduler
make test                          # 运行全部自动化测试
make check                         # 测试与 LaunchAgent 无副作用检查
make service ACTION=install        # 安装 LaunchAgent
make service ACTION=status         # 查看 LaunchAgent 状态
make service ACTION=uninstall      # 卸载 LaunchAgent
```

默认任务为 `product_hot_sale_all_level3`，可按需覆盖：

```bash
make run TASK=another_task_id
```

下面仍保留完整 CLI，方便理解每个快捷命令实际执行的内容。

## 2. 环境要求

- macOS；
- Google Chrome 正式版；
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

主配置位于 `config/tasks.yaml`。可以调整任务启停、每日执行时刻、动态分类范围、筛选条件和保留天数。

当前 cron 只支持每日固定时间：

```yaml
tasks:
  - id: product_hot_sale_all_level3
    schedule: "0 14 * * *"
    category_scope:
      mode: all_level1
      target_level: 3
      exclude_all: true
    filters:
      brand_type: 0
      price_bin: "不限"
      search_info: ""
```

Scheduler 使用 `Asia/Shanghai`。`misfire_grace_minutes: 600` 表示当天计划时间之后最多延迟 10 小时补采；跨天只记录 `missed`，不会用第二天实时榜单冒充前一天数据。

配置中的认证部分只能维护 Cookie 名称白名单。Cookie 值、Token、Webhook 和其他凭证不得写入 YAML、源码、日志或 Git。

当前已验证的筛选值为：`brand_type=-1` 表示不限、`brand_type=0` 表示非知名品牌；`price_bin=不限` 表示不限价格、`price_bin=10001-?` 表示价格严格大于 10000 且没有上限。新批次会在 SQLite 和 Manifest 中保存这两个实际请求值，配置变化不会改写历史批次。

每次任务只请求一次分类树，按接口原始顺序遍历所有非汇总一级分类，并枚举其三级分类；ID 为 `0` 或名称为“全部”的节点会排除，四级及更深节点不会进入采集队列。批次不制造“全部行业”根 ID，每个分类运行单独保存真实一级分类。榜单请求的 `industry_id` 使用当前三级分类所属的一级分类 ID，`category_id` 按级联选择器格式拼接二级分类 ID 与三级分类 ID。每个三级分类固定 `page_size=10`，按照第一页 `total` 完整请求全部页，不设置条数上限。

网络、HTTP 或响应契约类的普通分类失败会留下分类级失败材料，并跳过该分类继续后续采集。只要至少一个分类成功，正式模式就发布其余成功分类并标记 `partial_success`；只有全部分类都失败时才以 `failed` 收口且不发布。登录失效、中止、数据库、Manifest 或其他内部错误不适用该容错规则，会直接收口任务。

### 钉钉批次汇总

从示例创建本机配置：

```bash
cp .env.example .env
```

将已轮换的机器人配置写入仓库根目录 `.env`，并显式启用：

```dotenv
DINGTALK_ENABLED=true
DINGTALK_WEBHOOK_URL=<钉钉自定义机器人 Webhook>
DINGTALK_SECRET=<加签密钥>
```

`.env` 已加入 `.gitignore`，程序启动时主动读取，同名系统环境变量优先。Webhook 只允许官方 `https://oapi.dingtalk.com/robot/send` 地址且必须只有一个 `access_token`。

首次配置后执行：

```bash
make notify-test
```

`run`、`--force`、`--dry-run` 和 Scheduler 都会在每个批次结束时发送一条 Markdown 汇总。消息不 @ 任何人，只包含来源、模式、批次 ID、耗时、任务状态、页数/条数、CSV 文件名和安全错误分类。不发送本机绝对路径、原始响应或异常原文。

钉钉请求只尝试一次，不跟随重定向，不自动重试。发送失败只记入 JSONL 并在 GUI 显示，不会改变采集退出码、SQLite、CSV 或下一计划批次。只有 `make notify-test` 会在测试发送失败时返回非零状态。

### OSS 私有 CSV 下载链接

默认 `OSS_ENABLED=false`，不访问 OSS。启用后，只有正式 `success` 或 `partial_success` 的 CSV 在 SQLite 与本地文件都已发布成功后才上传；上传完成会生成最长 7 天有效的私有签名下载链接，并将中文 CSV 文件名作为钉钉 Markdown 链接发送。

```dotenv
OSS_ENABLED=true
OSS_REGION=cn-hangzhou
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET=<目标 Bucket 名称>
OSS_ACCESS_KEY_ID=<专用 RAM 用户 AccessKey ID>
OSS_ACCESS_KEY_SECRET=<专用 RAM 用户 AccessKey Secret>
OSS_OBJECT_PREFIX=compass
OSS_DOWNLOAD_URL_EXPIRES_SECONDS=604800
```

RAM 用户至少需要该 Bucket 的 `oss:PutObject` 和 `oss:GetObject`。对象保持私有；签名链接本身是短期 bearer 凭证，只在本次进程中进入钉钉正文，不写入 JSONL、SQLite、Manifest 或日志。OSS 上传失败不会回滚正式 CSV 或改变采集结果，钉钉结果列会标记 `OSS 上传失败`；第一版不自动重试。

### 公开榜单网站与 Vercel

网站源码位于 `web/`，使用 React + Vite 读取 OSS 上的最新公开榜单。每次正式发布后，程序先发送采集完成钉钉汇总，再生成只含 CSV 七列的 gzip JSON，上传到 `WEB_PUBLIC_PREFIX`（默认 `compass/web/`）：版本化 `batches/<batch_id>.json.gz`、同批次公开 CSV，以及不缓存的 `latest.json` 索引。网页始终先读取索引，因此刷新即可看到新批次，无需把数据提交 Git。

网页对象前缀需要在 OSS 控制台单独配置为公开读，并允许 Vercel 网站域名对该前缀发起 `GET` 跨域请求；不要公开 `runtime/`、原始响应或私有 CSV 前缀。Vercel 项目连接当前 GitHub 仓库后，将 Root Directory 设置为 `web`，并设置构建环境变量：

```text
VITE_DATA_INDEX_URL=https://<bucket>.oss-<region>.aliyuncs.com/compass/web/latest.json
```

本机 `.env` 需要额外配置：

```text
WEB_ENABLED=true
WEB_PUBLIC_PREFIX=compass/web
VERCEL_ENABLED=true
VERCEL_DEPLOY_HOOK_URL=<Vercel Deploy Hook>
VERCEL_API_TOKEN=<只读部署查询 Token>
VERCEL_PROJECT_ID=<Vercel Project ID>
VERCEL_TEAM_ID=<可选，团队项目才填写>
VERCEL_SITE_URL=https://<project>.vercel.app
```

网页数据上传失败不会回滚 CSV 或采集结果：首条钉钉仍说明采集完成，第二条说明网页未更新。上传成功后会触发 Vercel Deploy Hook，并最多轮询五分钟；部署 `READY` 才发送第二条网站链接通知，失败或超时会发送安全错误分类。部署 Hook、API Token 和钉钉密钥都只能保存在 `.env`。

本地前端开发使用 `make web-dev`，打开 `http://127.0.0.1:5175/`；保存前端文件后由 Vite 自动热更新。静态构建使用 `make web-build`。如需替换公开数据源，可用 `make web-dev WEB_DATA_INDEX_URL=https://.../latest.json` 覆盖默认值。当前界面按桌面优先设计，移动端自动改为商品卡片；支持三级类目、商品/店铺关键词、支付金额/成交件数下限、首次上榜、排序、分页和 CSV 下载。

## 4. 首次登录

```bash
uv run --frozen python -m compass_collector login
```

在打开的独立 Chrome 中完成登录。检查完成后回到终端按 Enter，程序会正常关闭 Chrome。该 Profile 位于 `runtime/browser-profile/`，不要与日常 Chrome Profile 混用。

## 5. GUI 手动运行

日常调试推荐直接打开空闲控制台：

```bash
make app
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

`make run` 默认打开 GUI 并正式采集；`MODE=dry-run` 或 `MODE=force` 切换模式，`GUI=no` 显式回退终端。`force` 开始前仍会二次确认。

GUI 关闭时不会留下自己启动的 Scheduler 或 Chrome。运行中的采集会先确认，再等待当前 HTTP 请求完成或超时后协作式中止。

## 6. 终端手动运行

显式添加 `--no-gui` 可回退终端模式。正式采集会保留完整审计，并发布正式商品记录和 CSV：

```bash
uv run --frozen python -m compass_collector run \
  --task product_hot_sale_all_level3 \
  --no-gui
```

同一计划时间已有成功版本时默认跳过。强制创建新版本：

```bash
uv run --frozen python -m compass_collector run \
  --task product_hot_sale_all_level3 \
  --force \
  --no-gui
```

试运行同样请求分类树并采集全部动态三级分类，保留 `collection_batches`、`category_runs`、`raw_responses`、Manifest 和 gzip 原始响应；它不写正式商品记录或 CSV，也不分配版本，`published_at` 始终为空：

```bash
uv run --frozen python -m compass_collector run \
  --task product_hot_sale_all_level3 \
  --dry-run \
  --no-gui
```

手动 `run` 成功或失败后都会保留 Chrome，检查完成后按 Enter 关闭。

查看最近状态：

```bash
uv run --frozen python -m compass_collector status
```

## 7. 开发期清除本地数据

GUI 底部的“清除本地采集数据”会先显示不可恢复确认。按钮只在当前采集、保留的 Chrome 和所有 Scheduler 都停止时可用。

终端调试可执行：

```bash
make clear-data
```

或使用带显式确认参数的完整命令：

```bash
uv run --frozen python -m compass_collector clear-data --yes
```

会删除 SQLite 主库及 sidecar、CSV、原始响应、失败材料和 JSONL 日志。会保留 `runtime/browser-profile/`、`runtime/locks/`、`.env`、`config/`、备份和 runtime 中其他未知文件。删除目标被严格限制在当前工程 `runtime/` 内；如果数据库配置到该边界之外，清理会在删除任何文件前拒绝执行。

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

正式 CSV 路径为 `runtime/exports/<YYYY-MM-DD>/<task_id>/<中文文件名>.csv`。即使两个任务使用相同展示名和计划时间，也不会互相覆盖；通知仍只展示中文文件名。

CSV 固定为 7 列：`分类、排名、商品、店铺名称、用户支付金额、成交件数、首次上榜`。先按分类接口发现顺序输出，再按分类内排名输出；只包含成功完成的三级分类。

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
5. 从 `.env.example` 创建 `.env`，填入当前有效凭证并执行 `make notify-test`；
6. 执行 `make app`，检查单窗口、最近日志、通知和 Scheduler 状态；
7. 执行一次 GUI `dry-run`，核对动态三级分类数量、完整分页、SQLite/raw 审计和批次汇总；
8. 执行 GUI 正式 `run`，核对 `published_at`、中文 7 列 CSV、打开文件和关闭 Chrome；
9. 前台启动 Scheduler 并用 Ctrl-C 停止；
10. 先执行 launchd `--dry-run`；
11. 获得明确授权后再安装 LaunchAgent；
12. 验证登录后启动、状态查询和卸载。

## 14. 后续 TODO

- 云主机和 systemd；
- 重试策略与 Scheduler 逻辑后续重新梳理；
- 以 SQLite 权威状态重建 Manifest 和 raw 索引的崩溃恢复；
- 继续裁剪分类接口的 `level/scene/default_cate_to_level` 业务参数；
- 多账号、多主机和基于真实限流证据的更高分类级并发；
- 其他榜单 Adapter。
