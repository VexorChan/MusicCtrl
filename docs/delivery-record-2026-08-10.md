# 2026-08-10 本机稳定交付记录

## 2026-08-10 08:10 重命名、快捷方式联动与统一选择修复最终交付

- `RUNTIME_HEAD=5f4c873051b55ac8628cd39c9777ce478de27134`，构建前已推送并通过 `git ls-remote` 回读，与 `origin/main` 完全一致。
- 主工作区和独立构建 worktree 均为 436 项测试通过；同时通过 `unittest discover`（启用 `ResourceWarning` 错误）、`compileall`、`smoke_test.py`、`git diff --check` 和隔离环境依赖检查。
- 独立构建 worktree：`work/p8-final-5f4c873-20260810-071852`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- `MusicCtrl.exe` SHA-256：`E6DF3D395BD9B7F1032E84E126318B42C4610CFEE0E237F6656536351965866B`；`SHA256SUMS.txt` 的 225 个条目全部回读一致，安装目录共 226 个文件。
- 安装目录：`%LOCALAPPDATA%\Programs\MusicCtrl`；旧安装完整保留为 `%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260810-072540`，未删除。安装目录内数据库和日志文件为 0。
- 桌面快捷方式 `%USERPROFILE%\Desktop\乐库整理助手.lnk` 的目标、工作目录和图标均已回读为最终安装 EXE；通过该快捷方式启动的进程路径为最终安装目录，界面正常显示并可正常关闭。
- Computer Use 仅在 `work/computer-use-20260810-072639` 和隔离 `manual-ui-*` 数据库中操作：验证主列表、重命名预览和歌词候选的勾选/高亮双向同步及再次点击取消；“无需更改”行默认禁用，编辑后可选，恢复原名后自动取消。
- 真人式重命名成功 2 项、失败或恢复 0 项；影响预统计为 2 个快捷方式。一首测试歌曲关联两个歌单后，两个 `playlist_items` 保持同一稳定 `audio_asset_id`，关系均为 `current/active`，两个 `.lnk` 回读目标均为新歌曲路径，未改动无关文件。
- 歌词真人式验收识别 2 个 LRC：100% 候选自动匹配，65% 候选保持待人工确认；候选行点击与复选框同步，再次点击同时取消。
- 本节之后的提交只更新交付证据，不改变已构建运行时。

## 2026-08-10 02:25 歌单选择、元数据与一致性修复最终交付

- `RUNTIME_HEAD=ba89f0c36e55b4333690935daa034bd823f176de`，构建前已推送并通过 `git ls-remote` 回读，与 `origin/main` 完全一致。
- 主工作区与独立构建 worktree 均为 432 项测试通过；隔离环境同时通过 `pip check`、`compileall`、`smoke_test.py` 和 `git diff --check`。
- 独立构建 worktree：`work/p8-final-ba89f0c-20260810-022221`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- `MusicCtrl.exe` SHA-256：`4098EF968421DA0EE25545AEC09EB411E96861ABEABDAC6403D24E69787AD070`。
- `SHA256SUMS.txt` 共 225 个条目并全部回读；含清单自身的构建包与安装目录均为 226 个文件，逐文件 SHA-256 一致。
- 构建 EXE、安装 EXE及桌面快捷方式均在隔离 `LOCALAPPDATA` 与项目外工作目录启动并保持 5 秒，工作目录零写入；安装目录内数据库、日志和备份文件为 0。
- 首次替换因旧版进程占用被 Windows 拒绝；关闭旧版后将原目录及失败复制副本整体保留为 `%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260810-021833`。最终边界修复重建后，前一安装另保留为 `%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260810-022508`；两者均未删除。
- 桌面快捷方式目标、工作目录和图标均回读为最终安装 EXE。
- 本节之后的提交只更新交付记录，不改变已构建运行时。

## 2026-08-10 01:13 多选与歌单管理修复交付

- `RUNTIME_HEAD=305f3d868555e91ee8c1aa0a0acaab93a0295e0b`；GitHub 443 当前不可达，连续四次推送均因连接超时或重置失败，因此构建包如实记录 `ORIGIN_MAIN_AT_BUILD=UNVERIFIED_NETWORK`，远端一致性门禁待网络恢复后补验。
- 主工作区与独立构建工作树均为 423 项测试通过；隔离环境同时通过 `pip check`、`compileall`、`smoke_test.py` 和 `git diff --check`。
- 独立构建工作树：`work/p8-final-305f3d8-20260810-010727`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- `MusicCtrl.exe` SHA-256：`E7EED3F942CFE747576C007872C7E876E0654300D519368171F3D5C23ABC2586`。
- `SHA256SUMS.txt` 共 225 个条目并全部回读；含清单自身的构建包与安装目录均为 226 个文件。
- 构建 EXE 与安装 EXE 均从项目目录外启动并保持 5 秒，两个临时工作目录均未产生文件。
- 安装目录内数据库和日志文件为 0；旧安装恢复副本为 `%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260810-011349`，未删除。
- 桌面快捷方式目标、工作目录和图标均已回读为最终安装 EXE。
- 本节之后的提交只更新交付记录，不改变已构建运行时；远端推送成功前不得声称 GitHub 交付门禁关闭。

## 2026-08-10 00:34 旧扫描来源文件状态刷新修复交付

- `RUNTIME_HEAD=72eef30fcd033d919a778b38eefc903a1ef8bdf2`，构建时与 `origin/main` 完全一致。
- 正式测试目录 `pytest` 417 项通过；隔离构建环境执行 `python -W error::ResourceWarning -m unittest discover -q` 亦为 417 项通过，`compileall`、`smoke_test.py`、`git diff --check` 和 `pip check` 全部通过。
- 独立构建工作树：`work/p8-final-72eef30-20260810-003129`；Python 3.13.5、PyInstaller 6.21.0、PySide6 6.9.2、Mutagen 1.48.1、pywin32 308。
- `MusicCtrl.exe` SHA-256：`3D8AD59275045B6FA88D9143DCCEEB24BD2C9ECE9CD526B2EDE7E5CF518D19EE`。
- `SHA256SUMS.txt` 共 225 个条目并全部回读；含清单自身的构建包与安装目录均为 226 个文件。
- 构建 EXE 从项目目录外启动并保持 5 秒，临时工作目录未产生文件；桌面快捷方式安装 EXE 亦启动并保持 5 秒。
- 安装目录内数据库和日志文件为 0；旧安装恢复副本为 `%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260810-003420`，未删除。
- 桌面快捷方式目标及工作目录已回读为最终安装目录。
- 本节之后的提交只更新交付记录，不改变已构建运行时。

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
