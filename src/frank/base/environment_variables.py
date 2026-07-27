"""Every environment variable Frank defines or reads, named once, so there is a single source of
truth and a typo is an ``ImportError`` rather than a silent miss. Import the constant, never the
raw string::

    from frank.base import environment_variables

    key = os.environ.get(environment_variables.EXA_API_KEY)

Frank sets none of these itself; they are read from the process environment the host or user
provides. Grouped by origin.
"""
from __future__ import annotations

# Frank-defined. Optional override for the outbound proxy the fetch/download tools route through;
# falls back to the standard proxy variables below when unset.
FRANK_FETCH_PROXY = "FRANK_FETCH_PROXY"

# Outbound proxy, host-provided. Consulted so server-initiated requests (A2A file fetches, push
# notifications) honour the same egress path as the rest of the process.
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
TZ = "TZ"
LANG = "LANG"
