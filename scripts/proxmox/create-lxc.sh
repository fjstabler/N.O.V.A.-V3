#!/usr/bin/env bash
#
# Create the LXC container N.O.V.A. will live in, and install it.
#
# Run this on the Proxmox host, as root, from a checkout of the repository.
# It creates an unprivileged Debian container, copies this checkout into it,
# and runs install-nova.sh inside.
#
#   ./scripts/proxmox/create-lxc.sh
#   ./scripts/proxmox/create-lxc.sh --restore ~/nova-export-20260904-2130.tar.gz
#   CORES=4 MEMORY=6144 STORAGE=local-zfs ./scripts/proxmox/create-lxc.sh
#
# Everything is overridable by environment variable; the defaults suit a
# four-core box with a few gigabytes to spare.
#
set -euo pipefail

CTID="${CTID:-}"                    # blank picks the next free id
CT_HOSTNAME="${CT_HOSTNAME:-nova}"
STORAGE="${STORAGE:-local-lvm}"     # where the container's disk goes
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
CORES="${CORES:-4}"
MEMORY="${MEMORY:-6144}"            # MB. Whisper and Kokoro both want room.
SWAP="${SWAP:-2048}"
DISK="${DISK:-16}"                  # GB. Models are ~1 GB, wheels a few more.
BRIDGE="${BRIDGE:-vmbr0}"
IPV4="${IPV4:-dhcp}"                # or e.g. 192.168.1.50/24
GATEWAY="${GATEWAY:-}"              # required when IPV4 is not dhcp
RESTORE_FROM=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restore) RESTORE_FROM="$2"; shift 2 ;;
        --ctid) CTID="$2"; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"
command -v pct >/dev/null || die "no 'pct' — this has to run on the Proxmox host itself"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO_ROOT/services/core/pyproject.toml" ]] ||
    die "run this from inside a N.O.V.A. checkout"

if [[ -n "$RESTORE_FROM" ]]; then
    [[ -f "$RESTORE_FROM" ]] || die "no such export: $RESTORE_FROM"
fi

if [[ -z "$CTID" ]]; then
    CTID="$(pvesh get /cluster/nextid)"
fi
pct status "$CTID" &>/dev/null && die "container $CTID already exists — pass --ctid to pick another"

# ---------------------------------------------------------------------- template

say "Finding a Debian template"
TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null |
    awk '{print $1}' | grep -E 'debian-12-standard.*\.tar\.(zst|gz|xz)$' | sort | tail -1 || true)"

if [[ -z "$TEMPLATE" ]]; then
    pveam update >/dev/null 2>&1 || true
    AVAILABLE="$(pveam available --section system |
        awk '{print $2}' | grep -E '^debian-12-standard' | sort | tail -1 || true)"
    [[ -n "$AVAILABLE" ]] || die "no debian-12-standard template available; try 'pveam available'"
    say "Downloading $AVAILABLE"
    pveam download "$TEMPLATE_STORAGE" "$AVAILABLE"
    TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$AVAILABLE"
fi
echo "  $TEMPLATE"

# --------------------------------------------------------------------- container

NET="name=eth0,bridge=$BRIDGE,ip=$IPV4"
if [[ "$IPV4" != "dhcp" ]]; then
    [[ -n "$GATEWAY" ]] || die "a static IPV4 needs GATEWAY set as well"
    NET="$NET,gw=$GATEWAY"
fi

say "Creating container $CTID ($CT_HOSTNAME): ${CORES} cores, ${MEMORY}MB, ${DISK}GB"
pct create "$CTID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CORES" \
    --memory "$MEMORY" \
    --swap "$SWAP" \
    --rootfs "$STORAGE:$DISK" \
    --net0 "$NET" \
    --unprivileged 1 \
    --features nesting=1 \
    --onboot 1 \
    --start 0 \
    --description "N.O.V.A. core — see https://github.com/fjstabler/N.O.V.A.-V3"

say "Starting it"
pct start "$CTID"

say "Waiting for the network"
for _ in $(seq 1 60); do
    if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 ||
    die "the container has no DNS — check the bridge ($BRIDGE) and your DHCP server"

# ------------------------------------------------------------------------ source

say "Copying this checkout into the container"
BUNDLE="$(mktemp /tmp/nova-src-XXXXXX.tar.gz)"
trap 'rm -f "$BUNDLE"' EXIT
# .git comes along so the container can pull updates later; the build outputs
# would be several hundred megabytes of things it will never run.
tar czf "$BUNDLE" -C "$REPO_ROOT" \
    --exclude=node_modules \
    --exclude=.venv \
    --exclude=apps/panel/build \
    --exclude=apps/panel/app/build \
    --exclude=apps/panel/.gradle \
    --exclude=apps/desktop/dist \
    --exclude=apps/desktop/dist-electron \
    .
pct exec "$CTID" -- mkdir -p /opt/nova
pct push "$CTID" "$BUNDLE" /tmp/nova-src.tar.gz
pct exec "$CTID" -- tar xzf /tmp/nova-src.tar.gz -C /opt/nova
pct exec "$CTID" -- rm -f /tmp/nova-src.tar.gz

INSTALL_ARGS=()
if [[ -n "$RESTORE_FROM" ]]; then
    say "Copying your export in"
    pct push "$CTID" "$RESTORE_FROM" /root/nova-export.tar.gz
    INSTALL_ARGS+=(--restore /root/nova-export.tar.gz)
fi

# ----------------------------------------------------------------------- install

say "Installing N.O.V.A. inside the container"
echo "    (ten minutes or so — most of it is downloading ML wheels)"
pct exec "$CTID" -- bash /opt/nova/scripts/proxmox/install-nova.sh "${INSTALL_ARGS[@]}"

if [[ -n "$RESTORE_FROM" ]]; then
    # It held every API key N.O.V.A. has; it has been read now.
    pct exec "$CTID" -- shred -u /root/nova-export.tar.gz 2>/dev/null ||
        pct exec "$CTID" -- rm -f /root/nova-export.tar.gz
fi

IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
cat <<SUMMARY

Container $CTID is up at $IP.

  Shell into it   pct enter $CTID
  Logs            pct exec $CTID -- journalctl -u nova -f
  Stop / start    pct stop $CTID  /  pct start $CTID

Next: give $IP a DHCP reservation in your router, so the panel is not pointing
at an address that moves.
SUMMARY
