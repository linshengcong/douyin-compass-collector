"""Exercise Qt startup with the actual collector command-line platform arguments."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("platform", ["compass", "taobao"])
@pytest.mark.parametrize("command", ["run", "app", "console_notice", "portable_notice"])
def test_collector_platform_argument_does_not_select_qt_plugin(tmp_path, platform, command):
    """Start the production GUI entrypoint in a fresh process without collecting."""
    # 独立解释器没有预先创建QApplication，能够复现Qt读取业务--platform的崩溃。
    code = """
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from compass_collector import cli, gui
from compass_collector.config import load_config
from compass_collector.qt_application import ensure_qt_application

# Qt初始化不得消费或改变业务CLI参数。
original_arguments = tuple(sys.argv)

class StartupWindow(QWidget):
    def __init__(self, config, request):
        super().__init__()
        print('QT_PLUGIN=' + QApplication.instance().platformName(), flush=True)
        QTimer.singleShot(0, self.close)

gui.CollectorWindow = StartupWindow
gui.RUNTIME_ROOT = Path(sys.argv[-1])
if sys.argv[1] == 'console_notice':
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.information = lambda *args: None
    assert cli._show_packaged_console_command_notice() == 2
    print('QT_PLUGIN=' + QApplication.instance().platformName(), flush=True)
    raise SystemExit(0)
if sys.argv[1] == 'portable_notice':
    from PySide6.QtWidgets import QMessageBox
    cli.ensure_portable_data_root = lambda: Path(sys.argv[-1])
    cli.dotenv_path = lambda: Path(sys.argv[-1]) / 'missing.env'
    QMessageBox.exec = lambda self: 0
    QMessageBox.clickedButton = lambda self: None
    try:
        cli._validate_portable_environment()
    except ValueError as error:
        assert str(error) == 'portable_env_missing'
    else:
        raise AssertionError('missing environment was accepted')
    print('QT_PLUGIN=' + QApplication.instance().platformName(), flush=True)
    raise SystemExit(0)
config = load_config(Path('config/tasks.yaml'))
request = gui.GuiLaunchRequest(config_path=Path('config/tasks.yaml'), task_id=None, platform=sys.argv[3], auto_start=False)
# 主入口创建应用后，共用初始化器必须返回同一个Qt实例。
exit_code = gui.run_gui(config, request)
assert tuple(sys.argv) == original_arguments
assert ensure_qt_application() is QApplication.instance()
raise SystemExit(exit_code)
"""
    # 使用可用offscreen插件；Qt若误消费compass/taobao仍会原样报错并中止。
    result = subprocess.run(
        [sys.executable, "-c", code, command, "--platform", platform, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": "src", "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "QT_PLUGIN=offscreen" in result.stdout
    assert "Could not find the Qt platform plugin" not in result.stderr
