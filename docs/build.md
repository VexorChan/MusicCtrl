# Windows 本机构建与安装

当前已验证环境：

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

当前 EXE 仅供私人本机自用，不上传或分享。对外分发前必须重新处理依赖许可、源码材料和构建复现要求。
