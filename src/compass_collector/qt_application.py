"""Keep collector CLI options outside Qt's native argument parser."""

import sys

from PySide6.QtWidgets import QApplication


def ensure_qt_application() -> QApplication:
    """Reuse Qt or initialize it with only the executable name, preserving CLI argv."""
    # Qt也解析--platform；业务平台compass/taobao不能作为Qt窗口插件名传入。
    return QApplication.instance() or QApplication([sys.argv[0]])
