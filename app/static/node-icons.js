/*
 * node-icons.js - network-diagram icons for the containerlab dashboard.
 *
 * Shared by the topology map and the builder palette. No dependencies.
 *
 *   NodeIcons.TYPES                 ["router","switch","server","firewall",
 *                                    "cloud","trafficgen"]
 *   NodeIcons.classify(node)        -> one of TYPES
 *       node: {name, kind, image, icon, external}
 *       Order: node.icon (the clab label `graph-icon`) wins, then external
 *       endpoints are clouds, then the kind, then - for kind linux - image
 *       and name heuristics.
 *   NodeIcons.label(type)           -> human name, e.g. "traffic generator"
 *   NodeIcons.markup(type)          -> SVG markup string for a <g>, drawn in
 *                                      a 48x48 box at the origin
 *   NodeIcons.svg(type, opts)       -> SVGGElement
 *       opts: {size: 48, x: 0, y: 0, title: ""} - (x,y) is the top-left of
 *       the box, size scales it.
 *
 * Colours: bodies use currentColor, so set `color:` on the element (or a
 * parent) to pick the family colour. Glyphs on the body (arrows, slots,
 * bricks) use var(--icon-glyph), falling back to var(--surface) and then
 * white, which keeps them readable in both light and dark themes.
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var GLYPH = "var(--icon-glyph, var(--surface, #fff))";
  var TYPES = ["router", "switch", "server", "firewall", "cloud", "trafficgen"];
  var LABELS = {
    router: "router", switch: "switch", server: "server / host",
    firewall: "firewall", cloud: "external / cloud", trafficgen: "traffic generator"
  };

  // clab graph-icon values -> our types
  var ICON_ALIASES = {
    router: "router", switch: "switch", host: "server", server: "server",
    client: "server", pc: "server", firewall: "firewall", fw: "firewall",
    cloud: "cloud", internet: "cloud", trafficgen: "trafficgen",
    "traffic-generator": "trafficgen", generator: "trafficgen"
  };

  var EXTERNAL = /^(host|mgmt-net|macvlan|bridge-ext|vxlan|vxlan-stitch|dummy)$/;
  var SWITCH_KINDS = /(^|_)(n9kv|nxos|ceos|eos|srl|srlinux|sonic|cvx|cumulus|vjunosswitch|vqfx|cat9kv|dell_|bridge$|ovs-bridge|ovs$)|^sonic|^bridge$|^ovs-bridge$/;
  var FIREWALL_KINDS = /(fortinet|fortigate|paloalto|panos|checkpoint|asav|ftdv|vsrx|pfsense|opnsense)/;
  var ROUTER_IMAGES = /(frr|frrouting|gobgp|bird|openbgpd|quagga|vyos|exabgp|bgpd)/;
  var TRAFFIC_IMAGES = /(traffic-gen|trafficgen|iperf|trex|ixia|ostinato|pktgen|scapy)/;

  function classify(node) {
    node = node || {};
    var icon = String(node.icon || "").toLowerCase().trim();
    if (icon && ICON_ALIASES[icon]) return ICON_ALIASES[icon];
    var kind = String(node.kind || "").toLowerCase();
    var image = String(node.image || "").toLowerCase();
    var name = String(node.name || "").toLowerCase();

    if (node.external || kind === "external" || EXTERNAL.test(kind)) return "cloud";
    if (kind === "bridge" || kind === "ovs-bridge" || SWITCH_KINDS.test(kind)) return "switch";
    if (FIREWALL_KINDS.test(kind)) return "firewall";
    if (kind === "linux" || kind === "" || kind === "k8s-kind" || kind === "ext-container") {
      if (TRAFFIC_IMAGES.test(image)) return "trafficgen";
      if (ROUTER_IMAGES.test(image)) return "router";
      if (/(^|[-_])(sw|leaf|spine|tor)\d*([-_]|$)|^sw\d|^leaf|^spine/.test(name)) return "switch";
      if (/(^|[-_])(fw|firewall)\d*([-_]|$)|^fw\d/.test(name)) return "firewall";
      return "server";
    }
    // any other NOS kind (cisco_*, juniper_*, nokia_sros, vr-*) is a router
    return "router";
  }

  // ---- geometry helpers --------------------------------------------------
  function arrow(x1, y1, x2, y2, head) {
    // line from (x1,y1) to (x2,y2) with a filled head at (x2,y2)
    var dx = x2 - x1, dy = y2 - y1, len = Math.sqrt(dx * dx + dy * dy) || 1;
    var ux = dx / len, uy = dy / len, h = head || 5.5, w = h * 0.62;
    var bx = x2 - ux * h, by = y2 - uy * h;
    return '<path d="M' + r(x1) + ' ' + r(y1) + 'L' + r(bx) + ' ' + r(by) +
      '" stroke="' + GLYPH + '" stroke-width="2.1" stroke-linecap="round" fill="none" vector-effect="non-scaling-stroke"/>' +
      '<path d="M' + r(x2) + ' ' + r(y2) + 'L' + r(bx - uy * w) + ' ' + r(by + ux * w) +
      'L' + r(bx + uy * w) + ' ' + r(by - ux * w) + 'Z" fill="' + GLYPH + '"/>';
  }
  function r(v) { return Math.round(v * 100) / 100; }
  var SHADE = 'fill="#000" fill-opacity=".22"';
  var LIGHT = 'fill="#fff" fill-opacity=".16"';

  var DRAW = {
    router: function () {
      // a puck seen from above-front, four arrows on the top face
      var arrows =
        arrow(0, 0, 13, -13, 6) + arrow(0, 0, -13, 13, 6) +      // out
        arrow(-15, -15, -4, -4, 6) + arrow(15, 15, 4, 4, 6);     // in
      return '' +
        '<path d="M3 18V30A21 8.5 0 0 0 45 30V18Z" fill="currentColor"/>' +
        '<path d="M3 18V30A21 8.5 0 0 0 45 30V18Z" ' + SHADE + '/>' +
        '<ellipse cx="24" cy="18" rx="21" ry="8.5" fill="currentColor"/>' +
        '<ellipse cx="24" cy="18" rx="21" ry="8.5" fill="none" stroke="#fff" stroke-opacity=".28" stroke-width="1"/>' +
        '<g transform="translate(24 18) scale(1 .42)">' + arrows + '</g>';
    },
    switch: function () {
      return '' +
        '<path d="M3 18L10 11H46L39 18Z" fill="currentColor"/>' +
        '<path d="M3 18L10 11H46L39 18Z" ' + LIGHT + '/>' +
        '<path d="M39 18L46 11V31L39 38Z" fill="currentColor"/>' +
        '<path d="M39 18L46 11V31L39 38Z" ' + SHADE + '/>' +
        '<rect x="3" y="18" width="36" height="20" fill="currentColor"/>' +
        arrow(7, 24, 19, 24, 4.6) + arrow(22, 24, 34, 24, 4.6) +
        arrow(34, 32, 22, 32, 4.6) + arrow(19, 32, 7, 32, 4.6);
    },
    server: function () {
      return serverBody() +
        '<g stroke="' + GLYPH + '" stroke-width="2" stroke-linecap="round" vector-effect="non-scaling-stroke">' +
        '<path d="M17 12H29M17 17H29M17 22H29"/></g>' +
        '<circle cx="23" cy="36" r="2.2" fill="' + GLYPH + '"/>';
    },
    trafficgen: function () {
      return serverBody() +
        '<path d="M15.5 21H18.5L20.5 14L23.5 28L26 18L27.5 21H30.5" fill="none" stroke="' + GLYPH +
        '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>' +
        '<circle cx="23" cy="36" r="2.2" fill="' + GLYPH + '"/>';
    },
    firewall: function () {
      var bricks = 'M4 18.5H38M4 25H38M4 31.5H38' +
        'M15 12V18.5M27 12V18.5M10 18.5V25M21 18.5V25M32 18.5V25' +
        'M15 25V31.5M27 25V31.5M10 31.5V38M21 31.5V38M32 31.5V38';
      return '' +
        '<path d="M4 12L10 6H44L38 12Z" fill="currentColor"/>' +
        '<path d="M4 12L10 6H44L38 12Z" ' + LIGHT + '/>' +
        '<path d="M38 12L44 6V32L38 38Z" fill="currentColor"/>' +
        '<path d="M38 12L44 6V32L38 38Z" ' + SHADE + '/>' +
        '<rect x="4" y="12" width="34" height="26" fill="currentColor"/>' +
        '<path d="' + bricks + '" stroke="' + GLYPH + '" stroke-width="1.6" fill="none" vector-effect="non-scaling-stroke"/>';
    },
    cloud: function () {
      var d = 'M13 37H36A8 8 0 0 0 37.5 21.2A11.5 11.5 0 0 0 16.3 17.6A9.8 9.8 0 0 0 13 37Z';
      return '<path d="' + d + '" fill="currentColor" fill-opacity=".16"/>' +
        '<path d="' + d + '" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
    }
  };

  function serverBody() {
    return '' +
      '<path d="M12 7L17 3H39L34 7Z" fill="currentColor"/>' +
      '<path d="M12 7L17 3H39L34 7Z" ' + LIGHT + '/>' +
      '<path d="M34 7L39 3V41L34 45Z" fill="currentColor"/>' +
      '<path d="M34 7L39 3V41L34 45Z" ' + SHADE + '/>' +
      '<rect x="12" y="7" width="22" height="38" rx="1.5" fill="currentColor"/>';
  }

  function markup(type) {
    var fn = DRAW[type] || DRAW.router;
    return '<g class="nodeicon ni-' + (DRAW[type] ? type : "router") + '">' + fn() + '</g>';
  }

  function svg(type, opts) {
    opts = opts || {};
    var size = opts.size || 48;
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "nodeicon ni-" + (DRAW[type] ? type : "router"));
    g.setAttribute("transform", "translate(" + (opts.x || 0) + " " + (opts.y || 0) +
      ") scale(" + (size / 48) + ")");
    var fn = DRAW[type] || DRAW.router;
    // parse inside an <svg> wrapper so the children get the SVG namespace
    var tmp = document.createElementNS(NS, "svg");
    tmp.innerHTML = fn();
    while (tmp.firstChild) g.appendChild(tmp.firstChild);
    if (opts.title) {
      var t = document.createElementNS(NS, "title");
      t.textContent = opts.title;
      g.insertBefore(t, g.firstChild);
    }
    return g;
  }

  window.NodeIcons = {
    TYPES: TYPES.slice(),
    classify: classify,
    label: function (type) { return LABELS[type] || type; },
    markup: markup,
    svg: svg
  };
})();
