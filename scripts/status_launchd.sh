#!/bin/bash

set -euo pipefail

# 服务标识必须与安装脚本和 plist 模板一致。
LABEL="com.zhuanz1.douyin-compass-collector"
# 当前用户 GUI 域用于只读查询服务状态。
LAUNCH_DOMAIN="gui/$(id -u)"
# plist 是否存在用于区分未安装和已停止。
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "${PLIST_PATH}" ]]; then
  echo "LaunchAgent 未安装：${PLIST_PATH}"
  exit 1
fi

/bin/launchctl print "${LAUNCH_DOMAIN}/${LABEL}"
