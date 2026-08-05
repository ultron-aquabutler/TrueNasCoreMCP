"""
Unit tests for snapshot tools.

Covers:
- Bug 1 (data-loss-adjacent): create_snapshot was unconditionally returning
  success=True regardless of the TrueNAS API response, fabricating a future
  timestamp. When the upstream POST silently failed (or returned a job that
  later failed), callers believed they had a backup.
- Bug 2: list_snapshots raised TypeError "dict object cannot be interpreted as
  an integer" because TrueNAS SCALE returns properties.creation as
  {"$date": <epoch_ms>} (BSON extended JSON), not as an int. The pre-existing
  code passed snap[properties][creation][parsed] straight into
  datetime.fromtimestamp(), which only accepts ints/floats.

These tests pin both behaviours down so the regressions cannot reappear.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from truenas_mcp_server.tools.snapshots import SnapshotTools


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    """Minimal settings — destructive ops enabled so we can exercise paths."""
    from truenas_mcp_server.config.settings import Settings
    from pydantic import SecretStr

    return Settings(
        truenas_url="https://truenas.local",
        truenas_api_key=SecretStr("test-api-key-1234567890"),
        truenas_verify_ssl=False,
        environment="development",
        log_level="DEBUG",
        enable_destructive_operations=True,
        http_timeout=30.0,
        http_pool_connections=10,
        http_pool_maxsize=20,
        http_max_retries=3,
    )


@pytest.fixture
def snapshot_tools(settings):
    """SnapshotTools wired against a mock client (no live HTTP)."""
    tools = SnapshotTools(client=AsyncMock(), settings=settings)
    return tools


# ---------------------------------------------------------------------------
# Bug 1 — create_snapshot must not fabricate success
# ---------------------------------------------------------------------------


class TestCreateSnapshot:
    """Pin the create_snapshot success-fabrication regression closed."""

    @pytest.mark.asyncio
    async def test_returns_success_when_api_returns_job_id(self, snapshot_tools):
        """Happy path: TrueNAS SCALE POST /zfs/snapshot returns 202 with job_id.

        The wrapper must surface that job_id (and a verifiable shape) rather
        than a fabricated datetime.
        """
        # TrueNAS SCALE returns a job object like this on snapshot creation.
        snapshot_tools.client.post = AsyncMock(
            return_value={"job_id": 42, "result": None}
        )

        result = await snapshot_tools.create_snapshot(
            dataset="tank/data", name="manual-20260805-040000"
        )

        assert result["success"] is True
        assert result["snapshot"]["name"] == "tank/data@manual-20260805-040000"
        assert result["snapshot"]["dataset"] == "tank/data"
        # 'created' must come from the API response (or be explicitly None) —
        # never a fabricated datetime.now() value.
        assert "created" in result["snapshot"]
        # The wrapper should pass through the upstream job id so callers can
        # verify the job actually completed.
        assert result.get("job_id") == 42

    @pytest.mark.asyncio
    async def test_returns_failure_when_post_raises(self, snapshot_tools):
        """If client.post raises (network, 4xx, 5xx), surface success=False.

        Reproduces the live failure mode from 2026-08-03: TrueNAS rejected the
        request but the wrapper returned success=True with a future timestamp.
        """
        from truenas_mcp_server.exceptions import TrueNASAPIError

        snapshot_tools.client.post = AsyncMock(
            side_effect=TrueNASAPIError("Client error (422): dataset is locked")
        )

        result = await snapshot_tools.create_snapshot(
            dataset="Stor1/Nextcloud", name="ultron-probe-2026-08-05"
        )

        assert result["success"] is False
        assert "dataset is locked" in result["error"]
        # Specifically: NO fabricated 'created' timestamp.
        assert "created" not in result.get("snapshot", {}) or result.get("snapshot") is None

    @pytest.mark.asyncio
    async def test_returns_failure_when_post_returns_error_dict(self, snapshot_tools):
        """Some TrueNAS endpoints return 200 with an inline error payload.

        The wrapper must inspect the body and refuse to claim success.
        """
        snapshot_tools.client.post = AsyncMock(
            return_value={
                "error": True,
                "reason": "Snapshot name conflicts with existing snapshot",
            }
        )

        result = await snapshot_tools.create_snapshot(
            dataset="tank/data", name="conflict"
        )

        assert result["success"] is False
        assert "conflicts" in result["error"].lower()


# ---------------------------------------------------------------------------
# Bug 2 — list_snapshots must handle BSON timestamps
# ---------------------------------------------------------------------------


class TestListSnapshotsTimestamps:
    """Pin the BSON-timestamp TypeError regression closed."""

    @pytest.mark.asyncio
    async def test_handles_int_creation(self, snapshot_tools):
        """Legacy path: TrueNAS CORE returns creation.parsed as a float epoch."""
        snapshot_tools.client.get = AsyncMock(
            return_value=[
                {
                    "name": "tank/data@auto-2024-01-01",
                    "properties": {
                        "creation": {"parsed": "2024-01-01T00:00:00+00:00"},
                    },
                }
            ]
        )

        result = await snapshot_tools.list_snapshots()

        assert result["success"] is True
        assert result["snapshots"][0]["name"] == "tank/data@auto-2024-01-01"
        # 'created' is preserved as parsed; no TypeError.
        assert "created_human" in result["snapshots"][0]

    @pytest.mark.asyncio
    async def test_handles_bson_dict_creation(self, snapshot_tools):
        """TrueNAS SCALE returns creation.parsed as a BSON extended JSON dict.

        Without unwrapping, datetime.fromtimestamp(dict) raises:
            TypeError: 'dict' object cannot be interpreted as an integer
        """
        snapshot_tools.client.get = AsyncMock(
            return_value=[
                {
                    "name": "tank/data@auto-2024-06-01",
                    "properties": {
                        # BSON-style: {"$date": <epoch_ms>}
                        "creation": {"parsed": {"$date": 1717200000000}},
                    },
                }
            ]
        )

        result = await snapshot_tools.list_snapshots()

        assert result["success"] is True
        snap = result["snapshots"][0]
        # 'created_human' must be a real ISO string, not a TypeError swallowed.
        assert snap["created_human"].startswith("2024-")
        # The wrapper should preserve the underlying epoch (in seconds, not ms).
        assert snap["created"] == 1717200000  # 1717200000000 ms / 1000

    @pytest.mark.asyncio
    async def test_handles_missing_creation(self, snapshot_tools):
        """A snapshot with no creation property must not crash."""
        snapshot_tools.client.get = AsyncMock(
            return_value=[
                {
                    "name": "tank/data@legacy",
                    "properties": {},
                }
            ]
        )

        result = await snapshot_tools.list_snapshots()

        assert result["success"] is True
        assert result["snapshots"][0]["created"] is None
        # No 'created_human' key on missing timestamp.
        assert "created_human" not in result["snapshots"][0]

    @pytest.mark.asyncio
    async def test_sort_does_not_crash_on_mixed_timestamp_shapes(self, snapshot_tools):
        """Sorting by .get('created', 0) must work when some are dicts, some ints.

        The sort lambda is `key=lambda x: x.get('created', 0)` — if 'created'
        holds a dict, sorting will compare dicts (Python 3 TypeError on <).
        """
        snapshot_tools.client.get = AsyncMock(
            return_value=[
                {
                    "name": "tank/data@old",
                    "properties": {
                        "creation": {"parsed": {"$date": 1577836800000}},
                    },
                },
                {
                    "name": "tank/data@new",
                    "properties": {
                        "creation": {"parsed": 1700000000},  # int-shaped
                    },
                },
            ]
        )

        result = await snapshot_tools.list_snapshots()

        assert result["success"] is True
        # Newest first.
        assert result["snapshots"][0]["name"].endswith("@new")


# ---------------------------------------------------------------------------
# create_snapshot_task — also captures-and-ignores the API response.
# Same Bug 1 class. Pin it down too.
# ---------------------------------------------------------------------------


class TestCreateSnapshotTask:
    @pytest.mark.asyncio
    async def test_returns_failure_when_post_raises(self, snapshot_tools):
        """create_snapshot_task also fabricates success on failure."""
        from truenas_mcp_server.exceptions import TrueNASAPIError

        snapshot_tools.client.post = AsyncMock(
            side_effect=TrueNASAPIError("Client error (422): invalid schedule")
        )

        result = await snapshot_tools.create_snapshot_task(
            dataset="tank/data",
            schedule={"minute": "0", "hour": "*/4", "dom": "*", "month": "*", "dow": "*"},
            retention=7,
        )

        assert result["success"] is False
        assert "invalid schedule" in result["error"]