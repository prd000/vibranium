"""Configuration loading and validation for the vibranium CLI."""
import configparser
import tomllib
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
    tests_dir: str = ""                       # blank = auto-discover at runtime
    hide_tests_from_executor: bool = True      # default ON; set False to opt out


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


def discover_tests_dir(project_root: Path) -> "Path | None":
    """Auto-discover the test directory for project_root.

    Discovery order (first match wins):
    1. pyproject.toml [tool.pytest.ini_options].testpaths
    2. pytest.ini [pytest] testpaths
    3. Sibling directories of project_root named tests/test/spec (external-first)
    4. Internal directories named tests/test/spec inside project_root

    Returns the resolved Path, or None if nothing is found.
    """
    root = project_root.resolve()

    # 1. pyproject.toml testpaths
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        testpaths = (
            data.get("tool", {})
            .get("pytest", {})
            .get("ini_options", {})
            .get("testpaths", [])
        )
        if testpaths:
            candidate = Path(testpaths[0])
            candidate = candidate if candidate.is_absolute() else root / candidate
            if candidate.exists():
                return candidate.resolve()

    # 2. pytest.ini testpaths
    pytest_ini = root / "pytest.ini"
    if pytest_ini.exists():
        cfg = configparser.ConfigParser()
        cfg.read(pytest_ini)
        testpaths_str = cfg.get("pytest", "testpaths", fallback="").strip()
        if testpaths_str:
            first = testpaths_str.split()[0]
            candidate = Path(first)
            candidate = candidate if candidate.is_absolute() else root / candidate
            if candidate.exists():
                return candidate.resolve()

    # 3. Convention scan — siblings first (external preferred), then internal
    for name in ("tests", "test", "spec"):
        sibling = root.parent / name
        if sibling.is_dir():
            return sibling.resolve()
    for name in ("tests", "test", "spec"):
        internal = root / name
        if internal.is_dir():
            return internal.resolve()

    return None


def effective_tests_dir(config: VibraniumConfig, project_root: Path) -> "Path | None":
    """Return the resolved tests directory.

    If config.paths.tests_dir is set, resolve and return it.
    Otherwise delegate to discover_tests_dir(project_root).
    Returns None when no tests directory can be found.
    """
    if config.paths.tests_dir:
        p = Path(config.paths.tests_dir)
        resolved = p if p.is_absolute() else project_root / p
        return resolved.resolve()
    return discover_tests_dir(project_root)
