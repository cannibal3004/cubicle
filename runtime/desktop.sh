#!/bin/sh
# Boot the sandbox desktop as the sandbox user, then hand over to the driver.
set -e

# Whatever the sandbox creates in /workspace and the shared folder must stay
# changeable by the host API, which runs as a different user, so everything
# started from here (the driver, its shells, the desktop apps) creates files
# writable by everyone. The API does the same for what it writes there.
umask 000

WIDTH="${VIEWPORT_WIDTH:-1280}"
HEIGHT="${VIEWPORT_HEIGHT:-720}"
export DISPLAY="${DISPLAY:-:1}"
export HOME="${HOME:-/home/pwuser}"
export XDG_CONFIG_HOME="$HOME/.config"
# Caches (pip, npm, browsers) live in the persistent home, not in /tmp, so
# they survive a restart and do not count against the tmpfs.
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)"
# Plank stores its settings through GSettings; the keyfile backend lets the
# image seed them (skel/.config/glib-2.0/settings/keyfile) without dconf.
export GSETTINGS_BACKEND=keyfile
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CACHE_HOME" "$HOME/.config"
chmod 700 "$XDG_RUNTIME_DIR"

# The home directory persists between starts. Seed the desktop configuration
# (dock items, GTK theme, window manager) from the image, without replacing
# anything that is already there.
cp -Rn /opt/cubicle-computer/skel/. "$HOME/" 2>/dev/null || true

# Chromium profile, in the home directory so logins and settings persist.
# Use the window manager's title bar rather than Chrome's own, so the "no
# minimize button" rule applies to the browser too; there is no task list to
# restore a minimized window from.
PROFILE="$HOME/.config/cubicle-chromium"
if [ ! -f "$PROFILE/Default/Preferences" ]; then
    mkdir -p "$PROFILE/Default"
    printf '{"browser":{"custom_chrome_frame":false,"check_default_browser":false}}' > "$PROFILE/Default/Preferences"
fi
# A profile left locked by a previous container would refuse to open.
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"

# X server with VNC built in. Loopback only; the driver bridges it over the
# token-protected port, so nothing unauthenticated reaches the network.
Xtigervnc "$DISPLAY" -geometry "${WIDTH}x${HEIGHT}" -depth 24 -rfbport 5900 -localhost \
    -SecurityTypes None -AlwaysShared -AcceptSetDesktopSize=0 -desktop "Cubicle" \
    >/tmp/xvnc.log 2>&1 &

i=0
until xdpyinfo >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 100 ]; then
        echo "X server did not start" >&2
        cat /tmp/xvnc.log >&2 || true
        exit 1
    fi
    sleep 0.1
done

eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS

# Wallpaper: the generated default, or a file mounted over it by the host
# (COMPUTER_DOCKER_WALLPAPER). Fall back to a flat colour if it cannot load.
xsetroot -solid '#15171c'
# A real arrow over the desktop: without a root cursor the X server reports an
# empty one and the VNC viewer hides the pointer while the user controls it.
xsetroot -cursor_name left_ptr
feh --no-fehbg --bg-fill /opt/cubicle-computer/wallpaper >/tmp/feh.log 2>&1 || true

# The compositor gives Plank its transparent theme and smooth hiding.
xfwm4 --compositor=on >/tmp/xfwm4.log 2>&1 &
sleep 0.5
plank >/tmp/plank.log 2>&1 &

exec node /opt/cubicle-computer/driver.mjs
