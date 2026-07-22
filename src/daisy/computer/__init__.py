"""Perceiving and driving a live surface — the browser and the native macOS desktop.

The model works this in two phases: ``search_screen`` retrieves the relevant elements (and, on the
web, the page's network exchanges) from the current surface via :mod:`daisy.computer.retrieval`,
and ``control_screen`` composes trusted actions over them, run in a killable subprocess by
:mod:`daisy.computer.control`. The web substrate lives in :mod:`daisy.computer.web` (Chrome over
Playwright) and the native one in :mod:`daisy.computer.engine` (the macOS accessibility tree).

Submodules are imported by the code that needs them, not here: the macOS surface pulls
``ApplicationServices`` and only loads where it can, while the retrieval and control layers are
platform-neutral and import anywhere the server runs.
"""
