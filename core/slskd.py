import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class SlskdClient:
    def __init__(self, host: str, api_key: str):
        self.base = f"{host}/api/v0"
        self.headers = {"X-API-Key": api_key}
        self.client = httpx.AsyncClient(
            base_url=self.base,
            headers=self.headers,
            timeout=60.0,
        )

    async def search(self, query: str, timeout_ms: int = 30000) -> str:
        """Start a search. Returns the search ID."""
        resp = await self.client.post("/searches", json={
            "searchText": query,
            "searchTimeout": timeout_ms,
            "filterResponses": True,
            "maximumPeerQueueLength": 50,
            "minimumPeerUploadSpeed": 0,
            "minimumResponseFileCount": 1,
            "responseLimit": 100,
        })
        resp.raise_for_status()
        data = resp.json()
        search_id = data["id"]
        logger.info(f"Search started: '{query}' -> {search_id}")
        return search_id

    async def get_search_state(self, search_id: str) -> dict:
        """Get search state. Returns dict with 'state' field."""
        resp = await self.client.get(
            f"/searches/{search_id}",
            params={"includeResponses": False},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_search_responses(self, search_id: str) -> list[dict]:
        """Get search responses (list of user results with files)."""
        resp = await self.client.get(f"/searches/{search_id}/responses")
        resp.raise_for_status()
        return resp.json()

    async def wait_for_search(
        self,
        search_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 45.0,
    ) -> list[dict]:
        """Poll until search completes, then return responses."""
        elapsed = 0.0
        while elapsed < max_wait:
            state = await self.get_search_state(search_id)
            state_str = str(state.get("state", ""))
            logger.info(f"Search {search_id}: state={state_str}, elapsed={elapsed:.0f}s")
            # Search is complete when state is no longer "InProgress"
            if state_str != "InProgress":
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        responses = await self.get_search_responses(search_id)
        logger.info(f"Search {search_id}: got {len(responses)} responses")
        return responses

    async def enqueue_download(self, username: str, files: list[dict]) -> None:
        """Enqueue files for download from a user."""
        logger.info(f"Enqueueing {len(files)} files from {username}")
        resp = await self.client.post(
            f"/transfers/downloads/{username}",
            json=files,
        )
        resp.raise_for_status()

    async def get_all_downloads(self) -> list[dict]:
        """Get all downloads."""
        resp = await self.client.get("/transfers/downloads/")
        resp.raise_for_status()
        return resp.json()

    async def get_user_downloads(self, username: str) -> list[dict]:
        """Get downloads for a specific user.
        Returns a list of directory objects, each with a 'files' list.
        """
        resp = await self.client.get(f"/transfers/downloads/{username}")
        resp.raise_for_status()
        data = resp.json()
        # The API can return either:
        # - A list of directory objects directly
        # - A dict with a "directories" key
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("directories", [])
        return []

    async def delete_search(self, search_id: str) -> None:
        """Delete a completed search."""
        try:
            await self.client.delete(f"/searches/{search_id}")
        except httpx.HTTPStatusError:
            pass

    async def close(self):
        await self.client.aclose()
