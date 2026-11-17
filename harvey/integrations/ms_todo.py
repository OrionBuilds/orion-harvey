"""Microsoft To Do over Graph: a "Harvey" list of things only the owner can do.

Harvey adds a task when a thread is handed to the owner (a reply it must not answer
itself) so it shows up in To Do on their phone and desktop. Adding is idempotent
by title: the same open task is never created twice.

Every method degrades like the calendar client: no sign-in or no Tasks
permission means None/False/[], never an exception into the run loop.
"""

import logging

from harvey.integrations.ms_auth import MicrosoftAuthError
from harvey.integrations.ms_graph import GraphClient, GraphError

logger = logging.getLogger("harvey.todo")

DEFAULT_LIST = "Harvey"
IMPORTANCE = {"low", "normal", "high"}


class TodoClient:
    def __init__(self, graph: GraphClient | None = None, list_name: str = DEFAULT_LIST):
        self.graph = graph
        self.list_name = list_name or DEFAULT_LIST
        self._list_id = ""

    def is_available(self) -> bool:
        auth = getattr(self.graph, "auth", None)
        return bool(auth and auth.is_authorized() and auth.has_feature("todo"))

    async def ensure_list(self) -> str:
        """Id of the named list, created on first use."""
        if self._list_id:
            return self._list_id
        for row in await self.graph.get_all("/me/todo/lists", limit=200):
            if (row.get("displayName") or "").lower() == self.list_name.lower():
                self._list_id = row["id"]
                return self._list_id
        created = await self.graph.request("POST", "/me/todo/lists", json={"displayName": self.list_name})
        self._list_id = created["id"]
        return self._list_id

    async def open_tasks(self, limit: int = 100) -> list[dict]:
        if not self.is_available():
            return []
        try:
            list_id = await self.ensure_list()
            return await self.graph.get_all(
                f"/me/todo/lists/{list_id}/tasks",
                params={"$filter": "status ne 'completed'", "$top": str(min(limit, 100))}, limit=limit)
        except (GraphError, MicrosoftAuthError, KeyError) as e:
            logger.error(f"To Do read failed: {e}")
            return []

    async def add_task(self, title: str, body: str = "", *, importance: str = "normal") -> dict | None:
        """Create an open task, or return the existing open one with this exact title."""
        if not self.is_available() or not title.strip():
            return None
        importance = importance if importance in IMPORTANCE else "normal"
        try:
            list_id = await self.ensure_list()
            for task in await self.open_tasks():
                if (task.get("title") or "").strip().lower() == title.strip().lower():
                    return task
            return await self.graph.request(
                "POST", f"/me/todo/lists/{list_id}/tasks",
                json={"title": title.strip()[:250], "importance": importance,
                      "body": {"contentType": "text", "content": body[:4000]}})
        except (GraphError, MicrosoftAuthError, KeyError) as e:
            logger.error(f"To Do task not created: {e}")
            return None

    async def complete_task(self, task_id: str) -> bool:
        if not self.is_available() or not task_id:
            return False
        try:
            list_id = await self.ensure_list()
            await self.graph.request("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}",
                                     json={"status": "completed"})
            return True
        except (GraphError, MicrosoftAuthError, KeyError) as e:
            logger.error(f"To Do task not completed: {e}")
            return False
