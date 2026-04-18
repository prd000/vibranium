"""Configuration loading and validation for the vibranium CLI."""
from pathlib import Path

from pydantic import BaseModel, Field
import yaml


class ModelsConfig(BaseModel):
    """Model name strings for each agent role."""
    executor: str = "opusplan"
    evaluator: str = "sonnet"
    fix_executor: str = "opusplan"
    refactor_analyzer: str = "opus"


class LimitsConfig(BaseModel):
    """Numeric limits controlling agent concurrency and costs."""
    max_fix_attempts: int = 3
    max_concurrent_fix_agents: int = 3
    max_turns_per_agent: int = 200
    max_total_cost_usd: float = 50.00
    max_cost_per_item_usd: float = 5.00


class PathsConfig(BaseModel):
    """Filesystem paths used at runtime."""
    plan: str = "./plan.md"
    state_dir: str = "./state"
    project_root: str = "./"


class EvaluatorConfig(BaseModel):
    """Evaluator pass/fail threshold settings."""
    severity_threshold: str = "major"
    max_minor_issues_before_fail: int = 3


class UiConfig(BaseModel):
    """WebSocket server and logging settings."""
    websocket_port: int = 8765
    log_level: str = "info"


class ShutdownConfig(BaseModel):
    """Shutdown behaviour when an item is flagged."""
    on_flagged_item: str = "continue"


class VibraniumConfig(BaseModel):
    """Root configuration model composed of all sub-configs."""
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    evaluator: EvaluatorConfig = Field(default_factory=EvaluatorConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)


def load_config(path: Path) -> VibraniumConfig:
    """Read YAML at path and return VibraniumConfig; return VibraniumConfig() if file does not exist."""
    if not path.exists():
        return VibraniumConfig()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return VibraniumConfig(**data)


def find_config(cwd: Path) -> Path | None:
    """Walk up from cwd looking for vibranium_config.yaml; return its Path or None if not found."""
    current = cwd.resolve()
    while True:
        candidate = current / "vibranium_config.yaml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent
