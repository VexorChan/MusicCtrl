# 2026-08-09 本机稳定交付记录

## 提交关系

- `RUNTIME_HEAD=fb89f8df685c80a7804d8fbd2a7150a701bc658f`
- 本文件所属提交是晚于 `RUNTIME_HEAD` 的纯文档交付记录，不改变 Python、资源、依赖、spec 或测试，因此安装 EXE 的权威运行时来源仍是上述提交。
- 构建时 `origin/main` 与 `RUNTIME_HEAD` 一致；交付记录提交推送后再次要求最终 `HEAD == origin/main`、ahead/behind `0/0`。

## 自动化与环境

- Windows 11 `10.0.26200`
- Python `3.13.5`
- PySide6 `6.9.2`
- Mutagen `1.48.1`
- pywin32 `308`
- PyInstaller `6.21.0`
- `pip check`：`No broken requirements found.`
- `python -W error::ResourceWarning -m unittest discover -q`：402 项通过，0 警告
- `python -m compileall -q .`：通过
- `python smoke_test.py`：通过
- `git diff --check`：通过

## 构建与视觉验收

- 独立源码快照：`work/p8-final-fb89f8d-20260809-072303`
- 完整包：该目录下的 `dist/MusicCtrl`
- 构建包 `BUILD-INFO.txt` 回读 `HEAD` 与 `ORIGIN_MAIN_AT_BUILD` 均为 `fb89f8df685c80a7804d8fbd2a7150a701bc658f`。
- 从项目目录外启动构建 EXE 并保持运行 5 秒，无提前退出。
- 重新生成并检查 960×600、1200×760、125% 和 150% Qt 缩放截图；主窗口、导入、重命名、歌词匹配、历史和只读扫描窗口未见文字截断、控件重叠或关键操作不可见。

## 哈希、安装与快捷方式

- `MusicCtrl.exe` SHA-256：`4B43D9B7539C90F78CB17EA4C02F0C8E315F543FC10A792435FCB467A5C46FD4`
- `SHA256SUMS.txt`：225 个条目全部回读一致；它覆盖除清单自身外的全部构建文件。
- 构建包和安装目录各 226 个文件，文件集合及逐文件 SHA-256 完全一致。
- 安装目录：`%LOCALAPPDATA%\Programs\MusicCtrl`
- 旧安装恢复副本：`%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260809-072647`
- 安装目录内 `.sqlite`、`.sqlite3`、`.db`、`.log` 或备份文件：0。
- 桌面快捷方式：`%USERPROFILE%\Desktop\乐库整理助手.lnk`
- 回读目标：`%LOCALAPPDATA%\Programs\MusicCtrl\MusicCtrl.exe`
- 回读工作目录：`%LOCALAPPDATA%\Programs\MusicCtrl`
- 回读图标：最终安装 EXE 的资源 0。
- 从项目目录外通过桌面快捷方式启动并保持运行 5 秒，无提前退出。

## 边界

- 本 EXE 仅供私人、本机、自用，不上传或分享。
- 用户应用数据仍位于 `%LOCALAPPDATA%\LocalMusicTools\乐库整理助手`，未复制到安装目录。
- 项目根旧 `dist/` 和历史构建目录未删除，也不作为本次交付权威来源。
