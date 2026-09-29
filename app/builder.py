#!/usr/bin/env python3
"""
Topology builder - turns a graph drawn in the browser into a containerlab
topology plus ready-to-load startup configs.

Everything here is pure: given a spec dict it returns filenames and contents.
The caller decides where (and whether) to write them, which keeps preview and
save on exactly the same code path.
"""

import ipaddress
import re
import subprocess
import sysbin

DOCKER = sysbin.find("docker")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
# Diagram icons the dashboard draws (static/node-icons.js). Written as clab's
# own `graph-icon` label, so the topology map shows what the builder showed.
ICONS = ("router", "switch", "server", "firewall", "cloud", "trafficgen")

# --------------------------------------------------------------------------
# what we know how to drive
# --------------------------------------------------------------------------
# if_style:  how ethN maps to the platform's interface name
# configure: can we generate a working startup config for it
KINDS = {
    "cisco_c8000v": {
        "os": "IOS-XE", "vendor": "Cisco", "if_style": "xe", "configure": True,
        "mgmt_in_config": False, "ram_mb": 4096,
        "env": {"USERNAME": "clab", "PASSWORD": "clab@123"},
    },
    "cisco_csr1000v": {
        "os": "IOS-XE", "vendor": "Cisco", "if_style": "xe", "configure": True,
        "mgmt_in_config": False, "ram_mb": 4096,
        "env": {"USERNAME": "clab", "PASSWORD": "clab@123"},
    },
    "cisco_xrd_vrouter": {
        "os": "IOS-XR", "vendor": "Cisco", "if_style": "xr", "configure": True,
        # XRd is tc-redirected onto the clab mgmt bridge, so the address has to
        # be in the config as well or the node comes up unmanageable.
        "mgmt_in_config": True, "ram_mb": 8192,
        "env": {"VCPU": "2", "RAM": "8192", "XRD_NIC_TYPE": "igb",
                "PASSWORD": "clab@123"},
    },
    "cisco_n9kv": {
        # vrnetlab n9kv: the launcher applies its own bootstrap (hostname, user,
        # mgmt0 in VRF management) and then our file, line by line, in config mode
        "os": "NX-OS", "vendor": "Cisco", "if_style": "nxos", "configure": True,
        "mgmt_in_config": False, "ram_mb": 10240, "services": False,
        "env": {"USERNAME": "clab", "PASSWORD": "clab@123"},
    },
    "frr": {
        # FRRouting in a plain linux container: containerlab kind linux, with
        # the daemons file and frr.conf bind-mounted from configs/. Addresses,
        # IGP, SR-MPLS, BGP and L3VPN all come from frr.conf; exec: lines only
        # set what FRR cannot (MTU, MPLS input, VRF devices).
        "os": "FRR", "vendor": "FRRouting", "if_style": "eth", "configure": True,
        "clab_kind": "linux", "mgmt_in_config": False, "ram_mb": 256, "env": {},
    },
    "nokia_srlinux": {
        # clab applies a .cli startup file as SR Linux CLI commands after boot.
        # The free container types (7220 IXR-D2L/D3L) have no MPLS, so SR-MPLS
        # and L3VPN are refused; addressing, IS-IS/OSPF and BGP are generated.
        "os": "SR Linux", "vendor": "Nokia", "if_style": "srl", "configure": True,
        "port": "e1-%d", "mgmt_in_config": False, "ram_mb": 2048, "services": "bgp",
        "env": {},
    },
    "linux": {
        "os": "Linux", "vendor": "-", "if_style": "eth", "configure": "exec",
        "mgmt_in_config": False, "ram_mb": 256, "env": {},
    },
}


def clab_kind(kind):
    """The containerlab kind a builder kind is written as."""
    return KINDS.get(kind, {}).get("clab_kind") or kind


def port_name(kind, eth_index):
    """The link endpoint name containerlab expects for data port N."""
    return (KINDS.get(kind, {}).get("port") or "eth%d") % eth_index

# repository name -> kind, plus anything worth warning about before you pick it
IMAGE_HINTS = [
    (r"vrnetlab/cisco_c8000v",     "cisco_c8000v",      None),
    (r"vrnetlab/cisco_n9kv",       "cisco_n9kv",
     "NX-OS: about 10 GB RAM and 5-8 min to boot per node; addressing + IS-IS/OSPF "
     "templates (no SR/BGP/L3VPN template yet)"),
    (r"vrnetlab/cisco_csr1000v",   "cisco_csr1000v",
     "IOS-XE 16.09 - SSH offers only SHA-1 kex and an ssh-rsa host key"),
    (r"vrnetlab/cisco_xrd-vrouter", "cisco_xrd_vrouter",
     "8 GiB per node is a hard floor in the launcher, whatever RAM you set"),
    (r"^ios-xr/xrd-vrouter",       None,
     "raw Cisco image - XRd refuses containerlab's linux interface names; "
     "use the vrnetlab/cisco_xrd-vrouter build instead"),
    (r"frrouting/frr",             "frr",
     "FRR: addressing, IS-IS/OSPF, SR-MPLS, BGP and L3VPN templates"),
    (r"nokia/srlinux",             "nokia_srlinux",
     "SR Linux: addressing, IS-IS/OSPF and BGP templates (no MPLS in the free container)"),
    (r"^alpine",                   "linux",             None),
    (r"^ubuntu|^debian",           "linux",             None),
    (r"traffic-generator",         "linux",             None),
    (r"sonic",                     None,                "SoNiC - no config template here"),
]


def list_images():
    """Local docker images, annotated with the containerlab kind we'd use."""
    try:
        p = subprocess.run(
            [DOCKER, "images", "--format", "{{.Repository}}\t{{.Tag}}\t{{.Size}}"],
            capture_output=True, text=True, timeout=30)
        raw = p.stdout if p.returncode == 0 else ""
    except Exception:                                   # noqa: BLE001
        raw = ""

    out = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        repo, tag, size = (x.strip() for x in parts)
        if repo == "<none>" or tag == "<none>":
            continue
        kind, note = None, None
        for pattern, k, n in IMAGE_HINTS:
            if re.search(pattern, repo):
                kind, note = k, n
                break
        meta = KINDS.get(kind or "", {})
        out.append({
            "image": "%s:%s" % (repo, tag),
            "repo": repo, "tag": tag, "size": size,
            "kind": kind,
            "os": meta.get("os", "unknown"),
            "vendor": meta.get("vendor", "-"),
            "ram_mb": meta.get("ram_mb", 512),
            # True = full startup config (interfaces + IGP). "exec" kinds
            # only get addresses put on their interfaces, no routing.
            "configurable": meta.get("configure") is True,
            "addressing_only": meta.get("configure") == "exec",
            "supported": kind is not None,
            "note": note,
        })
    out.sort(key=lambda i: (not i["supported"], i["repo"]))
    return out


# --------------------------------------------------------------------------
# interface naming
# --------------------------------------------------------------------------

def iface_name(style, eth_index):
    """eth_index is 1-based: eth1 is the first data interface."""
    if style == "xe":
        return "GigabitEthernet%d" % (eth_index + 1)
    if style == "xr":
        return "GigabitEthernet0/0/0/%d" % (eth_index - 1)
    if style == "nxos":
        return "Ethernet1/%d" % eth_index
    if style == "srl":
        return "ethernet-1/%d" % eth_index
    return "eth%d" % eth_index


# --------------------------------------------------------------------------
# config templates
# --------------------------------------------------------------------------
# Every template is driven by per-node facts worked out in generate():
#   node["run_igp"]  does this router take part in the core IGP
#   node["sid"]      prefix-SID index, or None when SR is off
#   node["vrfs"]     VRFs this PE carries: [{name, rd, rt}]
#   node["bgp"]      None, or {asn, ibgp: [...], ebgp: [...], ...}
#   itf["igp"]       "active" | "passive" | None (interface not in the IGP)
#   itf["vrf"]       VRF the interface belongs to, or None
# A spec with no services sets run_igp everywhere and leaves the rest empty,
# which renders exactly what the builder produced before services existed.

def isis_net(area, index):
    """49.<area>.0100.0000.<idx>.00 - system id derived from the node index."""
    return "%s.0100.0000.%04d.00" % (area, index)


def _bgp_xe(node):
    b = node["bgp"]
    L = ["router bgp %d" % b["asn"],
         " bgp router-id %s" % node["loopback"],
         " bgp log-neighbor-changes",
         " no bgp default ipv4-unicast"]
    for n in b["ibgp"]:
        L.append(" neighbor %s remote-as %d" % (n["ip"], b["asn"]))
        L.append(" neighbor %s description iBGP %s" % (n["ip"], n["peer"]))
        L.append(" neighbor %s update-source Loopback0" % n["ip"])
    glob_ebgp = [e for e in b["ebgp"] if not e["vrf"]]
    for e in glob_ebgp:
        L.append(" neighbor %s remote-as %d" % (e["ip"], e["asn"]))
        L.append(" neighbor %s description eBGP %s" % (e["ip"], e["peer"]))
    L.append(" !")
    if b["af_ipv4"]:
        L.append(" address-family ipv4")
        for net, mask in b["networks"]:
            L.append("  network %s mask %s" % (net, mask))
        for n in b["ibgp"]:
            L.append("  neighbor %s activate" % n["ip"])
            if n["rr_client"]:
                L.append("  neighbor %s route-reflector-client" % n["ip"])
            if b["next_hop_self"]:
                L.append("  neighbor %s next-hop-self" % n["ip"])
        for e in glob_ebgp:
            L.append("  neighbor %s activate" % e["ip"])
        L += [" exit-address-family", " !"]
    if b["af_vpnv4"]:
        L.append(" address-family vpnv4")
        for n in b["ibgp"]:
            L.append("  neighbor %s activate" % n["ip"])
            L.append("  neighbor %s send-community extended" % n["ip"])
            if n["rr_client"]:
                L.append("  neighbor %s route-reflector-client" % n["ip"])
        L += [" exit-address-family", " !"]
    for v in node.get("vrfs", []):
        L.append(" address-family ipv4 vrf %s" % v["name"])
        L.append("  redistribute connected")
        for e in b["ebgp"]:
            if e["vrf"] != v["name"]:
                continue
            L.append("  neighbor %s remote-as %d" % (e["ip"], e["asn"]))
            L.append("  neighbor %s description eBGP %s" % (e["ip"], e["peer"]))
            L.append("  neighbor %s activate" % e["ip"])
            # lets two sites of one customer share an AS number
            L.append("  neighbor %s as-override" % e["ip"])
        L += [" exit-address-family", " !"]
    L.append("!")
    return L


def cfg_iosxe(node, igp, opts):
    run_igp = node.get("run_igp", True)
    sid = node.get("sid")
    L = []
    L.append("hostname %s" % node["name"])
    L.append("!")
    L.append("no ip domain lookup")
    L.append("ip cef")
    L.append("!")
    for v in node.get("vrfs", []):
        L += ["vrf definition %s" % v["name"],
              " rd %s" % v["rd"],
              " !",
              " address-family ipv4",
              "  route-target export %s" % v["rt"],
              "  route-target import %s" % v["rt"],
              " exit-address-family",
              "!"]
    if sid is not None:
        # The SID rides on the loopback through the global prefix-SID map;
        # `range 1` is mandatory on IOS-XE.
        L += ["segment-routing mpls",
              " global-block %d %d" % (opts["srgb_lo"], opts["srgb_hi"]),
              " !",
              " connected-prefix-sid-map",
              "  address-family ipv4",
              "   %s/32 index %d range 1" % (node["loopback"], sid),
              "  exit-address-family",
              " !",
              "!"]
    L.append("interface Loopback0")
    L.append(" description router-id")
    L.append(" ip address %s 255.255.255.255" % node["loopback"])
    if run_igp and igp == "isis":
        L.append(" ip router isis %s" % opts["igp_tag"])
    elif run_igp and igp == "ospf":
        L.append(" ip ospf %s area %s" % (opts["ospf_pid"], opts["ospf_area"]))
    L.append("!")
    for itf in node["ifaces"]:
        L.append("interface %s" % itf["name"])
        L.append(" description to %s" % itf["peer"])
        if itf.get("vrf"):
            L.append(" vrf forwarding %s" % itf["vrf"])
        L.append(" mtu %d" % opts["mtu"])
        L.append(" ip address %s %s" % (itf["ip"], itf["netmask"]))
        in_igp = run_igp and itf.get("igp", "active") is not None
        if igp == "isis" and in_igp:
            L.append(" ip router isis %s" % opts["igp_tag"])
            if itf["igp_active"]:
                L.append(" isis network point-to-point")
        elif igp == "ospf" and in_igp:
            L.append(" ip ospf %s area %s" % (opts["ospf_pid"], opts["ospf_area"]))
            if itf["igp_active"]:
                L.append(" ip ospf network point-to-point")
        L.append(" no shutdown")
        L.append("!")
    passives = [i["name"] for i in node["ifaces"] if i.get("igp", "passive") == "passive"
                and not i["igp_active"]]
    if run_igp and igp == "isis":
        L += ["router isis %s" % opts["igp_tag"],
              " net %s" % node["net"],
              " is-type level-2-only",
              " metric-style wide",
              " log-adjacency-changes"]
        if sid is not None:
            L.append(" segment-routing mpls")
        L.append(" passive-interface Loopback0")
        L += [" passive-interface %s" % i for i in passives]
        L.append("!")
    elif run_igp and igp == "ospf":
        L += ["router ospf %s" % opts["ospf_pid"],
              " router-id %s" % node["loopback"],
              " log-adjacency-changes",
              " passive-interface Loopback0"]
        L += [" passive-interface %s" % i for i in passives]
        L.append("!")
    if node.get("bgp"):
        L += _bgp_xe(node)
    L.append("end")
    return "\n".join(L) + "\n"


def _bgp_xr(node):
    b = node["bgp"]
    L = ["router bgp %d" % b["asn"],
         " bgp router-id %s" % node["loopback"]]
    if b["af_ipv4"]:
        L.append(" address-family ipv4 unicast")
        for net, mask in b["networks"]:
            L.append("  network %s/%d" % (net, _prefixlen(mask)))
        L.append(" !")
    if b["af_vpnv4"]:
        L += [" address-family vpnv4 unicast", " !"]
    for n in b["ibgp"]:
        L += [" neighbor %s" % n["ip"],
              "  remote-as %d" % b["asn"],
              "  description iBGP %s" % n["peer"],
              "  update-source Loopback0"]
        if b["af_ipv4"]:
            L.append("  address-family ipv4 unicast")
            if n["rr_client"]:
                L.append("   route-reflector-client")
            if b["next_hop_self"]:
                L.append("   next-hop-self")
            L.append("  !")
        if b["af_vpnv4"]:
            L.append("  address-family vpnv4 unicast")
            if n["rr_client"]:
                L.append("   route-reflector-client")
            L.append("  !")
        L.append(" !")
    # XR accepts and advertises nothing to an eBGP peer without a policy.
    for e in b["ebgp"]:
        if e["vrf"]:
            continue
        L += [" neighbor %s" % e["ip"],
              "  remote-as %d" % e["asn"],
              "  description eBGP %s" % e["peer"],
              "  address-family ipv4 unicast",
              "   route-policy PASS in",
              "   route-policy PASS out",
              "  !",
              " !"]
    for v in node.get("vrfs", []):
        L += [" vrf %s" % v["name"],
              "  rd %s" % v["rd"],
              "  address-family ipv4 unicast",
              "   redistribute connected",
              "  !"]
        for e in b["ebgp"]:
            if e["vrf"] != v["name"]:
                continue
            L += ["  neighbor %s" % e["ip"],
                  "   remote-as %d" % e["asn"],
                  "   description eBGP %s" % e["peer"],
                  "   address-family ipv4 unicast",
                  "    route-policy PASS in",
                  "    route-policy PASS out",
                  "    as-override",
                  "   !",
                  "  !"]
        L.append(" !")
    L.append("!")
    return L


def _prefixlen(mask):
    return ipaddress.ip_network("0.0.0.0/%s" % mask).prefixlen


def cfg_iosxr(node, igp, opts):
    run_igp = node.get("run_igp", True)
    sid = node.get("sid")
    L = []
    L.append("hostname %s" % node["name"])
    L.append("logging console disable")
    L.append("!")
    L.append("line default")
    L.append(" transport input ssh")
    L.append("!")
    L.append("ssh server v2")
    L.append("ssh server vrf default")
    L.append("!")
    for v in node.get("vrfs", []):
        L += ["vrf %s" % v["name"],
              " address-family ipv4 unicast",
              "  import route-target",
              "   %s" % v["rt"],
              "  !",
              "  export route-target",
              "   %s" % v["rt"],
              "  !",
              " !",
              "!"]
    if node.get("bgp") and node["bgp"]["ebgp"]:
        L += ["route-policy PASS", "  pass", "end-policy", "!"]
    # Without this the node is unreachable on the clab bridge.
    L.append("interface MgmtEth0/RP0/CPU0/0")
    L.append(" ipv4 address %s %s" % (node["mgmt_ip"], node["mgmt_mask"]))
    L.append(" no shutdown")
    L.append("!")
    L.append("interface Loopback0")
    L.append(" ipv4 address %s 255.255.255.255" % node["loopback"])
    L.append("!")
    for itf in node["ifaces"]:
        L.append("interface %s" % itf["name"])
        L.append(" description to %s" % itf["peer"])
        # XR's mtu counts the 14-byte Ethernet header, IOS-XE's does not.
        L.append(" mtu %d" % min(opts["mtu"] + 14, 9216))
        if itf.get("vrf"):
            L.append(" vrf %s" % itf["vrf"])
        L.append(" ipv4 address %s %s" % (itf["ip"], itf["netmask"]))
        L.append(" no shutdown")
        L.append("!")
    if sid is not None:
        # XRd 26.2.1 rejects the `segment-routing / mpls / global-block` form
        # at startup (it lands in `show configuration failed startup`); the
        # SRGB belongs directly under segment-routing.
        L += ["segment-routing",
              " global-block %d %d" % (opts["srgb_lo"], opts["srgb_hi"]),
              "!"]
    igp_ifaces = [i for i in node["ifaces"] if i.get("igp", "active") is not None]
    if run_igp and igp == "isis":
        L += ["router isis %s" % opts["igp_tag"],
              " is-type level-2-only",
              " net %s" % node["net"],
              " log adjacency changes",
              " address-family ipv4 unicast",
              "  metric-style wide"]
        if sid is not None:
            L.append("  segment-routing mpls")
        L += [" !",
              " interface Loopback0",
              "  passive",
              "  address-family ipv4 unicast"]
        if sid is not None:
            L.append("   prefix-sid index %d" % sid)
        L += ["  !",
              " !"]
        for itf in igp_ifaces:
            L.append(" interface %s" % itf["name"])
            L.append("  passive" if not itf["igp_active"] else "  point-to-point")
            L += ["  address-family ipv4 unicast", "  !", " !"]
        L.append("!")
    elif run_igp and igp == "ospf":
        L += ["router ospf %s" % opts["ospf_pid"],
              " router-id %s" % node["loopback"],
              " area %s" % opts["ospf_area"],
              "  interface Loopback0",
              "   passive enable",
              "  !"]
        for itf in igp_ifaces:
            L.append("  interface %s" % itf["name"])
            L.append("   passive enable" if not itf["igp_active"]
                     else "   network point-to-point")
            L.append("  !")
        L += [" !", "!"]
    if node.get("bgp"):
        L += _bgp_xr(node)
    # XR needs an explicit commit; bare `end` leaves it prompting about
    # uncommitted changes.
    L.append("commit")
    L.append("end")
    return "\n".join(L) + "\n"


def linux_exec(node, opts):
    """clab exec lines that put the addresses on a plain linux node."""
    cmds = []
    for itf in node["ifaces"]:
        cmds.append("ip addr add %s/%d dev %s" % (itf["ip"], itf["prefix"], itf["name"]))
        cmds.append("ip link set %s up" % itf["name"])
    cmds.append("ip addr add %s/32 dev lo" % node["loopback"])
    # reach the rest of the lab through the first router it hangs off; the
    # default route stays on the management network
    gw = next((i for i in node["ifaces"] if i.get("far_router")), None)
    if gw and opts.get("pools"):
        peer = gw.get("peer_ip")
        if peer:
            for pool in opts["pools"]:
                cmds.append("ip route add %s via %s" % (pool, peer))
    return cmds


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

class SpecError(Exception):
    pass


def _need(cond, msg):
    if not cond:
        raise SpecError(msg)


ROLES = ("P", "PE", "CE", "RR")
BGP_MODES = ("off", "full-mesh", "rr")
VRF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def guess_role(name):
    for prefix, role in (("ce", "CE"), ("pe", "PE"), ("rr", "RR")):
        if name.startswith(prefix):
            return role
    return "P"


def cfg_nxos(node, igp, opts):
    """NX-OS: addressing and the IGP. The launcher already configured hostname,
    the user and mgmt0; these lines are applied after that, in config mode."""
    tag, pid, area = opts["igp_tag"], opts["ospf_pid"], opts["ospf_area"]
    L = ["hostname %s" % node["name"]]
    # containerlab starts n9kv as admin/admin whatever the topology says; add
    # the lab's usual login so ssh works the same on every node
    env = node["meta"].get("env") or {}
    if env.get("USERNAME") and env.get("PASSWORD"):
        L.append("username %s password 0 %s role network-admin" % (env["USERNAME"], env["PASSWORD"]))
    if igp == "isis":
        L.append("feature isis")
    elif igp == "ospf":
        L.append("feature ospf")
    # the routing process first: interfaces refer to it
    if igp == "isis":
        L += ["router isis %s" % tag, "  net %s" % node["net"], "  is-type level-2",
              "  log-adjacency-changes"]
    elif igp == "ospf":
        L += ["router ospf %s" % pid, "  router-id %s" % node["loopback"],
              "  log-adjacency-changes"]
    L += ["interface loopback0", "  description router-id",
          "  ip address %s/32" % node["loopback"]]
    if igp == "isis":
        L.append("  ip router isis %s" % tag)
    elif igp == "ospf":
        L.append("  ip router ospf %s area %s" % (pid, area))
    L.append("  no shutdown")
    for itf in node["ifaces"]:
        L += ["interface %s" % itf["name"], "  description to %s" % itf["peer"],
              "  no switchport", "  mtu %d" % opts["mtu"],
              "  ip address %s/%d" % (itf["ip"], itf["prefix"])]
        if igp == "isis":
            L.append("  ip router isis %s" % tag)
            if itf["igp_active"]:
                L.append("  isis network point-to-point")
            else:
                L.append("  isis passive-interface level-1-2")
        elif igp == "ospf":
            L.append("  ip router ospf %s area %s" % (pid, area))
            if itf["igp_active"]:
                L.append("  ip ospf network point-to-point")
            else:
                L.append("  ip ospf passive-interface")
        L.append("  no shutdown")
    return "\n".join(L) + "\n"


FRR_DAEMONS = ("zebra", "bgpd", "ospfd", "ospf6d", "ripd", "ripngd", "isisd", "pimd",
               "ldpd", "nhrpd", "eigrpd", "babeld", "sharpd", "staticd", "pbrd", "bfdd",
               "fabricd", "pathd")


def frr_daemons(node, igp):
    """/etc/frr/daemons: only what this node's frr.conf needs."""
    on = {"zebra", "staticd", "bfdd"}
    if node.get("run_igp") and igp == "isis":
        on.add("isisd")
    if node.get("run_igp") and igp == "ospf":
        on.add("ospfd")
    if node.get("bgp"):
        on.add("bgpd")
    L = ["# written by the containerlab dashboard builder"]
    L += ["%s=%s" % (d, "yes" if d in on else "no") for d in FRR_DAEMONS]
    L += ["vtysh_enable=yes",
          'zebra_options="  -A 127.0.0.1 -s 90000000"']
    L += ['%s_options="  -A 127.0.0.1"' % d for d in FRR_DAEMONS if d != "zebra"]
    L.append('frr_profile="traditional"')
    return "\n".join(L) + "\n"


def _bgp_frr(node):
    b = node["bgp"]
    L = ["router bgp %d" % b["asn"],
         " bgp router-id %s" % node["loopback"],
         " bgp log-neighbor-changes",
         " no bgp default ipv4-unicast",
         # the builder's eBGP sessions carry no policy, like the IOS templates
         " no bgp ebgp-requires-policy"]
    for n in b["ibgp"]:
        L += [" neighbor %s remote-as %d" % (n["ip"], b["asn"]),
              " neighbor %s description iBGP %s" % (n["ip"], n["peer"]),
              " neighbor %s update-source lo" % n["ip"]]
    glob_ebgp = [e for e in b["ebgp"] if not e["vrf"]]
    for e in glob_ebgp:
        L += [" neighbor %s remote-as %d" % (e["ip"], e["asn"]),
              " neighbor %s description eBGP %s" % (e["ip"], e["peer"])]
    L.append(" !")
    if b["af_ipv4"]:
        L.append(" address-family ipv4 unicast")
        for net, mask in b["networks"]:
            L.append("  network %s/%d" % (net, _prefixlen(mask)))
        for n in b["ibgp"]:
            L.append("  neighbor %s activate" % n["ip"])
            if n["rr_client"]:
                L.append("  neighbor %s route-reflector-client" % n["ip"])
            if b["next_hop_self"]:
                L.append("  neighbor %s next-hop-self" % n["ip"])
        for e in glob_ebgp:
            L.append("  neighbor %s activate" % e["ip"])
        L += [" exit-address-family", " !"]
    if b["af_vpnv4"]:
        L.append(" address-family ipv4 vpn")
        for n in b["ibgp"]:
            L.append("  neighbor %s activate" % n["ip"])
            if n["rr_client"]:
                L.append("  neighbor %s route-reflector-client" % n["ip"])
        L += [" exit-address-family", " !"]
    L.append("exit")
    L.append("!")
    for v in node.get("vrfs", []):
        L += ["router bgp %d vrf %s" % (b["asn"], v["name"]),
              " bgp router-id %s" % node["loopback"],
              " no bgp ebgp-requires-policy"]
        mine = [e for e in b["ebgp"] if e["vrf"] == v["name"]]
        for e in mine:
            L += [" neighbor %s remote-as %d" % (e["ip"], e["asn"]),
                  " neighbor %s description eBGP %s" % (e["ip"], e["peer"])]
        L += [" !", " address-family ipv4 unicast", "  redistribute connected"]
        for e in mine:
            L += ["  neighbor %s activate" % e["ip"],
                  "  neighbor %s as-override" % e["ip"]]
        L += ["  label vpn export auto",
              "  rd vpn export %s" % v["rd"],
              "  rt vpn both %s" % v["rt"],
              "  export vpn",
              "  import vpn",
              " exit-address-family",
              "exit",
              "!"]
    return L


def cfg_frr(node, igp, opts):
    """frr.conf. Addresses are FRR's too: zebra puts them on the interfaces."""
    run_igp = node.get("run_igp", True)
    sid = node.get("sid")
    tag = opts["igp_tag"]
    L = ["frr version 10",
         "frr defaults traditional",
         "hostname %s" % node["name"],
         "log stdout informational",
         "service integrated-vtysh-config",
         "!"]
    for v in node.get("vrfs", []):
        L += ["vrf %s" % v["name"], "exit-vrf", "!"]
    L += ["interface lo", " ip address %s/32" % node["loopback"]]
    if run_igp and igp == "isis":
        L += [" ip router isis %s" % tag, " isis passive"]
    elif run_igp and igp == "ospf":
        L += [" ip ospf area %s" % opts["ospf_area"], " ip ospf passive"]
    L += ["exit", "!"]
    for itf in node["ifaces"]:
        L += ["interface %s" % itf["name"],
              " description to %s" % itf["peer"],
              " ip address %s/%d" % (itf["ip"], itf["prefix"])]
        in_igp = run_igp and itf.get("igp", "active") is not None
        if igp == "isis" and in_igp:
            L.append(" ip router isis %s" % tag)
            L.append(" isis network point-to-point" if itf["igp_active"] else " isis passive")
        elif igp == "ospf" and in_igp:
            L.append(" ip ospf area %s" % opts["ospf_area"])
            L.append(" ip ospf network point-to-point" if itf["igp_active"] else " ip ospf passive")
        L += ["exit", "!"]
    if run_igp and igp == "isis":
        L += ["router isis %s" % tag,
              " is-type level-2-only",
              " net %s" % node["net"],
              " metric-style wide",
              " log-adjacency-changes"]
        if sid is not None:
            L += [" segment-routing on",
                  " segment-routing global-block %d %d" % (opts["srgb_lo"], opts["srgb_hi"]),
                  " segment-routing prefix %s/32 index %d" % (node["loopback"], sid)]
        L += ["exit", "!"]
    elif run_igp and igp == "ospf":
        L += ["router ospf",
              " ospf router-id %s" % node["loopback"],
              " log-adjacency-changes",
              "exit", "!"]
    if node.get("bgp"):
        L += _bgp_frr(node)
    L += ["line vty", "!"]
    return "\n".join(L) + "\n"


def frr_exec(node, opts):
    """What frr.conf cannot say: MTU, MPLS input per core port, VRF devices."""
    # vtysh complains on every call when vtysh.conf is missing
    cmds = ["touch /etc/frr/vtysh.conf"]
    for itf in node["ifaces"]:
        cmds.append("ip link set %s mtu %d" % (itf["name"], opts["mtu"]))
        if node.get("sid") is not None and itf["igp_active"]:
            cmds.append("sysctl -w net.mpls.conf.%s.input=1" % itf["name"])
    for k, v in enumerate(node.get("vrfs", []), start=1):
        cmds += ["ip link add %s type vrf table %d" % (v["name"], 1000 + k),
                 "ip link set %s up" % v["name"]]
        cmds += ["ip link set %s master %s" % (i["name"], v["name"])
                 for i in node["ifaces"] if i.get("vrf") == v["name"]]
    return cmds


def _srl_area(area):
    """OSPF area as SR Linux wants it: dotted quad."""
    a = str(area)
    if re.match(r"^\d+$", a):
        return str(ipaddress.ip_address(int(a)))
    return a


def cfg_srl(node, igp, opts):
    """SR Linux CLI (flat `set` commands) that clab applies after boot."""
    run_igp = node.get("run_igp", True)
    tag = opts["igp_tag"]
    ni = "set / network-instance default"
    L = []
    for itf in node["ifaces"]:
        i = "set / interface %s" % itf["name"]
        L += ["%s admin-state enable" % i,
              '%s description "to %s"' % (i, itf["peer"]),
              # port MTU counts the Ethernet header; ip-mtu is what IOS calls mtu
              "%s mtu %d" % (i, min(opts["mtu"] + 14, 9500)),
              "%s subinterface 0 admin-state enable" % i,
              "%s subinterface 0 ip-mtu %d" % (i, opts["mtu"]),
              "%s subinterface 0 ipv4 admin-state enable" % i,
              "%s subinterface 0 ipv4 address %s/%d" % (i, itf["ip"], itf["prefix"])]
    L += ["set / interface system0 admin-state enable",
          "set / interface system0 subinterface 0 ipv4 admin-state enable",
          "set / interface system0 subinterface 0 ipv4 address %s/32" % node["loopback"],
          "%s type default" % ni,
          "%s admin-state enable" % ni,
          "%s router-id %s" % (ni, node["loopback"]),
          "%s interface system0.0" % ni]
    L += ["%s interface %s.0" % (ni, itf["name"]) for itf in node["ifaces"]]
    igp_ifaces = [i for i in node["ifaces"] if run_igp and i.get("igp", "active") is not None]
    if run_igp and igp == "isis":
        p = "%s protocols isis instance %s" % (ni, tag)
        L += ["%s admin-state enable" % p,
              "%s level-capability L2" % p,
              "%s net [ %s ]" % (p, node["net"]),
              "%s ipv4-unicast admin-state enable" % p,
              "%s level 2 metric-style wide" % p,
              "%s interface system0.0 passive true" % p,
              "%s interface system0.0 ipv4-unicast admin-state enable" % p]
        for itf in igp_ifaces:
            q = "%s interface %s.0" % (p, itf["name"])
            if itf["igp_active"]:
                L.append("%s circuit-type point-to-point" % q)
            else:
                L.append("%s passive true" % q)
            L.append("%s ipv4-unicast admin-state enable" % q)
    elif run_igp and igp == "ospf":
        p = "%s protocols ospf instance %s" % (ni, tag)
        area = "%s area %s" % (p, _srl_area(opts["ospf_area"]))
        L += ["%s admin-state enable" % p,
              "%s version ospf-v2" % p,
              "%s router-id %s" % (p, node["loopback"]),
              "%s interface system0.0 passive true" % area]
        for itf in igp_ifaces:
            q = "%s interface %s.0" % (area, itf["name"])
            L.append(("%s interface-type point-to-point" % q) if itf["igp_active"]
                     else ("%s passive true" % q))
    if node.get("bgp"):
        L += _bgp_srl(node)
    return "\n".join(L) + "\n"


def _bgp_srl(node):
    b = node["bgp"]
    ni = "set / network-instance default"
    p = "%s protocols bgp" % ni
    L = ["%s admin-state enable" % p,
         "%s autonomous-system %d" % (p, b["asn"]),
         "%s router-id %s" % (p, node["loopback"]),
         "%s afi-safi ipv4-unicast admin-state enable" % p,
         # SR Linux rejects every eBGP route without a policy unless told not to
         "%s ebgp-default-policy import-reject-all false" % p,
         "%s ebgp-default-policy export-reject-all false" % p]
    if b["ibgp"]:
        g = "%s group ibgp" % p
        L += ["%s peer-as %d" % (g, b["asn"]),
              "%s transport local-address %s" % (g, node["loopback"])]
        if b["next_hop_self"]:
            L.append("%s next-hop-self true" % g)
        clients = [n for n in b["ibgp"] if n["rr_client"]]
        if clients:
            L += ["%s group ibgp-rr-clients peer-as %d" % (p, b["asn"]),
                  "%s group ibgp-rr-clients transport local-address %s" % (p, node["loopback"]),
                  "%s group ibgp-rr-clients route-reflector client true" % p,
                  "%s group ibgp-rr-clients route-reflector cluster-id %s" % (p, node["loopback"])]
            if b["next_hop_self"]:
                L.append("%s group ibgp-rr-clients next-hop-self true" % p)
        for n in b["ibgp"]:
            L += ["%s neighbor %s peer-group %s" % (p, n["ip"], "ibgp-rr-clients" if n["rr_client"] else "ibgp"),
                  '%s neighbor %s description "iBGP %s"' % (p, n["ip"], n["peer"])]
    for e in b["ebgp"]:
        L += ["%s group ebgp-%d peer-as %d" % (p, e["asn"], e["asn"]),
              "%s neighbor %s peer-group ebgp-%d" % (p, e["ip"], e["asn"]),
              '%s neighbor %s description "eBGP %s"' % (p, e["ip"], e["peer"])]
    if b["networks"]:
        # SR Linux has no `network` statement: advertise through an export policy
        rp = "set / routing-policy"
        for net, mask in b["networks"]:
            L.append("%s prefix-set BUILDER-NETWORKS prefix %s/%d mask-length-range exact"
                     % (rp, net, _prefixlen(mask)))
        L += ["%s policy BUILDER-EXPORT statement 10 match prefix prefix-set BUILDER-NETWORKS" % rp,
              "%s policy BUILDER-EXPORT statement 10 action policy-result accept" % rp,
              "%s policy BUILDER-EXPORT default-action policy-result next-policy" % rp,
              "%s export-policy [ BUILDER-EXPORT ]" % p]
    return L


def _services(spec, igp):
    s = spec.get("services") or {}
    svc = {
        "sr": bool(s.get("sr")),
        "bgp": (s.get("bgp") or "off"),
        "l3vpn": bool(s.get("l3vpn")),
        "core_asn": s.get("core_asn") or 65000,
        "srgb": s.get("srgb") or [16000, 23999],
    }
    _need(svc["bgp"] in BGP_MODES, "bgp must be one of %s" % ", ".join(BGP_MODES))
    try:
        svc["core_asn"] = int(svc["core_asn"])
        lo, hi = int(svc["srgb"][0]), int(svc["srgb"][1])
    except (TypeError, ValueError, IndexError):
        raise SpecError("core AS and SRGB must be numbers")
    _need(1 <= svc["core_asn"] <= 4294967295, "core AS out of range")
    _need(16000 <= lo < hi <= 1048575, "SRGB must be a range inside 16000-1048575")
    svc["srgb_lo"], svc["srgb_hi"] = lo, hi
    _need(not svc["sr"] or igp == "isis",
          "Segment Routing is generated for IS-IS only - pick IS-IS as the IGP")
    _need(not svc["l3vpn"] or svc["bgp"] != "off",
          "L3VPN needs BGP - choose iBGP full mesh or route reflectors")
    _need(not svc["l3vpn"] or svc["sr"],
          "L3VPN needs an MPLS transport - enable Segment Routing")
    return svc


def generate(spec):
    """spec -> {"files": {name: text}, "summary": {...}, "warnings": [...]}"""
    warnings = []

    name = (spec.get("name") or "").strip().lower()
    _need(NAME_RE.match(name), "lab name must match [a-z][a-z0-9-]{0,30}: %r" % name)

    nodes_in = spec.get("nodes") or []
    links_in = spec.get("links") or []
    _need(nodes_in, "the topology has no nodes")
    _need(len(nodes_in) <= 40, "too many nodes (limit 40)")

    igp = (spec.get("igp") or "none").lower()
    _need(igp in ("isis", "ospf", "none"), "igp must be isis, ospf or none")

    svc = _services(spec, igp)
    # Roles and AS numbers only change the generated routing once BGP is on;
    # without it every router is one flat IGP domain, as before.
    bgp_on = svc["bgp"] != "off"

    opts = {
        "igp_tag": (spec.get("isis_tag") or "CORE").strip() or "CORE",
        "isis_area": (spec.get("isis_area") or "49.0001").strip(),
        "ospf_pid": str(spec.get("ospf_pid") or 1),
        "ospf_area": str(spec.get("ospf_area") or 0),
        "mtu": int(spec.get("mtu") or 1500),
        "srgb_lo": svc["srgb_lo"], "srgb_hi": svc["srgb_hi"],
    }
    _need(re.match(r"^49(\.[0-9a-f]{4})+$", opts["isis_area"]),
          "IS-IS area must look like 49.0001")
    _need(1500 <= opts["mtu"] <= 9216, "mtu must be between 1500 and 9216")

    # ---- address pools -------------------------------------------------
    try:
        link_net = ipaddress.ip_network(spec.get("link_subnet") or "10.10.0.0/16",
                                        strict=False)
        lo_net = ipaddress.ip_network(spec.get("loopback_subnet") or "10.0.0.0/24",
                                      strict=False)
        mgmt_net = ipaddress.ip_network(spec.get("mgmt_subnet") or "172.20.20.0/24",
                                        strict=False)
    except ValueError as exc:
        raise SpecError("bad subnet: %s" % exc)
    _need(link_net.version == 4 and lo_net.version == 4, "IPv4 pools only")

    opts["pools"] = [str(link_net), str(lo_net)]
    link_prefix = int(spec.get("link_prefix") or 30)
    _need(link_prefix in (30, 31), "link prefix length must be 30 or 31")
    _need(link_prefix >= link_net.prefixlen,
          "link prefix /%d does not fit inside %s" % (link_prefix, link_net))

    # ---- nodes ---------------------------------------------------------
    nodes, by_id, seen = [], {}, set()
    lo_hosts = lo_net.hosts()
    mgmt_hosts = mgmt_net.hosts()
    next(mgmt_hosts, None)          # .1 is the bridge gateway

    for i, n in enumerate(nodes_in, start=1):
        nname = (n.get("name") or "").strip().lower()
        _need(NAME_RE.match(nname), "bad node name: %r" % nname)
        _need(nname not in seen, "duplicate node name: %s" % nname)
        seen.add(nname)

        kind = n.get("kind")
        _need(kind in KINDS, "unsupported kind for %s: %r" % (nname, kind))
        meta = KINDS[kind]

        try:
            lo = next(lo_hosts)
        except StopIteration:
            raise SpecError("loopback pool %s is too small for %d nodes"
                            % (lo_net, len(nodes_in)))
        try:
            mg = next(mgmt_hosts)
        except StopIteration:
            raise SpecError("management subnet %s is too small" % mgmt_net)

        role = (n.get("role") or guess_role(nname)).upper()
        _need(role in ROLES, "role of %s must be one of %s" % (nname, ", ".join(ROLES)))
        vrf = (n.get("vrf") or "CUST-A").strip()
        _need(VRF_RE.match(vrf), "bad VRF name on %s: %r" % (nname, vrf))

        node = {
            "id": n.get("id") or nname,
            "name": nname,
            "kind": kind,
            "image": n.get("image") or "",
            "meta": meta,
            "index": i,
            "loopback": str(lo),
            "net": isis_net(opts["isis_area"], i),
            "mgmt_ip": str(mg),
            "mgmt_mask": str(mgmt_net.netmask),
            "mgmt_prefix": mgmt_net.prefixlen,
            "x": n.get("x", 0), "y": n.get("y", 0),
            "ifaces": [],
            "_next_eth": 1,
            "role": role,
            "asn_in": n.get("asn"),
            "vrf": vrf if role == "CE" else None,
            "router": meta["configure"] is True,
            "icon": n.get("icon") or None,
        }
        _need(node["icon"] in (None,) + ICONS,
              "node %s: unknown icon %r" % (nname, node["icon"]))
        _need(node["image"], "node %s has no image selected" % nname)
        nodes.append(node)
        by_id[node["id"]] = node

    # ---- AS numbers ----------------------------------------------------
    core_asn = svc["core_asn"]
    used = set()
    for n in nodes:
        if n["asn_in"] not in (None, ""):
            try:
                n["asn"] = int(n["asn_in"])
            except (TypeError, ValueError):
                raise SpecError("AS of %s is not a number" % n["name"])
            _need(1 <= n["asn"] <= 4294967295, "AS of %s out of range" % n["name"])
            used.add(n["asn"])
    next_ce = 65101
    for n in nodes:
        if "asn" in n:
            continue
        if n["role"] == "CE":
            while next_ce in used or next_ce == core_asn:
                next_ce += 1
            n["asn"] = next_ce
            used.add(next_ce)
        else:
            n["asn"] = core_asn
    for n in nodes:
        if bgp_on and n["router"] and n["role"] == "CE":
            _need(n["asn"] != core_asn,
                  "CE %s uses the core AS %d - give it its own AS" % (n["name"], core_asn))
        # core = inside the provider IGP. Without BGP, every router is.
        n["core"] = n["router"] and (not bgp_on or (n["role"] != "CE" and n["asn"] == core_asn))
        n["run_igp"] = n["core"] and igp != "none"
        if n["meta"].get("services") is False and n["router"]:
            _need(not (svc["sr"] or bgp_on),
                  "%s is %s: the builder generates addressing and IS-IS/OSPF for it, but "
                  "Segment Routing, BGP and L3VPN only for IOS-XE and IOS-XR - turn those "
                  "services off, or configure %s by hand after the lab is up"
                  % (n["name"], n["meta"]["os"], n["name"]))
        if n["meta"].get("services") == "bgp" and n["router"]:
            _need(not (svc["sr"] and n["run_igp"]) and not (svc["l3vpn"] and n["core"]),
                  "%s is %s: the builder generates addressing, IS-IS/OSPF and BGP for it, but "
                  "its free container types have no MPLS dataplane, so it cannot take part in "
                  "Segment Routing or L3VPN - turn those off, or make %s a CE"
                  % (n["name"], n["meta"]["os"], n["name"]))
        n["sid"] = n["index"] if (svc["sr"] and n["run_igp"]) else None
        if n["sid"] is not None:
            _need(opts["srgb_lo"] + n["sid"] <= opts["srgb_hi"],
                  "SRGB too small for prefix-SID index %d" % n["sid"])

    # ---- links ---------------------------------------------------------
    subnets = link_net.subnets(new_prefix=link_prefix)
    links = []
    pairs = set()
    sessions = []                   # eBGP sessions, for the summary
    for li, l in enumerate(links_in, start=1):
        a, b = by_id.get(l.get("a")), by_id.get(l.get("b"))
        _need(a is not None and b is not None, "link %d references an unknown node" % li)
        _need(a is not b, "link %d connects %s to itself" % (li, a["name"]))
        key = tuple(sorted((a["id"], b["id"])))
        if key in pairs:
            warnings.append("more than one link between %s and %s - each gets its "
                            "own subnet and interface pair" % (a["name"], b["name"]))
        pairs.add(key)

        try:
            sub = next(subnets)
        except StopIteration:
            raise SpecError("link pool %s cannot supply %d /%d subnets"
                            % (link_net, len(links_in), link_prefix))
        hosts = list(sub) if link_prefix == 31 else list(sub.hosts())
        ip_a, ip_b = str(hosts[0]), str(hosts[1])

        ea, eb = a["_next_eth"], b["_next_eth"]
        a["_next_eth"] += 1
        b["_next_eth"] += 1

        # An IGP adjacency only makes sense when we configure both ends and
        # both sit in the provider core. Toward a plain host the interface
        # still joins the IGP, but passive, so the subnet is advertised and no
        # adjacency is attempted. Toward another AS it stays out of the IGP.
        adj = a["core"] and b["core"]

        def side_igp(me, far):
            if me["core"] and far["core"]:
                return "active"
            if me["core"] and not far["router"]:
                return "passive"
            return None

        ebgp = bgp_on and a["router"] and b["router"] and a["asn"] != b["asn"]
        vrf_a = vrf_b = None
        if ebgp and svc["l3vpn"]:
            if b["role"] == "CE" and a["core"]:
                vrf_a = b["vrf"]
            if a["role"] == "CE" and b["core"]:
                vrf_b = a["vrf"]
            for pe, ce in ((a, b), (b, a)):
                if ce["role"] == "CE" and pe["core"] and pe["role"] != "PE":
                    warnings.append("CE %s is attached to %s, which is not a PE - its "
                                    "session is put in VRF %s anyway"
                                    % (ce["name"], pe["name"], ce["vrf"]))
        if bgp_on and a["router"] and b["router"] and not a["core"] and not b["core"] \
                and a["asn"] == b["asn"]:
            warnings.append("%s and %s share AS %d outside the core - the link is "
                            "addressed but carries no routing"
                            % (a["name"], b["name"], a["asn"]))

        a["ifaces"].append({
            "eth": ea, "name": iface_name(a["meta"]["if_style"], ea),
            "ip": ip_a, "netmask": str(sub.netmask), "prefix": sub.prefixlen,
            "peer": b["name"], "peer_ip": ip_b, "subnet": str(sub), "igp_active": adj,
            "igp": side_igp(a, b), "vrf": vrf_a,
            "ebgp": {"ip": ip_b, "asn": b["asn"], "vrf": vrf_a, "peer": b["name"]} if ebgp else None,
            "far_router": b["router"],
        })
        b["ifaces"].append({
            "eth": eb, "name": iface_name(b["meta"]["if_style"], eb),
            "ip": ip_b, "netmask": str(sub.netmask), "prefix": sub.prefixlen,
            "peer": a["name"], "peer_ip": ip_a, "subnet": str(sub), "igp_active": adj,
            "igp": side_igp(b, a), "vrf": vrf_b,
            "ebgp": {"ip": ip_a, "asn": a["asn"], "vrf": vrf_b, "peer": a["name"]} if ebgp else None,
            "far_router": a["router"],
        })
        if ebgp:
            sessions.append({"type": "eBGP", "a": a["name"], "b": b["name"],
                             "a_ip": ip_a, "b_ip": ip_b, "a_as": a["asn"], "b_as": b["asn"],
                             "vrf": vrf_a or vrf_b})
        links.append({
            "a": a["name"], "b": b["name"], "a_eth": ea, "b_eth": eb,
            "a_if": a["ifaces"][-1]["name"], "b_if": b["ifaces"][-1]["name"],
            "subnet": str(sub), "a_ip": ip_a, "b_ip": ip_b,
        })

    for n in nodes:
        if not n["ifaces"]:
            warnings.append("%s has no links - it will boot isolated" % n["name"])
        if n["_next_eth"] - 1 > 8:
            warnings.append("%s has %d links; most vrnetlab kinds expose 8 data "
                            "interfaces" % (n["name"], n["_next_eth"] - 1))

    # ---- BGP -----------------------------------------------------------
    vrf_names = sorted({i["vrf"] for n in nodes for i in n["ifaces"] if i["vrf"]})
    vrf_index = {v: k for k, v in enumerate(vrf_names, start=1)}
    if bgp_on:
        _plan_bgp(nodes, svc, vrf_index, sessions, warnings)
    for n in nodes:
        n.setdefault("bgp", None)
        n.setdefault("vrfs", [])

    # ---- clab topology file --------------------------------------------
    # one kinds: entry per containerlab kind; a node whose image differs from
    # its kind's (two linux images, FRR next to plain hosts) names its own
    used_kinds = {}
    for n in nodes:
        used_kinds.setdefault(clab_kind(n["kind"]), n)
    by_name = {n["name"]: n for n in nodes}

    y = []
    y.append("# Generated by the containerlab dashboard topology builder.")
    y.append("# Edit freely - regenerating from the builder overwrites this file.")
    y.append("#")
    y.append("# IGP:       %s" % (igp if igp != "none" else "none (addressing only)"))
    y.append("# Loopbacks: %s" % lo_net)
    y.append("# Links:     %s in /%d" % (link_net, link_prefix))
    if svc["sr"]:
        y.append("# SR-MPLS:   SRGB %d-%d, prefix-SID index = node number"
                 % (opts["srgb_lo"], opts["srgb_hi"]))
    if bgp_on:
        y.append("# BGP:       core AS %d, iBGP %s%s"
                 % (core_asn, "via route reflectors" if svc["bgp"] == "rr" else "full mesh",
                    ", L3VPN (VPNv4)" if svc["l3vpn"] else ""))
    y.append("")
    y.append("name: %s" % name)
    y.append("")
    y.append("mgmt:")
    y.append("  network: %s-mgmt" % name)
    y.append("  ipv4-subnet: %s" % mgmt_net)
    y.append("")
    y.append("topology:")
    y.append("  kinds:")
    for kind, sample in used_kinds.items():
        y.append("    %s:" % kind)
        y.append("      image: %s" % sample["image"])
        env = KINDS[sample["kind"]].get("env") or {}
        if env:
            y.append("      env:")
            for k, v in env.items():
                y.append('        %s: "%s"' % (k, v))
    y.append("")
    y.append("  nodes:")
    for n in nodes:
        y.append("    %s:" % n["name"])
        y.append("      kind: %s" % clab_kind(n["kind"]))
        if n["image"] != used_kinds[clab_kind(n["kind"])]["image"]:
            y.append("      image: %s" % n["image"])
        y.append("      mgmt-ipv4: %s" % n["mgmt_ip"])
        execs = []
        if n["kind"] == "frr":
            y.append("      binds:")
            y.append("        - configs/%s.daemons:/etc/frr/daemons" % n["name"])
            y.append("        - configs/%s.frr.conf:/etc/frr/frr.conf" % n["name"])
            # set at container creation, before zebra starts and looks for MPLS
            y.append("      sysctls:")
            y.append("        net.ipv4.ip_forward: 1")
            if n["sid"] is not None or n.get("vrfs"):
                y.append("        net.mpls.platform_labels: 1048575")
            execs = frr_exec(n, opts)
        elif n["kind"] == "nokia_srlinux":
            y.append("      startup-config: configs/%s.cli" % n["name"])
        elif n["meta"]["configure"] is True:
            y.append("      startup-config: configs/%s.cfg" % n["name"])
        elif n["meta"]["configure"] == "exec" and n["ifaces"]:
            execs = linux_exec(n, opts)
        if execs:
            y.append("      exec:")
            for cmd in execs:
                y.append("        - %s" % cmd)
        y.append('      labels:')
        if n["kind"] != clab_kind(n["kind"]):
            y.append('        builder-kind: "%s"' % n["kind"])
        y.append('        builder-pos: "%s,%s"' % (n["x"], n["y"]))
        if n["icon"]:
            y.append('        graph-icon: "%s"' % n["icon"])
        if bgp_on:
            y.append('        builder-role: "%s"' % n["role"])
            y.append('        builder-asn: "%d"' % n["asn"])
            if n["vrf"]:
                y.append('        builder-vrf: "%s"' % n["vrf"])
    y.append("")
    y.append("  links:")
    for l in links:
        y.append('    - endpoints: ["%s:%s", "%s:%s"]   # %s'
                 % (l["a"], port_name(by_name[l["a"]]["kind"], l["a_eth"]),
                    l["b"], port_name(by_name[l["b"]]["kind"], l["b_eth"]), l["subnet"]))
    y.append("")

    files = {"%s.clab.yml" % name: "\n".join(y)}

    # ---- per-node configs ----------------------------------------------
    for n in nodes:
        if n["meta"]["configure"] is not True:
            continue
        if n["kind"] == "frr":
            files["configs/%s.frr.conf" % n["name"]] = cfg_frr(n, igp, opts)
            files["configs/%s.daemons" % n["name"]] = frr_daemons(n, igp)
        elif n["kind"] == "nokia_srlinux":
            files["configs/%s.cli" % n["name"]] = cfg_srl(n, igp, opts)
        elif n["meta"]["if_style"] == "xr":
            files["configs/%s.cfg" % n["name"]] = cfg_iosxr(n, igp, opts)
        elif n["meta"]["if_style"] == "nxos":
            files["configs/%s.cfg" % n["name"]] = cfg_nxos(n, igp, opts)
        else:
            files["configs/%s.cfg" % n["name"]] = cfg_iosxe(n, igp, opts)

    # ---- addressing plan, for humans ------------------------------------
    plan = ["# %s - addressing plan" % name, ""]
    if bgp_on or svc["sr"]:
        plan.append("%-10s %-22s %-5s %-11s %-16s %-16s %-8s %s"
                    % ("NODE", "KIND", "ROLE", "AS", "LOOPBACK", "MGMT", "SID", "IS-IS NET"))
        for n in nodes:
            sid = ("%d (%d)" % (n["sid"], opts["srgb_lo"] + n["sid"])) if n["sid"] is not None else "-"
            plan.append("%-10s %-22s %-5s %-11s %-16s %-16s %-8s %s"
                        % (n["name"], n["kind"], n["role"] if bgp_on else "-",
                           n["asn"] if (bgp_on and n["router"]) else "-",
                           n["loopback"], n["mgmt_ip"], sid,
                           n["net"] if (igp == "isis" and n["run_igp"]) else "-"))
    else:
        plan.append("%-10s %-22s %-16s %-16s %s" % ("NODE", "KIND", "LOOPBACK", "MGMT", "IS-IS NET"))
        for n in nodes:
            plan.append("%-10s %-22s %-16s %-16s %s"
                        % (n["name"], n["kind"], n["loopback"], n["mgmt_ip"],
                           n["net"] if igp == "isis" else "-"))
    plan.append("")
    plan.append("%-10s %-22s %-22s %s" % ("SUBNET", "A SIDE", "B SIDE", "INTERFACES"))
    for l in links:
        plan.append("%-10s %-22s %-22s %s <-> %s"
                    % (l["subnet"], "%s %s" % (l["a"], l["a_ip"]),
                       "%s %s" % (l["b"], l["b_ip"]), l["a_if"], l["b_if"]))
    if vrf_names:
        plan.append("")
        plan.append("%-16s %-14s %s" % ("VRF", "ROUTE-TARGET", "PEs (RD)"))
        for v in vrf_names:
            pes = ["%s (%s)" % (n["name"], x["rd"]) for n in nodes for x in n["vrfs"]
                   if x["name"] == v]
            plan.append("%-16s %-14s %s" % (v, "%d:%d" % (core_asn, vrf_index[v]),
                                            ", ".join(pes)))
    if sessions:
        plan.append("")
        plan.append("%-6s %-26s %-26s %s" % ("BGP", "A SIDE", "B SIDE", "AF"))
        for s in sessions:
            plan.append("%-6s %-26s %-26s %s"
                        % (s["type"], "%s %s AS%s" % (s["a"], s["a_ip"], s["a_as"]),
                           "%s %s AS%s" % (s["b"], s["b_ip"], s["b_as"]), s["af"]))
    files["ADDRESSING.txt"] = "\n".join(plan) + "\n"

    summary = {
        "name": name,
        "node_count": len(nodes),
        "link_count": len(links),
        "igp": igp,
        "services": {"sr": svc["sr"], "bgp": svc["bgp"], "l3vpn": svc["l3vpn"],
                     "core_asn": core_asn,
                     "srgb": [opts["srgb_lo"], opts["srgb_hi"]]},
        "est_ram_mb": sum(n["meta"]["ram_mb"] for n in nodes),
        "configured": sum(1 for n in nodes if n["meta"]["configure"] is True),
        "nodes": [{"name": n["name"], "kind": n["kind"], "loopback": n["loopback"],
                   "mgmt": n["mgmt_ip"], "net": n["net"],
                   "role": n["role"], "asn": n["asn"], "vrf": n["vrf"],
                   "sid": n["sid"],
                   "ifaces": [{"name": i["name"], "ip": i["ip"],
                               "prefix": i["prefix"], "peer": i["peer"],
                               "vrf": i["vrf"]}
                              for i in n["ifaces"]]}
                  for n in nodes],
        "links": links,
        "vrfs": [{"name": v, "rt": "%d:%d" % (core_asn, vrf_index[v])} for v in vrf_names],
        "bgp_sessions": sessions,
    }
    return {"files": files, "summary": summary, "warnings": warnings}


def _plan_bgp(nodes, svc, vrf_index, sessions, warnings):
    """Fill node["bgp"] and node["vrfs"]; append iBGP sessions to `sessions`."""
    core_asn = svc["core_asn"]
    l3vpn = svc["l3vpn"]

    for n in nodes:
        n["vrfs"] = []
        if not n["router"]:
            continue
        seen = []
        for i in n["ifaces"]:
            if i["vrf"] and i["vrf"] not in seen:
                seen.append(i["vrf"])
        for v in seen:
            k = vrf_index[v]
            n["vrfs"].append({"name": v, "rd": "%s:%d" % (n["loopback"], k),
                              "rt": "%d:%d" % (core_asn, k)})

    # Who speaks iBGP. PEs and RRs always; plain P routers only when there is
    # no label-switched core to carry traffic past them, or when they have an
    # eBGP neighbour of their own.
    def has_global_ebgp(n):
        return any(i["ebgp"] and not i["ebgp"]["vrf"] for i in n["ifaces"])

    speakers = [n for n in nodes if n["core"] and
                (n["role"] in ("PE", "RR") or not svc["sr"] or has_global_ebgp(n)
                 or n["vrfs"])]
    rrs = [n for n in speakers if n["role"] == "RR"]
    if svc["bgp"] == "rr":
        _need(rrs, "iBGP via route reflectors needs at least one node with role RR")

    any_global_ebgp = any(has_global_ebgp(n) for n in nodes if n["core"])
    af_ipv4 = (not l3vpn) or any_global_ebgp

    for n in speakers:
        ibgp = []
        if svc["bgp"] == "full-mesh":
            peers = [(p, False) for p in speakers if p is not n]
        elif n["role"] == "RR":
            peers = [(p, p["role"] != "RR") for p in speakers if p is not n]
        else:
            peers = [(p, False) for p in rrs]
        for p, client in peers:
            ibgp.append({"ip": p["loopback"], "peer": p["name"], "rr_client": client})
        n["bgp"] = {"asn": core_asn, "ibgp": ibgp,
                    "ebgp": [i["ebgp"] for i in n["ifaces"] if i["ebgp"]],
                    "af_ipv4": af_ipv4, "af_vpnv4": l3vpn,
                    # eBGP-learned next hops are link addresses the IGP does
                    # not carry, so iBGP has to rewrite them.
                    "next_hop_self": has_global_ebgp(n),
                    "networks": []}
        if not ibgp and not n["bgp"]["ebgp"]:
            n["bgp"] = None

    names = sorted(n["name"] for n in speakers)
    for n in speakers:
        for p in (n["bgp"] or {}).get("ibgp", []):
            if n["name"] < p["peer"] or p["peer"] not in names:
                sessions.append({"type": "iBGP", "a": n["name"], "b": p["peer"],
                                 "a_ip": n["loopback"], "b_ip": p["ip"],
                                 "a_as": core_asn, "b_as": core_asn,
                                 "af": " + ".join(x for x, on in
                                                  (("ipv4", af_ipv4), ("vpnv4", l3vpn)) if on)
                                       + (" (RR client)" if p["rr_client"] else "")})

    # Routers outside the core: eBGP only, advertising their loopback and any
    # host subnets hanging off them.
    for n in nodes:
        if not n["router"] or n["core"]:
            continue
        ebgp = [i["ebgp"] for i in n["ifaces"] if i["ebgp"]]
        if not ebgp:
            warnings.append("%s (AS %d) has no eBGP neighbour - nothing routes to it"
                            % (n["name"], n["asn"]))
            continue
        nets = [(n["loopback"], "255.255.255.255")]
        for i in n["ifaces"]:
            if not i["far_router"]:
                nets.append((str(ipaddress.ip_network(i["subnet"]).network_address), i["netmask"]))
        n["bgp"] = {"asn": n["asn"], "ibgp": [], "ebgp": ebgp,
                    "af_ipv4": True, "af_vpnv4": False, "next_hop_self": False,
                    "networks": nets}

    for s in sessions:
        s.setdefault("af", "ipv4" + (" in VRF %s" % s["vrf"] if s.get("vrf") else ""))
