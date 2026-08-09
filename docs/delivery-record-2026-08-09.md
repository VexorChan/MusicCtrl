# 2026-08-09 本机稳定交付记录

## 2026-08-09 09:47 真人操作体验修正交付

- `RUNTIME_HEAD=a98c026a30cf098a7db043853e2314fe1f9d508c`，构建时与 `origin/main` 完全一致。
- 电脑插件按普通用户路径执行 12 项隔离测试；历史时间和维护按钮两项 P2 易用性问题已修正并复测通过。
- 定向测试 13 项、完整单元测试 410 项、`compileall` 和 `smoke_test.py` 全部通过。
- 独立构建工作树：`work/p8-final-a98c026-20260809-094359`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- `MusicCtrl.exe` SHA-256：`1206CD45E01C0D41165AE88587900E8344AA14B5BEF07C45DD4E89900ACE1912`。
- `SHA256SUMS.txt` 共 225 个条目并全部回读；含清单自身的构建包与安装目录均为 226 个文件，文件集合与逐文件 SHA-256 一致。
- 构建 EXE 和安装 EXE 均从项目目录外启动并保持 5 秒，窗口标题为“乐库整理助手”，随后正常关闭。
- 安装目录：`%LOCALAPPDATA%\Programs\MusicCtrl`；安装目录内数据库、日志或备份文件为 0。
- 旧安装恢复副本：`%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260809-094724`，未删除。
- 桌面快捷方式目标、工作目录和图标均回读正确。
- 本节之后的提交只更新报告、截图和交付记录，不改变已构建运行时。

## 2026-08-09 08:05 缺失记录清理与主页面刷新维护交付

- `RUNTIME_HEAD=896d2e1d56ecfcb009421a5988b996fa208b94ef`，构建时已回读 `ORIGIN_MAIN_AT_BUILD` 完全一致。
- `python -W error::ResourceWarning -m unittest discover -q`：408 项通过；`compileall`、`smoke_test.py`、`git diff --check` 通过。
- 系统共享 Python 环境的 `pip check` 存在与本项目无关的旧包冲突；未修改该共享环境。实际构建使用 `.venv-build`，其 `pip check` 为 `No broken requirements found.`。
- 独立构建工作树：`work/p8-final-896d2e1-20260809-080217`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- 构建 EXE 从项目目录外启动并保持运行 5 秒，无提前退出；安装 EXE 再次从项目外启动，窗口标题回读为“乐库整理助手”。
- 960×600 窗口在 125% 和 150% Qt 缩放下，“刷新”及相邻操作按钮完整可见且无重叠。离屏平台未正确栅格化中文字体，因此这两张维护截图只作为几何布局证据，不冒充字体渲染验收。
- `MusicCtrl.exe` SHA-256：`60732C8F931B31E9D4718FAC99463AC94C54E256688340B020954E75AF01C6D6`。
- `SHA256SUMS.txt` 共 225 个条目并全部回读；含清单自身的构建包与安装目录均为 226 个文件，文件集合与逐文件 SHA-256 完全一致。
- 安装目录：`%LOCALAPPDATA%\Programs\MusicCtrl`；安装目录内数据库、日志或备份文件为 0。
- 旧安装恢复副本：`%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260809-080535`，未删除。
- 桌面快捷方式回读目标为 `%LOCALAPPDATA%\Programs\MusicCtrl\MusicCtrl.exe`，工作目录为 `%LOCALAPPDATA%\Programs\MusicCtrl`，图标为该 EXE 资源 0。
- 本节之后的提交只更新受版本控制的交付记录，不改变已构建运行时；最终仍要求文档提交推送后 `HEAD == origin/main`、ahead/behind `0/0`。

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
