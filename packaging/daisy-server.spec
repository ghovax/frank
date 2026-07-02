# PyInstaller spec that freezes the harness FastAPI server (server.py) into a
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
    "harness",
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
    "aiofiles",
    "certifi",
    "charset_normalizer",
]

for package in _collect:
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hiddenimports
    except Exception as error:  # noqa: BLE001 - a missing optional package must not abort the freeze
        print(f"[daisy-server.spec] skipping {package}: {error}")

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
    name="daisy-server",
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
    name="daisy-server",
)
