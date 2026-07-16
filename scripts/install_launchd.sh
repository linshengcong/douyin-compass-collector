#!/bin/bash

set -euo pipefail

# LaunchAgent 使用稳定反向域名作为系统服务标识。
LABEL="com.zhuanz1.douyin-compass-collector"
# 脚本目录用于推导可迁移的工程绝对路径。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# launchd 必须使用绝对工作目录，不能依赖调用者当前目录。
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# 模板只保存非敏感启动结构，绝对路径在安装时写入。
TEMPLATE_PATH="${PROJECT_ROOT}/launchd/${LABEL}.plist.template"
# 当前用户 LaunchAgents 是阶段五唯一允许的安装目标。
PLIST_DIRECTORY="${HOME}/Library/LaunchAgents"
# 最终 plist 名称与 Label 一致，便于 status 和 uninstall 定位。
PLIST_PATH="${PLIST_DIRECTORY}/${LABEL}.plist"
# 当前 GUI 用户域用于现代 launchctl bootstrap/bootout。
LAUNCH_DOMAIN="gui/$(id -u)"
# 可执行 uv 的绝对路径避免依赖 launchd 的精简 PATH。
UV_PATH="$(command -v uv || true)"
# 可选 dry-run 只渲染和校验，不修改系统或用户 LaunchAgents。
MODE="${1:-install}"

if [[ "${MODE}" != "install" && "${MODE}" != "--dry-run" ]]; then
  echo "用法：$0 [--dry-run]" >&2
  exit 2
fi

if [[ -z "${UV_PATH}" ]]; then
  echo "未找到 uv，请先完成 README 中的环境安装" >&2
  exit 1
fi

if [[ ! -f "${TEMPLATE_PATH}" || ! -f "${PROJECT_ROOT}/config/tasks.yaml" ]]; then
  echo "工程文件不完整，无法生成 LaunchAgent" >&2
  exit 1
fi

# 临时 plist 在校验或安装结束后始终删除。
TEMPORARY_PLIST="$(mktemp "${TMPDIR:-/tmp}/${LABEL}.XXXXXX.plist")"
trap 'rm -f "${TEMPORARY_PLIST}"' EXIT
cp "${TEMPLATE_PATH}" "${TEMPORARY_PLIST}"

# PlistBuddy 负责字符串转义和结构化修改，避免用文本替换破坏 XML。
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 ${UV_PATH}" "${TEMPORARY_PLIST}"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:8 ${PROJECT_ROOT}/config/tasks.yaml" "${TEMPORARY_PLIST}"
/usr/libexec/PlistBuddy -c "Set :WorkingDirectory ${PROJECT_ROOT}" "${TEMPORARY_PLIST}"
/usr/bin/plutil -lint "${TEMPORARY_PLIST}"

if [[ "${MODE}" == "--dry-run" ]]; then
  echo "dry-run 通过：plist 结构有效，未写入 ${PLIST_PATH}，未调用 launchctl"
  exit 0
fi

# runtime 目录必须在 launchd 第一次启动前可写。
mkdir -p "${PROJECT_ROOT}/runtime/logs" "${PLIST_DIRECTORY}"

# 更新已安装服务前先从当前 GUI 域卸载旧定义。
if /bin/launchctl print "${LAUNCH_DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  /bin/launchctl bootout "${LAUNCH_DOMAIN}/${LABEL}"
fi

# plist 权限限制为当前用户可读写。
/usr/bin/install -m 600 "${TEMPORARY_PLIST}" "${PLIST_PATH}"
/bin/launchctl enable "${LAUNCH_DOMAIN}/${LABEL}"
/bin/launchctl bootstrap "${LAUNCH_DOMAIN}" "${PLIST_PATH}"
/bin/launchctl kickstart -k "${LAUNCH_DOMAIN}/${LABEL}"

echo "LaunchAgent 已安装并启动：${LABEL}"
echo "查看状态：${PROJECT_ROOT}/scripts/status_launchd.sh"
