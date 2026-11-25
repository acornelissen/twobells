# shellcheck shell=bash
# The app's release folders under /opt/twobells, for install.sh and
# update.sh, which source this file from the release they run from. Each
# release gets a venv of its own, built while the one in use carries on:
#   /opt/twobells/releases/<version>-<6 random characters>/venv
#   /opt/twobells/venv      a link to the current release's venv: the units
#                           and every command run the app through it
#   /opt/twobells/previous  a link to the one before, kept to go back to
# Older releases are removed. A link is switched by making the new one beside
# it and renaming that over it, so it always names one release or the other.
# It expects PREFIX and REPO, set -euo pipefail and umask 022.

# The lock that keeps a second install.sh or update.sh from running beside
# this one: its start-up prune would remove the release this one is
# building. In /run, which only root can write to, so nobody else can put a
# file or a link there first; and in memory, gone at each start.
LOCK=/run/twobells-deploy.lock

# Takes the lock, or fails saying why: another run holds it, or there's no
# flock (util-linux, which every Raspberry Pi OS has). It is held on file
# descriptor 9, open until this shell exits, however it exits, going back
# to the release before included: nothing has to let it go. The file is
# never replaced, as a run holding the old one would then run beside one
# holding the new. Commands run as the services' user get 9 closed (9<&-).
take_lock() {
  if ! command -v flock >/dev/null; then
    echo "flock isn't installed, so this can't make sure it runs alone. It comes with util-linux:" >&2
    echo "  sudo apt-get install util-linux" >&2
    return 1
  fi
  : >>"$LOCK"
  exec 9<"$LOCK"
  if ! flock --nonblock 9; then
    echo "Another install.sh or update.sh is running. Let it finish, then run this again." >&2
    return 1
  fi
}

# A release's version, as release.yml tags it (upgrade.sh's valid_version).
valid_release_version() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Sets version to the version in this release's pyproject.toml, read by
# Debian's python3 in isolated mode, and fails unless it is a release's, as it
# names a folder.
read_version() {
  version="$(python3 -I -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
    "$REPO/pyproject.toml")"
  if ! valid_release_version "$version"; then
    echo "pyproject.toml's version isn't a release's (like 0.2.0), so it can't name a folder" >&2
    return 1
  fi
}

# The name of the release folder the link $1 (venv or previous) points to;
# nothing if it isn't a link to a venv in releases/ (a folder named . or ..
# isn't one).
linked() {
  local target
  target="$(readlink "$PREFIX/$1")" || return 0
  target="${target#"$PREFIX/"}"
  if [[ "$target" =~ ^releases/([0-9A-Za-z][0-9A-Za-z.-]*)/venv$ ]]; then
    echo "${BASH_REMATCH[1]}"
  fi
}

# Points the link $1 at release $2's venv, in one step.
point() {
  ln -sfnT "releases/$2/venv" "$PREFIX/$1.new" && mv -T "$PREFIX/$1.new" "$PREFIX/$1"
}

# Removes release $1 unless a link points to it: the one a run made, when it
# stops before switching to it.
discard() {
  if [[ -n "$1" && "$1" != "$(linked venv)" && "$1" != "$(linked previous)" ]]; then
    rm -rf -- "${PREFIX:?}/releases/$1"
  fi
}

# After a power cut between moving an install from before releases had
# folders of their own into releases/ and linking venv to the new one
# (switch_to), venv is gone and previous names the one moved: venv points to
# it again.
recover() {
  local previous
  if [[ -e "$PREFIX/venv" || -L "$PREFIX/venv" ]]; then
    return 0
  fi
  previous="$(linked previous)"
  if [[ -n "$previous" && -x "$PREFIX/releases/$previous/venv/bin/twobells" ]]; then
    point venv "$previous"
    echo "Put back $PREFIX/venv, which an update stopped part way left out"
  fi
}

# Makes a new release folder, root's and readable by the services' user, and
# sets release to its name. Its random part keeps it apart from every other,
# the same version run again too.
new_release() {
  install -d -o root -g root -m 755 "$PREFIX" "$PREFIX/releases"
  release="$(mktemp -d "$PREFIX/releases/$version-XXXXXX")"
  release="${release##*/}"
  chmod 755 "$PREFIX/releases/$release"
}

# Builds release $1's venv as install.sh always has, with Debian's python3
# and its system site packages, then checks it all imports, before anything
# points to it. Build from a clean copy, in build (the caller removes it):
# pip builds in the source tree, and as root that leaves root-owned build/ and
# *.egg-info behind (and a stale build/ can leak old files into the next
# install). Each command names the release's own venv, never the venv link.
build_release() {
  local venv="$PREFIX/releases/$1/venv"
  python3 -I -m venv --system-site-packages "$venv"
  build="$(mktemp -d)"
  cp -a "$REPO/." "$build/"
  rm -rf "$build/build" "$build"/src/*.egg-info
  # Pinned versions, not the newest of each that day. Debian's own pip is used
  # as is: upgrading it would fetch and run an unpinned package as root.
  # From PyPI only inky and its helper, at exact versions with their hashes;
  # their dependencies are Debian's. (Not pip check: with system site packages
  # it also judges Debian's own, some of which it reports as broken.)
  "$venv/bin/pip" install --quiet --require-hashes --no-deps -r "$REPO/deploy/pi/requirements-pi.txt"
  # The code is built with the hashed setuptools, not one fetched for the build.
  "$venv/bin/pip" install --quiet --require-hashes --no-deps -r "$REPO/deploy/pi/requirements-build.txt"
  "$venv/bin/pip" install --quiet --no-deps --no-build-isolation "$build"
  # -I, here and making the venv above: as root, never import an inky.py,
  # numpy.py or venv.py from the current folder. The system site packages
  # still load.
  "$venv/bin/python" -I -c "import twobells.cli.main, inky, PIL, numpy, smbus2, spidev, gpiod" \
    || { echo "A dependency is missing; see the error above" >&2; return 1; }
}

# The name the install in $PREFIX/venv, from before releases had folders of
# their own, takes in releases/: its version, from the name of its package's
# metadata folder (nothing of it is run), and -inplace.
inplace_name() {
  local dir version
  for dir in "$PREFIX"/venv/lib/python3*/site-packages/twobells-*.dist-info; do
    version="${dir##*/twobells-}"
    version="${version%.dist-info}"
    if valid_release_version "$version"; then
      echo "$version-inplace"
      return 0
    fi
  done
  echo "unknown-inplace"
}

# Makes release $1 the current one, and the one it takes over from the
# previous. An install from before releases had folders of their own (venv a
# folder, not a link) moves into releases/ first, previous pointing to it
# before it moves, so a power cut at any step leaves what recover puts right
# or this redoes. Its commands find their Python through the venv link, so
# they run it again whenever it is current. Synced either side, so the new
# venv is on the card before anything points to it.
switch_to() {
  local old
  sync
  if [[ -d "$PREFIX/venv" && ! -L "$PREFIX/venv" ]]; then
    old="$(inplace_name)"
    install -d -o root -g root -m 755 "$PREFIX/releases/$old"
    point previous "$old"
    mv -T "$PREFIX/venv" "$PREFIX/releases/$old/venv"
  else
    old="$(linked venv)"
    if [[ -n "$old" ]]; then
      point previous "$old"
    fi
  fi
  point venv "$1"
  sync
}

# Makes the previous release current again after release $1 failed, previous
# pointing where it did before switch_to ($2; nowhere if that is empty), and
# removes $1. Fails without changing anything if there's no previous one.
switch_back() {
  local previous
  previous="$(linked previous)"
  if [[ -z "$previous" || ! -x "$PREFIX/releases/$previous/venv/bin/twobells" ]]; then
    return 1
  fi
  point venv "$previous" || return 1
  if [[ -n "$2" ]]; then
    point previous "$2" || return 1
  else
    rm -f "$PREFIX/previous" || return 1
  fi
  sync
  discard "$1"
}

# Removes every release folder but the current one and the previous one.
# Nothing, unless venv is a link into releases/: without one, which to keep
# isn't known.
prune() {
  local current previous folder
  current="$(linked venv)"
  previous="$(linked previous)"
  if [[ -z "$current" ]]; then
    return 0
  fi
  for folder in "$PREFIX"/releases/*; do
    if [[ -e "$folder" && "${folder##*/}" != "$current" && "${folder##*/}" != "$previous" ]]; then
      rm -rf -- "$folder"
    fi
  done
}
