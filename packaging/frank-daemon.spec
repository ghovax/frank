# PyInstaller spec that freezes Frank into one self-contained binary you install. It is a
# single image with three entry points — `frank` (the CLI), `frankd` (the daemon) and
# `prototype` (the process sessions are forked out of) — selected by the first argument,
# because the prototype must be a re-exec of the *same signed binary* for macOS to treat it as
# the same code identity and keep one Accessibility grant covering every session. Sessions
# themselves are forks of the prototype, and a fork inherits the parent's signature, so the
# whole fleet stays one TCC row without any session ever being exec'd.
#
# The desktop app used to bundle the result of this as a resource and spawn it. It no longer
# does: the app is a client of a daemon it neither contains nor starts, so this produces the
# daemon's own installable artifact.
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
    "frank",
    "litellm",
    "langchain",
    "langchain_core",
    "langchain_text_splitters",
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
    # Dictation: the on-device speech model and the array framework under it. Absent from this
    # list, `import mlx.core` inside the worker failed on `mlx._reprlib_fix` — a submodule
    # nothing references by name, so nothing collected it, so the extension could not
    # initialise. Dictation therefore worked from a checkout and had never once worked in the
    # packaged app, which is a difference no amount of reading either would show.
    "mlx",
    "parakeet_mlx",
]

for package in _collect:
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hiddenimports
    except Exception as error:  # noqa: BLE001 - a missing optional package must not abort the freeze
        print(f"[frank-daemon.spec] skipping {package}: {error}")

# The shipped agents/skills/MCP defaults live in the repo-root `.agents/` — a SIBLING of
# the `harness` package, so `collect_all("frank")` never sees them. Bundle them at the
# frozen root as `.agents/...` so `_bundled_dotagents_root()` (frozen-aware, sys._MEIPASS)
# finds them: this is the harness-shipped base layer of agents the app always has, and the
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
        print(f"[frank-daemon.spec] WARNING: bundled resource missing: {absolute}")
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

# The built interface, so `frank web` works from an installed binary and not only from a
# checkout. ~15 MB against a ~228 MB image, for the difference between "the desktop app is the
# only way to see this" and "any browser is". Flattened from `web/out` to `web/` because the
# `out` is Next.js's build directory name, not part of the layout the server expects. Absent
# when the UI has not been built: the freeze still succeeds and `frank web` says what to run.
_interface = _os.path.join(_repo_root, "web", "out")
if _os.path.isdir(_interface):
    for _directory, _subdirectories, _filenames in _os.walk(_interface):
        for _filename in _filenames:
            if _filename == ".DS_Store":
                continue
            _source = _os.path.join(_directory, _filename)
            datas.append((_source, _os.path.join("web", _os.path.relpath(_directory, _interface))))
else:
    print("[frank-daemon.spec] web/out is absent; `frank web` will not work from this build")

# The automation tools' runtime-loaded assets: the message templates (.md), one folder per
# surface (messages/browser, messages/computer), and the browser selection script (scripts/*.js).
# The tools degrade without them, so bundle every file, preserving its folder, to be certain.
for _asset_subdir in ("messages", "scripts"):
    _asset_source = _os.path.join(_repo_root, "src", "frank", "computer", _asset_subdir)
    for _dirpath, _dirnames, _filenames in _os.walk(_asset_source):
        for _asset_name in _filenames:
            if _asset_name.endswith((".md", ".js")):
                _relative = _os.path.relpath(_dirpath, _asset_source)
                _destination = _os.path.join("frank", "computer", _asset_subdir, _relative)
                datas.append((_os.path.join(_dirpath, _asset_name), _destination))

# The tokenizer's vocabulary, fetched once at build time and carried in the image.
#
# `tiktoken` is a hard dependency but ships no vocabulary: `get_encoding` downloads
# `o200k_base` from a blob store on first use and caches it under a sha1 of that URL. So a
# frozen build that had never run was one network failure away from having no tokenizer, and
# the tokenizer is what every size cap in the harness is measured with. Downloading it here
# — where the network is a given and a failure stops the build — puts the file in the bundle
# and makes the runtime path offline by construction. `frank.base.tuning` points
# `TIKTOKEN_CACHE_DIR` at it.
_VOCABULARY_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
import hashlib as _hashlib
import urllib.request as _urllib_request

_vocabulary_cache = _os.path.join(_repo_root, "packaging", "build", "tiktoken-cache")
_os.makedirs(_vocabulary_cache, exist_ok=True)
_vocabulary_file = _os.path.join(_vocabulary_cache, _hashlib.sha1(_VOCABULARY_URL.encode()).hexdigest())
if not _os.path.isfile(_vocabulary_file):
    print(f"[frank-daemon.spec] fetching the tokenizer vocabulary from {_VOCABULARY_URL}")
    with _urllib_request.urlopen(_VOCABULARY_URL, timeout=120) as _response:
        _payload = _response.read()
    with open(_vocabulary_file, "wb") as _handle:
        _handle.write(_payload)
datas.append((_vocabulary_file, _os.path.join("frank", "tokenizer")))

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
        print(f"[frank-daemon.spec] no metadata for {distribution}: {error}")

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
    ["entry.py"],
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
    name="frank",
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
    name="frank",
)

# Wrap the frozen image as a background .app. A session process — not the desktop app — is the
# process that calls the macOS Accessibility API for the computer-use tool, and TCC lists
# whichever process actually exercises the permission. As a bare executable it would show only
# its filename; as a bundle, macOS resolves it to this Info.plist. It carries the *same*
# CFBundleName and bundle identifier as the desktop app, so the two fold into a single "Frank"
# Accessibility entry rather than adding a second one — which is why this survives the app no
# longer bundling it, and why both are signed with the same certificate. The bundle file is
# named "Frank Computer Use.app" for clarity on disk. LSUIElement, so running the daemon never
# puts an icon in the Dock.
app = BUNDLE(
    collection,
    name="Frank Computer Use.app",
    icon=None,
    bundle_identifier="com.ghovax.frank",
    info_plist={
        "CFBundleName": "Frank",
        "CFBundleDisplayName": "Frank",
        "LSUIElement": True,
    },
)
