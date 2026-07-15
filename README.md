# 乐库整理助手

面向 Windows 11 的本地音乐文件整理工具。M1 高保真 UI 原型已经完成，当前正在开发 **P1 只读扫描与 SQLite 索引**。

## 当前安全边界

- 开发和自动化测试不扫描真实的 Downloads、Music 或其他用户媒体目录，只使用临时目录夹具。
- 开发和自动化测试只在临时目录建立 SQLite 索引；正式应用只扫描用户显式选择的目录，并仅在用户应用数据目录保存索引。P1 不读取音频内容、不计算真实文件哈希。
- 不移动、复制、重命名或删除真实音乐与歌词文件。
- 不创建或修改 Windows 快捷方式。
- 导入、重命名、匹配、删除和恢复在对应安全阶段完成前仍是原型演示。

本项目是音乐文件管理工具，不是音乐播放器。

## 环境要求

- Windows 11
- Python 3.12
- PySide6（版本要求见 `requirements.txt`）

## 获取并运行

```powershell
git clone https://github.com/VexorChan/MusicCtrl.git
cd MusicCtrl
pip install -r requirements.txt
python main.py
```

主窗口默认尺寸为 `1200 × 760`，最小尺寸为 `960 × 600`，支持最大化和自由缩放。

## 验证

在项目根目录执行：

```powershell
python -m compileall -q .
python smoke_test.py
```

验证成功时，冒烟测试会输出：

```text
SMOKE TEST PASSED
```

## 生成验收截图

```powershell
python capture_screenshots.py
```

脚本会在 `screenshots/` 目录生成完整的 10 张 M1 验收截图：所有音乐、所有歌词、歌单、导入、重命名预览、歌词匹配、操作历史、设置、删除音乐确认和搜索无结果。

## 项目图标

- Windows 图标：`assets/app_icon.ico`
- 透明 PNG：`assets/app_icon.png`
