# clab-dashboard

A web UI for [containerlab](https://containerlab.dev): run, draw, break and inspect
network labs from the browser.

- **Labs** - every topology on the host, running or not, with start / redeploy / stop,
  memory pre-flight, in-browser CLI to every node, and an audit log of every action.
- **Topology map** - live IGP adjacencies, BGP sessions and link traffic; fail a link or
  add delay/loss/jitter; capture packets in the browser (Wireshark-style decode, pcap
  download); control-plane **path trace** through SR-MPLS, SRv6 and L3VPN; convergence
  measurements.
- **Builder** - drag-and-drop topologies for IOS-XE, IOS-XR (XRd), NX-OS, FRR, SR Linux and
  Linux, with generated addressing, IS-IS/OSPF, SR-MPLS, BGP (full mesh / RR) and L3VPN configs.
- **Catalogue** - ready-to-run service-provider scenarios (Inter-AS A/B/C, CsC, SRv6,
  Flex-Algo + ODN, 6PE/6VPE, EVPN-VPWS, RR add-path, multicast …) with built-in lab guides
  that check themselves, plus imports from zip, git and the containerlab examples.
- **Live editing** - add/remove nodes and links on a running lab, push and save device
  configs, config snapshots with diff and restore.
- **Manage** - container images (pull, search, upload, build vrnetlab router images from a
  vendor ISO/qcow2 with a boot test), Docker networks, trash, and **LAN access**: give lab
  nodes addresses on your LAN, including an SSH gateway into nodes that have no SSH server.

> clab-dashboard is a community project and is not affiliated with the containerlab project or Nokia.

## Install

One line, on a Linux host (Ubuntu/Debian or Rocky/Alma/RHEL/Fedora; x86_64 or arm64):

```bash
curl -fsSL https://raw.githubusercontent.com/leoniri/clab-dashboard/main/get.sh | sudo bash
```

This installs **Docker** (if missing), the **latest containerlab**, and the dashboard, then
prints the address and the generated `admin` password:

```
==> clab-dashboard 0.9.0 is running - http://10.0.0.5:8080/
    containerlab 0.79.0
    login: admin / Xk7mPq2vRt9sLw4n
```

The password is also kept in `/var/lib/clab-dashboard/initial-admin-password` until you
change it (*Account › Change password* in the left rail).

Prefer to read the code first? Clone and run the installer yourself - it is the same thing:

```bash
git clone https://github.com/leoniri/clab-dashboard.git
cd clab-dashboard
sudo ./install.sh
```

### Options

Pass them to `install.sh`, or through the one-liner with `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/leoniri/clab-dashboard/main/get.sh | sudo bash -s -- --port 9000
```

| Option | |
|---|---|
| `--port N` | web UI port (default 8080) |
| `--listen ADDR` | listen on one address only, e.g. `127.0.0.1` behind an SSH tunnel |
| `--clab-version X.Y.Z` | install this containerlab release instead of the latest |
| `--no-clab` | leave Docker and containerlab exactly as they are |
| `--no-auth` / `--auth` | switch the login off / on (off only makes sense with `--listen 127.0.0.1`) |
| `--uninstall` | remove the services and web config; labs, images and data stay |
| `--version vX.Y.Z` | (get.sh only) install that release instead of the latest; `main` for the development branch |

### Upgrade

```bash
sudo clab-dashboard update
```

Re-runs the installer from the latest release: the dashboard is replaced, containerlab is
upgraded to its latest release (add `--clab-version X.Y.Z` or `--no-clab` to prevent that),
and users, settings and labs are kept.

### Requirements

- systemd, and a kernel that runs Docker. A VM is fine; nested virtualisation (KVM) is
  needed for VM-based router images (vrnetlab: c8000v, n9kv, …), not for container NOSes
  (FRR, SR Linux, XRd, cEOS, Linux).
- RAM is what limits you: FRR/Linux nodes need tens of MB, SR Linux ~1.5 GB, c8000v ~4 GB,
  XRd ~8 GB each. The lab list estimates what a lab needs before you start it.
- Router images are **not** included - pull the free ones from *Manage › Images*, or
  build vendor ones there from your own ISO/qcow2.

## Admin commands

```
clab-dashboard status | logs | restart | version
clab-dashboard passwd [user]            # set a password (creates the user if new)
clab-dashboard reset-password [user]    # new random password, printed
clab-dashboard users | deluser <user>
clab-dashboard auth on|off
clab-dashboard update [options]
clab-dashboard uninstall
```

## What gets installed where

| | |
|---|---|
| `/opt/clab-dashboard` | the application (Python 3 stdlib + PyYAML, paramiko, netmiko, ruamel.yaml) |
| `/var/lib/clab-dashboard` | users, settings, trash, captures, image builds |
| `/opt/clab-topologies` | labs created by the builder and the catalogue |
| `clab-dashboard.service` | backend on 127.0.0.1:8090 |
| `clab-term.service` | web terminal (ttyd) on 127.0.0.1:8091 |
| nginx site `clab-dashboard` | the public port, login check for the terminal |
| `/usr/local/bin/clab-dashboard` | admin commands |

Topologies are discovered under `/opt`, `/srv`, `/root` and every home directory (add more
with `CLABD_SCAN_ROOTS=/a:/b` in the service environment); running labs are found wherever
they live.

## Security

The dashboard runs as root and can do anything containerlab and Docker can on the host.
Read [SECURITY.md](SECURITY.md) before exposing it beyond a lab network. In short: keep the
login on, put it behind HTTPS or a VPN if it leaves your LAN, and change the generated
passwords.

## LAN access

*Expose on LAN* gives lab nodes addresses on the host's LAN: the host answers for
each address and forwards it to the node's management interface (DNAT). The first time
you use it the dashboard asks which address range it may use. It proposes one from the
host's subnet; pick addresses your DHCP server does not hand out.

Nodes without an SSH server of their own (FRR, plain Linux, other container NOSes) are
reached through the built-in SSH gateway. Port 22 of their LAN address lands in the
node's CLI:

```bash
ssh clab@<lan ip>                   # vtysh / Cli / sr_cli ...
ssh -t clab@<lan ip> shell          # a shell in the node
ssh clab@<lan ip> "show ip route"   # one command, for scripts
```

The gateway password is generated at install time and printed by the installer; see and
change it under *Manage › LAN access*. The gateway is part of the dashboard: it adds no
system account and does not change the host's sshd.

## SP scenarios

*Catalogue › SP scenarios* generates complete, pre-configured provider labs. Each scenario
is described once (`app/scenarios.py`) and rendered for IOS-XR (XRd), IOS-XE (c8000v), FRR
and SR Linux (customer edges only - the free SR Linux container has no MPLS). Only platform
presets that were deployed and passed an end-to-end check are offered. Every lab gets a
README with the design and addressing, and a lab guide whose checks run from the topology
view. `python3 app/scenarios.py` renders every scenario and preset and checks the result.

## Development

The app is plain Python 3 (no framework) and static HTML/JS (no build step):

```
app/server.py        HTTP server, job queue, lab discovery, collector thread
app/auth.py          login (sessions, users, CLI)
app/builder.py       topology + config generator
app/scenarios.py     SP scenario generator
app/static/          pages; ui.css + ui.js are the shared design system
install.sh           installer / upgrader
get.sh               one-line bootstrap
```

Run the checks with `python3 -m py_compile app/*.py && python3 app/scenarios.py`. Pull
requests welcome - please describe how you tested (which NOS images, which host OS).

## Licence

[Apache 2.0](LICENSE)
