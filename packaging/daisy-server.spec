# PyInstaller spec that freezes the daisy FastAPI server (server.py) into a
# self-contained binary the Daisy desktop app bundles and spawns for local mode.
#
# The dependency tree is heavy and full of *dynamic* imports (litellm loads
# providers by name, uvicorn[standard] picks loops/protocols at runtime, langchain
# and a2a pull submodules lazily), so PyInstaller's static analysis misses them.
# `collect_all` pulls each package's submodules + data files + binaries, and
# `copy_metadata` keeps the importlib.metadata version lookups those libraries do.
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas = []
binaries = []
hiddenimports = []

# Packages whose submodules/data must be collected wholesale.
_collect = [
    "daisy",
    "litellm",
    "langchain",
    "langchain_core",
    "langchain_text_splitters",
    "langgraph",
    "langgraph_prebuilt",
    "langgraph_checkpoint",
    "a2a",
    "mcp",
    "exa_py",
    "curl_cffi",
    "sse_starlette",
    "aiosqlite",
    "greenlet",
    "markdownify",
    "tiktoken",
    "tiktoken_ext",
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_core",
    "sqlalchemy",
    "watchfiles",
    "anyio",
    "httpx",
    "httpcore",
    "h11",
    "websockets",
    "httptools",
    "yaml",
    "dotenv",
    "certifi",
    "charset_normalizer",
    # Screen-search retrieval (search_screen) and code search (search_code): static embeddings
    # plus BM25 and their data/model plumbing. collect_all pulls each package's data files and
    # dynamic submodules PyInstaller would otherwise miss; any not installed are skipped above.
    "model2vec",
    "semble",
    "tokenizers",
    "safetensors",
    "bm25s",
    "vicinity",
    "tree_sitter_language_pack",
    "joblib",
    "numpy",
]

for package in _collect:
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hiddenimports
    except Exception as error:  # noqa: BLE001 - a missing optional package must not abort the freeze
        print(f"[daisy-server.spec] skipping {package}: {error}")

# The shipped agents/skills/MCP defaults live in the repo-root `.agents/` — a SIBLING of
# the `harness` package, so `collect_all("daisy")` never sees them. Bundle them at the
# frozen root as `.agents/...` so `_bundled_dotagents_root()` (frozen-aware, sys._MEIPASS)
# finds them: this is the server-shipped base layer of agents the app always has, and the
# source the app seeds editable copies from on first run. `memories` is user data — not shipped.
import os as _os
_repo_root = _os.path.dirname(SPECPATH)  # SPECPATH is the packaging/ dir holding this spec

# Regenerable runtime artifacts a skill may leave in its source tree — a per-skill uv devshell
# (`.venv`), byte-cache, VCS, or node deps. They are recreated on demand where the skill runs and
# must NOT ride into the freeze: the literature-search skill's committed `.venv` alone is ~145 MB
# (pymupdf/libmupdf, lxml, numpy), which is what bloated the shipped `.agents` from ~10 MB to 80 MB.
_skip_directory_names = {".venv", "venv", "__pycache__", ".git", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _bundle_tree(relative):
    """Add a repo-root tree to `datas` file-by-file, pruning regenerable runtime artifacts.

    A bare `datas.append((dir, dest))` copies the tree wholesale — including any committed
    `.venv`. Walking it ourselves lets us drop the skip-listed directories while preserving the
    layout the frozen-aware loader expects.
    """
    absolute = _os.path.join(_repo_root, relative)
    if not _os.path.isdir(absolute):
        print(f"[daisy-server.spec] WARNING: bundled resource missing: {absolute}")
        return
    for directory, subdirectories, filenames in _os.walk(absolute):
        subdirectories[:] = [name for name in subdirectories if name not in _skip_directory_names]
        for filename in filenames:
            if filename == ".DS_Store":
                continue
            source_file = _os.path.join(directory, filename)
            destination = _os.path.join(relative, _os.path.relpath(directory, absolute))
            datas.append((source_file, destination))


for _relative in (".agents/agents", ".agents/skills"):
    _bundle_tree(_relative)
_mcp = _os.path.join(_repo_root, ".agents", "mcp.json")
if _os.path.isfile(_mcp):
    datas.append((_mcp, ".agents"))

# The automation tools' runtime-loaded assets: the message templates (.md), one folder per
# surface (messages/browser, messages/computer), and the browser selection script (scripts/*.js).
# The tools degrade without them, so bundle every file, preserving its folder, to be certain.
for _asset_subdir in ("messages", "scripts"):
    _asset_source = _os.path.join(_repo_root, "src", "daisy", "computer", _asset_subdir)
    for _dirpath, _dirnames, _filenames in _os.walk(_asset_source):
        for _asset_name in _filenames:
            if _asset_name.endswith((".md", ".js")):
                _relative = _os.path.relpath(_dirpath, _asset_source)
                _destination = _os.path.join("daisy", "computer", _asset_subdir, _relative)
                datas.append((_os.path.join(_dirpath, _asset_name), _destination))

# Distributions whose runtime version is read via importlib.metadata.
for distribution in [
    "litellm",
    "langchain",
    "langchain-core",
    "openai",
    "tiktoken",
    "a2a-sdk",
    "mcp",
    "fastapi",
    "uvicorn",
    "pydantic",
]:
    try:
        datas += copy_metadata(distribution)
    except Exception as error:  # noqa: BLE001
        print(f"[daisy-server.spec] no metadata for {distribution}: {error}")

# uvicorn[standard] resolves these at runtime by string; name them explicitly too.
hiddenimports += [
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

analysis = Analysis(
    ["../server.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="daisy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="daisy",
)

# Wrap the frozen server as a background helper .app. The server — not the desktop app — is
# the process that calls the macOS Accessibility API for the computer-use tool, and TCC lists
# whichever process actually exercises the permission. As a bare executable it would show only
# its filename ("daisy-server"); as a bundle, macOS resolves it to this Info.plist. It carries
# the *same* CFBundleName and bundle identifier as the desktop app, so it folds into the app's
# single "Daisy" Accessibility entry rather than adding a second one. The bundle file itself is
# named "Daisy Computer Use.app" for clarity on disk. A pure background helper (LSUIElement),
# launched by the desktop app and never shown in the Dock.
app = BUNDLE(
    collection,
    name="Daisy Computer Use.app",
    icon=None,
    bundle_identifier="com.ghovax.daisy",
    info_plist={
        "CFBundleName": "Daisy",
        "CFBundleDisplayName": "Daisy",
        "LSUIElement": True,
    },
)
