# Windows 本机构建与安装

当前源码与既有构建流程已验证环境：

- Windows 11 10.0.26200
- Python 3.13.5
- PySide6 6.9.2
- Mutagen 1.48.1
- pywin32 308
- PyInstaller 6.21.0

项目固定使用隔离环境构建，避免共享 Conda 环境中的 Qt DLL 污染：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean MusicCtrl.spec
```

产物位于 `dist\MusicCtrl\MusicCtrl.exe`，完整的 `dist\MusicCtrl` 目录必须整体保留。
本机安装目录为 `%LOCALAPPDATA%\Programs\MusicCtrl`；应用数据库和备份位于
`%LOCALAPPDATA%\LocalMusicTools\乐库整理助手`，不得写入安装目录。

仓库当前 `HEAD` 晚于 `dist` 和本机安装目录中的既有 EXE；这些旧产物只能作为历史构建证据，不能作为当前源码的最终交付。完成本次维护后必须重新执行：全量测试、隔离构建、项目外启动探针、安装目录更新、桌面快捷方式目标与工作目录回读，以及最终 EXE 的 SHA-256 记录。

文档和最终报告必须以实际执行构建的解释器版本为准。当前已验证口径为 Python 3.13.5，不得用未参与本次构建的其他版本冒充已验证环境。

当前 EXE 仅供私人本机自用，不上传或分享。对外分发前必须重新处理依赖许可、源码材料和构建复现要求。
