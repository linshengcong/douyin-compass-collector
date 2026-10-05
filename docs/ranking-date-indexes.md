# 榜单日期索引

采集发布器在不可变 JSON/CSV 上传成功后更新 `compass/web/{platform}/{task_id}/dates/YYYY-MM-DD.json`，然后更新 `latest.json`。日期取任务 `business_date`，与实际上传时间分开；同日后一次成功发布替换当天入口，其他日期入口和批次文件保持原样。日期索引沿用完整 latest 元信息，不增加前端字段版本，也不让浏览器列举 OSS 对象。

已有本地发布快照可离线重建日期入口：

```sh
PYTHONPATH=src .venv/bin/python scripts/backfill_ranking_dates.py --runtime-root runtime --output-dir /tmp/ranking-date-indexes
```

默认只生成本地文件，不访问 OSS，不修改最新索引和已有选品记录。按平台、任务、日期选择发布时间最新的本地有效快照，并核对其批次、日期和记录数。本机快照可能不包含全部线上历史，生成结果不代表完整历史目录。

审阅后可在配置好现有 OSS/WEB 进程环境的终端追加 `--publish`；工具不自动读取 `.env`。上传前先核验远端 JSON 和 CSV 已存在且属于配置的数据源；已有不同内容的日期入口会停止发布，需人工检查，避免本地旧快照覆盖线上较新版本。工具只更新 dates 入口，不改 latest。生产切换顺序建议为：补日期索引／更新采集器、更新 API、发布 Web。

本次本地盘点找到两个平台 2026-10-03 与 2026-10-04 的四个 v4 批次。是否还有更早远端历史需另行盘点；本次没有重采商品，也没有迁移已归错日期的选品。

## 业务数据库同步

`scripts/sync_ranking_history.py --env-file .env` 从采集 PostgreSQL 读取正式商品记录，只读核对本地发布批次；追加 `--sync` 才发送到业务 API。配置 `RANKING_API_URL` 和仅服务端共享的 `RANKING_SYNC_TOKEN` 后，每次网页发布完成会同步商品与历史关联。配置沿用 `COMPASS_DATABASE_URL` / `TAOBAO_DATABASE_URL`，不会迁移或删除采集库。

同步保留原 `source_record_id`，不改变历史选品、评分或来源日期。业务 API 在一个事务里写入商品、批次和历史记录，相同批次可幂等重试，不同内容拒绝覆盖。同步失败写入 `ranking_sync_failed` 日志，并沿现有发布反馈报告 `web_ranking_sync_failed`，不会误报网站已更新；可用上述命令补同步，不会撤销已完成的 OSS 发布。成功回执在批次目录 `ranking-sync.json`，不含任何密钥。
