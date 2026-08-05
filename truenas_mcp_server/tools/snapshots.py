"""
Snapshot management tools for TrueNAS
"""

from typing import Dict, Any, Optional, List, Union
from datetime import datetime
from .base import BaseTool, tool_handler
from ..exceptions import TrueNASAPIError


def _coerce_creation_timestamp(value: Any) -> Optional[Union[int, float]]:
    """Normalize TrueNAS snapshot creation timestamps into an epoch (seconds).

    TrueNAS SCALE returns ``properties.creation.parsed`` in BSON extended JSON
    (e.g. ``{"$date": 1717200000000}``), while TrueNAS CORE historically
    returned a string ISO timestamp or a numeric epoch. Passing any of these
    straight into ``datetime.fromtimestamp`` either raises ``TypeError`` (dict)
    or silently produces a wrong date (string). This helper unwraps the BSON
    shape into epoch seconds and lets everything else pass through.

    Returns ``None`` if the value is missing or not coercible to a number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        # BSON extended JSON: {"$date": <epoch_ms or ISO string>}
        bson_date = value.get("$date")
        if isinstance(bson_date, (int, float)):
            # BSON $date is milliseconds since epoch.
            return int(bson_date) // 1000
        if isinstance(bson_date, str):
            # Fall back to parsing the ISO string the server sent us.
            try:
                # Python 3.11+: tolerate trailing 'Z'.
                return int(datetime.fromisoformat(bson_date.replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError):
                return None
        # Some TrueNAS endpoints nest .parsed inside another dict.
        nested = value.get("parsed")
        if nested is not None and nested is not value:
            return _coerce_creation_timestamp(nested)
        return None
    if isinstance(value, str):
        # ISO 8601 string — common on TrueNAS CORE.
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return None
    return None


def _parse_snapshot_timestamp(snap: Dict[str, Any]) -> Optional[Union[int, float]]:
    """Extract the creation timestamp from a raw TrueNAS snapshot dict.

    Walks the known nested shapes (``.properties.creation.parsed``,
    ``.properties.creation``, ``.creation``) and coerces to an epoch.
    """
    props = snap.get("properties") or {}
    creation = props.get("creation") if isinstance(props, dict) else None
    if creation is None:
        creation = snap.get("creation")
    if isinstance(creation, dict):
        parsed = creation.get("parsed", creation)
        if isinstance(parsed, dict):
            # parsed is itself a BSON dict — coerce recursively.
            return _coerce_creation_timestamp(parsed)
        return _coerce_creation_timestamp(parsed)
    return _coerce_creation_timestamp(creation)


class SnapshotTools(BaseTool):
    """Tools for managing ZFS snapshots"""
    
    def get_tool_definitions(self) -> list:
        """Get tool definitions for snapshot management"""
        return [
            ("list_snapshots", self.list_snapshots, "List snapshots for a dataset",
             {"dataset": {"type": "string", "required": False,
                         "description": "Filter by dataset path"},
              "limit": {"type": "integer", "required": False,
                       "description": "Max items to return (default: 100, max: 500)"},
              "offset": {"type": "integer", "required": False,
                        "description": "Items to skip for pagination"}}),
            ("create_snapshot", self.create_snapshot, "Create a snapshot of a dataset",
             {"dataset": {"type": "string", "required": True},
              "name": {"type": "string", "required": False},
              "recursive": {"type": "boolean", "required": False}}),
            ("delete_snapshot", self.delete_snapshot, "Delete a snapshot",
             {"snapshot": {"type": "string", "required": True}}),
            ("rollback_snapshot", self.rollback_snapshot, "Rollback dataset to a snapshot",
             {"snapshot": {"type": "string", "required": True},
              "force": {"type": "boolean", "required": False}}),
            ("clone_snapshot", self.clone_snapshot, "Clone a snapshot to a new dataset",
             {"snapshot": {"type": "string", "required": True},
              "target": {"type": "string", "required": True}}),
            ("list_snapshot_tasks", self.list_snapshot_tasks, "List automated snapshot tasks",
             {"limit": {"type": "integer", "required": False,
                       "description": "Max items to return (default: 100, max: 500)"},
              "offset": {"type": "integer", "required": False,
                        "description": "Items to skip for pagination"}}),
            ("create_snapshot_task", self.create_snapshot_task, "Create automated snapshot task",
             {"dataset": {"type": "string", "required": True},
              "schedule": {"type": "object", "required": True},
              "retention": {"type": "integer", "required": True},
              "recursive": {"type": "boolean", "required": False}}),
        ]
    
    @tool_handler
    async def list_snapshots(
        self,
        dataset: Optional[str] = None,
        limit: int = BaseTool.DEFAULT_LIMIT,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        List snapshots for a dataset or all snapshots

        Args:
            dataset: Optional dataset path to filter snapshots
            limit: Maximum number of items to return (default: 100, max: 500)
            offset: Number of items to skip for pagination

        Returns:
            Dictionary containing list of snapshots
        """
        await self.ensure_initialized()

        # Get all snapshots
        params = {}
        if dataset:
            params["dataset"] = dataset

        snapshots = await self.client.get("/zfs/snapshot", params)

        snapshot_list = []
        for snap in snapshots:
            # Parse snapshot name to extract dataset and snapshot name
            full_name = snap.get("name", "")
            if "@" in full_name:
                ds_name, snap_name = full_name.split("@", 1)
            else:
                ds_name = full_name
                snap_name = ""

            # TrueNAS SCALE returns properties.creation.parsed in BSON extended
            # JSON ({"$date": <ms>}); CORE historically returns an ISO string
            # or a numeric epoch. _parse_snapshot_timestamp normalises all of
            # those into an epoch (int/float) so we can never pass a dict to
            # datetime.fromtimestamp and crash the whole listing.
            created_epoch = _parse_snapshot_timestamp(snap)
            props = snap.get("properties") or {}

            snapshot_info = {
                "name": full_name,
                "dataset": ds_name,
                "snapshot": snap_name,
                "created": created_epoch,
                "referenced": props.get("referenced", {}).get("value") if isinstance(props, dict) else None,
                "used": props.get("used", {}).get("value") if isinstance(props, dict) else None,
                "holds": snap.get("holds", [])
            }

            # Format timestamp if available. Guard against OSError/ValueError
            # for edge values (e.g. epoch before 1970) so one bad row can't
            # take the whole listing down.
            if created_epoch is not None:
                try:
                    snapshot_info["created_human"] = datetime.fromtimestamp(created_epoch).isoformat()
                except (TypeError, ValueError, OSError):
                    snapshot_info["created_human"] = None

            snapshot_list.append(snapshot_info)

        # Sort by creation time (newest first). 'created' is now guaranteed to
        # be int/float/None, so the default of 0 sorts unknowns last and the
        # comparison is well-defined.
        snapshot_list.sort(key=lambda x: x.get("created") or 0, reverse=True)
        
        # Group by dataset (before pagination for accurate counts)
        by_dataset = {}
        for snap in snapshot_list:
            ds = snap["dataset"]
            if ds not in by_dataset:
                by_dataset[ds] = []
            by_dataset[ds].append(snap)

        # Apply pagination
        paginated_snapshots, pagination = self.apply_pagination(snapshot_list, limit, offset)

        return {
            "success": True,
            "snapshots": paginated_snapshots,
            "metadata": {
                "total_snapshots": len(snapshot_list),
                "datasets_with_snapshots": len(by_dataset),
                "by_dataset": {ds: len(snaps) for ds, snaps in by_dataset.items()}
            },
            "pagination": pagination
        }
    
    @tool_handler
    async def create_snapshot(
        self,
        dataset: str,
        name: Optional[str] = None,
        recursive: bool = False,
        properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Create a snapshot of a dataset
        
        Args:
            dataset: Dataset path (e.g., "tank/data")
            name: Snapshot name (auto-generated if not provided)
            recursive: Create snapshots recursively for child datasets
            properties: Optional ZFS properties for the snapshot
            
        Returns:
            Dictionary containing created snapshot information
        """
        await self.ensure_initialized()
        
        # Generate snapshot name if not provided
        if not name:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"manual-{timestamp}"
        
        snapshot_data = {
            "dataset": dataset,
            "name": name,
            "recursive": recursive
        }
        
        if properties:
            snapshot_data["properties"] = properties

        # TrueNAS SCALE returns a job object ({"job_id": <int>}) on success and
        # an error dict ({"error": ..., "reason": ...}) on failure. We MUST
        # inspect the response — passing it through unconditionally is what
        # caused the data-loss-adjacent bug where the wrapper reported success
        # even when the snapshot never existed.
        #
        # HTTP-level errors (4xx/5xx) are already raised by client.post via
        # TrueNASAPIError; @tool_handler translates that into success=False.
        # This block handles the inline-error and missing-job_id shapes.
        try:
            response = await self.client.post("/zfs/snapshot", snapshot_data) or {}
        except TrueNASAPIError:
            # Let the decorator turn it into a structured failure response.
            raise

        # Inline error payload? Some TrueNAS endpoints return 200 with an error.
        if isinstance(response, dict) and (response.get("error") or response.get("errored")):
            reason = (
                response.get("reason")
                or response.get("error")
                or "TrueNAS API returned an error response without details"
            )
            return {
                "success": False,
                "error": str(reason),
                "error_type": "TrueNASAPIError",
                "details": response,
            }

        # TrueNAS SCALE returns a job descriptor; the snapshot itself appears
        # asynchronously once the job completes. Surface the job_id so callers
        # can poll /core/get_jobs and verify completion.
        job_id = response.get("job_id") if isinstance(response, dict) else None
        snapshot_full_name = f"{dataset}@{name}"

        return {
            "success": True,
            "message": (
                f"Snapshot job '{job_id}' accepted for '{snapshot_full_name}'"
                if job_id is not None
                else f"Snapshot '{snapshot_full_name}' accepted by TrueNAS"
            ),
            "snapshot": {
                "name": snapshot_full_name,
                "dataset": dataset,
                "snapshot": name,
                "recursive": recursive,
                # IMPORTANT: don't fabricate a 'created' timestamp from
                # datetime.now() — that's the bug that made cutover-prep
                # believe it had a backup when it didn't. Callers that need
                # the actual creation time must re-query list_snapshots.
                "created": None,
            },
            "job_id": job_id,
            "raw_response": response,
        }
    
    @tool_handler
    async def delete_snapshot(self, snapshot: str, defer: bool = False) -> Dict[str, Any]:
        """
        Delete a snapshot
        
        Args:
            snapshot: Full snapshot name (e.g., "tank/data@snapshot1")
            defer: Defer deletion (mark for later deletion)
            
        Returns:
            Dictionary confirming deletion
        """
        await self.ensure_initialized()
        
        # Check if destructive operations are allowed
        if not self.settings.enable_destructive_operations:
            return {
                "success": False,
                "error": "Destructive operations are disabled. Enable TRUENAS_ENABLE_DESTRUCTIVE_OPS to allow snapshot deletion."
            }
        
        # Validate snapshot name format
        if "@" not in snapshot:
            return {
                "success": False,
                "error": f"Invalid snapshot name format. Expected 'dataset@snapshot', got '{snapshot}'"
            }
        
        delete_data = {
            "defer": defer
        }
        
        # Delete the snapshot
        await self.client.delete(f"/zfs/snapshot/id/{snapshot}")
        
        dataset, snap_name = snapshot.split("@", 1)
        
        return {
            "success": True,
            "message": f"Snapshot '{snapshot}' deleted successfully",
            "deleted": {
                "snapshot": snapshot,
                "dataset": dataset,
                "name": snap_name,
                "deferred": defer
            }
        }
    
    @tool_handler
    async def rollback_snapshot(self, snapshot: str, force: bool = False) -> Dict[str, Any]:
        """
        Rollback dataset to a snapshot
        
        Args:
            snapshot: Full snapshot name (e.g., "tank/data@snapshot1")
            force: Force rollback even if it will destroy newer snapshots
            
        Returns:
            Dictionary confirming rollback
        """
        await self.ensure_initialized()
        
        # Check if destructive operations are allowed
        if not self.settings.enable_destructive_operations:
            return {
                "success": False,
                "error": "Destructive operations are disabled. Enable TRUENAS_ENABLE_DESTRUCTIVE_OPS to allow snapshot rollback."
            }
        
        # Validate snapshot name format
        if "@" not in snapshot:
            return {
                "success": False,
                "error": f"Invalid snapshot name format. Expected 'dataset@snapshot', got '{snapshot}'"
            }
        
        rollback_data = {
            "force": force
        }
        
        # Perform rollback
        result = await self.client.post(f"/zfs/snapshot/id/{snapshot}/rollback", rollback_data)
        
        dataset, snap_name = snapshot.split("@", 1)
        
        return {
            "success": True,
            "message": f"Dataset '{dataset}' rolled back to snapshot '{snap_name}'",
            "rollback": {
                "snapshot": snapshot,
                "dataset": dataset,
                "forced": force
            },
            "warning": "Data written after this snapshot has been lost" if force else None
        }
    
    @tool_handler
    async def clone_snapshot(self, snapshot: str, target: str) -> Dict[str, Any]:
        """
        Clone a snapshot to a new dataset
        
        Args:
            snapshot: Full snapshot name (e.g., "tank/data@snapshot1")
            target: Target dataset path for the clone
            
        Returns:
            Dictionary containing clone information
        """
        await self.ensure_initialized()
        
        # Validate snapshot name format
        if "@" not in snapshot:
            return {
                "success": False,
                "error": f"Invalid snapshot name format. Expected 'dataset@snapshot', got '{snapshot}'"
            }
        
        clone_data = {
            "snapshot": snapshot,
            "dataset_dst": target
        }
        
        # Create the clone
        result = await self.client.post("/zfs/snapshot/clone", clone_data)
        
        dataset, snap_name = snapshot.split("@", 1)
        
        return {
            "success": True,
            "message": f"Snapshot '{snapshot}' cloned to '{target}'",
            "clone": {
                "source_snapshot": snapshot,
                "source_dataset": dataset,
                "target_dataset": target,
                "created": datetime.now().isoformat()
            }
        }
    
    @tool_handler
    async def list_snapshot_tasks(
        self,
        limit: int = BaseTool.DEFAULT_LIMIT,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        List automated snapshot tasks

        Args:
            limit: Maximum number of items to return (default: 100, max: 500)
            offset: Number of items to skip for pagination

        Returns:
            Dictionary containing list of snapshot tasks
        """
        await self.ensure_initialized()

        tasks = await self.client.get("/pool/snapshottask")
        
        task_list = []
        for task in tasks:
            # Parse schedule
            schedule = task.get("schedule", {})
            schedule_str = self._format_schedule(schedule)
            
            task_info = {
                "id": task.get("id"),
                "dataset": task.get("dataset"),
                "recursive": task.get("recursive", False),
                "enabled": task.get("enabled", True),
                "naming_schema": task.get("naming_schema"),
                "schedule": schedule,
                "schedule_human": schedule_str,
                "retention": {
                    "value": task.get("lifetime_value"),
                    "unit": task.get("lifetime_unit")
                },
                "allow_empty": task.get("allow_empty", True)
            }
            task_list.append(task_info)

        # Calculate counts before pagination
        total_tasks = len(task_list)
        enabled_tasks = sum(1 for t in task_list if t["enabled"])
        recursive_tasks = sum(1 for t in task_list if t["recursive"])

        # Apply pagination
        paginated_tasks, pagination = self.apply_pagination(task_list, limit, offset)

        return {
            "success": True,
            "tasks": paginated_tasks,
            "metadata": {
                "total_tasks": total_tasks,
                "enabled_tasks": enabled_tasks,
                "recursive_tasks": recursive_tasks
            },
            "pagination": pagination
        }
    
    @tool_handler
    async def create_snapshot_task(
        self,
        dataset: str,
        schedule: Dict[str, str],
        retention: int,
        retention_unit: str = "DAY",
        naming_schema: Optional[str] = None,
        recursive: bool = True,
        enabled: bool = True
    ) -> Dict[str, Any]:
        """
        Create automated snapshot task
        
        Args:
            dataset: Dataset to snapshot
            schedule: Cron-like schedule {"minute": "0", "hour": "*/4", ...}
            retention: How long to keep snapshots
            retention_unit: Unit for retention (HOUR, DAY, WEEK, MONTH, YEAR)
            naming_schema: Naming pattern for snapshots (default: auto-%Y%m%d-%H%M%S)
            recursive: Include child datasets
            enabled: Enable task immediately
            
        Returns:
            Dictionary containing created task information
        """
        await self.ensure_initialized()
        
        # Default naming schema
        if not naming_schema:
            naming_schema = "auto-%Y%m%d-%H%M%S"
        
        # Validate schedule
        required_schedule_keys = ["minute", "hour", "dom", "month", "dow"]
        for key in required_schedule_keys:
            if key not in schedule:
                schedule[key] = "*"
        
        task_data = {
            "dataset": dataset,
            "recursive": recursive,
            "lifetime_value": retention,
            "lifetime_unit": retention_unit,
            "naming_schema": naming_schema,
            "schedule": schedule,
            "enabled": enabled,
            "allow_empty": True
        }
        
        created = await self.client.post("/pool/snapshottask", task_data) or {}

        # Inline error payload? Some TrueNAS endpoints return 200 with an error
        # rather than raising on the HTTP layer. Surface it instead of
        # fabricating success. (HTTP 4xx/5xx already propagate via the
        # @tool_handler decorator.)
        if isinstance(created, dict) and (created.get("error") or created.get("errored")):
            reason = created.get("reason") or created.get("error") or "TrueNAS API error"
            return {
                "success": False,
                "error": str(reason),
                "error_type": "TrueNASAPIError",
                "details": created,
            }

        return {
            "success": True,
            "message": f"Snapshot task created for dataset '{dataset}'",
            "task": {
                "id": created.get("id") if isinstance(created, dict) else None,
                "dataset": dataset,
                "schedule": self._format_schedule(schedule),
                "retention": f"{retention} {retention_unit}(S)",
                "enabled": enabled
            }
        }
    
    def _format_schedule(self, schedule: Dict[str, str]) -> str:
        """Format cron schedule as human-readable string"""
        minute = schedule.get("minute", "*")
        hour = schedule.get("hour", "*")
        dom = schedule.get("dom", "*")
        month = schedule.get("month", "*")
        dow = schedule.get("dow", "*")
        
        # Try to create a human-readable description
        if minute == "0" and hour == "0" and dom == "*" and month == "*" and dow == "*":
            return "Daily at midnight"
        elif minute == "0" and hour == "*" and dom == "*" and month == "*" and dow == "*":
            return "Every hour"
        elif minute == "0" and hour == "*/4" and dom == "*" and month == "*" and dow == "*":
            return "Every 4 hours"
        elif minute == "*/15" and hour == "*" and dom == "*" and month == "*" and dow == "*":
            return "Every 15 minutes"
        elif minute == "0" and hour == "0" and dom == "*" and month == "*" and dow == "0":
            return "Weekly on Sunday at midnight"
        elif minute == "0" and hour == "0" and dom == "1" and month == "*" and dow == "*":
            return "Monthly on the 1st at midnight"
        else:
            return f"{minute} {hour} {dom} {month} {dow}"