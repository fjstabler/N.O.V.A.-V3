#!/usr/bin/env bash
#
# Install N.O.V.A.'s core as a system service on a fresh Debian or Ubuntu box.
#
# Written for an LXC container on Proxmox, but there is nothing Proxmox-specific
# in it — it works the same on a VM or bare metal. Run it as root, from inside
# the machine that is going to run the assistant.
#
#   ./install-nova.sh                                  # from a checkout here
#   ./install-nova.sh --restore /root/nova-export.tar.gz
#   ./install-nova.sh --host 0.0.0.0 --no-wake
#
# Safe to run again: it is how you update. Re-running keeps the config, the
# memory and the token, and only replaces code and dependencies.
#
set -euo pipefail

SOURCE_DIR="${NOVA_SOURCE:-/opt/nova}"
NOVA_HOME="${NOVA_HOME:-/var/lib/nova}"
SERVICE_USER="nova"
BIND_HOST="0.0.0.0"
RESTORE_FROM=""
INSTALL_WAKE=1
INSTALL_VOICE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SOURCE_DIR="$2"; shift 2 ;;
        --home) NOVA_HOME="$2"; shift 2 ;;
        --host) BIND_HOST="$2"; shift 2 ;;
        --restore) RESTORE_FROM="$2"; shift 2 ;;
        --no-wake) INSTALL_WAKE=0; shift ;;
        --no-voice) INSTALL_VOICE=0; INSTALL_WAKE=0; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
# `sudo` is not installed in a minimal Debian container; `runuser` is part of
# util-linux and always is.
as_nova() { runuser -u "$SERVICE_USER" -- "$@"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this as root (it creates a user and a systemd unit)"
[[ -f "$SOURCE_DIR/services/core/pyproject.toml" ]] ||
    die "no N.O.V.A. checkout at $SOURCE_DIR — pass --source /path/to/repo"
command -v systemctl >/dev/null || die "this needs systemd"

VENV="$SOURCE_DIR/.venv"
PIP="$VENV/bin/pip"
PY="$VENV/bin/python"

# --------------------------------------------------------------- system packages

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# ffmpeg: Whisper's audio decoding. libportaudio2: sounddevice loads it when
# imported, and it is imported lazily — but a headless box that later gets a USB
# microphone should not need a second install to use it.
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-dev python3-pip \
    build-essential git curl ca-certificates \
    ffmpeg libportaudio2 libgomp1 tzdata

PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Python $PYTHON_VERSION"
python3 - <<'PY' || die "N.O.V.A. needs Python 3.11 or newer"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

# ------------------------------------------------------------------- service user

if ! id "$SERVICE_USER" &>/dev/null; then
    say "Creating the $SERVICE_USER user"
    # A real shell, not nologin: this is a homelab box and being able to
    # `su - nova` to poke at the venv is worth more than the hardening.
    useradd --system --create-home --home-dir "$NOVA_HOME" --shell /bin/bash "$SERVICE_USER"
fi
mkdir -p "$NOVA_HOME"
chown -R "$SERVICE_USER:$SERVICE_USER" "$NOVA_HOME"
chmod 700 "$NOVA_HOME"

# ------------------------------------------------------------------------ restore

if [[ -n "$RESTORE_FROM" ]]; then
    [[ -f "$RESTORE_FROM" ]] || die "no such export: $RESTORE_FROM"
    say "Restoring settings and memory from $(basename "$RESTORE_FROM")"
    STAGE="$(mktemp -d)"
    tar xzf "$RESTORE_FROM" -C "$STAGE"
    [[ -d "$STAGE/nova" ]] || die "that does not look like a nova-export tarball"
    # NOVA_HOME is flat, and the export was written flat to match.
    cp -r "$STAGE/nova/." "$NOVA_HOME/"
    rm -rf "$STAGE"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$NOVA_HOME"
fi

# -------------------------------------------------------------------------- code

chown -R "$SERVICE_USER:$SERVICE_USER" "$SOURCE_DIR"

if [[ ! -x "$PY" ]]; then
    say "Creating the virtualenv"
    as_nova python3 -m venv "$VENV"
fi

say "Installing the core"
as_nova "$PIP" install --quiet --upgrade pip wheel
as_nova "$PIP" install --quiet -e "$SOURCE_DIR/services/core[ai,embeddings,home,server]"

if [[ $INSTALL_VOICE -eq 1 ]]; then
    say "Installing the voice stack (this pulls a few hundred MB of wheels)"
    as_nova "$PIP" install --quiet -e "$SOURCE_DIR/services/core[voice]"
fi

if [[ $INSTALL_WAKE -eq 1 ]]; then
    say "Installing the wake word engine"
    # openWakeWord declares tflite-runtime on Linux but never uses it when
    # driven through ONNX, which is how N.O.V.A. drives it. The declaration
    # alone blocks installation on any Python without a tflite wheel, so this
    # installs it without its dependency list and adds what it actually imports.
    as_nova "$PIP" install --quiet --no-deps openwakeword
    as_nova "$PIP" install --quiet onnxruntime numpy scipy scikit-learn requests tqdm
    as_nova "$PY" -c \
        "import openwakeword.utils as u; u.download_models()" || \
        echo "  (wake word models did not download — 'make wake' can retry later)"
fi

# ------------------------------------------------------------------------ models

if [[ $INSTALL_VOICE -eq 1 ]]; then
    say "Downloading the voice models"
    as_nova env NOVA_HOME="$NOVA_HOME" \
        "$PY" "$SOURCE_DIR/scripts/fetch_models.py" || \
        echo "  (some models did not download — scripts/fetch_models.py can retry)"
fi

# ------------------------------------------------------------------------ config

# The bridge binds to loopback by default, which on a headless box means
# nothing can ever reach it — including the panel and the phone this machine
# exists to serve. It has to be set before the first start, or there is no way
# in to change it.
say "Pointing the bridge at $BIND_HOST"
as_nova env NOVA_HOME="$NOVA_HOME" "$PY" - "$NOVA_HOME/config.toml" "$BIND_HOST" <<'PY'
import pathlib
import sys

import tomlkit

path, host = pathlib.Path(sys.argv[1]), sys.argv[2]
document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
transport = document.get("transport")
if transport is None:
    transport = tomlkit.table()
    document["transport"] = transport
previous = transport.get("host")
transport["host"] = host
path.write_text(tomlkit.dumps(document))
print(f"  transport.host: {previous or '127.0.0.1 (default)'} -> {host}")
PY
chmod 600 "$NOVA_HOME/config.toml"
chown "$SERVICE_USER:$SERVICE_USER" "$NOVA_HOME/config.toml"

# ------------------------------------------------------------------ systemd unit

say "Installing the systemd service"
cat > /etc/systemd/system/nova.service <<UNIT
[Unit]
Description=N.O.V.A. core
Documentation=https://github.com/fjstabler/N.O.V.A.-V3
# The assistant is useless without an address, and half its integrations reach
# out on start, so wait for real connectivity rather than just for the stack.
After=network-online.target
Wants=network-online.target
# A machine that has just been powered on may not have DNS for a few seconds.
# Without this, an early restart loop counts as a failure and systemd gives up
# on the one service the box exists to run.
StartLimitIntervalSec=0

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
Environment=NOVA_HOME=$NOVA_HOME
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$SOURCE_DIR
ExecStart=$PY -m nova
Restart=always
RestartSec=5

# Deliberately little sandboxing. The container is already the boundary, and
# the options worth adding here are the ones that would break what N.O.V.A.
# does: ProtectKernelTunables and friends can fail outright inside an
# unprivileged LXC, and NoNewPrivileges would block the sudoers entry that
# system unit control asks for. PrivateTmp is free and costs nothing.
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --quiet nova.service
systemctl restart nova.service

# ------------------------------------------------------------------------ report

say "Waiting for the bridge"
TOKEN=""
for _ in $(seq 1 30); do
    if [[ -f "$NOVA_HOME/bridge.json" ]]; then
        TOKEN="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['token'])" \
            "$NOVA_HOME/bridge.json" 2>/dev/null || true)"
        [[ -n "$TOKEN" ]] && break
    fi
    sleep 1
done

ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['port'])" \
    "$NOVA_HOME/bridge.json" 2>/dev/null || echo 8765)"

echo
if [[ -z "$TOKEN" ]]; then
    printf '\033[1;31mThe service did not come up.\033[0m\n\n'
    echo "  journalctl -u nova -n 50 --no-pager"
    exit 1
fi

printf '\033[1;32mN.O.V.A. is running.\033[0m\n\n'
echo "  Interface   http://$ADDRESS:$PORT/app/?token=$TOKEN"
echo "  Phone       http://$ADDRESS:$PORT/?token=$TOKEN"
echo
echo "  Host        $ADDRESS"
echo "  Port        $PORT"
echo "  Token       $TOKEN"
echo
echo "  Logs        journalctl -u nova -f"
echo "  Restart     systemctl restart nova"
echo "  Data        $NOVA_HOME"
echo
echo "Open that first link once on a device and it keeps the token; the panel"
echo "app wants the host, port and token typed in separately."
echo
echo "Give this machine a fixed address in your router before pairing anything,"
echo "or the panel will be pointing at an IP that moves."
