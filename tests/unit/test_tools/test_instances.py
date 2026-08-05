"""
Unit tests for instance tools.

Covers:
- Bug 4: list_instances called GET /virt/instance and got a 404 on TrueNAS
  SCALE 24.10. The decorator produced a generic
  success=False, error_type="TrueNASAPIError" with no signal that the
  endpoint was simply not available on this TrueNAS version. Triage was
  hard: "is this a wrapper bug or a TrueNAS version mismatch?"
- Improved error classification in tool_handler: TypeError/AttributeError/
  KeyError/ValueError -> error_type="WrapperBug"; httpx.* -> error_type=
  "TransportError"; TrueNASError subclasses keep their specific error_type.
"""

import pytest
from unittest.mock import AsyncMock

from truenas_mcp_server.tools.instances import InstanceTools
from truenas_mcp_server.tools.base import tool_handler
from truenas_mcp_server.exceptions import TrueNASAPIError


# ---------------------------------------------------------------------------
# Fixtures
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
def instance_tools(settings):
    return InstanceTools(client=AsyncMock(), settings=settings)


# ---------------------------------------------------------------------------
# Bug 4 — list_instances surfaces 404 as EndpointNotAvailable
# ---------------------------------------------------------------------------


class TestListInstancesBug4:
    @pytest.mark.asyncio
    async def test_404_returns_endpoint_not_available(self, instance_tools):
        """When TrueNAS returns 404 from /virt/instance, surface a structured
        'EndpointNotAvailable' error so callers can distinguish version
        mismatch from wrapper bugs.
        """
        instance_tools.client.get = AsyncMock(
            side_effect=TrueNASAPIError("Client error (404): 404: Not Found")
        )

        result = await instance_tools.list_instances()

        assert result["success"] is False
        assert result["error_type"] == "EndpointNotAvailable"
        assert "/virt/instance" in result["error"]
        assert result["details"]["endpoint"] == "/virt/instance"

    @pytest.mark.asyncio
    async def test_500_propagates_as_truenas_error(self, instance_tools):
        """A genuine 5xx must still surface as a TrueNASAPIError, not be
        misclassified as 'endpoint not available'.
        """
        instance_tools.client.get = AsyncMock(
            side_effect=TrueNASAPIError("Server error (500): upstream broken")
        )

        result = await instance_tools.list_instances()

        assert result["success"] is False
        assert result["error_type"] == "TrueNASAPIError"
        assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_happy_path_still_works(self, instance_tools):
        """Don't break the working path. list_instances still returns the
        expected shape when the endpoint is reachable.
        """
        instance_tools.client.get = AsyncMock(
            return_value=[
                {
                    "id": "vm1",
                    "name": "vm1",
                    "type": "VM",
                    "status": "RUNNING",
                    "cpu": "2",
                    "memory": 4294967296,
                    "autostart": True,
                    "image": "ubuntu/22.04",
                }
            ]
        )

        result = await instance_tools.list_instances()

        assert result["success"] is True
        assert result["metadata"]["total_instances"] == 1
        assert result["instances"][0]["name"] == "vm1"


# ---------------------------------------------------------------------------
# tool_handler exception classification (Ultron improvement #1)
# ---------------------------------------------------------------------------


class _Stub:
    """Empty stand-in for `self` since tool_handler only inspects func name."""


class TestToolHandlerClassification:
    """tool_handler must classify non-TrueNAS exceptions so triage is cheap."""

    @pytest.mark.asyncio
    async def test_typeerror_is_classified_as_wrapper_bug(self):
        """The original Bug 2 TypeError now lands as error_type=WrapperBug
        instead of the generic UnexpectedError.
        """
        @tool_handler
        async def boom(self):
            raise TypeError("'dict' object cannot be interpreted as an integer")

        class _Stub:
            pass

        result = await boom(_Stub())

        assert result["success"] is False
        assert result["error_type"] == "WrapperBug"
        assert result["exception_class"].endswith("TypeError")

    @pytest.mark.asyncio
    async def test_httpx_connecterror_is_classified_as_transport(self):
        import httpx

        @tool_handler
        async def boom(self):
            raise httpx.ConnectError("Connection refused")

        result = await boom(_Stub())

        assert result["success"] is False
        assert result["error_type"] == "TransportError"
        assert "ConnectError" in result["exception_class"]

    @pytest.mark.asyncio
    async def test_truenas_api_error_keeps_its_specific_type(self):
        """TrueNASAPIError must NOT be downgraded to WrapperBug."""
        @tool_handler
        async def boom(self):
            raise TrueNASAPIError("Client error (422): dataset is locked")

        result = await boom(_Stub())

        assert result["success"] is False
        assert result["error_type"] == "TrueNASAPIError"
        assert "exception_class" not in result  # TrueNAS path doesn't set it