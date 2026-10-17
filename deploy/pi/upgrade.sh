#!/usr/bin/env bash
# Moves an installed board to another release, checked as when installing:
#   sudo ./deploy/pi/upgrade.sh 0.2.0 [--config FILE] [--secrets FILE]
# It downloads that release's tarball and SHA256SUMS over https into a
# temporary folder, stops unless the tarball's SHA-256 matches, unpacks it,
# and runs that release's deploy/pi/update.sh with the options after the
# version. With cosign installed, it checks SHA256SUMS's signature first.
# The temporary folder is removed when it ends, whatever happens. Going back
# to an older release, it says how to put back the punctuality history as it
# was before a newer release moved it on, which the older one may not read.
set -euo pipefail

PREFIX=/opt/twobells
STORE=/var/lib/twobells
RELEASES=https://github.com/acornelissen/twobells/releases/download
IDENTITY=https://github.com/acornelissen/twobells/.github/workflows/release.yml
ISSUER=https://token.actions.githubusercontent.com

usage() {
  echo "Usage: sudo $0 VERSION [--config FILE] [--secrets FILE]" >&2
  echo "VERSION is a release's number, like 0.2.0 (no v)." >&2
}

# A release's version, as release.yml tags it (without the v).
valid_version() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Whether version $1 is older than version $2, both valid_version's.
older() {
  local -a a b
  IFS=. read -r -a a <<< "$1"
  IFS=. read -r -a b <<< "$2"
  local i
  for i in 0 1 2; do
    if (( 10#${a[i]} != 10#${b[i]} )); then
      (( 10#${a[i]} < 10#${b[i]} ))
      return
    fi
  done
  return 1
}

# The version of the app installed under the prefix $1, from the name of its
# package's metadata folder (nothing of it is run); nothing if there is none,
# or it isn't a release's.
installed_version() {
  local dir version
  for dir in "$1"/venv/lib/python3*/site-packages/twobells-*.dist-info; do
    version="${dir##*/twobells-}"
    version="${version%.dist-info}"
    if valid_version "$version"; then
      echo "$version"
      return 0
    fi
  done
}

# Going back from version $1 to $2: how to put back the punctuality history
# kept from before a newer release moved it on (dashboard/history.py).
going_back() {
  cat <<EOF
Went back from $1 to $2. A newer release may have moved the punctuality
history on to a version $2 can't read; then the dashboard has no history
and no Performance page. To put back the copy kept from before it was moved
on (what was recorded since then is lost):
  sudo systemctl stop twobells-web twobells-fetch
  sudo -u twobells cp $STORE/history.sqlite3.v1 $STORE/history.sqlite3
  sudo -u twobells rm -f $STORE/history.sqlite3-wal $STORE/history.sqlite3-shm
  sudo systemctl start twobells-fetch twobells-web
EOF
}

# Checks the tarball for version $2 in folder $1 against SHA256SUMS there:
# exactly one line must name it, and the SHA-256 must match that line.
check_tarball() {
  local dir="$1" version="$2"
  local name="twobells-$version.tar.gz"
  local expected actual
  expected="$(awk -v name="$name" '$2 == name { print $1 }' "$dir/SHA256SUMS")"
  # No line or several lines for it fail here too: the pattern is one sum.
  if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
    echo "SHA256SUMS has no single sum for $name" >&2
    return 1
  fi
  actual="$(sha256sum < "$dir/$name")"
  actual="${actual%% *}"
  if [[ "$actual" != "$expected" ]]; then
    echo "$name does not match SHA256SUMS: it was changed or damaged" >&2
    return 1
  fi
  echo "$name: OK"
}

# Downloads $1 to the file $2, over https only, redirects included.
fetch() {
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --retry 3 --max-time 600 --output "$2" "$1"
}

main() {
  if [[ $# -lt 1 ]] || ! valid_version "$1"; then
    usage
    exit 2
  fi
  local version="$1"
  shift
  if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo" >&2
    exit 1
  fi
  if ! command -v curl >/dev/null; then
    echo "curl is needed: sudo apt-get install curl" >&2
    exit 1
  fi

  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  local base="$RELEASES/v$version"
  local name="twobells-$version.tar.gz"
  echo "Downloading release $version"
  fetch "$base/$name" "$work/$name"
  fetch "$base/SHA256SUMS" "$work/SHA256SUMS"

  if command -v cosign >/dev/null; then
    fetch "$base/SHA256SUMS.sigstore.json" "$work/SHA256SUMS.sigstore.json"
    if ! cosign verify-blob "$work/SHA256SUMS" --bundle "$work/SHA256SUMS.sigstore.json" \
        --certificate-identity "$IDENTITY@refs/tags/v$version" --certificate-oidc-issuer "$ISSUER"; then
      echo "SHA256SUMS's signature doesn't check out (it needs cosign 2.5 or later). Nothing was installed." >&2
      exit 1
    fi
  else
    echo "cosign isn't installed, so SHA256SUMS's signature is not checked; the tarball's SHA-256 is."
  fi
  if ! check_tarball "$work" "$version"; then
    echo "Nothing was installed." >&2
    exit 1
  fi

  tar -xzf "$work/$name" -C "$work" --no-same-owner
  local unpacked="$work/twobells-$version"
  if [[ ! -f "$unpacked/deploy/pi/update.sh" ]]; then
    echo "$name has no deploy/pi/update.sh. Nothing was installed." >&2
    exit 1
  fi
  local installed
  installed="$(installed_version "$PREFIX")"
  # Run with bash, as /tmp may not allow running files from it.
  bash "$unpacked/deploy/pi/update.sh" "$@"
  if [[ -n "$installed" ]] && older "$version" "$installed"; then
    going_back "$installed" "$version"
  fi
}

# Run, unless sourced (the tests call the functions above on their own).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
