#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <repository-name> <go-main-package> <brew-tap>" >&2
  exit 2
fi

repository_name="$1"
go_main_package="$2"
brew_tap="$3"

if [ -z "$go_main_package" ]; then
  go_main_package="./cmd/${repository_name}"
fi
relative_package_pattern='^\./([A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*$'
if [[ "$go_main_package" != "./" && ! "$go_main_package" =~ $relative_package_pattern ]]; then
  echo "go_main_package must be ./ or a relative package path without empty, . or .. components: $go_main_package" >&2
  exit 1
fi

if [ -z "$brew_tap" ]; then
  brew_tap="homebrew-baton"
fi
brew_tap_pattern='^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
if [[ ! "$brew_tap" =~ $brew_tap_pattern ]]; then
  echo "brew_tap must be a GitHub repository name without a path separator: $brew_tap" >&2
  exit 1
fi

printf 'go_main_package=%s\n' "$go_main_package"
printf 'brew_tap=%s\n' "$brew_tap"
