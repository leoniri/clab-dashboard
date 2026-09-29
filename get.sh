#!/usr/bin/env bash
# clab-dashboard one-line installer:
#
#     curl -fsSL https://raw.githubusercontent.com/leoniri/clab-dashboard/main/get.sh | sudo bash
#
# Options go after `bash -s --`, and are passed to install.sh:
#
#     curl -fsSL .../get.sh | sudo bash -s -- --port 9000
#     curl -fsSL .../get.sh | sudo bash -s -- --version v0.9.0     # a given release
#     curl -fsSL .../get.sh | sudo bash -s -- --version main       # the development branch
#
# Downloads the latest release (checksum-verified when the release carries one),
# unpacks it in a temporary directory and runs its install.sh, which installs
# Docker, the latest containerlab and the dashboard.
set -euo pipefail

REPO="${CLABD_REPO:-leoniri/clab-dashboard}"
REF=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --version) REF="${2:-}"; shift 2 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root: curl -fsSL <url>/get.sh | sudo bash"
command -v curl >/dev/null || die "curl is required"
command -v tar >/dev/null || die "tar is required"

if [ -z "$REF" ]; then
    # newest published release; the development branch while there is none
    REF=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
          | sed -n 's/^ *"tag_name": *"\([^"]*\)".*/\1/p' | head -1 || true)
    [ -n "$REF" ] || REF=main
fi
case "$REF" in *[!A-Za-z0-9._/-]*) die "bad --version $REF" ;; esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

asset="https://github.com/$REPO/releases/download/$REF/clab-dashboard-$REF.tar.gz"
if curl -fsSL -o src.tar.gz "$asset" 2>/dev/null; then
    say "clab-dashboard $REF (release)"
    if curl -fsSL -o src.tar.gz.sha256 "$asset.sha256" 2>/dev/null; then
        want=$(awk '{print $1}' src.tar.gz.sha256)
        got=$(sha256sum src.tar.gz | awk '{print $1}')
        [ "$want" = "$got" ] || die "checksum mismatch for $asset"
        say "checksum ok"
    fi
else
    say "clab-dashboard $REF (source archive)"
    case "$REF" in
        main|develop|*/*) url="https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" ;;
        *) url="https://github.com/$REPO/archive/refs/tags/$REF.tar.gz" ;;
    esac
    curl -fsSL -o src.tar.gz "$url" || die "could not download $url"
fi

mkdir src
tar -xzf src.tar.gz -C src --strip-components=1
[ -x src/install.sh ] || chmod +x src/install.sh
bash src/install.sh ${ARGS[@]+"${ARGS[@]}"}
