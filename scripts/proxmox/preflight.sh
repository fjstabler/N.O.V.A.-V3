#!/usr/bin/env bash
#
# Look at this Proxmox host and work out what create-lxc.sh should be told.
#
# Reads nothing but status, changes nothing, and finishes by printing the exact
# command to run. The storage name is the usual reason the create script fails
# on a first attempt — it is `local-lvm` on a stock install and something else
# on anything with ZFS, Ceph or a second disk — so rather than guessing, this
# asks the host.
#
#   ./scripts/proxmox/preflight.sh
#
set -uo pipefail   # deliberately not -e: a failed probe should report, not abort

CORES_WANTED=4
MEMORY_WANTED=6144   # MB
DISK_WANTED=16       # GB

ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[1;31m✗\033[0m %s\n' "$*"; }
head_() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

PROBLEMS=0
note_problem() { PROBLEMS=$((PROBLEMS + 1)); }

# ------------------------------------------------------------------ the host

head_ "Proxmox host"

if ! command -v pct >/dev/null 2>&1; then
    bad "No 'pct' command — this is not a Proxmox host."
    echo
    echo "  Run this on the Proxmox machine itself: either over SSH, or in the"
    echo "  web UI under Datacenter -> your node -> Shell."
    exit 1
fi
ok "$(pveversion 2>/dev/null | head -1)"

if [[ $EUID -ne 0 ]]; then
    bad "Not root. Creating a container needs root — log in as root or use sudo -i."
    note_problem
else
    ok "Running as root"
fi

HOST_CORES="$(nproc 2>/dev/null || echo 0)"
if [[ "$HOST_CORES" -ge "$CORES_WANTED" ]]; then
    ok "$HOST_CORES CPU cores"
else
    warn "$HOST_CORES cores — the container asks for $CORES_WANTED; it will share them"
fi

HOST_FREE_MB="$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}')"
if [[ -n "$HOST_FREE_MB" && "$HOST_FREE_MB" -ge "$MEMORY_WANTED" ]]; then
    ok "${HOST_FREE_MB} MB RAM available (container wants ${MEMORY_WANTED} MB)"
elif [[ -n "$HOST_FREE_MB" ]]; then
    warn "only ${HOST_FREE_MB} MB RAM available; ${MEMORY_WANTED} MB is the ask"
    echo "      Either free some up, or pass MEMORY=4096 (the practical floor"
    echo "      once Whisper and Kokoro are both loaded)."
    note_problem
fi

# --------------------------------------------------------------- disk storage

head_ "Where the container's disk can go"

mapfile -t ROOTDIR_STORES < <(
    pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1}'
)

STORAGE=""
if [[ ${#ROOTDIR_STORES[@]} -eq 0 ]]; then
    bad "No active storage accepts container disks."
    echo "      Check 'pvesm status'. A storage needs the 'Container' content"
    echo "      type enabled (Datacenter -> Storage -> edit -> Content)."
    note_problem
else
    # Prefer the conventional names, else whichever has the most room.
    for candidate in local-lvm local-zfs; do
        for found in "${ROOTDIR_STORES[@]}"; do
            [[ "$found" == "$candidate" && -z "$STORAGE" ]] && STORAGE="$found"
        done
    done
    [[ -z "$STORAGE" ]] && STORAGE="$(
        pvesm status --content rootdir 2>/dev/null |
            awk 'NR>1 && $3=="active" {print $6, $1}' | sort -rn | head -1 | awk '{print $2}'
    )"
    [[ -z "$STORAGE" ]] && STORAGE="${ROOTDIR_STORES[0]}"

    for found in "${ROOTDIR_STORES[@]}"; do
        AVAIL_GB="$(
            pvesm status --storage "$found" 2>/dev/null |
                awk 'NR>1 {printf "%d", $6/1024/1024}'
        )"
        LABEL="$found (${AVAIL_GB:-?} GB free)"
        if [[ "$found" == "$STORAGE" ]]; then
            if [[ -n "$AVAIL_GB" && "$AVAIL_GB" -lt "$DISK_WANTED" ]]; then
                bad "$LABEL — less than the ${DISK_WANTED} GB the container wants"
                note_problem
            else
                ok "$LABEL  <- will use this one"
            fi
        else
            printf '    %s\n' "$LABEL"
        fi
    done
fi

# ------------------------------------------------------------------ templates

head_ "Debian template"

TEMPLATE_STORAGE="$(
    pvesm status --content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}'
)"
if [[ -z "$TEMPLATE_STORAGE" ]]; then
    bad "No storage accepts templates — enable the 'Container template' content type."
    note_problem
    TEMPLATE_STORAGE="local"
else
    ok "Templates go on '$TEMPLATE_STORAGE'"
fi

EXISTING_TEMPLATE="$(
    pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '{print $1}' | grep -c 'debian-12-standard'
)"
if [[ "${EXISTING_TEMPLATE:-0}" -gt 0 ]]; then
    ok "Debian 12 template already downloaded"
else
    warn "No Debian 12 template yet — the create script downloads it (~120 MB)"
fi

# -------------------------------------------------------------------- network

head_ "Network"

mapfile -t BRIDGES < <(ip -br link show type bridge 2>/dev/null | awk '{print $1}')
BRIDGE=""
if [[ ${#BRIDGES[@]} -eq 0 ]]; then
    bad "No bridge interfaces found — expected something like vmbr0."
    note_problem
else
    for found in "${BRIDGES[@]}"; do
        [[ "$found" == "vmbr0" ]] && BRIDGE="$found"
    done
    [[ -z "$BRIDGE" ]] && BRIDGE="${BRIDGES[0]}"
    for found in "${BRIDGES[@]}"; do
        ADDRESS="$(ip -4 -br addr show "$found" 2>/dev/null | awk '{print $3}')"
        if [[ "$found" == "$BRIDGE" ]]; then
            ok "$found ${ADDRESS:+($ADDRESS)}  <- will use this one"
        else
            printf '    %s %s\n' "$found" "${ADDRESS:+($ADDRESS)}"
        fi
    done
fi

if getent hosts deb.debian.org >/dev/null 2>&1; then
    ok "The host can resolve deb.debian.org"
else
    warn "The host cannot resolve deb.debian.org — the container will need DNS too"
fi

# ---------------------------------------------------------------- the checkout

head_ "This checkout"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
if [[ -f "$REPO_ROOT/services/core/pyproject.toml" ]]; then
    ok "N.O.V.A. checkout at $REPO_ROOT"
else
    bad "Not inside a N.O.V.A. checkout."
    note_problem
fi

NEXT_ID="$(pvesh get /cluster/nextid 2>/dev/null || echo '')"
[[ -n "$NEXT_ID" ]] && ok "Next free container id: $NEXT_ID"

EXPORT="$(ls -t /root/nova-export-*.tar.gz 2>/dev/null | head -1)"
if [[ -n "$EXPORT" ]]; then
    ok "Found an export to restore: $EXPORT"
else
    warn "No /root/nova-export-*.tar.gz — you will get a fresh install with a new token."
    echo "      To bring your settings and memory across, run scripts/nova-export.sh"
    echo "      on the machine N.O.V.A. runs on now and scp it to /root/ here."
fi

# --------------------------------------------------------------------- verdict

head_ "What to run"

if [[ $PROBLEMS -gt 0 ]]; then
    printf '\033[1;31m%d thing(s) above need sorting first.\033[0m\n' "$PROBLEMS"
    echo
fi

COMMAND="./scripts/proxmox/create-lxc.sh"
[[ -n "$EXPORT" ]] && COMMAND="$COMMAND --restore $EXPORT"

ENVIRONMENT=""
[[ -n "$STORAGE" && "$STORAGE" != "local-lvm" ]] && ENVIRONMENT="STORAGE=$STORAGE "
[[ -n "$BRIDGE" && "$BRIDGE" != "vmbr0" ]] && ENVIRONMENT="${ENVIRONMENT}BRIDGE=$BRIDGE "
[[ "$TEMPLATE_STORAGE" != "local" ]] && ENVIRONMENT="${ENVIRONMENT}TEMPLATE_STORAGE=$TEMPLATE_STORAGE "

echo "  cd $REPO_ROOT"
echo "  ${ENVIRONMENT}${COMMAND}"
echo
echo "Nothing has been changed by running this."
