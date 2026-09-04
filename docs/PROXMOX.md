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

> **You do not need to create a VM or container first.** `create-lxc.sh` makes
> one — it is the command-line equivalent of clicking *Create CT* in the web
> UI. If you would rather click through it yourself, see
> [Doing it through the web UI instead](#doing-it-through-the-web-ui-instead);
> the two paths meet at the same installer.

## Before you start

- Proxmox VE 7 or 8, with room for 4 cores / 6 GB / 16 GB of disk
- A checkout of this repository **on the Proxmox host** (`apt install git`
  first if it is not there)
- Optionally, an export from the machine N.O.V.A. currently runs on

An LXC rather than a VM on purpose: the core is a Python process with no kernel
demands, and a container boots in seconds and gives back the memory it is not
using. A VM works identically if you prefer one — skip to
[By hand, or on a VM](#by-hand-or-on-a-vm).

## 0. Prepare the Proxmox host

### Get a shell on it

Either SSH in as root, or use the web UI: **Datacenter → your node → Shell**.
The web console is a real root shell and is the simpler option if you have not
set up SSH keys.

Everything below runs on the Proxmox host itself, not on a container and not on
your desktop.

### Put a checkout on it

Proxmox does not ship git:

```sh
apt update && apt install -y git
cd /root
git clone https://github.com/fjstabler/N.O.V.A.-V3.git
cd N.O.V.A.-V3
ls scripts/proxmox/     # create-lxc.sh, install-nova.sh, preflight.sh
```

If that directory is missing, `git clone` checked out a branch that predates
these scripts. `git branch -a` lists what the remote has; `git checkout <branch>`
switches to the one you want.

A private repository will ask for credentials. A personal access token as the
password works, or clone it on your laptop and `scp -r` the directory to
`/root/` instead — the create script only needs the files, not the remote.

### Check the host before building anything

```sh
./scripts/proxmox/preflight.sh
```

It changes nothing. It reports the Proxmox version, free cores and memory,
which storages can actually hold a container disk and how much room they have,
whether a Debian template is already downloaded, which bridge to attach to, and
whether it can see an export to restore. It finishes by printing the exact
create command for **your** host, with any overrides already filled in.

This exists mainly because of the storage name. It is `local-lvm` on a stock
install, but anything with ZFS or a second disk will call it something else,
and that is the usual reason a first attempt fails.

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

On the Proxmox host, from the checkout — run whatever `preflight.sh` printed:

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

## Doing it through the web UI instead

`create-lxc.sh` creates the container for you — there is nothing to set up
first. But if you would rather see it happen, or the script tripped on
something about your host, this is the same thing done by hand. The installer
in step 4 is identical either way.

### 1. Download a Debian template

In the left-hand tree: **your node → local → CT Templates**, then the
**Templates** button. Search for `debian-12-standard`, select it, **Download**.

About 120 MB. If `local` has no *CT Templates* entry, the storage does not have
container templates enabled: **Datacenter → Storage → local → Edit**, and tick
*Container template* under Content.

### 2. Create the container

**Create CT**, top right. The tabs, in order:

| Tab | What to put |
| --- | --- |
| **General** | Hostname `nova`. Set a root **Password** — you need it to log in. Leave **Unprivileged container** and **Nesting** ticked. |
| **Template** | Storage `local`, Template the Debian 12 one you just downloaded. |
| **Disks** | `16` GiB. Storage is `local-lvm` on a stock install — whatever your other containers use. |
| **CPU** | Cores `4`. |
| **Memory** | Memory `6144` MiB, Swap `2048` MiB. |
| **Network** | Bridge `vmbr0`. IPv4 **DHCP**. |
| **DNS** | Leave blank — it uses the host's. |
| **Confirm** | Tick **Start after created**, then **Finish**. |

Nothing here is precious except the disk size. Memory can be changed later
without rebuilding; 4096 MiB is the practical floor once Whisper and Kokoro are
both loaded.

### 3. Open its console

Select the new container in the tree, then **Console**. Log in as `root` with
the password you set.

### 4. Install N.O.V.A. inside it

```sh
apt update && apt install -y git
git clone https://github.com/fjstabler/N.O.V.A.-V3.git /opt/nova
cd /opt/nova
bash scripts/proxmox/install-nova.sh
```

`ls scripts/proxmox/` should list three files. If it says the directory does not
exist, the clone gave you a branch that predates these scripts — `git branch -a`
to see what is there, then `git checkout <branch>` and try again.

If the repository is private, git asks for a username and password — the
password is a GitHub personal access token, not your account password
(**GitHub → Settings → Developer settings → Personal access tokens**).

To bring your existing settings and memory across, get the export into the
container first. From the **Proxmox host** shell, not the container's:

```sh
pct push <ctid> /root/nova-export-20260904-2130.tar.gz /root/nova-export.tar.gz
```

then in the container's console:

```sh
bash scripts/proxmox/install-nova.sh --restore /root/nova-export.tar.gz
```

It finishes by printing the address, token and pairing link, exactly as the
scripted path does. Carry on from [step 3](#3-give-it-a-fixed-address).

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
