# PyInstaller spec freezing LangMesh into one signed binary whose first argument selects `langmesh` or `langmeshd`, so the fleet is one TCC row.
# The desktop app neither contains nor starts the daemon, so this produces the daemon's own installable artifact.
# `collect_all` and `copy_metadata` are used throughout, because litellm, uvicorn, langchain and a2a import dynamically and static analysis misses it.
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas = []
binaries = []
hiddenimports = []

# Packages whose submodules/data must be collected wholesale.
_collect = [
    "langmesh",
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
    "minify_html",
    "bs4",
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
    # Screen-search retrieval: static embeddings whose data files and dynamic submodules `collect_all` pulls in; any not installed are skipped.
    "model2vec",
    "tokenizers",
    "safetensors",
    "vicinity",
    "tree_sitter_language_pack",
    "joblib",
    "numpy",
    # Dictation's speech model and array framework: `mlx._reprlib_fix` is named by nothing, and without it `import mlx.core` failed in the packaged app alone.
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
        print(f"[langmesh-daemon.spec] skipping {package}: {error}")

# The shipped `.agents/` defaults sit beside the package where `collect_all` never looks, so they are bundled at the frozen root; `memories` is user data.
import os as _os
_repo_root = _os.path.dirname(SPECPATH)  # SPECPATH is the packaging/ dir holding this spec

# Regenerable per-skill artifacts, recreated where the skill runs: one committed `.venv` alone took the shipped `.agents` from ~10 MB to 80 MB.
_skip_directory_names = {".venv", "venv", "__pycache__", ".git", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _bundle_tree(relative):
    """Add a repo-root tree to `datas` file-by-file, pruning regenerable runtime artifacts.

    A bare `datas.append((dir, dest))` copies the tree wholesale — including any committed
    `.venv`. Walking it ourselves lets us drop the skip-listed directories while preserving the
    layout the frozen-aware loader expects.
    """
    absolute = _os.path.join(_repo_root, relative)
    if not _os.path.isdir(absolute):
        print(f"[langmesh-daemon.spec] WARNING: bundled resource missing: {absolute}")
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

# The built interface, flattened from `web/out` to the layout the server expects, so `langmesh web` works installed; absent, the freeze still succeeds.
_interface = _os.path.join(_repo_root, "web", "out")
if _os.path.isdir(_interface):
    for _directory, _subdirectories, _filenames in _os.walk(_interface):
        for _filename in _filenames:
            if _filename == ".DS_Store":
                continue
            _source = _os.path.join(_directory, _filename)
            datas.append((_source, _os.path.join("web", _os.path.relpath(_directory, _interface))))
else:
    print("[langmesh-daemon.spec] web/out is absent; `langmesh web` will not work from this build")

# The automation tools' runtime assets — per-surface message templates and browser scripts — bundled whole, since the tools degrade without them.
for _asset_subdir in ("messages", "scripts"):
    _asset_source = _os.path.join(_repo_root, "src", "langmesh", "computer", _asset_subdir)
    for _dirpath, _dirnames, _filenames in _os.walk(_asset_source):
        for _asset_name in _filenames:
            if _asset_name.endswith((".md", ".js")):
                _relative = _os.path.relpath(_dirpath, _asset_source)
                _destination = _os.path.join("langmesh", "computer", _asset_subdir, _relative)
                datas.append((_os.path.join(_dirpath, _asset_name), _destination))

# The tokenizer's vocabulary, fetched at build time because `tiktoken` downloads it on first use, which left a frozen build offline with no tokenizer.
_VOCABULARY_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
import hashlib as _hashlib
import urllib.request as _urllib_request

_vocabulary_cache = _os.path.join(_repo_root, "packaging", "build", "tiktoken-cache")
_os.makedirs(_vocabulary_cache, exist_ok=True)
_vocabulary_file = _os.path.join(_vocabulary_cache, _hashlib.sha1(_VOCABULARY_URL.encode()).hexdigest())
if not _os.path.isfile(_vocabulary_file):
    print(f"[langmesh-daemon.spec] fetching the tokenizer vocabulary from {_VOCABULARY_URL}")
    with _urllib_request.urlopen(_VOCABULARY_URL, timeout=120) as _response:
        _payload = _response.read()
    with open(_vocabulary_file, "wb") as _handle:
        _handle.write(_payload)
datas.append((_vocabulary_file, _os.path.join("langmesh", "tokenizer")))

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
        print(f"[langmesh-daemon.spec] no metadata for {distribution}: {error}")

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
    name="langmesh",
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
    name="langmesh",
)

# Wrapped as a background .app under the desktop app's bundle identifier, so Accessibility is one TCC entry; LSUIElement keeps it out of the Dock.
app = BUNDLE(
    collection,
    name="LangMesh Computer Use.app",
    icon=None,
    bundle_identifier="com.ghovax.langmesh",
    info_plist={
        "CFBundleName": "LangMesh",
        "CFBundleDisplayName": "LangMesh",
        "LSUIElement": True,
    },
)
