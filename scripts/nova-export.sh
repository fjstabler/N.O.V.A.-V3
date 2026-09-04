#!/usr/bin/env bash
#
# Bundle this machine's N.O.V.A. settings and memory into one file.
#
# Run it on the machine N.O.V.A. currently lives on, carry the tarball to the
# new one, and hand it to install-nova.sh. What comes across is everything that
# would be painful to lose — API keys, integration tokens, conversation memory,
# the calendar, enrolled faces, upcoming expenses — and nothing that can simply
# be downloaded again.
#
#   ./scripts/nova-export.sh                 # writes ./nova-export-<date>.tar.gz
#   ./scripts/nova-export.sh /mnt/usb        # writes it somewhere else
#
set -euo pipefail

DESTINATION="${1:-$PWD}"

# Mirrors nova.config.paths: NOVA_HOME collapses config and data into one
# directory, otherwise they are two separate XDG locations.
if [[ -n "${NOVA_HOME:-}" ]]; then
    CONFIG_DIR="$NOVA_HOME"
    DATA_DIR="$NOVA_HOME"
else
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nova"
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nova"
fi

if [[ ! -d "$CONFIG_DIR" ]]; then
    echo "No N.O.V.A. configuration at $CONFIG_DIR." >&2
    echo "If it lives somewhere else, set NOVA_HOME and run this again." >&2
    exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/nova"

# The export is flat, which is the shape NOVA_HOME wants on the other side.
[[ -f "$CONFIG_DIR/config.toml" ]] && cp "$CONFIG_DIR/config.toml" "$STAGE/nova/"

if [[ -d "$DATA_DIR" ]]; then
    for entry in "$DATA_DIR"/*; do
        [[ -e "$entry" ]] || continue
        case "$(basename "$entry")" in
            # Models are hundreds of megabytes and are re-downloaded on the
            # other side; logs and cache describe a machine being left behind;
            # bridge.json is written fresh by whichever core is running.
            models|cache|logs|bridge.json) continue ;;
        esac
        cp -r "$entry" "$STAGE/nova/"
    done
fi

STAMP="$(date +%Y%m%d-%H%M)"
ARCHIVE="$DESTINATION/nova-export-$STAMP.tar.gz"
tar czf "$ARCHIVE" -C "$STAGE" nova
# It contains every API key N.O.V.A. holds — a world-readable copy in a
# downloads folder is the sort of thing that is only noticed later.
chmod 600 "$ARCHIVE"

echo "Wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo
echo "Contains:"
tar tzf "$ARCHIVE" | sed 's|^nova/|  |' | grep -v '^  $' | head -20
echo
echo "This file holds your API keys and your assistant's memory. Copy it over"
echo "SSH rather than anything that keeps a copy, and delete it when it has"
echo "been restored on the other end."
