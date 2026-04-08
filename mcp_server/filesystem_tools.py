"""
Filesystem MCP tools — list_files and move_file.
All operations are sandboxed to FS_ALLOWED_ROOT (default: ./sandbox).
Any path that escapes the sandbox raises PermissionError.
"""
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


def _resolve_safe(base: Path, relative: str) -> Path:
    """Resolve a path relative to base; raise PermissionError if it escapes base."""
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise PermissionError(
            f"Path '{relative}' escapes the allowed sandbox directory."
        )
    return resolved


def register_filesystem_tools(mcp: FastMCP) -> None:
    """Register list_files and move_file tools onto the FastMCP instance."""

    @mcp.tool()
    def list_files(directory: str = ".") -> list[dict[str, Any]]:
        """
        List files and folders in a sandboxed directory.
        Returns list of {name, path, size_bytes, modified_iso, is_dir}.
        directory: relative path within the sandbox root.
        """
        from config.settings import get_settings
        sandbox = Path(get_settings().fs_allowed_root).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)

        target = _resolve_safe(sandbox, directory)
        if not target.exists():
            return []

        entries = []
        for item in sorted(target.iterdir()):
            stat = item.stat()
            entries.append(
                {
                    "name": item.name,
                    "path": str(item.relative_to(sandbox)),
                    "size_bytes": stat.st_size if item.is_file() else 0,
                    "modified_iso": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "is_dir": item.is_dir(),
                }
            )
        return entries

    @mcp.tool()
    def move_file(source: str, destination: str) -> dict[str, Any]:
        """
        Move a file within the sandbox.
        source / destination: relative paths within the sandbox root.
        Returns {source, destination, success, error}.
        """
        from config.settings import get_settings
        sandbox = Path(get_settings().fs_allowed_root).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)

        try:
            src = _resolve_safe(sandbox, source)
            dst = _resolve_safe(sandbox, destination)
            if not src.exists():
                return {"source": source, "destination": destination, "success": False, "error": "Source not found"}
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return {"source": source, "destination": destination, "success": True, "error": ""}
        except PermissionError as e:
            return {"source": source, "destination": destination, "success": False, "error": str(e)}
        except Exception as e:
            return {"source": source, "destination": destination, "success": False, "error": str(e)}
