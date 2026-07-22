"""Local-machine and user-context probes for the system prompt.

Two entry points build the JSON snapshots injected into the prompt:
:func:`probe_local_environment` (the neutral machine snapshot — OS, toolchain, PATH,
environment, and the shell-history-derived ``frequent_commands`` histogram) and
:func:`probe_user_context` (the opt-in, more personal snapshot — where/what/who/when the
user works, and what they browse). Everything here reads local metadata only and degrades
to silence on failure; the module holds no state.

Privacy: both snapshots are injected into the system prompt and therefore travel to the
configured model provider, so they place machine and (opt-in) user PII in front of that
provider. This is a deliberate design choice — the agent is meant to know who the user is,
what they do, and what it can do for them — and it is open to revisiting (narrower snapshots,
field redaction, wider opt-in). See the privacy section of SECURITY.md.
"""

from __future__ import annotations

import json
import os
from daisy.core import environment_variables
import platform
from datetime import datetime
from pathlib import Path
from typing import Iterator


# Command-line wrappers/keywords to skip when finding the real command a history line ran.
_HISTORY_WRAPPERS = frozenset({
    "sudo", "doas", "env", "nice", "nohup", "time", "command", "exec", "builtin",
    "xargs", "then", "do", "else", "elif", "fi", "done", "&&", "||", "!", "{", "}",
})


def _is_subcommand_token(token: str) -> bool:
    """A bare word that is safely a sub-command/verb (git *status*, uv *run*) rather than a
    value like a path, URL, or secret — so it can be shown without leaking anything."""
    return bool(token) and token[0].isalpha() and len(token) <= 25 and all(
        character.isalnum() or character in "_-" for character in token
    )


def _parse_history_invocation(line: str) -> tuple[str, str, list[str]] | None:
    """Reduce one history line to ``(base, subcommand_chain, flag_names)``. The chain is the
    leading sub-command verbs joined by spaces (``compose up`` for ``docker compose up``),
    empty when the command takes none. Only verbs and flag NAMES are extracted — positional
    arguments and flag values (paths, URLs, secrets) are dropped, so nothing sensitive is
    read. Returns ``None`` for a line with no usable command."""
    tokens = line.split()
    index = 0
    # Skip leading VAR=value assignments and command wrappers (sudo, env, …).
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-") and "/" not in token.split("=", 1)[0]:
            index += 1
            continue
        if token in _HISTORY_WRAPPERS:
            index += 1
            continue
        break
    if index >= len(tokens):
        return None
    base = tokens[index].split("/")[-1]
    if not base or base[0] in "-$(){}<>|&;'\"`.":
        return None
    # Sub-commands are only collected before the first flag, so a flag's value (e.g. the
    # username after ``-u``) is never mistaken for a sub-command.
    subcommands: list[str] = []
    flags: list[str] = []
    seen_flag = False
    for token in tokens[index + 1:]:
        if token.startswith("-") and token not in ("-", "--"):
            if len(flags) >= 4:
                break
            seen_flag = True
            flags.append(token.split("=", 1)[0])
        elif not seen_flag and len(subcommands) < 3 and _is_subcommand_token(token):
            subcommands.append(token)
        else:
            break
    return base, " ".join(subcommands), flags


def _top_counter(counter: dict[str, int], limit: int) -> dict[str, int]:
    """The ``limit`` highest-count entries of a name→count map, ordered by count desc."""
    return {
        name: counter[name]
        for name in sorted(counter, key=lambda key: counter[key], reverse=True)[:limit]
    }


def _shell_command_usage(
    command_limit: int = 24, subcommand_limit: int = 8, flag_limit: int = 8
) -> dict[str, dict]:
    """The user's habitual commands AND how they invoke them, mined from shell history so
    the agent can match the user's actual workflow. Returns a *nested histogram* that names
    each root command once and hangs its usage beneath it, avoiding the repetition of a flat
    ``git commit -m`` / ``git push`` / ``git status`` list::

        {"git": {"count": 812,
                 "subcommands": {"commit": {"count": 240, "flags": {"-m": 236, "--amend": 31}},
                                 "push":   {"count": 118, "flags": {"--force": 12}}},
                 "flags": {"--version": 3}}}

    Each command carries its total ``count``, a ``subcommands`` histogram (each with its own
    ``count`` and per-subcommand ``flags`` histogram), and a top-level ``flags`` histogram
    for invocations that used no sub-command. Only sub-command verbs and flag NAMES are ever
    recorded; positional arguments and flag values (paths, URLs, secrets) are never read,
    so nothing sensitive leaves the history file. Silent on any failure."""
    # command -> {"count", "subcommands": {chain: {"count", "flags": {name: count}}},
    #             "flags": {name: count}}
    commands: dict[str, dict] = {}
    for line in _iter_history_lines():
        parsed = _parse_history_invocation(line)
        if parsed is None:
            continue
        command, subcommand_chain, flag_names = parsed
        command_entry = commands.setdefault(command, {"count": 0, "subcommands": {}, "flags": {}})
        command_entry["count"] += 1
        if subcommand_chain:
            subcommand_entry = command_entry["subcommands"].setdefault(
                subcommand_chain, {"count": 0, "flags": {}}
            )
            subcommand_entry["count"] += 1
            flag_counts = subcommand_entry["flags"]
        else:
            flag_counts = command_entry["flags"]
        for flag_name in flag_names:
            flag_counts[flag_name] = flag_counts.get(flag_name, 0) + 1
    top_commands = sorted(commands, key=lambda name: commands[name]["count"], reverse=True)[:command_limit]
    usage: dict[str, dict] = {}
    for command in top_commands:
        command_entry = commands[command]
        command_node: dict = {"count": command_entry["count"]}
        subcommand_entries = command_entry["subcommands"]
        if subcommand_entries:
            top_subcommands = sorted(
                subcommand_entries,
                key=lambda chain: subcommand_entries[chain]["count"],
                reverse=True,
            )[:subcommand_limit]
            rendered_subcommands: dict[str, dict] = {}
            for subcommand_chain in top_subcommands:
                subcommand_entry = subcommand_entries[subcommand_chain]
                subcommand_node: dict = {"count": subcommand_entry["count"]}
                if subcommand_entry["flags"]:
                    subcommand_node["flags"] = _top_counter(subcommand_entry["flags"], flag_limit)
                rendered_subcommands[subcommand_chain] = subcommand_node
            command_node["subcommands"] = rendered_subcommands
        if command_entry["flags"]:
            command_node["flags"] = _top_counter(command_entry["flags"], flag_limit)
        usage[command] = command_node
    return usage


def probe_local_environment() -> str:
    """A structured JSON snapshot of the local machine the agent's tools run on by
    default: OS, key toolchain availability, PATH, locale, and the full environment.
    It gives the agent initial pointers so it probes reality instead of assuming a
    capability exists. The full environment is included, values as-is."""
    import shutil
    import socket

    uname = platform.uname()
    # Presence-only probe of a broad, common toolchain — never versions/paths, which the
    # agent can pull itself if it needs them. Grouped by category for maintainability.
    probed = [
        # Language runtimes
        "python3", "node", "deno", "bun", "ruby", "perl", "php", "go", "rustc",
        "java", "kotlin", "swift", "dotnet", "gcc", "g++", "clang", "lua", "Rscript",
        "julia", "dart", "elixir", "erl",
        # Package and version managers
        "uv", "uvx", "pip", "pipx", "pipenv", "poetry", "pdm", "rye", "hatch", "conda",
        "mamba", "pixi", "npm", "pnpm", "yarn", "cargo", "rustup", "gem", "bundle",
        "composer", "mvn", "gradle", "sdk", "opam", "luarocks", "cabal", "stack",
        "brew", "port", "nix", "nix-env", "guix", "spack", "asdf", "mise", "volta", "fnm",
        "apt-get", "dnf", "yum", "zypper", "pacman", "apk", "emerge", "xbps-install",
        "snap", "flatpak", "pkg", "choco", "scoop", "winget",
        # Build tools
        "make", "cmake", "ninja", "meson", "bazel",
        # Version control
        "git", "gh", "glab", "hg", "svn",
        # Search, text, and data
        "rg", "ag", "fd", "fzf", "grep", "sed", "awk", "jq", "yq", "xmllint", "pandoc",
        # Network
        "curl", "wget", "http", "nc", "dig", "nslookup", "ssh", "scp", "rsync", "ping",
        "ip", "ifconfig", "netstat", "ss", "openssl",
        # Containers, cloud, and infrastructure
        "docker", "docker-compose", "podman", "kubectl", "helm", "terraform", "ansible",
        "vagrant", "aws", "gcloud", "az", "flyctl",
        # Databases
        "psql", "mysql", "sqlite3", "redis-cli", "mongosh",
        # Media, graphics, and documents
        "ffmpeg", "convert", "magick", "gs", "sox", "tesseract", "dot", "gnuplot",
        # Editors and multiplexers
        "vim", "nvim", "emacs", "nano", "tmux", "screen", "code",
    ]
    present = [name for name in probed if shutil.which(name)]
    try:
        host = socket.gethostname()
    except Exception:
        host = None

    # The full environment, values included as-is. PATH is dropped here since it is already
    # broken out as a parsed list above.
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "PATH"
    }

    payload = {
        "os": {
            "system": uname.system,
            "release": uname.release,
            "platform": platform.platform(terse=True),
            "arch": uname.machine,
        },
        "host": host,
        "shell": os.environ.get(environment_variables.SHELL),
        "locale": os.environ.get(environment_variables.LANG),
        "timezone": os.environ.get(environment_variables.TZ) or "system default",
        "python": platform.python_version(),
        "tools": {
            "present": present,
            "absent": [name for name in probed if name not in present],
        },
        # The user's habitual commands mapped to how they invoke them (sub-commands + flag
        # names), mined from their shell history. No positional args / flag values.
        "frequent_commands": _shell_command_usage(),
        "path": [entry for entry in os.environ.get(environment_variables.PATH, "").split(os.pathsep) if entry],
        "env": environment,
    }
    return json.dumps(payload)


def _iter_history_lines() -> Iterator[str]:
    """Yield each shell-history command line, normalized across zsh/bash/fish formats.
    Shared by the environment and user-context probes. Silent on unreadable files."""
    home = Path.home()
    sources: list[tuple[Path, str]] = [
        (home / ".zsh_history", "zsh"),
        (home / ".bash_history", "plain"),
        (home / ".local/share/fish/fish_history", "fish"),
    ]
    for path, fmt in sources:
        try:
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if fmt == "zsh" and line.startswith(":"):
                semicolon = line.find(";")
                if semicolon != -1:
                    line = line[semicolon + 1:].strip()
            elif fmt == "fish":
                if not line.startswith("- cmd:"):
                    continue
                line = line[len("- cmd:"):].strip()
            yield line


def _home_relative(path: Path | str) -> str:
    """Render a path with the home directory folded to ``~`` for compact, readable output."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


# Directory-changing commands whose first non-flag argument is a navigation target — mined
# from history to surface the folders the user actually works in.
_DIRECTORY_JUMP_COMMANDS = frozenset({"cd", "pushd", "z", "j", "zi"})

# Chromium-family browser history databases, relative to ~/Library/Application Support.
# Each browser keeps one `History` SQLite file per profile; the glob covers extra profiles.
_CHROMIUM_HISTORY_GLOBS = (
    "Google/Chrome/*/History",
    "BraveSoftware/Brave-Browser/*/History",
    "Microsoft Edge/*/History",
    "Arc/User Data/*/History",
    "Vivaldi/*/History",
    "Chromium/*/History",
)


def _run_command(argv: list[str], timeout: float = 4) -> str:
    """Run a short, read-only system command and return its stripped stdout, or ``""`` on any
    failure/non-zero exit/timeout. The backbone of the macOS metadata probes below — every one
    of them degrades to nothing rather than raising or blocking the prompt build."""
    import subprocess

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _normalize_host(url: str) -> str:
    """The bare hostname of a URL, lowercased and with a leading ``www.`` stripped; ``""`` for
    URLs with no usable host or for local loopbacks (which say nothing about browsing habits)."""
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("localhost", "127.0.0.1", "::1"):
        return ""
    return host


# Chromium timestamps are microseconds since 1601-01-01; this is the offset to the Unix epoch.
_CHROMIUM_EPOCH_OFFSET_SECONDS = 11644473600


def _browser_site_activity(
    most_visited_limit: int = 22, recent_limit: int = 15, recent_days: int = 14
) -> dict:
    """Two complementary views of the user's browsing, summed across every browser's history
    database: ``most_visited_sites`` (hostname → all-time visit count — enduring interests) and
    ``recently_active_sites`` (hostnames touched within the last ``recent_days``, newest first —
    current focus). Only hostnames, counts, and last-visit *timestamps* are read; never full
    URLs, titles, or page content. Databases are opened read-only/immutable so a running browser
    is never disturbed. Silent on failure; omits either view when empty."""
    import sqlite3
    import time

    support = Path.home() / "Library" / "Application Support"
    # (database, SQL, has_timestamp). Chromium keeps counts + microsecond timestamps in `urls`;
    # Safari's `history_items` has counts but not a per-item time, so it feeds only the totals.
    queries: list[tuple[Path, str, bool]] = []
    for pattern in _CHROMIUM_HISTORY_GLOBS:
        for database in sorted(support.glob(pattern)):
            queries.append((database, "SELECT url, visit_count, last_visit_time FROM urls WHERE visit_count > 0", True))
    safari = Path.home() / "Library" / "Safari" / "History.db"
    if safari.is_file():
        queries.append((safari, "SELECT url, visit_count FROM history_items WHERE visit_count > 0", False))

    visit_counts: dict[str, int] = {}
    last_seen: dict[str, float] = {}
    for database, sql, has_timestamp in queries:
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
            try:
                for row in connection.execute(sql):
                    host = _normalize_host(row[0])
                    if not host:
                        continue
                    visit_counts[host] = visit_counts.get(host, 0) + int(row[1] or 0)
                    if has_timestamp and row[2]:
                        seconds = int(row[2]) / 1_000_000 - _CHROMIUM_EPOCH_OFFSET_SECONDS
                        if seconds > last_seen.get(host, 0):
                            last_seen[host] = seconds
            finally:
                connection.close()
        except Exception:
            continue

    activity: dict = {}
    if visit_counts:
        activity["most_visited_sites"] = _top_counter(visit_counts, most_visited_limit)
    cutoff = time.time() - recent_days * 86400
    recent_hosts = sorted(
        (host for host, seconds in last_seen.items() if seconds >= cutoff),
        key=lambda host: last_seen[host],
        reverse=True,
    )
    if recent_hosts:
        activity["recently_active_sites"] = recent_hosts[:recent_limit]
    return activity


def _installed_applications(limit: int = 48) -> list[str]:
    """Names of installed macOS applications across the standard bundle locations — a strong
    signal of the tools and platforms the user has available. Names only, sorted, deduped."""
    names: set[str] = set()
    for directory in (Path("/Applications"), Path.home() / "Applications", Path("/System/Applications")):
        try:
            for entry in directory.iterdir():
                if entry.suffix == ".app":
                    names.add(entry.stem)
        except OSError:
            continue
    return sorted(names)[:limit]


def _running_applications(limit: int = 30) -> list[str]:
    """Names of the currently *visible* applications (foreground, not background daemons),
    read via System Events. Best-effort: returns empty if automation is unavailable or the
    probe times out, and never blocks the prompt build for long."""
    import subprocess

    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of (every process whose background only is false)'],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    names = [part.strip() for part in result.stdout.split(",") if part.strip()]
    # Preserve order but dedupe, then cap.
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)[:limit]


# macOS Cocoa/absolute time is seconds since 2001-01-01; this offsets it to the Unix epoch.
_MAC_EPOCH_OFFSET_SECONDS = 978307200


def _system_uptime() -> dict:
    """When the machine last booted and how long it has been up, from ``sysctl kern.boottime``
    (instant, no permissions). A small but real signal of the user's session rhythm — a box
    up for weeks reads differently from one rebooted this morning. Empty off macOS."""
    import re
    import time

    boottime = _run_command(["sysctl", "-n", "kern.boottime"])
    match = re.search(r"sec\s*=\s*(\d+)", boottime)
    if not match:
        return {}
    try:
        booted = int(match.group(1))
    except ValueError:
        return {}
    return {
        "booted_at": datetime.fromtimestamp(booted).strftime("%Y-%m-%d %H:%M"),
        "uptime_hours": round(max(0.0, time.time() - booted) / 3600, 1),
    }


def _app_usage(days: int = 7, limit: int = 18) -> dict:
    """How the user actually spends time in their apps: total hours each app has been *in
    focus* over the last ``days``, mined from macOS's Screen Time / Knowledge store
    (``knowledgeC.db``). This is the real utilization pattern — which apps dominate the day
    — not just what happens to be open now. Keyed by bundle id → hours (one decimal), top
    apps only. Read-only/immutable so Screen Time is never disturbed; needs Full Disk Access,
    so it degrades to empty when unreadable. Silent on any failure."""
    import sqlite3
    import time

    database = Path.home() / "Library" / "Application Support" / "Knowledge" / "knowledgeC.db"
    if not database.is_file():
        return {}
    cutoff = time.time() - days * 86400 - _MAC_EPOCH_OFFSET_SECONDS
    totals: dict[str, float] = {}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        try:
            rows = connection.execute(
                "SELECT ZVALUESTRING, SUM(ZENDDATE - ZSTARTDATE) FROM ZOBJECT "
                "WHERE ZSTREAMNAME = '/app/inFocus' AND ZVALUESTRING IS NOT NULL "
                "AND ZSTARTDATE > ? GROUP BY ZVALUESTRING",
                (cutoff,),
            )
            for bundle_id, seconds in rows:
                if not bundle_id or seconds is None or seconds <= 0:
                    continue
                totals[bundle_id] = totals.get(bundle_id, 0.0) + float(seconds)
        finally:
            connection.close()
    except Exception:
        return {}
    top = sorted(totals, key=lambda bundle: totals[bundle], reverse=True)[:limit]
    return {bundle: round(totals[bundle] / 3600, 1) for bundle in top}


def _parse_etime(text: str) -> int | None:
    """Parse a macOS ``ps`` elapsed-time field (``[[dd-]hh:]mm:ss``) to whole seconds."""
    try:
        days = 0
        if "-" in text:
            day_text, text = text.split("-", 1)
            days = int(day_text)
        pieces = [int(piece) for piece in text.split(":")]
        while len(pieces) < 3:
            pieces.insert(0, 0)
        hours, minutes, seconds = pieces[-3], pieces[-2], pieces[-1]
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except (ValueError, IndexError):
        return None


def _running_app_durations(limit: int = 22) -> dict:
    """Currently-running GUI apps and how many hours each has been open, from ``ps`` process
    elapsed time — a reliable 'what's running and for how long' that needs no permissions and
    works even when Screen Time's deeper history is locked. Only top-level app bundles are
    counted (nested helper processes — browser renderers, etc. — are skipped). App → hours."""
    import re

    # `command=` gives the full executable path (unlike `comm=`, which truncates to the short
    # accounting name); macOS uses `etime` (formatted), not Linux's `etimes`.
    output = _run_command(["ps", "-axo", "etime=,command="], timeout=6)
    if not output:
        return {}
    # A top-level app runs from ``…/Applications/<Name>.app/Contents/MacOS/`` — a helper's
    # ``.app`` sits under Frameworks/Helpers and never directly under an Applications dir.
    top_level = re.compile(r"/Applications/([^/]+)\.app/Contents/MacOS/")
    durations: dict[str, float] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        match = top_level.search(parts[1])
        seconds = _parse_etime(parts[0])
        if match is None or seconds is None:
            continue
        app = match.group(1)
        hours = round(seconds / 3600, 1)
        if hours >= durations.get(app, -1.0):
            durations[app] = hours
    top = sorted(durations, key=lambda name: durations[name], reverse=True)[:limit]
    return {name: durations[name] for name in top}


def _login_items(limit: int = 25) -> list[str]:
    """Applications set to launch at login — what the user always wants running from the
    moment they sit down. Read via System Events; best-effort, empty if unavailable."""
    import subprocess

    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get the name of every login item'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [part.strip() for part in result.stdout.split(",") if part.strip()][:limit]


def _homebrew_packages(limit: int = 80) -> dict:
    """The tools the user deliberately installed with Homebrew — one of the sharpest signals
    of their toolset and interests. ``leaves`` are the top-level formulae they asked for (not
    pulled in as dependencies); casks are GUI apps. Names only. Empty when brew is absent."""
    profile: dict = {}
    formulae = _run_command(["brew", "leaves"], timeout=8)
    if formulae:
        profile["formulae"] = formulae.split()[:limit]
    casks = _run_command(["brew", "list", "--cask"], timeout=8)
    if casks:
        profile["casks"] = casks.split()[:limit]
    return profile


def _editor_extensions(limit: int = 70) -> list[str]:
    """Installed VS Code / Cursor extensions (by id) — a precise signal of the languages,
    frameworks, and tools the user actually works in. Read from the extensions directories;
    the trailing ``-<version>[-platform]`` is stripped so multiple installed versions of the
    same extension collapse to one id. Names only, sorted."""
    import re

    names: set[str] = set()
    for base in (".vscode/extensions", ".cursor/extensions", ".vscode-insiders/extensions"):
        try:
            for entry in os.scandir(Path.home() / base):
                if entry.is_dir(follow_symlinks=False) and "-" in entry.name and not entry.name.startswith("."):
                    # Dir names look like `publisher.name-1.2.3` or `…-1.2.3-darwin-arm64`;
                    # drop everything from the first `-<digit>` to the end.
                    names.add(re.sub(r"-\d.*$", "", entry.name))
        except OSError:
            continue
    return sorted(names)[:limit]


def _appearance() -> dict:
    """Small interface preferences that colour the user's day — light vs dark mode. Instant
    ``defaults`` read; light mode simply has no key set. Empty off macOS."""
    style = _run_command(["defaults", "read", "-g", "AppleInterfaceStyle"])
    if not style:
        return {}
    return {"theme": "dark" if style.strip() == "Dark" else "light"}


def _bluetooth_devices(limit: int = 15) -> list[str]:
    """Connected Bluetooth peripherals (keyboard, mouse, headphones, trackpad, …) — part of
    the user's physical setup. From ``system_profiler`` (slow, so tightly timed); degrades to
    empty. Names only."""
    import json as json_module

    raw = _run_command(["system_profiler", "-json", "SPBluetoothDataType"], timeout=8)
    if not raw:
        return []
    try:
        data = json_module.loads(raw)
    except Exception:
        return []
    names: dict[str, None] = {}
    for section in data.get("SPBluetoothDataType", []):
        if not isinstance(section, dict):
            continue
        for key in ("device_connected", "device_not_connected"):
            for entry in section.get(key, []) if isinstance(section.get(key), list) else []:
                if isinstance(entry, dict):
                    for name in entry:
                        names.setdefault(name, None)
    return list(names)[:limit]


def _file_type_activity(limit: int = 14, scan_limit: int = 6000) -> dict:
    """What the user works with, by file type: a histogram of file extensions across their
    Desktop, Downloads, and Documents. Reveals whether the day is code, images, PDFs,
    spreadsheets, media, … Counts only, never file names."""
    home = Path.home()
    counts: dict[str, int] = {}
    scanned = 0
    for folder in (home / "Desktop", home / "Downloads", home / "Documents"):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if scanned >= scan_limit:
                break
            scanned += 1
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            extension = Path(entry.name).suffix.lower().lstrip(".")
            if extension and len(extension) <= 12:
                counts[extension] = counts.get(extension, 0) + 1
    return _top_counter(counts, limit)


def _home_dotfiles(limit: int = 64) -> list[str]:
    """Every hidden dotfile and dot-directory at the top of the home directory, by name — a
    dense fingerprint of the shells, tools, and services the user has configured (``.ssh``,
    ``.aws``, ``.docker``, ``.config``, ``.vimrc``, ``.gitconfig``, …). Names only, sorted.
    Empty on failure. Complements ``home_layout`` (the visible folders)."""
    home = Path.home()
    try:
        return sorted(
            entry.name for entry in os.scandir(home)
            if entry.name.startswith(".") and entry.name not in (".", "..")
        )[:limit]
    except OSError:
        return []


def _recent_entries_by_mtime(
    roots: list[Path], want_directories: bool, limit: int, scan_limit: int = 4000
) -> list[str]:
    """The most recently modified non-hidden entries (files or directories, per
    ``want_directories``) directly under the given roots, newest first, as ``~``-relative
    paths, deduped. A cheap, dependency-free view of what the user has been touching lately.
    Scanning is capped so a huge folder cannot stall the probe."""
    candidates: list[tuple[float, str]] = []
    scanned = 0
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if scanned >= scan_limit:
                break
            scanned += 1
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False) != want_directories:
                    continue
                modified = entry.stat().st_mtime
            except OSError:
                continue
            candidates.append((modified, _home_relative(entry.path)))
    candidates.sort(key=lambda item: item[0], reverse=True)
    ordered: dict[str, None] = {}
    for _, path in candidates:
        ordered.setdefault(path, None)
    return list(ordered)[:limit]


def _recent_files(roots: list[Path], limit: int = 18) -> list[str]:
    """The most recently modified files directly under the given roots — what the user has
    been editing/creating lately."""
    return _recent_entries_by_mtime(roots, want_directories=False, limit=limit)


def _recent_directories(roots: list[Path], limit: int = 15) -> list[str]:
    """The most recently modified sub-directories directly under the given roots — a
    folder-level view of the user's *current* focus (the projects and folders touched most
    recently), complementing ``frequent_directories`` (all-time navigation counts)."""
    return _recent_entries_by_mtime(roots, want_directories=True, limit=limit)


def _frequent_directories(limit: int = 15) -> dict[str, int]:
    """Directory → count of how often the user changed into it (``cd``/``z``/``pushd`` …),
    mined from shell history. The clearest signal of where the user actually works. Only
    destinations that resolve to a directory *still on disk* are kept — long-deleted or
    unresolvable relative targets are dropped as noise — and each is rendered ``~``-relative."""
    counts: dict[str, int] = {}
    for line in _iter_history_lines():
        tokens = line.split()
        if not tokens or tokens[0] not in _DIRECTORY_JUMP_COMMANDS:
            continue
        target = next((token for token in tokens[1:] if not token.startswith("-")), None)
        if not target or target in ("..", "...", "~", "/") or target.startswith("$"):
            continue
        path = Path(target).expanduser()
        # A relative target (``cd src``) cannot be located without the working directory it
        # was run from, and a path that no longer exists is dead weight; keep only absolute
        # destinations that are still real directories.
        if not path.is_absolute() or not path.is_dir():
            continue
        rendered = _home_relative(path)
        counts[rendered] = counts.get(rendered, 0) + 1
    return _top_counter(counts, limit)


def _shell_activity_timeline() -> dict:
    """The *rhythm* of the user's work, mined from timestamped shell history: how their commands
    distribute across the 24 hours of the day and the 7 days of the week, the span the history
    covers, and when they were last active. This is the temporal backbone of the snapshot — it
    tells the agent *when* the user works, not just what they run. Timestamps only; no command
    text is read here. Returns an empty dict when no timestamped history is available."""
    home = Path.home()
    timestamps: list[int] = []
    for path, fmt in ((home / ".zsh_history", "zsh"), (home / ".local/share/fish/fish_history", "fish")):
        try:
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if fmt == "zsh":
                # ": <epoch>:<elapsed>;<command>"
                if not line.startswith(":"):
                    continue
                colon = line.find(":", 1)
                if colon == -1:
                    continue
                try:
                    timestamps.append(int(line[1:colon].strip()))
                except ValueError:
                    continue
            else:  # fish: "  when: <epoch>"
                stripped = line.strip()
                if stripped.startswith("when:"):
                    try:
                        timestamps.append(int(stripped[len("when:"):].strip()))
                    except ValueError:
                        continue
    # A handful of timestamps (e.g. zsh EXTENDED_HISTORY only recently enabled) produces a
    # misleading "timeline" — omit it entirely rather than imply a rhythm from a few points.
    if len(timestamps) < 30:
        return {}
    by_hour: dict[int, int] = {}
    by_iso_weekday: dict[int, int] = {}
    for epoch in timestamps:
        try:
            moment = datetime.fromtimestamp(epoch)
        except (OverflowError, OSError, ValueError):
            continue
        by_hour[moment.hour] = by_hour.get(moment.hour, 0) + 1
        # ISO 8601 weekday: locale-independent, Monday=1 … Sunday=7 (no culture-specific day
        # names or week-start assumptions bleed into the snapshot).
        weekday = moment.isoweekday()
        by_iso_weekday[weekday] = by_iso_weekday.get(weekday, 0) + 1
    timeline: dict = {
        "commands_recorded": len(timestamps),
        "history_span_days": round((max(timestamps) - min(timestamps)) / 86400, 1),
        "last_active": datetime.fromtimestamp(max(timestamps)).strftime("%Y-%m-%d %H:%M"),
        # Hours are the 24-hour clock (0–23); weekdays are ISO 8601 (Monday=1 … Sunday=7).
        "commands_by_hour_of_day": {str(hour): by_hour.get(hour, 0) for hour in range(24)},
        "commands_by_iso_weekday": {str(day): by_iso_weekday.get(day, 0) for day in range(1, 8)},
    }
    return timeline


def _hardware_profile() -> dict:
    """The machine's silicon: model identifier, CPU/chip, core count, and memory. Read from
    ``sysctl`` (instant, no permissions) so the agent knows the horsepower it is working with —
    what will run locally, how much parallelism is reasonable. Empty off macOS."""
    profile: dict = {}
    model = _run_command(["sysctl", "-n", "hw.model"])
    if model:
        profile["model_identifier"] = model
    chip = _run_command(["sysctl", "-n", "machdep.cpu.brand_string"])
    if chip:
        profile["cpu"] = chip
    cores = _run_command(["sysctl", "-n", "hw.ncpu"])
    if cores.isdigit():
        profile["cpu_cores"] = int(cores)
    memory = _run_command(["sysctl", "-n", "hw.memsize"])
    if memory.isdigit():
        profile["memory_gb"] = round(int(memory) / 1024 ** 3)
    return profile


def _hardware_status() -> dict:
    """Live hardware and connectivity status: battery charge and power source, whether Wi-Fi is
    connected (with signal strength, and the network name when macOS discloses it), the attached
    displays, and connected USB peripherals. Tells the agent whether the user is on battery or
    plugged in, on Wi-Fi, and what is physically attached. From ``pmset``/``system_profiler``;
    each field degrades to absent. Empty off macOS."""
    import json as json_module
    import re

    status: dict = {}

    # Battery + power source — pmset is instant and needs no permission.
    battery = _run_command(["pmset", "-g", "batt"])
    if battery:
        source = re.search(r"drawing from '([^']+)'", battery)
        if source:
            status["power_source"] = source.group(1)  # 'AC Power' / 'Battery Power'
        detail = re.search(r"(\d+)%; ([^;]+);(?:\s*(\d+:\d\d) remaining)?", battery)
        if detail:
            status["battery_percent"] = int(detail.group(1))
            state = detail.group(2).strip()
            if state:
                status["battery_state"] = state  # charged / charging / discharging
            remaining = detail.group(3)
            if remaining and remaining != "0:00":
                status["battery_time_remaining"] = remaining

    # Wi-Fi. macOS discloses the SSID only to a process holding Location Services permission,
    # so the connection state and signal (which are not gated) are always reported and the
    # network name is added when the system returns it.
    airport = _run_command(["system_profiler", "-json", "SPAirPortDataType"], timeout=8)
    if airport:
        try:
            data = json_module.loads(airport)
            for controller in data.get("SPAirPortDataType", []):
                interfaces = controller.get("spairport_airport_interfaces", []) if isinstance(controller, dict) else []
                current = next(
                    (interface.get("spairport_current_network_information")
                     for interface in interfaces
                     if isinstance(interface, dict) and isinstance(interface.get("spairport_current_network_information"), dict)),
                    None,
                )
                if current is None:
                    continue
                wifi: dict = {"connected": True}
                network = current.get("_name")
                if network and network != "<redacted>":
                    wifi["network"] = network
                signal = current.get("spairport_signal_noise")
                rssi = re.search(r"(-?\d+)\s*dBm", signal) if isinstance(signal, str) else None
                if rssi:
                    wifi["signal_dbm"] = int(rssi.group(1))
                channel = current.get("spairport_network_channel")
                if channel:
                    wifi["channel"] = channel
                status["wifi"] = wifi
                break
        except Exception:
            pass

    # Attached displays, by name.
    displays_raw = _run_command(["system_profiler", "-json", "SPDisplaysDataType"], timeout=8)
    if displays_raw:
        try:
            data = json_module.loads(displays_raw)
            names = [
                screen.get("_name")
                for gpu in data.get("SPDisplaysDataType", []) if isinstance(gpu, dict)
                for screen in gpu.get("spdisplays_ndrvs", []) if isinstance(screen, dict) and screen.get("_name")
            ]
            if names:
                status["displays"] = names
        except Exception:
            pass

    # Connected USB peripherals — external only; internal controllers, buses, and hubs are skipped.
    usb_raw = _run_command(["system_profiler", "-json", "SPUSBDataType"], timeout=8)
    if usb_raw:
        try:
            data = json_module.loads(usb_raw)
            peripherals: list[str] = []

            def _collect(items: list) -> None:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("_name")
                    if name and not any(marker in name for marker in ("Controller", "Bus", "Hub", "Root")):
                        peripherals.append(name)
                    _collect(item.get("_items", []))

            _collect(data.get("SPUSBDataType", []))
            if peripherals:
                status["usb_devices"] = peripherals[:15]
        except Exception:
            pass

    return status


def _locale_profile() -> dict:
    """The user's region and language settings — locale, preferred languages, measurement units,
    and the system time zone. Tells the agent what language to lean toward, how to format dates,
    numbers, and units, and what "today"/working hours mean locally. Empty off macOS."""
    import re

    profile: dict = {}
    locale = _run_command(["defaults", "read", "-g", "AppleLocale"])
    if locale:
        profile["locale"] = locale
    languages = _run_command(["defaults", "read", "-g", "AppleLanguages"])
    preferred = re.findall(r'"([^"]+)"', languages)
    if preferred:
        profile["preferred_languages"] = preferred[:6]
    units = _run_command(["defaults", "read", "-g", "AppleMeasurementUnits"])
    if units:
        profile["measurement_units"] = units
    try:
        link = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        if marker in link:
            profile["timezone"] = link.split(marker, 1)[1]
    except OSError:
        pass
    return profile


def _git_identity() -> dict:
    """The user's global Git identity and preferences — name, email, default branch, and editor.
    Lets the agent author commits and configuration that match the user rather than guessing.
    Empty when Git is absent or unconfigured."""
    identity: dict = {}
    for key, field in (
        ("user.name", "name"), ("user.email", "email"),
        ("init.defaultBranch", "default_branch"), ("core.editor", "editor"),
    ):
        value = _run_command(["git", "config", "--global", key])
        if value:
            identity[field] = value
    return identity


def _dock_applications(limit: int = 30) -> list[str]:
    """The applications the user has pinned to their Dock, in order — a curated, high-signal list
    of the tools they value most (distinct from everything merely installed). Names only. Empty
    when the Dock preference cannot be read."""
    import re

    raw = _run_command(["defaults", "read", "com.apple.dock", "persistent-apps"])
    if not raw:
        return []
    labels = re.findall(r'"file-label"\s*=\s*"?([^";]+?)"?\s*;', raw)
    ordered: dict[str, None] = {}
    for label in labels:
        ordered.setdefault(label.strip(), None)
    return list(ordered)[:limit]


# Config files/directories whose presence reveals a tool the user has set up, mapped to the tool
# name reported. Checked under the home directory; first hit per tool wins.
_TOOLING_MARKERS = {
    ".vimrc": "vim", ".config/nvim": "neovim", ".tmux.conf": "tmux",
    ".config/fish": "fish", ".zshrc": "zsh", ".bashrc": "bash",
    ".config/starship.toml": "starship", ".p10k.zsh": "powerlevel10k", ".oh-my-zsh": "oh-my-zsh",
    "Library/Application Support/Code/User/settings.json": "vscode",
    ".cargo": "rust", ".rustup": "rust", ".nvm": "nvm", ".pyenv": "pyenv",
    ".rbenv": "rbenv", ".config/direnv": "direnv", ".config/nix": "nix", ".nixpkgs": "nix",
    ".docker": "docker", ".kube": "kubernetes", ".aws": "aws", ".gcloud": "gcloud",
    ".config/gh": "github-cli", ".ssh/config": "ssh", ".gnupg": "gpg", ".config/direnv/direnvrc": "direnv",
}


def _tooling_preferences() -> dict:
    """The user's environment and tooling preferences: shell/editor environment variables and the
    developer tools their config files reveal they have set up. Rounds out *how* the user likes to
    work at the terminal. Env values are plain strings (editor/pager names), never secrets."""
    profile: dict = {}
    # NOTE ON NAMING: ``$EDITOR`` is the *terminal fallback* editor (what pops up for a git
    # commit message), not the user's real IDE — it is labelled ``cli_editor`` so it is never
    # mistaken for how they actually edit code. Their true editor shows up in app usage,
    # launch counts, default-app handlers, and editor extensions, which weigh far more.
    for variable, label in (("EDITOR", "cli_editor"), ("VISUAL", "cli_visual_editor"),
                            ("PAGER", "pager"), ("TERM_PROGRAM", "terminal")):
        value = os.environ.get(variable)
        if value:
            profile[label] = value
    home = Path.home()
    configured = {
        tool for relative, tool in _TOOLING_MARKERS.items() if (home / relative).exists()
    }
    if configured:
        profile["configured_tools"] = sorted(configured)
    return profile


def _spotlight_app_usage(limit: int = 24) -> dict:
    """How often the user actually launches each app, from Spotlight's ``kMDItemUseCount`` —
    the real 'which apps do they live in' ranking, and it needs **no** Full Disk Access. This
    is the authoritative signal for the user's true tools (their editor, browser, terminal),
    far stronger than any config default. App name → launch count. Empty if unavailable."""
    import re

    app_paths: list[str] = []
    for directory in (Path("/Applications"), Path.home() / "Applications", Path("/System/Applications")):
        try:
            app_paths += [entry.path for entry in os.scandir(directory) if entry.name.endswith(".app")]
        except OSError:
            continue
    if not app_paths:
        return {}
    # One mdls call for every app; attributes print alphabetically, so FSName precedes UseCount.
    output = _run_command(["mdls", "-name", "kMDItemFSName", "-name", "kMDItemUseCount", *app_paths], timeout=12)
    if not output:
        return {}
    counts: dict[str, int] = {}
    current: str | None = None
    for line in output.splitlines():
        name_match = re.match(r'\s*kMDItemFSName\s*=\s*"(.+?)"', line)
        if name_match:
            current = name_match.group(1)
            continue
        count_match = re.match(r"\s*kMDItemUseCount\s*=\s*(\d+)", line)
        if count_match and current:
            app = current[:-4] if current.endswith(".app") else current
            counts[app] = int(count_match.group(1))
            current = None
    top = sorted(counts, key=lambda name: counts[name], reverse=True)[:limit]
    return {name: counts[name] for name in top if counts[name] > 0}


# Content types worth reporting a default handler for, mapped to a human label. Reveals which
# app the user has chosen to open each kind of thing — the truest statement of preference.
_HANDLER_CONTENT_TYPES = {
    "public.html": "web pages",
    "public.python-script": "Python files",
    "public.source-code": "source code",
    "public.shell-script": "shell scripts",
    "public.plain-text": "plain text",
    "net.daringfireball.markdown": "Markdown",
    "public.json": "JSON",
    "com.adobe.pdf": "PDFs",
    "public.jpeg": "images",
    "public.movie": "video",
}


def _default_handlers() -> dict:
    """The app the user has set as the *default* for each kind of file, plus their default web
    browser — a direct statement of preference (e.g. source code → their real editor). Parsed
    from LaunchServices. Empty if unreadable. Values are bundle ids."""
    import json as json_module

    plist = Path.home() / "Library" / "Preferences" / "com.apple.LaunchServices" / "com.apple.launchservices.secure.plist"
    if not plist.is_file():
        return {}
    raw = _run_command(["plutil", "-convert", "json", "-o", "-", str(plist)], timeout=5)
    if not raw:
        return {}
    try:
        data = json_module.loads(raw)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for handler in data.get("LSHandlers", []):
        if not isinstance(handler, dict):
            continue
        role = handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleEditor") or handler.get("LSHandlerRoleViewer")
        if not role:
            continue
        if handler.get("LSHandlerURLScheme") in ("http", "https"):
            result.setdefault("web browser", role)
        content_type = handler.get("LSHandlerContentType")
        if content_type in _HANDLER_CONTENT_TYPES:
            result[_HANDLER_CONTENT_TYPES[content_type]] = role
    return result


def _recently_used_files(limit: int = 20) -> list[str]:
    """Files the user has actually **opened** in the last week (``kMDItemLastUsedDate``), not
    merely modified — a direct access-pattern signal from Spotlight, no Full Disk Access
    needed. ``~``-relative paths; library/cache/trash noise is filtered out."""
    output = _run_command(
        ["mdfind", "-onlyin", str(Path.home()), "kMDItemLastUsedDate >= $time.this_week"],
        timeout=8,
    )
    if not output:
        return []
    files: list[str] = []
    for path in output.splitlines():
        path = path.strip()
        if not path or path.endswith(".app"):
            continue
        if "/Library/" in path or "/.Trash/" in path or "/node_modules/" in path or "/.git/" in path:
            continue
        files.append(_home_relative(path))
        if len(files) >= limit:
            break
    return files


# Shell-config markers → the tool they reveal is loaded. Presence in ~/.zshrc means the user
# has wired it into their terminal, a strong statement of how they work.
_SHELL_TOOL_MARKERS = (
    "nvm", "pyenv", "rbenv", "conda", "direnv", "starship", "zoxide", "fnm", "rustup",
    "sdkman", "thefuck", "powerlevel10k", "fzf", "atuin", "mise", "asdf", "volta",
)


def _shell_setup() -> dict:
    """The shape of the user's terminal from ``~/.zshrc``: the framework and plugins they load
    (oh-my-zsh + its plugin list), the version managers / tools they source (nvm, pyenv, conda,
    direnv, starship, …), and how many aliases they have — i.e. how customized, and toward
    what, their shell is. No command text or secrets. Empty if there is no zshrc."""
    import re

    try:
        text = (Path.home() / ".zshrc").read_text(errors="replace")
    except OSError:
        return {}
    profile: dict = {}
    plugins_match = re.search(r"^\s*plugins=\(([^)]*)\)", text, re.MULTILINE)
    if plugins_match:
        plugins = [plugin for plugin in plugins_match.group(1).split() if plugin]
        if plugins:
            profile["oh_my_zsh_plugins"] = plugins[:30]
    loaded = sorted({marker for marker in _SHELL_TOOL_MARKERS if re.search(r"\b" + re.escape(marker) + r"\b", text)})
    if loaded:
        profile["loaded_tools"] = loaded
    alias_count = len(re.findall(r"^\s*alias\s+\w", text, re.MULTILINE))
    if alias_count:
        profile["alias_count"] = alias_count
    return profile


def probe_user_context() -> str:
    """An opt-in JSON snapshot of *who the user is and how they work on this machine* — as
    complete a picture as local metadata allows. It spans where they work (frequent directories,
    home layout, recent files), what they work with (installed, running, and Dock-pinned apps;
    configured developer tooling; hardware), who they are (Git identity, locale, languages, time
    zone), when they work (a shell-activity timeline across hours and weekdays), and what they
    are into (most-visited and recently-active sites).

    This is deliberately far more personal than :func:`probe_local_environment` and is only ever
    built when the user has enabled it in settings. Everything here is local metadata — filesystem
    timestamps, application bundles, browser-history visit *counts and timestamps*, system
    preferences — reduced to names, counts, and times so no file contents or browsing content
    travel with it. Every sub-probe degrades to silence on failure; empty sections are omitted."""
    home = Path.home()
    payload: dict = {}

    frequent_directories = _frequent_directories()
    if frequent_directories:
        payload["frequent_directories"] = frequent_directories

    try:
        home_layout = sorted(
            entry.name for entry in os.scandir(home)
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
        )
        if home_layout:
            payload["home_layout"] = home_layout
    except OSError:
        pass

    # Recent activity: the standard drop folders plus the directories the user actually works
    # in. Files show what they have been editing; sub-directories show which projects/folders
    # they have most recently touched (current focus, vs. frequent_directories' all-time count).
    recent_roots = [home / "Desktop", home / "Downloads", home / "Documents"]
    for relative in frequent_directories:
        candidate = Path(relative.replace("~", str(home), 1)) if relative.startswith("~") else Path(relative)
        if candidate.is_dir() and candidate not in recent_roots:
            recent_roots.append(candidate)
    existing_roots = [root for root in recent_roots if root.is_dir()]

    # Simple sections keyed by their probe: only non-empty results are attached.
    for key, value in (
        ("home_dotfiles", _home_dotfiles()),
        ("recent_files", _recent_files(existing_roots)),
        ("recently_active_directories", _recent_directories(existing_roots)),
        ("shell_activity", _shell_activity_timeline()),
        ("system_uptime", _system_uptime()),
        ("hardware", _hardware_profile()),
        ("hardware_status", _hardware_status()),
        ("appearance", _appearance()),
        ("locale", _locale_profile()),
        ("git_identity", _git_identity()),
        ("tooling", _tooling_preferences()),
        ("shell_setup", _shell_setup()),
        ("homebrew", _homebrew_packages()),
        ("editor_extensions", _editor_extensions()),
        ("default_apps", _default_handlers()),
        ("bluetooth_devices", _bluetooth_devices()),
        ("file_type_activity", _file_type_activity()),
        ("recently_used_files", _recently_used_files()),
        ("installed_applications", _installed_applications()),
        ("dock_applications", _dock_applications()),
        ("login_items", _login_items()),
        ("running_applications", _running_applications()),
        ("running_app_durations_hours", _running_app_durations()),
        ("app_launch_counts", _spotlight_app_usage()),
        ("app_usage_hours_last_week", _app_usage()),
    ):
        if value:
            payload[key] = value
    payload.update(_browser_site_activity())

    return json.dumps(payload)
