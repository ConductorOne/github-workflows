#!/usr/bin/env bash
# Install only a committed artifact; never execute downloaded code here.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <tool> <dest-dir>" >&2
  exit 1
fi
tool="$1"
dest="$2"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock="$script_dir/../tools/tool-locks.txt"
case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) echo '::error title=GF13::Unsupported operating system' >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) echo '::error title=GF13::Unsupported architecture' >&2; exit 1 ;;
esac
row="$(awk -v tool="$tool" -v platform="$os/$arch" '$1 == tool && $3 == platform { print; count++ } END { if (count != 1) exit 1 }' "$lock")" || {
  echo "::error title=GF13::Expected exactly one $tool lock for $os/$arch" >&2
  exit 1
}
read -r _ version _ expected url extra <<< "$row"
if [[ ! "$expected" =~ ^[0-9a-f]{64}$ || -z "$url" || -n "$extra" ]]; then
  echo "::error title=GF13::Invalid artifact lock for $tool" >&2
  exit 1
fi
# A version-only updater must not leave an old URL silently installable.
case "$tool" in
  go) canonical="https://go.dev/dl/${version}.${os}-${arch}.tar.gz" ;;
  syft) canonical="https://github.com/anchore/syft/releases/download/${version}/syft_${version#v}_${os}_${arch}.tar.gz" ;;
  node)
    node_arch="$arch"
    if [ "$arch" = amd64 ]; then
      node_arch=x64
    fi
    package="node-${version}-${os}-${node_arch}"
    canonical="https://nodejs.org/dist/${version}/${package}.tar.gz"
    ;;
  goreleaser)
    case "$os/$arch" in
      linux/amd64) asset=goreleaser_Linux_x86_64.tar.gz ;;
      darwin/arm64) asset=goreleaser_Darwin_all.tar.gz ;;
      *) echo "::error title=GF13::Unsupported $tool platform" >&2; exit 1 ;;
    esac
    canonical="https://github.com/goreleaser/goreleaser/releases/download/${version}/${asset}"
    ;;
  *) echo "::error title=GF13::Unsupported artifact tool: $tool" >&2; exit 1 ;;
esac
if [ "$url" != "$canonical" ]; then
  echo "::error title=GF13::$tool lock version/platform does not match its URL" >&2
  exit 1
fi
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
archive="$workdir/${url##*/}"
curl -sSfL --retry 3 -o "$archive" "$url"
if [ "$os" = darwin ]; then
  if ! printf '%s  %s\n' "$expected" "$archive" | shasum -a 256 -c -; then
    echo "::error title=GF13::$tool sha256 mismatch" >&2
    exit 1
  fi
elif ! printf '%s  %s\n' "$expected" "$archive" | sha256sum -c -; then
  echo "::error title=GF13::$tool sha256 mismatch" >&2
  exit 1
fi
# No extraction, installation, or invocation is allowed before verification.
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
if [ "$tool" = go ]; then
  tar -xzf "$archive" -C "$dest" go
  printf 'GOROOT=%s/go\n' "$dest" >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
  bin="$dest/go/bin"
elif [ "$tool" = node ]; then
  tar -xzf "$archive" -C "$dest"
  bin="$dest/$package/bin"
else
  tar -xzf "$archive" -C "$dest" "$tool"
  chmod +x "$dest/$tool"
  bin="$dest"
fi
# GitHub prepends each GITHUB_PATH entry to PATH for subsequent steps.
printf '%s\n' "$bin" >> "${GITHUB_PATH:?GITHUB_PATH must be set}"
echo "Installed $tool $version ($os/$arch), sha256 verified"
