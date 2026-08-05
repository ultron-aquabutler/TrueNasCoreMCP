"""
Base class and utilities for MCP tools
"""

import logging
from functools import wraps
from typing import Any, Dict, List, Optional, Callable, Tuple
from abc import ABC, abstractmethod

from ..client import TrueNASClient
from ..config import Settings
from ..exceptions import TrueNASError
from ..models.base import ResponseModel

logger = logging.getLogger(__name__)


def tool_handler(func: Callable) -> Callable:
    """
    Decorator for handling tool execution with consistent error handling and logging

    Wraps tool methods to:
    - Log execution start/end
    - Handle exceptions consistently
    - Return standardized responses
    - Classify exceptions so callers can distinguish wrapper bugs (TypeError,
      AttributeError, etc.) from TrueNAS API errors (TrueNASError subclasses)
      and from transport failures (httpx.*).
    """
    @wraps(func)
    async def wrapper(self, *args, **kwargs) -> Dict[str, Any]:
        tool_name = func.__name__
        logger.info(f"Executing tool: {tool_name}")

        try:
            # Execute the tool function
            result = await func(self, *args, **kwargs)

            # Ensure we have a dict response
            if isinstance(result, ResponseModel):
                response = result.dict()
            elif isinstance(result, dict):
                response = result
            else:
                response = {"success": True, "data": result}

            logger.info(f"Tool {tool_name} completed successfully")
            return response

        except TrueNASError as e:
            logger.error(f"Tool {tool_name} failed with TrueNAS error: {e.message}")
            return {
                "success": False,
                "error": e.message,
                "error_type": e.__class__.__name__,
                "details": e.details,
            }
        except Exception as e:
            # Classify non-TrueNAS exceptions so triage can tell wrapper bugs
            # apart from transport errors at a glance. (Improvement suggested
            # by Ultron 2026-08-03 audit.)
            exc_class = e.__class__.__name__
            exc_module = e.__class__.__module__
            logger.exception(f"Tool {tool_name} failed with unexpected error")
            # Heuristic: TypeError, AttributeError, KeyError, ValueError are
            # almost always wrapper bugs (the TrueNAS API returned something
            # the wrapper didn't expect). httpx errors are transport.
            if exc_class in ("TypeError", "AttributeError", "KeyError", "ValueError"):
                error_type = "WrapperBug"
            elif exc_module.startswith("httpx"):
                error_type = "TransportError"
            else:
                error_type = "UnexpectedError"
            return {
                "success": False,
                "error": str(e),
                "error_type": error_type,
                "exception_class": f"{exc_module}.{exc_class}",
            }

    return wrapper


class BaseTool(ABC):
    """
    Base class for all MCP tools

    Provides common functionality for tool implementations including:
    - Client management
    - Configuration access
    - Logging setup
    - Error handling utilities
    - Pagination support
    """

    # Pagination defaults
    DEFAULT_LIMIT = 100
    MAX_LIMIT = 500

    def __init__(self, client: Optional[TrueNASClient] = None, settings: Optional[Settings] = None):
        """
        Initialize the tool
        
        Args:
            client: Optional TrueNASClient instance
            settings: Optional Settings instance
        """
        self.client = client
        self.settings = settings
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._initialized = False
    
    async def initialize(self):
        """Initialize the tool (connect client, etc.)"""
        if not self._initialized:
            if self.client is None:
                from ..client import get_client
                self.client = await get_client()
            
            if self.settings is None:
                from ..config import get_settings
                self.settings = get_settings()
            
            self._initialized = True
            self.logger.debug(f"{self.__class__.__name__} initialized")
    
    async def ensure_initialized(self):
        """Ensure the tool is initialized before use"""
        if not self._initialized:
            await self.initialize()
    
    @abstractmethod
    def get_tool_definitions(self) -> list:
        """
        Get MCP tool definitions for this tool class
        
        Returns:
            List of tool definitions for MCP registration
        """
        pass
    
    def format_size(self, size_bytes) -> str:
        """Format bytes as human-readable size.

        Tolerates the upstream BSON extended JSON wrapper shape
        (``{"value": "1K", "parsed": 1024}``), bare strings (``"1024"``),
        and ``None``. Any value that can't be coerced to a non-negative
        number is returned as ``"unknown"`` rather than raising — one bad
        record must not take down an entire list_iscsi_targets (etc.).
        """
        # Unwrap BSON extended-JSON shapes before coercing.
        if isinstance(size_bytes, dict):
            for key in ("parsed", "rawvalue", "value"):
                if key in size_bytes:
                    size_bytes = size_bytes[key]
                    break
            else:
                return "unknown"

        if size_bytes is None:
            return "unknown"
        if isinstance(size_bytes, str):
            # Some TrueNAS endpoints serialise numbers as strings. Try to
            # parse; fall back to "unknown" on garbage.
            try:
                size_bytes = int(size_bytes)
            except (ValueError, TypeError):
                return "unknown"
        if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
            return "unknown"

        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} EB"
    
    def parse_size(self, size_str: str) -> int:
        """
        Parse human-readable size to bytes
        
        Args:
            size_str: Size string (e.g., "10G", "500M")
            
        Returns:
            Size in bytes
        """
        units = {
            'B': 1,
            'K': 1024,
            'KB': 1024,
            'M': 1024**2,
            'MB': 1024**2,
            'G': 1024**3,
            'GB': 1024**3,
            'T': 1024**4,
            'TB': 1024**4,
            'P': 1024**5,
            'PB': 1024**5
        }
        
        size_str = size_str.upper().strip()
        
        # Find the unit
        unit = None
        for u in sorted(units.keys(), key=len, reverse=True):
            if size_str.endswith(u):
                unit = u
                number_str = size_str[:-len(u)].strip()
                break
        
        if unit is None:
            # No unit specified, assume bytes
            return int(float(size_str))
        
        try:
            number = float(number_str)
            return int(number * units[unit])
        except ValueError:
            raise ValueError(f"Invalid size format: {size_str}")
    
    def validate_required_fields(self, data: Dict[str, Any], required: list) -> bool:
        """
        Validate that required fields are present in data

        Args:
            data: Data dictionary to validate
            required: List of required field names

        Returns:
            True if all required fields are present

        Raises:
            ValueError: If any required fields are missing
        """
        missing = [field for field in required if field not in data or data[field] is None]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")
        return True

    def apply_pagination(
        self,
        items: List[Any],
        limit: int = DEFAULT_LIMIT,
        offset: int = 0
    ) -> Tuple[List[Any], Dict[str, Any]]:
        """
        Apply pagination to a list of items

        Args:
            items: Full list of items to paginate
            limit: Maximum items to return (capped at MAX_LIMIT)
            offset: Number of items to skip

        Returns:
            Tuple of (paginated_items, pagination_metadata)
        """
        # Cap limit at MAX_LIMIT
        limit = min(limit, self.MAX_LIMIT)

        total = len(items)
        paginated = items[offset:offset + limit]

        pagination = {
            "total": total,
            "limit": limit,
            "offset": offset,
            "returned": len(paginated),
            "has_more": offset + limit < total
        }

        return paginated, pagination