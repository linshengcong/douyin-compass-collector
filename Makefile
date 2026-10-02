# 平台是显式必填参数，tt映射抖音罗盘，tb映射淘宝。
PLATFORM ?=
# 当前命令集合用于仅校验实际使用的参数，help/install/start不需要平台。
COMMANDS := $(if $(MAKECMDGOALS),$(MAKECMDGOALS),help)
# start启动两个平台；其余业务入口必须明确选择一个平台。
PLATFORM_COMMANDS := run login status clean schedule notify web check
ifneq ($(filter $(PLATFORM_COMMANDS),$(COMMANDS)),)
ifneq ($(words $(PLATFORM)),1)
$(error 必须指定 PLATFORM=tt 或 PLATFORM=tb，例如 make run PLATFORM=tb)
endif
ifeq ($(filter tt tb,$(PLATFORM)),)
$(error PLATFORM 只允许 tt 或 tb)
endif
endif

# 配置、任务和平台标识统一映射，登录/运行/状态/调度共用同一配置。
COLLECTOR_PLATFORM = $(if $(filter tb,$(PLATFORM)),taobao,compass)
# 抖音保持既有主配置，允许通过CONFIG显式覆盖。
TT_CONFIG ?= config/tasks.yaml
# 本机优先复用已经登录的淘宝全量配置；其他机器使用可分发的全量配置。
TB_CONFIG ?= $(if $(wildcard runtime/acceptance/taobao/2026-10-01/full-root-config.yaml),runtime/acceptance/taobao/2026-10-01/full-root-config.yaml,config/taobao.yaml)
# 最终配置是全部平台入口的唯一配置来源。
CONFIG ?= $(if $(filter tb,$(PLATFORM)),$(TB_CONFIG),$(TT_CONFIG))
# 默认任务按平台选择，显式TASK仍由CLI核验平台归属。
TASK ?= $(if $(filter tb,$(PLATFORM)),taobao_household_cleaning_realtime,compass_household_cleaning_realtime)
# 安装依赖使用uv，执行命令使用统一解释器，可在命令行覆盖。
UV ?= uv
PYTHON ?= .venv/bin/python
# 每次执行立即在GUI创建新批次并通知，可显式选择其他行为。
MODE ?= force
GUI ?= yes
START ?= yes
NOTIFY ?= yes
# ACTION由对应命令设置默认值；清理必须显式指定data或login。
ACTION ?=
# 网站两个平台的公开地址独立，不能将淘宝地址注入抖音变量。
TT_WEB_DATA_INDEX_URL ?= https://e-commerce-data.oss-cn-shanghai.aliyuncs.com/compass/web/latest.json
TB_WEB_DATA_INDEX_URL ?= https://e-commerce-data.oss-cn-shanghai.aliyuncs.com/compass/web/taobao/taobao_household_cleaning_realtime/latest.json
# 环境注入只影响本次进程，不修改.env或真实凭证。
NOTIFY_ENABLED = $(if $(filter yes,$(NOTIFY)),true,false)
RUN_MODE_OPTION = $(if $(filter force,$(MODE)),--force,$(if $(filter dry-run,$(MODE)),--dry-run))
RUN_GUI_OPTION = $(if $(filter no,$(GUI)),--no-gui)
# 动作只在对应命令解析，check不继承服务安装动作，clean没有隐式默认值。
VALID_ACTIONS_clean := data login
VALID_ACTIONS_schedule := run check install status uninstall
VALID_ACTIONS_web := dev build
VALID_ACTIONS_check := test all
# 用于Make读取阶段的默认动作，目标内默认值保持相同语义。
DEFAULT_ACTION_schedule := run
DEFAULT_ACTION_web := dev
DEFAULT_ACTION_check := all
ifneq ($(filter clean schedule web check,$(COMMANDS)),)
ifneq ($(strip $(ACTION)),)
ifneq ($(words $(ACTION)),1)
$(error ACTION 必须是一个动作)
endif
endif
$(foreach command,$(filter clean schedule web check,$(COMMANDS)),$(if $(filter $(if $(strip $(ACTION)),$(ACTION),$(DEFAULT_ACTION_$(command))),$(VALID_ACTIONS_$(command))),,$(error $(command) 的 ACTION 可选值为 $(VALID_ACTIONS_$(command)))))
endif

ifneq ($(filter run start,$(COMMANDS)),)
ifneq ($(words $(MODE)),1)
$(error MODE 只允许 force、normal 或 dry-run)
endif
ifeq ($(filter force normal dry-run,$(MODE)),)
$(error MODE 只允许 force、normal 或 dry-run)
endif
ifneq ($(filter-out yes no,$(GUI) $(START)),)
$(error GUI 和 START 只允许 yes 或 no)
endif
ifneq ($(words $(GUI)),1)
$(error GUI 和 START 只允许 yes 或 no)
endif
ifneq ($(words $(START)),1)
$(error GUI 和 START 只允许 yes 或 no)
endif
ifeq ($(GUI)$(START),nono)
$(error START=no 只能与 GUI=yes 一起使用)
endif
endif
ifneq ($(filter run start schedule notify,$(COMMANDS)),)
ifneq ($(words $(NOTIFY)),1)
$(error NOTIFY 只允许 yes 或 no)
endif
ifeq ($(filter yes no,$(NOTIFY)),)
$(error NOTIFY 只允许 yes 或 no)
endif
endif

.DEFAULT_GOAL := help
.PHONY: help install start run login status clean schedule notify web check

help: ## 帮助；单平台命令须带PLATFORM=tt|tb（install/start除外）
	@awk 'BEGIN {FS = ":.*## "; print "用法：make start [参数] 或 make <command> PLATFORM=tt|tb [参数]\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo "run 默认：GUI=yes START=yes MODE=force NOTIFY=yes；tt与tb可并行，同平台互斥"
	@echo "示例：make start（同时启动两平台）；make run PLATFORM=tb；make clean PLATFORM=tb ACTION=data"

install: ## 按uv.lock安装依赖
	$(UV) sync --frozen

start: ## 同时启动抖音和淘宝独立GUI；沿用run的MODE/GUI/START/NOTIFY参数
	@# tt_pid/tb_pid仅等待各自子命令；start_exit_code保留失败码，一个窗口结束不停止另一个。
	@$(MAKE) run PLATFORM=tt CONFIG="$(TT_CONFIG)" TASK=compass_household_cleaning_realtime & tt_pid=$$!; \
	$(MAKE) run PLATFORM=tb CONFIG="$(TB_CONFIG)" TASK=taobao_household_cleaning_realtime & tb_pid=$$!; \
	start_exit_code=0; \
	wait "$$tt_pid" || start_exit_code=$$?; \
	wait "$$tb_pid" || start_exit_code=$$?; \
	exit "$$start_exit_code"

run: ## 立即GUI采集；MODE=force|normal|dry-run GUI=yes|no START=yes|no NOTIFY=yes|no
	DINGTALK_ENABLED=$(NOTIFY_ENABLED) PYTHONPATH=src $(PYTHON) -m compass_collector run --config "$(CONFIG)" --platform $(COLLECTOR_PLATFORM) --task "$(TASK)" $(RUN_MODE_OPTION) $(RUN_GUI_OPTION) $(if $(filter no,$(START)),--idle)

login: ## 登录所选平台的采集Profile
	PYTHONPATH=src $(PYTHON) -m compass_collector login --config "$(CONFIG)" --platform $(COLLECTOR_PLATFORM)

status: ## 查看所选平台最近批次
	PYTHONPATH=src $(PYTHON) -m compass_collector status --config "$(CONFIG)" --platform $(COLLECTOR_PLATFORM)

clean: ## 必须指定ACTION=data（采集数据）或login（登录态）
	@case "$(ACTION)" in data|login) ;; *) echo "用法：make clean PLATFORM=$(PLATFORM) ACTION=data|login" >&2; exit 2;; esac
	PYTHONPATH=src $(PYTHON) -m compass_collector $(if $(filter login,$(ACTION)),clear-auth,clear-data) --config "$(CONFIG)" --platform $(COLLECTOR_PLATFORM) --yes

schedule: ACTION = run
schedule: ## ACTION=run|check|install|status|uninstall；默认前台调度
	@case "$(ACTION)" in run|check|install|status|uninstall) ;; *) echo "ACTION只允许run、check、install、status、uninstall" >&2; exit 2;; esac
	$(if $(filter run,$(ACTION)),DINGTALK_ENABLED=$(NOTIFY_ENABLED) PYTHONPATH=src $(PYTHON) -m compass_collector scheduler --config "$(CONFIG)" --platform $(COLLECTOR_PLATFORM),COLLECTOR_PLATFORM=$(COLLECTOR_PLATFORM) COLLECTOR_CONFIG="$(CONFIG)" COLLECTOR_NOTIFY=$(NOTIFY_ENABLED) ./scripts/$(if $(filter status,$(ACTION)),status_launchd,$(if $(filter uninstall,$(ACTION)),uninstall_launchd,install_launchd)).sh $(if $(filter check,$(ACTION)),--dry-run))

notify: ## 真实发送所选平台钉钉测试消息
	DINGTALK_ENABLED=$(NOTIFY_ENABLED) PYTHONPATH=src $(PYTHON) -m compass_collector notify-test --platform $(COLLECTOR_PLATFORM)

web: ACTION = dev
web: ## ACTION=dev|build；默认开发服务，并默认展示所选平台
	@case "$(ACTION)" in dev|build) ;; *) echo "ACTION只允许dev或build" >&2; exit 2;; esac
	VITE_DEFAULT_PLATFORM=$(COLLECTOR_PLATFORM) VITE_DATA_INDEX_URL="$(TT_WEB_DATA_INDEX_URL)" VITE_TAOBAO_DATA_INDEX_URL="$(TB_WEB_DATA_INDEX_URL)" npm --prefix web run $(if $(filter build,$(ACTION)),build,dev -- --host 127.0.0.1 --port 5175)

check: ACTION = all
check: ## ACTION=test|all；默认后端/前端测试及所选平台服务无副作用检查
	@case "$(ACTION)" in test|all) ;; *) echo "ACTION只允许test或all" >&2; exit 2;; esac
	PYTHONPATH=src $(PYTHON) -m pytest
	$(if $(filter all,$(ACTION)),npm --prefix web test,@true)
	$(if $(filter all,$(ACTION)),bash -n scripts/install_launchd.sh scripts/uninstall_launchd.sh scripts/status_launchd.sh,@true)
	$(if $(filter all,$(ACTION)),COLLECTOR_PLATFORM=$(COLLECTOR_PLATFORM) COLLECTOR_CONFIG="$(CONFIG)" ./scripts/install_launchd.sh --dry-run,@true)
