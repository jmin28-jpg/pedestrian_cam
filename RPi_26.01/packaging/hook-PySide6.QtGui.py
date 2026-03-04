# Override PyInstaller's standard hook for PySide6.QtGui.
# We intentionally do not add extra Qt plugins here because PySide6 is already collected via packaging/hook-PySide6.py
hiddenimports = []
datas = []
binaries = []