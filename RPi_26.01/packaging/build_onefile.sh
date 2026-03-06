#!/bin/bash
set -e

# [PKG-FIX-2] Ensure running from packaging directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Calculate Project Root (parent of packaging)
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "[Build] Starting OPAS-200 OneFile Build for RPi5..."
echo "[Build] Script Dir: $SCRIPT_DIR"
echo "[Build] Project Root: $PROJECT_ROOT"

# 1. Collect Dependencies (Run from Project Root)
echo "[Build] Collecting dependencies..."
cd "$PROJECT_ROOT"
python3 tools/collect_deps.py

# 2. Run PyInstaller (Run from packaging directory)
echo "[Build] Running PyInstaller..."
cd "$SCRIPT_DIR"
pyinstaller --clean --noconfirm opas200.spec

# 3. Move Output
# PyInstaller output is in packaging/dist/OPAS-200
# Move to PROJECT_ROOT/dist/OPAS-200
cd "$PROJECT_ROOT"
mkdir -p dist

if [ -f "$SCRIPT_DIR/dist/OPAS-200" ]; then
    # Remove old file if exists
    rm -f dist/OPAS-200
    mv "$SCRIPT_DIR/dist/OPAS-200" dist/OPAS-200
    
    # Cleanup PyInstaller dist folder in packaging if empty
    rmdir "$SCRIPT_DIR/dist" 2>/dev/null || true

    # 4. Report
    SIZE=$(du -h dist/OPAS-200 | cut -f1)
    echo "---------------------------------------------------"
    echo "[Build] Success! Output: dist/OPAS-200"
    echo "[Build] Size: $SIZE"
    echo "[Build] You can now copy 'dist/OPAS-200' to the target RPi5."
    echo "---------------------------------------------------"
else
    echo "[Build] Error: Output file not found."
    exit 1
fi