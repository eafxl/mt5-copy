#!/usr/bin/env bash
# Build distributable copy: all .bat and .py files + config, preserving folder structure.
# Excludes venv, .git, __pycache__, tests, dist itself.

set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
DIST="$SRC/dist"

echo "Cleaning dist..."
rm -rf "$DIST"
mkdir -p "$DIST"

echo "Copying files..."
cd "$SRC"
# Assets matter as much as code here: the GUI loads its toolbar glyphs and the
# app icon from src/gui/icons at runtime, so a code-only dist ships a window
# with no icon and blank buttons.
find . -type f \( -name "*.bat" -o -name "*.py" -o -name "*.svg" \
                  -o -name "*.png" -o -name "*.ico" -o -name "*.mq5" \) \
    -not -path "./venv/*" \
    -not -path "./.venv/*" \
    -not -path "./.git/*" \
    -not -path "./dist/*" \
    -not -path "./tests/*" \
    -not -path "./*__pycache__*" \
    -not -name "make_dist.sh" \
    | while read -r f; do
        mkdir -p "$DIST/$(dirname "$f")"
        cp "$f" "$DIST/$f"
    done

# Copy config
if [ -f config/config.yaml ]; then
    mkdir -p "$DIST/config"
    cp config/config.yaml "$DIST/config/"
fi

echo ""
echo "Done. Distribution ready in: $DIST"
echo ""
find "$DIST" -type f | sort
