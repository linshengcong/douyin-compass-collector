# 默认任务 ID 可在命令行通过 TASK=... 覆盖。
TASK ?= product_hot_sale_drinks
# uv 命令可在不同安装环境中通过 UV=/absolute/path/uv 覆盖。
UV ?= uv
# 所有运行命令使用锁定依赖，避免后台或调试时隐式更新环境。
PYTHON := $(UV) run --frozen python

.DEFAULT_GOAL := help

.PHONY: help install login run dry-run force status scheduler test check \
	launchd-check launchd-install launchd-status launchd-uninstall

help: ## 显示所有快捷命令
	@awk 'BEGIN {FS = ":.*## "; printf "用法：make <command> [TASK=task_id]\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## 按 uv.lock 安装依赖
	$(UV) sync --frozen

login: ## 打开独立 Chrome，人工登录
	$(PYTHON) -m compass_collector login

run: ## 正式采集并发布 SQLite/CSV
	$(PYTHON) -m compass_collector run --task $(TASK)

dry-run: ## 真实采集和校验，但不发布 SQLite/CSV
	$(PYTHON) -m compass_collector run --task $(TASK) --dry-run

force: ## 强制创建同一计划时间的新版本
	$(PYTHON) -m compass_collector run --task $(TASK) --force

status: ## 查看最近运行状态
	$(PYTHON) -m compass_collector status

scheduler: ## 前台启动 Scheduler，按 Ctrl-C 停止
	$(PYTHON) -m compass_collector scheduler

test: ## 执行全部自动化测试
	$(PYTHON) -m pytest

check: test launchd-check ## 执行测试和 launchd 无副作用检查
	@echo "全部检查通过"

launchd-check: ## 校验 plist 和安装脚本，不修改系统
	bash -n scripts/install_launchd.sh scripts/uninstall_launchd.sh scripts/status_launchd.sh
	plutil -lint launchd/com.zhuanz1.douyin-compass-collector.plist.template
	./scripts/install_launchd.sh --dry-run

launchd-install: ## 安装并启动 LaunchAgent（会修改用户系统状态）
	./scripts/install_launchd.sh

launchd-status: ## 查看 LaunchAgent 状态
	./scripts/status_launchd.sh

launchd-uninstall: ## 停止并卸载 LaunchAgent（会修改用户系统状态）
	./scripts/uninstall_launchd.sh
