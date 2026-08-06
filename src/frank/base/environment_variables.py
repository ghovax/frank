"""Every environment variable Frank defines or reads, named once, so there is a single source of truth and a typo is an ``ImportError`` rather than a silent miss."""
from __future__ import annotations

# Frank-defined.
FETCH_PROXY = "FETCH_PROXY"

# Set by a worker into its own environment, so that anything it spawns which is not a confined tool child — an MCP server over stdio, a helper — carries the session's identity.
SESSION_ID = "SESSION_ID"

# Set for a tool child, and only where the session has a toolbox: the two the package manager reads to install into *this session's* profile rather than the machine's.
XDG_STATE_HOME = "XDG_STATE_HOME"
NIX_CONFIG = "NIX_CONFIG"

# Outbound proxy, host-provided.
HTTPS_PROXY = "HTTPS_PROXY"
ALL_PROXY = "ALL_PROXY"

# Third-party integration keys, user-provided; each enables its tool or provider when present.
EXA_API_KEY = "EXA_API_KEY"              # web search (search_web)
JINA_API_KEY = "JINA_API_KEY"            # a fetch_url rendering fallback
FIRECRAWL_API_KEY = "FIRECRAWL_API_KEY"  # a fetch_url rendering fallback
FIRECRAWL_API_URL = "FIRECRAWL_API_URL"  # self-hosted Firecrawl endpoint override
COMPOSIO_API_KEY = "COMPOSIO_API_KEY"    # hosted MCP integrations

# Standard OS variables, consulted read-only for the system/user snapshot shown in the prompt.
SHELL = "SHELL"
PATH = "PATH"
EDITOR = "EDITOR"
VISUAL = "VISUAL"
TZ = "TZ"
LANG = "LANG"
