# 乐库整理助手

面向 Windows 11 的本地音乐文件整理工具。M1～P3 已完成功能门禁，当前正在开发 **P4 歌词扫描与匹配**。

## 当前安全边界

- 开发、自动化测试和独立验收只使用 `TemporaryDirectory` 创建的媒体夹具与数据库，不读取或修改用户真实的 Downloads、Music 或其他媒体目录。
- 正式应用只扫描用户显式选择的目录，并仅在用户应用数据目录保存索引。当前“导入”入口执行只读扫描和索引，不会移动文件；真实移动导入属于 P6。
- P2-A 只读分析、P2-B 同目录安全重命名及 P2-C 候选副本标签同步均已接入；数据库失败会恢复原标签文件和原文件名。
- P3 只改进正式列表的查询、歌名优先搜索、稳定排序和扫描差异状态，不扩大文件写入范围。
- P4 只接入 `.lrc` 扫描、编码安全读取、候选匹配和关系保存；低置信度结果必须人工确认。
- P2 不删除文件、不跨目录移动、不计算导入去重哈希；WAV、OGG、AAC 只允许安全重命名。
- 不创建或修改 Windows 快捷方式。
- 歌词匹配、真实导入、删除和恢复在对应安全阶段完成前仍是原型演示。

本项目是音乐文件管理工具，不是音乐播放器。

## 环境要求

- Windows 11
- Python 3.12
- PySide6（版本要求见 `requirements.txt`）
- Mutagen（固定版本见 `requirements.txt`）

## 获取并运行

```powershell
git clone https://github.com/VexorChan/MusicCtrl.git
cd MusicCtrl
pip install -r requirements.txt
python main.py
```

主窗口默认尺寸为 `1200 × 760`，最小尺寸为 `960 × 600`，支持最大化和自由缩放。

当前可用流程：点击“导入” → 选择音乐目录 → 点击“开始扫描” → 扫描完成后主列表刷新。只有成功扫描才记住目录；取消或失败不会覆盖上一次成功目录。

## 依赖许可与本机构建

Mutagen 使用 GPL-2.0-or-later。当前私人、本机、自用开发和最终本机安装可以继续；P8 生成的 `.exe` 不上传或分享。若以后需要对外分发，必须先完成项目许可证兼容决策并准备对应源码、构建材料和许可证文本，或者更换为许可兼容的元数据依赖。本说明是工程边界，不是法律意见。

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
