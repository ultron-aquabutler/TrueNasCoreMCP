#!/bin/sh
# Read TRUENAS_API_KEY from file if TRUENAS_API_KEY_FILE is set
if [ -n "$TRUENAS_API_KEY_FILE" ] && [ -f "$TRUENAS_API_KEY_FILE" ]; then
    TRUENAS_API_KEY=$(cat "$TRUENAS_API_KEY_FILE")
    export TRUENAS_API_KEY
fi

exec "$@"