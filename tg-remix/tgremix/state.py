"""记录哪些视频已经下载 / 已经处理，避免重复"""
from __future__ import annotations

import json
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"downloaded": {}, "processed": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                print(f"⚠️ {path.name} 读取失败，将重新记录")
        self._processed = set(self.data["processed"])

    def is_downloaded(self, channel: str, msg_id: int) -> bool:
        return msg_id in self.data["downloaded"].get(channel, [])

    def mark_downloaded(self, channel: str, msg_id: int) -> None:
        ids = self.data["downloaded"].setdefault(channel, [])
        if msg_id not in ids:
            ids.append(msg_id)
            del ids[:-5000]

    def is_processed(self, key: str) -> bool:
        return key in self._processed

    def mark_processed(self, key: str) -> None:
        if key not in self._processed:
            self._processed.add(key)
            self.data["processed"].append(key)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
