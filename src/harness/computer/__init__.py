"""macOS computer-use: control any running app through the most accurate approach available.

Scripting a cooperative app returns exact structured data; the accessibility tree reads
and drives app UI as semantic elements; screen pixels are the last resort for apps with
no accessible structure. Every action targets a specific process so it never disturbs,
and is never disturbed by, what the user is doing. The ``computer`` harness tool
dispatches into ``engine``.
"""
from harness.computer import engine, permissions

__all__ = ["engine", "permissions"]
