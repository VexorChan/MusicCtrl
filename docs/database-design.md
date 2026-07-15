# SQLite 数据库设计

## 1. 设计原则

- 本设计属于后续正式功能阶段，M1 不创建或写入真实数据库。
- 文件系统是事实来源；SQLite 保存索引、关系、设置和审计日志。
- 所有数据库访问必须经过 repository 层。
- 启用 `PRAGMA foreign_keys = ON`。
- 正式运行建议使用 WAL 模式，但测试必须验证正常关闭和异常恢复。
- 时间统一保存为 UTC ISO 8601 文本。
- 主键使用应用生成的 UUID 文本，避免依赖跨事务自增编号。
- 文件级操作结果必须持久化，以支持部分成功、诊断和恢复。

## 2. 关系概览

```text
assets
├── audio_tracks
│   ├── lyrics_matches ── lyrics_files
│   └── playlist_items ── playlists
├── backup_entries
└── operation_items ── operations

scan_sessions ── scan_items
settings
schema_migrations
```

## 3. 表结构

### 3.1 schema_migrations

记录数据库结构版本。

| 字段 | 类型 | 约束 |
|---|---|---|
| version | INTEGER | PRIMARY KEY |
| description | TEXT | 允许为空 |
| applied_at | TEXT | NOT NULL |

### 3.2 assets

统一表示音频和歌词文件。

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| kind | TEXT | audio / lyric |
| canonical_path | TEXT | NOT NULL，用户可见规范路径 |
| normalized_path | TEXT | NOT NULL，大小写和分隔符规范后的比较路径 |
| original_path | TEXT | 初次导入前路径 |
| file_name | TEXT | NOT NULL |
| extension | TEXT | NOT NULL |
| size_bytes | INTEGER | NOT NULL |
| sha256 | TEXT | 完成计算前允许为空 |
| mtime_ns | INTEGER | 文件修改时间 |
| file_state | TEXT | active / missing / backup / external_changed |
| is_standardized | INTEGER | 0 / 1 |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |
| deleted_at | TEXT | 软删除时间 |

索引：

- `UNIQUE(normalized_path)`
- `INDEX(kind, file_state)`
- `INDEX(sha256)`

### 3.3 audio_tracks

| 字段 | 类型 | 约束 |
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

### 3.4 lyrics_files

| 字段 | 类型 | 约束 |
|---|---|---|
| asset_id | TEXT | PRIMARY KEY，引用 assets.id |
| parsed_title | TEXT | 允许为空 |
| parsed_artist | TEXT | 允许为空 |
| encoding | TEXT | UTF-8 / GBK / Big5 等 |
| match_state | TEXT | unchecked / matched / possible / unmatched / conflict |
| checked_at | TEXT | 允许为空 |

### 3.5 lyrics_matches

保存当前匹配和历史匹配。

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| audio_asset_id | TEXT | 引用 audio_tracks.asset_id |
| lyric_asset_id | TEXT | 引用 lyrics_files.asset_id，内嵌歌词时允许为空 |
| source_kind | TEXT | embedded / external |
| confidence | REAL | 0～100 |
| method | TEXT | automatic / manual |
| state | TEXT | matched / possible / ignored / cancelled / conflict |
| is_current | INTEGER | 0 / 1 |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：

- 每首音频最多一个当前匹配。
- 当前外部 LRC 最多绑定一首音频。
- 检测到内嵌歌词时优先创建 `source_kind = embedded` 的当前状态。
- 更换匹配时将旧记录设为非当前，不直接删除历史。

建议使用部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_current_lyrics_by_audio
ON lyrics_matches(audio_asset_id)
WHERE is_current = 1;

CREATE UNIQUE INDEX uq_current_audio_by_external_lyric
ON lyrics_matches(lyric_asset_id)
WHERE is_current = 1 AND lyric_asset_id IS NOT NULL;
```

### 3.6 playlists

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| name | TEXT | NOT NULL |
| normalized_name | TEXT | NOT NULL，唯一 |
| folder_path | TEXT | NOT NULL |
| state | TEXT | active / missing / external_changed / deleted |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |
| deleted_at | TEXT | 允许为空 |

### 3.7 playlist_items

| 字段 | 类型 | 约束 |
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

约束：`UNIQUE(playlist_id, audio_asset_id)`。

### 3.8 scan_sessions

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| mode | TEXT | audio / lyric |
| source_folder | TEXT | NOT NULL |
| target_folder | TEXT | NOT NULL |
| status | TEXT | running / cancelled / completed / failed |
| started_at | TEXT | NOT NULL |
| completed_at | TEXT | 允许为空 |

### 3.9 scan_items

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| session_id | TEXT | 引用 scan_sessions.id |
| source_path | TEXT | NOT NULL |
| suggested_path | TEXT | 允许为空 |
| size_bytes | INTEGER | 允许为空 |
| sha256 | TEXT | 允许为空 |
| status | TEXT | waiting / importable / rename_required / duplicate / conflict / failed |
| reason | TEXT | 允许为空 |
| selected | INTEGER | 0 / 1 |

### 3.10 operations

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| operation_type | TEXT | scan / import / rename / match / playlist / delete / restore / undo / purge |
| status | TEXT | planned / running / success / partial / failed / cancelled |
| success_count | INTEGER | 默认 0 |
| failure_count | INTEGER | 默认 0 |
| is_undoable | INTEGER | 0 / 1 |
| undo_deadline | TEXT | 允许为空 |
| parent_operation_id | TEXT | 引用 operations.id |
| summary_json | TEXT | 批次摘要 |
| started_at | TEXT | NOT NULL |
| completed_at | TEXT | 允许为空 |

### 3.11 operation_items

| 字段 | 类型 | 约束 |
|---|---|---|
| id | TEXT | PRIMARY KEY |
| operation_id | TEXT | 引用 operations.id |
| asset_id | TEXT | 引用 assets.id，允许为空 |
| source_path | TEXT | 允许为空 |
| target_path | TEXT | 允许为空 |
| backup_path | TEXT | 允许为空 |
| result | TEXT | planned / success / skipped / failed / rolled_back |
| error_code | TEXT | 允许为空 |
| error_message | TEXT | 允许为空 |
| before_json | TEXT | 操作前快照 |
| after_json | TEXT | 操作后快照 |
| created_at | TEXT | NOT NULL |

### 3.12 backup_entries

| 字段 | 类型 | 约束 |
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

### 3.13 settings

| 字段 | 类型 | 约束 |
|---|---|---|
| key | TEXT | PRIMARY KEY |
| value_json | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

设置中不得保存密钥、密码或其他凭据。

## 4. 事务边界

- 扫描：每个批次一个会话，文件索引可以分批提交，但会话最终状态单独提交。
- 导入和移动：目标校验成功后，文件状态、操作明细和相关关系在同一个数据库事务中提交。
- 删除：文件成功进入备份后，才能提交 `assets.file_state = backup` 和 `backup_entries`。
- 重命名：文件、元数据和快捷方式全部成功时提交成功；部分失败必须保留可恢复的操作明细。
- 数据库事务不能替代文件系统回滚，应用层必须维护补偿步骤。

## 5. 数据库与文件系统重新校准

重新扫描时：

1. 路径和文件指纹一致：更新 `mtime_ns` 等非关键字段。
2. 数据库有记录但文件不存在：标记 `missing`，不直接删除数据库记录。
3. 路径存在但大小、时间或哈希变化：标记 `external_changed`。
4. 文件系统出现新文件：创建新索引记录，初始状态为未检查。
5. 歌单快捷方式外部变化：更新 `playlist_items.state`，不自动删除用户新增内容。

## 6. 迁移与备份

- 每次结构变更必须新增 migration，不得在运行时临时修改表结构。
- migration 执行前备份数据库文件。
- migration 必须有升级测试；破坏性变更还必须有数据保留验证。
- 不允许通过删除数据库重新初始化来处理正式用户数据。

