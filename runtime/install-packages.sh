#!/bin/sh
# Install Ubuntu packages into the sandbox. Runs as root: at boot to restore
# the computer's package list (CUBICLE_PACKAGES), and from the host through
# `docker exec` for computer_install_packages. apt's download and index
# caches live on the persistent /var/lib/cubicle/apt mount so a restart only
# unpacks what it already fetched.
set -e
[ "$#" -gt 0 ] || exit 0

export DEBIAN_FRONTEND=noninteractive
CACHE=/var/lib/cubicle/apt
mkdir -p "$CACHE/archives/partial" "$CACHE/lists/partial"
APT="apt-get -o Dir::Cache::archives=$CACHE/archives -o Dir::State::lists=$CACHE/lists -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300"
INSTALL="$APT install -y --no-install-recommends"

# Reuse a package index younger than a day; refresh it otherwise, and again
# if an install fails (a package version may have moved on since).
if [ -z "$(find "$CACHE/lists" -maxdepth 1 -name '*_Packages' -mmin -1440 2>/dev/null | head -n 1)" ]; then
    $APT update
fi
if ! $INSTALL "$@"; then
    $APT update
    $INSTALL "$@"
fi
# The downloaded .debs stay in the cache for the next boot; the apt lists
# inside the container layer are not used (Dir::State::lists above).
