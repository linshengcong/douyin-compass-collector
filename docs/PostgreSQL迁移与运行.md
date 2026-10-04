# PostgreSQL 本地运行与 RDS 接入

当前只支持 PostgreSQL，保留 SQLAlchemy 和 Alembic。第一阶段仍为单机采集，原始响应、Manifest、CSV、Profile 留在本地；不引入后台 API、多节点调度或历史数据导入。

## 本地启动

安装 Docker 后执行 `docker compose -f compose.postgres.yaml up -d`。本轮已启动的专用容器名为 `compass-collector-postgres`，可用 `docker start compass-collector-postgres` 恢复；它运行时不要重复启动 Compose 占用同一端口。新环境使用上述 Compose。服务仅绑定 `127.0.0.1:55432`，初始化 `compass`、`taobao`、`taobao_poc` 三个独立数据库；命名卷保存数据库数据。初始化 SQL 只在空数据目录首次运行。

执行 `uv sync --group test`，将 `.env.example` 中三个数据库连接变量加入项目 `.env`，保留已有通知与 OSS 配置。已有环境变量优先于 `.env`。示例密码仅用于本机开发，不用于 RDS。

```bash
make status PLATFORM=tt
make status PLATFORM=tb
make run PLATFORM=tt MODE=dry-run GUI=no NOTIFY=no
```

YAML 中 `database.url_env` 和 `platforms.<id>.database_env` 只保存变量名。运行前解析连接，缺失变量或非 PostgreSQL URL 直接拒绝；不回退 SQLite。两个平台可以共享服务实例，必须使用不同数据库。环境变量别名指向同一数据库时，`runtime_platform` 归属检查拒绝第二个平台。

## 结构与迁移

当前关系保持批次 → 分类执行 → raw 索引 / 正式排名 → 店铺快照。配置、分类路径和安全参数仍保存 JSON；数值区间使用 NUMERIC(24,4)。数据库时间保持北京时间无时区墙上时间，带时区输入先转换为北京时间。

新库从 `migrations_postgresql/versions/pg0001_initial.py` 建立固定基线；仓库仅保留当前 PostgreSQL 迁移链。未来结构变化新增 PostgreSQL 修订，不修改已发布基线，也不通过运行时 `create_all` 升级。

应用入口按既有行为初始化数据库并执行 Alembic 升级，因此连接账号需要迁移所需 DDL 权限。数据库迁移和平台认领在同一事务中，使用 PostgreSQL advisory lock 串行化并发初始化。独立执行 Alembic 时设置 `DATABASE_URL`，再执行 `uv run alembic upgrade head`；单独迁移建表后，平台归属由第一次运行入口认领。

## 数据清理边界

新库不导入旧 SQLite 记录。旧配置中的 `database.path` / `database_path` 不再有效，使用当前仓库 YAML 的数据库连接变量字段。历史数据清理必须按平台、业务日期和批次核对数据库与本地材料；保留已发布结果时，一并保留对应 raw、Manifest、CSV 与网站快照。一次性整理不改变自动保留策略。

`clear-data --platform ... --yes` 删除配置的 PostgreSQL 数据库中当前平台批次及检查点，级联清除子记录；仅删除这些批次登记的本地产物。旧 SQLite、未登记文件、归档、日志和 Profile 保留。该命令是数据库数据删除命令，连接 RDS 后同样会影响远端数据；GUI 确认文案明确这一点。

## RDS 连接与备份

RDS 中为抖音、淘宝分别建立数据库，将三个环境变量替换为相应 `postgresql+psycopg://...` DSN，账号密码需 URL 编码。SSL 参数随 DSN 保留，使用服务端提供的 CA 和 `sslmode=verify-full&sslrootcert=...` 验证连接。云服务器与 RDS 优先使用同地域 VPC，配置对应白名单。

迁移前做 RDS 备份或 `pg_dump --format=custom`，恢复使用 `pg_restore`。凭证通过安全环境或 PostgreSQL 支持的密码文件提供，不放命令行或仓库。raw、CSV、Manifest 单独备份；数据库备份不能包含这些文件。切换机器时需同步产物目录并保持路径可访问，本阶段未解决跨机器共享文件。

本地数据库与 CSV 仍采用协调发布和补偿，不是跨系统原子事务。出现 `publication_unconfirmed` 时保留 CSV 和原批次状态，暂停再次运行该任务，待连接恢复后核对该 batch 的 `published_at`、版本与 CSV；不自动重试或改写未知终态。数据库断网时停止当前操作，不自动重放写入；提交后终态以数据库查询为准。连接池预检查仅检测陈旧连接，不能保证事务中途不断网。多节点采集的任务互斥与调度选主另行设计，当前运行仍使用单机平台/Profile 文件锁。

## 自动化验收

测试需显式设置独立 `TEST_DATABASE_URL`，数据库名称必须以 `_test` 结尾。测试自动创建随机 schema，结束后只清理本进程创建的 schema，不使用开发或 RDS 生产数据。

```bash
TEST_DATABASE_URL='postgresql+psycopg://collector:collector_local@127.0.0.1:55432/collector_test' \
QT_QPA_PLATFORM=offscreen uv run pytest
```

本地 Compose 首次初始化同时创建 `collector_test`；其他环境需提前创建独立测试库。未配置连接时，真实数据库测试明确跳过，不能据此声称 PostgreSQL 集成通过。真实浏览器登录、分页和 RDS TLS 联通需分别验收。
