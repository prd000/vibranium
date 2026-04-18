"""WebSocket server for streaming live pipeline status to external clients."""
import asyncio
import json
import logging
from typing import Any, Callable

import websockets

from vibranium.models import ProgressTotals, Verdict

logger = logging.getLogger(__name__)


class WsServer:
    """Asyncio WebSocket broadcast server for live pipeline status."""

    def __init__(self, port: int) -> None:
        """Store port; initialize _clients, _state_getter, running, _server."""
        self.port = port
        self._clients: set = set()
        self._state_getter = None
        self.running = False
        self._server = None

    def set_state_getter(self, fn: Callable[[], Any]) -> None:
        """Store fn as _state_getter; called by Orchestrator to wire progress snapshot."""
        self._state_getter = fn

    async def start(self) -> None:
        """Await websockets.serve; store server; set running=True. On OSError: log warning, return."""
        try:
            self._server = await websockets.serve(self._handler, "localhost", self.port)
            self.running = True
        except OSError as exc:
            logger.warning("WsServer: cannot bind port %d: %s", self.port, exc)

    async def _handler(self, ws: Any) -> None:
        """Add ws to _clients; send full_state if _state_getter set; drain incoming until disconnect; remove from _clients in finally."""
        self._clients.add(ws)
        try:
            if self._state_getter is not None:
                await ws.send(json.dumps({"type": "full_state", "state": self._state_getter()}, default=str))
            async for _ in ws:
                pass
        finally:
            self._clients.discard(ws)

    async def broadcast(self, event: dict) -> None:
        """Send json.dumps(event, default=str) to all current clients; silently remove on any exception."""
        for client in self._clients.copy():
            try:
                await client.send(json.dumps(event, default=str))
            except Exception:
                self._clients.discard(client)

    def emit_item_status(self, item_id: str, status: str) -> None:
        """Fire-and-forget broadcast of an item_status event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "item_status",
            "item_id": item_id,
            "status": status,
        }
        asyncio.create_task(self.broadcast(event))

    def emit_eval_result(self, item_id: str, verdict: Verdict) -> None:
        """Fire-and-forget broadcast of an eval_result event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "eval_result",
            "item_id": item_id,
            "passed": verdict.passed,
            "issues": [iss.model_dump() for iss in verdict.issues],
        }
        asyncio.create_task(self.broadcast(event))

    def emit_fix_attempt(self, item_id: str, attempt: int, max_attempts: int) -> None:
        """Fire-and-forget broadcast of a fix_attempt event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "fix_attempt",
            "item_id": item_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        asyncio.create_task(self.broadcast(event))

    def emit_flagged(self, item_id: str, reason: str, issues: list) -> None:
        """Fire-and-forget broadcast of a flagged event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "flagged",
            "item_id": item_id,
            "reason": reason,
            "issues": issues,
        }
        asyncio.create_task(self.broadcast(event))

    def emit_cost_update(self, total: float, limit: float) -> None:
        """Fire-and-forget broadcast of a cost_update event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "cost_update",
            "total_cost_usd": total,
            "limit_usd": limit,
        }
        asyncio.create_task(self.broadcast(event))

    def emit_agent_log(self, item_id: str, agent_type: str, message: str) -> None:
        """Fire-and-forget broadcast of an agent_log event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "agent_log",
            "item_id": item_id,
            "agent_type": agent_type,
            "message": message,
        }
        asyncio.create_task(self.broadcast(event))

    def emit_complete(self, totals: ProgressTotals, duration_seconds: float) -> None:
        """Fire-and-forget broadcast of a complete event; no-op if not running."""
        if not self.running:
            return
        event = {
            "type": "complete",
            "items_complete": totals.items_complete,
            "items_flagged": totals.items_flagged,
            "items_pending": totals.items_pending,
            "total_cost_usd": totals.total_cost_usd,
            "duration_seconds": duration_seconds,
        }
        asyncio.create_task(self.broadcast(event))
