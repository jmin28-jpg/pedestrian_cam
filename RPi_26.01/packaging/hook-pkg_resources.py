# Override PyInstaller's standard hook for pkg_resources.
# We intentionally keep this hook empty to avoid setuptools-vendored submodule collection
# that triggers warnings on this platform.
hiddenimports = []
datas = []
binaries = []