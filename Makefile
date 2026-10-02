# 默认任务 ID 可在命令行通过 TASK=... 覆盖。
TASK ?= compass_household_cleaning_realtime
# 通用命令保持原主配置，可通过 CONFIG=... 切换。
CONFIG ?= config/tasks.yaml
# 当前淘宝全量验收配置使用已经登录成功的独立 Profile，可显式覆盖。
TAOBAO_CONFIG ?= runtime/acceptance/taobao/2026-10-01/full-root-config.yaml
# 淘宝采集默认启用钉钉汇总，可显式关闭；只影响本次进程，不修改本机凭证。
TAOBAO_DINGTALK_ENABLED ?= true
# uv 命令可在不同安装环境中通过 UV=/absolute/path/uv 覆盖。
UV ?= uv
# 登录和清除认证按平台选择独立 Profile。
PLATFORM ?= compass
# 采集模式统一通过 MODE 选择：normal、dry-run 或 force。
MODE ?= normal
# GUI 统一通过 GUI 选择：yes 为桌面窗口，no 为终端模式。
GUI ?= yes
# LaunchAgent 操作统一通过 ACTION 选择：check、install、status 或 uninstall。
ACTION ?= check
# 本地前端默认读取当前公开榜单索引；更换环境时可通过命令行覆盖。
WEB_DATA_INDEX_URL ?= https://e-commerce-data.oss-cn-shanghai.aliyuncs.com/compass/web/latest.json
# 所有运行命令使用锁定依赖，避免后台或调试时隐式更新环境。
PYTHON := $(UV) run --frozen python

# MODE 只映射采集器现有互斥参数，避免新增重复 Make 目标。
ifneq ($(filter normal dry-run force,$(MODE)),$(MODE))
$(error MODE must be normal, dry-run, or force)
endif
ifneq ($(words $(MODE)),1)
$(error MODE must be normal, dry-run, or force)
endif
# 延迟展开以使用淘宝目标的 force 默认值，命令行 MODE 仍具有最高优先级。
RUN_MODE_OPTION = $(if $(filter force,$(MODE)),--force,$(if $(filter dry-run,$(MODE)),--dry-run))

# GUI=no 显式回退终端，其他值在 Make 阶段立即拒绝。
ifneq ($(filter yes no,$(GUI)),$(GUI))
$(error GUI must be yes or no)
endif
ifneq ($(words $(GUI)),1)
$(error GUI must be yes or no)
endif
# 延迟展开让淘宝默认终端模式，同时允许 GUI=yes 覆盖。
RUN_GUI_OPTION = $(if $(filter no,$(GUI)),--no-gui)

# 淘宝三个入口使用同一配置和任务；解释器与当前已验证命令一致。
taobao-run taobao-login taobao-status: CONFIG = $(TAOBAO_CONFIG)
taobao-run taobao-login taobao-status: TASK = taobao_household_cleaning_realtime
taobao-run taobao-login taobao-status: PYTHON = .venv/bin/python
# 淘宝默认终端强制采集；显式 MODE/GUI 参数仍可覆盖这些目标默认值。
taobao-run: MODE = force
taobao-run: GUI = no

.DEFAULT_GOAL := help

.PHONY: help install login app run taobao-run taobao-login taobao-status notify-test clear-data clear-login status scheduler web-dev web-build test check service

help: ## 显示所有快捷命令
	@awk 'BEGIN {FS = ":.*## "; printf "用法：make <command> [TASK=task_id]\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## 按 uv.lock 安装依赖
	$(UV) sync --frozen

login: ## 打开独立 Chrome，人工登录
	$(PYTHON) -m compass_collector login --config "$(CONFIG)" --platform $(PLATFORM)

app: ## 打开空闲 PySide6 采集控制台
	$(PYTHON) -m compass_collector app --config "$(CONFIG)" --task $(TASK)

run: ## 采集：MODE=normal|dry-run|force，GUI=yes|no
	$(PYTHON) -m compass_collector run --config "$(CONFIG)" --task $(TASK) $(RUN_MODE_OPTION) $(RUN_GUI_OPTION)

taobao-run: ## 淘宝全量采集，默认 MODE=force GUI=no，钉钉通知开启
	DINGTALK_ENABLED=$(TAOBAO_DINGTALK_ENABLED) PYTHONPATH=src $(PYTHON) -m compass_collector run --config "$(CONFIG)" --task $(TASK) $(RUN_MODE_OPTION) $(RUN_GUI_OPTION)

taobao-login: ## 打开淘宝采集所用 Profile，人工登录
	PYTHONPATH=src $(PYTHON) -m compass_collector login --config "$(CONFIG)" --platform taobao

taobao-status: ## 查看淘宝验收数据库的最近批次
	PYTHONPATH=src $(PYTHON) -m compass_collector status --config "$(CONFIG)"

notify-test: ## 真实发送一条钉钉配置测试消息
	$(PYTHON) -m compass_collector notify-test

clear-data: ## 清除本地采集数据，保留 Chrome 登录态
	$(PYTHON) -m compass_collector clear-data --config "$(CONFIG)" --yes

clear-login: ## 清除 Chrome 登录态，保留本地采集数据
	$(PYTHON) -m compass_collector clear-auth --config "$(CONFIG)" --platform $(PLATFORM) --yes

status: ## 查看最近运行状态
	$(PYTHON) -m compass_collector status --config "$(CONFIG)"

scheduler: ## 前台启动 Scheduler，按 Ctrl-C 停止
	$(PYTHON) -m compass_collector scheduler --config "$(CONFIG)"

web-dev: ## 启动网站本地开发服务
	VITE_DATA_INDEX_URL="$(WEB_DATA_INDEX_URL)" npm --prefix web run dev -- --host 127.0.0.1 --port 5175

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
