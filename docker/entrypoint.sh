#!/bin/sh
# Drop root after making the Recertia data root writable by uid 65532.
# Compose named volumes are created as root; the process user cannot mkdir them.
set -eu

uid=65532
gid=65532
data="${RECERTIA_DATA_ROOT:-/home/recertia/.recertia}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data"
    chown "$uid:$gid" "$data"
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$uid" --regid="$gid" --init-groups -- "$@"
    fi
    echo "recertia-entrypoint: setpriv is missing; cannot drop root" >&2
    exit 1
fi

exec "$@"
