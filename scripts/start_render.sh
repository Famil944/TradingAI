#!/bin/sh
# Link ROOT/data to persistent Render disk before Python imports initialize SQLite.
set -eu
mkdir -p /var/data
if [ -L data ]; then
    if [ "$(readlink data)" != "/var/data" ]; then
        echo "Unexpected data symlink: refusing to replace it." >&2
        exit 1
    fi
elif [ -e data ]; then
    # Copy any existing ephemeral files before linking the persistent disk.
    # Keep the original as a local backup instead of deleting it.
    cp -an data/. /var/data/
    mv data "data.pre-persist.$$"
fi
if [ ! -L data ]; then
    ln -s /var/data data
fi
exec python main.py
