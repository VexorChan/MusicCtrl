# SQLite 数据库设计

## 1. 文档状态与设计原则

本文同时记录当前已经部署的 P1 v1、P2-B1 v2、P4 v3 schema、P2-B2/P2-C 运行时契约，以及 P5～P7 尚未部署的结构草案：

- 标记为“P1 v1 已部署”的表、字段、约束、索引和事务已经存在于当前 migration。
- 标记为“P2-B1 v2 已部署”的重命名审计表和 repository 状态机已经存在于当前 migration；v2 本身不执行文件重命名。
- P2-C 已复用 v2 rename operation 的 `after_json.metadata_sync` 保存原始/写入标签快照和实际新指纹，没有新增 schema；P4 v3 只新增最小 `lyrics_matches`；其余标记为目标草案的表或字段仍未落入 migration，不得作为当前数据库能力使用。
- 后续新增正式字段或表时，必须先更新设计、增加连续 migration，并完成升级、回滚和数据保留测试。

通用原则：

- 文件系统是事实来源；SQLite 在 P1 保存只读索引、扫描会话和设置，后续阶段再保存关系和审计信息。
- 所有数据库访问必须经过 repository 层，UI 不直接连接 SQLite。
- 数据库路径必须由调用方以绝对 `Path` 显式注入；连接层不选择默认路径，也不创建缺失的父目录。
- 每个 SQLite 连接和 `LibraryRepository` 只允许创建它们的线程使用和关闭。
- P1 启用 `PRAGMA foreign_keys = ON` 和显式 `busy_timeout`，使用 autocommit 连接并由 migration/repository 明确控制事务。
- P1 明确不启用 WAL，保持 SQLite 默认 rollback journal；未来出现并发读写需求时再通过专项测试评估 WAL。
- 时间统一保存为 UTC ISO 8601 文本。
- 主键使用应用生成的 UUID 文本，避免依赖跨事务自增编号。
- 不得通过删除数据库重新初始化正式用户数据。

## 2. 关系概览

### 2.1 当前已部署：P1 v1 + P2-B1 v2 + P4 v3

```text
schema_migrations
assets
settings
scan_sessions ── scan_items
assets ── operation_items ── operations
assets ── lyrics_matches（audio / external lyric）
```

当前持久化外键为：

- `scan_items.session_id → scan_sessions.id`，删除 session 时级联删除其 scan items；
- `operation_items.operation_id → operations.id`，删除 operation 时级联删除其 items；
- `operation_items.asset_id → assets.id`，使用 `ON DELETE RESTRICT` 保护审计引用。
- `lyrics_matches.audio_asset_id → assets.id` 与可空的 `lyric_asset_id → assets.id` 均使用 `ON DELETE RESTRICT`，避免删除仍有匹配历史的资产。

### 2.2 尚未部署：P5～P7 目标草案

```text
assets
├── audio_tracks
│   ├── lyrics_files
│   └── playlist_items ── playlists
└── backup_entries
```

以上关系均未存在于当前 v3 schema，必须在对应阶段重新审查并通过新 migration 落地。P4 v3 已直接使用 `assets(kind='audio'/'lyric')` 建立最小歌词匹配历史，不依赖尚未部署的 `audio_tracks` 或 `lyrics_files`。

## 3. 当前已部署的 P1 v1 规范

v1 共五张表；v2 在保留这五张表的基础上增加两张重命名审计表；v3 再增加一张歌词匹配历史表。

### 3.1 schema_migrations（P1 v1 已部署）

记录数据库结构版本。

| 字段 | 类型 | 当前约束 |
|---|---|---|
| version | INTEGER | PRIMARY KEY |
| description | TEXT | 允许为空 |
| applied_at | TEXT | NOT NULL |

### 3.2 assets（P1 v1 已部署）

保存音频或歌词文件的只读索引。P1 产品扫描当前只接入音频，但 schema 为后续歌词索引保留 `lyric` 类型。

| 字段 | 类型 | 当前约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY，NOT NULL |
| kind | TEXT | NOT NULL；audio / lyric |
| canonical_path | TEXT | NOT NULL；用户可见规范路径 |
| normalized_path | TEXT | NOT NULL，UNIQUE；用于 Windows 等价路径比较 |
| file_name | TEXT | NOT NULL |
| extension | TEXT | NOT NULL |
| size_bytes | INTEGER | NOT NULL，且 `>= 0` |
| mtime_ns | INTEGER | 允许为空；非空时 `>= 0` |
| file_state | TEXT | NOT NULL；active / missing / external_changed |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

当前命名索引：

- `idx_assets_kind_state(kind, file_state)`

`original_path`、`sha256`、`backup` 状态、`is_standardized`、`deleted_at` 和 SHA-256 索引均未在 P1 v1 中部署。

### 3.3 settings（P1 v1 已部署）

| 字段 | 类型 | 当前约束 |
|---|---|---|
| key | TEXT | PRIMARY KEY，NOT NULL |
| value_json | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

- repository 负责标准 JSON 序列化和严格反序列化。
- 不接受 NaN、Infinity 或无法序列化为标准 JSON 的值。
- 设置中不得保存密钥、密码或其他凭据。

### 3.4 scan_sessions（P1 v1 已部署）

一次扫描尝试对应一个 session；同一 session 可以包含多个原子索引批次。

| 字段 | 类型 | 当前约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY，NOT NULL |
| mode | TEXT | NOT NULL；audio / lyric |
| source_folder | TEXT | NOT NULL |
| status | TEXT | NOT NULL；running / cancelled / completed / failed |
| started_at | TEXT | NOT NULL |
| completed_at | TEXT | 允许为空 |

`target_folder` 未在 P1 v1 中部署；它属于后续真实导入流程的目标草案。

### 3.5 scan_items（P1 v1 已部署）

| 字段 | 类型 | 当前约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY，NOT NULL |
| session_id | TEXT | NOT NULL；引用 scan_sessions.id，ON DELETE CASCADE |
| source_path | TEXT | NOT NULL |
| size_bytes | INTEGER | 允许为空；非空时 `>= 0` |
| status | TEXT | NOT NULL；waiting / indexed / skipped / failed |
| reason | TEXT | 允许为空 |

当前约束和索引：

- `UNIQUE(session_id, source_path)`
- `idx_scan_items_session(session_id)`

`suggested_path`、`sha256`、`selected` 以及 importable / rename_required / duplicate / conflict 状态均未在 P1 v1 中部署。

## 4. P1 扫描事务边界

1. 每轮扫描先创建一个状态为 running 的 `scan_sessions` 记录。
2. 一轮扫描可以包含多个索引批次。每个 `index_scan_batch()` 使用独立的 `BEGIN IMMEDIATE`：
   - 读取并验证 session 的状态、模式和扫描根；
   - 使用 repository 的权威路径规范化和 `os.path.commonpath` 做组件级根边界检查；
   - 对同一批次逐项 upsert `assets` 并插入对应 `scan_items`。
3. 同一批次的 assets 与 scan items 原子提交；任一校验、约束、SQL 或 COMMIT 失败时整批回滚，不允许留下半批资产。
4. 已成功提交的早期批次，在后续扫描取消或失败时保留。
5. worker 的终态数量是 `committed_count`，即数据库实际已经提交的 assets/scan items 数量；它不等同于已经发出的 `batch_ready` 数量。
6. 取消发生在原子批次执行期间时，允许该批次完成并计入 `committed_count`；取消被观察后不再写后续批次，也不再发送该批 `batch_ready`。
7. cancelled 和 failed 使用独立的 session 终结事务，不执行负向 missing 推导。
8. audio 扫描完整成功时调用 `complete_scan_and_reconcile()`；missing 更新与当前 session 的 completed 状态在同一个 `BEGIN IMMEDIATE` 事务中提交。
9. reconciliation 的 SQL 或 COMMIT 失败时，missing 更新和 completed 状态全部回滚，worker 随后将 session 终结为 failed。

P1 不执行复制、移动、重命名、删除、哈希或标签写入，因此数据库事务不涉及真实文件补偿。

## 5. P1 数据库与文件系统重新校准

### 5.1 当前已见文件的状态

索引批次在覆盖资产指纹前先读取旧 `size_bytes` 和 `mtime_ns`，使用 Python 相等比较处理可空 mtime：

- 新路径：`active`。
- size 与 mtime 均未变化：`active`。
- 原状态为 missing，且以相同指纹重新出现：`active`。
- size 或 mtime 任一变化：`external_changed`。

P1 不读取文件内容，也不计算 SHA-256。

### 5.2 成功扫描后的 missing 推导

P1 当前只对 running audio session 执行 reconciliation；“同 mode”在 P1 中即 audio。

1. 从当前 session 的 `source_folder` 生成权威规范化根。
2. 只选择最近一次同时满足以下条件的历史 session 作为基线：
   - mode 与当前 reconciliation 模式相同，P1 为 audio；
   - status 为 completed；
   - `source_folder` 规范化后与当前根完全相同。
3. 候选 session 按 `completed_at DESC、started_at DESC、id DESC` 稳定决胜。
4. cancelled、failed、其他根、相似前缀根和嵌套根均不参与当前根的基线。
5. 本次与历史 scan items 都按精确 normalized path 集合比较，并再次通过组件级根边界校验；不使用字符串 `LIKE` 或前缀判断限定根。
6. 上一次成功扫描出现、本次未出现、且资产当前状态为 active 的记录改为 `missing`。
7. 当前状态为 `external_changed` 的缺席资产保持 `external_changed`。
8. 没有上一次同根成功基线时，不推导 missing。
9. missing 只更新状态，不删除资产记录。
10. missing 更新与当前 session completed 同事务提交，失败时一起回滚。

## 6. migration、备份与 fail-closed 规则

- migration 版本必须从 1 开始连续、唯一、升序，逐版本记录到 `schema_migrations`。
- 全新数据库文件或真正空的 SQLite 数据库应用 v1 时不创建备份。
- 没有待执行 migration 时不创建备份。
- 已有受 MusicCtrl 管理的数据库从 v1 升级到后续版本前，使用 SQLite Backup API 在数据库同目录创建一致性备份。
- 备份创建后执行 `PRAGMA integrity_check`；备份失败时禁止开始 migration。
- 每个 migration 使用独立的 `BEGIN IMMEDIATE / COMMIT`，逐条执行 SQL，不使用 `executescript`。
- migration 失败时回滚当前版本的 DDL 和版本记录；已经成功创建的升级前备份保留，供诊断和恢复。
- 包含未知表或数据、但没有有效 `schema_migrations` 的非空数据库 fail-closed，不原地初始化，也不删除原数据。
- migration 历史存在断档、版本高于当前程序支持范围或数据库损坏时 fail-closed。
- 当前正式 migration 为连续的 `[1, 2, 3]`。全新空库直接应用 v1+v2+v3，不创建备份；已有受管库升级下一版本前必须成功创建一份可打开且 `integrity_check=ok` 的 SQLite Backup API 备份。
- v2 的 DDL、版本记录或 COMMIT 失败时整体回滚，v1 数据与迁移历史保持不变，升级前备份保留。
- 已有 v3 幂等打开时不重复备份。旧程序打开更高版本数据库时必须把它视为 future version 并 0 写入；回退只能显式恢复升级前备份副本，禁止原地 schema downgrade。

## 7. 当前已部署的 P2-B1 v2 重命名审计规范

v2 只提供“计划、逐项状态、数据库原子提交和恢复判定”基础，不打开或修改媒体文件；真实同目录重命名由后续 P2-B2 文件执行器完成。

### 7.1 operations（P2-B1 v2 已部署）

| 字段 | 类型 | 当前约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY，NOT NULL |
| operation_type | TEXT | NOT NULL；当前仅 rename |
| status | TEXT | NOT NULL；planned / running / success / partial / failed / cancelled |
| success_count | INTEGER | NOT NULL，默认 0，且 `>= 0` |
| failure_count | INTEGER | NOT NULL，默认 0，且 `>= 0` |
| summary_json | TEXT | NOT NULL；repository 严格 JSON |
| created_at | TEXT | NOT NULL |
| started_at | TEXT | 允许为空 |
| completed_at | TEXT | 允许为空 |

### 7.2 operation_items（P2-B1 v2 已部署）

| 字段 | 类型 | 当前约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY，NOT NULL |
| operation_id | TEXT | NOT NULL；引用 operations.id，ON DELETE CASCADE |
| asset_id | TEXT | NOT NULL；引用 assets.id，ON DELETE RESTRICT |
| source_path | TEXT | NOT NULL |
| normalized_source_path | TEXT | NOT NULL |
| target_path | TEXT | NOT NULL |
| normalized_target_path | TEXT | NOT NULL |
| expected_size_bytes | INTEGER | NOT NULL，且 `>= 0` |
| expected_mtime_ns | INTEGER | 允许为空；非空时 `>= 0` |
| result | TEXT | NOT NULL；planned / running / success / failed / cancelled / rolled_back / rollback_failed |
| error_code | TEXT | 允许为空 |
| error_message | TEXT | 允许为空 |
| before_json | TEXT | NOT NULL；repository 严格 JSON |
| after_json | TEXT | 允许为空；repository 严格 JSON |
| created_at | TEXT | NOT NULL |
| completed_at | TEXT | 允许为空 |

当前约束和索引：

- `UNIQUE(operation_id, asset_id)`、`UNIQUE(operation_id, normalized_source_path)`、`UNIQUE(operation_id, normalized_target_path)`；
- `idx_operation_items_operation(operation_id, result)`；
- 对 result 为 planned/running 的 `asset_id` 和 `normalized_target_path` 分别建立部分唯一索引，防止并行活动计划抢占同一资产或 Windows 等价目标；终态历史不阻塞后续计划。

### 7.3 P2-B1 repository 事务边界

1. 创建计划时，operation 与全部 items 在同一个 `BEGIN IMMEDIATE` 中写入；任一资产、路径、指纹、格式、根边界或冲突校验失败时 0 写入。
2. operation 只允许 planned→running；item 只允许 planned→running。planned item 只能取消，失败、rolled_back 和 rollback_failed 只能从 running 记录。
3. 单项成功提交在同一事务中重新核对资产源路径与 size/mtime，更新 asset 目标路径、item success/after_json/completed_at 和 operation.success_count；任一步确定失败全部回滚。
4. operation 最终状态只能从实际 item 聚合推导：全 success→success，全 cancelled→cancelled，有 success 且存在非 success→partial，无 success 且存在失败类→failed；仍有 planned/running 时禁止终结。
5. repository 在 COMMIT 抛错后检查连接事务状态：仍在事务中则 ROLLBACK，属于确定未提交；已经离开事务则抛 `RepositoryCommitOutcomeUnknown`，不得自动重试或声称回滚。后续执行器必须用新连接同时回读 asset 与 item，只有两者一致时才能决定接受提交或恢复文件，混合/不可读状态必须 fail-closed。
6. `summary_json`、`before_json` 和 `after_json` 由 repository 使用 `allow_nan=False` 严格序列化/反序列化，不依赖 SQLite JSON1。

### 7.4 P2-B2 / P2-C 已部署运行时契约（无新 schema）

- P2-B2 在用户确认后只执行同目录、禁止覆盖的 `os.rename`，并在数据库确定失败时恢复原文件名。
- P2-C 仅对 MP3、FLAC、M4A 创建同目录候选副本，写入并回读 Title/Artist；原文件先保留为内部回滚副本，repository 提交成功后才清理。
- 标签写入后的实际 `size_bytes`、`mtime_ns` 与 `after_json.metadata_sync` 原始/写入快照，和 rename item success 在同一 repository 事务提交。
- COMMIT 结果未知时必须用新连接同时回读 asset 路径、实际指纹和 item success；未证明成功前不得清理回滚副本。
- 若内部候选或回滚副本因安全原因被保留，其 `.musicctrl-` 前缀由只读扫描器排除，不会被索引为用户音乐。
- `audio_tracks` 等下节结构仍是未部署草案；当前 P2-C 不依赖这些表。

## 8. 当前已部署的 P4 v3 歌词匹配历史

v3 只保存当前歌词关系及其更换/取消历史；LRC 文件本身继续作为 `assets(kind='lyric')` 索引，不增加 `lyrics_files` 或 `audio_tracks` 表。

### 8.1 lyrics_matches（P4 v3 已部署）

| 字段 | 类型 | 当前约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY，NOT NULL |
| audio_asset_id | TEXT | NOT NULL；引用 assets.id，ON DELETE RESTRICT |
| lyric_asset_id | TEXT | 外部 LRC 时引用 assets.id，ON DELETE RESTRICT；内嵌歌词时为空 |
| source_kind | TEXT | NOT NULL；embedded / external |
| confidence | INTEGER | NOT NULL；0～100 |
| method | TEXT | NOT NULL；automatic / manual |
| state | TEXT | NOT NULL；matched / cancelled |
| is_current | INTEGER | NOT NULL；0 / 1；当前记录必须为 matched |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

当前索引：

- `uq_current_lyrics_by_audio`：每首音频最多一个当前匹配；
- `uq_current_audio_by_external_lyric`：每个外部 LRC 最多服务一个当前音频匹配；
- `idx_lyrics_matches_audio_history(audio_asset_id, created_at)`：按音频读取历史。

repository 只允许 active audio 作为音频端、active lyric 作为外部歌词端。automatic 方法只允许置信度至少 95 的结果；embedded 必须为 100 分且优先于 external。更换匹配时在同一事务内将旧记录设为非当前并插入新记录，取消时保留历史，不物理删除。

## 9. P5～P7 未部署目标草案

以下结构均未存在于当前 v3 数据库。字段、枚举、索引和外键必须在对应阶段重新审查；只有新增连续 migration 并通过升级、回滚和数据保留测试后，才能标记为已部署。

### 9.1 assets 后续扩展（P6、P7 目标草案，未部署）

计划增加：

- `original_path`：初次导入前路径。
- `sha256`：完成计算前允许为空，并增加 SHA-256 查询索引。
- `file_state = backup`。
- `is_standardized`：0 / 1。
- `deleted_at`：软删除时间。

### 9.2 audio_tracks（后续增强草案，未部署）

| 字段 | 类型 | 目标约束 |
|---|---|---|
| asset_id | TEXT | PRIMARY KEY，引用 assets.id |
| title | TEXT | 规范歌名 |
| artist_display | TEXT | 多歌手使用 `、` |
| duration_ms | INTEGER | 允许为空 |
| metadata_source | TEXT | id3 / filename / manual |
| id3_title | TEXT | 原始 Title |
| id3_artist | TEXT | 原始 Artist |
| original_metadata_json | TEXT | 原始元数据快照 |
| has_embedded_lyrics | INTEGER | 0 / 1 |
| rename_state | TEXT | unchecked / ready / manual / conflict / completed |
| checked_at | TEXT | 允许为空 |

### 9.3 lyrics_files（后续增强草案，未部署）

| 字段 | 类型 | 目标约束 |
|---|---|---|
| asset_id | TEXT | PRIMARY KEY，引用 assets.id |
| parsed_title | TEXT | 允许为空 |
| parsed_artist | TEXT | 允许为空 |
| encoding | TEXT | UTF-8 / GBK / Big5 等 |
| match_state | TEXT | unchecked / matched / possible / unmatched / conflict |
| checked_at | TEXT | 允许为空 |

### 9.4 playlists（P5 目标草案，未部署）

| 字段 | 类型 | 目标约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| name | TEXT | NOT NULL |
| normalized_name | TEXT | NOT NULL，唯一 |
| folder_path | TEXT | NOT NULL |
| state | TEXT | active / missing / external_changed / deleted |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |
| deleted_at | TEXT | 允许为空 |

### 9.5 playlist_items（P5 目标草案，未部署）

| 字段 | 类型 | 目标约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| playlist_id | TEXT | 引用 playlists.id |
| audio_asset_id | TEXT | 引用 audio_tracks.asset_id |
| shortcut_path | TEXT | NOT NULL |
| normalized_shortcut_path | TEXT | NOT NULL，唯一 |
| position | INTEGER | 允许为空 |
| state | TEXT | active / broken / external_changed / removed |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

目标约束：`UNIQUE(playlist_id, audio_asset_id)`。

### 9.6 scan_sessions 与 scan_items 后续扩展（P6 目标草案，未部署）

计划增加：

- `scan_sessions.target_folder`。
- `scan_items.suggested_path`、`sha256`、`selected`。
- importable / rename_required / duplicate / conflict 等导入分析状态。

### 9.7 operations 后续扩展（P5～P7 目标草案，未部署）

以下字段和扩展枚举尚未部署；不能与上文 v2 已有字段混淆。

| 字段 | 类型 | 目标约束 |
|---|---|---|
| operation_type 扩展 | TEXT | scan / import / match / playlist / delete / restore / undo / purge |
| is_undoable | INTEGER | 0 / 1 |
| undo_deadline | TEXT | 允许为空 |
| parent_operation_id | TEXT | 引用 operations.id |

### 9.8 operation_items 后续扩展（P5～P7 目标草案，未部署）

| 字段 | 类型 | 目标约束 |
|---|---|---|
| backup_path | TEXT | 允许为空 |
| result 扩展 | TEXT | skipped 等后续操作状态，须由独立 migration 审查 |

### 9.9 backup_entries（P7 目标草案，未部署）

| 字段 | 类型 | 目标约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| asset_id | TEXT | 引用 assets.id |
| operation_item_id | TEXT | 引用 operation_items.id |
| original_path | TEXT | NOT NULL |
| backup_path | TEXT | NOT NULL |
| sha256 | TEXT | 允许为空 |
| expires_at | TEXT | 默认创建后 7 天；永久保留时为空 |
| restored_at | TEXT | 允许为空 |
| purged_at | TEXT | 允许为空 |
| created_at | TEXT | NOT NULL |

## 10. P5～P7 未来事务目标

以下规则尚未实现，落地时数据库事务不能替代文件系统回滚，应用层必须维护补偿步骤：

- 导入和移动：目标校验成功后，文件状态、操作明细和相关关系在同一个数据库事务中提交。
- 删除：文件成功进入备份后，才能提交 `assets.file_state = backup` 和 `backup_entries`。
- 重命名：文件、元数据和快捷方式全部成功时提交成功；部分失败必须保留可恢复的操作明细。
- 歌单快捷方式外部变化：更新未来的 `playlist_items.state`，不自动删除用户新增内容。

## 11. 当前验证基线

- `tests/test_database.py`：v1 五表、v2 两张重命名审计表、v3 歌词关系表、字段约束、命名索引、外键、无 WAL、migration 幂等、升级备份和回滚。
- `tests/test_lyrics_repository.py`：外部 LRC 一对一、内嵌优先、自动匹配阈值、更换和取消历史、线程与关闭边界。
- `tests/test_library_repository.py`：批次原子性、路径根边界、指纹状态、reconciliation 基线和事务回滚。
- `tests/test_scan_worker.py`：成功、取消、失败、终态互斥、`committed_count` 及真实两轮 SQLite 重新校准。
- `tests/test_rename_repository.py`：计划整批校验、状态机、成功原子提交、失败补偿记录、COMMIT 结果不明确与只读 readback。
