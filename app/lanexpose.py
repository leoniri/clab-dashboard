#!/usr/bin/env python3
"""
LAN exposure - put lab management addresses on the real LAN.

Method (the one proven on this host for both vrnetlab IOS-XE and XRd):
alias a LAN address onto the host's LAN interface and DNAT it to the node's
address on the clab management bridge; MASQUERADE the DNATed flows so the
routers need no route back to the LAN. A macvlan management network is
deliberately NOT used: vrnetlab clones the guest MAC onto the container eth0
and tc-redirects it to the VM tap, and a macvlan child black-holes unicast
addressed to that cloned MAC.

State lives in SETTINGS_FILE. The rules live in our own chains, so the
dashboard can rebuild them atomically on every reconcile without touching
anything docker or a human put in the standard chains:

    nat    PREROUTING, OUTPUT -> CLABD-DNAT   -d <lan ip> -j DNAT --to <node ip>
    nat    POSTROUTING        -> CLABD-SNAT   -d <node ip> --ctstate DNAT -j MASQUERADE
    filter DOCKER-USER        -> CLABD-FWD    -d/-s <node ip> -j ACCEPT
    filter INPUT              -> CLABD-IN     -d <lan ip> --dport <gateway port> -j ACCEPT
                                              (so a host firewall does not block the gateway)

Aliases we add carry the label "<iface>:cl", which is how we tell ours from
addresses someone added by hand (those are never removed).

FRR, plain Linux and other container-native nodes often run no SSH server.
For those, port 22 of the LAN address goes to the dashboard's built-in SSH
gateway instead (sshgw.py, <lan ip>:22 -> <lan ip>:<gateway port>), which
opens the node's CLI with docker exec. Nothing about the LAN is assumed: the
interface is the one holding the default route, and the address pool is
whatever the user sets (a fresh install starts with none and the UI proposes
a range from the host's subnet).

Reconcile is idempotent and cheap. It runs from the collector, so exposure
survives redeploys (new container, same node name) and host reboots.
"""

import ipaddress
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import sysbin

SETTINGS_FILE = "/var/lib/clab-dashboard/settings.json"
IP = sysbin.find("ip")
IPT = sysbin.find("iptables")
IPT_RESTORE = sysbin.find("iptables-restore")
PING = sysbin.find("ping")
DOCKER = sysbin.find("docker")
NSENTER = sysbin.find("nsenter")

DNAT_CHAIN, SNAT_CHAIN, FWD_CHAIN, IN_CHAIN = "CLABD-DNAT", "CLABD-SNAT", "CLABD-FWD", "CLABD-IN"

# SSH gateway for nodes without an SSH server of their own. Only kinds that
# are plain containers qualify - a vrnetlab VM that is still booting also
# refuses :22, and must not be sent into its container's shell.
# The password is generated per install (see _load) - a fixed default would be
# the same on every host that runs this.
GW_DEFAULTS = {"port": 2222, "user": "clab"}
GW_KINDS = ("linux", "nokia_srlinux", "srl", "arista_ceos", "ceos", "juniper_crpd", "crpd",
            "sonic-vs", "sonic-docker", "cisco_xrd", "xrd", "cumulus_cvx", "cvx", "host")

DEFAULT_POOL = []          # no guess about somebody else's LAN - see pool_hint()


class LanError(Exception):
    pass


def _run(cmd, inp=None, timeout=20):
    try:
        p = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                              # noqa: BLE001
        return 1, "", str(exc)


def default_iface():
    rc, out, _ = _run([IP, "-o", "-4", "route", "show", "default"])
    parts = out.split()
    if rc == 0 and "dev" in parts:
        return parts[parts.index("dev") + 1]
    return "eth0"


def iface_addrs(iface):
    """[(ip, prefix, label)] for the IPv4 addresses on iface."""
    rc, out, _ = _run([IP, "-o", "-4", "addr", "show", "dev", iface])
    res = []
    for line in out.splitlines():
        f = line.split()
        if "inet" not in f:
            continue
        cidr = f[f.index("inet") + 1]
        ip, _, pfx = cidr.partition("/")
        # the label is the last field before "valid_lft", when present
        label = iface
        if "valid_lft" in f:
            cand = f[f.index("valid_lft") - 1].rstrip("\\")
            if cand.startswith(iface):
                label = cand
        res.append((ip, int(pfx or 32), label))
    return res


def expand_pool(ranges):
    out = []
    for r in ranges:
        r = r.strip()
        if not r:
            continue
        if "-" in r:
            a, b = (x.strip() for x in r.split("-", 1))
            a = ipaddress.ip_address(a)
            b = ipaddress.ip_address(b) if "." in b else ipaddress.ip_address(
                str(a).rsplit(".", 1)[0] + "." + b)
            if int(b) < int(a) or int(b) - int(a) > 1024:
                raise LanError("bad pool range %s" % r)
            out += [str(ipaddress.ip_address(i)) for i in range(int(a), int(b) + 1)]
        else:
            out.append(str(ipaddress.ip_address(r)))
    return out


def probe_in_use(ip):
    """True when something on the LAN answers for ip (ICMP or an ARP entry)."""
    rc, _, _ = _run([PING, "-c", "1", "-W", "1", "-n", ip], timeout=4)
    if rc == 0:
        return True
    rc, out, _ = _run([IP, "neigh", "show", ip])
    return "lladdr" in out and "FAILED" not in out and "INCOMPLETE" not in out


class Lan:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = self._load()
        self.last_error = None
        self.last_apply = 0.0
        self.active = []            # [{lab, node, lan_ip, node_ip, gateway}] after last reconcile
        self._sshd_seen = {}        # (container, ip) -> (has sshd, checked at)
        self.gw_map = {}            # lan ip -> (container, "lab/node", kind)
        self._mgmt_fixed = set()    # containers whose eth0 we brought up once
        self._log = lambda text: None
        self.gw = None

    # ---------------------------------------------------------------- settings
    def _load(self):
        try:
            with open(SETTINGS_FILE) as fh:
                s = json.load(fh)
        except (OSError, ValueError):
            s = {}
        lan = s.setdefault("lan", {})
        lan.setdefault("iface", default_iface())
        lan.setdefault("pool", list(DEFAULT_POOL))
        s.setdefault("exposure", {})
        gw = s.setdefault("gateway", {})
        for k, v in GW_DEFAULTS.items():
            gw.setdefault(k, v)
        if not gw.get("password"):
            alpha = "abcdefghjkmnpqrstuvwxyz23456789"
            gw["password"] = "".join(secrets.choice(alpha) for _ in range(12))
            self._save(s)
        return s

    def _save(self, s=None):
        s = s if s is not None else self.settings
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(s, fh, indent=2, sort_keys=True)
        os.replace(tmp, SETTINGS_FILE)

    def lan_net(self):
        iface = self.settings["lan"]["iface"]
        for ip, pfx, label in iface_addrs(iface):
            if label == iface:                          # the primary, not an alias
                return ipaddress.ip_interface("%s/%d" % (ip, pfx))
        raise LanError("no IPv4 address on %s" % iface)

    def public(self):
        with self.lock:
            s = json.loads(json.dumps(self.settings))
        try:
            s["lan"]["host"] = str(self.lan_net())
        except LanError as exc:
            s["lan"]["host"] = None
            s["lan"]["error"] = str(exc)
        s["active"] = list(self.active)
        s["gateway"] = self.gateway_status()
        s["pool_hint"] = self.pool_hint() if not s["lan"].get("pool") else None
        s["last_error"] = self.last_error
        return s

    def set_lan(self, iface, pool):
        rc, out, _ = _run([IP, "-o", "link", "show", "dev", iface])
        if rc != 0:
            raise LanError("no such interface: %s" % iface)
        ips = expand_pool(pool)
        with self.lock:
            old = self.settings["lan"]["iface"]
            self.settings["lan"]["iface"] = iface
            try:
                net = self.lan_net()
            except LanError:
                self.settings["lan"]["iface"] = old
                raise
            for ip in ips:
                if ipaddress.ip_address(ip) not in net.network:
                    self.settings["lan"]["iface"] = old
                    raise LanError("%s is not inside %s" % (ip, net.network))
            self.settings["lan"]["pool"] = [p.strip() for p in pool if p.strip()]
            self._save()

    # ------------------------------------------------------------- assignment
    def _taken(self, except_lab=None):
        used = {}
        for lab, e in self.settings["exposure"].items():
            if lab == except_lab:
                continue
            for node, ip in (e.get("nodes") or {}).items():
                used[ip] = "%s/%s" % (lab, node)
        return used

    def suggest(self, lab, node_names):
        """Proposed LAN address per node: keep existing ones, fill the rest
        from the pool, skipping addresses reserved by other labs, the host's
        own, and anything that answers on the LAN."""
        with self.lock:
            cur = dict((self.settings["exposure"].get(lab) or {}).get("nodes") or {})
            taken = self._taken(except_lab=lab)
            pool = expand_pool(self.settings["lan"]["pool"])
        host_ips = {a[0] for a in iface_addrs(self.settings["lan"]["iface"])}
        own_active = {a["lan_ip"] for a in self.active if a["lab"] == lab}
        want = [n for n in node_names if n not in cur]
        cands = [ip for ip in pool if ip not in taken and ip not in cur.values()
                 and ip not in host_ips]
        result = {n: cur[n] for n in node_names if n in cur}
        notes = {}
        # probe a batch at a time, in parallel; stop once everyone has one
        i = 0
        with ThreadPoolExecutor(max_workers=16) as ex:
            while want and i < len(cands):
                batch = cands[i:i + max(len(want) * 2, 8)]
                i += len(batch)
                busy = dict(zip(batch, ex.map(
                    lambda ip: ip not in own_active and probe_in_use(ip), batch)))
                for ip in batch:
                    if not want:
                        break
                    if busy[ip]:
                        notes[ip] = "in use on the LAN - skipped"
                        continue
                    result[want.pop(0)] = ip
        return {"nodes": result, "unassigned": want, "skipped": notes}

    def set_lab(self, lab, enabled, nodes):
        with self.lock:
            net = self.lan_net()
            taken = self._taken(except_lab=lab)
            clean = {}
            seen = set()
            for node, ip in (nodes or {}).items():
                ip = (ip or "").strip()
                if not ip:
                    continue
                try:
                    a = ipaddress.ip_address(ip)
                except ValueError:
                    raise LanError("%s: %r is not an IPv4 address" % (node, ip))
                if a not in net.network or a in (net.network.network_address,
                                                 net.network.broadcast_address):
                    raise LanError("%s: %s is not a usable address in %s"
                                   % (node, ip, net.network))
                if str(a) == str(net.ip):
                    raise LanError("%s: %s is this host's own address" % (node, ip))
                if ip in taken:
                    raise LanError("%s: %s is already reserved for %s"
                                   % (node, ip, taken[ip]))
                if ip in seen:
                    raise LanError("%s is used twice" % ip)
                seen.add(ip)
                clean[node] = ip
            self.settings["exposure"][lab] = {"enabled": bool(enabled), "nodes": clean}
            self._save()

    def forget_lab(self, lab):
        with self.lock:
            self.settings["exposure"].pop(lab, None)
            self._save()

    # --------------------------------------------------------------- dataplane
    def reconcile(self, labs):
        """Make aliases + NAT match settings for the containers running now.

        labs: the collector's lab list (name, containers[short, ipv4, state]).
        Called from the collector every few seconds and from request threads
        after a change, hence the lock around the whole thing.
        """
        with self.lock:
            self._reconcile(labs)

    def _reconcile(self, labs):
        with self.lock:
            exp = json.loads(json.dumps(self.settings["exposure"]))
            iface = self.settings["lan"]["iface"]
        try:
            prefix = self.lan_net().network.prefixlen
        except LanError as exc:
            self.last_error = str(exc)
            return

        running = {}
        for lab in labs:
            for c in lab.get("containers") or []:
                if c.get("state") == "running" and c.get("ipv4"):
                    running[(lab.get("name"), c.get("short"))] = c

        desired = []
        for lab, e in exp.items():
            if not e.get("enabled"):
                continue
            for node, lan_ip in (e.get("nodes") or {}).items():
                c = running.get((lab, node))
                if c:
                    desired.append(({"lab": lab, "node": node, "lan_ip": lan_ip,
                                     "node_ip": c["ipv4"]}, c))
        self._ssh_modes(desired)
        desired = [d for d, _ in desired]

        errs = []
        # ---- aliases
        label = (iface + ":cl")[:15]
        have = iface_addrs(iface)
        mine = {ip for ip, _, lb in have if lb == label}
        present = {ip for ip, _, _ in have}
        want_ips = {d["lan_ip"] for d in desired}
        for ip in sorted(want_ips - present):
            rc, _, err = _run([IP, "addr", "add", "%s/%d" % (ip, prefix), "dev", iface,
                               "label", label])
            if rc != 0:
                errs.append("alias %s: %s" % (ip, err.strip()))
        for ip in sorted(mine - want_ips):
            _run([IP, "addr", "del", "%s/%d" % (ip, prefix), "dev", iface])

        # ---- chains, rebuilt atomically
        nat = ["*nat", ":%s - [0:0]" % DNAT_CHAIN, ":%s - [0:0]" % SNAT_CHAIN]
        flt = ["*filter", ":%s - [0:0]" % FWD_CHAIN, ":%s - [0:0]" % IN_CHAIN]
        for d in desired:
            if d["gateway"]:
                flt.append("-A %s -d %s/32 -p tcp --dport %d -j ACCEPT"
                           % (IN_CHAIN, d["lan_ip"], self.gw.port))
                nat.append("-A %s -d %s/32 -p tcp --dport 22 -j DNAT --to-destination %s:%d"
                           % (DNAT_CHAIN, d["lan_ip"], d["lan_ip"], self.gw.port))
            nat.append("-A %s -d %s/32 -j DNAT --to-destination %s"
                       % (DNAT_CHAIN, d["lan_ip"], d["node_ip"]))
            nat.append("-A %s -d %s/32 -m conntrack --ctstate DNAT -j MASQUERADE"
                       % (SNAT_CHAIN, d["node_ip"]))
            flt.append("-A %s -d %s/32 -j ACCEPT" % (FWD_CHAIN, d["node_ip"]))
            flt.append("-A %s -s %s/32 -j ACCEPT" % (FWD_CHAIN, d["node_ip"]))
        blob = "\n".join(nat + ["COMMIT"] + flt + ["COMMIT"]) + "\n"
        # The jump rules can vanish if docker or a human rebuilds a chain, so
        # check them now and then, not on every 3 s tick.
        now = time.time()
        jumps_due = now - getattr(self, "_last_jump_check", 0) > 30
        if jumps_due:
            self._last_jump_check = now
        if blob != getattr(self, "_last_blob", None) or (jumps_due and not self._jumps_ok()):
            rc, _, err = _run([IPT_RESTORE, "--noflush"], inp=blob)
            if rc != 0:
                errs.append("iptables-restore: %s" % err.strip()[:300])
            else:
                self._last_blob = blob
            self._ensure_jumps(errs)

        self._write_gw_map(desired, labs, errs)
        self.active = desired
        self.last_error = "; ".join(errs) or None
        self.last_apply = time.time()

    # ------------------------------------------------------------ gateway
    def start_gateway(self, log=lambda text: None):
        import sshgw
        self._log = log
        if self.gw is None:
            self.gw = sshgw.Gateway(self._gw_resolve, self._gw_credentials, log)
        with self.lock:
            want = int(self.settings["gateway"].get("port") or GW_DEFAULTS["port"])
        self.gw.start(want)
        return self.gw

    def _gw_resolve(self, local_ip):
        return self.gw_map.get(local_ip, (None, None, None))

    def _gw_credentials(self):
        g = self.settings.get("gateway") or {}
        return str(g.get("user") or ""), str(g.get("password") or "")

    def gateway_status(self):
        g = dict(self.settings.get("gateway") or {})
        g["running"] = bool(self.gw and self.gw.running)
        g["listening"] = self.gw.port if self.gw else None
        g["error"] = self.gw.error if self.gw else "not started"
        return g

    def set_gateway(self, port=None, user=None, password=None):
        with self.lock:
            g = self.settings["gateway"]
            if port is not None:
                port = int(port)
                if not 1024 <= port <= 65000:
                    raise LanError("gateway port must be 1024-65000")
                g["port"] = port
            if user is not None:
                if not user.strip() or len(user) > 32:
                    raise LanError("gateway user must be 1-32 characters")
                g["user"] = user.strip()
            if password is not None:
                if len(password) < 4:
                    raise LanError("gateway password must be at least 4 characters")
                g["password"] = password
            self._save()
        if port is not None and self.gw:
            self.gw.start(port)

    def pool_hint(self):
        """A range to propose when no pool is set: the top of the host's
        subnet (DHCP servers usually hand out the lower part), 40 addresses
        at most, never the host's own address or the broadcast."""
        try:
            net = self.lan_net()
        except LanError:
            return None
        n = net.network
        if n.num_addresses < 8:
            return None
        last = int(n.broadcast_address) - 5
        size = min(40, max(4, n.num_addresses // 6))
        first = last - size + 1
        if int(net.ip) in range(first, last + 1):
            last = int(net.ip) - 1
            first = last - size + 1
        return "%s-%s" % (ipaddress.ip_address(first), ipaddress.ip_address(last))

    def _probe_ssh(self, c):
        """Whether the node answers on TCP 22 itself - cached for 2 minutes."""
        key = (c.get("name"), c["ipv4"])
        hit = self._sshd_seen.get(key)
        if hit and time.time() - hit[1] < 120:
            return hit[0]
        try:
            with socket.create_connection((c["ipv4"], 22), timeout=1):
                has = True
        except OSError:
            has = False
        self._sshd_seen[key] = (has, time.time())
        return has

    def _ssh_modes(self, pairs):
        """[(desired entry, container)] -> sets d["gateway"] and d["ssh"]:
        node (its own sshd) | gateway | none (nothing answers yet)."""
        now = time.time()
        self._sshd_seen = {k: v for k, v in self._sshd_seen.items() if now - v[1] < 600}
        gw_ok = bool(self.gw and self.gw.running)
        with ThreadPoolExecutor(max_workers=16) as ex:
            has = list(ex.map(lambda p: self._probe_ssh(p[1]), pairs))
            # a node with no sshd may not answer at all - check its address
            reach = list(ex.map(lambda p: True if p[1] is None else self._reach(p[1]),
                                [(d, None if own else c) for (d, c), own in zip(pairs, has)]))
        for (d, c), own, up in zip(pairs, has, reach):
            d["gateway"] = (not own) and gw_ok and c.get("kind") in GW_KINDS
            d["ssh"] = "node" if own else ("gateway" if d["gateway"] else "none")
            d["kind"] = c.get("kind")
            d["reachable"] = up
            if not up and c.get("kind") in GW_KINDS:
                note = self._mgmt_up(c)
                if note:
                    d["note"] = note
                    d["reachable"] = self._reach(c, fresh=True)

    def _reach(self, c, fresh=False):
        """Whether the node's management address answers a ping (1 min cache)."""
        key = ("ping", c.get("name"), c["ipv4"])
        hit = self._sshd_seen.get(key)
        if hit and not fresh and time.time() - hit[1] < 60:
            return hit[0]
        rc, _, _ = _run([PING, "-c", "1", "-W", "1", "-n", c["ipv4"]], timeout=4)
        self._sshd_seen[key] = (rc == 0, time.time())
        return rc == 0

    def _mgmt_up(self, c):
        """Some container NOSes (SONiC-VS images, for one) leave their
        management interface eth0 down after start, so nothing can reach
        them. For a container-native node, bring eth0 up once per container
        instance - it is the interface containerlab gave it for exactly this."""
        cid = c.get("id") or c.get("name")
        if cid in self._mgmt_fixed:
            return None
        self._mgmt_fixed.add(cid)
        rc, pid, _ = _run([DOCKER, "inspect", "-f", "{{.State.Pid}}", c.get("name")])
        pid = pid.strip()
        if rc != 0 or not pid.isdigit() or pid == "0":
            return None
        rc, out, _ = _run([NSENTER, "-t", pid, "-n", IP, "-o", "link", "show", "eth0"])
        flags = out.split("<", 1)[-1].split(">", 1)[0].split(",") if "<" in out else []
        if rc != 0 or "UP" in flags:
            return None
        rc, _, err = _run([NSENTER, "-t", pid, "-n", IP, "link", "set", "eth0", "up"])
        if rc != 0:
            return "management interface eth0 is down inside the node (could not bring it up: %s)" % err.strip()
        self._log("LAN %s: management interface eth0 was down inside the node - brought it up"
                  % c.get("name"))
        return "management interface eth0 was down inside the node - brought it up"

    def _write_gw_map(self, desired, labs, errs):
        names = {(lab.get("name"), c.get("short")): c.get("name")
                 for lab in labs for c in lab.get("containers") or []}
        self.gw_map = {d["lan_ip"]: (names.get((d["lab"], d["node"])), "%s/%s" % (d["lab"], d["node"]),
                                     d.get("kind"))
                       for d in desired if d["gateway"]}

    @staticmethod
    def _jump_specs():
        return [("nat", "PREROUTING", DNAT_CHAIN), ("nat", "OUTPUT", DNAT_CHAIN),
                ("nat", "POSTROUTING", SNAT_CHAIN), ("filter", "DOCKER-USER", FWD_CHAIN),
                ("filter", "INPUT", IN_CHAIN)]

    def _jumps_ok(self):
        for table, parent, chain in self._jump_specs():
            rc, _, _ = _run([IPT, "-t", table, "-C", parent, "-j", chain])
            if rc != 0:
                return False
        return True

    def _ensure_jumps(self, errs):
        for table, parent, chain in self._jump_specs():
            if parent == "DOCKER-USER":
                _run([IPT, "-t", table, "-N", parent])       # docker makes it; be safe
            rc, _, _ = _run([IPT, "-t", table, "-C", parent, "-j", chain])
            if rc != 0:
                rc, _, err = _run([IPT, "-t", table, "-I", parent, "1", "-j", chain])
                if rc != 0:
                    errs.append("jump %s/%s: %s" % (table, parent, err.strip()))

    def ssh_config(self, lab, users):
        """A ~/.ssh/config snippet for the exposed nodes of one lab."""
        with self.lock:
            e = self.settings["exposure"].get(lab) or {}
        out = ["# %s - generated by the containerlab dashboard" % lab]
        gw = {a["node"] for a in self.active if a["lab"] == lab and a.get("gateway")}
        for node, ip in sorted((e.get("nodes") or {}).items()):
            out += ["Host %s-%s" % (lab, node), "    HostName %s" % ip]
            user = self._gw_credentials()[0] if node in gw else users.get(node)
            if user:
                out.append("    User %s" % user)
            if node in gw:
                out.append("    # dashboard SSH gateway into the node's CLI, password %s"
                           % self._gw_credentials()[1])
            out.append("")
        return "\n".join(out)
