"""
Unit tests for the iSCSI sharing tools, focused on the Bug 3 hardening:

- Bug 3 (data-loss-adjacent for Stor1 inventory parity): list_iscsi_targets
  raised TypeError `'< not supported between instances of str and float'` on
  TrueNAS SCALE 24.10 because TrueNAS serialises extent.filesize inconsistently
  across records (sometimes an int, sometimes a string, sometimes a BSON
  extended JSON wrapper). The wrapper called format_size() with mixed types and
  format_size() crashed on the first bad row, taking the whole listing down.

This test pins:
- format_size is now defensive against dict, str, and unparseable values
- list_iscsi_targets survives mixed-shape records without crashing
- list_iscsi_targets surfaces a partial result rather than total failure
"""

import pytest
from unittest.mock import AsyncMock

from truenas_mcp_server.tools.sharing import SharingTools


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
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
def sharing_tools(settings):
    return SharingTools(client=AsyncMock(), settings=settings)


# ---------------------------------------------------------------------------
# format_size defensive behaviour
# ---------------------------------------------------------------------------


class TestFormatSize:
    """format_size must tolerate the BSON / stringified number shapes."""

    def test_int_bytes(self, sharing_tools):
        assert sharing_tools.format_size(1024) == "1.00 KB"

    def test_dict_bson_parsed(self, sharing_tools):
        # BSON extended JSON: {"parsed": <int>}
        assert sharing_tools.format_size({"parsed": 1024}) == "1.00 KB"

    def test_dict_bson_value(self, sharing_tools):
        # BSON shape: {"value": <int>}
        assert sharing_tools.format_size({"value": 2048}) == "2.00 KB"

    def test_stringified_number(self, sharing_tools):
        # Some TrueNAS endpoints serialise numbers as strings.
        assert sharing_tools.format_size("1024") == "1.00 KB"

    def test_none(self, sharing_tools):
        assert sharing_tools.format_size(None) == "unknown"

    def test_garbage_string(self, sharing_tools):
        assert sharing_tools.format_size("not a number") == "unknown"

    def test_empty_dict(self, sharing_tools):
        assert sharing_tools.format_size({}) == "unknown"

    def test_negative(self, sharing_tools):
        assert sharing_tools.format_size(-100) == "unknown"


# ---------------------------------------------------------------------------
# list_iscsi_targets Bug 3 hardening
# ---------------------------------------------------------------------------


class TestListIscsiTargetsBug3:
    """list_iscsi_targets must survive mixed-shape records without crashing."""

    @pytest.mark.asyncio
    async def test_returns_partial_result_on_mixed_filesize_shapes(self, sharing_tools):
        """Some extents have int filesize, others have BSON-dict filesize.

        The wrapper must not crash; the bad row should appear with
        filesize='unknown' and the rest of the data must be intact.
        """
        sharing_tools.client.get = AsyncMock(side_effect=[
            # /iscsi/target
            [
                {
                    "id": 1,
                    "name": "iqn.test:target1",
                    "alias": "tgt1",
                    "mode": "ISCSI",
                    "groups": [],
                }
            ],
            # /iscsi/extent — mixed shape on purpose
            [
                {"id": 10, "name": "ext-int", "type": "FILE", "path": "/mnt/stor1/x",
                 "filesize": 1024, "enabled": True},
                {"id": 11, "name": "ext-bson", "type": "FILE", "path": "/mnt/stor1/y",
                 "filesize": {"parsed": 2048}, "enabled": True},
                {"id": 12, "name": "ext-str", "type": "FILE", "path": "/mnt/stor1/z",
                 "filesize": "4096", "enabled": True},
                {"id": 13, "name": "ext-garbage", "type": "FILE", "path": "/mnt/stor1/q",
                 "filesize": {"weird": "shape"}, "enabled": True},
            ],
            # /iscsi/targetextent — first target gets the first three extents
            [
                {"id": 100, "target": 1, "extent": 10},
                {"id": 101, "target": 1, "extent": 11},
                {"id": 102, "target": 1, "extent": 12},
                {"id": 103, "target": 1, "extent": 13},
            ],
        ])

        result = await sharing_tools.list_iscsi_targets()

        assert result["success"] is True
        # Every extent row must come through; none should have crashed the call.
        target = result["targets"][0]
        extents = target["extents"]
        assert len(extents) == 4
        # The good rows get formatted sizes; the bad row gets "unknown".
        sizes = {e["name"]: e["filesize"] for e in extents}
        assert sizes["ext-int"] == "1.00 KB"
        assert sizes["ext-bson"] == "2.00 KB"
        assert sizes["ext-str"] == "4.00 KB"
        assert sizes["ext-garbage"] == "unknown"
        # Counts survive.
        assert result["metadata"]["total_targets"] == 1
        assert result["metadata"]["total_extents"] == 4

    @pytest.mark.asyncio
    async def test_skips_unhashable_extent_ids(self, sharing_tools):
        """If an extent's id is a BSON dict (unhashable), the listing must
        still come back instead of crashing with TypeError on dict.__hash__.
        """
        sharing_tools.client.get = AsyncMock(side_effect=[
            [{"id": 1, "name": "tgt", "alias": "t", "mode": "ISCSI", "groups": []}],
            [
                {"id": {"$oid": "abc"}, "name": "ext-bson-id", "type": "FILE",
                 "path": "/x", "filesize": 1024, "enabled": True},
            ],
            # targetextent pointing at the (unhashable) extent id
            [{"id": 1, "target": 1, "extent": {"$oid": "abc"}}],
        ])

        # No exception — that's the assertion.
        result = await sharing_tools.list_iscsi_targets()
        assert result["success"] is True
        assert result["metadata"]["total_targets"] == 1

    @pytest.mark.asyncio
    async def test_skips_malformed_targetextent_records(self, sharing_tools):
        """Records with missing target/extent fields must be skipped, not crash."""
        sharing_tools.client.get = AsyncMock(side_effect=[
            [{"id": 1, "name": "tgt", "alias": "t", "mode": "ISCSI", "groups": []}],
            [{"id": 10, "name": "ext", "type": "FILE", "path": "/x",
              "filesize": 1024, "enabled": True}],
            # All malformed targetextents.
            [
                {"id": 1},  # missing target & extent
                {"target": 1},  # missing extent
                {"extent": 10},  # missing target
            ],
        ])

        result = await sharing_tools.list_iscsi_targets()
        assert result["success"] is True
        # No extents attached because all targetextents were malformed.
        assert result["targets"][0]["extents"] == []