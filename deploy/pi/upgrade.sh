#!/usr/bin/env bash
# Moves an installed board to another release, checked as when installing:
#   sudo ./deploy/pi/upgrade.sh 0.2.0 [--config FILE] [--secrets FILE]
# It downloads that release's tarball and SHA256SUMS over https into a
# temporary folder, stops unless the tarball's SHA-256 matches, unpacks it,
# and runs that release's deploy/pi/update.sh with the options after the
# version. With cosign installed, it checks SHA256SUMS's signature first.
# The temporary folder is removed when it ends, whatever happens.
set -euo pipefail

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
      echo "SHA256SUMS's signature doesn't check out. Nothing was installed." >&2
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
  # Run with bash, as /tmp may not allow running files from it.
  bash "$unpacked/deploy/pi/update.sh" "$@"
}

# Run, unless sourced (the tests call the functions above on their own).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
