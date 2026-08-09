# 电脑插件真人操作测试结果

## 结论

- 12 项用户任务全部通过；其中 2 项在首轮发现易用性问题，修正后复测通过。
- 全程使用隔离模拟数据，导入、重命名、歌词匹配、删除和清理均未越过确认步骤。
- 410 项完整单元测试、compileall、smoke、隔离 EXE 构建、本机安装和桌面快捷方式回读均通过。

## 人性化完善

1. 操作历史时间从原始 ISO 字符串改为本机易读的 `YYYY-MM-DD HH:MM`，完整原值仍可通过工具提示查看。
2. 设置中的维护按钮从统一“执行”改为“重新检查 / 打开目录 / 清理备份”，清理按钮保留危险色和既有确认流程。

## 证据

截图保存在 `docs/test-evidence/2026-08-09-codex/`：

- `01-search-known.jpg`：已知关键词搜索。
- `02-playlist.jpg`：歌单安全说明。
- `03-import-dialog.jpg`：导入入口安全边界。
- `04-rename-dialog.jpg`：重命名预览。
- `05-lyrics-match.jpg`：歌词匹配候选。
- `06-history.jpg`、`09-history-friendly.jpg`：历史时间改进前后。
- `08-settings-maintenance.jpg`、`10-settings-maintenance-friendly.jpg`：维护按钮改进前后。
- `11-delete-confirm.jpg`：删除前确认。
- `12-minimum-window.jpg`：960×600 布局。

## 交付验证

- 运行时提交：`a98c026a30cf098a7db043853e2314fe1f9d508c`
- EXE SHA-256：`1206CD45E01C0D41165AE88587900E8344AA14B5BEF07C45DD4E89900ACE1912`
- 构建清单：225 条全部回读，完整包 226 个文件。
- 安装目录：`%LOCALAPPDATA%\Programs\MusicCtrl`，数据库、日志和备份文件为 0。
- 安装前版本已保留在 `%LOCALAPPDATA%\Programs\MusicCtrl-backup-20260809-094724`。
- 桌面快捷方式目标和工作目录回读正确，项目外启动 5 秒稳定。

## 已知限制

本机没有 LibreOffice，因此 DOCX 未执行页面 PNG 渲染门禁；已执行结构化回读检查。该限制只影响测试报告的像素级版面确认，不影响应用测试结果。
