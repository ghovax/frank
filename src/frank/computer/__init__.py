"""Perceiving and driving a live surface — the browser and the native macOS desktop.

The model works the screen through one tool, ``control_screen``: its script retrieves the relevant
elements (and, on the web, the page's network exchanges) from the current surface with
``find_one``/``find_many`` via :mod:`frank.computer.retrieval`, and composes trusted actions over
them, run in a killable subprocess by :mod:`frank.computer.control`. The web substrate lives in :mod:`frank.computer.web` (Chrome over
Playwright) and the native one in :mod:`frank.computer.engine` (the macOS accessibility tree).

Submodules are imported by the code that needs them, not here: the macOS surface pulls
``ApplicationServices`` and only loads where it can, while the retrieval and control layers are
platform-neutral and import anywhere a worker runs.
"""
