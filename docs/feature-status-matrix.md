# 需求—实现—测试状态矩阵

## 结论

- 需求范围内完全未实现功能：**0 项**。
- M1～P8 产品能力均有真实 UI/controller/repository 或受管文件实现，并有自动化验证。
- 本轮确认的 6 项缺陷已完成代码修复或规则统一；最终 EXE 重建与安装回读仍属于本轮 P8 交付门禁，完成后写入交付记录。

## 功能矩阵

| 阶段 | 用户能力 | 主要实现 | 自动化证据 | 状态 |
|---|---|---|---|---|
| M1 | 中文界面、导航、搜索、排序、选择与弹窗原型 | `ui/`、`dialogs/`、`mock/` | UI、交互、smoke 与截图探针 | 已实现 |
| P1 | 显式目录只读扫描、SQLite 索引、取消与差异同步 | `library_scan_controller.py`、repository、migration v1 | scan worker、P1 integration、repository | 已实现 |
| P2 | 标签/文件名识别、可编辑预览、安全重命名和受支持格式元数据写入 | `metadata_preview.py`、`safe_rename.py`、migration v2 | P2 preview/rename/metadata tests | 已实现 |
| P3 | Model/View 正式列表、歌名优先搜索、稳定排序、外部变化状态 | `music_page.py`、`tables.py` | library model、production tables、10k records | 已实现 |
| P4 | LRC 扫描、编码保护、候选匹配、人工确认和关系历史 | `lyrics_match_controller.py`、migration v3 | P4 integration、lyrics repository | 已实现 |
| P5 | 受管歌单创建/移除/刷新、重命名联动和启动恢复 | `playlist_controller.py`、`windows_shortcuts.py` | playlist controller/shortcut tests | 已实现 |
| P6 | 预览、大小与 SHA-256 校验、安全移动、恢复与最近完整导入撤销 | `safe_import.py` | safe import、recovery、UI tests | 已实现 |
| P7 | 引用检查、备份、恢复、保留期清理与组恢复 | `backup_manager.py` | backup manager、history/settings tests | 已实现 |
| P8 | 隔离构建、项目外启动、安装目录与快捷方式回读 | `MusicCtrl.spec`、构建/清单脚本和交付文档 | compile、smoke、manifest、EXE probes | 已实现；本轮重建待门禁 |

## 本轮缺陷处理

| 缺陷 | 处理结果 | 验证 |
|---|---|---|
| 本地提交未推送 | 已恢复推送，当前开发提交持续要求 ahead/behind `0/0` | Git 远端回读 |
| 快捷方式发现失败无恢复点 | journal v2 先写 `discover`，条件式转为 `apply`；v1 兼容 | 发现失败、取消、换根、无引用、v1 续跑与原子转换测试 |
| 正式表格仍使用 `QTableWidget` | 正式主列表和业务弹窗统一为 Model/View，模拟路径保留 | 生产实例类型、勾选、编辑、选择、delegate 与空状态测试 |
| 安全导入临时仓库警告 | 增加幂等 `close()`；运行中拒绝关闭；测试显式释放 | `ResourceWarning` 按错误运行全量测试 |
| Python 规则版本不一致 | 项目规则统一为实际验证的 Python 3.13.5 | 文档检查与最终构建环境回读 |
| 最终交付证据不完整 | 本文件固定状态矩阵；最终提交、EXE 哈希、清单、安装及快捷方式证据写入交付记录 | P8 最终门禁 |

## 非阻塞 backlog

- 轻微视觉与非关键文案优化，仅在明确需求下继续，不扩大播放器、在线能力或数据模型范围。
- SQLite 保持 v3；P5～P7 当前 settings JSON 与文件 journal 已满足本机自用边界，除非出现明确查询或完整性收益，否则不新增 migration。
- EXE 继续只供私人、本机、自用，不上传或分享。
