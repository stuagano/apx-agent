#!/usr/bin/env bash
# Install the bd release named in BD_VERSION. Workflows and docs call this
# script; they do not hardcode the version.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
version="$(tr -d '[:space:]' < "$root/scripts/beads/BD_VERSION")"
if [[ -z "$version" ]]; then
  echo "scripts/beads/BD_VERSION is empty" >&2
  exit 1
fi

if command -v bd >/dev/null 2>&1; then
  current="$(bd version | head -n 1)"
  if [[ "$current" == "bd version ${version} "* ]]; then
    exit 0
  fi
fi

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$arch" in
  x86_64) arch="amd64" ;;
  aarch64|arm64) arch="arm64" ;;
  *)
    echo "unsupported architecture: $arch" >&2
    exit 1
    ;;
esac
case "$os" in
  linux|darwin) ;;
  *)
    echo "unsupported OS: $os" >&2
    exit 1
    ;;
esac

prefix="${BD_INSTALL_DIR:-"$HOME/.local/bin"}"
mkdir -p "$prefix"
asset="beads_${version}_${os}_${arch}.tar.gz"
url="https://github.com/steveyegge/beads/releases/download/v${version}/${asset}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/$asset" "$url"
curl -fsSL -o "$work/checksums.txt" \
  "https://github.com/steveyegge/beads/releases/download/v${version}/checksums.txt"
(
  cd "$work"
  grep -F " ${asset}" checksums.txt | sha256sum -c -
)
tar -xzf "$work/$asset" -C "$work" bd
install -m 755 "$work/bd" "$prefix/bd"
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$prefix" >> "$GITHUB_PATH"
fi
"$prefix/bd" version
