#!/bin/bash

set -euo pipefail

# 服务标识必须与 plist 模板完全一致。
LABEL="com.zhuanz1.douyin-compass-collector"
# 卸载严格寻址所选平台的服务，不删除另一平台或旧服务。
if [[ -n "${COLLECTOR_PLATFORM:-}" ]]; then
  case "${COLLECTOR_PLATFORM}" in compass|taobao) LABEL="${LABEL}.${COLLECTOR_PLATFORM}";; *) echo "不支持的平台" >&2; exit 2;; esac
fi
# 当前用户 GUI 域用于卸载用户级 LaunchAgent。
LAUNCH_DOMAIN="gui/$(id -u)"
# 卸载只删除当前用户的明确目标 plist。
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if /bin/launchctl print "${LAUNCH_DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  /bin/launchctl bootout "${LAUNCH_DOMAIN}/${LABEL}"
fi

/bin/launchctl disable "${LAUNCH_DOMAIN}/${LABEL}"
/bin/rm -f "${PLIST_PATH}"

echo "LaunchAgent 已卸载：${LABEL}"
