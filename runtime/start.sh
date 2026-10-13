#!/bin/sh
# Container entrypoint. The container starts as root with a handful of
# capabilities so that it can fix up the mounted home directory and restore
# the computer's Ubuntu packages; then it drops to the sandbox user with an
# empty bounding set and hands over to desktop.sh. Run as a non-root user
# (an image used outside the provider) it skips straight to the desktop.
set -e

SANDBOX_USER=pwuser
SANDBOX_HOME=/home/pwuser

if [ "$(id -u)" = "0" ]; then
    # The host API creates the home directory (bind mode) as its own user;
    # hand it to the sandbox user before anything writes there.
    chown "$SANDBOX_USER:$SANDBOX_USER" "$SANDBOX_HOME" 2>/dev/null || true
    mkdir -p /var/lib/cubicle/apt

    if [ -n "$CUBICLE_PACKAGES" ]; then
        # shellcheck disable=SC2086
        if ! /opt/cubicle-computer/install-packages.sh $CUBICLE_PACKAGES >/tmp/packages.log 2>&1; then
            echo "Restoring packages failed; see /tmp/packages.log. Continuing without them." >&2
            tail -n 20 /tmp/packages.log >&2 || true
        fi
    fi

    exec setpriv --reuid="$SANDBOX_USER" --regid="$SANDBOX_USER" --init-groups \
        --inh-caps=-all --bounding-set=-all --no-new-privs \
        /opt/cubicle-computer/desktop.sh
fi

exec /opt/cubicle-computer/desktop.sh
