from __future__ import annotations


SONGS = [
    {"title": "Dear Leslie", "artist": "古巨基", "duration": "04:18", "format": "MP3", "size": "9.6 MB", "status": "未检查"},
    {"title": "晴天", "artist": "周杰伦", "duration": "04:29", "format": "FLAC", "size": "31.2 MB", "status": "已匹配"},
    {"title": "珊瑚海", "artist": "周杰伦、梁心颐", "duration": "04:14", "format": "MP3", "size": "10.1 MB", "status": "已匹配"},
    {"title": "富士山下", "artist": "陈奕迅", "duration": "04:19", "format": "M4A", "size": "8.4 MB", "status": "可能匹配"},
    {"title": "爱与诚", "artist": "古巨基", "duration": "03:44", "format": "MP3", "size": "8.1 MB", "status": "已有内嵌歌词"},
    {"title": "七里香", "artist": "周杰伦", "duration": "04:59", "format": "FLAC", "size": "37.8 MB", "status": "冲突"},
    {"title": "月半小夜曲", "artist": "李克勤", "duration": "04:49", "format": "FLAC", "size": "34.6 MB", "status": "已匹配"},
    {"title": "十年", "artist": "陈奕迅", "duration": "03:25", "format": "MP3", "size": "7.9 MB", "status": "未匹配"},
    {"title": "海阔天空", "artist": "Beyond", "duration": "05:24", "format": "MP3", "size": "12.3 MB", "status": "已匹配"},
    {"title": "好久不见", "artist": "陈奕迅", "duration": "04:10", "format": "M4A", "size": "8.9 MB", "status": "可能匹配"},
    {"title": "光年之外", "artist": "G.E.M.邓紫棋", "duration": "03:55", "format": "FLAC", "size": "29.7 MB", "status": "未检查"},
    {"title": "大鱼", "artist": "周深", "duration": "05:13", "format": "MP3", "size": "11.8 MB", "status": "已匹配"},
    {"title": "单车", "artist": "陈奕迅", "duration": "03:26", "format": "MP3", "size": "7.8 MB", "status": "已匹配"},
    {"title": "必杀技", "artist": "古巨基", "duration": "03:51", "format": "MP3", "size": "8.7 MB", "status": "未匹配"},
]

LYRICS = [
    {"title": "Dear Leslie", "artist": "古巨基", "format": "LRC", "size": "32 KB", "status": "已匹配"},
    {"title": "晴天", "artist": "周杰伦", "format": "LRC", "size": "28 KB", "status": "已匹配"},
    {"title": "富士山下", "artist": "陈奕迅", "format": "LRC", "size": "25 KB", "status": "可能匹配"},
    {"title": "未知歌词", "artist": "未知", "format": "LRC", "size": "18 KB", "status": "未匹配"},
    {"title": "七里香", "artist": "周杰伦", "format": "LRC", "size": "30 KB", "status": "冲突"},
    {"title": "月半小夜曲", "artist": "李克勤", "format": "LRC", "size": "26 KB", "status": "已匹配"},
    {"title": "海阔天空", "artist": "Beyond", "format": "LRC", "size": "35 KB", "status": "已匹配"},
    {"title": "大鱼", "artist": "周深", "format": "LRC", "size": "24 KB", "status": "未检查"},
]

PLAYLISTS = ["我喜欢的", "粤语", "通勤", "怀旧", "古巨基"]

PLAYLIST_MAP = {
    "我喜欢的": [0, 1, 3, 4, 6, 10, 11],
    "粤语": [0, 3, 4, 6, 7, 8, 9, 12, 13],
    "通勤": [1, 2, 5, 10, 11],
    "怀旧": [1, 5, 6, 7, 8],
    "古巨基": [0, 4, 13],
}

IMPORT_AUDIO = [
    ("晴天 - 周杰伦 [320K].mp3", "名称待确认"),
    ("Dear Leslie-古巨基#f9o8c.mp3", "可导入"),
    ("月半小夜曲-李克勤.flac", "可导入"),
    ("七里香-周杰伦.flac", "目标文件已存在"),
    ("海阔天空-Beyond.mp3", "重复文件"),
    ("富士山下-陈奕迅.m4a", "内容冲突"),
    ("好久不见-陈奕迅.m4a", "可导入"),
    ("大鱼-周深.mp3", "等待处理"),
    ("光年之外-G.E.M.邓紫棋.flac", "校验中"),
    ("单车-陈奕迅.mp3", "导入成功"),
    ("必杀技-古巨基.mp3", "已跳过"),
    ("未知音频.aac", "导入失败"),
]

IMPORT_LYRICS = [
    ("晴天-周杰伦.lrc", "可导入"),
    ("富士山下-陈奕迅.lrc", "可导入"),
    ("周杰伦-晴天.lrc", "名称待确认"),
    ("七里香-周杰伦.lrc", "内容冲突"),
    ("海阔天空-Beyond.lrc", "重复文件"),
    ("未知歌词.lrc", "等待处理"),
]

RENAME_ROWS = [
    (True, "Dear Leslie-古巨基#f9o8c.mp3", "Dear Leslie-古巨基.mp3", "ID3", "可自动处理"),
    (True, "晴天 - 周杰伦 [320K].mp3", "晴天-周杰伦.mp3", "文件名", "可自动处理"),
    (False, "未知文件.mp3", "—", "无法识别", "待手动确认"),
    (False, "示例.mp3", "—", "ID3 与文件名不一致", "冲突"),
    (True, "月半小夜曲-李克勤 (1).flac", "月半小夜曲-李克勤.flac", "文件名", "可自动处理"),
]

HISTORY = [
    ("2026-07-15 10:42", "导入音频", 9, 1, "部分成功"),
    ("2026-07-15 10:26", "导入歌词", 12, 0, "成功"),
    ("2026-07-14 21:08", "批量重命名", 18, 2, "部分成功"),
    ("2026-07-14 20:51", "歌词匹配", 24, 0, "成功"),
    ("2026-07-13 18:12", "创建歌单", 1, 0, "成功"),
    ("2026-07-13 18:14", "添加到歌单", 8, 0, "成功"),
    ("2026-07-12 16:30", "删除音乐", 3, 0, "成功"),
    ("2026-07-11 09:05", "撤销导入", 6, 0, "成功"),
]
