#!/usr/bin/env python3
"""
Advanced service-provider scenarios for the catalogue: Inter-AS options A/B/C
over SR-MPLS and SRv6, Carrier supporting Carrier, and a handful of other SP
designs. Each scenario is written once as a list of devices in a vendor-neutral
model (Dev below) and rendered per platform:

    xr    IOS-XR   (vrnetlab/cisco_xrd-vrouter, kind cisco_xrd_vrouter)
    xe    IOS-XE   (vrnetlab/cisco_c8000v, kind cisco_c8000v)
    frr   FRR      (quay.io/frrouting/frr, clab kind linux)
    srl   SR Linux (ghcr.io/nokia/srlinux) - customer edge only: the free
                   container has no MPLS

A *preset* maps each role of a scenario to a platform. Only presets that were
deployed and checked end to end are offered - see PRESETS / VERIFIED in each
scenario. Everything the renderers emit is startup configuration, so a lab is
fully configured the moment it has booted.

Conventions shared by every scenario:
  AS 65001 / 65002       the two provider ASes (Inter-AS), 65000 the backbone
                         carrier (CsC), 651xx customer sites
  10.<as>.0.<n>/32       loopbacks (as = 1, 2, 0 ...), SID index = <as><n>
  fc00:0:<sid>::/48      SRv6 uSID locators (block fc00:0::/32, f3216)
  CUST-A 65001:100       the customer VPN
"""

import ipaddress
import json

import scenario_guides

ROUTE_POLICY = "PASS"
MTU = 1500                        # L3 MTU everywhere; XR counts the L2 header (+14)

KIND = {"xr": "cisco_xrd_vrouter", "xe": "cisco_c8000v", "frr": "linux", "srl": "nokia_srlinux"}
IMAGE_SLOT = {"xr": "cisco_xrd_vrouter", "xe": "cisco_c8000v", "frr": "frr", "srl": "nokia_srlinux"}
RAM = {"xr": 8192, "xe": 4096, "frr": 256, "srl": 2048}
ENV = {
    "cisco_xrd_vrouter": {"VCPU": "2", "RAM": "8192", "XRD_NIC_TYPE": "igb", "PASSWORD": "clab@123"},
    "cisco_c8000v": {"USERNAME": "clab", "PASSWORD": "clab@123"},
}


# --------------------------------------------------------------------------
# the device model
# --------------------------------------------------------------------------

class If:
    """One routed interface. eth = the clab port number; vlan makes it a dot1q sub-interface."""

    def __init__(self, eth, peer, v4=None, v6=None, vrf=None, isis=False, mpls=False, ldp=False,
                 vlan=None, isis_passive=False, bgp_fwd=False, metric=None, delay=None, l2=False,
                 pim=False, igmp=False, joins=()):
        self.metric, self.delay, self.l2 = metric, delay, l2
        self.pim, self.igmp = pim, igmp          # PIM-SM on the interface / IGMPv3 querier towards hosts
        self.joins = list(joins)                # host side: (group, source or None) joined on this port
        self.eth, self.peer, self.v4, self.v6, self.vrf = eth, peer, v4, v6, vrf
        self.isis, self.mpls, self.ldp, self.vlan = isis, mpls, ldp, vlan
        self.isis_passive = isis_passive
        self.bgp_fwd = bgp_fwd          # MPLS forwarding for a directly connected eBGP LU / VPN peer


class Nbr:
    """A BGP neighbour. afs: ipv4 ipv6 ipv4lu ipv6lu vpnv4 vpnv6."""

    def __init__(self, ip, ras, afs, desc="", lo=False, multihop=False, nhs=(), rrc=(), vrf=None,
                 as_override=False, allowas=False, nh_unchanged=(), addpath=None):
        self.ip, self.ras, self.afs, self.desc = ip, ras, list(afs), desc
        self.lo, self.multihop, self.vrf = lo, multihop, vrf
        self.nhs, self.rrc = set(nhs), set(rrc)
        self.as_override, self.allowas = as_override, allowas
        self.nh_unchanged = set(nh_unchanged)
        self.addpath = addpath          # "send" (RR: advertise all paths) or "recv" (PE: accept them)
        self.xr_af = []                 # extra IOS-XR lines under the neighbour's address family


class Vrf:
    def __init__(self, name, rd, rt, v4=True, v6=False, srv6=False, lu=False, dual=False):
        self.name, self.rd, self.rt = name, rd, rt
        self.v4, self.v6, self.srv6 = v4, v6, srv6
        self.lu = lu                    # CsC: the VRF hands out labels to its CE (BGP labelled unicast)
        self.dual = dual                # SRv6 VRF that also allocates MPLS labels (migration)
        self.stitch_rt = None           # SRv6/MPLS gateway: the RT used on the other side


class Dev:
    def __init__(self, name, plat, x, y, role="", asn=None, icon="router"):
        self.name, self.plat, self.x, self.y = name, plat, x, y
        self.role, self.asn, self.icon = role, asn, icon
        self.lo4 = self.lo6 = None
        self.ifs = []
        self.isis = None                # {"net", "sr": sid index, "srv6": True, "tilfa", "redist": [...], "flex": [...]}
        self.ldp = False
        self.locator = None             # "fc00:0:11::/48"
        self.vrfs = []
        self.bgp = None                 # {"nbrs": [...], "net4": [...], "net6": [...], "retain_rt", "alloc_lu", "redist4": [...], "redist6": [...]}
        self.statics = []               # (prefix, next hop or interface eth)
        self.mapping_server = []        # (prefix, first sid, range) - SR/LDP interworking
        self.extra = {}                 # platform-specific additions: {"xr": [...], ...}
        self.mgmt = None
        self.pim = None                 # {"rp": addr, "asm": "239.0.0.0/8", "msdp": [peer...], "lo": True}
        self.lo_extra = []              # more /32s on the loopback (anycast RP), XE puts them on Loopback1..
        self.host = False               # an end host: default route, answers pings to groups it joined

    # convenience
    def add_if(self, *a, **k):
        i = If(*a, **k)
        self.ifs.append(i)
        return i

    def bgp_init(self, asn=None):
        if self.bgp is None:
            self.bgp = {"asn": asn or self.asn, "nbrs": [], "net4": [], "net6": [], "retain_rt": False,
                        "alloc_lu": False, "redist4": [], "redist6": []}
        return self.bgp

    def nbr(self, *a, **k):
        n = Nbr(*a, **k)
        self.bgp_init()["nbrs"].append(n)
        return n


def _net(addr):
    return ipaddress.ip_interface(addr)


def _v4mask(addr):
    i = _net(addr)
    return "%s %s" % (i.ip, i.netmask)


def _ip(addr):
    return str(_net(addr).ip)


def isis_net(sid):
    return "49.0001.0000.0000.%04d.00" % sid


# --------------------------------------------------------------------------
# IOS-XR
# --------------------------------------------------------------------------

def xr_if(eth, vlan=None):
    return "GigabitEthernet0/0/0/%d%s" % (eth - 1, (".%d" % vlan) if vlan else "")


def render_xr(d, mask):
    L = ["hostname %s" % d.name, "logging console disable", "!", "line default", " transport input ssh", "!",
         "ssh server v2", "ssh server vrf default", "!",
         "interface MgmtEth0/RP0/CPU0/0", " ipv4 address %s %s" % (d.mgmt, mask), " no shutdown", "!",
         "route-policy %s" % ROUTE_POLICY, "  pass", "end-policy", "!"]
    for v in d.vrfs:
        L.append("vrf %s" % v.name)
        for af, on in (("ipv4", v.v4), ("ipv6", v.v6)):
            if on:
                st = ["   %s stitching" % v.stitch_rt] if v.stitch_rt else []
                L += [" address-family %s unicast" % af, "  import route-target", "   %s" % v.rt] + st + ["  !",
                      "  export route-target", "   %s" % v.rt] + st + ["  !", " !"]
        L.append("!")
    L.append("interface Loopback0")
    if d.lo4:
        L.append(" ipv4 address %s 255.255.255.255" % d.lo4)
    if d.lo6:
        L.append(" ipv6 address %s/128" % d.lo6)
    L.append("!")
    parents = set()
    for i in d.ifs:
        if i.vlan and i.eth not in parents:
            parents.add(i.eth)
            L += ["interface %s" % xr_if(i.eth), " mtu %d" % (MTU + 18), " no shutdown", "!"]
    for i in d.ifs:
        if i.l2:
            L += ["interface %s" % xr_if(i.eth), " description to %s (attachment circuit)" % i.peer,
                  " mtu %d" % (MTU + 14), " l2transport", " !", "!"]
            continue
        L += ["interface %s" % xr_if(i.eth, i.vlan), " description to %s" % i.peer]
        if i.vrf:
            L.append(" vrf %s" % i.vrf)
        if i.vlan:
            L.append(" encapsulation dot1q %d" % i.vlan)
        else:
            L.append(" mtu %d" % (MTU + 14))
        if i.v4:
            L.append(" ipv4 address %s" % _v4mask(i.v4))
        if i.v6:
            L.append(" ipv6 address %s" % i.v6)
        L += [" no shutdown", "!"]
    if d.statics:
        # (prefix, via, [vrf]); via = a clab port number (interface) or an address
        L += ["router static"]
        for vrf in sorted({(s[2] if len(s) > 2 else "") for s in d.statics}):
            ind = " "
            if vrf:
                L.append(" vrf %s" % vrf)
                ind = "  "
            for af in ("ipv4", "ipv6"):
                rows = [s for s in d.statics if (":" in s[0]) == (af == "ipv6") and (s[2] if len(s) > 2 else "") == vrf]
                if rows:
                    L.append(ind + "address-family %s unicast" % af)
                    for row in rows:
                        L.append(ind + " %s %s" % (row[0], xr_if(row[1]) if isinstance(row[1], int) else row[1]))
                    L.append(ind + "!")
            if vrf:
                L.append(" !")
        L.append("!")
    if d.isis and d.isis.get("sr") is not None or d.locator or d.extra.get("xr_sr"):
        L.append("segment-routing")
        L += d.extra.get("xr_sr", [])
        if d.isis and d.isis.get("sr") is not None:
            L.append(" global-block 16000 23999")
        if d.mapping_server:
            L += [" mapping-server", "  prefix-sid-map", "   address-family ipv4"]
            L += ["    %s %d range %d" % m for m in d.mapping_server]
            L += ["   !", "  !", " !"]
        if d.locator:
            L += [" srv6", "  encapsulation", "   source-address %s" % d.lo6, "  !", "  locators",
                  "   locator MAIN", "    micro-segment behavior unode psp-usd", "    prefix %s" % d.locator,
                  "   !", "  !", " !"]
        L.append("!")
    if d.isis:
        s = d.isis
        L += ["router isis CORE", " is-type level-2-only", " net %s" % s["net"], " log adjacency changes"]
        if s.get("te"):
            # the SR-TE headend computes on-demand paths from the topology IS-IS hands it
            L.append(" distribute link-state")
        if s.get("flex"):
            for algo, metric in s["flex"]:
                L += [" flex-algo %d" % algo, "  metric-type %s" % metric, "  advertise-definition", " !"]
        if s.get("v4", True):
            L += [" address-family ipv4 unicast", "  metric-style wide"]
            if s.get("te"):
                L += ["  mpls traffic-eng level-2-only", "  mpls traffic-eng router-id Loopback0"]
            if s.get("sr") is not None:
                L.append("  segment-routing mpls")
            if s.get("mapping_rx"):
                L.append("  segment-routing prefix-sid-map advertise-local")
            for r in s.get("redist4", []):
                L.append("  redistribute %s" % r)
            L.append(" !")
        if s.get("v6") or s.get("srv6"):
            L += [" address-family ipv6 unicast", "  metric-style wide"]
            if s.get("srv6"):
                L += ["  segment-routing srv6", "   locator MAIN", "   !", "  !"]
            for r in s.get("redist6", []):
                L.append("  redistribute %s" % r)
            L.append(" !")
        L += [" interface Loopback0", "  passive"]
        if s.get("v4", True):
            L.append("  address-family ipv4 unicast")
            if s.get("sr") is not None:
                L.append("   prefix-sid index %d" % s["sr"])
                for algo, _ in s.get("flex", []):
                    L.append("   prefix-sid algorithm %d index %d" % (algo, s["sr"] + algo * 10))
            L.append("  !")
        if s.get("v6") or s.get("srv6"):
            L += ["  address-family ipv6 unicast", "  !"]
        L.append(" !")
        for i in d.ifs:
            if not i.isis:
                continue
            L += [" interface %s" % xr_if(i.eth, i.vlan)]
            if i.isis_passive:
                L.append("  passive")
            else:
                L.append("  point-to-point")
            if s.get("v4", True):
                L.append("  address-family ipv4 unicast")
                if i.metric:
                    L.append("   metric %d" % i.metric)
                if s.get("tilfa") and not i.isis_passive:
                    L += ["   fast-reroute per-prefix", "   fast-reroute per-prefix ti-lfa"]
                L.append("  !")
            if s.get("v6") or s.get("srv6"):
                L.append("  address-family ipv6 unicast")
                if s.get("tilfa") and s.get("srv6") and not i.isis_passive:
                    L += ["   fast-reroute per-prefix", "   fast-reroute per-prefix ti-lfa"]
                L.append("  !")
            L.append(" !")
        L.append("!")
    if d.ldp:
        L += ["mpls ldp", " router-id %s" % d.lo4]
        L += [" interface %s" % xr_if(i.eth, i.vlan) for i in d.ifs if i.ldp]
        L.append("!")
    if any(i.bgp_fwd for i in d.ifs):
        # MPLS on the interface to a directly connected eBGP LU / VPNv4 peer. XR does not
        # enable it on its own for a VRF interface (CsC), so labelled packets were dropped.
        L.append("mpls static")
        L += [" interface %s" % xr_if(i.eth, i.vlan) for i in d.ifs if i.bgp_fwd]
        L.append("!")
    if any(i.delay for i in d.ifs):
        L.append("performance-measurement")
        for i in d.ifs:
            if i.delay:
                L += [" interface %s" % xr_if(i.eth), "  delay-measurement", "   advertise-delay %d" % i.delay,
                      "  !", " !"]
        L.append("!")
    if d.bgp:
        L += _xr_bgp(d)
    L += d.extra.get("xr", [])
    return "\n".join(L + ["commit", "end"]) + "\n"


XR_AF = {"ipv4": "ipv4 unicast", "ipv6": "ipv6 unicast", "ipv4lu": "ipv4 labeled-unicast",
         "ipv6lu": "ipv6 labeled-unicast", "vpnv4": "vpnv4 unicast", "vpnv6": "vpnv6 unicast",
         "evpn": "l2vpn evpn"}


XR_ADDPATH = {
    # RR: keep and advertise every path; PE: install the second-best path as a PIC backup
    "send": ["route-policy ADDPATH-ALL", "  set path-selection all advertise", "end-policy", "!"],
    "recv": ["route-policy PIC-BACKUP", "  set path-selection backup 1 install", "end-policy", "!"],
}


def _xr_bgp(d):
    b = d.bgp
    ap = {n.addpath for n in b["nbrs"] if n.addpath}
    L = []
    for k in ("send", "recv"):
        if k in ap:
            L += XR_ADDPATH[k]
    L += ["router bgp %d" % b["asn"], " bgp router-id %s" % (d.lo4 or "10.255.255.%d" % (hash(d.name) % 250))]
    if b.get("cluster_id"):
        L.append(" bgp cluster-id %s" % b["cluster_id"])
    glob = [n for n in b["nbrs"] if not n.vrf]
    afs = []
    for n in glob:
        for af in n.afs:
            base = {"ipv4lu": "ipv4", "ipv6lu": "ipv6"}.get(af, af)
            if base not in afs:
                afs.append(base)
    for af in ("ipv4", "ipv6"):
        if b["net4" if af == "ipv4" else "net6"] or b["redist4" if af == "ipv4" else "redist6"]:
            if af not in afs:
                afs.insert(0, af)
    for af in ("ipv4", "ipv6", "vpnv4", "vpnv6", "evpn"):
        if af not in afs:
            continue
        L.append(" address-family %s" % XR_AF[af])
        if af == "ipv4":
            L += ["  network %s" % p for p in b["net4"]]
            L += ["  redistribute %s" % r for r in b["redist4"]]
            if b["alloc_lu"]:
                L.append("  allocate-label all")
        if af == "ipv6":
            L += ["  network %s" % p for p in b["net6"]]
            L += ["  redistribute %s" % r for r in b["redist6"]]
            if b["alloc_lu"]:
                L.append("  allocate-label all")
        if af in ("vpnv4", "vpnv6") and b["retain_rt"]:
            L.append("  retain route-target all")
        if af == "vpnv4" and ap:
            L += ["  additional-paths receive"]
            L += ["  additional-paths send", "  additional-paths selection route-policy ADDPATH-ALL"] if "send" in ap \
                else ["  additional-paths selection route-policy PIC-BACKUP"]
        L.append(" !")
    for n in glob:
        L += _xr_nbr(n, b["asn"], " ")
    for v in d.vrfs:
        L += [" vrf %s" % v.name, "  rd %s" % v.rd]
        for af, on in (("ipv4", v.v4), ("ipv6", v.v6)):
            if not on:
                continue
            L.append("  address-family %s unicast" % af)
            if v.srv6:
                if v.dual:
                    # a label as well as the SID: MPLS-only PEs can still reach this VRF
                    L += ["   mpls alloc enable", "   label mode per-vrf"]
                L += ["   segment-routing srv6", "    locator MAIN", "    alloc mode per-vrf", "   !"]
            elif not v.lu:
                # CsC needs a label per carrier loopback, so only plain VPNs share one
                L.append("   label mode per-vrf")
            if v.lu:
                L.append("   allocate-label all")
            L += ["   redistribute connected", "  !"]
        for n in b["nbrs"]:
            if n.vrf == v.name:
                L += _xr_nbr(n, b["asn"], "  ")
        L.append(" !")
    L.append("!")
    return L


def _xr_nbr(n, asn, ind):
    L = [ind + "neighbor %s" % n.ip, ind + " remote-as %d" % n.ras]
    if n.desc:
        L.append(ind + " description %s" % n.desc)
    if n.lo:
        L.append(ind + " update-source Loopback0")
    if n.multihop:
        L.append(ind + " ebgp-multihop 255")
    for af in n.afs:
        L.append(ind + " address-family %s" % XR_AF[af])
        if n.ras != asn:
            L += [ind + "  route-policy %s in" % ROUTE_POLICY, ind + "  route-policy %s out" % ROUTE_POLICY]
        if af in n.nhs:
            L.append(ind + "  next-hop-self")
        if af in n.nh_unchanged:
            L.append(ind + "  next-hop-unchanged")
        if af in n.rrc:
            L.append(ind + "  route-reflector-client")
        if n.as_override:
            L.append(ind + "  as-override")
        if n.allowas:
            L.append(ind + "  allowas-in")
        L += [ind + "  " + x for x in n.xr_af]
        L.append(ind + " !")
    L.append(ind + "!")
    return L


# --------------------------------------------------------------------------
# IOS-XE (c8000v)
# --------------------------------------------------------------------------

def xe_if(eth, vlan=None):
    return "GigabitEthernet%d%s" % (eth + 1, (".%d" % vlan) if vlan else "")


def _xe_loopbacks(d):
    L = ["interface Loopback0"]
    if d.lo4:
        L.append(" ip address %s 255.255.255.255" % d.lo4)
    if d.lo6:
        L.append(" ipv6 address %s/128" % d.lo6)
    if d.isis:
        L.append(" ip router isis CORE")
        if d.isis.get("v6"):
            L.append(" ipv6 router isis CORE")
    if d.pim:
        L.append(" ip pim sparse-mode")
    L.append("!")
    for k, a in enumerate(d.lo_extra, start=1):
        L += ["interface Loopback%d" % k, " ip address %s 255.255.255.255" % a]
        L += [" ip router isis CORE"] if d.isis else []
        L += [" ip pim sparse-mode"] if d.pim else []
        L.append("!")
    return L


def render_xe(d, mask):
    L = ["hostname %s" % d.name, "!", "no ip domain lookup", "ip cef", "ipv6 unicast-routing", "!"]
    for v in d.vrfs:
        L += ["vrf definition %s" % v.name, " rd %s" % v.rd, " !"]
        for af, on in (("ipv4", v.v4), ("ipv6", v.v6)):
            if on:
                L += [" address-family %s" % af]
                L += ["  route-target export %s" % v.rt, "  route-target import %s" % v.rt, " exit-address-family", " !"]
        L.append("!")
    if pim_on(d):
        L += ["ip multicast-routing distributed", "ip pim ssm default"]
        if d.pim and d.pim.get("rp"):
            g = ipaddress.ip_network(d.pim.get("asm", "224.0.0.0/4"))
            L += ["ip access-list standard ASM-GROUPS", " permit %s %s" % (g.network_address, g.hostmask),
                  "ip pim rp-address %s ASM-GROUPS" % d.pim["rp"]]
        L.append("!")
    if d.isis and d.isis.get("sr") is not None:
        L += ["segment-routing mpls", " global-block 16000 23999", " !", " connected-prefix-sid-map",
              "  address-family ipv4", "   %s/32 index %d range 1" % (d.lo4, d.isis["sr"]),
              "  exit-address-family", " !"]
        if d.mapping_server:
            L += [" mapping-server", "  prefix-sid-map", "   address-family ipv4"]
            L += ["    %s index %d range %d" % m for m in d.mapping_server]
            L += ["   exit-address-family", "  !", " !"]
        L.append("!")
    if d.ldp:
        L += ["mpls label protocol ldp", "mpls ldp router-id Loopback0 force", "!"]
    L += _xe_loopbacks(d)
    for i in d.ifs:
        if i.vlan:
            L += ["interface %s" % xe_if(i.eth), " mtu %d" % MTU, " no ip address", " no shutdown", "!"]
        L += ["interface %s" % xe_if(i.eth, i.vlan), " description to %s" % i.peer]
        if i.vlan:
            L.append(" encapsulation dot1Q %d" % i.vlan)
        if i.vrf:
            L.append(" vrf forwarding %s" % i.vrf)
        if not i.vlan:
            L.append(" mtu %d" % MTU)
        if i.v4:
            L.append(" ip address %s" % _v4mask(i.v4))
        if i.v6:
            L.append(" ipv6 address %s" % i.v6)
        if i.isis:
            L.append(" ip router isis CORE")
            if d.isis.get("v6"):
                L.append(" ipv6 router isis CORE")
            if not i.isis_passive:
                L.append(" isis network point-to-point")
            if i.metric:
                L.append(" isis metric %d level-2" % i.metric)
        if i.ldp:
            L.append(" mpls ip")
        if i.bgp_fwd:
            L.append(" mpls bgp forwarding")
        if i.pim:
            L.append(" ip pim sparse-mode")
        if i.igmp:
            L.append(" ip igmp version 3")
        L += [" no shutdown", "!"]
    if d.pim and d.pim.get("msdp"):
        # after the interfaces: a connect-source on a Loopback0 that does not exist yet is
        # dropped at boot, and the RPs then never exchange Source-Active messages
        L += ["ip msdp peer %s connect-source Loopback0" % p for p in d.pim["msdp"]]
        L += ["ip msdp originator-id Loopback0", "!"]
    for row in d.statics:
        pfx, via = row[0], row[1]
        n = ipaddress.ip_network(pfx)
        L.append("ip route %s%s %s %s" % (("vrf %s " % row[2]) if len(row) > 2 else "", n.network_address, n.netmask,
                                          xe_if(via) if isinstance(via, int) else via))
    if d.isis:
        s = d.isis
        L += ["router isis CORE", " net %s" % s["net"], " is-type level-2-only", " metric-style wide",
              " log-adjacency-changes"]
        if s.get("sr") is not None:
            L.append(" segment-routing mpls")
            if s.get("mapping_rx"):
                L.append(" segment-routing prefix-sid-map receive")
        L.append(" passive-interface Loopback0")
        L += [" passive-interface Loopback%d" % k for k in range(1, len(d.lo_extra) + 1)]
        L += [" passive-interface %s" % xe_if(i.eth, i.vlan) for i in d.ifs if i.isis and i.isis_passive]
        for r in s.get("redist4", []):
            L.append(" redistribute %s" % r)
        if s.get("v6"):
            L += [" address-family ipv6", "  multi-topology", " exit-address-family"]
        L.append("!")
    if d.bgp:
        L += _xe_bgp(d)
    L += d.extra.get("xe", [])
    L.append("end")
    return "\n".join(L) + "\n"


XE_AF = {"ipv4": "ipv4", "ipv6": "ipv6", "ipv4lu": "ipv4", "ipv6lu": "ipv6", "vpnv4": "vpnv4", "vpnv6": "vpnv6"}


def _xe_bgp(d):
    b = d.bgp
    L = ["router bgp %d" % b["asn"], " bgp router-id %s" % d.lo4, " bgp log-neighbor-changes",
         " no bgp default ipv4-unicast"]
    if b["retain_rt"]:
        L.append(" no bgp default route-target filter")
    if b.get("cluster_id"):
        L.append(" bgp cluster-id %s" % b["cluster_id"])
    glob = [n for n in b["nbrs"] if not n.vrf]
    for n in glob:
        L.append(" neighbor %s remote-as %d" % (n.ip, n.ras))
        if n.desc:
            L.append(" neighbor %s description %s" % (n.ip, n.desc))
        if n.lo:
            L.append(" neighbor %s update-source Loopback0" % n.ip)
        if n.multihop:
            L.append(" neighbor %s ebgp-multihop 255" % n.ip)
    L.append(" !")
    for af in ("ipv4", "ipv6", "vpnv4", "vpnv6"):
        nbrs = [(n, a) for n in glob for a in n.afs if XE_AF[a] == af]
        nets = b["net4"] if af == "ipv4" else b["net6"] if af == "ipv6" else []
        red = b["redist4"] if af == "ipv4" else b["redist6"] if af == "ipv6" else []
        if not nbrs and not nets and not red:
            continue
        L.append(" address-family %s" % af)
        # (no add-path send/receive here: IOS-XE 17.12 has neither under VPNv4 - only
        # `select backup` / `install`, so an XE PE keeps what the RRs' best paths give it)
        for p in nets:
            n = ipaddress.ip_network(p)
            L.append(("  network %s mask %s" % (n.network_address, n.netmask)) if n.version == 4
                     else "  network %s" % p)
        L += ["  redistribute %s" % r for r in red]
        for n, a in nbrs:
            L.append("  neighbor %s activate" % n.ip)
            if a in ("ipv4lu", "ipv6lu"):
                L.append("  neighbor %s send-label" % n.ip)
            if af in ("vpnv4", "vpnv6"):
                L.append("  neighbor %s send-community extended" % n.ip)

            if a in n.nhs:
                L.append("  neighbor %s next-hop-self" % n.ip)
            if a in n.nh_unchanged:
                L.append("  neighbor %s next-hop-unchanged" % n.ip)
            if a in n.rrc:
                L.append("  neighbor %s route-reflector-client" % n.ip)
        L += [" exit-address-family", " !"]
    for v in d.vrfs:
        for af, on in (("ipv4", v.v4), ("ipv6", v.v6)):
            if not on:
                continue
            L += [" address-family %s vrf %s" % (af, v.name), "  redistribute connected"]
            if b.get("pic"):
                L.append("  bgp additional-paths install")
            for n in b["nbrs"]:
                if n.vrf != v.name or (":" in n.ip) != (af == "ipv6"):
                    continue
                L += ["  neighbor %s remote-as %d" % (n.ip, n.ras), "  neighbor %s activate" % n.ip]
                if "ipv4lu" in n.afs or "ipv6lu" in n.afs:
                    L.append("  neighbor %s send-label" % n.ip)
                if n.as_override:
                    L.append("  neighbor %s as-override" % n.ip)
                if n.allowas:
                    L.append("  neighbor %s allowas-in" % n.ip)
            L += [" exit-address-family", " !"]
    L.append("!")
    return L


# --------------------------------------------------------------------------
# FRR
# --------------------------------------------------------------------------

FRR_ALL = ("zebra", "bgpd", "ospfd", "ospf6d", "ripd", "ripngd", "isisd", "pimd", "pim6d", "ldpd", "nhrpd",
           "eigrpd", "babeld", "sharpd", "staticd", "pbrd", "bfdd", "fabricd", "pathd")


def frr_daemons(d):
    on = {"zebra", "staticd", "mgmtd"}
    if d.isis:
        on.add("isisd")
    if d.bgp:
        on.add("bgpd")
    if d.ldp:
        on.add("ldpd")
    if pim_on(d):
        on.add("pimd")
    L = ["%s=%s" % (x, "yes" if x in on else "no") for x in FRR_ALL]
    L += ["vtysh_enable=yes", 'zebra_options="  -A 127.0.0.1 -s 90000000"']
    L += ['%s_options="  -A 127.0.0.1"' % x for x in FRR_ALL if x != "zebra"]
    L.append('frr_profile="traditional"')
    return "\n".join(L) + "\n"


def pim_on(d):
    return bool(d.pim or any(i.pim or i.igmp or i.joins for i in d.ifs))


def frr_ifname(i):
    return "eth%d%s" % (i.eth, (".%d" % i.vlan) if i.vlan else "")


FRR_AF = {"ipv4": "ipv4 unicast", "ipv6": "ipv6 unicast", "ipv4lu": "ipv4 labeled-unicast",
          "ipv6lu": "ipv6 labeled-unicast", "vpnv4": "ipv4 vpn", "vpnv6": "ipv6 vpn"}


def render_frr(d):
    L = ["frr version 10", "frr defaults traditional", "hostname %s" % d.name, "log stdout informational",
         "service integrated-vtysh-config", "!"]
    for v in d.vrfs:
        L += ["vrf %s" % v.name, "exit-vrf", "!"]
    L.append("interface lo")
    if d.lo4:
        L.append(" ip address %s/32" % d.lo4)
    if d.lo6:
        L.append(" ipv6 address %s/128" % d.lo6)
    L += [" ip address %s/32" % a for a in d.lo_extra]
    if d.pim:
        L.append(" ip pim")
    if d.isis:
        L += [" ip router isis CORE"] if d.isis.get("v4", True) else []
        L += [" ipv6 router isis CORE"] if (d.isis.get("v6") or d.isis.get("srv6")) else []
        L.append(" isis passive")
    L += ["exit", "!"]
    for i in d.ifs:
        L.append("interface %s%s" % (frr_ifname(i), (" vrf %s" % i.vrf) if i.vrf else ""))
        L.append(" description to %s" % i.peer)
        if i.v4:
            L.append(" ip address %s" % i.v4)
        if i.v6:
            L.append(" ipv6 address %s" % i.v6)
        if i.isis:
            if d.isis.get("v4", True):
                L.append(" ip router isis CORE")
            if d.isis.get("v6") or d.isis.get("srv6"):
                L.append(" ipv6 router isis CORE")
            L.append(" isis passive" if i.isis_passive else " isis network point-to-point")
            if i.metric:
                L.append(" isis metric %d" % i.metric)
        if i.delay:
            L += [" link-params", "  enable", "  delay %d" % i.delay, " exit-link-params"]
        if i.pim:
            L.append(" ip pim")
        if i.igmp or i.joins:
            # FRR refuses join-group on an interface without IGMP ("multicast not enabled")
            L += [" ip igmp", " ip igmp version 3"]
        for g, src in i.joins:
            L.append(" ip igmp join-group %s%s" % (g, (" " + src) if src else ""))
        L += ["exit", "!"]
    for row in d.statics:
        pfx, via = row[0], row[1]
        L.append("%s route %s %s%s" % ("ipv6" if ":" in pfx else "ip", pfx, ("eth%d" % via) if isinstance(via, int) else via,
                                       (" vrf %s" % row[2]) if len(row) > 2 else ""))
    if d.locator:
        L += ["segment-routing", " srv6", "  locators", "   locator MAIN",
              "    prefix %s block-len 32 node-len 16 func-bits 16" % d.locator, "    behavior usid",
              "   exit", "   !", "  exit", "  !", " exit", " !", "exit", "!"]
    if d.isis:
        s = d.isis
        L += ["router isis CORE", " is-type level-2-only", " net %s" % s["net"], " metric-style wide",
              " log-adjacency-changes"]
        if s.get("sr") is not None:
            L += [" segment-routing on", " segment-routing global-block 16000 23999",
                  " segment-routing prefix %s/32 index %d" % (d.lo4, s["sr"])]
            for algo, _ in s.get("flex", []):
                L.append(" segment-routing prefix %s/32 algorithm %d index %d" % (d.lo4, algo, s["sr"] + algo * 10))
        if s.get("srv6"):
            L += [" segment-routing srv6", "  locator MAIN", " exit"]
        if s.get("flex"):
            for algo, metric in s["flex"]:
                L += [" flex-algo %d" % algo, "  advertise-definition",
                      "  metric-type %s" % {"delay": "min-delay"}.get(metric, metric), " exit"]
        for r in s.get("redist4", []):
            L.append(" redistribute ipv4 %s level-2" % r)
        for r in s.get("redist6", []):
            L.append(" redistribute ipv6 %s level-2" % r)
        L += ["exit", "!"]
    if d.pim and (d.pim.get("rp") or d.pim.get("msdp")):
        L.append("router pim")
        if d.pim.get("rp"):
            L.append(" rp %s %s" % (d.pim["rp"], d.pim.get("asm", "224.0.0.0/4")))
        for peer in d.pim.get("msdp", []):
            L.append(" msdp peer %s source %s" % (peer, d.lo4))
        if d.pim.get("msdp"):
            L.append(" msdp originator-id %s" % d.lo4)
        L += ["exit", "!"]
    if d.ldp:
        L += ["mpls ldp", " router-id %s" % d.lo4, " !", " address-family ipv4",
              "  discovery transport-address %s" % d.lo4]
        L += ["  interface %s" % frr_ifname(i) for i in d.ifs if i.ldp]
        L += [" exit-address-family", " !", "exit", "!"]
    if d.bgp:
        L += _frr_bgp(d)
    L += d.extra.get("frr", [])
    L += ["line vty", "!"]
    return "\n".join(L) + "\n"


def _frr_bgp(d):
    b = d.bgp
    L = ["router bgp %d" % b["asn"], " bgp router-id %s" % d.lo4, " bgp log-neighbor-changes",
         " no bgp ebgp-requires-policy", " no bgp default ipv4-unicast", " no bgp network import-check"]
    if b.get("cluster_id"):
        L.append(" bgp cluster-id %s" % b["cluster_id"])
    glob = [n for n in b["nbrs"] if not n.vrf]
    for n in glob:
        L.append(" neighbor %s remote-as %d" % (n.ip, n.ras))
        if n.desc:
            L.append(" neighbor %s description %s" % (n.ip, n.desc))
        if n.lo:
            L.append(" neighbor %s update-source lo" % n.ip)
        if n.multihop:
            L.append(" neighbor %s ebgp-multihop 255" % n.ip)
        if ":" in n.ip and any(a in ("ipv4", "vpnv4", "ipv4lu") for a in n.afs):
            L.append(" neighbor %s capability extended-nexthop" % n.ip)
    if d.locator and any(v.srv6 for v in d.vrfs):
        L += [" !", " segment-routing srv6", "  locator MAIN", " exit"]
    L.append(" !")
    for af in ("ipv4", "ipv4lu", "ipv6", "ipv6lu", "vpnv4", "vpnv6"):
        nbrs = [n for n in glob if af in n.afs]
        nets = b["net4"] if af in ("ipv4", "ipv4lu") else b["net6"] if af in ("ipv6", "ipv6lu") else []
        red = b["redist4"] if af == "ipv4" else b["redist6"] if af == "ipv6" else []
        if af in ("ipv4lu", "ipv6lu") and not nbrs:
            nets = []
        if not nbrs and not nets and not red:
            continue
        L.append(" address-family %s" % FRR_AF[af])
        L += ["  network %s" % p for p in nets]
        L += ["  redistribute %s" % r for r in red]
        for n in nbrs:
            L.append("  neighbor %s activate" % n.ip)
            if af in n.nhs:
                L.append("  neighbor %s next-hop-self" % n.ip)
            if af in n.nh_unchanged:
                L.append("  neighbor %s attribute-unchanged next-hop" % n.ip)
            if af in n.rrc:
                L.append("  neighbor %s route-reflector-client" % n.ip)
            if n.allowas:
                L.append("  neighbor %s allowas-in" % n.ip)
            if n.addpath == "send":
                L.append("  neighbor %s addpath-tx-all-paths" % n.ip)
        L += [" exit-address-family", " !"]
    L += ["exit", "!"]
    for v in d.vrfs:
        L += ["router bgp %d vrf %s" % (b["asn"], v.name), " bgp router-id %s" % d.lo4,
              " no bgp ebgp-requires-policy", " no bgp default ipv4-unicast"]
        mine = [n for n in b["nbrs"] if n.vrf == v.name]
        for n in mine:
            L.append(" neighbor %s remote-as %d" % (n.ip, n.ras))
        L.append(" !")
        for af, on in (("ipv4", v.v4), ("ipv6", v.v6)):
            if not on:
                continue
            L += [" address-family %s unicast" % af, "  redistribute connected"]
            for n in mine:
                if (":" in n.ip) != (af == "ipv6"):
                    continue
                if not (v.lu and af == "ipv4"):
                    L.append("  neighbor %s activate" % n.ip)
                if n.as_override:
                    L.append("  neighbor %s as-override" % n.ip)
                if n.allowas:
                    L.append("  neighbor %s allowas-in" % n.ip)
            L.append("  sid vpn export auto" if v.srv6 else "  label vpn export auto")
            L += ["  rd vpn export %s" % v.rd, "  rt vpn both %s" % v.rt, "  export vpn", "  import vpn",
                  " exit-address-family", " !"]
            if v.lu and af == "ipv4":
                L.append(" address-family ipv4 labeled-unicast")
                L += ["  neighbor %s activate" % n.ip for n in mine if ":" not in n.ip]
                L += [" exit-address-family", " !"]
        L += ["exit", "!"]
    return L


def frr_exec(d):
    """What frr.conf cannot say: MTU, VLAN and VRF devices, MPLS input."""
    cmds = ["touch /etc/frr/vtysh.conf"]
    for eth in sorted({i.eth for i in d.ifs}):
        cmds.append("ip link set eth%d mtu %d" % (eth, MTU + (4 if any(i.vlan for i in d.ifs if i.eth == eth) else 0)))
    for i in d.ifs:
        if i.vlan:
            cmds += ["ip link add link eth%d name eth%d.%d type vlan id %d" % (i.eth, i.eth, i.vlan, i.vlan),
                     "ip link set eth%d.%d up" % (i.eth, i.vlan)]
    for v in d.vrfs:
        cmds += ["ip link set %s master %s" % (frr_ifname(i), v.name) for i in d.ifs if i.vrf == v.name]
    if mpls_on(d):
        for i in d.ifs:
            cmds.append("sysctl -w net.mpls.conf.%s.input=1" % frr_ifname(i).replace(".", "/"))
    if d.pim and d.pim.get("rp"):
        cmds.append("sh /etc/frr/rpwatch.sh")
    return cmds


def frr_rpwatch(d):
    """FRR 10.4 pimd can register the RP for next-hop tracking before IS-IS has a route to it
    and then miss the update (seen with ECMP to the RP): rp-info stays at OIF "Unknown" and
    no (*,G) join is ever sent. Re-applying the RP fixes it, so this watchdog does that
    whenever the RP is unresolved."""
    rp = "rp %s %s" % (d.pim["rp"], d.pim.get("asm", "224.0.0.0/4"))
    return ("#!/bin/sh\n# RP next-hop watchdog for FRR pimd - see the comment in scenarios.frr_rpwatch\n"
            "setsid sh -c 'while sleep 30; do vtysh -c \"show ip pim rp-info\" | grep -q Unknown && "
            "vtysh -c \"conf t\" -c \"router pim\" -c \"no %s\" -c \"%s\" >/dev/null; done' "
            ">/dev/null 2>&1 </dev/null &\n" % (rp, rp))


def frr_prestart(d):
    """Devices zebra must see at startup: VRFs (so SIDs/labels bind to them) and
    sr0, the dummy FRR installs local SRv6 SIDs on. Returns a clab cmd: or None."""
    pre = []
    for k, v in enumerate(d.vrfs, start=1):
        pre += ["ip link add %s type vrf table %d" % (v.name, 1000 + k), "ip link set %s up" % v.name]
    if d.locator:
        # End.DT4/DT6 are refused by the kernel unless VRF strict mode is on
        pre += ["sysctl -w net.vrf.strict_mode=1", "ip link add sr0 type dummy", "ip link set sr0 up"]
    if not pre:
        return None
    return "sh -c '%s; exec /usr/lib/frr/docker-start'" % "; ".join(pre)


def mpls_on(d):
    return bool((d.isis and d.isis.get("sr") is not None) or d.ldp or any(i.mpls or i.bgp_fwd for i in d.ifs)
                or any(not v.srv6 for v in d.vrfs) or (d.bgp and any(
                    a in ("ipv4lu", "ipv6lu", "vpnv4", "vpnv6") for n in d.bgp["nbrs"] for a in n.afs)))


# --------------------------------------------------------------------------
# SR Linux - customer edge (addresses, a loopback, eBGP to the PE)
# --------------------------------------------------------------------------

def render_srl(d):
    L = []
    for i in d.ifs:
        p = "set / interface ethernet-1/%d" % i.eth
        L += ["%s admin-state enable" % p, '%s description "to %s"' % (p, i.peer),
              "%s subinterface 0 admin-state enable" % p]
        if i.v4:
            L += ["%s subinterface 0 ipv4 admin-state enable" % p, "%s subinterface 0 ipv4 address %s" % (p, i.v4)]
        if i.v6:
            L += ["%s subinterface 0 ipv6 admin-state enable" % p, "%s subinterface 0 ipv6 address %s" % (p, i.v6)]
    ni = "set / network-instance default"
    L += ["set / interface system0 admin-state enable",
          "set / interface system0 subinterface 0 ipv4 admin-state enable",
          "set / interface system0 subinterface 0 ipv4 address %s/32" % d.lo4]
    if d.lo6:
        L += ["set / interface system0 subinterface 0 ipv6 admin-state enable",
              "set / interface system0 subinterface 0 ipv6 address %s/128" % d.lo6]
    L += ["%s type default" % ni, "%s admin-state enable" % ni, "%s router-id %s" % (ni, d.lo4),
          "%s interface system0.0" % ni]
    L += ["%s interface ethernet-1/%d.0" % (ni, i.eth) for i in d.ifs]
    b = d.bgp
    p = "%s protocols bgp" % ni
    L += ["%s admin-state enable" % p, "%s autonomous-system %d" % (p, b["asn"]), "%s router-id %s" % (p, d.lo4),
          "%s afi-safi ipv4-unicast admin-state enable" % p,
          "%s ebgp-default-policy import-reject-all false" % p,
          "%s ebgp-default-policy export-reject-all false" % p]
    if d.lo6:
        L.append("%s afi-safi ipv6-unicast admin-state enable" % p)
    for n in b["nbrs"]:
        L += ["%s group ebgp-%d peer-as %d" % (p, n.ras, n.ras),
              "%s neighbor %s peer-group ebgp-%d" % (p, n.ip, n.ras)]
    rp = "set / routing-policy"
    L += ["%s prefix-set LOCAL prefix %s/32 mask-length-range exact" % (rp, d.lo4)]
    if d.lo6:
        L.append("%s prefix-set LOCAL prefix %s/128 mask-length-range exact" % (rp, d.lo6))
    L += ["%s policy EXPORT-LOCAL statement 10 match prefix prefix-set LOCAL" % rp,
          "%s policy EXPORT-LOCAL statement 10 action policy-result accept" % rp,
          "%s policy EXPORT-LOCAL default-action policy-result next-policy" % rp,
          "%s export-policy [ EXPORT-LOCAL ]" % p]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# lab assembly: mgmt addresses, ports, topology file
# --------------------------------------------------------------------------

class Files(dict):
    """The lab's files; .lab is the Lab they came from."""
    lab = None


class Lab:
    def __init__(self, name, mgmt, images, title):
        self.name, self.images, self.title = name, images, title
        self.net = ipaddress.ip_network(mgmt, strict=False)
        self._hosts = self.net.hosts()
        next(self._hosts)
        self.mask = str(self.net.netmask)
        self.devs = {}
        self.links = []                  # (a, pa, b, pb, note)
        self._eth = {}
        self._subnet = {}

    def dev(self, name, plat, x, y, **k):
        d = Dev(name, plat, x, y, **k)
        d.mgmt = str(next(self._hosts))
        self.devs[name] = d
        return d

    def link(self, a, b, note=""):
        pa = self._eth[a] = self._eth.get(a, 0) + 1
        pb = self._eth[b] = self._eth.get(b, 0) + 1
        self.links.append((a, pa, b, pb, note))
        return pa, pb

    def p2p(self, a, b, v4net=None, v6net=None, note="", **kw):
        """Link a<->b and address both ends: a gets .1 / ::1, b gets .2 / ::2."""
        pa, pb = self.link(a, b, note)
        da, db = self.devs[a], self.devs[b]
        net4 = ipaddress.ip_network(v4net) if v4net else None
        net6 = ipaddress.ip_network(v6net) if v6net else None
        ka = dict(kw.get("end_a", {}), **{k: v for k, v in kw.items() if k not in ("end_a", "end_b")})
        kb = dict(kw.get("end_b", {}), **{k: v for k, v in kw.items() if k not in ("end_a", "end_b")})
        ia = da.add_if(pa, b, v4="%s/%d" % (net4[1], net4.prefixlen) if net4 else None,
                       v6="%s/%d" % (net6[1], net6.prefixlen) if net6 else None, **ka)
        ib = db.add_if(pb, a, v4="%s/%d" % (net4[2], net4.prefixlen) if net4 else None,
                       v6="%s/%d" % (net6[2], net6.prefixlen) if net6 else None, **kb)
        return ia, ib

    def ram_mb(self):
        return sum(RAM[d.plat] for d in self.devs.values())

    def files(self, readme):
        out = Files()
        out.lab = self
        plats = sorted({d.plat for d in self.devs.values()}, key=lambda p: list(KIND).index(p))
        y = ["# %s" % self.title,
             "# generated by the containerlab dashboard catalogue (scenarios.py) - see README.md",
             "", "name: %s" % self.name, "", "mgmt:", "  network: %s-mgmt" % self.name,
             "  ipv4-subnet: %s" % self.net, "", "topology:", "  kinds:"]
        for p in plats:
            k = KIND[p]
            y += ["    %s:" % k, "      image: %s" % self.images[IMAGE_SLOT[p]]]
            if ENV.get(k):
                y.append("      env:")
                y += ['        %s: "%s"' % kv for kv in ENV[k].items()]
        y += ["", "  nodes:"]
        for d in self.devs.values():
            y += ["    %s:" % d.name, "      kind: %s" % KIND[d.plat], "      mgmt-ipv4: %s" % d.mgmt]
            if d.plat in ("xr", "xe"):
                y.append("      startup-config: configs/%s.cfg" % d.name)
                out["configs/%s.cfg" % d.name] = render_xr(d, self.mask) if d.plat == "xr" else render_xe(d, self.mask)
            elif d.plat == "srl":
                y.append("      startup-config: configs/%s.cli" % d.name)
                out["configs/%s.cli" % d.name] = render_srl(d)
            else:
                pre = frr_prestart(d)
                if pre:
                    y.append('      cmd: "%s"' % pre)
                y += ["      binds:", "        - configs/%s.daemons:/etc/frr/daemons" % d.name,
                      "        - configs/%s.frr.conf:/etc/frr/frr.conf" % d.name]
                if d.pim and d.pim.get("rp"):
                    y.append("        - configs/%s.rpwatch.sh:/etc/frr/rpwatch.sh" % d.name)
                    out["configs/%s.rpwatch.sh" % d.name] = frr_rpwatch(d)
                y += [
                      "      sysctls:", "        net.ipv4.ip_forward: 1",
                      "        net.ipv6.conf.all.forwarding: 1", "        net.ipv6.conf.all.disable_ipv6: 0"]
                if mpls_on(d):
                    y.append("        net.mpls.platform_labels: 1048575")
                if d.locator:
                    y += ["        net.ipv6.conf.all.seg6_enabled: 1", "        net.ipv4.conf.all.rp_filter: 0"]
                if d.vrfs:
                    y += ["        net.ipv4.tcp_l3mdev_accept: 1", "        net.ipv4.udp_l3mdev_accept: 1"]
                if any(i.joins for i in d.ifs):
                    # answer echo requests sent to a joined group - the receiver check in the README
                    y.append("        net.ipv4.icmp_echo_ignore_broadcasts: 0")
                y.append("      exec:")
                y += ["        - %s" % (json.dumps(c) if any(ch in c for ch in "\"'&:#") else c) for c in frr_exec(d)]
                out["configs/%s.frr.conf" % d.name] = render_frr(d)
                out["configs/%s.daemons" % d.name] = frr_daemons(d)
            y += ["      labels:", '        builder-pos: "%d,%d"' % (d.x, d.y), '        graph-icon: "%s"' % d.icon]
            if d.plat == "frr":
                y.append('        builder-kind: "frr"')
            if d.role:
                y.append('        scenario-role: "%s"' % d.role)
        y += ["", "  links:"]
        for a, pa, b, pb, note in self.links:
            ea = ("ethernet-1/%d" if self.devs[a].plat == "srl" else "eth%d") % pa
            eb = ("ethernet-1/%d" if self.devs[b].plat == "srl" else "eth%d") % pb
            y.append('    - endpoints: ["%s:%s", "%s:%s"]%s' % (a, ea.replace("ethernet-1/", "e1-"), b,
                                                               eb.replace("ethernet-1/", "e1-"), ("   # " + note) if note else ""))
        out["%s.clab.yml" % self.name] = "\n".join(y) + "\n"
        out["README.md"] = readme
        return out


# --------------------------------------------------------------------------
# building blocks shared by the scenarios
# --------------------------------------------------------------------------

# vrnetlab's QEMU nodes (c8000v, csr1000v, n9kv) use 10.0.0.0/24 inside the container for their
# management NIC (Gi1 = 10.0.0.15, gateway 10.0.0.2), so loopbacks there collide with it. The
# newer single-AS scenarios therefore number loopbacks from LO_NET.
LO_NET = "10.255.0"


def core_router(lab, name, plat, x, y, asn, n, role, sr=True, srv6=False, v6=False, tilfa=False, ldp=False,
                sid_base=None, lo_base=None):
    """A provider router: loopback, IS-IS, SR-MPLS SID or SRv6 locator."""
    d = lab.dev(name, plat, x, y, role=role, asn=asn)
    a = asn % 100                                       # 65001 -> 1
    sid = (sid_base if sid_base is not None else a * 10) + n
    d.lo4 = "%s.%d" % (lo_base or "10.%d.0" % a, n)
    if srv6 or v6:
        d.lo6 = "2001:db8:%d::%d" % (a, n)
    d.isis = {"net": isis_net(sid), "sr": sid if (sr and not srv6) else None, "srv6": srv6,
              "v4": not srv6, "v6": v6 and not srv6, "tilfa": tilfa}
    if srv6:
        d.locator = "fc00:0:%d::/48" % sid
    d.ldp = ldp
    d.sid = sid
    return d


def core_link(lab, a, b, k, asn, srv6=False, ldp=False):
    """An intra-AS core link: IS-IS, plus v4 (MPLS) or v6 (SRv6) addressing."""
    x = asn % 100
    if srv6:
        return lab.p2p(a, b, v6net="2001:db8:%d:%d::/64" % (x, k), note="AS%d core" % asn, isis=True)
    return lab.p2p(a, b, v4net="10.%d.%d.0/30" % (x, 100 + k), note="AS%d core" % asn, isis=True,
                   mpls=True, ldp=ldp)


def customer(lab, name, plat, x, y, asn, idx, pe, vrf, v6=False, vlan=None, pe_asn=None):
    """A CE with a loopback, linked to its PE, eBGP in the PE's VRF."""
    ce = lab.dev(name, plat, x, y, role="CE", asn=asn, icon="router")
    ce.lo4 = "192.168.%d.1" % (100 + idx)
    if v6:
        ce.lo6 = "2001:db8:c:%d::1" % idx
    ia, ib = lab.p2p(pe, name, v4net="172.16.%d.0/30" % idx, v6net=("2001:db8:16:%d::/64" % idx) if v6 else None,
                     note="%s site %s" % (vrf, name), end_a={"vrf": vrf})
    p = lab.devs[pe]
    ce.bgp_init()
    ce.nbr(_ip(ia.v4), pe_asn or p.asn, ["ipv4"], desc=pe)
    ce.bgp["net4"].append("%s/32" % ce.lo4)
    if v6:
        ce.nbr(_ip(ia.v6), pe_asn or p.asn, ["ipv6"], desc=pe)
        ce.bgp["net6"].append("%s/128" % ce.lo6)
    p.nbr(_ip(ib.v4), asn, ["ipv4"], desc=name, vrf=vrf)
    if v6:
        p.nbr(_ip(ib.v6), asn, ["ipv6"], desc=name, vrf=vrf)
    return ce


def vrf_a(asn, srv6=False, v6=False):
    return Vrf("CUST-A", "%d:100" % asn, "65001:100", v4=True, v6=v6, srv6=srv6)


# --------------------------------------------------------------------------
# Inter-AS option A / B / C over SR-MPLS
# --------------------------------------------------------------------------
#
#   ce1 --- pe1 --- p1 --- asbr1 ====== asbr2 --- p2 --- pe2 --- ce2
#           \_______ AS 65001 ______/        \______ AS 65002 _____/
#
def group_interas(node):
    return "ce" if node.startswith("ce") else "as" + node[-1]


def plat_of(P, node, group=group_interas):
    """Preset lookup: the exact node name wins, else the node's group."""
    return P.get(node) or P.get(group(node))


def inter_as_mpls(option, name, images, mgmt, preset):
    P = PRESETS_INTERAS[preset]
    lab = Lab(name, mgmt, images, "Inter-AS option %s over SR-MPLS - %s" % (option, P["label"]))
    ases = [(65001, 0), (65002, 1)]
    for asn, side in ases:
        s = "%d" % (side + 1)
        xo = 0 if side == 0 else 520
        core_router(lab, "pe" + s, plat_of(P, "pe" + s), xo + (0 if side == 0 else 240), 140, asn, 1, "PE")
        core_router(lab, "p" + s, plat_of(P, "p" + s), xo + 120, 40, asn, 2, "P")
        core_router(lab, "asbr" + s, plat_of(P, "asbr" + s), xo + (240 if side == 0 else 0), 140, asn, 3, "ASBR")
    for asn, side in ases:
        s = "%d" % (side + 1)
        core_link(lab, "pe" + s, "p" + s, 1, asn)
        core_link(lab, "p" + s, "asbr" + s, 2, asn)
    a1, a2, pe1, pe2 = (lab.devs[n] for n in ("asbr1", "asbr2", "pe1", "pe2"))
    for pe in (pe1, pe2):
        pe.vrfs.append(vrf_a(pe.asn))
    customer(lab, "ce1", P["ce1"], -160, 140, 65101, 1, "pe1", "CUST-A")
    customer(lab, "ce2", P["ce2"], 920, 140, 65102, 2, "pe2", "CUST-A")

    if option == "A":
        # back-to-back VRFs: one dot1q sub-interface per VPN, plain eBGP inside it
        a1.vrfs.append(vrf_a(65001))
        a2.vrfs.append(vrf_a(65002))
        ia, ib = lab.p2p("asbr1", "asbr2", v4net="10.12.100.0/30", note="inter-AS link, VLAN 100 = CUST-A",
                         vrf="CUST-A", vlan=100)
        a1.nbr(_ip(ib.v4), 65002, ["ipv4"], desc="asbr2", vrf="CUST-A")
        a2.nbr(_ip(ia.v4), 65001, ["ipv4"], desc="asbr1", vrf="CUST-A")
        for pe, asbr in ((pe1, a1), (pe2, a2)):
            pe.nbr(asbr.lo4, pe.asn, ["vpnv4"], desc=asbr.name, lo=True)
            asbr.nbr(pe.lo4, pe.asn, ["vpnv4"], desc=pe.name, lo=True)
    elif option == "B":
        # eBGP VPNv4 between the ASBRs; they keep every VPN route and rewrite the next hop
        ia, ib = lab.p2p("asbr1", "asbr2", v4net="10.12.0.0/30", note="inter-AS link, eBGP VPNv4",
                         bgp_fwd=True)
        for me, peer_if, other, mine_if in ((a1, ib, a2, ia), (a2, ia, a1, ib)):
            me.bgp_init()["retain_rt"] = True
            me.nbr(_ip(peer_if.v4), other.asn, ["vpnv4"], desc=other.name)
            me.statics.append(("%s/32" % _ip(peer_if.v4), mine_if.eth))
        for pe, asbr in ((pe1, a1), (pe2, a2)):
            pe.nbr(asbr.lo4, pe.asn, ["vpnv4"], desc=asbr.name, lo=True)
            asbr.nbr(pe.lo4, pe.asn, ["vpnv4"], desc=pe.name, lo=True, nhs=["vpnv4"])
    else:
        # option C: ASBRs trade PE loopbacks as labelled unicast, the PEs peer VPNv4 multihop
        ia, ib = lab.p2p("asbr1", "asbr2", v4net="10.12.0.0/30", note="inter-AS link, eBGP IPv4 LU",
                         bgp_fwd=True)
        # each ASBR originates its own PE's loopback into BGP-LU (network + allocate-label:
        # the label swaps onto the SR prefix-SID towards the PE) and relays the other AS's
        # to its PE with next-hop-self. An iBGP-learned LU route is not enough: when IS-IS
        # holds the best path, neither XR nor FRR programs a forwarding entry for its label.
        for me, peer_if, other, mine_if, pe in ((a1, ib, a2, ia, pe1), (a2, ia, a1, ib, pe2)):
            me.bgp_init()["alloc_lu"] = True
            me.bgp["net4"].append("%s/32" % pe.lo4)
            me.nbr(_ip(peer_if.v4), other.asn, ["ipv4lu"], desc=other.name)
            me.statics.append(("%s/32" % _ip(peer_if.v4), mine_if.eth))
            me.nbr(pe.lo4, pe.asn, ["ipv4lu"], desc=pe.name, lo=True, nhs=["ipv4lu"])
            pe.nbr(me.lo4, pe.asn, ["ipv4lu"], desc=me.name, lo=True)
            pe.bgp["alloc_lu"] = True
        pe1.nbr(pe2.lo4, 65002, ["vpnv4"], desc="pe2 (multihop)", lo=True, multihop=True)
        pe2.nbr(pe1.lo4, 65001, ["vpnv4"], desc="pe1 (multihop)", lo=True, multihop=True)
    return lab.files(README_INTERAS[option].format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


def _gb(mb):
    return "%.0f GB" % (mb / 1024.0) if mb >= 1024 else "%d MB" % mb


# --------------------------------------------------------------------------
# presets - platform per role. Only verified combinations are listed.
# --------------------------------------------------------------------------

PRESETS_INTERAS = {
    "frr": {"label": "FRR everywhere", "as1": "frr", "as2": "frr", "ce1": "frr", "ce2": "frr"},
    "xr-asbr": {"label": "IOS-XR ASBRs, FRR PEs, Ps and CEs", "as1": "frr", "as2": "frr",
                "asbr1": "xr", "asbr2": "xr", "ce1": "frr", "ce2": "frr"},
    "xr": {"label": "IOS-XR provider, FRR CEs", "as1": "xr", "as2": "xr", "ce1": "frr", "ce2": "frr"},
    "xe-xr": {"label": "AS 65001 IOS-XE, AS 65002 IOS-XR, SR Linux + FRR CEs",
              "as1": "xe", "as2": "xr", "ce1": "srl", "ce2": "frr"},
}


def preset_counts(presets, preset, nodes):
    """Images needed by a preset: {image slot: node count}."""
    c = {}
    for n in nodes:
        p = plat_of(presets[preset], n)
        c[IMAGE_SLOT[p]] = c.get(IMAGE_SLOT[p], 0) + 1
    return c


README_INTERAS = {
"A": """# {name} - Inter-AS option A (back-to-back VRF) over SR-MPLS

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 --- p1 --- asbr1 ==VLAN 100== asbr2 --- p2 --- pe2 --- ce2
  AS65101   \\_____ AS 65001 _____/          \\_____ AS 65002 _____/   AS65102
```

Each AS is a self-contained SR-MPLS L3VPN (IS-IS L2, SRGB 16000-23999, SID index
= 10 x AS digit + node: pe1 11, p1 12, asbr1 13, pe2 21, p2 22, asbr2 23).
The ASBRs treat each other as a CE: one dot1q sub-interface per VPN (VLAN 100 =
CUST-A) with its own VRF and a plain eBGP IPv4 session inside it. No labels
cross the AS boundary - the simplest option and the least scalable one (one
sub-interface, VRF and session per VPN on every ASBR pair).

| Node | Loopback | Role |
|---|---|---|
| pe1 / p1 / asbr1 | 10.1.0.1 / .2 / .3 | AS 65001 |
| pe2 / p2 / asbr2 | 10.2.0.1 / .2 / .3 | AS 65002 |
| ce1 / ce2 | 192.168.101.1 / 192.168.102.1 | CUST-A sites |

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1` - end to end through both VPNs
- asbr1: `show bgp vrf CUST-A` - ce2's prefix arrives as plain IPv4 from asbr2
- pe1: `show bgp vpnv4 unicast` - the same prefix, re-originated by asbr1 with a new VPN label
- capture the asbr1-asbr2 link: unlabelled IPv4 in VLAN 100
""",
"B": """# {name} - Inter-AS option B (eBGP VPNv4 between ASBRs) over SR-MPLS

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 --- p1 --- asbr1 ======== asbr2 --- p2 --- pe2 --- ce2
  AS65101   \\_____ AS 65001 _____/  eBGP  \\_____ AS 65002 _____/   AS65102
                                    VPNv4
```

Inside each AS an SR-MPLS core (IS-IS, SID index 11-13 / 21-23) and iBGP VPNv4
PE <-> ASBR. Between the ASBRs a single eBGP **VPNv4** session carries every
VPN route: the ASBRs keep routes for VRFs they do not have (`retain
route-target all` / `no bgp default route-target filter`), set next-hop-self
towards their PE and allocate a new VPN label per route, so the label is
swapped at each ASBR. One session for all VPNs, but every VPN route lives on
the ASBRs.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- asbr1: `show bgp vpnv4 unicast` - ce2's route with RD 65002:100, next hop asbr2
- pe1: `show bgp vpnv4 unicast rd 65002:100 192.168.102.1/32` - next hop asbr1, label assigned by asbr1
- capture asbr1-asbr2: a single VPN label (asbr2's), no transport label on the inter-AS link
""",
"C": """# {name} - Inter-AS option C (multihop VPNv4, labelled-unicast loopbacks) over SR-MPLS

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 --- p1 --- asbr1 ======== asbr2 --- p2 --- pe2 --- ce2
            \\                  eBGP IPv4-LU                 /
             \\_________ multihop eBGP VPNv4 (pe1 <-> pe2) __/
```

The ASBRs only exchange **PE loopbacks as BGP labelled unicast** (eBGP IPv4-LU,
then iBGP LU to their own PE with next-hop-self). The PEs peer **VPNv4 directly**
with each other over a multihop eBGP session and keep the next hop - so VPN
routes never touch the ASBRs. A packet from pe1 carries three labels: pe1 ->
asbr1 SR transport, the BGP-LU label for pe2's loopback, and pe2's VPN label.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- pe1: `show bgp vpnv4 unicast` - ce2's prefix with next hop **10.2.0.1** (pe2 itself)
- pe1: `show bgp ipv4 labeled-unicast` / `show bgp ipv4 unicast labels` - pe2's loopback via asbr1 with a label
- pe1: `show cef vrf CUST-A 192.168.102.1` (XR) / `show ip cef vrf CUST-A 192.168.102.1 detail` (XE) - the 3-label stack
- asbr1: `show bgp vpnv4 unicast` - empty: the ASBRs carry no VPN routes
""",
}


# --------------------------------------------------------------------------
# Inter-AS over SRv6 (uSID): option A and end-to-end (option C style)
# --------------------------------------------------------------------------
#
#   ce1 --- pe1 --- p1 --- asbr1 ====== asbr2 --- p2 --- pe2 --- ce2
#   IPv6-only SRv6 cores, locator fc00:0:<sid>::/48 per node
#
def inter_as_srv6(option, name, images, mgmt, preset):
    P = PRESETS_INTERAS_SRV6[preset]
    lab = Lab(name, mgmt, images, "Inter-AS %s over SRv6 uSID - %s"
              % ("option A" if option == "A" else "option C (locator exchange)", P["label"]))
    for asn, side in ((65001, 0), (65002, 1)):
        s = "%d" % (side + 1)
        xo = 0 if side == 0 else 520
        core_router(lab, "pe" + s, plat_of(P, "pe" + s), xo + (0 if side == 0 else 240), 140, asn, 1, "PE", srv6=True)
        core_router(lab, "p" + s, plat_of(P, "p" + s), xo + 120, 40, asn, 2, "P", srv6=True)
        core_router(lab, "asbr" + s, plat_of(P, "asbr" + s), xo + (240 if side == 0 else 0), 140, asn, 3, "ASBR",
                    srv6=True)
    for asn, s in ((65001, "1"), (65002, "2")):
        core_link(lab, "pe" + s, "p" + s, 1, asn, srv6=True)
        core_link(lab, "p" + s, "asbr" + s, 2, asn, srv6=True)
    a1, a2, pe1, pe2 = (lab.devs[n] for n in ("asbr1", "asbr2", "pe1", "pe2"))
    for pe in (pe1, pe2):
        pe.vrfs.append(vrf_a(pe.asn, srv6=True))
    customer(lab, "ce1", P["ce1"], -160, 140, 65101, 1, "pe1", "CUST-A")
    customer(lab, "ce2", P["ce2"], 920, 140, 65102, 2, "pe2", "CUST-A")
    if option == "A":
        a1.vrfs.append(vrf_a(65001, srv6=True))
        a2.vrfs.append(vrf_a(65002, srv6=True))
        ia, ib = lab.p2p("asbr1", "asbr2", v4net="10.12.100.0/30", note="inter-AS link, VLAN 100 = CUST-A",
                         vrf="CUST-A", vlan=100)
        a1.nbr(_ip(ib.v4), 65002, ["ipv4"], desc="asbr2", vrf="CUST-A")
        a2.nbr(_ip(ia.v4), 65001, ["ipv4"], desc="asbr1", vrf="CUST-A")
        for pe, asbr in ((pe1, a1), (pe2, a2)):
            pe.nbr(asbr.lo6, pe.asn, ["vpnv4"], desc=asbr.name, lo=True)
            asbr.nbr(pe.lo6, pe.asn, ["vpnv4"], desc=pe.name, lo=True)
    else:
        # the ASBRs trade their AS's locators and loopbacks as plain IPv6 and inject the
        # other AS's into IS-IS; the PEs then peer VPNv4 multihop with SRv6 service SIDs
        ia, ib = lab.p2p("asbr1", "asbr2", v6net="2001:db8:12::/64", note="inter-AS link, eBGP IPv6 (locators)")
        for me, peer_if, other, side in ((a1, ib, a2, "1"), (a2, ia, a1, "2")):
            b = me.bgp_init()
            for n in ("pe", "p", "asbr"):
                d = lab.devs[n + side]
                b["net6"] += [d.locator, "%s/128" % d.lo6]
            me.nbr(_ip(peer_if.v6), other.asn, ["ipv6"], desc=other.name)
            me.isis["redist6"] = ["bgp %d" % me.asn] if me.plat == "xr" else ["bgp"]
        pe1.nbr(pe2.lo6, 65002, ["vpnv4"], desc="pe2-multihop", lo=True, multihop=True)
        pe2.nbr(pe1.lo6, 65001, ["vpnv4"], desc="pe1-multihop", lo=True, multihop=True)
    return lab.files(README_INTERAS_SRV6[option].format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


PRESETS_INTERAS_SRV6 = {
    "frr": {"label": "FRR everywhere", "as1": "frr", "as2": "frr", "ce1": "frr", "ce2": "frr"},
    "xr": {"label": "IOS-XR provider, FRR CEs", "as1": "xr", "as2": "xr", "ce1": "frr", "ce2": "frr"},
}

README_INTERAS_SRV6 = {
"A": """# {name} - Inter-AS option A over SRv6 uSID

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 --- p1 --- asbr1 ==VLAN 100== asbr2 --- p2 --- pe2 --- ce2
            \\___ AS 65001 SRv6 ___/            \\___ AS 65002 SRv6 ___/
```

Two IPv6-only SRv6 cores (IS-IS L2 for IPv6, uSID block fc00:0::/32, locator
fc00:0:<sid>::/48 - pe1 11, p1 12, asbr1 13, pe2 21, p2 22, asbr2 23). Inside
each AS the PE and ASBR exchange VPNv4 over their IPv6 loopbacks with per-VRF
uDT4 SIDs. Between the ASBRs a back-to-back VRF on a dot1q sub-interface
(VLAN 100) and plain eBGP IPv4 - no SRv6 crosses the boundary.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- pe1 (XR): `show segment-routing srv6 sid`; `show cef vrf CUST-A 192.168.102.1/32 detail` - H.Encaps.Red to asbr1's uDT4
- asbr1: `show bgp vrf CUST-A` - ce2's prefix as plain IPv4 from asbr2
- capture p1-asbr1: IPv6 to fc00:0:13:e00x:: with the IPv4 packet inside, no SRH
""",
"C": """# {name} - Inter-AS SRv6 L3VPN, option C style (locator exchange)

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 --- p1 --- asbr1 ======== asbr2 --- p2 --- pe2 --- ce2
            \\              eBGP IPv6: locators + loopbacks          /
             \\______ multihop eBGP VPNv4 with SRv6 SIDs (pe1 <-> pe2) __/
```

SRv6 needs no labels at the border - only reachability. The ASBRs advertise
their AS's locators (fc00:0:1x::/48 / fc00:0:2x::/48) and loopbacks to each
other over eBGP IPv6 and redistribute what they learn into IS-IS. The PEs peer
VPNv4 multihop over their IPv6 loopbacks and hand out uDT4 service SIDs, so
pe1 encapsulates straight to pe2's SID: one IPv6 header end to end, the ASBRs
and P routers just route on the outer destination.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- pe1: `show bgp vpnv4 unicast` - ce2's prefix with next hop 2001:db8:2::1 and an SRv6 SID in fc00:0:21::
- p1: `show route ipv6` - pe2's locator fc00:0:21::/48, redistributed from BGP by asbr1
- capture asbr1-asbr2: IPv6 to fc00:0:21:e00x:: carrying the customer IPv4 packet
""",
}


# --------------------------------------------------------------------------
# Carrier supporting Carrier
# --------------------------------------------------------------------------
#
#          customer carrier AS 65001                          customer carrier AS 65001
#   ce1 --- cpe1 --- ccse1 ===== bpe1 --- bp1 --- bpe2 ===== ccse2 --- cpe2 --- ce2
#                              \_____ backbone carrier AS 65000 _____/
#
def group_csc(node):
    return "ce" if node.startswith("ce") else "bb" if node.startswith("b") else "cc"


def csc_mpls(name, images, mgmt, preset):
    P = PRESETS_CSC[preset]
    pl = lambda n: plat_of(P, n, group_csc)          # noqa: E731
    lab = Lab(name, mgmt, images, "Carrier supporting Carrier over SR-MPLS - %s" % P["label"])
    for n, k, x in (("bpe1", 1, 300), ("bp1", 2, 440), ("bpe2", 3, 580)):
        core_router(lab, n, pl(n), x, 40 if n == "bp1" else 140, 65000, k, "backbone P" if n == "bp1" else "backbone PE",
                    sid_base=0)
    site_igp = P.get("site_igp", True)
    for n, k, x in (("cpe1", 1, 20), ("ccse1", 2, 160), ("cpe2", 11, 860), ("ccse2", 12, 720)):
        d = core_router(lab, n, pl(n), x, 140, 65001, k, "carrier PE" if n.startswith("cpe") else "CSC-CE")
        if not site_igp:
            d.isis = None
    core_link(lab, "bpe1", "bp1", 1, 65000)
    core_link(lab, "bp1", "bpe2", 2, 65000)
    for side in ("1", "2"):
        if site_igp:
            core_link(lab, "cpe" + side, "ccse" + side, 10 * int(side), 65001)
        else:
            lab.p2p("cpe" + side, "ccse" + side, v4net="10.1.%d.0/30" % (100 + 10 * int(side)),
                    note="carrier site %s" % side, mpls=True)
    bpe1, bpe2 = lab.devs["bpe1"], lab.devs["bpe2"]
    for b in (bpe1, bpe2):
        b.vrfs.append(Vrf("CARRIER", "65000:1", "65000:1", lu=True))
    bpe1.nbr(bpe2.lo4, 65000, ["vpnv4"], desc="bpe2", lo=True)
    bpe2.nbr(bpe1.lo4, 65000, ["vpnv4"], desc="bpe1", lo=True)
    for side, bpe in (("1", bpe1), ("2", bpe2)):
        ccse, cpe = lab.devs["ccse" + side], lab.devs["cpe" + side]
        ia, ib = lab.p2p(bpe.name, ccse.name, v4net="10.0.%d.0/30" % (200 + int(side)),
                         note="CsC link, eBGP labelled unicast", end_a={"vrf": "CARRIER"}, bgp_fwd=True)
        bpe.nbr(_ip(ib.v4), 65001, ["ipv4lu"], desc=ccse.name, vrf="CARRIER", as_override=True)
        # XR resolves an eBGP-LU path "via /32": without a /32 to the CSC-CE the labelled
        # routes towards it stay unresolved and are dropped (Cisco's CsC-with-BGP recipe)
        bpe.statics.append(("%s/32" % _ip(ib.v4), ia.eth, "CARRIER"))
        ccse.nbr(_ip(ia.v4), 65000, ["ipv4lu"], desc=bpe.name, allowas=True)
        ccse.statics.append(("%s/32" % _ip(ia.v4), ib.eth))
        cb = ccse.bgp
        cb["alloc_lu"] = True
        # the site's loopbacks go into BGP-LU; cpe originates its own (see inter-AS option C)
        cb["net4"].append("%s/32" % ccse.lo4)
        link = [i for i in cpe.ifs if i.peer == ccse.name][0]
        peer_ip = ccse.lo4 if site_igp else _ip([i for i in ccse.ifs if i.peer == cpe.name][0].v4)
        my_ip = cpe.lo4 if site_igp else _ip(link.v4)
        ccse.nbr(my_ip, 65001, ["ipv4lu"], desc=cpe.name, lo=site_igp, nhs=["ipv4lu"])
        cpe.nbr(peer_ip, 65001, ["ipv4lu"], desc=ccse.name, lo=site_igp)
        cpe.bgp["alloc_lu"] = True
        cpe.bgp["net4"].append("%s/32" % cpe.lo4)
        cpe.vrfs.append(Vrf("CUST-A", "65001:100", "65001:100"))
    cpe1, cpe2 = lab.devs["cpe1"], lab.devs["cpe2"]
    cpe1.nbr(cpe2.lo4, 65001, ["vpnv4"], desc="cpe2", lo=True)
    cpe2.nbr(cpe1.lo4, 65001, ["vpnv4"], desc="cpe1", lo=True)
    customer(lab, "ce1", P["ce1"], -140, 140, 65101, 1, "cpe1", "CUST-A")
    customer(lab, "ce2", P["ce2"], 1000, 140, 65102, 2, "cpe2", "CUST-A")
    return lab.files(README_CSC_MPLS.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb()),
                                            site="IS-IS + SR-MPLS inside each site" if site_igp else
                                            "BGP labelled unicast only inside each site (no IGP)"))


def csc_srv6(name, images, mgmt, preset):
    P = PRESETS_CSC_SRV6[preset]
    pl = lambda n: plat_of(P, n, group_csc)          # noqa: E731
    lab = Lab(name, mgmt, images, "Carrier supporting Carrier over SRv6 - %s" % P["label"])
    for n, k, x in (("bpe1", 1, 300), ("bp1", 2, 440), ("bpe2", 3, 580)):
        core_router(lab, n, pl(n), x, 40 if n == "bp1" else 140, 65000, k, "backbone P" if n == "bp1" else "backbone PE",
                    srv6=True, sid_base=0)
    for n, k, x in (("cpe1", 1, 20), ("ccse1", 2, 160), ("cpe2", 11, 860), ("ccse2", 12, 720)):
        d = core_router(lab, n, pl(n), x, 140, 65001, k, "carrier PE" if n.startswith("cpe") else "CSC-CE", srv6=True)
        d.locator = "fc01:0:%d::/48" % d.sid              # the carrier's own uSID block
    core_link(lab, "bpe1", "bp1", 1, 65000, srv6=True)
    core_link(lab, "bp1", "bpe2", 2, 65000, srv6=True)
    for side in ("1", "2"):
        core_link(lab, "cpe" + side, "ccse" + side, 10 * int(side), 65001, srv6=True)
    bpe1, bpe2 = lab.devs["bpe1"], lab.devs["bpe2"]
    for b in (bpe1, bpe2):
        b.vrfs.append(Vrf("CARRIER", "65000:1", "65000:1", v4=False, v6=True, srv6=True))
    bpe1.nbr(bpe2.lo6, 65000, ["vpnv6"], desc="bpe2", lo=True)
    bpe2.nbr(bpe1.lo6, 65000, ["vpnv6"], desc="bpe1", lo=True)
    for side, bpe in (("1", bpe1), ("2", bpe2)):
        ccse, cpe = lab.devs["ccse" + side], lab.devs["cpe" + side]
        ia, ib = lab.p2p(bpe.name, ccse.name, v6net="2001:db8:0:%d::/64" % (200 + int(side)),
                         note="CsC link, eBGP IPv6", end_a={"vrf": "CARRIER"})
        bpe.nbr(_ip(ib.v6), 65001, ["ipv6"], desc=ccse.name, vrf="CARRIER", as_override=True)
        ccse.nbr(_ip(ia.v6), 65000, ["ipv6"], desc=bpe.name, allowas=True)
        ccse.bgp["net6"] += [cpe.locator, "%s/128" % cpe.lo6, ccse.locator, "%s/128" % ccse.lo6]
        ccse.isis["redist6"] = ["bgp %d" % 65001] if ccse.plat == "xr" else ["bgp"]
        cpe.vrfs.append(Vrf("CUST-A", "65001:100", "65001:100", srv6=True))
    cpe1, cpe2 = lab.devs["cpe1"], lab.devs["cpe2"]
    cpe1.nbr(cpe2.lo6, 65001, ["vpnv4"], desc="cpe2", lo=True)
    cpe2.nbr(cpe1.lo6, 65001, ["vpnv4"], desc="cpe1", lo=True)
    customer(lab, "ce1", P["ce1"], -140, 140, 65101, 1, "cpe1", "CUST-A")
    customer(lab, "ce2", P["ce2"], 1000, 140, 65102, 2, "cpe2", "CUST-A")
    return lab.files(README_CSC_SRV6.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


PRESETS_CSC = {
    "xr-bb": {"label": "IOS-XR backbone, FRR customer carrier and CEs", "bb": "xr", "cc": "frr",
              "ce1": "frr", "ce2": "frr", "site_igp": False},
    "xr": {"label": "IOS-XR everywhere, FRR CEs", "bb": "xr", "cc": "xr", "ce1": "frr", "ce2": "frr"},
}
PRESETS_CSC_SRV6 = {
    "frr": {"label": "FRR everywhere", "bb": "frr", "cc": "frr", "ce1": "frr", "ce2": "frr"},
    "xr-bb": {"label": "IOS-XR backbone, FRR customer carrier and CEs", "bb": "xr", "cc": "frr",
              "ce1": "frr", "ce2": "frr"},
}

README_CSC_MPLS = """# {name} - Carrier supporting Carrier over SR-MPLS

Preset: **{preset}** - about {ram} of RAM.

```
   customer carrier AS 65001 (site 1)                       customer carrier AS 65001 (site 2)
  ce1 --- cpe1 --- ccse1 ====== bpe1 --- bp1 --- bpe2 ====== ccse2 --- cpe2 --- ce2
                         BGP-LU  \\__ backbone AS 65000 __/  BGP-LU
                                     VRF CARRIER
```

The **backbone carrier** (AS 65000, SR-MPLS, SID index 1-3) sells a labelled VPN:
its PEs hold VRF CARRIER and talk **eBGP labelled unicast** to the customer
carrier's CSC-CEs, so the customer carrier's own loopbacks - and the labels
for them - cross the backbone inside the VPN.

The **customer carrier** (AS 65001, two sites: {site}) runs its own L3VPN on
top: cpe1 and cpe2 peer VPNv4 directly over their loopbacks. A customer packet
leaves cpe1 with [label to cpe2 + CUST-A VPN label]; at bpe1 the backbone puts
its own [transport + CARRIER VPN label] on top - four labels deep in the core.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- bpe1: `show bgp vrf CARRIER` (XR) - the customer carrier's loopbacks with labels
- cpe1: `show bgp ipv4 vpn` / `show bgp vpnv4 unicast` - ce2's route, next hop cpe2
- capture bpe1-bp1 while ce1 pings: the 4-label stack
"""

README_CSC_SRV6 = """# {name} - Carrier supporting Carrier over SRv6

Preset: **{preset}** - about {ram} of RAM.

```
   customer carrier AS 65001 (SRv6, block fc01:0::/32)        (site 2)
  ce1 --- cpe1 --- ccse1 ====== bpe1 --- bp1 --- bpe2 ====== ccse2 --- cpe2 --- ce2
                         eBGP v6  \\_ backbone AS 65000 SRv6 _/  eBGP v6
                                   VRF CARRIER (IPv6, uDT6)
```

With SRv6 a carrier's carrier needs no labels at all: the customer carrier's
SRv6 packets are just IPv6 to the backbone. The backbone (AS 65000, uSID block
fc00:0::/32) sells an **IPv6 L3VPN** (VRF CARRIER, per-VRF uDT6 SIDs); the
CSC-CEs advertise their site's locators (fc01:0:<sid>::/48) and loopbacks into
it and redistribute the other site's into IS-IS. cpe1 and cpe2 then run their
own SRv6 L3VPN (VPNv4, uDT4) over their loopbacks.

A packet from ce1 is encapsulated twice: cpe1 wraps it in IPv6 to cpe2's uDT4
SID (fc01:0:11:e00x::); bpe1 looks that up in VRF CARRIER and wraps it again
in IPv6 to bpe2's uDT6 SID (fc00:0:3:e00x::).

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- bpe1: `show bgp vrf CARRIER ipv6 unicast` (XR) / `show bgp vrf CARRIER ipv6` (FRR) - carrier locators
- cpe1: `show bgp ipv4 vpn` - ce2's prefix with an SRv6 SID in fc01:0:11::
- capture bpe1-bp1: IPv6 in IPv6 in ... the customer IPv4 packet, two SRv6 layers
"""


# --------------------------------------------------------------------------
# other SP designs - single AS 65000, loopbacks 10.0.0.<n>, SID index <n>
# --------------------------------------------------------------------------

def group_single(node):
    return "ce" if node.startswith("ce") else "core"


def _pe_pair_vpn(pe_a, pe_b, afs=("vpnv4",)):
    pe_a.nbr(pe_b.lo4, 65000, list(afs), desc=pe_b.name, lo=True)
    pe_b.nbr(pe_a.lo4, 65000, list(afs), desc=pe_a.name, lo=True)


#   ce1 --- pe1 ----- p1 ======= p2 ----- pe2 --- ce2
#           SR-MPLS only  SR+LDP   LDP only
def sr_ldp(name, images, mgmt, preset):
    P = PRESETS_SRLDP[preset]
    pl = lambda n: plat_of(P, n, group_single)          # noqa: E731
    lab = Lab(name, mgmt, images, "SR-MPLS / LDP interworking with a mapping server - %s" % P["label"])
    pe1 = core_router(lab, "pe1", pl("pe1"), 0, 140, 65000, 1, "PE (SR only)")
    p1 = core_router(lab, "p1", pl("p1"), 180, 40, 65000, 2, "P (SR + LDP, mapping server)")
    p2 = core_router(lab, "p2", pl("p2"), 380, 40, 65000, 3, "P (LDP only)", sr=False, ldp=True)
    pe2 = core_router(lab, "pe2", pl("pe2"), 560, 140, 65000, 4, "PE (LDP only)", sr=False, ldp=True)
    p1.ldp = True
    core_link(lab, "pe1", "p1", 1, 65000)
    core_link(lab, "p1", "p2", 2, 65000, ldp=True)
    core_link(lab, "p2", "pe2", 3, 65000, ldp=True)
    # p1 advertises SIDs on behalf of the LDP-only routers (10.0.0.3 and .4 -> index 3, 4)
    p1.mapping_server = [("10.0.0.3/32", 3, 2)]
    p1.isis["mapping_rx"] = True
    for pe in (pe1, pe2):
        pe.vrfs.append(Vrf("CUST-A", "65000:100", "65000:100"))
    _pe_pair_vpn(pe1, pe2)
    customer(lab, "ce1", P["ce1"], -160, 140, 65101, 1, "pe1", "CUST-A")
    customer(lab, "ce2", P["ce2"], 720, 140, 65102, 2, "pe2", "CUST-A")
    return lab.files(README_SRLDP.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


#            p1 (metric 10, delay 50 ms each way)
#          /    \
#   ce1 - pe1    pe2 - ce2
#          \    /
#            p2 (metric 30, delay 2 ms)
def flex_algo(name, images, mgmt, preset):
    P = PRESETS_FLEX[preset]
    pl = lambda n: plat_of(P, n, group_single)          # noqa: E731
    lab = Lab(name, mgmt, images, "Flex-Algo 128 low-delay slice - %s" % P["label"])
    nodes = {}
    for n, k, x, y in (("pe1", 1, 0, 140), ("p1", 2, 220, 20), ("p2", 3, 220, 260), ("pe2", 4, 440, 140)):
        d = nodes[n] = core_router(lab, n, pl(n), x, y, 65000, k, "PE" if n.startswith("pe") else "P")
        d.isis["flex"] = [(128, "delay")]
        d.isis["te"] = True
    k = 0
    for a, b, metric, delay in (("pe1", "p1", 10, 50000), ("p1", "pe2", 10, 50000),
                                ("pe1", "p2", 30, 2000), ("p2", "pe2", 30, 2000)):
        k += 1
        lab.p2p(a, b, v4net="10.0.%d.0/30" % (100 + k), note="metric %d, delay %d ms" % (metric, delay // 1000),
                isis=True, mpls=True, metric=metric, delay=delay)
    pe1, pe2 = nodes["pe1"], nodes["pe2"]
    for pe in (pe1, pe2):
        pe.vrfs.append(Vrf("CUST-A", "65000:100", "65000:100"))
        pe.vrfs.append(Vrf("LOW-DELAY", "65000:128", "65000:128"))
    _pe_pair_vpn(pe1, pe2)
    customer(lab, "ce1", P["ce"], -160, 60, 65101, 1, "pe1", "CUST-A")
    customer(lab, "ce2", P["ce"], 600, 60, 65102, 2, "pe2", "CUST-A")
    customer(lab, "ce3", P["ce"], -160, 220, 65103, 3, "pe1", "LOW-DELAY")
    customer(lab, "ce4", P["ce"], 600, 220, 65104, 4, "pe2", "LOW-DELAY")
    for pe in (pe1, pe2):
        if pe.plat == "xr":
            # VRF LOW-DELAY is exported with colour 128; the headend builds an on-demand
            # SR policy for colour 128 restricted to flex-algo 128 (the low-delay slice)
            pe.extra["xr"] = ["extcommunity-set opaque COLOR-128", "  128", "end-set", "!",
                              "route-policy SET-COLOR-128", "  set extcommunity color COLOR-128", "  pass",
                              "end-policy", "!",
                              "vrf LOW-DELAY", " address-family ipv4 unicast",
                              "  export route-policy SET-COLOR-128", " !", "!"]
            # inside the one segment-routing block: XR applies neither of two top-level blocks
            # (XR 26.2 takes sid-algorithm under constraints/segments, not under dynamic)
            pe.extra["xr_sr"] = [" traffic-eng", "  on-demand color 128", "   dynamic", "   !", "   constraints",
                                 "    segments", "     sid-algorithm 128", "    !", "   !", "  !", " !"]
    return lab.files(README_FLEX.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


#   ce1 (IPv6) --\                        /-- ce2 (IPv6)          6PE: global IPv6 over an IPv4 SR-MPLS core
#                 pe1 ---- p1 ---- pe2
#   ce3 (VRF) ---/                        \-- ce4 (VRF)           6VPE: IPv6 in a VRF, VPNv6
def sixpe(name, images, mgmt, preset):
    P = PRESETS_6PE[preset]
    pl = lambda n: plat_of(P, n, group_single)          # noqa: E731
    lab = Lab(name, mgmt, images, "6PE and 6VPE over an IPv4 SR-MPLS core - %s" % P["label"])
    pe1 = core_router(lab, "pe1", pl("pe1"), 0, 140, 65000, 1, "PE")
    core_router(lab, "p1", pl("p1"), 220, 140, 65000, 2, "P")
    pe2 = core_router(lab, "pe2", pl("pe2"), 440, 140, 65000, 3, "PE")
    core_link(lab, "pe1", "p1", 1, 65000)
    core_link(lab, "p1", "pe2", 2, 65000)
    for pe in (pe1, pe2):
        pe.vrfs.append(Vrf("CUST-A", "65000:100", "65000:100", v4=True, v6=True))
        pe.bgp_init()["alloc_lu"] = True
        # not routed anywhere (the core has no IPv6); FRR refuses 6PE without a local
        # IPv6 address on the update-source interface
        pe.lo6 = "2001:db8::%d" % (1 if pe is pe1 else 3)
    pe1.nbr(pe2.lo4, 65000, ["ipv6lu", "vpnv4", "vpnv6"], desc="pe2", lo=True, nhs=["ipv6lu"])
    pe2.nbr(pe1.lo4, 65000, ["ipv6lu", "vpnv4", "vpnv6"], desc="pe1", lo=True, nhs=["ipv6lu"])
    # 6PE customers sit in the global table: IPv6-only link and eBGP IPv6
    for ce_name, idx, pe, x in (("ce1", 1, pe1, -160), ("ce2", 2, pe2, 600)):
        ce = lab.dev(ce_name, P["ce"], x, 40, role="CE (6PE, global IPv6)", asn=65100 + idx)
        ce.lo4 = "192.168.%d.1" % (100 + idx)
        ce.lo6 = "2001:db8:c:%d::1" % idx
        ia, ib = lab.p2p(pe.name, ce_name, v6net="2001:db8:6:%d::/64" % idx, note="6PE site %s" % ce_name)
        ce.bgp_init()
        ce.nbr(_ip(ia.v6), 65000, ["ipv6"], desc=pe.name)
        ce.bgp["net6"].append("%s/128" % ce.lo6)
        pe.nbr(_ip(ib.v6), ce.asn, ["ipv6"], desc=ce_name)
    customer(lab, "ce3", P["ce"], -160, 240, 65103, 3, "pe1", "CUST-A", v6=True)
    customer(lab, "ce4", P["ce"], 600, 240, 65104, 4, "pe2", "CUST-A", v6=True)
    return lab.files(README_6PE.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


#   ce1 ==AC== pe1 ---- p1 ---- pe2 ==AC== ce2      one Ethernet segment each side, EVPN-VPWS evi 100
def evpn_vpws(name, images, mgmt, preset):
    P = PRESETS_VPWS[preset]
    pl = lambda n: plat_of(P, n, group_single)          # noqa: E731
    lab = Lab(name, mgmt, images, "EVPN-VPWS point-to-point service - %s" % P["label"])
    pe1 = core_router(lab, "pe1", pl("pe1"), 0, 140, 65000, 1, "PE")
    core_router(lab, "p1", pl("p1"), 220, 140, 65000, 2, "P")
    pe2 = core_router(lab, "pe2", pl("pe2"), 440, 140, 65000, 3, "PE")
    core_link(lab, "pe1", "p1", 1, 65000)
    core_link(lab, "p1", "pe2", 2, 65000)
    pe1.nbr(pe2.lo4, 65000, ["evpn"], desc="pe2", lo=True)
    pe2.nbr(pe1.lo4, 65000, ["evpn"], desc="pe1", lo=True)
    for ce_name, idx, pe, x, local, remote in (("ce1", 1, pe1, -160, 1, 2), ("ce2", 2, pe2, 600, 2, 1)):
        ce = lab.dev(ce_name, P["ce"], x, 140, role="CE", asn=65100 + idx)
        ce.lo4 = "192.168.%d.1" % (100 + idx)
        pa, pc = lab.link(pe.name, ce_name, "attachment circuit, EVPN-VPWS evi 100")
        pe.add_if(pa, ce_name, l2=True)
        ce.add_if(pc, pe.name, v4="192.168.12.%d/24" % idx)
        # the two CEs share one subnet across the pseudowire and exchange loopbacks over eBGP
        ce.bgp_init()
        ce.nbr("192.168.12.%d" % (3 - idx), 65100 + 3 - idx, ["ipv4"], desc="ce%d over the VPWS" % (3 - idx))
        ce.bgp["net4"].append("%s/32" % ce.lo4)
        if pe.plat == "xr":
            pe.extra["xr"] = ["evpn", " evi 100", "  bgp", "   rd %s:100" % pe.lo4, "   route-target import 65000:100",
                              "   route-target export 65000:100", "  !", " !", "!",
                              "l2vpn", " xconnect group VPWS", "  p2p CUST-B",
                              "   interface %s" % xr_if(pa),
                              "   neighbor evpn evi 100 target %d source %d" % (remote, local),
                              "   !", "  !", " !", "!"]
    return lab.files(README_VPWS.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


PRESETS_SRLDP = {
    "xr": {"label": "IOS-XR everywhere, FRR CEs", "core": "xr", "ce1": "frr", "ce2": "frr"},
    "xr-frr": {"label": "IOS-XR SR domain + mapping server, FRR LDP domain", "core": "frr", "pe1": "xr",
               "p1": "xr", "ce1": "frr", "ce2": "frr"},
}
PRESETS_FLEX = {
    "xr": {"label": "IOS-XR, FRR CEs", "core": "xr", "ce": "frr"},
}
PRESETS_6PE = {
    "frr": {"label": "FRR everywhere", "core": "frr", "ce": "frr"},
    "xr": {"label": "IOS-XR provider, FRR CEs", "core": "xr", "ce": "frr"},
}
PRESETS_VPWS = {
    "xr": {"label": "IOS-XR provider, FRR CEs", "core": "xr", "ce": "frr"},
}

README_SRLDP = """# {name} - SR-MPLS / LDP interworking

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 ------ p1 ======= p2 ------ pe2 --- ce2
          SR only   SR + LDP    LDP only  LDP only
                    mapping server
```

A network half-way through its migration from LDP to Segment Routing. One
IS-IS L2 domain; pe1 speaks only SR-MPLS, p2 and pe2 only LDP, and p1 runs both
and is the **SR mapping server**: it advertises prefix SIDs on behalf of the
LDP-only routers (10.0.0.3/32 index 3, range 2). pe1 can therefore push an SR
label (16004) towards pe2, and p1 **stitches** SR to LDP in one direction and
LDP to SR in the other. pe1 and pe2 run an L3VPN (VPNv4) over it.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1`
- p1: `show segment-routing mapping-server prefix-sid-map ipv4` (XR)
- pe1: `show isis segment-routing prefix-sid-map active-policy` / `show cef 10.0.0.4/32` - SID 16004 from the mapping server
- p1: `show mpls forwarding labels 16004` - SR in, LDP label out (and the LDP binding for 10.0.0.1 swapped to 16001)
- pe2: `show mpls ldp bindings 10.0.0.1/32` - pe1 reachable over plain LDP
"""

README_FLEX = """# {name} - Flex-Algo 128: a low-delay slice

Preset: **{preset}** - about {ram} of RAM.

```
                 p1   (IGP metric 10, delay 50 ms per link)
               /    \\
  ce1/ce3 - pe1      pe2 - ce2/ce4
               \\    /
                 p2   (IGP metric 30, delay 2 ms per link)
```

IS-IS carries two topologies. Algorithm 0 (normal SPF on the IGP metric) goes
via p1; **Flex-Algo 128** is defined with metric-type *delay* and every node
advertises an extra prefix SID for it (index = node + 1280, label 17281 for
pe1), so algo-128 paths go via p2. Link delays are static values advertised by
performance-measurement (XR `advertise-delay`, in microseconds).

Two VRFs share the same PEs: **CUST-A** follows algo 0; **LOW-DELAY** is exported
with colour 128, and the ingress PE builds an **on-demand SR-TE policy** for
colour 128 with `sid-algorithm 128`, steering it over the low-delay slice.

## Try it
- `show isis flex-algo 128` / `show isis ipv4 route flex-algo 128` - algo-128 paths via p2
- pe1: `show mpls forwarding labels 17284` vs `16004` - two labels to pe2, two next hops
- pe1: `show segment-routing traffic-eng policy color 128` - the on-demand policy for LOW-DELAY
- ce3: `traceroute 192.168.104.1 -s 192.168.103.1`-style checks: `show cef vrf LOW-DELAY 192.168.104.1/32` on pe1 goes via p2, `show cef vrf CUST-A 192.168.102.1/32` via p1
"""

README_6PE = """# {name} - 6PE and 6VPE over an IPv4 SR-MPLS core

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 (IPv6 only) --\\                  /-- ce2 (IPv6 only)     6PE  - global table
                     pe1 --- p1 --- pe2
  ce3 (dual stack) --/                  \\-- ce4 (dual stack)    6VPE - VRF CUST-A
```

The core is IPv4-only (IS-IS, SR-MPLS, no IPv6 on p1). IPv6 still crosses it:
- **6PE** (RFC 4798): pe1 and pe2 exchange global IPv6 prefixes as *IPv6 labelled
  unicast* over their IPv4 iBGP session. The next hop is the IPv4-mapped
  address ::ffff:10.0.0.x and the route carries a label, so IPv6 packets ride
  the IPv4 LSP with no IPv6 in the core.
- **6VPE** (RFC 4659): the same for VRF CUST-A, via VPNv6.

## Try it
- ce1: `ping 2001:db8:c:2::1` (6PE); ce3: `ping 192.168.104.1` and `ping 2001:db8:c:4::1` (6VPE)
- pe1: `show bgp ipv6 labeled-unicast` / `show bgp ipv6 unicast` - next hop ::ffff:10.0.0.3 with a label
- pe1: `show bgp vpnv6 unicast` - CUST-A IPv6 routes
- p1: nothing IPv6 at all - `show route ipv6` is empty apart from link-locals
"""

README_VPWS = """# {name} - EVPN-VPWS

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 ==AC== pe1 --- p1 --- pe2 ==AC== ce2
      192.168.12.1/24          192.168.12.2/24
```

A point-to-point Ethernet service signalled by BGP EVPN (RFC 8214) instead of
LDP pseudowires. Each PE advertises an EVPN route type 1 (Ethernet A-D per EVI)
for evi 100 with its local service id (pe1 = 1, pe2 = 2); the other end
matches it as `target`, and the attachment circuits are cross-connected over
the SR-MPLS core. The CEs see one Ethernet segment - same subnet, eBGP between
their link addresses, loopbacks advertised.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1` (via eBGP over the pseudowire)
- pe1: `show l2vpn xconnect detail` - UP, EVPN signalling, the remote label
- pe1: `show bgp l2vpn evpn` - the two type-1 routes
- `show evpn evi vpn-id 100 detail`
"""


# --------------------------------------------------------------------------
# multicast: plain IPv4 PIM-SM / SSM / anycast RP
# --------------------------------------------------------------------------
# XRd has no multicast forwarding plane (see the module docstring of the catalogue
# UI / reference notes), so every multicast preset is IOS-XE and/or FRR. Receivers
# are FRR hosts that join with `ip igmp join-group` and answer pings sent to the
# group, which makes "ping 239.1.1.1 from the source" the end-to-end check.

def group_mcast(node):
    return "host" if node.startswith("h") else "ce" if node.startswith("ce") else "core"


def host(lab, name, x, y, router, net, joins=(), role="host"):
    """An end host on its own /24 behind a router; the router side is the IGMP querier / PIM DR."""
    h = lab.dev(name, "frr", x, y, role=role, icon="server")
    h.host = True
    ia, ib = lab.p2p(router, name, v4net=net, note="%s LAN" % name,
                     end_a={"pim": True, "igmp": True, "isis": True, "isis_passive": True},
                     end_b={"joins": list(joins)})
    # not a default route: eth0 (management) already owns that one
    h.statics.append(("10.0.0.0/8", _ip(ia.v4)))
    return h


#          r2 (RP)
#         /    \
#  h1 - r1      r3 - h2
#         \    /
#          r4 (anycast RP in "anycast") - h3
def mcast_ipv4(mode, name, images, mgmt, preset):
    P = PRESETS_MCAST[preset]
    pl = lambda n: plat_of(P, n, group_mcast)          # noqa: E731
    lab = Lab(name, mgmt, images, "IPv4 multicast, PIM-SM %s - %s"
              % ("with a static RP" if mode == "static" else "with anycast RP and MSDP", P["label"]))
    rp = LO_NET + (".2" if mode == "static" else ".100")
    for k, (n, x, y) in enumerate((("r1", 0, 140), ("r2", 220, 20), ("r3", 440, 140), ("r4", 220, 260)), 1):
        d = core_router(lab, n, pl(n), x, y, 65000, k, "router", sr=False, lo_base=LO_NET)
        d.pim = {"rp": rp, "asm": "239.0.0.0/8"}
        if mode == "anycast" and n in ("r2", "r4"):
            d.role = "anycast RP"
            d.lo_extra = [rp]
            d.pim["msdp"] = [LO_NET + (".4" if n == "r2" else ".2")]
        elif n == "r2":
            d.role = "RP"
    for k, (a, b) in enumerate((("r1", "r2"), ("r2", "r3"), ("r1", "r4"), ("r4", "r3")), 1):
        lab.p2p(a, b, v4net="10.0.%d.0/30" % (100 + k), note="core, IS-IS + PIM-SM", isis=True, pim=True)
    host(lab, "h1", -180, 140, "r1", "10.10.1.0/24", role="source")
    host(lab, "h2", 620, 140, "r3", "10.10.2.0/24", joins=[("239.1.1.1", None), ("232.1.1.1", "10.10.1.2")],
         role="receiver")
    host(lab, "h3", 400, 340, "r4", "10.10.3.0/24", joins=[("239.1.1.1", None)], role="receiver")
    return lab.files(README_MCAST[mode].format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


PRESETS_MCAST = {
    "frr": {"label": "FRR routers and hosts", "core": "frr", "host": "frr"},
    "xe": {"label": "IOS-XE routers, FRR hosts", "core": "xe", "host": "frr"},
    "xe-frr": {"label": "IOS-XE RPs (r2, r4), FRR r1/r3 and hosts", "core": "frr", "r2": "xe", "r4": "xe",
               "host": "frr"},
}
README_MCAST = {
"static": """# {name} - IPv4 multicast: PIM-SM with a static RP, and SSM

Preset: **{preset}** - about {ram} of RAM.

```
            r2 (RP 10.255.0.2)
           /    \\
  h1 --- r1      r3 --- h2      h1 = source 10.10.1.2
  source   \\    /                h2 joins 239.1.1.1 and (10.10.1.2, 232.1.1.1)
            r4 --------- h3     h3 joins 239.1.1.1
```

Plain IPv4 - no MPLS, no VPN. IS-IS carries unicast (every host LAN is a passive
IS-IS interface, which is what RPF checks against), PIM sparse mode runs on every
core link and on the host LANs, where the router is also the IGMPv3 querier.

- **ASM (239.0.0.0/8)** uses the static RP r2. A receiver's join builds a shared
  tree towards r2; h1's first packet is registered to r2 by r1, r2 joins towards
  h1, and the last-hop routers switch over to the shortest-path tree.
- **SSM (232.0.0.0/8, `ip pim ssm default` / FRR's default range)** needs no RP:
  h2 asks for (10.10.1.2, 232.1.1.1) with IGMPv3 and r3 joins straight towards h1.

The hosts join with `ip igmp join-group` (a socket join, exactly like an
application) and answer echo requests sent to the group, so a ping from the
source is the end-to-end test.

## Try it
- h1: `ping -c3 -t 16 -I eth1 239.1.1.1` - replies from h2 (10.10.2.2) and h3 (10.10.3.2)
- h1: `ping -c3 -t 16 -I eth1 232.1.1.1` - a reply from h2 only (SSM, h3 did not ask)
- r2: `show ip mroute` / FRR `show ip mroute` - (*,239.1.1.1) and the (S,G) after the register
- r3: `show ip pim rp mapping` (XE) / `show ip pim rp-info` (FRR); `show ip igmp groups`

FRR routers carry a small watchdog (`/etc/frr/rpwatch.sh`): FRR 10.4 pimd can miss
the IS-IS route to the RP at boot and leave the RP unresolved, so the script
re-applies the `rp` line whenever `show ip pim rp-info` says Unknown. The ASM
ping can therefore take up to 30 s after boot to get its first replies.
""",
"anycast": """# {name} - IPv4 multicast: anycast RP with MSDP

Preset: **{preset}** - about {ram} of RAM.

```
            r2 (RP 10.255.0.100, MSDP 10.255.0.2)
           /    \\
  h1 --- r1      r3 --- h2
  source   \\    /
            r4 (RP 10.255.0.100, MSDP 10.255.0.4) --- h3
```

Two routers own the **same RP address** 10.255.0.100 (a second /32 on the
loopback, advertised into IS-IS). Every router points at 10.255.0.100 and simply
reaches the closer one - RP redundancy and load sharing with no election. The
two RPs run **MSDP** between their unique loopbacks (originator-id = the unique
loopback), so a source registered at one RP is announced to the other in a
Source-Active message and receivers joined at either RP get the traffic.

SSM (232.0.0.0/8) needs no RP at all and works as in the static-RP lab.

## Try it
- h1: `ping -c3 -t 16 -I eth1 239.1.1.1` - replies from h2 and h3
- r2 / r4: `show ip msdp sa-cache` (XE) / `show ip msdp sa` (FRR) - the SA for (10.10.1.2, 239.1.1.1)
- `show ip msdp summary` / FRR `show ip msdp peer` - the MSDP session r2 <-> r4
- shut r1's link to the RP it registers with and ping again: the other RP takes over

FRR routers carry the same RP watchdog as the static-RP lab (`/etc/frr/rpwatch.sh`).
""",
}

# --------------------------------------------------------------------------
# SR-MPLS L3VPN with redundant route reflectors and BGP add-path
# --------------------------------------------------------------------------
#
#              rr1            rr2
#               |              |
#   ce1 ===== pe1 ---- p1 ---- p2 ---- pe2 ===== ce1   (ce1 dual-homed)
#                        \      /
#                          pe3 --- ce2
#
def rr_addpath(name, images, mgmt, preset):
    P = PRESETS_RR[preset]
    pl = lambda n: plat_of(P, n, group_rr)          # noqa: E731
    lab = Lab(name, mgmt, images, "SR-MPLS L3VPN with redundant route reflectors and add-path - %s" % P["label"])
    nodes = (("pe1", 0, 60, "PE"), ("pe2", 0, 300, "PE"), ("p1", 220, 60, "P"), ("p2", 220, 300, "P"),
             ("pe3", 440, 180, "PE"), ("rr1", 220, -80, "route reflector"), ("rr2", 220, 440, "route reflector"))
    for k, (n, x, y, role) in enumerate(nodes, 1):
        core_router(lab, n, pl(n), x, y, 65000, k, role, tilfa=pl(n) == "xr", lo_base=LO_NET)
    for k, (a, b) in enumerate((("pe1", "p1"), ("pe2", "p2"), ("p1", "p2"), ("p1", "pe3"), ("p2", "pe3"),
                                ("rr1", "p1"), ("rr2", "p2")), 1):
        core_link(lab, a, b, k, 65000)
    pes = [lab.devs[n] for n in ("pe1", "pe2", "pe3")]
    rrs = [lab.devs[n] for n in ("rr1", "rr2")]
    for pe in pes:
        # one RD for the whole VPN: the two paths to ce1 are the same VPNv4 prefix, so
        # without add-path each RR would reflect only its best one
        pe.vrfs.append(Vrf("CUST-A", "65000:100", "65000:100"))
        pe.bgp_init()["pic"] = True
        for rr in rrs:
            pe.nbr(rr.lo4, 65000, ["vpnv4"], desc=rr.name, lo=True, addpath="recv")
            rr.nbr(pe.lo4, 65000, ["vpnv4"], desc=pe.name, lo=True, rrc=["vpnv4"], addpath="send")
    # no rr1 <-> rr2 session: every PE already peers with both, and an RR-to-RR session only
    # makes each RR reflect the other's copies (8 paths on pe3 instead of 4)
    ce1 = customer(lab, "ce1", pl("ce1"), -200, 180, 65101, 1, "pe1", "CUST-A")
    ce1.role = "CE (dual-homed)"
    ia, ib = lab.p2p("pe2", "ce1", v4net="172.16.11.0/30", note="CUST-A second link of ce1", end_a={"vrf": "CUST-A"})
    ce1.nbr(_ip(ia.v4), 65000, ["ipv4"], desc="pe2")
    lab.devs["pe2"].nbr(_ip(ib.v4), 65101, ["ipv4"], desc="ce1", vrf="CUST-A")
    customer(lab, "ce2", pl("ce2"), 640, 180, 65102, 2, "pe3", "CUST-A")
    return lab.files(README_RR.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


def group_rr(node):
    return "ce" if node.startswith("ce") else "rr" if node.startswith("rr") else "core"


PRESETS_RR = {
    "frr": {"label": "FRR everywhere", "core": "frr", "rr": "frr", "ce": "frr"},
    "xr": {"label": "IOS-XR provider and RRs, FRR CEs", "core": "xr", "rr": "xr", "ce": "frr"},
    "xr-frr-rr": {"label": "IOS-XR PEs and Ps, FRR route reflectors and CEs", "core": "xr", "rr": "frr",
                  "ce": "frr"},
    # pe3 stays XR: IOS-XE 17.12 cannot receive add-paths for VPNv4, so an XE pe3 would
    # only ever see the RRs' best path. XE on the dual-homed side only originates.
    "xe-xr": {"label": "IOS-XE dual-homed PEs (pe1, pe2), IOS-XR pe3, Ps and RRs, FRR CEs", "core": "xr",
              "pe1": "xe", "pe2": "xe", "rr": "xr", "ce": "frr"},
}

README_RR = """# {name} - SR-MPLS L3VPN with redundant route reflectors and BGP add-path

Preset: **{preset}** - about {ram} of RAM.

```
               rr1              rr2          (out of the forwarding path)
                |                |
  ce1 ====== pe1 ---- p1 ---- p2 ---- pe2 ====== ce1     ce1 dual-homed, AS 65101
                        \\      /
                          pe3 --- ce2
```

An SR-MPLS core (IS-IS, SRGB 16000-23999, SID index = node number) with
**two route reflectors** for VPNv4. Every PE peers with both RRs (the RRs need
no session to each other, since each already has every client). Each RR has
its own cluster-id (its router-id), so a PE receives every route twice - once
per RR - and loses nothing when one RR dies.

VRF CUST-A uses **the same RD (65000:100) on every PE**. ce1 is dual-homed to
pe1 and pe2, so its loopback 192.168.101.1/32 exists twice with the *same*
VPNv4 prefix. Classic BGP would let each RR advertise only its best path and
pe3 would never learn the second exit. With **add-path** the RRs advertise all
paths (XR `set path-selection all advertise`, XE `bgp additional-paths select
all`, FRR `addpath-tx-all-paths`) and pe3 keeps the second one as a
pre-programmed **PIC edge backup**.

## Try it
- ce2: `ping 192.168.101.1 -I 192.168.102.1`
- pe3: `show bgp vpnv4 unicast rd 65000:100 192.168.101.1/32` (FRR `show bgp ipv4 vpn 192.168.101.1/32`) - four paths: via pe1 (10.255.0.1) and pe2 (10.255.0.2), each from rr1 and rr2
- pe3 (XR): `show cef vrf CUST-A 192.168.101.1/32` - primary and `backup` path
- rr1: `show bgp vpnv4 unicast neighbors 10.255.0.5 advertised-count` / FRR `show bgp ipv4 vpn neighbors 10.255.0.5 advertised-routes`
- fail the pe1-ce1 link from the topology view while ce2 pings: PIC switches to pe2 without waiting for BGP
- stop rr1: nothing changes for the customers, rr2 still reflects every path
"""


# --------------------------------------------------------------------------
# SR-MPLS to SRv6 migration: legacy PE, migrated PE, SRv6/MPLS gateway
# --------------------------------------------------------------------------
#
#   ce1 ---- pe1 (SR-MPLS) ---- p1 ================ p2 ---- pe2 (SRv6) ---- ce2
#      ce3 =/                     \   both planes  /                \= ce3   (dual-homed)
#                                  \              /
#                                   gw (dual plane, SRv6/MPLS gateway) ---- ce4
#
def group_mig(node):
    return "ce" if node.startswith("ce") else "gw" if node == "gw" else "core"


# One RT for the VPN on both planes; the gateway lists it a second time as its *stitching*
# RT. pe1 and pe2 only ever peer with the gateway, so a shared RT cannot leak routes
# between the planes directly. (Separate RTs per plane did not work: see below.)
RT_VPN = "65000:100"


def migration(name, images, mgmt, preset):
    P = PRESETS_MIG[preset]
    pl = lambda n: plat_of(P, n, group_mig)          # noqa: E731
    lab = Lab(name, mgmt, images, "SR-MPLS to SRv6 migration, L3VPN - %s" % P["label"])
    both = lambda d: d.isis.update(sr=d.sid, v4=True, srv6=True)          # noqa: E731
    pe1 = core_router(lab, "pe1", pl("pe1"), 0, 60, 65000, 1, "PE, SR-MPLS only (not migrated)", lo_base=LO_NET)
    p1 = core_router(lab, "p1", pl("p1"), 220, 60, 65000, 2, "P, SR-MPLS + SRv6", srv6=True, lo_base=LO_NET)
    p2 = core_router(lab, "p2", pl("p2"), 480, 60, 65000, 3, "P, SR-MPLS + SRv6", srv6=True, lo_base=LO_NET)
    pe2 = core_router(lab, "pe2", pl("pe2"), 700, 60, 65000, 4, "PE, SRv6 only (migrated)", srv6=True, lo_base=LO_NET)
    gw = core_router(lab, "gw", pl("gw"), 350, 240, 65000, 5, "PE, dual plane + SRv6/MPLS gateway", srv6=True, lo_base=LO_NET)
    for d in (p1, p2, gw):
        both(d)
    lab.p2p("pe1", "p1", v4net="10.0.101.0/30", note="SR-MPLS", isis=True, mpls=True)
    for k, (a, b) in enumerate((("p1", "p2"), ("p1", "gw"), ("p2", "gw")), 2):
        lab.p2p(a, b, v4net="10.0.%d.0/30" % (100 + k), v6net="2001:db8:0:%d::/64" % k, note="SR-MPLS + SRv6",
                isis=True, mpls=True)
    lab.p2p("p2", "pe2", v6net="2001:db8:0:5::/64", note="SRv6", isis=True)
    pe1.vrfs.append(Vrf("CUST-A", "65000:1", RT_VPN))
    pe2.vrfs.append(Vrf("CUST-A", "65000:4", RT_VPN, srv6=True))
    v = Vrf("CUST-A", "65000:5", RT_VPN, srv6=True, dual=True)
    v.stitch_rt = RT_VPN
    gw.vrfs.append(v)
    # The PEs of each plane only peer with the gateway, which re-originates routes from one
    # plane into the other (label <-> SRv6 SID). What works on XRd 26.2.1, found by trying
    # every combination: the plain `advertise ... re-originated` towards BOTH sides (the
    # `stitching-rt` variant advertised nothing), the PEs as route-reflector clients (the
    # re-originated paths are still iBGP-learned, so split horizon blocks them otherwise)
    # and next-hop-self. The import knob made no difference with a shared RT.
    pe1.nbr(gw.lo4, 65000, ["vpnv4"], desc="gw", lo=True)
    n = gw.nbr(pe1.lo4, 65000, ["vpnv4"], desc="pe1 (MPLS side)", lo=True, rrc=["vpnv4"], nhs=["vpnv4"])
    n.xr_af = ["import stitching-rt re-originate", "advertise vpnv4 unicast re-originated"]
    pe2.nbr(gw.lo6, 65000, ["vpnv4"], desc="gw", lo=True)
    n = gw.nbr(pe2.lo6, 65000, ["vpnv4"], desc="pe2 (SRv6 side)", lo=True, rrc=["vpnv4"], nhs=["vpnv4"])
    n.xr_af = ["import re-originate", "encapsulation-type srv6", "advertise vpnv4 unicast re-originated"]
    customer(lab, "ce1", pl("ce1"), -180, 0, 65101, 1, "pe1", "CUST-A").role = "CE, single-homed (SR-MPLS)"
    customer(lab, "ce2", pl("ce2"), 880, 0, 65102, 2, "pe2", "CUST-A").role = "CE, single-homed (SRv6)"
    ce3 = customer(lab, "ce3", pl("ce3"), 350, -100, 65103, 3, "pe1", "CUST-A")
    ce3.role = "CE, dual-homed to both planes"
    ia, ib = lab.p2p("pe2", "ce3", v4net="172.16.13.0/30", note="CUST-A ce3 second link", end_a={"vrf": "CUST-A"})
    ce3.nbr(_ip(ia.v4), 65000, ["ipv4"], desc="pe2")
    pe2.nbr(_ip(ib.v4), 65103, ["ipv4"], desc="ce3", vrf="CUST-A")
    customer(lab, "ce4", pl("ce4"), 350, 400, 65104, 4, "gw", "CUST-A").role = "CE on the dual-plane PE"
    return lab.files(README_MIG.format(name=name, preset=P["label"], ram=_gb(lab.ram_mb())))


PRESETS_MIG = {
    "xr": {"label": "IOS-XR provider, FRR CEs", "core": "xr", "gw": "xr", "ce": "frr"},
    # FRR as the legacy / migrated PEs next to an XR core does not work yet: the IPv6-only FRR
    # PE stays "Initializing" with XR (XR runs IPv6 as its own IS-IS topology and puts the
    # IPv4 AF on the v6-only link), and the FRR MPLS PE received nothing from the gateway.
}

README_MIG = """# {name} - SR-MPLS to SRv6 migration (L3VPN)

Preset: **{preset}** - about {ram} of RAM.

```
  ce1 --- pe1 (SR-MPLS) --- p1 ============= p2 --- pe2 (SRv6) --- ce2
     ce3 =/                  \\  both planes /             \\= ce3     ce3 dual-homed
                              \\            /
                               gw (dual plane + gateway) --- ce4
```

A network in the middle of moving its L3VPN from SR-MPLS to SRv6 uSID. One
IS-IS L2 instance carries both data planes side by side ("ships in the night"):
IPv4 with SR-MPLS prefix SIDs (SRGB 16000-23999) and IPv6 with SRv6 locators
(fc00:0:<n>::/48). The P routers and the gateway run both.

| Node | Plane | VPN routes carry |
|---|---|---|
| pe1 (10.255.0.1) | SR-MPLS only - not migrated yet | a VPN label |
| pe2 (2001:db8:0::4) | SRv6 only - already migrated | a uDT4 SID |
| gw (10.255.0.5 / 2001:db8:0::5) | both | label **and** SID (`mpls alloc enable` + `segment-routing srv6`) |

pe1 and pe2 cannot talk to each other directly - one only understands labels,
the other only SIDs. The **SRv6/MPLS L3 service interworking gateway** (gw)
peers with both (they are its route-reflector clients) and **re-originates**
VPN routes between them: pe2's routes leave towards pe1 with next hop gw and
gw's own VPN label, pe1's routes leave towards pe2 with gw's uDT4 SID. Traffic
between the planes is decapsulated in gw's VRF and re-encapsulated in the other
plane. The VPN keeps one RT (65000:100) on both planes; gw lists it a second time
as its *stitching* RT (`import stitching-rt re-originate` on the MPLS side,
`import re-originate` on the SRv6 side, `advertise vpnv4 unicast re-originated`
towards both).

Customers:
- **ce1** single-homed on the legacy MPLS PE, **ce2** single-homed on the SRv6 PE:
  they reach each other only through the gateway.
- **ce3** dual-homed to pe1 *and* pe2 - one leg on each plane. Each PE uses
  its local leg; the gateway sees both and keeps the site reachable if either
  leg fails.
- **ce4** on gw itself: a site on a dual-plane PE, reachable from both sides
  without any re-origination.

Migrating pe1 later means adding a locator and `segment-routing srv6` to its
VRF and IPv6 to its core link (it becomes dual plane like gw), then peering it
with pe2 directly - once every PE speaks SRv6 the gateway can be removed.

## Try it
- ce1: `ping 192.168.102.1 -I 192.168.101.1` - legacy site to migrated site, through gw
- ce2: `ping 192.168.104.1 -I 192.168.102.1` - SRv6 straight to gw's uDT4 SID
- gw: `show bgp vpnv4 unicast rd 65000:5 192.168.102.1/32 detail` - pe2's route, `reoriginated`, with gw's label and SID
- pe1: `show bgp vpnv4 unicast rd 65000:5 192.168.102.1/32` - ce2 behind gw, with an MPLS label
- pe2: `show bgp vpnv4 unicast rd 65000:5 192.168.101.1/32` - ce1 behind gw, with an SRv6 SID in fc00:0:5::
- gw: `show cef vrf CUST-A 192.168.102.1/32` - SRv6 H.Encaps.Red; `show cef vrf CUST-A 192.168.101.1/32` - labels
- fail pe1-ce3: ce1 still reaches ce3 over gw and pe2's SRv6 leg
"""


# --------------------------------------------------------------------------
# the catalogue: scenarios and the presets each one was verified with
# --------------------------------------------------------------------------
# verified = presets that were deployed and passed an end-to-end check (CE to CE
# ping through the service, plus the control-plane look in the README). A preset
# that is defined but not in `verified` is never offered.

SCENARIOS = [
    {"id": "interas-a", "family": "Inter-AS", "gen": inter_as_mpls, "args": ("A",), "presets": PRESETS_INTERAS,
     "title": "Inter-AS option A - back-to-back VRF (SR-MPLS)",
     "summary": "Two SR-MPLS provider networks joined VRF-to-VRF: the ASBRs treat each other as CEs on a "
                "dot1q sub-interface per VPN with plain eBGP. No labels cross the AS boundary.",
     "tags": ["Inter-AS", "option A", "SR-MPLS", "L3VPN"], "verified": ["frr", "xr-asbr", "xe-xr"]},
    {"id": "interas-b", "family": "Inter-AS", "gen": inter_as_mpls, "args": ("B",), "presets": PRESETS_INTERAS,
     "title": "Inter-AS option B - eBGP VPNv4 between ASBRs (SR-MPLS)",
     "summary": "One eBGP VPNv4 session between the ASBRs carries every VPN route; the ASBRs keep all "
                "route targets, set next-hop-self and swap the VPN label.",
     "tags": ["Inter-AS", "option B", "SR-MPLS", "VPNv4"], "verified": ["xr-asbr", "xe-xr"]},
    {"id": "interas-c", "family": "Inter-AS", "gen": inter_as_mpls, "args": ("C",), "presets": PRESETS_INTERAS,
     "title": "Inter-AS option C - multihop VPNv4, BGP-LU loopbacks (SR-MPLS)",
     "summary": "The ASBRs only swap PE loopbacks as BGP labelled unicast; the PEs peer VPNv4 multihop "
                "across both ASes. Three-label stack: SR transport, BGP-LU, VPN.",
     "tags": ["Inter-AS", "option C", "SR-MPLS", "BGP-LU", "RFC 8277"], "verified": ["xr-asbr", "xe-xr"]},
    {"id": "interas-srv6-a", "family": "Inter-AS", "gen": inter_as_srv6, "args": ("A",),
     "presets": PRESETS_INTERAS_SRV6,
     "title": "Inter-AS option A over SRv6 uSID",
     "summary": "Two IPv6-only SRv6 uSID cores with per-VRF uDT4 SIDs, joined back-to-back VRF on a "
                "dot1q sub-interface.",
     "tags": ["Inter-AS", "option A", "SRv6", "uSID"], "verified": ["frr", "xr"]},
    {"id": "interas-srv6-c", "family": "Inter-AS", "gen": inter_as_srv6, "args": ("C",),
     "presets": PRESETS_INTERAS_SRV6,
     "title": "Inter-AS SRv6 L3VPN - locator exchange (option C style)",
     "summary": "The ASBRs trade locators and loopbacks over eBGP IPv6 and redistribute them into IS-IS; "
                "the PEs peer VPNv4 multihop and encapsulate straight to the remote PE's uDT4 SID.",
     "tags": ["Inter-AS", "SRv6", "uSID", "multihop VPNv4"], "verified": ["frr", "xr"]},
    {"id": "csc-mpls", "family": "Carrier supporting Carrier", "gen": csc_mpls, "args": (), "presets": PRESETS_CSC,
     "title": "Carrier supporting Carrier over SR-MPLS",
     "summary": "A backbone carrier sells a labelled VPN (eBGP-LU in VRF CARRIER) to a customer carrier, "
                "which runs its own L3VPN on top: four labels deep in the backbone core.",
     "tags": ["CsC", "SR-MPLS", "BGP-LU", "L3VPN"], "verified": ["xr-bb"]},
    {"id": "csc-srv6", "family": "Carrier supporting Carrier", "gen": csc_srv6, "args": (),
     "presets": PRESETS_CSC_SRV6,
     "title": "Carrier supporting Carrier over SRv6",
     "summary": "The backbone sells an IPv6 VPN (uDT6); the customer carrier's own SRv6 L3VPN rides "
                "inside it - two SRv6 encapsulations, no labels anywhere.",
     "tags": ["CsC", "SRv6", "uSID", "uDT6"], "verified": ["frr", "xr-bb"]},
    {"id": "sr-ldp", "family": "Other SP designs", "gen": sr_ldp, "args": (), "presets": PRESETS_SRLDP,
     "title": "SR-MPLS / LDP interworking (mapping server)",
     "summary": "Half-way through an LDP-to-SR migration: an SR-only PE, an LDP-only PE, and a border P "
                "that is the SR mapping server and stitches SR to LDP both ways.",
     "tags": ["SR-MPLS", "LDP", "mapping server", "migration"], "verified": ["xr-frr", "xr"]},
    {"id": "flex-algo", "family": "Other SP designs", "gen": flex_algo, "args": (), "presets": PRESETS_FLEX,
     "title": "Flex-Algo 128 low-delay slice with on-demand SR-TE",
     "summary": "IS-IS Flex-Algo 128 on delay next to the normal topology; one VRF is coloured 128 and "
                "rides an on-demand SR policy on the low-delay slice.",
     "tags": ["Flex-Algo", "SR-TE", "ODN", "delay"], "verified": ["xr"]},
    {"id": "6pe", "family": "Other SP designs", "gen": sixpe, "args": (), "presets": PRESETS_6PE,
     "title": "6PE and 6VPE over an IPv4 SR-MPLS core",
     "summary": "IPv6 customers across an IPv4-only core: global IPv6 as labelled unicast (6PE) and a "
                "dual-stack VRF via VPNv6 (6VPE).",
     "tags": ["6PE", "6VPE", "IPv6", "SR-MPLS"], "verified": ["xr"]},
    {"id": "evpn-vpws", "family": "Other SP designs", "gen": evpn_vpws, "args": (), "presets": PRESETS_VPWS,
     "title": "EVPN-VPWS point-to-point service",
     "summary": "An Ethernet pseudowire signalled by BGP EVPN route type 1 across an SR-MPLS core; the "
                "two CEs share one subnet.",
     "tags": ["EVPN", "VPWS", "L2VPN", "SR-MPLS"], "verified": ["xr"]},
    {"id": "srmpls-srv6-migration", "family": "Migration", "gen": migration, "args": (), "presets": PRESETS_MIG,
     "title": "SR-MPLS to SRv6 migration - L3VPN with an interworking gateway",
     "summary": "A legacy SR-MPLS PE, a migrated SRv6 PE and a dual-plane PE that stitches VPN routes between "
                "them; single-homed sites on each plane and a site dual-homed across both.",
     "tags": ["SRv6", "SR-MPLS", "migration", "interworking gateway", "dual-homed"], "verified": ["xr"]},
    {"id": "rr-addpath", "family": "L3VPN designs", "gen": rr_addpath, "args": (), "presets": PRESETS_RR,
     "title": "SR-MPLS L3VPN - redundant route reflectors + BGP add-path",
     "summary": "Two out-of-path VPNv4 route reflectors, a dual-homed CE and one RD for the VPN: add-path "
                "gets both exits to the remote PE, which keeps the second as a PIC edge backup.",
     "tags": ["SR-MPLS", "L3VPN", "route reflector", "add-path", "PIC edge"], "verified": ["frr", "xr", "xr-frr-rr", "xe-xr"]},
    {"id": "mcast-static-rp", "family": "Multicast", "gen": mcast_ipv4, "args": ("static",),
     "presets": PRESETS_MCAST,
     "title": "IPv4 multicast - PIM-SM with a static RP, plus SSM",
     "summary": "Plain IPv4, no MPLS: IS-IS unicast, PIM sparse mode with a static RP for 239/8 and "
                "source-specific multicast for 232/8, FRR hosts as source and receivers.",
     "tags": ["PIM-SM", "SSM", "IGMPv3", "RP"], "verified": ["frr", "xe", "xe-frr"]},
    {"id": "mcast-anycast-rp", "family": "Multicast", "gen": mcast_ipv4, "args": ("anycast",),
     "presets": PRESETS_MCAST,
     "title": "IPv4 multicast - anycast RP with MSDP",
     "summary": "Two RPs share one address and exchange Source-Active messages over MSDP: RP redundancy "
                "and load sharing without an election.",
     "tags": ["PIM-SM", "anycast RP", "MSDP"], "verified": ["frr", "xe", "xe-frr"]},
]

_DUMMY_IMAGES = {v: "x" for v in IMAGE_SLOT.values()}


def scenario(sid):
    return next((s for s in SCENARIOS if s["id"] == sid), None)


def describe(sc, preset):
    """What a preset needs and looks like: image slots with node counts, RAM, the drawing."""
    files = sc["gen"](*(sc["args"] + ("preview", _DUMMY_IMAGES, "172.31.0.0/24", preset)))
    lab = files.lab
    kinds = {}
    for d in lab.devs.values():
        k = IMAGE_SLOT[d.plat]
        kinds[k] = kinds.get(k, 0) + 1
    return {"id": preset, "label": sc["presets"][preset]["label"], "kinds": kinds, "est_ram_mb": lab.ram_mb(),
            "nodes": len(lab.devs), "links": len(lab.links),
            "graph": {"nodes": [{"n": d.name, "x": d.x, "y": d.y, "k": IMAGE_SLOT[d.plat], "role": d.role}
                                for d in lab.devs.values()],
                      "links": [[a, b] for a, _, b, _, _ in lab.links]}}


def generate(sid, preset, name, images, mgmt):
    sc = scenario(sid)
    if sc is None or preset not in sc["verified"]:
        raise ValueError("no such scenario / preset")
    files = sc["gen"](*(sc["args"] + (name, images, mgmt, preset)))
    out = dict(files)
    g = scenario_guides.guide_json(sid, preset, sc["presets"][preset]["label"], files.lab)
    if g:
        out["lab-guide.json"] = g           # read by the topology view's lab guide (guide.py)
    return out


def selftest():
    """Every scenario x preset renders a topology that parses, with no port used twice, every
    link end a real node, every startup-config file present and no address configured twice."""
    import collections
    import re
    import yaml
    img = {k: "img/%s:1" % k for k in IMAGE_SLOT.values()}
    problems = []
    for sc in SCENARIOS:
        for pid in sc["presets"]:
            name = "t-" + sc["id"]
            f = sc["gen"](*(sc["args"] + (name, img, "172.30.1.0/24", pid)))
            topo = yaml.safe_load(f["%s.clab.yml" % name])["topology"]
            eps = [e for link in topo["links"] for e in link["endpoints"]]
            ips = collections.Counter(m.group(1) for p, t in f.items() if p.startswith("configs/")
                                      for m in re.finditer(r"ip(?:v4)? address (\d+\.\d+\.\d+\.\d+)", t))
            errs = ["port used twice: %s" % e for e, n in collections.Counter(eps).items() if n > 1]
            errs += ["unknown node in link: %s" % e for e in eps if e.split(":")[0] not in topo["nodes"]]
            errs += ["missing %s" % v["startup-config"] for v in topo["nodes"].values()
                     if v.get("startup-config") and v["startup-config"] not in f]
            anycast = {a for d in f.lab.devs.values() for a in d.lo_extra}
            errs += ["address used twice: %s" % ip for ip, n in ips.items()
                     if n > 1 and not ip.startswith("172.30.1.") and ip not in anycast]
            problems += ["%s/%s: %s" % (sc["id"], pid, e) for e in errs]
            if pid in sc["verified"]:
                g = json.loads(generate(sc["id"], pid, name, img, "172.30.1.0/24")["lab-guide.json"])
                gerr = ["guide: unknown node %s" % c["node"] for c in g["checks"] if c["node"] not in topo["nodes"]]
                gerr += ["guide: %s has no role text" % r["node"] for r in g["roles"] if not r["text"]]
                gerr += ["guide: bad regex in %s" % c["title"] for c in g["checks"] if c.get("expect") and not _re_ok(c["expect"])]
                gerr += ["guide: no checks"] if not g["checks"] else []
                problems += ["%s/%s: %s" % (sc["id"], pid, e) for e in gerr]
    return problems


def _re_ok(rx):
    import re
    try:
        re.compile(rx)
        return True
    except re.error:
        return False


if __name__ == "__main__":
    import sys
    bad = selftest()
    print("\n".join(bad) or "all scenarios and presets render cleanly")
    sys.exit(1 if bad else 0)
