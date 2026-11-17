from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


class AtomicJsonState:
    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self.default = default

    def load(self) -> tuple[dict[str, Any], bool]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state root must be an object")
            return data, True
        except FileNotFoundError:
            log.info("State does not exist yet: %s", self.path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.error("Ignoring unreadable state %s: %s", self.path, exc)
        return deepcopy(self.default), False

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["updatedAt"] = now_kst()
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def news_state_default(seed_urls: list[str]) -> dict[str, Any]:
    return {
        "updatedAt": now_kst(),
        "posted": list(dict.fromkeys(seed_urls))[-300:],
        "initializedSources": [],
        "emptyRuns": {},
    }


def repo_state_default(repo: str, branch: str) -> dict[str, Any]:
    return {
        "repo": repo,
        "branch": branch,
        "updatedAt": now_kst(),
        "initialized": False,
        "seenCommits": [],
        "seenTags": [],
    }
