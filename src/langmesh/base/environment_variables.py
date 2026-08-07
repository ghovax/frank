"""Every environment variable LangMesh defines or reads, named once so a typo fails at import."""

from __future__ import annotations

# An optional override for the outbound proxy, falling back to the standard variables below.
FETCH_PROXY = "FETCH_PROXY"

# Set by a worker so anything it spawns that is not a confined tool child carries the session's identity.
SESSION_ID = "SESSION_ID"

# Set for a tool child where the session has a toolbox, so the package manager installs into its own profile.
XDG_STATE_HOME = "XDG_STATE_HOME"
NIX_CONFIG = "NIX_CONFIG"

# The host-provided outbound proxy, so server-initiated requests honour the same egress path.
HTTPS_PROXY = "HTTPS_PROXY"
ALL_PROXY = "ALL_PROXY"

# Third-party integration keys, user-provided; each enables its tool or provider when present.
EXA_API_KEY = "EXA_API_KEY"  # web search (search_web)
JINA_API_KEY = "JINA_API_KEY"  # a fetch_url rendering fallback
FIRECRAWL_API_KEY = "FIRECRAWL_API_KEY"  # a fetch_url rendering fallback
FIRECRAWL_API_URL = "FIRECRAWL_API_URL"  # self-hosted Firecrawl endpoint override
COMPOSIO_API_KEY = "COMPOSIO_API_KEY"  # hosted MCP integrations

# Standard OS variables, consulted read-only for the system/user snapshot shown in the prompt.
SHELL = "SHELL"
PATH = "PATH"
EDITOR = "EDITOR"
VISUAL = "VISUAL"
TZ = "TZ"
LANG = "LANG"
