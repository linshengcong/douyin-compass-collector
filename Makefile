# 默认任务 ID 可在命令行通过 TASK=... 覆盖。
TASK ?= product_hot_sale_all_level3
# uv 命令可在不同安装环境中通过 UV=/absolute/path/uv 覆盖。
UV ?= uv
# 采集模式统一通过 MODE 选择：normal、dry-run 或 force。
MODE ?= normal
# GUI 统一通过 GUI 选择：yes 为桌面窗口，no 为终端模式。
GUI ?= yes
# LaunchAgent 操作统一通过 ACTION 选择：check、install、status 或 uninstall。
ACTION ?= check
# 所有运行命令使用锁定依赖，避免后台或调试时隐式更新环境。
PYTHON := $(UV) run --frozen python

# MODE 只映射采集器现有互斥参数，避免新增重复 Make 目标。
ifeq ($(MODE),normal)
RUN_MODE_OPTION :=
else ifeq ($(MODE),dry-run)
RUN_MODE_OPTION := --dry-run
else ifeq ($(MODE),force)
RUN_MODE_OPTION := --force
else
$(error MODE must be normal, dry-run, or force)
endif

# GUI=no 显式回退终端，其他值在 Make 阶段立即拒绝。
ifeq ($(GUI),yes)
RUN_GUI_OPTION :=
else ifeq ($(GUI),no)
RUN_GUI_OPTION := --no-gui
else
$(error GUI must be yes or no)
endif

.DEFAULT_GOAL := help

.PHONY: help install login app run notify-test clear-data status scheduler web-dev web-build test check service

help: ## 显示所有快捷命令
	@awk 'BEGIN {FS = ":.*## "; printf "用法：make <command> [TASK=task_id]\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## 按 uv.lock 安装依赖
	$(UV) sync --frozen

login: ## 打开独立 Chrome，人工登录
	$(PYTHON) -m compass_collector login

app: ## 打开空闲 PySide6 采集控制台
	$(PYTHON) -m compass_collector app --task $(TASK)

run: ## 采集：MODE=normal|dry-run|force，GUI=yes|no
	$(PYTHON) -m compass_collector run --task $(TASK) $(RUN_MODE_OPTION) $(RUN_GUI_OPTION)

notify-test: ## 真实发送一条钉钉配置测试消息
	$(PYTHON) -m compass_collector notify-test

clear-data: ## 清除本地采集数据，保留 Chrome 登录态
	$(PYTHON) -m compass_collector clear-data --yes

status: ## 查看最近运行状态
	$(PYTHON) -m compass_collector status

scheduler: ## 前台启动 Scheduler，按 Ctrl-C 停止
	$(PYTHON) -m compass_collector scheduler

web-dev: ## 启动网站本地开发服务
	npm --prefix web run dev

web-build: ## 构建网站静态文件
	npm --prefix web run build

test: ## 执行全部自动化测试
	$(PYTHON) -m pytest

check: test service ## 执行测试和 LaunchAgent 无副作用检查
	@echo "全部检查通过"

service: ## LaunchAgent：ACTION=check|install|status|uninstall
ifeq ($(ACTION),check)
	bash -n scripts/install_launchd.sh scripts/uninstall_launchd.sh scripts/status_launchd.sh
	plutil -lint launchd/com.zhuanz1.douyin-compass-collector.plist.template
	./scripts/install_launchd.sh --dry-run
else ifeq ($(ACTION),install)
	./scripts/install_launchd.sh
else ifeq ($(ACTION),status)
	./scripts/status_launchd.sh
else ifeq ($(ACTION),uninstall)
	./scripts/uninstall_launchd.sh
else
$(error ACTION must be check, install, status, or uninstall)
endif
