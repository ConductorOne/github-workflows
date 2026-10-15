#!/usr/bin/env bash
set -euo pipefail

# Predicate only: callers retain their existing diagnostics and output streams.
if [ "$#" -ne 1 ]; then
  echo "usage: $0 <path>" >&2
  exit 2
fi

path="$1"
if [[ "$path" == /* ]] || [[ "$path" == *".."* ]] || [[ ! "$path" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  exit 1
fi
