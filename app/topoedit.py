#!/usr/bin/env python3
"""
Structural edits to a lab from the topology view: add/remove nodes and links.

A change set is planned first (what will happen, and how disruptive it is),
then applied as a dashboard job. The topology file is always updated -
round-trip YAML, comments kept, previous version in .clabd-history - so a
later deploy reproduces what you see. On a *running* lab each change is also
applied to the live lab in the least disruptive way that works. What works was
measured on c8000v 17.12 and XRd 26.2.1 under vrnetlab (2026-09-19):

  remove link        delete the veth pair                          live
  remove node        remove its container                          live
  add link, linux    clab tools veth create                        live
  add link, router   a vrnetlab router is a VM; its NICs are made
                     at boot, one tapN per ethN that existed then.
                     port N has a tap  -> veth + re-point the
                                          ethN<->tapN tc redirects live
                     port N has none  -> restart that router only  ~2-4 min
  add node           containerlab cannot add a node to a running
                     lab (deploy --node-filter refuses; and with
                     --reconfigure it deletes the running lab's
                     directory first!) -> save every router's
                     running config, then redeploy the lab        whole lab

Restarting one router: save its running config to its startup file, copy
that into the clab node dir, stop the container, delete the disks the last
launcher run created (otherwise the VM boots its old disk and the launcher
never finishes first-boot), start it and freeze it with `docker pause`, create
every link it has (the launcher boots the VM as soon as it sees as many data
interfaces as at deploy - anything later gets no NIC), fill port gaps and the
deploy-time interface count with dummy interfaces, re-point the tc redirects on
running router peers, unpause. The first boot then applies the startup file.
"""

import io
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time

import editor
import sysbin

DOCKER = sysbin.find("docker")
CLAB = sysbin.find("clab", "containerlab")
NSENTER = sysbin.find("nsenter")
IP = sysbin.find("ip")
TC = sysbin.find("tc")

# vrnetlab router kinds: VM inside the container, NICs fixed at boot
ROUTERS = {
    "cisco_c8000v":      {"max_eth": 9,  "contiguous": False, "if": "xe", "disks": (".qcow2",)},
    "cisco_csr1000v":    {"max_eth": 9,  "contiguous": False, "if": "xe", "disks": (".qcow2",)},
    # XRd creates NICs only for eth1..ethN while they exist consecutively
    "cisco_xrd_vrouter": {"max_eth": 32, "contiguous": True,  "if": "xr", "disks": (".qcow2", ".iso")},
    "cisco_n9kv":        {"max_eth": 64, "contiguous": False, "if": "nxos", "disks": (".qcow2",)},
}
# native containers: interfaces can come and go at any time (SR Linux picks up
# e1-N netdevs whenever they appear - containerlab itself adds them after start)
NATIVE = {"linux", "nokia_srlinux"}
LINUX_MAX_ETH = 64
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
PORT_RE = re.compile(r"^(?:eth|e1-)([1-9][0-9]{0,2})$")
# data port N as containerlab names it, per kind (default ethN)
PORT_FMT = {"nokia_srlinux": "e1-%d"}


def port_fmt(kind):
    return PORT_FMT.get(kind, "eth%d")


def port_num(port):
    m = PORT_RE.match(str(port or ""))
    return int(m.group(1)) if m else 0
SPECIAL = ("host", "mgmt-net", "macvlan", "vxlan", "vxlan-stitch", "dummy", "bridge")


class TopoError(Exception):
    pass


def _run(cmd, timeout=120, inp=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=inp)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                              # noqa: BLE001
        return 1, "", str(exc)


def iface_name(kind, n):
    style = (ROUTERS.get(kind) or {}).get("if")
    if style == "xe":
        return "GigabitEthernet%d" % (n + 1)
    if style == "xr":
        return "GigabitEthernet0/0/0/%d" % (n - 1)
    if style == "nxos":
        return "Ethernet1/%d" % n
    if kind == "nokia_srlinux":
        return "ethernet-1/%d" % n
    return "eth%d" % n


# --------------------------------------------------------------------------
# reading the lab
# --------------------------------------------------------------------------

def _load(lab):
    import devcfg
    with open(lab["path"]) as fh:
        text = fh.read()
    y = devcfg._pick_yaml(text)
    return text, y, y.load(text)


def _node_kind(doc, name):
    topo = doc.get("topology") or {}
    n = (topo.get("nodes") or {}).get(name) or {}
    k = n.get("kind") or (topo.get("defaults") or {}).get("kind") or "linux"
    return {"srl": "nokia_srlinux"}.get(k, k)


def _links(doc):
    """[(a, a_port, b, b_port, index)] for plain node:port links."""
    out = []
    for i, link in enumerate((doc.get("topology") or {}).get("links") or []):
        eps = link.get("endpoints") if hasattr(link, "get") else None
        if not eps or len(eps) != 2:
            continue
        a, _, ai = str(eps[0]).partition(":")
        b, _, bi = str(eps[1]).partition(":")
        out.append((a, ai, b, bi, i))
    return out


def _container(lab, node):
    for c in lab.get("containers") or []:
        if c.get("short") == node:
            return c
    return None


def _pid(cname):
    rc, out, _ = _run([DOCKER, "inspect", "-f", "{{.State.Pid}}", cname], timeout=20)
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else 0


def _netns_ifaces(cname):
    pid = _pid(cname)
    if not pid:
        return set()
    rc, out, _ = _run([NSENTER, "-t", str(pid), "-n", IP, "-o", "link"], timeout=20)
    names = set()
    for line in out.splitlines():
        f = line.split(":")
        if len(f) > 1:
            names.add(f[1].strip().split("@")[0])
    return names


def ports(lab):
    """Per node: kind, used ports, ports with a live NIC, limits - for the UI."""
    _, _, doc = _load(lab)
    nodes = (doc.get("topology") or {}).get("nodes") or {}
    used = {n: {} for n in nodes}
    for a, ai, b, bi, _ in _links(doc):
        if a in used:
            used[a][ai] = "%s:%s" % (b, bi)
        if b in used:
            used[b][bi] = "%s:%s" % (a, ai)
    res = {}
    for n in nodes:
        kind = _node_kind(doc, n)
        r = ROUTERS.get(kind)
        c = _container(lab, n)
        live = []
        if r and c and c.get("state") == "running":
            names = _netns_ifaces(c["name"])
            live = sorted(int(x[3:]) for x in names if re.match(r"^tap[1-9][0-9]*$", x))
        res[n] = {"kind": kind, "router": bool(r), "native": kind in NATIVE,
                  "running": bool(c and c.get("state") == "running"),
                  "used": used.get(n, {}), "nic_ports": live,
                  "max_eth": r["max_eth"] if r else LINUX_MAX_ETH,
                  "port_fmt": port_fmt(kind),
                  "names": {(port_fmt(kind) % i): iface_name(kind, i)
                            for i in range(1, (r["max_eth"] if r else 8) + 1)}}
    return res


def pick_port(info, taken=()):
    """Best free port: one the running VM already has a NIC for, else the lowest free."""
    used = set(info["used"]) | set(taken)
    fmt = info.get("port_fmt") or "eth%d"
    for n in info["nic_ports"]:
        if fmt % n not in used:
            return fmt % n
    for n in range(1, info["max_eth"] + 1):
        if fmt % n not in used:
            return fmt % n
    return None


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def _norm_changes(ch):
    return {
        "add_nodes": list(ch.get("add_nodes") or []),
        "remove_nodes": [str(x) for x in ch.get("remove_nodes") or []],
        "add_links": list(ch.get("add_links") or []),
        "remove_links": list(ch.get("remove_links") or []),
    }


def plan(lab, changes, kinds_supported):
    """Validate a change set and say how it will be applied."""
    ch = _norm_changes(changes)
    _, _, doc = _load(lab)
    topo = doc.get("topology") or {}
    nodes = dict(topo.get("nodes") or {})
    errors, steps = [], []
    running = bool(lab.get("running"))
    info = ports(lab) if nodes else {}

    # ---- node removals / additions
    for n in ch["remove_nodes"]:
        if n not in nodes:
            errors.append("there is no node %s" % n)
    names_after = set(nodes) - set(ch["remove_nodes"])
    for nn in ch["add_nodes"]:
        name = str(nn.get("name") or "")
        if not NAME_RE.match(name):
            errors.append("node name %r must match [a-z][a-z0-9-]{0,30}" % name)
        elif name in names_after:
            errors.append("a node called %s already exists" % name)
        if nn.get("kind") not in kinds_supported:
            errors.append("%s: kind %r is not supported here" % (name, nn.get("kind")))
        if not nn.get("image"):
            errors.append("%s: no image" % name)
        names_after.add(name)

    # ---- links
    existing = {}
    for a, ai, b, bi, i in _links(doc):
        existing[frozenset(("%s:%s" % (a, ai), "%s:%s" % (b, bi)))] = i
    removed_ports = set()
    for l in ch["remove_links"]:
        key = frozenset(("%s:%s" % (l.get("a"), l.get("a_if")), "%s:%s" % (l.get("b"), l.get("b_if"))))
        if key not in existing:
            errors.append("no link %s:%s - %s:%s" % (l.get("a"), l.get("a_if"), l.get("b"), l.get("b_if")))
        removed_ports |= set(key)
    for n in ch["remove_nodes"]:
        for a, ai, b, bi, _ in _links(doc):
            if n in (a, b):
                removed_ports |= {"%s:%s" % (a, ai), "%s:%s" % (b, bi)}
    busy = set()
    for a, ai, b, bi, _ in _links(doc):
        for p in ("%s:%s" % (a, ai), "%s:%s" % (b, bi)):
            if p not in removed_ports:
                busy.add(p)
    new_kind = {nn.get("name"): nn.get("kind") for nn in ch["add_nodes"]}
    for l in ch["add_links"]:
        for side in ("a", "b"):
            n, p = l.get(side), l.get(side + "_if")
            if n not in names_after:
                errors.append("link end %s is not a node" % n)
                continue
            kind = new_kind.get(n) or _node_kind(doc, n)
            kind = "linux" if kind == "frr" else kind
            lim = (ROUTERS.get(kind) or {}).get("max_eth", LINUX_MAX_ETH)
            fmt = port_fmt(kind)
            if not re.match("^" + fmt.replace("%d", r"([1-9][0-9]{0,2})") + "$", str(p or "")) \
                    or port_num(p) > lim:
                errors.append("%s: port %r must be %s..%s" % (n, p, fmt % 1, fmt % lim))
            elif "%s:%s" % (n, p) in busy:
                errors.append("%s:%s is already connected" % (n, p))
            busy.add("%s:%s" % (n, p))
        if l.get("a") == l.get("b"):
            errors.append("a link cannot connect %s to itself" % l.get("a"))
        for side in ("a", "b"):
            ip = l.get(side + "_ip")
            if ip:
                try:
                    ipaddress.ip_interface(ip)
                except ValueError:
                    errors.append("%s: %r is not an address/prefix" % (l.get(side), ip))

    redeploy, restart, live_links = False, set(), []
    if running and not errors:
        if ch["add_nodes"]:
            redeploy = True
            steps.append(("redeploy", "Adding a node needs the whole lab redeployed "
                          "(containerlab cannot add a node to a running lab). Every router's "
                          "running config is saved to its startup file first, so nothing "
                          "configured on the boxes is lost."))
        for n in ch["remove_nodes"]:
            steps.append(("live", "remove node %s (its container is removed; its links go with it)" % n))
        for l in ch["remove_links"]:
            steps.append(("live", "remove link %s:%s - %s:%s" % (l["a"], l["a_if"], l["b"], l["b_if"])))
        if not redeploy:
            for l in ch["add_links"]:
                how = []
                for side in ("a", "b"):
                    n, p = l[side], l[side + "_if"]
                    kind = _node_kind(doc, n)
                    num = port_num(p)
                    if kind in NATIVE:
                        continue
                    if kind in ROUTERS:
                        if num in info.get(n, {}).get("nic_ports", []):
                            continue
                        restart.add(n)
                        how.append("%s has no NIC for %s yet - it is restarted" % (n, p))
                    else:
                        redeploy = True
                        how.append("%s (%s) cannot take a new link live" % (n, kind))
                label = "add link %s:%s - %s:%s" % (l["a"], l["a_if"], l["b"], l["b_if"])
                if how:
                    steps.append(("restart" if not redeploy else "redeploy", label + " - " + "; ".join(how)))
                else:
                    steps.append(("live", label))
                    live_links.append(l)
            if redeploy:
                restart = set()
        for n in sorted(restart):
            steps.append(("restart", "restart %s: its running config is saved to its startup "
                          "file, then it boots again with all its links (~2-4 min; its "
                          "neighbours stay up)" % n))
    elif not errors:
        steps.append(("file", "the lab is not running - only the topology file changes"))
    for l in ch["add_links"]:
        if l.get("a_ip") or l.get("b_ip"):
            steps.append(("config", "configure %s on %s:%s and %s on %s:%s%s"
                          % (l.get("a_ip") or "-", l["a"], l["a_if"], l.get("b_ip") or "-",
                             l["b"], l["b_if"], " + IGP" if l.get("igp") else "")))
    return {"errors": errors, "running": running, "redeploy": redeploy,
            "restart": sorted(restart), "steps": steps, "changes": ch}


# --------------------------------------------------------------------------
# the topology file
# --------------------------------------------------------------------------

def _next_mgmt_ip(doc, taken_extra=()):
    mgmt = doc.get("mgmt") or {}
    subnet = mgmt.get("ipv4-subnet")
    nodes = (doc.get("topology") or {}).get("nodes") or {}
    used = {str(v.get("mgmt-ipv4")) for v in nodes.values() if hasattr(v, "get") and v.get("mgmt-ipv4")}
    if not subnet or not used:
        return None                     # the lab lets docker assign addresses
    used |= set(taken_extra)
    net = ipaddress.ip_network(str(subnet), strict=False)
    hosts = net.hosts()
    next(hosts, None)                   # .1 is the bridge
    for h in hosts:
        if str(h) not in used:
            return str(h)
    raise TopoError("management subnet %s is full" % subnet)


def _base_config(kind, name):
    if ROUTERS.get(kind, {}).get("if") == "xe":
        return "hostname %s\n!\nend\n" % name
    if ROUTERS.get(kind, {}).get("if") == "xr":
        return "hostname %s\nend\n" % name
    return None


def write_topology(lab, ch, builder_kinds, log):
    """Apply the change set to the topology file. Returns new-node config files written."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ
    text, y, doc = _load(lab)
    # write endpoints the way this file already writes them
    quoted = '"%s:' % (next(iter(_links(doc)), ("",))[0]) in text if _links(doc) else True
    topo = doc["topology"]
    nodes = topo["nodes"]
    links = topo.get("links")
    if links is None:
        links = topo["links"] = CommentedSeq()
    base = os.path.dirname(lab["path"])
    created = []

    def drop_link(pred):
        for i in range(len(links) - 1, -1, -1):
            eps = links[i].get("endpoints") if hasattr(links[i], "get") else None
            if eps and len(eps) == 2 and pred(set(str(e) for e in eps)):
                del links[i]

    for l in ch["remove_links"]:
        key = {"%s:%s" % (l["a"], l["a_if"]), "%s:%s" % (l["b"], l["b_if"])}
        drop_link(lambda s, key=key: s == key)
    for n in ch["remove_nodes"]:
        drop_link(lambda s, n=n: any(e.split(":")[0] == n for e in s))
        del nodes[n]

    kinds = topo.get("kinds")
    new_ips = []
    for nn in ch["add_nodes"]:
        name, kind, image = nn["name"], nn["kind"], nn["image"]
        node = CommentedMap()
        bkind = kind
        if kind == "frr":                 # a builder kind: FRR runs as kind linux
            kind = "linux"
        node["kind"] = kind
        kimage = (kinds or {}).get(kind, {}).get("image") if kinds else None
        if kimage != image:
            node["image"] = image
        env = (builder_kinds.get(kind) or {}).get("env") or {}
        if env and not ((kinds or {}).get(kind) or {}).get("env"):
            node["env"] = CommentedMap((k, str(v)) for k, v in env.items())
        ip = _next_mgmt_ip(doc, new_ips)
        if ip:
            node["mgmt-ipv4"] = ip
            new_ips.append(ip)
        if bkind == "frr":
            import builder
            files = {"frr.conf": "frr version 10\nfrr defaults traditional\nhostname %s\n"
                                 "service integrated-vtysh-config\n!\nline vty\n!\n" % name,
                     # every routing daemon on, so whatever gets configured later runs
                     "daemons": builder.frr_daemons({"run_igp": True, "bgp": True}, "isis")
                                .replace("ospfd=no", "ospfd=yes")}
            binds = CommentedSeq()
            for suffix, target in (("daemons", "/etc/frr/daemons"), ("frr.conf", "/etc/frr/frr.conf")):
                rel = os.path.join("configs", "%s.%s" % (name, suffix))
                full = os.path.join(base, rel)
                if not os.path.exists(full):
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "w") as fh:
                        fh.write(files[suffix])
                    st = os.stat(base)
                    os.chown(full, st.st_uid, st.st_gid)
                    created.append(rel)
                binds.append("%s:%s" % (rel, target))
            node["binds"] = binds
            node["sysctls"] = CommentedMap([("net.ipv4.ip_forward", 1),
                                            ("net.mpls.platform_labels", 1048575)])
            node["exec"] = CommentedSeq(["touch /etc/frr/vtysh.conf"])
        cfg = _base_config(kind, name)
        if cfg is not None:
            rel = os.path.join("configs", "%s.cfg" % name)
            full = os.path.join(base, rel)
            if not os.path.exists(full):
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as fh:
                    fh.write(cfg)
                st = os.stat(base)
                os.chown(full, st.st_uid, st.st_gid)
                created.append(rel)
            node["startup-config"] = rel
        labels = CommentedMap()
        if bkind != kind:
            labels["builder-kind"] = bkind
        labels["builder-pos"] = "%d,%d" % (int(nn.get("x") or 0), int(nn.get("y") or 0))
        if nn.get("icon"):
            labels["graph-icon"] = nn["icon"]
        node["labels"] = labels
        nodes[name] = node
        log("topology: + node %s (%s%s)" % (name, kind, ", mgmt " + ip if ip else ""))

    for l in ch["add_links"]:
        pair = ["%s:%s" % (l["a"], l["a_if"]), "%s:%s" % (l["b"], l["b_if"])]
        eps = CommentedSeq([DQ(p) for p in pair] if quoted else pair)
        eps.fa.set_flow_style()
        item = CommentedMap()
        item["endpoints"] = eps
        links.append(item)
        log("topology: + link %s:%s - %s:%s" % (l["a"], l["a_if"], l["b"], l["b_if"]))
    for l in ch["remove_links"]:
        log("topology: - link %s:%s - %s:%s" % (l["a"], l["a_if"], l["b"], l["b_if"]))
    for n in ch["remove_nodes"]:
        log("topology: - node %s and its links" % n)

    buf = io.StringIO()
    y.dump(doc, buf)
    res = editor.write_file(lab, os.path.basename(lab["path"]), buf.getvalue(), editor.sha(text))
    log("topology file saved (previous version kept in history: %s)" % res.get("saved_version"))
    for w in res.get("warnings") or []:
        log("  note: " + w)

    # a builder drawing no longer matches the file; keep it in history and let
    # the builder rebuild its drawing from the topology labels next time
    spec = os.path.join(base, editor.SPEC_FILE)
    if os.path.isfile(spec):
        d = os.path.join(base, editor.HISTORY, editor.SPEC_FILE)
        os.makedirs(d, exist_ok=True)
        shutil.move(spec, os.path.join(d, editor._unique_stamp([d])))
        log("builder drawing retired to history - the builder rebuilds it from the file")
    return created


# --------------------------------------------------------------------------
# live operations
# --------------------------------------------------------------------------

def _ns(cname, *cmd):
    pid = _pid(cname)
    if not pid:
        return 1, "", "%s is not running" % cname
    return _run([NSENTER, "-t", str(pid), "-n"] + list(cmd), timeout=30)


def rewire(cname, port, log):
    """Point ethN and tapN of a vrnetlab container at each other again."""
    tap = "tap" + port[3:]
    for cmd in ([IP, "link", "set", port, "up"],
                [TC, "qdisc", "add", "dev", port, "clsact"],
                [TC, "filter", "del", "dev", port, "ingress"],
                [TC, "filter", "add", "dev", port, "ingress", "flower", "action", "mirred",
                 "egress", "redirect", "dev", tap],
                [TC, "filter", "del", "dev", tap, "ingress"],
                [TC, "filter", "add", "dev", tap, "ingress", "flower", "action", "mirred",
                 "egress", "redirect", "dev", port]):
        rc, _, err = _ns(cname, *cmd)
        if rc != 0 and cmd[1] == "filter" and cmd[2] == "add":
            raise TopoError("rewire %s %s: %s" % (cname, port, err.strip()))
    log("  %s: %s <-> %s redirects re-pointed" % (cname, port, tap))


def veth(ca, pa, cb, pb, log):
    rc, out, err = _run([CLAB, "tools", "veth", "create", "-a", "%s:%s" % (ca, pa),
                         "-b", "%s:%s" % (cb, pb)], timeout=60)
    if rc != 0:
        raise TopoError("veth %s:%s - %s:%s failed: %s" % (ca, pa, cb, pb, (err or out).strip()[-300:]))
    log("  veth %s:%s <-> %s:%s created" % (ca, pa, cb, pb))


def _health(cname):
    rc, out, _ = _run([DOCKER, "inspect", "-f",
                       "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                       cname], timeout=20)
    return out.strip()


def wait_healthy(cnames, log, timeout=900):
    t0 = time.time()
    pending = set(cnames)
    last = 0
    while pending and time.time() - t0 < timeout:
        for c in sorted(pending):
            st = _health(c)
            if st in ("running healthy", "running"):
                pending.discard(c)
                log("  %s is up (%ds)" % (c, time.time() - t0))
        if pending and time.time() - last > 30:
            log("  waiting for %s to boot ... %ds" % (", ".join(sorted(pending)), time.time() - t0))
            last = time.time()
        time.sleep(5)
    if pending:
        raise TopoError("%s did not become healthy within %d s" % (", ".join(sorted(pending)), timeout))


def _cname(lab, node):
    c = _container(lab, node)
    if c:
        return c["name"]
    return "clab-%s-%s" % (lab.get("name"), node)


def _reset_disks(cname, kind, log):
    """Remove the VM disks the previous launcher run created, so the next start is
    a first boot from the startup-config (the image's own files are kept)."""
    rc, up, _ = _run([DOCKER, "inspect", "-f", "{{.GraphDriver.Data.UpperDir}}", cname])
    rc2, img, _ = _run([DOCKER, "inspect", "-f", "{{.Config.Image}}", cname])
    up, img = up.strip(), img.strip()
    if rc or rc2 or not up.startswith("/var/lib/docker/") or not os.path.isdir(up):
        raise TopoError("cannot find the writable layer of %s" % cname)
    exts = ROUTERS[kind]["disks"]
    for fn in os.listdir(up):
        if not fn.endswith(exts) or not os.path.isfile(os.path.join(up, fn)):
            continue
        rc, _, _ = _run([DOCKER, "run", "--rm", "--entrypoint", "test", img, "-e", "/" + fn], timeout=60)
        if rc == 0:
            continue                    # shipped in the image: keep
        os.remove(os.path.join(up, fn))
        log("  %s: removed the previous run's %s" % (cname, fn))


def restart_routers(lab, doc_after, nodes, log):
    """Restart routers so their VMs boot with every link in doc_after."""
    import devcfg
    base = os.path.dirname(lab["path"])
    links = _links(doc_after)
    cn = {n: _cname(lab, n) for n in nodes}
    # 1. save running configs and hand the startup file to the node dir
    for n in nodes:
        try:
            r = devcfg.save_startup(lab, n)
            log("  %s: running config saved to %s" % (n, r["path"]))
        except Exception as exc:                          # noqa: BLE001
            raise TopoError("saving %s's running config failed (%s) - nothing was restarted"
                            % (n, exc))
    _, _, doc_after = _load(lab)        # save_startup may have added startup-config lines
    for n in nodes:
        ncfg = doc_after["topology"]["nodes"][n]
        rel = ncfg.get("startup-config")
        dst = os.path.join(base, "clab-%s" % lab["name"], n, "config", "startup-config.cfg")
        if rel and os.path.isdir(os.path.dirname(dst)):
            shutil.copyfile(os.path.join(base, rel), dst)
            log("  %s: startup file handed to the node" % n)
    # 2. stop all, reset disks
    for n in nodes:
        _run([DOCKER, "stop", "-t", "10", cn[n]], timeout=120)
        _reset_disks(cn[n], _node_kind(doc_after, n), log)
    # 3. start frozen
    for n in nodes:
        rc, _, err = _run([DOCKER, "start", cn[n]], timeout=120)
        if rc != 0:
            raise TopoError("docker start %s: %s" % (cn[n], err.strip()))
        _run([DOCKER, "pause", cn[n]], timeout=60)
        log("  %s started and frozen while its links are created" % n)
    # 4. every link of a restarted node
    running = {c.get("short") for c in lab.get("containers") or [] if c.get("state") == "running"}
    done = set()
    for a, ai, b, bi, _ in links:
        if a not in nodes and b not in nodes:
            continue
        key = frozenset((a + ":" + ai, b + ":" + bi))
        if key in done:
            continue
        done.add(key)
        other = b if a in nodes else a
        if other not in nodes and other not in running:
            log("  skip %s:%s - %s:%s (%s is not running)" % (a, ai, b, bi, other))
            continue
        veth(_cname(lab, a), ai, _cname(lab, b), bi, log)
        for n, p in ((a, ai), (b, bi)):
            if n not in nodes and _node_kind(doc_after, n) in ROUTERS:
                rewire(_cname(lab, n), p, log)
    # 5. dummies: fill gaps (XRd needs consecutive ports) and reach the
    #    interface count the launcher waits for
    for n in nodes:
        kind = _node_kind(doc_after, n)
        mine = sorted(port_num(p) for a, ai, b, bi, _ in links
                      for (x, p) in ((a, ai), (b, bi)) if x == n)
        rc, env, _ = _run([DOCKER, "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", cn[n]])
        want = 0
        for line in env.splitlines():
            if line.startswith("CLAB_INTFS="):
                want = int(line.split("=", 1)[1] or 0)
        have = set(mine)
        fill = []
        if ROUTERS[kind]["contiguous"] and have:
            fill += [i for i in range(1, max(have)) if i not in have]
        i = 1
        while len(have) + len(fill) < want:
            if i not in have and i not in fill:
                fill.append(i)
            i += 1
        for i in fill:
            _ns(cn[n], IP, "link", "add", "eth%d" % i, "type", "dummy")
            _ns(cn[n], IP, "link", "set", "eth%d" % i, "up")
        if fill:
            log("  %s: placeholder interfaces %s" % (n, ", ".join("eth%d" % i for i in fill)))
    # 6. go
    for n in nodes:
        _run([DOCKER, "unpause", cn[n]], timeout=60)
    log("  booting %s" % ", ".join(nodes))
    wait_healthy([cn[n] for n in nodes], log)


# --------------------------------------------------------------------------
# link addressing
# --------------------------------------------------------------------------

def link_config(lab, node, kind, port, addr, igp):
    """Config lines for one end of a new link, in the router's own dialect."""
    try:
        with open(os.path.join(os.path.dirname(lab["path"]),
                               _startup_of(lab, node) or "")) as fh:
            cfg = fh.read()
    except OSError:
        cfg = ""
    ifc = ipaddress.ip_interface(addr)
    name = iface_name(kind, port_num(port))
    style = ROUTERS[kind]["if"]
    isis = re.search(r"^router isis (\S+)", cfg, re.M)
    ospf = re.search(r"^router ospf (\S+)", cfg, re.M)
    area = (re.search(r"ip (?:router )?ospf \S+ area (\S+)", cfg)
            or re.search(r"^\s+area (\S+)", cfg, re.M))
    mtu = re.search(r"^\s+mtu (\d+)", cfg, re.M)
    # the port may have carried another link before: start from a clean interface
    L = ["default interface %s" % name] if style in ("xe", "nxos") else ["no interface %s" % name]
    if style == "nxos":
        L += ["interface %s" % name, "  description dashboard link", "  no switchport"]
        if mtu:
            L.append("  mtu %s" % mtu.group(1))
        L.append("  ip address %s" % ifc.with_prefixlen)
        if igp and isis:
            L += ["  ip router isis %s" % isis.group(1), "  isis network point-to-point"]
        elif igp and ospf:
            L += ["  ip router ospf %s area %s" % (ospf.group(1), area.group(1) if area else "0"),
                  "  ip ospf network point-to-point"]
        L.append("  no shutdown")
        return "\n".join(L) + "\n"
    if style == "xe":
        L += ["interface %s" % name, " description dashboard link",
              " ip address %s %s" % (ifc.ip, ifc.network.netmask)]
        if mtu:
            L.append(" mtu %s" % mtu.group(1))
        if igp and isis:
            L += [" ip router isis %s" % isis.group(1), " isis network point-to-point"]
        elif igp and ospf:
            L += [" ip ospf %s area %s" % (ospf.group(1), area.group(1) if area else "0"),
                  " ip ospf network point-to-point"]
        L.append(" no shutdown")
    else:
        L += ["interface %s" % name, " description dashboard link",
              " ipv4 address %s %s" % (ifc.ip, ifc.network.netmask)]
        if mtu:
            L.append(" mtu %s" % mtu.group(1))
        L += [" no shutdown", "!"]
        if igp and isis:
            L += ["router isis %s" % isis.group(1), " interface %s" % name, "  point-to-point",
                  "  address-family ipv4 unicast", "  !", " !", "!"]
        elif igp and ospf:
            L += ["router ospf %s" % ospf.group(1),
                  " area %s" % (area.group(1) if area else "0"),
                  "  interface %s" % name, "   network point-to-point", "  !", " !", "!"]
    return "\n".join(L) + "\n"


def exec_link_config(platform, running, port, addr, igp):
    """Lines for one end of a new link on an FRR or SR Linux node. `running` is
    the node's current config, read for the IGP it runs."""
    ifc = ipaddress.ip_interface(addr)
    if platform == "frr":
        isis = re.search(r"^router isis (\S+)", running, re.M)
        ospf = re.search(r"^router ospf", running, re.M)
        area = re.search(r"ip ospf area (\S+)", running)
        L = ["interface %s" % port, " description dashboard link",
             " ip address %s" % ifc.with_prefixlen]
        if igp and isis:
            L += [" ip router isis %s" % isis.group(1), " isis network point-to-point"]
        elif igp and ospf:
            L += [" ip ospf area %s" % (area.group(1) if area else "0"),
                  " ip ospf network point-to-point"]
        L.append("exit")
        return "\n".join(L) + "\n"
    name = iface_name("nokia_srlinux", port_num(port))
    i = "set / interface %s" % name
    ni = "set / network-instance default"
    L = ["%s admin-state enable" % i, '%s description "dashboard link"' % i,
         "%s subinterface 0 admin-state enable" % i,
         "%s subinterface 0 ipv4 admin-state enable" % i,
         "%s subinterface 0 ipv4 address %s" % (i, ifc.with_prefixlen),
         "%s interface %s.0" % (ni, name)]
    isis = re.search(r"protocols isis instance (\S+)", running)
    ospf = re.search(r"protocols ospf instance (\S+) area (\S+)", running)
    if igp and isis:
        q = "%s protocols isis instance %s interface %s.0" % (ni, isis.group(1), name)
        L += ["%s circuit-type point-to-point" % q, "%s ipv4-unicast admin-state enable" % q]
    elif igp and ospf:
        L.append("%s protocols ospf instance %s area %s interface %s.0 interface-type point-to-point"
                 % (ni, ospf.group(1), ospf.group(2), name))
    return "\n".join(L) + "\n"


def _add_exec(lab, node, cmds, log):
    """Append exec: lines to a node in the topology (round-trip YAML, no duplicates)."""
    from ruamel.yaml.comments import CommentedSeq
    text, y, doc = _load(lab)
    n = doc["topology"]["nodes"][node]
    ex = n.get("exec")
    if ex is None:
        n["exec"] = ex = CommentedSeq()
    new = [c for c in cmds if c not in ex]
    if not new:
        return
    ex.extend(new)
    buf = io.StringIO()
    y.dump(doc, buf)
    editor.write_file(lab, os.path.basename(lab["path"]), buf.getvalue(), editor.sha(text))
    log("  %s: topology exec: + %s" % (node, "; ".join(new)))


def _node_platform(lab, doc, node):
    import devcfg
    n = ((doc.get("topology") or {}).get("nodes") or {}).get(node) or {}
    kind = _node_kind(doc, node)
    image = n.get("image") or (((doc.get("topology") or {}).get("kinds") or {}).get(kind) or {}).get("image")
    return devcfg.platform_of(kind, image)


def _startup_of(lab, node):
    _, _, doc = _load(lab)
    n = ((doc.get("topology") or {}).get("nodes") or {}).get(node) or {}
    return n.get("startup-config")


def configure_links(lab_index_fn, lab_id, ch, log):
    import devcfg
    todo = [l for l in ch["add_links"] if l.get("a_ip") or l.get("b_ip")]
    if not todo:
        return
    for l in todo:
        for side in ("a", "b"):
            addr = l.get(side + "_ip")
            if not addr:
                continue
            lab = lab_index_fn().get(lab_id)
            n, p = l[side], l[side + "_if"]
            _, _, doc = _load(lab)
            kind = _node_kind(doc, n)
            c = _container(lab, n)
            plat = _node_platform(lab, doc, n)
            if plat in devcfg.EXEC_PLATFORMS and c:
                cur = devcfg.running_config(lab_index_fn(), lab_id, n)["text"]
                if plat == "frr":
                    # FRR has no MTU command: match the node's other data ports
                    # (clab leaves veths at 9500, IOS peers pad IS-IS hellos to 1500)
                    rc, out, _ = _ns(_cname(lab, n), IP, "-o", "link")
                    mtus = [int(m) for nm, m in re.findall(r"^\d+: (eth[1-9]\d*)[@:].*? mtu (\d+)", out, re.M)
                            if nm != p]
                    if mtus:
                        _ns(_cname(lab, n), IP, "link", "set", p, "mtu", str(min(mtus)))
                    _ns(_cname(lab, n), sysbin.find("sysctl"), "-q", "-w", "net.mpls.conf.%s.input=1" % p)
                    # and the same at the next deploy
                    _add_exec(lab_index_fn().get(lab_id), n,
                              (["ip link set %s mtu %d" % (p, min(mtus))] if mtus else [])
                              + ["sysctl -w net.mpls.conf.%s.input=1" % p], log)
                text = exec_link_config(plat, cur, p, addr, l.get("igp"))
                r = devcfg.push_config(lab_index_fn(), lab_id, n, text, save_startup_after=True)
                log("  %s: %s" % (n, ("%s configured on %s and saved" % (addr, p)) if r.get("ok")
                                  else "configuration errors: " + "; ".join(r.get("errors") or [])))
                continue
            if kind in NATIVE:
                ifc = ipaddress.ip_interface(addr)
                cmds = [[IP, "addr", "add", str(ifc), "dev", p], [IP, "link", "set", p, "up"]]
                for cmd in cmds:
                    _ns(_cname(lab, n), *cmd)
                log("  %s: %s on %s (not persistent - add an exec: line for a redeploy)" % (n, addr, p))
                continue
            if kind not in ROUTERS or not c:
                log("  %s: skipped (%s)" % (n, "not running" if not c else "no template for " + kind))
                continue
            text = link_config(lab, n, kind, p, addr, l.get("igp"))
            r = devcfg.push_config(lab_index_fn(), lab_id, n, text, save_startup_after=True)
            if r.get("ok"):
                log("  %s: %s configured on %s and saved to its startup file"
                    % (n, addr, iface_name(kind, port_num(p))))
            else:
                log("  %s: configuration errors: %s" % (n, "; ".join(r.get("errors") or [])))


def suggest_subnets(lab, count=12):
    """Free point-to-point subnets: the builder's link pool (from the file header)
    or 10.10.0.0/16, minus every IPv4 network mentioned in the lab's configs."""
    base = os.path.dirname(lab["path"])
    with open(lab["path"]) as fh:
        head = fh.read()
    m = re.search(r"^#\s*Links:\s*(\S+)\s+in\s+/(\d+)", head, re.M)
    pool = ipaddress.ip_network(m.group(1) if m else "10.10.0.0/16", strict=False)
    plen = int(m.group(2)) if m else 30
    used = []
    texts = [head]
    for rel in [f["path"] for f in editor.editable_files(lab)]:
        try:
            with open(os.path.join(base, rel), errors="replace") as fh:
                texts.append(fh.read())
        except OSError:
            pass
    for t in texts:
        for a, mask in re.findall(r"(\d+\.\d+\.\d+\.\d+)[ /](\d+\.\d+\.\d+\.\d+|\d{1,2})\b", t):
            try:
                used.append(ipaddress.ip_interface("%s/%s" % (a, mask)).network)
            except ValueError:
                pass
    out = []
    for sub in pool.subnets(new_prefix=plen):
        if any(sub.overlaps(u) for u in used if u.prefixlen >= 24):
            continue
        hosts = list(sub) if plen == 31 else list(sub.hosts())
        out.append({"subnet": str(sub), "a": "%s/%d" % (hosts[0], plen),
                    "b": "%s/%d" % (hosts[1], plen)})
        if len(out) >= count:
            break
    return out
