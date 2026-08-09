# 2026-08-10 本机稳定交付记录

## 2026-08-10 00:18 文件状态列交付

- `RUNTIME_HEAD=6b3a163126aaf13a09801b62e30884ff7bf65b46`，构建时与 `origin/main` 完全一致。
- 正式测试目录 `pytest` 416 项通过；隔离构建环境执行 `python -W error::ResourceWarning -m unittest discover -q` 亦为 416 项通过，`compileall`、`smoke_test.py`、`git diff --check` 和 `pip check` 全部通过。
- 独立构建工作树：`work/p8-final-6b3a163-20260810-001420`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- `MusicCtrl.exe` SHA-256：`CC9C2CA8DFE65D4C649544F9CC5AB42509BB87EAC0F177635578958DCBBBE00C`。
- `SHA256SUMS.txt` 共 225 个条目并全部回读；含清单自身的构建包与安装目录均为 226 个文件，清单文件和逐文件 SHA-256 一致。
- 构建 EXE 从项目目录外启动并保持 5 秒，临时工作目录未产生文件；桌面快捷方式安装 EXE 亦启动并保持 5 秒，无提前退出。
- 安装目录：`%LOCALAPPDATA%\Programs\MusicCtrl`；安装目录内数据库和日志文件为 0，应用数据继续位于 `%LOCALAPPDATA%\LocalMusicTools\乐库整理助手`。
- 旧安装恢复副本：`%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260810-001756`，未删除。
- 桌面快捷方式目标、工作目录和图标均指向最终安装 EXE，目标和工作目录已回读正确。
- 本节之后的提交只更新交付记录，不改变已构建运行时。

## 边界

- 本 EXE 仅供私人、本机、自用，不上传或分享。
- 项目根旧 `dist/` 和历史构建目录未删除，也不作为本次交付权威来源。
