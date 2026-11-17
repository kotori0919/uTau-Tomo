from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    config_path: Path
    config: dict[str, Any]
    discord_token: str
    llm_url: str
    llm_api_key: str
    embedding_model: str
    summary_model: str
    summary_max_tokens: int
    github_token: str
    state_path: Path
    repo_state_path: Path
    knowledge_threshold: float
    knowledge_margin: float
    dry_run: bool
    browser_headless: bool

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        project_root = (root or Path.cwd()).resolve()
        config_path = Path(
            os.getenv("NEWSROOM_CONFIG_PATH", project_root / "config/newsroom.json")
        )
        if not config_path.is_absolute():
            config_path = project_root / config_path
        config = json.loads(config_path.read_text(encoding="utf-8"))

        def resolve_path(name: str, default: str) -> Path:
            path = Path(os.getenv(name, default))
            return path if path.is_absolute() else project_root / path

        return cls(
            root=project_root,
            config_path=config_path,
            config=config,
            discord_token=os.getenv("NEWSROOM_DISCORD_TOKEN", os.getenv("DISCORD_TOKEN", "")),
            llm_url=os.getenv("NEWSROOM_LLM_URL", os.getenv("LLM_URL", "")).rstrip("/"),
            llm_api_key=os.getenv(
                "NEWSROOM_LLM_API_KEY", os.getenv("LLM_API_KEY", "")
            ),
            embedding_model=os.getenv(
                "NEWSROOM_EMBEDDING_MODEL", "embeddinggemma-2"
            ),
            summary_model=os.getenv(
                "NEWSROOM_SUMMARY_MODEL",
                os.getenv("LLM_MODEL", "deepseek-v4.1-flash"),
            ),
            summary_max_tokens=int(
                os.getenv("NEWSROOM_SUMMARY_MAX_TOKENS", "2048")
            ),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            state_path=resolve_path(
                "NEWSROOM_STATE_PATH", "data/newsroom/state.json"
            ),
            repo_state_path=resolve_path(
                "NEWSROOM_REPO_STATE_PATH", "data/newsroom/repo-state.json"
            ),
            knowledge_threshold=float(
                os.getenv("NEWSROOM_KNOWLEDGE_THRESHOLD", "0.62")
            ),
            knowledge_margin=float(os.getenv("NEWSROOM_KNOWLEDGE_MARGIN", "0.035")),
            dry_run=_env_bool("NEWSROOM_DRY_RUN", False),
            browser_headless=_env_bool("NEWSROOM_BROWSER_HEADLESS", True),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.discord_token and not self.dry_run:
            errors.append("DISCORD_TOKEN or NEWSROOM_DISCORD_TOKEN is required")
        if not self.llm_url:
            errors.append("LLM_URL or NEWSROOM_LLM_URL is required")
        if not self.llm_api_key:
            errors.append("LLM_API_KEY or NEWSROOM_LLM_API_KEY is required")
        required_channels = {
            "technology", "ai", "hardware", "security", "research",
            "github_repo",
            "samsung_display",
            "samsung_semiconductor",
            "threads_choi_openai",
            "threads_security",
            "selection_log",
        }
        missing = required_channels - set(self.config.get("channels", {}))
        if missing:
            errors.append(f"missing channel mappings: {', '.join(sorted(missing))}")
        return errors

    @property
    def channels(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self.config["channels"].items()}

    @property
    def github(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self.config["github"].items()}
