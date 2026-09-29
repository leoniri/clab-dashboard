"""
Lab guides for the catalogue's SP scenarios (scenarios.py): what the lab is for,
what each node does, and a list of checks that prove it works - written once per
scenario and resolved to each node's platform when a lab is generated.

The result is lab-guide.json in the lab directory. The topology view shows it and
runs the checks through guide.py, which only ever executes commands taken from
this file (the browser sends a check id, never a command).

A check is either
  cli   a show command on a node; `expect` is a regex that must match
        (`min` times) for the check to pass
  ping  a ping from a CE / host (FRR, SR Linux, linux): `src` is the source
        address or interface, `responders` how many distinct hosts must answer
        (more than one for a multicast group)
Checks carry a `group` (underlay, transport, service, data plane, end to end)
and `look_for`: what to read in the output, in plain words.
"""

import json
import re

GROUPS = ("Underlay", "Transport", "Service", "Multicast", "Data plane", "End to end")


class Guide:
    def __init__(self, lab, title, goal):
        self.lab, self.title, self.goal = lab, title, goal
        self.roles = {}
        self.checks = []
        self.experiments = []

    def plat(self, node):
        return self.lab.devs[node].plat

    def role(self, node, text):
        self.roles[node] = text

    def cli(self, group, node, title, cmds, expect=None, look_for="", min_count=1):
        """cmds / expect: {platform: value}; a node whose platform has no command is skipped."""
        if node not in self.lab.devs:
            return
        p = self.plat(node)
        cmd = cmds.get(p)
        if not cmd:
            return
        exp = expect.get(p) if isinstance(expect, dict) else expect
        self.checks.append({"id": "c%d" % (len(self.checks) + 1), "group": group, "node": node, "title": title,
                            "kind": "cli", "cmd": cmd, "expect": exp, "min": min_count, "look_for": look_for})

    def ping(self, node, src, dst, title, responders=1, look_for="", group="End to end"):
        self.checks.append({"id": "c%d" % (len(self.checks) + 1), "group": group, "node": node, "title": title,
                            "kind": "ping", "src": src, "dst": dst, "responders": responders,
                            "look_for": look_for or "replies from %s" % dst})

    def experiment(self, title, text, link=None, checks=()):
        ids = [c["id"] for c in self.checks if c["title"] in checks]
        self.experiments.append({"title": title, "text": text, "link": list(link) if link else None,
                                 "checks": ids})

    def as_dict(self):
        roles = []
        for d in self.lab.devs.values():
            roles.append({"node": d.name, "role": d.role or "", "text": self.roles.get(d.name, ""),
                          "platform": d.plat})
        return {"version": 1, "title": self.title, "goal": self.goal, "roles": roles,
                "checks": self.checks, "experiments": self.experiments, "groups": list(GROUPS)}


# --------------------------------------------------------------------------
# commands shared by several scenarios, per platform
# --------------------------------------------------------------------------

ISIS_ADJ = {"xr": "show isis adjacency", "xe": "show isis neighbors", "frr": "show isis neighbor"}
ISIS_UP = {"xr": r"\bUp\b", "xe": r"\bUP\b", "frr": r"\bUp\b"}


def vrf_route(vrf, pfx):
    return {"xr": "show bgp vrf %s %s" % (vrf, pfx),
            "xe": "show bgp vpnv4 unicast vrf %s %s" % (vrf, pfx),
            "frr": "show bgp vrf %s ipv4 unicast %s" % (vrf, pfx)}


def rib_vrf(vrf, pfx):
    return {"xr": "show cef vrf %s %s" % (vrf, pfx),
            "xe": "show ip cef vrf %s %s detail" % (vrf, pfx),
            "frr": "show ip route vrf %s %s" % (vrf, pfx)}


def mpls_prefix(pfx):
    ip, _, ln = pfx.partition("/")
    return {"xr": "show mpls forwarding prefix %s" % pfx,
            "xe": "show mpls forwarding-table %s %s" % (ip, ln),
            "frr": "show mpls table"}


def srv6_locator():
    return {"xr": "show segment-routing srv6 locator", "frr": "show segment-routing srv6 locator"}


def ce_ping(g, a, b, sa, sb, extra=""):
    g.ping(a, sa, sb, "%s -> %s%s" % (a, b, extra),
           look_for="replies from %s: the customer loopbacks reach each other through the service" % sb)


def esc(s):
    return re.escape(s)


# --------------------------------------------------------------------------
# per scenario
# --------------------------------------------------------------------------

def g_interas(lab, option):
    goal = {
        "A": "Join two SR-MPLS provider networks for one L3VPN customer using Inter-AS option A: the two ASBRs "
             "treat each other as customer edges. Each AS runs its own SR-MPLS L3VPN; between the ASBRs a "
             "dot1q sub-interface per VPN carries plain IP and a plain eBGP session. No labels cross the border.",
        "B": "Join two SR-MPLS provider networks using Inter-AS option B: one eBGP VPNv4 session between the "
             "ASBRs carries every VPN route. The ASBRs keep all VPN routes, set next-hop-self towards their "
             "own PE and swap the VPN label - so only one label crosses the border.",
        "C": "Join two SR-MPLS provider networks using Inter-AS option C: the ASBRs only exchange PE loopbacks "
             "as BGP labelled unicast, and the PEs peer VPNv4 directly over a multihop eBGP session. The VPN "
             "routes never touch the ASBRs; a packet carries three labels (SR transport, BGP-LU, VPN).",
    }[option]
    g = Guide(lab, "Inter-AS option %s over SR-MPLS" % option, goal)
    g.role("pe1", "Provider edge of AS 65001. Holds VRF CUST-A and the eBGP session to ce1.")
    g.role("p1", "Core router of AS 65001: IS-IS and SR-MPLS only, no BGP. Just swaps SR labels.")
    g.role("pe2", "Provider edge of AS 65002, mirror of pe1 for ce2.")
    g.role("p2", "Core router of AS 65002.")
    g.role("ce1", "Customer site 1 (AS 65101), advertises 192.168.101.1/32.")
    g.role("ce2", "Customer site 2 (AS 65102), advertises 192.168.102.1/32.")
    asbr = {"A": "Border router. Has its own copy of VRF CUST-A and talks plain eBGP to the other ASBR over "
                 "VLAN 100 - it is a CE for the other AS.",
            "B": "Border router. Keeps every VPN route (retain route-target all), talks eBGP VPNv4 to the other "
                 "ASBR and sets next-hop-self towards its PE, allocating a new VPN label.",
            "C": "Border router. Only carries PE loopbacks as BGP labelled unicast - no VPN routes at all."}[option]
    g.role("asbr1", asbr)
    g.role("asbr2", asbr)
    g.cli("Underlay", "pe1", "IS-IS adjacency pe1 - p1", ISIS_ADJ, ISIS_UP,
          "p1 listed with state Up: the SR-MPLS core of AS 65001 is formed")
    g.cli("Transport", "pe1", "SR label towards asbr1", mpls_prefix("10.1.0.3/32"), r"16013",
          "label 16013 (SRGB 16000 + index 13) for asbr1's loopback 10.1.0.3")
    if option == "A":
        g.cli("Service", "asbr1", "ce2's route arrives as plain IPv4 at asbr1", vrf_route("CUST-A", "192.168.102.1/32"),
              r"65002 65102", "AS path 65002 65102: learned from asbr2 over the VLAN 100 eBGP session")
        g.cli("Service", "pe1", "pe1 learns ce2 from asbr1", vrf_route("CUST-A", "192.168.102.1/32"), esc("10.1.0.3"),
              "next hop 10.1.0.3 (asbr1): asbr1 re-originated the route into its own VPN")
    elif option == "B":
        g.cli("Service", "asbr1", "asbr1 holds ce2's VPNv4 route from asbr2",
              {"xr": "show bgp vpnv4 unicast rd 65002:100 192.168.102.1/32",
               "xe": "show bgp vpnv4 unicast rd 65002:100 192.168.102.1/32"}, esc("10.12.0.2"),
              "RD 65002:100, next hop 10.12.0.2 (asbr2): the eBGP VPNv4 session carries it")
        g.cli("Service", "pe1", "pe1 learns ce2 via asbr1", vrf_route("CUST-A", "192.168.102.1/32"), esc("10.1.0.3"),
              "next hop 10.1.0.3: asbr1 set next-hop-self and put its own VPN label on the route")
    else:
        g.cli("Transport", "pe1", "pe2's loopback as BGP labelled unicast",
              {"xr": "show bgp ipv4 labeled-unicast 10.2.0.1/32", "xe": "show bgp ipv4 unicast 10.2.0.1/32",
               "frr": "show bgp ipv4 labeled-unicast 10.2.0.1/32"}, esc("10.1.0.3"),
              "10.2.0.1/32 via 10.1.0.3 (asbr1) with a label: the inter-AS LSP to pe2")
        g.cli("Service", "pe1", "pe1 learns ce2 straight from pe2", vrf_route("CUST-A", "192.168.102.1/32"),
              esc("10.2.0.1"), "next hop 10.2.0.1 (pe2 itself): the multihop VPNv4 session between the PEs")
        g.cli("Data plane", "pe1", "Three-label stack towards ce2", rib_vrf("CUST-A", "192.168.102.1/32"),
              {"xr": r"labels imposed \{\S+ \S+ \S+\}"},
              "three labels: SR to asbr1, the BGP-LU label for pe2, pe2's VPN label")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1")
    ce_ping(g, "ce2", "ce1", "192.168.102.1", "192.168.101.1")
    g.experiment("Fail the inter-AS link", "Fail asbr1 - asbr2 from the link's Failure tab and run the end-to-end "
                 "checks: they fail, since this lab has one border link. Restore it and watch BGP re-converge.",
                 link=("asbr1", "asbr2"), checks=("ce1 -> ce2",))
    return g


def g_interas_srv6(lab, option):
    goal = {
        "A": "Two IPv6-only SRv6 uSID cores, each with its own L3VPN, joined with Inter-AS option A: a "
             "back-to-back VRF on a dot1q sub-interface between the ASBRs, plain IPv4 and eBGP across the border.",
        "C": "Two IPv6-only SRv6 uSID cores joined end to end: the ASBRs only trade their AS's locators and "
             "loopbacks over eBGP IPv6 and redistribute them into IS-IS; the PEs peer VPNv4 multihop and pe1 "
             "encapsulates straight to pe2's uDT4 SID - one IPv6 header from PE to PE.",
    }[option]
    g = Guide(lab, "Inter-AS %s over SRv6 uSID" % ("option A" if option == "A" else "locator exchange"), goal)
    g.role("pe1", "SRv6 PE of AS 65001 (locator fc00:0:11::/48), VRF CUST-A with a per-VRF uDT4 SID.")
    g.role("pe2", "SRv6 PE of AS 65002 (locator fc00:0:21::/48).")
    g.role("p1", "IPv6-only core router of AS 65001: routes on the outer IPv6 destination, knows no VPNs.")
    g.role("p2", "IPv6-only core router of AS 65002.")
    g.role("asbr1", "Border router of AS 65001. " + ("Back-to-back VRF CUST-A towards asbr2 (VLAN 100)."
                                                      if option == "A" else
                                                      "Advertises AS 65001's locators to asbr2 over eBGP IPv6 and "
                                                      "redistributes AS 65002's into IS-IS."))
    g.role("asbr2", "Border router of AS 65002, mirror of asbr1.")
    g.role("ce1", "Customer site 1, 192.168.101.1/32.")
    g.role("ce2", "Customer site 2, 192.168.102.1/32.")
    g.cli("Underlay", "pe1", "IS-IS adjacency pe1 - p1", ISIS_ADJ, ISIS_UP, "p1 Up")
    g.cli("Transport", "pe1", "pe1's SRv6 locator", srv6_locator(), r"MAIN.*[Uu]p",
          "locator MAIN, fc00:0:11::/48, state Up")
    if option == "A":
        g.cli("Service", "pe1", "pe1 learns ce2 from asbr1", vrf_route("CUST-A", "192.168.102.1/32"),
              esc("2001:db8:1::3"), "next hop 2001:db8:1::3 (asbr1's IPv6 loopback)")
        g.cli("Service", "asbr1", "ce2's route as plain IPv4 from asbr2", vrf_route("CUST-A", "192.168.102.1/32"),
              r"65002 65102", "AS path 65002 65102 over the VLAN 100 session")
    else:
        g.cli("Transport", "p1", "pe2's locator reaches AS 65001", {"xr": "show route ipv6 fc00:0:21::/48",
                                                                    "frr": "show ipv6 route fc00:0:21::/48"},
              esc("fc00:0:21::/48"), "fc00:0:21::/48 in p1's table, redistributed from BGP by asbr1")
        g.cli("Service", "pe1", "pe1 learns ce2 straight from pe2", vrf_route("CUST-A", "192.168.102.1/32"),
              esc("2001:db8:2::1"), "next hop 2001:db8:2::1 (pe2) - the multihop VPNv4 session")
    g.cli("Data plane", "pe1", "pe1 encapsulates in SRv6",
          {"xr": "show cef vrf CUST-A 192.168.102.1/32",
           # FRR's plain route view shows only "label N"; the JSON has the seg6 encapsulation
           "frr": "show ip route vrf CUST-A 192.168.102.1/32 json"},
          {"xr": r"H\.Encaps\.Red", "frr": r'"seg6":\{'},
          "SRv6 encapsulation to the remote uDT4 SID (XR: H.Encaps.Red; FRR: a \"seg6\" block with the SID)")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1")
    ce_ping(g, "ce2", "ce1", "192.168.102.1", "192.168.101.1")
    g.experiment("Capture SRv6 on the wire", "Select the p1 - asbr1 link, open Capture and ping from ce1: the "
                 "customer IPv4 packet is inside an IPv6 header addressed to a uSID (fc00:0:...), with no SRH.",
                 link=("p1", "asbr1"))
    return g


def g_csc_mpls(lab):
    g = Guide(lab, "Carrier supporting Carrier over SR-MPLS",
              "A backbone carrier (AS 65000) sells a labelled VPN to a customer carrier (AS 65001): the backbone "
              "PEs hold VRF CARRIER and speak eBGP labelled unicast to the customer carrier's CSC-CEs, so the "
              "customer carrier's loopbacks and labels cross the backbone. The customer carrier runs its own "
              "L3VPN (CUST-A) between cpe1 and cpe2 on top - four labels deep in the backbone core.")
    for n in ("bpe1", "bpe2"):
        g.role(n, "Backbone PE: VRF CARRIER, eBGP labelled unicast to the CSC-CE, VPNv4 to the other backbone PE.")
    g.role("bp1", "Backbone P router: SR-MPLS only.")
    for n in ("ccse1", "ccse2"):
        g.role(n, "The customer carrier's edge towards the backbone (CSC-CE): hands its site's loopbacks to the "
                  "backbone as labelled unicast and relays the other site's to its PE.")
    for n in ("cpe1", "cpe2"):
        g.role(n, "The customer carrier's own PE: VRF CUST-A, VPNv4 straight to the other carrier PE.")
    g.role("ce1", "End customer of the customer carrier, site 1.")
    g.role("ce2", "End customer, site 2.")
    g.cli("Underlay", "bp1", "Backbone IS-IS", ISIS_ADJ, ISIS_UP, "bpe1 and bpe2 Up", min_count=2)
    g.cli("Transport", "bpe1", "Customer carrier loopbacks in VRF CARRIER",
          {"xr": "show bgp vrf CARRIER", "frr": "show bgp vrf CARRIER ipv4 labeled-unicast"}, esc("10.1.0.11"),
          "10.1.0.11/32 (cpe2) learned across the backbone - the carrier's loopbacks are VPN routes here")
    g.cli("Transport", "ccse1", "Labelled routes to the other carrier site",
          {"xr": "show bgp ipv4 labeled-unicast", "frr": "show bgp ipv4 labeled-unicast"}, esc("10.1.0.11"),
          "10.1.0.11/32 via the backbone PE, with a label")
    g.cli("Service", "cpe1", "cpe1 learns ce2 from cpe2", vrf_route("CUST-A", "192.168.102.1/32"), esc("10.1.0.11"),
          "next hop 10.1.0.11 (cpe2): the customer carrier's own VPN")
    g.cli("Data plane", "bpe1", "Backbone forwarding for cpe2's loopback",
          {"xr": "show cef vrf CARRIER 10.1.0.11/32"}, r"labels imposed",
          "a label stack towards bpe2: backbone transport + the CARRIER VPN label")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1")
    g.experiment("See the label stack", "Select bpe1 - bp1, open Capture and ping from ce1: four MPLS labels "
                 "(backbone transport, CARRIER VPN, carrier transport, CUST-A VPN).", link=("bpe1", "bp1"))
    return g


def g_csc_srv6(lab):
    g = Guide(lab, "Carrier supporting Carrier over SRv6",
              "With SRv6 a carrier's carrier needs no labels: the backbone (AS 65000) sells an IPv6 L3VPN "
              "(VRF CARRIER, uDT6) and the customer carrier's SRv6 packets are just IPv6 to it. The CSC-CEs "
              "advertise their site's locators (fc01:0::/32 block) into VRF CARRIER; cpe1 and cpe2 run their "
              "own SRv6 L3VPN on top, so customer packets are encapsulated twice in the backbone.")
    for n in ("bpe1", "bpe2"):
        g.role(n, "Backbone SRv6 PE: VRF CARRIER (IPv6, uDT6).")
    g.role("bp1", "Backbone P router: IPv6 / SRv6 only.")
    for n in ("ccse1", "ccse2"):
        g.role(n, "CSC-CE: eBGP IPv6 to the backbone PE, carries the site's locators both ways.")
    for n in ("cpe1", "cpe2"):
        g.role(n, "The customer carrier's SRv6 PE (block fc01:0::/32), VRF CUST-A with a uDT4 SID.")
    g.role("ce1", "End customer, site 1.")
    g.role("ce2", "End customer, site 2.")
    g.cli("Underlay", "bp1", "Backbone IS-IS", ISIS_ADJ, ISIS_UP, "bpe1 and bpe2 Up", min_count=2)
    g.cli("Transport", "bpe1", "Carrier locators inside VRF CARRIER",
          {"xr": "show bgp vrf CARRIER ipv6 unicast", "frr": "show bgp vrf CARRIER ipv6 unicast"},
          esc("fc01:0:11::/48"), "fc01:0:11::/48 (cpe2's locator) learned across the backbone")
    g.cli("Transport", "cpe1", "The remote carrier site's locator in IS-IS", {"frr": "show ipv6 route fc01:0:11::/48",
                                                                             "xr": "show route ipv6 fc01:0:11::/48"},
          esc("fc01:0:11::/48"), "fc01:0:11::/48, redistributed into IS-IS by ccse1")
    g.cli("Service", "cpe1", "cpe1 learns ce2 from cpe2", vrf_route("CUST-A", "192.168.102.1/32"),
          esc("2001:db8:1::11"), "next hop 2001:db8:1::11 (cpe2)")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1")
    g.experiment("Two SRv6 layers", "Select bpe1 - bp1, open Capture and ping from ce1: IPv6 to the backbone's "
                 "uDT6 SID (fc00:0:3:...), inside it IPv6 to cpe2's uDT4 SID (fc01:0:11:...), inside that the "
                 "customer IPv4 packet.", link=("bpe1", "bp1"))
    return g


def g_sr_ldp(lab):
    g = Guide(lab, "SR-MPLS / LDP interworking",
              "A network half-way through an LDP-to-Segment-Routing migration. pe1 speaks only SR, p2 and pe2 "
              "only LDP; p1 runs both and is the SR mapping server, advertising prefix SIDs on behalf of the "
              "LDP-only routers. pe1 can push an SR label towards pe2 and p1 stitches SR to LDP and back.")
    g.role("pe1", "SR-only PE. Uses the SIDs the mapping server advertises for LDP-only routers.")
    g.role("p1", "SR + LDP border and SR mapping server (10.0.0.3/32 index 3, range 2).")
    g.role("p2", "LDP-only core router.")
    g.role("pe2", "LDP-only PE.")
    g.role("ce1", "Customer site behind the SR-only PE.")
    g.role("ce2", "Customer site behind the LDP-only PE.")
    g.cli("Transport", "p1", "Mapping-server advertisements", {"xr": "show segment-routing mapping-server "
                                                              "prefix-sid-map ipv4"}, esc("10.0.0.3/32"),
          "10.0.0.3/32 with SID index 3 and range 2 - SIDs for p2 and pe2")
    g.cli("Transport", "p1", "LDP session to p2", {"xr": "show mpls ldp neighbor brief",
                                                  "frr": "show mpls ldp neighbor"}, esc("10.0.0.3"),
          "p2 (10.0.0.3) as an LDP neighbour")
    g.cli("Transport", "pe1", "pe1 pushes the mapped SID for pe2", mpls_prefix("10.0.0.4/32"), r"16004",
          "outgoing label 16004: an SR label for an LDP-only router")
    g.cli("Data plane", "p1", "SR to LDP stitching", {"xr": "show mpls forwarding labels 16004"}, r"16004",
          "local label 16004 swapped to p2's LDP label")
    g.cli("Transport", "pe2", "pe1 reachable over plain LDP", {"xr": "show mpls ldp bindings 10.0.0.1/32",
                                                              "frr": "show mpls ldp binding 10.0.0.1/32"},
          esc("10.0.0.1/32"), "an LDP binding for 10.0.0.1/32 (pe1) from p2")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1")
    return g


def g_flex(lab):
    g = Guide(lab, "Flex-Algo 128 low-delay slice",
              "IS-IS carries two topologies: normal SPF on the IGP metric goes via p1, Flex-Algo 128 (metric "
              "type delay) goes via p2. VRF CUST-A follows the normal topology; VRF LOW-DELAY is exported with "
              "colour 128 and the ingress PE builds an on-demand SR policy for colour 128 restricted to the "
              "algo-128 SIDs.")
    g.role("pe1", "Headend PE: VRFs CUST-A and LOW-DELAY, on-demand SR policy for colour 128.")
    g.role("pe2", "Remote PE.")
    g.role("p1", "Cheap but slow path: IGP metric 10, 50 ms delay per link.")
    g.role("p2", "Expensive but fast path: IGP metric 30, 2 ms delay per link.")
    g.role("ce1", "CUST-A site 1 (normal topology).")
    g.role("ce2", "CUST-A site 2.")
    g.role("ce3", "LOW-DELAY site 1 (algo 128).")
    g.role("ce4", "LOW-DELAY site 2.")
    g.cli("Underlay", "pe1", "Flex-Algo 128 definition", {"xr": "show isis flex-algo 128"}, r"128",
          "algorithm 128 with metric type delay, advertised by every node")
    g.cli("Transport", "pe1", "Algo-128 SID for pe2", {"xr": "show mpls forwarding labels 17284"}, r"17284",
          "local label 17284 (algo-128 SID of pe2) - it leaves towards p2")
    g.cli("Transport", "pe1", "On-demand SR policy for colour 128",
          {"xr": "show segment-routing traffic-eng policy color 128"}, r"Operational: up",
          "Admin up, Operational up, a dynamic path using algo 128")
    g.cli("Data plane", "pe1", "LOW-DELAY traffic rides the policy", rib_vrf("LOW-DELAY", "192.168.104.1/32"),
          {"xr": r"srte_c_128|color 128|128_"}, "the next hop is the colour-128 SR policy, not the plain IGP path")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1", " (CUST-A, normal topology)")
    ce_ping(g, "ce3", "ce4", "192.168.103.1", "192.168.104.1", " (LOW-DELAY, algo 128)")
    g.experiment("Break the low-delay path", "Fail pe1 - p2: the colour-128 policy loses its path (algo 128 has "
                 "no other way) while CUST-A keeps working via p1.", link=("pe1", "p2"),
                 checks=("On-demand SR policy for colour 128",))
    return g


def g_6pe(lab):
    g = Guide(lab, "6PE and 6VPE over an IPv4 SR-MPLS core",
              "IPv6 customers across a core that has no IPv6 at all. 6PE: global IPv6 prefixes travel as IPv6 "
              "labelled unicast over the IPv4 iBGP session, next hop the PE's IPv4 loopback. 6VPE: the same for "
              "a dual-stack VRF, via VPNv6.")
    g.role("pe1", "PE: 6PE (global IPv6 to ce1) and 6VPE (VRF CUST-A to ce3).")
    g.role("pe2", "PE, mirror of pe1.")
    g.role("p1", "IPv4-only SR-MPLS core router - never sees an IPv6 header.")
    g.role("ce1", "6PE customer, IPv6 only, in the global table.")
    g.role("ce2", "6PE customer on pe2.")
    g.role("ce3", "6VPE customer (dual stack) in VRF CUST-A.")
    g.role("ce4", "6VPE customer on pe2.")
    g.cli("Service", "pe1", "6PE route for ce2", {"xr": "show bgp ipv6 labeled-unicast 2001:db8:c:2::1/128"},
          esc("10.0.0.3"), "next hop 10.0.0.3 (pe2's IPv4 loopback) with a label")
    g.cli("Service", "pe1", "6VPE route for ce4", {"xr": "show bgp vpnv6 unicast"}, esc("2001:db8:c:4::1"),
          "2001:db8:c:4::1/128 in RD 65000:100")
    g.cli("Transport", "p1", "The core has no IPv6", {"xr": "show route ipv6"}, None,
          "no IPv6 prefixes apart from link-locals - the IPv6 traffic is inside MPLS")
    g.ping("ce1", "2001:db8:c:1::1", "2001:db8:c:2::1", "ce1 -> ce2 over 6PE")
    g.ping("ce3", "192.168.103.1", "192.168.104.1", "ce3 -> ce4 IPv4 (6VPE lab, VPNv4)")
    g.ping("ce3", "2001:db8:c:3::1", "2001:db8:c:4::1", "ce3 -> ce4 IPv6 over 6VPE")
    return g


def g_vpws(lab):
    g = Guide(lab, "EVPN-VPWS point-to-point service",
              "A point-to-point Ethernet service signalled by BGP EVPN (route type 1) instead of LDP "
              "pseudowires. The two CEs share one subnet across the SR-MPLS core and run eBGP over it.")
    g.role("pe1", "PE with attachment circuit to ce1, EVI 100, local service id 1.")
    g.role("pe2", "PE with attachment circuit to ce2, EVI 100, local service id 2.")
    g.role("p1", "SR-MPLS core router.")
    g.role("ce1", "CE 192.168.12.1/24, loopback 192.168.101.1.")
    g.role("ce2", "CE 192.168.12.2/24, loopback 192.168.102.1.")
    g.cli("Service", "pe1", "Cross-connect state", {"xr": "show l2vpn xconnect"}, r"\bUP\b",
          "the p2p CUST-B cross-connect UP on both segments")
    g.cli("Service", "pe1", "EVPN route type 1", {"xr": "show bgp l2vpn evpn"}, esc("[1]"),
          "[1][...] Ethernet A-D per EVI routes from both PEs")
    g.ping("ce1", "192.168.12.1", "192.168.12.2", "ce1 -> ce2 over the pseudowire (same subnet)")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1")
    return g


def g_migration(lab):
    g = Guide(lab, "SR-MPLS to SRv6 migration (L3VPN)",
              "One L3VPN mid-way from SR-MPLS to SRv6. The core runs both planes side by side; pe1 still speaks "
              "only SR-MPLS, pe2 only SRv6, and the dual-plane gw re-originates VPN routes between them "
              "(SRv6/MPLS L3 service interworking gateway). Customers: single-homed on each plane, one "
              "dual-homed across both, one on the gateway itself.")
    g.role("pe1", "Legacy PE, SR-MPLS only: VPN routes carry a label.")
    g.role("pe2", "Migrated PE, SRv6 only: VPN routes carry a uDT4 SID.")
    g.role("gw", "Dual-plane PE and interworking gateway: label and SID on its own routes, re-originates pe1's "
                 "routes towards pe2 and pe2's towards pe1.")
    g.role("p1", "Core router running SR-MPLS (IPv4) and SRv6 (IPv6) at once.")
    g.role("p2", "Core router running both planes.")
    g.role("ce1", "Site on the legacy plane only.")
    g.role("ce2", "Site on the SRv6 plane only.")
    g.role("ce3", "Dual-homed site: one leg on pe1 (SR-MPLS), one on pe2 (SRv6).")
    g.role("ce4", "Site on the dual-plane gateway PE.")
    g.cli("Underlay", "p1", "p1 adjacencies (both planes)", ISIS_ADJ, ISIS_UP, "pe1, p2 and gw Up", min_count=3)
    g.cli("Transport", "gw", "gw's SRv6 locator", srv6_locator(), r"MAIN.*[Uu]p", "locator MAIN fc00:0:5::/48 Up")
    g.cli("Service", "gw", "pe2's route re-originated by gw",
          {"xr": "show bgp vpnv4 unicast rd 65000:5 192.168.102.1/32 detail"}, r"reoriginated",
          "`reoriginated`, a Local Label and an SRv6-VPN SID: gw offers ce2 to both planes")
    g.cli("Service", "pe1", "pe1 reaches ce2 through gw",
          {"xr": "show bgp vpnv4 unicast rd 65000:5 192.168.102.1/32"}, esc("10.255.0.5"),
          "next hop 10.255.0.5 (gw) with an MPLS label")
    g.cli("Service", "pe2", "pe2 reaches ce1 through gw",
          {"xr": "show bgp vpnv4 unicast rd 65000:5 192.168.101.1/32 detail"}, esc("fc00:0:5::"),
          "an SRv6 SID in gw's locator fc00:0:5::")
    g.cli("Data plane", "pe1", "Label stack from pe1 to gw", rib_vrf("CUST-A", "192.168.102.1/32"),
          {"xr": r"labels imposed \{16005 "}, "labels {16005 <gw VPN label>}: SR to gw, then gw's VPN label")
    g.cli("Data plane", "gw", "gw re-encapsulates towards pe2 in SRv6", rib_vrf("CUST-A", "192.168.102.1/32"),
          {"xr": r"H\.Encaps\.Red"}, "SRv6 H.Encaps.Red to pe2's uDT4 SID (fc00:0:4:...)")
    g.cli("Data plane", "gw", "gw re-encapsulates towards pe1 in MPLS", rib_vrf("CUST-A", "192.168.101.1/32"),
          {"xr": r"labels imposed"}, "labels {16001 <pe1 VPN label>}")
    ce_ping(g, "ce1", "ce2", "192.168.101.1", "192.168.102.1", " (MPLS -> gw -> SRv6)")
    ce_ping(g, "ce2", "ce1", "192.168.102.1", "192.168.101.1", " (SRv6 -> gw -> MPLS)")
    ce_ping(g, "ce2", "ce4", "192.168.102.1", "192.168.104.1", " (SRv6 to the dual-plane PE)")
    ce_ping(g, "ce4", "ce1", "192.168.104.1", "192.168.101.1", " (dual-plane PE to MPLS)")
    ce_ping(g, "ce1", "ce3", "192.168.101.1", "192.168.103.1", " (dual-homed site, MPLS leg)")
    ce_ping(g, "ce2", "ce3", "192.168.102.1", "192.168.103.1", " (dual-homed site, SRv6 leg)")
    g.experiment("Lose the MPLS leg of the dual-homed site", "Fail pe1 - ce3, wait for BGP, then run "
                 "\"ce1 -> ce3\": ce1 now reaches ce3 over gw and pe2's SRv6 leg.", link=("pe1", "ce3"),
                 checks=("ce1 -> ce3 (dual-homed site, MPLS leg)",))
    return g


def g_rr(lab):
    g = Guide(lab, "Redundant route reflectors with BGP add-path",
              "An SR-MPLS L3VPN with two out-of-path VPNv4 route reflectors and one RD for the whole VPN. ce1 is "
              "dual-homed to pe1 and pe2, so its prefix exists twice as the same VPNv4 route. With add-path the "
              "RRs advertise both paths and pe3 keeps the second exit as a pre-programmed PIC edge backup.")
    for n in ("rr1", "rr2"):
        g.role(n, "VPNv4 route reflector (own cluster-id), outside the forwarding path, advertises all paths.")
    g.role("pe1", "PE, first exit to the dual-homed ce1.")
    g.role("pe2", "PE, second exit to ce1.")
    g.role("pe3", "Remote PE for ce2: receives both paths to ce1 and installs one as backup.")
    g.role("p1", "SR-MPLS core router (rr1 hangs off it).")
    g.role("p2", "SR-MPLS core router (rr2 hangs off it).")
    g.role("ce1", "Dual-homed customer site (AS 65101).")
    g.role("ce2", "Single-homed customer site on pe3.")
    g.cli("Service", "rr1", "rr1 has sessions to every PE", {"xr": "show bgp vpnv4 unicast summary",
                                                            "frr": "show bgp ipv4 vpn summary"},
          r"10\.255\.0\.[125]\s", "10.255.0.1, .2 and .5 (pe1, pe2, pe3) established", min_count=3)
    g.cli("Service", "pe3", "pe3 holds four paths to ce1",
          {"xr": "show bgp vpnv4 unicast rd 65000:100 192.168.101.1/32",
           "frr": "show bgp ipv4 vpn 192.168.101.1/32"}, r"from 10\.255\.0\.[67]",
          "four paths: via pe1 (10.255.0.1) and pe2 (10.255.0.2), each from rr1 (.6) and rr2 (.7)", min_count=4)
    g.cli("Data plane", "pe3", "PIC edge backup installed", {"xr": "show cef vrf CUST-A 192.168.101.1/32"},
          r"backup", "a second path marked backup: switch-over without waiting for BGP")
    ce_ping(g, "ce2", "ce1", "192.168.102.1", "192.168.101.1")
    g.experiment("Fail one exit", "Fail pe1 - ce1 while running \"ce2 -> ce1\" again: traffic moves to pe2.",
                 link=("pe1", "ce1"), checks=("ce2 -> ce1",))
    g.experiment("Lose a route reflector", "Stop rr1 (open its CLI or fail rr1 - p1): the paths from rr2 keep "
                 "everything working.", link=("rr1", "p1"), checks=("ce2 -> ce1", "pe3 holds four paths to ce1"))
    return g


def g_mcast(lab, mode):
    rp = "10.255.0.2" if mode == "static" else "10.255.0.100"
    g = Guide(lab, "IPv4 multicast - " + ("PIM-SM with a static RP, plus SSM" if mode == "static" else
                                          "anycast RP with MSDP"),
              ("Plain IPv4 multicast, no MPLS: IS-IS for unicast, PIM sparse mode everywhere. " +
               ("Group 239.1.1.1 uses the static RP r2 (shared tree, register, switch to the shortest-path "
                "tree); group 232.1.1.1 is source-specific (SSM) and needs no RP." if mode == "static" else
                "r2 and r4 share the RP address 10.255.0.100 and exchange Source-Active messages over MSDP, so "
                "every router uses the closer RP and sources registered at one are known to the other. "
                "232.1.1.1 is source-specific (SSM).")) +
              " The hosts join like applications and answer pings sent to the group, so a ping from the "
              "source is the end-to-end test.")
    g.role("h1", "Multicast source, 10.10.1.2.")
    g.role("h2", "Receiver: joined 239.1.1.1 and (10.10.1.2, 232.1.1.1).")
    g.role("h3", "Receiver: joined 239.1.1.1 only.")
    g.role("r1", "First-hop router of the source (PIM DR, registers the source to the RP).")
    g.role("r3", "Last-hop router for h2: IGMPv3 querier, sends the PIM joins.")
    if mode == "static":
        g.role("r2", "Rendezvous point for 239.0.0.0/8 (10.255.0.2).")
        g.role("r4", "Last-hop router for h3.")
    else:
        g.role("r2", "Anycast RP (10.255.0.100 on a second loopback), MSDP peer of r4.")
        g.role("r4", "Anycast RP, MSDP peer of r2; also the last-hop router for h3.")
    rp_cmd = {"xe": "show ip pim rp mapping", "frr": "show ip pim rp-info"}
    g.cli("Multicast", "r3", "RP known on r3", rp_cmd, {"xe": esc(rp), "frr": esc(rp) + r"(?!.*Unknown)"},
          "RP %s for 239.0.0.0/8 (on FRR the OIF must not be Unknown)" % rp)
    g.cli("Multicast", "r3", "IGMP membership from h2", {"xe": "show ip igmp groups", "frr": "show ip igmp groups"},
          r"239\.1\.1\.1", "239.1.1.1 on the LAN towards h2")
    g.cli("Multicast", "r1", "Source state on the first-hop router", {"xe": "show ip mroute 239.1.1.1",
                                                                     "frr": "show ip mroute"},
          r"10\.10\.1\.2", "(10.10.1.2, 239.1.1.1) once h1 has sent (run the ping check first)")
    if mode == "anycast":
        g.cli("Multicast", "r2", "MSDP session r2 - r4", {"xe": "show ip msdp summary", "frr": "show ip msdp peer"},
              {"xe": r"\bUp\b", "frr": r"[Ee]stablished"}, "the peer 10.255.0.4 Up / established")
        g.cli("Multicast", "r2", "Source-Active cache", {"xe": "show ip msdp sa-cache", "frr": "show ip msdp sa"},
              r"10\.10\.1\.2", "(10.10.1.2, 239.1.1.1) - known to both RPs (run the ping check first)")
    g.ping("h1", "eth1", "239.1.1.1", "h1 -> 239.1.1.1 (ASM, both receivers)", responders=2,
           look_for="replies from 10.10.2.2 and 10.10.3.2 - both receivers get the group")
    g.ping("h1", "eth1", "232.1.1.1", "h1 -> 232.1.1.1 (SSM)", responders=1,
           look_for="a reply from 10.10.2.2 only: h3 did not ask for this source")
    g.ping("h2", "10.10.2.2", "10.10.1.2", "h2 -> h1 unicast", look_for="unicast works (RPF depends on it)")
    if mode == "anycast":
        g.experiment("Lose one RP", "Fail r1 - r2 and ping the group again: r1 now reaches the other anycast RP.",
                     link=("r1", "r2"), checks=("h1 -> 239.1.1.1 (ASM, both receivers)",))
    else:
        g.experiment("Watch the RP drop out", "Run the ASM ping, then \"Source state\" on r2: after the "
                     "switch-over r2 has pruned itself off the (S,G) - the traffic goes r1 - r4/r3 directly.",
                     checks=("h1 -> 239.1.1.1 (ASM, both receivers)",))
    return g


BUILDERS = {
    "interas-a": lambda lab: g_interas(lab, "A"),
    "interas-b": lambda lab: g_interas(lab, "B"),
    "interas-c": lambda lab: g_interas(lab, "C"),
    "interas-srv6-a": lambda lab: g_interas_srv6(lab, "A"),
    "interas-srv6-c": lambda lab: g_interas_srv6(lab, "C"),
    "csc-mpls": g_csc_mpls,
    "csc-srv6": g_csc_srv6,
    "sr-ldp": g_sr_ldp,
    "flex-algo": g_flex,
    "6pe": g_6pe,
    "evpn-vpws": g_vpws,
    "srmpls-srv6-migration": g_migration,
    "rr-addpath": g_rr,
    "mcast-static-rp": lambda lab: g_mcast(lab, "static"),
    "mcast-anycast-rp": lambda lab: g_mcast(lab, "anycast"),
}


def guide_json(sid, preset, preset_label, lab):
    b = BUILDERS.get(sid)
    if b is None:
        return None
    d = b(lab).as_dict()
    d.update(scenario=sid, preset=preset, preset_label=preset_label)
    return json.dumps(d, indent=1) + "\n"
