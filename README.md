# 抖音电商罗盘榜单采集器

这是一个本地 Mac 工程：使用独立 Chrome Profile 保存人工登录态，Playwright 读取白名单内认证状态，`httpx` 串行请求抖音电商罗盘商品榜单，完成严格校验后写入 SQLite、CSV 和 gzip 原始响应。

当前只支持已真实验证的“食品饮料 / 水饮冲调 / 商品实时总榜”。`page_size` 固定为 10，默认最多采集 200 条，分页间隔随机 1～2 秒，不实现任何自动重试。

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

仓库根目录提供 `Makefile`，作用类似 npm 项目的 `package.json scripts`：

```bash
make help              # 查看全部命令
make install           # 安装锁定依赖
make login             # 人工登录
make app               # 打开空闲 GUI 控制台
make run               # GUI 正式采集
make dry-run           # GUI 试运行
make force             # GUI 强制发布新版本
make run-cli           # 终端正式采集
make dry-run-cli       # 终端试运行
make force-cli         # 终端强制采集
make notify-test       # 真实发送一条钉钉测试消息
make status            # 查看最近状态
make scheduler         # 前台启动 Scheduler
make test              # 运行测试
make check             # 完整无副作用检查
make launchd-check     # 只检查 launchd，不安装
```

默认任务为 `product_hot_sale_drinks`，可按需覆盖：

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

主配置位于 `config/tasks.yaml`。可以调整任务启停、每日执行时刻、筛选条件、最大条数和保留天数。

当前 cron 只支持每日固定时间：

```yaml
tasks:
  - id: product_hot_sale_drinks
    schedule: "0 14 * * *"
```

Scheduler 使用 `Asia/Shanghai`。`misfire_grace_minutes: 600` 表示当天计划时间之后最多延迟 10 小时补采；跨天只记录 `missed`，不会用第二天实时榜单冒充前一天数据。

配置中的认证部分只能维护 Cookie 名称白名单。Cookie 值、Token、Webhook 和其他凭证不得写入 YAML、源码、日志或 Git。

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
- 实时查看阶段、分页进度和脱敏日志；
- 协作式中止采集；
- 成功、失败或中止后保留 Chrome，检查完成后由按钮关闭；
- 启动和优雅停止 GUI 自己创建的 Scheduler；
- 只读识别终端或 launchd 启动的外部 Scheduler；
- 打开本次或最近成功 CSV，以及 `runtime/exports/`。
- 查看当前或最近批次的钉钉发送状态。

`make run`、`make dry-run` 和 `make force` 会打开 GUI 并立即执行对应任务。`force` 开始前仍会二次确认。

GUI 关闭时不会留下自己启动的 Scheduler 或 Chrome。运行中的采集会先确认，再等待当前 HTTP 请求完成或超时后协作式中止。

## 6. 终端手动运行

显式添加 `--no-gui` 可回退终端模式。正式采集并发布 SQLite 和 CSV：

```bash
uv run --frozen python -m compass_collector run \
  --task product_hot_sale_drinks \
  --no-gui
```

同一计划时间已有成功版本时默认跳过。强制创建新版本：

```bash
uv run --frozen python -m compass_collector run \
  --task product_hot_sale_drinks \
  --force \
  --no-gui
```

只采集和校验，不写 SQLite、CSV：

```bash
uv run --frozen python -m compass_collector run \
  --task product_hot_sale_drinks \
  --dry-run \
  --no-gui
```

手动 `run` 成功或失败后都会保留 Chrome，检查完成后按 Enter 关闭。

查看最近状态：

```bash
uv run --frozen python -m compass_collector status
```

## 7. 前台 Scheduler

```bash
uv run --frozen python -m compass_collector scheduler
```

Scheduler 在前台常驻，按 Ctrl-C 正常停止。它不会等待 Enter；每个批次结束后自动关闭本次 Chrome。

GUI 中也可以启动 Scheduler。GUI 只允许停止自己创建的子进程；发现终端或 launchd Scheduler 时只显示“外部 Scheduler 运行中”，不会终止它。停止 GUI Scheduler 时，正在执行的批次会完成后再退出；“中止本次采集”是单独的二次确认操作。

Scheduler 到期时若 Chrome 正被登录或手动采集占用，本次任务记录为 `skipped_busy`，不排队、不重试。

同一计划时间已有 `success`、`failed`、`auth_required` 或 `missed` 等任意终态时，Scheduler 都不会自动重试。失败后只能等待下一次计划执行，或由人工运行命令补跑。

## 8. launchd 守护

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

## 9. 运行产物

```text
runtime/
├── browser-profile/    # 登录凭证，敏感
├── data/collector.db   # SQLite 正式数据
├── exports/            # CSV，长期保留
├── raw/                # gzip 原始响应，默认 30 天
├── logs/               # JSONL，默认 10 天
├── locks/              # GUI、Scheduler、采集 advisory lock 元数据
└── artifacts/          # 失败截图和诊断材料，默认 10 天
```

`runtime/` 已整体加入 `.gitignore`。完整响应只保存在本机 `runtime/raw/`，仓库中的 Fixture 只是脱敏契约样本。

GUI 日志直接消费同一份安全事件；JSONL 仍是唯一持久日志。启动 GUI 时只恢复最近采集批次的最后 500 条事件。

## 10. 安全边界

- 不把 Cookie 值、Token、Webhook、签名密钥或完整请求头写入仓库；
- 不输出完整接口 URL 或认证异常原文；
- HTTP 失败正文最多保存 1 MiB，只进入本机失败材料；
- Chrome Profile 包含登录凭证，不提交、不普通复制、不通过聊天传输；
- `launchd` plist 不包含业务凭证；
- GUI 不展示响应正文、Cookie、Token、请求头或原始异常文本；
- 钉钉凭证只存放在已忽略的本机 `.env` 或更高优先级的系统环境变量中。

## 11. 备份、恢复与故障处理

- [备份与恢复](docs/备份与恢复.md)
- [故障处理](docs/故障处理.md)
- [完整工程方案](docs/工程方案.md)

## 12. 新 Mac 交付检查清单

本仓库当前没有在第二台 Mac 上实际验收。迁移时应逐项执行：

1. 安装 Chrome、Python 3.12 和 uv；
2. 克隆仓库并运行 `uv sync --frozen`；
3. 检查 `config/tasks.yaml`；
4. 执行 `login` 并人工登录；
5. 从 `.env.example` 创建 `.env`，填入当前有效凭证并执行 `make notify-test`；
6. 执行 `make app`，检查单窗口、最近日志、通知和 Scheduler 状态；
7. 执行一次 GUI `dry-run`，核对 200 条、JSONL 和批次汇总；
8. 执行 GUI 正式 `run`，核对 SQLite、CSV、打开文件和关闭 Chrome；
9. 前台启动 Scheduler 并用 Ctrl-C 停止；
10. 先执行 launchd `--dry-run`；
11. 获得明确授权后再安装 LaunchAgent；
12. 验证登录后启动、状态查询和卸载。

## 13. 后续 TODO

- 云主机和 systemd；
- 页级或批次重试；
- 多账号、多主机和并发；
- 其他榜单 Adapter。
