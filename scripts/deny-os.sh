#!/usr/bin/env bash
# Bash 3.2-compatible OS mutations only; identities come from the pinned JSON.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fixtures="$root/.github/actions/ca-trust-guard/fixtures"
manifest="$fixtures/incident-certs.json"
dry_run=false
if [ "${1:-}" = --dry-run ]; then dry_run=true; shift; fi
[ "$#" = 0 ] || { echo 'usage: deny-os.sh [--dry-run]' >&2; exit 2; }
(cd "$fixtures" && { sha256sum -c --quiet SHA256SUMS 2>/dev/null || shasum -a 256 -c --quiet SHA256SUMS; })
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Native Windows jq.exe writes CRLF even for LF JSON inputs. Normalize generated
# records, not pinned fixtures; consumers also strip CR at path boundaries.
jq -er '.certs[] | [.kind, .name, .sha1, .sha256, .pem] | @tsv' "$manifest" \
  | tr -d '\r' > "$work/identities"
os=$(uname -s)
row() {
  if [ "$dry_run" = false ]; then
    "$CA_GUARD_BIN" record --out "$CA_GUARD_STATE/os-deny.jsonl" --verdict "$1" --detail "$2"
  fi
}
elevated() {
  if [ "$dry_run" = true ]; then printf 'DRY-RUN '; printf '%q ' "$@"; printf '\n'
  elif [ "$(id -u)" = 0 ]; then "$@"
  else sudo -n "$@"; fi
}
fingerprint() { openssl x509 -in "$1" -noout -fingerprint "-$2" | tr -d '\r' | sed 's/.*=//; s/://g'; }
check_fingerprint() {
  local got_sha1 got_sha256
  got_sha1=$(fingerprint "$1" sha1); got_sha256=$(fingerprint "$1" sha256)
  if [ "$got_sha1" != "$2" ] || [ "$got_sha256" != "$3" ]; then
    echo "::error title=CA trust guard::$4 fingerprint mismatch -- refusing unexpected certificate" >&2
    exit 1
  fi
}
case "$os" in
  Linux|Darwin)
    if [ "$dry_run" = false ] && [ "$(id -u)" != 0 ]; then
      if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true; then
        row SKIPPED-NOT-ELEVATED 'OS deny requires root or noninteractive sudo; no mutation attempted'
        exit 0
      fi
    fi ;;
esac
# Materialize each pinned fixture identity before any OS mutation. The two
# compromised intermediates share a CN: fingerprints, never names, select them.
i=0
while IFS="$(printf '\t')" read -r kind name want_sha1 want_sha256 pem; do
  pem=${pem%$'\r'}
  case "$pem" in */*|*\\*|..|'') echo 'invalid certificate fixture path' >&2; exit 1;; esac
  awk -v prefix="$work/input-$i" \
    '/-----BEGIN CERTIFICATE-----/{n++} n {print > sprintf("%s-%03d.pem", prefix, n)}' "$fixtures/$pem"
  found=0
  for cert in "$work/input-$i"-*.pem; do
    [ -s "$cert" ] || continue
    if [ "$(fingerprint "$cert" sha1)" = "$want_sha1" ]; then
      check_fingerprint "$cert" "$want_sha1" "$want_sha256" "$name"
      cp "$cert" "$work/$want_sha1.pem"
      found=1
      break
    fi
  done
  [ "$found" = 1 ] || { echo "fixture fingerprint mismatch: $name" >&2; exit 1; }
  i=$((i+1))
done < "$work/identities"
case "$os" in
  Linux)
    CONF=/etc/ca-certificates.conf
    if [ ! -f "$CONF" ]; then row SKIPPED-NO-CONFIG 'no ca-certificates.conf'; exit 0; fi
    find /usr/share/ca-certificates -type f -name '*.crt' > "$work/candidates"
    : > "$work/entries"
    while IFS= read -r cert; do
      cert=${cert%$'\r'}
      subject=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253 | tr -d '\r')
      got_sha1=$(fingerprint "$cert" sha1)
      while IFS="$(printf '\t')" read -r kind name want_sha1 want_sha256 pem; do
        [ "$kind" = root ] || continue
        candidate=0
        [ "$got_sha1" = "$want_sha1" ] && candidate=1
        case "$subject" in *"CN=$name,"*|*"CN=$name") candidate=1;; esac
        [ "$candidate" = 1 ] || continue
        check_fingerprint "$cert" "$want_sha1" "$want_sha256" "$name"
        printf '%s\n' "${cert#/usr/share/ca-certificates/}" >> "$work/entries"
      done < "$work/identities"
    done < "$work/candidates"
    changed=0
    while IFS= read -r entry; do
      entry=${entry%$'\r'}
      if grep -qxF "$entry" "$CONF"; then
        awk -v entry="$entry" '{if ($0 == entry) print "!" $0; else print}' "$CONF" > "$work/conf"
        elevated cp "$work/conf" "$CONF"
        row BLACKLISTED "$entry"
        changed=1
      elif grep -qxF "!$entry" "$CONF"; then row ALREADY-DENIED "$entry"; fi
    done < "$work/entries"
    if [ "$changed" = 1 ]; then elevated update-ca-certificates; fi
    if [ "$dry_run" = true ]; then exit 0; fi
    BUNDLE=/etc/ssl/certs/ca-certificates.crt
    [ -s "$BUNDLE" ] || { echo 'compiled system bundle missing/empty' >&2; exit 1; }
    # Retain #951's compiled-bundle verification, scoped to incident roots.
    awk -v dir="$work" '/-----BEGIN CERTIFICATE-----/{n++} n {print > sprintf("%s/dca-%03d.pem", dir, n)}' "$BUNDLE"
    for cert in "$work"/dca-*.pem; do
      [ -s "$cert" ] || continue
      subject=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253 | tr -d '\r')
      got_sha1=$(fingerprint "$cert" sha1)
      while IFS="$(printf '\t')" read -r kind name want_sha1 want_sha256 pem; do
        [ "$kind" = root ] || continue
        still_trusted=0
        [ "$got_sha1" = "$want_sha1" ] && still_trusted=1
        case "$subject" in *"CN=$name,"*|*"CN=$name") still_trusted=1;; esac
        if [ "$still_trusted" = 1 ]; then echo "still trusted after blacklist: $subject" >&2; exit 1; fi
      done < "$work/identities"
    done
    row VERIFIED-DISTRUST 'none of the four incident roots remains in compiled Linux bundle'
    row NOT-APPLICABLE 'intermediates cannot be blacklisted by ca-certificates; root removal is the Linux evidence' ;;
  Darwin)
    : > "$work/active"
    i=0
    while IFS="$(printf '\t')" read -r kind name want_sha1 want_sha256 pem; do
      cert="$work/$want_sha1.pem"
      if [ "$kind" = root ]; then
        cert="$work/root-$i.pem"; found=0
        for kc in /System/Library/Keychains/SystemRootCertificates.keychain /Library/Keychains/System.keychain; do
          if security find-certificate -c "$name" -p "$kc" > "$cert" 2>/dev/null; then found=1; break; fi
        done
        if [ "$found" != 1 ]; then row NOT-PRESENT "$name"; i=$((i+1)); continue; fi
      fi
      check_fingerprint "$cert" "$want_sha1" "$want_sha256" "$name"
      printf '%s\t%s\t%s\t%s\n' "$name" "$want_sha1" "$want_sha256" "$cert" >> "$work/active"
      i=$((i+1))
    done < "$work/identities"
    # Root exports and both fixture intermediates all passed before mutation.
    while IFS="$(printf '\t')" read -r name want_sha1 want_sha256 cert; do
      cert=${cert%$'\r'}
      check_fingerprint "$cert" "$want_sha1" "$want_sha256" "$name"
      elevated security add-trusted-cert -d -r deny -k /Library/Keychains/System.keychain "$cert"
      row DENIED "$name ($want_sha1) in System.keychain"
    done < "$work/active" ;;
  MINGW*|MSYS*|CYGWIN*)
    invoke_windows() {
      powershell -NoProfile -NonInteractive -File "$(cygpath -w "$root/scripts/deny-os.ps1")" \
        -Manifest "$(cygpath -w "$manifest")" -Guard "$(cygpath -w "${CA_GUARD_BIN:-/unused}")" \
        -Out "$(cygpath -w "${CA_GUARD_STATE:-$work}/os-deny.jsonl")" "$@"
    }
    if [ "$dry_run" = true ]; then invoke_windows -DryRun; else invoke_windows; fi ;;
  *) row SKIPPED-UNSUPPORTED-OS 'no OS deny implementation for this platform' ;;
esac
