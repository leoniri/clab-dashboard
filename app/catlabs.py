#!/usr/bin/env python3
"""
Hand-built catalogue labs - service-provider designs the topology builder
cannot express (SRv6, TI-LFA, route reflectors).

Each lab is a function that returns the lab's files for a given lab name,
images and management subnet: the topology, one config per node and a
README with the design and how to verify it. The management addresses have
to be generated, not fixed - an IOS-XR vRouter under vrnetlab only answers on
its management port if the address in its config equals the one containerlab
gives the container.

Kinds used: cisco_xrd_vrouter (vrnetlab XRd, 8 GiB each - the launcher's
floor) for every provider router, and FRR (a linux container) for customer
edges, sources and receivers, so the CE count costs almost nothing.

Every config here was brought up and checked on XRd 26.2.1 / FRR 10.4.1 -
see VERIFIED in each lab's entry.
"""

import ipaddress

XR_ENV = {"VCPU": "2", "RAM": "8192", "XRD_NIC_TYPE": "igb", "PASSWORD": "clab@123"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def xr_if(eth):
    """clab ethN -> the XRd vRouter interface name (eth1 = Gi0/0/0/0)."""
    return "GigabitEthernet0/0/0/%d" % (eth - 1)


def xr_head(name, mgmt_ip, mask, srv6=False):
    L = ["hostname %s" % name,
         "logging console disable",
         "!",
         "line default",
         " transport input ssh",
         "!",
         "ssh server v2",
         "ssh server vrf default",
         "!"]
    # no `hw-module profile segment-routing srv6 mode micro-segment` here: XRd
    # 26.2.1 rejects it as a syntax error and allocates uSIDs without it
    L += ["interface MgmtEth0/RP0/CPU0/0",
          " ipv4 address %s %s" % (mgmt_ip, mask),
          " no shutdown",
          "!",
          "route-policy PASS",
          "  pass",
          "end-policy",
          "!"]
    return L


def xr_tail(L):
    return "\n".join(L + ["commit", "end"]) + "\n"


def frr_daemons(bgp=True):
    on = {"zebra", "staticd"} | ({"bgpd"} if bgp else set())
    names = ("zebra", "bgpd", "ospfd", "ospf6d", "ripd", "ripngd", "isisd", "pimd", "pim6d",
             "ldpd", "nhrpd", "eigrpd", "babeld", "sharpd", "staticd", "pbrd", "bfdd", "fabricd")
    L = ["%s=%s" % (d, "yes" if d in on else "no") for d in names]
    L += ["vtysh_enable=yes", 'zebra_options="  -A 127.0.0.1 -s 90000000"']
    L += ['%s_options="  -A 127.0.0.1"' % d for d in names if d != "zebra"]
    L.append('frr_profile="traditional"')
    return "\n".join(L) + "\n"


class Lab:
    """Collects nodes and links, then writes the clab topology."""

    def __init__(self, name, mgmt, images, header):
        self.name = name
        self.net = ipaddress.ip_network(mgmt, strict=False)
        self.hosts = self.net.hosts()
        next(self.hosts)                             # .1 is the bridge
        self.mask = str(self.net.netmask)
        self.images = images
        self.header = header
        self.nodes = []                              # (name, kind, mgmt ip, x, y, icon, extra yaml lines)
        self.links = []
        self.files = {}
        self.eth = {}

    def ip(self):
        return str(next(self.hosts))

    def port(self, node):
        self.eth[node] = self.eth.get(node, 0) + 1
        return self.eth[node]

    def link(self, a, b, note=""):
        pa, pb = self.port(a), self.port(b)
        self.links.append((a, pa, b, pb, note))
        return pa, pb

    def xr(self, name, mgmt, x, y, cfg, icon="router"):
        self.nodes.append((name, "cisco_xrd_vrouter", mgmt, x, y, icon,
                           ["startup-config: configs/%s.cfg" % name]))
        self.files["configs/%s.cfg" % name] = cfg

    def frr(self, name, mgmt, x, y, conf, execs, icon="router", daemons=True):
        extra = ["binds:",
                 "  - configs/%s.daemons:/etc/frr/daemons" % name,
                 "  - configs/%s.frr.conf:/etc/frr/frr.conf" % name,
                 "sysctls:",
                 "  net.ipv4.ip_forward: 1",
                 "  net.ipv6.conf.all.forwarding: 1",
                 "  net.ipv6.conf.all.disable_ipv6: 0",
                 "exec:"] + ["  - %s" % c for c in ["touch /etc/frr/vtysh.conf"] + execs]
        self.nodes.append((name, "linux", mgmt, x, y, icon, extra + ["labels-extra: frr"]))
        self.files["configs/%s.frr.conf" % name] = conf
        self.files["configs/%s.daemons" % name] = frr_daemons(daemons)

    def topology(self):
        y = ["# " + l if l else "#" for l in self.header]
        y += ["", "name: %s" % self.name, "", "mgmt:", "  network: %s-mgmt" % self.name,
              "  ipv4-subnet: %s" % self.net, "", "topology:", "  kinds:",
              "    cisco_xrd_vrouter:", "      image: %s" % self.images["cisco_xrd_vrouter"], "      env:"]
        y += ['        %s: "%s"' % kv for kv in XR_ENV.items()]
        y += ["    linux:", "      image: %s" % self.images["frr"], "", "  nodes:"]
        for name, kind, mgmt, px, py, icon, extra in self.nodes:
            y += ["    %s:" % name, "      kind: %s" % kind, "      mgmt-ipv4: %s" % mgmt]
            frr = "labels-extra: frr" in extra
            y += ["      " + e for e in extra if e != "labels-extra: frr"]
            y += ["      labels:", '        builder-pos: "%d,%d"' % (px, py), '        graph-icon: "%s"' % icon]
            if frr:
                y.append('        builder-kind: "frr"')
        y += ["", "  links:"]
        for a, pa, b, pb, note in self.links:
            y.append('    - endpoints: ["%s:eth%d", "%s:eth%d"]%s' % (a, pa, b, pb, ("   # " + note) if note else ""))
        return "\n".join(y) + "\n"

    def done(self, readme):
        self.files["%s.clab.yml" % self.name] = self.topology()
        self.files["README.md"] = readme
        return self.files


def frr_ce(name, asn, rid, links, lo4, lo6, peers):
    """A customer router: addresses, loopbacks, eBGP (v4 + v6) to its PE(s)."""
    L = ["frr version 10", "frr defaults traditional", "hostname %s" % name,
         "service integrated-vtysh-config", "!", "interface lo",
         " ip address %s/32" % lo4, " ipv6 address %s/128" % lo6, "exit", "!"]
    for ifn, a4, a6 in links:
        L += ["interface %s" % ifn]
        if a4:
            L.append(" ip address %s" % a4)
        if a6:
            L.append(" ipv6 address %s" % a6)
        L += ["exit", "!"]
    L += ["router bgp %d" % asn, " bgp router-id %s" % rid, " no bgp ebgp-requires-policy",
          " no bgp default ipv4-unicast"]
    for ip, ras in peers:
        L.append(" neighbor %s remote-as %d" % (ip, ras))
    L += [" !", " address-family ipv4 unicast", "  network %s/32" % lo4]
    L += ["  neighbor %s activate" % ip for ip, _ in peers if ":" not in ip]
    L += [" exit-address-family", " !", " address-family ipv6 unicast", "  network %s/128" % lo6]
    L += ["  neighbor %s activate" % ip for ip, _ in peers if ":" in ip]
    L += [" exit-address-family", "exit", "!", "line vty", "!"]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# the two SP core labs share one physical design
# --------------------------------------------------------------------------
#
#            CE1(A)   CE4(B)                        CE2(A)
#               \     /                               |
#                PE1 ------ P1 ============ P2 ------ PE2
#                  \       /  \            /  \       /
#                   \     /    RR1 -------/    \     /
#                    \   /                      \   /
#                     P3 ========================  PE3 --- CE3(A)
#                                                    \
#                                                     CE5(B)
#
CORE = {                       # name: (index, x, y)
    "pe1": (1, 80, 170), "pe2": (2, 620, 60), "pe3": (3, 620, 300),
    "p1": (11, 260, 60), "p2": (12, 440, 60), "p3": (13, 260, 300),
    "rr1": (21, 350, 190),
}
CORE_LINKS = [("pe1", "p1"), ("pe1", "p3"), ("p1", "p2"), ("p1", "p3"), ("p2", "p3"),
              ("pe2", "p2"), ("pe2", "p1"), ("pe3", "p3"), ("pe3", "p2"),
              ("rr1", "p1"), ("rr1", "p2")]
CES = [  # name, pe, vrf, asn, x, y
    ("ce1", "pe1", "CUST-A", 65101, -120, 100),
    ("ce4", "pe1", "CUST-B", 65104, -120, 250),
    ("ce2", "pe2", "CUST-A", 65102, 820, 60),
    ("ce3", "pe3", "CUST-A", 65103, 820, 250),
    ("ce5", "pe3", "CUST-B", 65105, 820, 380),
]
VRFS = {"CUST-A": "65000:100", "CUST-B": "65000:200"}


def _core_common(name, mgmt, images, transport):
    header = ["%s - service-provider core, IOS-XR, %s" % (name, transport),
              "generated by the containerlab dashboard catalogue - see README.md",
              "3 PE + 3 P + 1 route reflector (XRd), 5 CEs (FRR) in two VRFs"]
    lab = Lab(name, mgmt, images, header)
    mg = {n: lab.ip() for n in list(CORE) + [c[0] for c in CES]}
    ifs = {n: [] for n in CORE}                          # (eth, peer, link index)
    for k, (a, b) in enumerate(CORE_LINKS, start=1):
        pa, pb = lab.link(a, b, "core link %d" % k)
        ifs[a].append((pa, b, k))
        ifs[b].append((pb, a, k))
    ce_if = {}
    for k, (ce, pe, vrf, asn, x, y) in enumerate(CES, start=1):
        pp, pc = lab.link(pe, ce, "%s %s" % (vrf, ce))
        ce_if[ce] = (pp, pc, k)
    return lab, mg, ifs, ce_if


def _ce_nodes(lab, mg, ce_if, pe_asn=65000):
    for ce, pe, vrf, asn, x, y in CES:
        pp, pc, k = ce_if[ce]
        num = int(ce[2:])                          # loopbacks follow the CE's name
        conf = frr_ce(ce, asn, "192.168.%d.1" % (100 + num), [("eth%d" % pc, "10.100.%d.2/30" % k,
                      "2001:db8:100:%d::2/64" % k)],
                      "192.168.%d.1" % (100 + num), "2001:db8:c:%d::1" % num,
                      [("10.100.%d.1" % k, pe_asn), ("2001:db8:100:%d::1" % k, pe_asn)])
        lab.frr(ce, mg[ce], x, y, conf, ["ip link set eth%d mtu 1500" % pc], icon="router")


def _pe_vrf_ifs(pe, ce_if):
    """VRF interface lines for the CEs of one PE."""
    L = []
    for ce, p, vrf, asn, x, y in CES:
        if p != pe:
            continue
        pp, pc, k = ce_if[ce]
        L += ["interface %s" % xr_if(pp), " description to %s (%s)" % (ce, vrf), " vrf %s" % vrf,
              " ipv4 address 10.100.%d.1 255.255.255.252" % k, " ipv6 address 2001:db8:100:%d::1/64" % k,
              " no shutdown", "!"]
    return L


def _vrf_defs(pe):
    L = []
    for vrf in sorted({c[2] for c in CES if c[1] == pe}):
        rt = VRFS[vrf]
        L += ["vrf %s" % vrf]
        for af in ("ipv4", "ipv6"):
            L += [" address-family %s unicast" % af, "  import route-target", "   %s" % rt, "  !",
                  "  export route-target", "   %s" % rt, "  !", " !"]
        L.append("!")
    return L


def _bgp_vrfs(pe, srv6):
    L = []
    for vrf in sorted({c[2] for c in CES if c[1] == pe}):
        L += [" vrf %s" % vrf, "  rd auto"]
        for af in ("ipv4", "ipv6"):
            L.append("  address-family %s unicast" % af)
            if srv6:
                L += ["   segment-routing srv6", "    locator MAIN", "    alloc mode per-vrf", "   !"]
            else:
                L.append("   label mode per-vrf")
            L += ["   redistribute connected", "  !"]
        for ce, p, v, asn, x, y in CES:
            if p != pe or v != vrf:
                continue
            k = [c[0] for c in CES].index(ce) + 1
            for ip, af in (("10.100.%d.2" % k, "ipv4"), ("2001:db8:100:%d::2" % k, "ipv6")):
                L += ["  neighbor %s" % ip, "   remote-as %d" % asn, "   description %s" % ce,
                      "   address-family %s unicast" % af, "    route-policy PASS in",
                      "    route-policy PASS out", "   !", "  !"]
        L.append(" !")
    return L


def sp_srmpls(name, images, mgmt):
    lab, mg, ifs, ce_if = _core_common(name, mgmt, images, "SR-MPLS")
    for n, (idx, x, y) in CORE.items():
        L = xr_head(n, mg[n], lab.mask)
        L += _vrf_defs(n)
        L += ["interface Loopback0", " ipv4 address 10.0.0.%d 255.255.255.255" % idx,
              " ipv6 address 2001:db8::%d/128" % idx, "!"]
        for eth, peer, k in ifs[n]:
            me = 1 if (n, peer) in CORE_LINKS else 2
            L += ["interface %s" % xr_if(eth), " description to %s" % peer, " mtu 9014",
                  " ipv4 address 10.1.%d.%d 255.255.255.252" % (k, me),
                  " ipv6 address 2001:db8:1:%d::%d/64" % (k, me), " no shutdown", "!"]
        L += _pe_vrf_ifs(n, ce_if)
        L += ["segment-routing", " global-block 16000 23999", "!",
              "router isis CORE", " is-type level-2-only", " net 49.0001.0000.0000.%04d.00" % idx,
              " log adjacency changes",
              " address-family ipv4 unicast", "  metric-style wide", "  segment-routing mpls", " !",
              " address-family ipv6 unicast", "  metric-style wide", " !",
              " interface Loopback0", "  passive", "  address-family ipv4 unicast",
              "   prefix-sid index %d" % idx, "  !", "  address-family ipv6 unicast", "  !", " !"]
        for eth, peer, k in ifs[n]:
            L += [" interface %s" % xr_if(eth), "  point-to-point",
                  "  address-family ipv4 unicast", "   fast-reroute per-prefix",
                  "   fast-reroute per-prefix ti-lfa", "  !", "  address-family ipv6 unicast", "  !", " !"]
        L.append("!")
        L += _bgp(n, idx, srv6=False)
        lab.xr(n, mg[n], CORE[n][1], CORE[n][2], xr_tail(L), icon="router")
    _ce_nodes(lab, mg, ce_if)
    return lab.done(README_SRMPLS.replace("{name}", name))


def sp_srv6(name, images, mgmt):
    lab, mg, ifs, ce_if = _core_common(name, mgmt, images, "SRv6 uSID")
    for n, (idx, x, y) in CORE.items():
        L = xr_head(n, mg[n], lab.mask, srv6=True)
        L += _vrf_defs(n)
        L += ["interface Loopback0", " ipv4 address 10.0.0.%d 255.255.255.255" % idx,
              " ipv6 address 2001:db8::%d/128" % idx, "!"]
        for eth, peer, k in ifs[n]:
            me = 1 if (n, peer) in CORE_LINKS else 2
            L += ["interface %s" % xr_if(eth), " description to %s" % peer, " mtu 9014",
                  " ipv6 address 2001:db8:1:%d::%d/64" % (k, me), " no shutdown", "!"]
        L += _pe_vrf_ifs(n, ce_if)
        L += ["segment-routing", " srv6", "  encapsulation", "   source-address 2001:db8::%d" % idx, "  !",
              "  locators", "   locator MAIN", "    micro-segment behavior unode psp-usd",
              "    prefix fc00:0:%d::/48" % idx, "   !", "  !", " !", "!",
              "router isis CORE", " is-type level-2-only", " net 49.0001.0000.0000.%04d.00" % idx,
              " log adjacency changes",
              " address-family ipv6 unicast", "  metric-style wide", "  segment-routing srv6",
              "   locator MAIN", "   !", "  !", " !",
              " interface Loopback0", "  passive", "  address-family ipv6 unicast", "  !", " !"]
        for eth, peer, k in ifs[n]:
            L += [" interface %s" % xr_if(eth), "  point-to-point",
                  "  address-family ipv6 unicast", "   fast-reroute per-prefix",
                  "   fast-reroute per-prefix ti-lfa", "  !", " !"]
        L.append("!")
        L += _bgp(n, idx, srv6=True)
        lab.xr(n, mg[n], CORE[n][1], CORE[n][2], xr_tail(L), icon="router")
    _ce_nodes(lab, mg, ce_if)
    return lab.done(README_SRV6.replace("{name}", name))


def _bgp(n, idx, srv6):
    """PEs peer with the RR only; the RR reflects VPNv4/VPNv6 to every PE."""
    rr = CORE["rr1"][0]
    pes = [p for p in CORE if p.startswith("pe")]
    if n.startswith("p") and not n.startswith("pe"):
        return []
    peer_ip = (lambda i: "2001:db8::%d" % i) if srv6 else (lambda i: "10.0.0.%d" % i)
    L = ["router bgp 65000", " bgp router-id 10.0.0.%d" % idx,
         " address-family vpnv4 unicast", " !", " address-family vpnv6 unicast", " !"]
    if n == "rr1":
        for p in pes:
            L += [" neighbor %s" % peer_ip(CORE[p][0]), "  remote-as 65000", "  description %s" % p,
                  "  update-source Loopback0",
                  "  address-family vpnv4 unicast", "   route-reflector-client", "  !",
                  "  address-family vpnv6 unicast", "   route-reflector-client", "  !", " !"]
    else:
        L += [" neighbor %s" % peer_ip(rr), "  remote-as 65000", "  description rr1",
              "  update-source Loopback0",
              "  address-family vpnv4 unicast", "  !", "  address-family vpnv6 unicast", "  !", " !"]
        L += _bgp_vrfs(n, srv6)
    L.append("!")
    return L


README_SRMPLS = """# {name} - service-provider core on IOS-XR, SR-MPLS

A typical SP core: three PEs, three P routers and a route reflector, all
IOS-XR (XRd), with five customer sites in two L3VPNs.

```
  ce1 (CUST-A) --\\                               /-- ce2 (CUST-A)
  ce4 (CUST-B) -- pe1 --- p1 ======= p2 --- pe2
                    \\     |  \\     /  |     /
                     \\    |   rr1    |    /
                      \\   |          |   /
                        p3 ========== +-- pe3 -- ce3 (CUST-A), ce5 (CUST-B)
```

Every PE is dual-homed into the P triangle, so there is always a backup path.

| | |
|---|---|
| IGP | IS-IS L2, wide metrics, point-to-point links, IPv4 + IPv6 |
| Transport | SR-MPLS, SRGB 16000-23999, prefix-SID index = node number |
| Protection | TI-LFA on every core interface |
| BGP | AS 65000; PEs peer only with rr1 (VPNv4 + VPNv6 route reflection) |
| Services | L3VPN CUST-A (RT 65000:100) and CUST-B (RT 65000:200), IPv4 + IPv6 (6VPE), per-VRF labels |
| CEs | FRR, eBGP v4 + v6 to their PE, AS 6510x, loopbacks 192.168.10x.1 / 2001:db8:c:x::1 |

| Node | Loopback0 | SID |
|---|---|---|
| pe1 / pe2 / pe3 | 10.0.0.1 / .2 / .3 | 16001 / 16002 / 16003 |
| p1 / p2 / p3 | 10.0.0.11 / .12 / .13 | 16011 / 16012 / 16013 |
| rr1 | 10.0.0.21 | 16021 |

Needs about **56 GB** of RAM (XRd vRouter takes 8 GiB per node) and 4-5
minutes to boot. Logins: XRd `clab` / `clab@123`; CEs open vtysh from the CLI button.

## Try it

- `show isis fast-reroute summary` / `show isis fast-reroute 10.0.0.2/32 detail` on pe1 - the TI-LFA backup
- `show bgp vpnv4 unicast summary` on rr1 - three PEs, routes reflected
- `show cef vrf CUST-A 192.168.103.1/32` on pe1 - transport + VPN label
- From ce1: `ping 192.168.103.1 -I 192.168.101.1` (CUST-A) - and `ping 192.168.105.1` must fail: CUST-B is another VPN
- Topology page: **Trace path** ce1 -> ce3 (pe1 > p3 > pe3), then fail pe1-p3 and watch the path move
- **Convergence tests**: probe ce1 -> ce3 and fail pe1-p3 - compare *shut both ends* with a silent *cut*
"""

README_SRV6 = """# {name} - service-provider core on IOS-XR, SRv6 uSID

The same SP core as the SR-MPLS lab - three PEs, three P routers, a route
reflector and five customer sites in two L3VPNs - but the core is IPv6-only
and the transport is SRv6 micro-SIDs (uSID).

```
  ce1 (CUST-A) --\\                               /-- ce2 (CUST-A)
  ce4 (CUST-B) -- pe1 --- p1 ======= p2 --- pe2
                    \\     |  \\     /  |     /
                     \\    |   rr1    |    /
                      \\   |          |   /
                        p3 ========== +-- pe3 -- ce3 (CUST-A), ce5 (CUST-B)
```

| | |
|---|---|
| Core | IPv6 only (2001:db8:1:<link>::/64), no MPLS |
| IGP | IS-IS L2 IPv6, SRv6 locator advertised, TI-LFA on every core interface |
| SRv6 | uSID format f3216, block fc00::/24, locator MAIN = fc00:0:<node>::/48, `unode psp-usd` |
| BGP | AS 65000 over IPv6 loopbacks, PEs peer with rr1; VPNv4 + VPNv6 with SRv6 SIDs per VRF (uDT4 / uDT6) |
| Services | CUST-A (RT 65000:100), CUST-B (RT 65000:200), IPv4 + IPv6 |

| Node | Loopback0 (v6) | Locator |
|---|---|---|
| pe1 / pe2 / pe3 | 2001:db8::1 / ::2 / ::3 | fc00:0:1::/48 / :2:: / :3:: |
| p1 / p2 / p3 | 2001:db8::11 / ::12 / ::13 | fc00:0:11::/48 / :12:: / :13:: |
| rr1 | 2001:db8::21 | fc00:0:21::/48 |

Needs about **56 GB** of RAM and 4-5 minutes to boot. Logins: XRd `clab` / `clab@123`.

## Try it

- `show segment-routing srv6 sid` on pe1 - uN for the node, uDT4 / uDT6 per VRF
- `show isis ipv6 fast-reroute summary` on pe1 - TI-LFA coverage of the IPv6 core
- `show bgp vpnv4 unicast vrf CUST-A 192.168.103.1/32 detail` - the SRv6 service SID in the route
- `show cef vrf CUST-A 192.168.103.1/32 detail` on pe1 - `H.Encaps.Red SID-list {fc00:0:3:e00x::}`
- Capture on pe1's link to p1 while ce1 pings ce3: IPv6 packets to fc00:0:3:e00x:: with the customer packet inside, no SRH
- **Convergence tests** / **Trace path** as in the SR-MPLS lab (the trace shows the H.Encaps.Red on pe1, the P routers forwarding on the outer IPv6 destination, and the decapsulation on the egress PE)
"""
