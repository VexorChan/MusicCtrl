from __future__ import annotations

import argparse
import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication

from main import build_app
from ui.main_window import MainWindow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在正常 Windows Qt 平台抓取指定客户区尺寸的 M1.2 核验图。")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = build_app()
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.resize(args.width, args.height)
    window.setWindowTitle(f"乐库整理助手 · {args.width}x{args.height} 原生核验")
    window.show()

    result = {"saved": False}

    def capture() -> None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pixmap = window.grab()
        window_geometry = window.geometry()
        frame_geometry = window.frameGeometry()
        result.update(
            {
                "qt_platform": QGuiApplication.platformName(),
                "font_family": app.font().family(),
                "requested_client_size": [args.width, args.height],
                "window_size": [window.width(), window.height()],
                "window_geometry": [
                    window_geometry.x(),
                    window_geometry.y(),
                    window_geometry.width(),
                    window_geometry.height(),
                ],
                "frame_geometry": [
                    frame_geometry.x(),
                    frame_geometry.y(),
                    frame_geometry.width(),
                    frame_geometry.height(),
                ],
                "captured_client_size": [pixmap.width(), pixmap.height()],
                "device_pixel_ratio": pixmap.devicePixelRatio(),
                "output": str(args.output.resolve()),
                "saved": pixmap.save(str(args.output), "PNG"),
            }
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        window.close()
        app.quit()

    QTimer.singleShot(500, capture)
    app.exec()
    return 0 if result["saved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

