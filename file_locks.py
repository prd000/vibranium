"""File locking utilities to prevent concurrent writes to shared state files."""
from collections import defaultdict
import asyncio
from pathlib import Path
from typing import Any


class FileLockManager:
    """Per-file asyncio lock manager with alphabetical acquisition ordering."""

    _locks: defaultdict
    _owners: dict

    def __init__(self) -> None:
        """Initialize empty lock and owner dicts."""
        self._locks = defaultdict(asyncio.Lock)
        self._owners = {}

    async def acquire(self, files: list, item_id: str) -> None:
        """Normalize paths, sort alphabetically, acquire each lock in order, record owner."""
        normalized = sorted(Path(f).resolve().as_posix() for f in files)
        for path in normalized:
            await self._locks[path].acquire()
            self._owners[path] = item_id

    def release(self, files: list, item_id: str) -> None:
        """Release and deregister only the locks owned by item_id for these paths."""
        normalized = [Path(f).resolve().as_posix() for f in files]
        for path in normalized:
            if self._owners.get(path) == item_id:
                self._locks[path].release()
                del self._owners[path]

    def get_owner(self, filepath: str) -> "str | None":
        """Normalize filepath and return the current owner item_id, or None."""
        normalized = Path(filepath).resolve().as_posix()
        return self._owners.get(normalized)


WRITE_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "MultiEdit"})


def make_pretooluse_hook(manager: FileLockManager, item_id: str) -> Any:
    """Return an async hook callable that enforces file-lock rules for write tools.

    The returned function matches the PreToolUse HookCallback signature expected
    by the Claude Agent SDK: async (input_data, tool_use_id, context) -> dict.
    """

    async def hook(input_data: Any, tool_use_id: Any, context: Any) -> dict:
        """Inspect the pending tool call; deny if file locked by another item, else allow.

        Extracts tool_name from input_data, returns allow immediately for non-write tools.
        For write tools, extracts file_path from input_data['tool_input'], resolves it
        via manager's normalization, checks ownership, and returns deny or allow dict.
        """
        tool_name = input_data["tool_name"]
        if tool_name not in WRITE_TOOLS:
            return {"type": "allow"}

        file_path = input_data["tool_input"]["file_path"]
        path = Path(file_path).resolve().as_posix()
        owner = manager.get_owner(file_path)

        if owner is None:
            await manager.acquire([file_path], item_id)
            return {"type": "allow"}

        if owner == item_id:
            return {"type": "allow"}

        return {
            "type": "deny",
            "message": f"File {path} is locked by item {owner}. Skip this write and note it in your item log.",
        }

    return hook
