"""Location primitives for the projects/locations model.

A *location* is a named place a project can run tools in — the home server's own
filesystem (``local``) or a remote machine reached over SSH. Each location is addressed
by a fully-qualified URI (``file:///base`` or ``ssh://user@host:port/base``), and the
home server executes tools against it locally or over multiplexed OpenSSH.

This package holds the self-contained, transport-level pieces: URI parsing/formatting
(:mod:`location_uri`), the ``~/.ssh/config`` host registry (:mod:`ssh_hosts`), and the
local/remote execution primitive (:mod:`executor`). Higher layers (the agent runtime,
tools, endpoints) build on these.
"""
