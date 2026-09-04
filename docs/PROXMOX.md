# Running N.O.V.A. on Proxmox

Moving the core off a desktop and onto a server that is always on. The
assistant stops depending on a machine you switch off, and the screens — a
panel on the wall, a phone, the desktop shell — become clients of one brain
that is always awake.

## What you end up with

An unprivileged Debian LXC running the core as a systemd service, starting on
boot, reachable from the rest of your network. Roughly 2 GB of disk once the
models are down, and idle CPU when nobody is talking to it.

The container has no sound hardware and does not need any: a paired panel or
phone lends the core its microphone and speaker, and the wake word,
transcription and synthesis all still run on the server. See
[apps/panel/README.md](../apps/panel/README.md).

## Before you start

- Proxmox VE 7 or 8, with room for 4 cores / 6 GB / 16 GB of disk
- A checkout of this repository **on the Proxmox host** (`apt install git`
  first if it is not there)
- Optionally, an export from the machine N.O.V.A. currently runs on

An LXC rather than a VM on purpose: the core is a Python process with no kernel
demands, and a container boots in seconds and gives back the memory it is not
using. A VM works identically if you prefer one — skip to
[By hand, or on a VM](#by-hand-or-on-a-vm).

## 1. Take your settings and memory with you

On the machine N.O.V.A. runs on now:

```sh
./scripts/nova-export.sh
```

That writes `nova-export-<date>.tar.gz` — your API keys, conversation memory,
calendar, enrolled faces, upcoming expenses and **the bridge token**. Models
and logs are left behind; they are re-downloaded on the other side.

Because the bridge token comes across, anything already paired keeps working
after the move. Only the address changes.

Copy it to the Proxmox host:

```sh
scp nova-export-*.tar.gz root@proxmox:/root/
```

The file holds every key N.O.V.A. has. Send it over SSH rather than anything
that keeps a copy, and delete it once it is restored.

> Skip this step entirely for a fresh start — the installer will generate a new
> config and token.

## 2. Build the container

On the Proxmox host, from the checkout:

```sh
./scripts/proxmox/create-lxc.sh --restore /root/nova-export-*.tar.gz
```

It picks a free container ID, downloads a Debian 12 template if you do not have
one, creates the container, copies this checkout in, and runs the installer.
Ten minutes or so, most of it downloading ML wheels.

It finishes by printing the address, the token, and a link that pairs a browser
in one click.

Override anything that does not suit your host:

```sh
STORAGE=local-zfs CORES=4 MEMORY=8192 ./scripts/proxmox/create-lxc.sh
IPV4=192.168.1.50/24 GATEWAY=192.168.1.1 ./scripts/proxmox/create-lxc.sh
```

| Variable | Default | |
| --- | --- | --- |
| `CTID` | next free | container id |
| `CT_HOSTNAME` | `nova` | |
| `STORAGE` | `local-lvm` | where the disk goes — `local-zfs` on ZFS hosts |
| `CORES` | `4` | |
| `MEMORY` | `6144` | MB; 4096 is the floor once models are loaded |
| `DISK` | `16` | GB |
| `BRIDGE` | `vmbr0` | |
| `IPV4` | `dhcp` | or `192.168.1.50/24`, with `GATEWAY` set |

## 3. Give it a fixed address

**Do this before pairing anything.** The panel stores the host as a fixed
string, so a DHCP lease that moves leaves it waiting for an address nobody
answers on.

Either set a DHCP reservation in your router for the container's MAC, or create
it with a static `IPV4`.

## 4. Point your screens at it

- **Browser** — open the link the installer printed. It takes the token out of
  the URL and remembers it.
- **Panel** — the host, port and token go in separately; see
  [apps/panel/README.md](../apps/panel/README.md).
- **Desktop shell** — easiest is to open the browser interface, which is
  literally the same application. If you want the Electron window against the
  remote core, write it a descriptor pointing there and it will attach on
  startup instead of spawning a local core:

  ```sh
  mkdir -p ~/.local/share/nova
  cat > ~/.local/share/nova/bridge.json <<'JSON'
  {"host": "192.168.1.50", "port": 8765, "token": "<the token>", "pid": 0, "version": 1, "startedAt": 0}
  JSON
  ```

  The shell probes that address before falling back to starting its own, so
  keep the Ubuntu machine's local core stopped or it will win the port.

## Settings worth checking on a CPU-only box

No GPU means Whisper runs on CPU, which is fine but wants pinning down. In
**Settings → Voice**:

- **Model size** `base`. `small` is where a four-core desktop CPU starts to
  struggle; `tiny` if `base` feels slow, at some cost in accuracy on names.
- **Device** `cpu` and **compute type** `int8`. Both are what `auto` should
  choose with no CUDA present, but pinning them means no surprises after an
  upgrade.
- Leave **stream sentences** on. It speaks each sentence as it is produced, so
  you hear the first words about a second in rather than waiting for the whole
  reply to render. On a CPU-only box this matters more than anything else here.

## Updating

Re-running the installer is the update path. It keeps your config, memory and
token, and replaces only code and dependencies.

```sh
pct enter <ctid>
cd /opt/nova && git pull
./scripts/proxmox/install-nova.sh
```

## Day to day

```sh
pct enter <ctid>                        # a shell inside
pct exec <ctid> -- journalctl -u nova -f   # follow the log
pct exec <ctid> -- systemctl restart nova
```

Everything N.O.V.A. keeps lives in `/var/lib/nova`. Back that up — or run
`scripts/nova-export.sh` inside the container, which skips the models.

## Troubleshooting

**The service did not come up.** `journalctl -u nova -n 50 --no-pager` inside
the container. A missing API key degrades rather than crashes, so a hard
failure is usually a broken config file or a permissions problem on
`/var/lib/nova`.

**Nothing can reach it.** Check `transport.host` is `0.0.0.0` in
`/var/lib/nova/config.toml` — loopback is the default and the installer only
changes it on the machine it runs on. Then check the container actually has the
address you think: `pct exec <ctid> -- hostname -I`.

**Voice models missing.** The download is best-effort so a flaky connection
does not fail the whole install:

```sh
pct enter <ctid>
runuser -u nova -- env NOVA_HOME=/var/lib/nova \
    /opt/nova/.venv/bin/python /opt/nova/scripts/fetch_models.py
```

**Wake word not working.** openWakeWord ships `alexa`, `hey_jarvis`,
`hey_mycroft` and `hey_rhasspy`; "hey nova" needs a trained model. See
[SETUP.md](SETUP.md).

## By hand, or on a VM

`install-nova.sh` has nothing Proxmox-specific in it. On any Debian or Ubuntu
machine with systemd:

```sh
sudo git clone <this repo> /opt/nova
sudo /opt/nova/scripts/proxmox/install-nova.sh --restore ~/nova-export.tar.gz
```

Useful flags: `--host` to bind somewhere other than `0.0.0.0`, `--home` to put
the data somewhere other than `/var/lib/nova`, `--no-voice` for a text-only
core, `--no-wake` to skip the wake word engine.

## Reaching it from outside the house

Not needed on your own network — the panel and your phone talk to the container
directly. The day you want to ask N.O.V.A. something while you are out, install
Tailscale in the container and set `transport.host` to its tailnet address. The
token gate does not change; Tailscale becomes the network boundary in place of
the LAN.
