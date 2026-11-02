/*
 * ui.js - shared chrome for the dashboard pages.
 *
 *   UI.icon(name)                    -> SVG element (stroke icons, currentColor)
 *   UI.appbar(el, {active, crumbs, after, right, narrow})
 *                                    builds the left navigation rail (once per page)
 *                                    and fills <header class="appbar"> with a
 *                                    prompt-style breadcrumb, the command-palette
 *                                    button and the theme toggle; `right` is an
 *                                    element for the page's own actions. Returns
 *                                    the right-hand container.
 *   UI.menu(anchor, items)           dropdown under `anchor`; items are
 *                                    {label, icon, onClick, href, target, danger,
 *                                     disabled, title, hint, checked} or {sep:1} /
 *                                    {head:"..."}; checked:true|false shows a checkbox
 *                                    and keeps the menu open
 *   UI.moreButton(itemsFn, title, cls)
 *   UI.button(label, cls, onClick, icon)
 *   UI.seg(options, value, onChange) segmented control
 *   UI.tabs(parent, tabs, selected, onSelect)   tabs = [{id, label, n}]
 *   UI.toast(msg, kind, ms)          non-blocking notice; kind ok|warn|crit|info
 *   UI.confirm(title, body, {ok, danger, extra})   -> Promise<boolean>
 *   UI.prompt(title, body, value, {ok})            -> Promise<string|null>
 *   UI.commands(list)                register page commands for the palette:
 *                                    [{label, icon, hint, run, group}]
 *   UI.palette()                     open the command palette (Ctrl/Cmd+K)
 *   UI.kindClass(kind, image)        -> "k-xe" | "k-xr" | "k-nx" | "k-frr" | "k-srl" | "k-lin" | "k-oth"
 *   UI.kindLabel(kind, image)        -> "IOS-XE", "IOS-XR", ...
 *   UI.state()                       the last /api/state the shell fetched (or null)
 *   UI.term.open(container, {name, lab, meta})
 *                                    open a CLI session in the terminal dock (tabs,
 *                                    up to four panes); UI.term.url(container) is
 *                                    the ttyd URL for a plain browser tab
 *
 * window.alert is replaced by a toast, so a failed request never blocks the page.
 */
(function(){
"use strict";
var P = {
  play:'<path d="M7 5l12 7-12 7z"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  redo:'<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  more:'<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  map:'<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="8" r="2.5"/><circle cx="10" cy="18" r="2.5"/><path d="M8.3 7l7.3.8M16.6 10.1l-5 6M7.2 8.3l2 7.4"/>',
  edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  file:'<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  download:'<path d="M12 4v11"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/>',
  upload:'<path d="M12 20V9"/><path d="M7 14l5-5 5 5"/><path d="M5 4h14"/>',
  trash:'<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  lan:'<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
  terminal:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/>',
  list:'<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  layers:'<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  fit:'<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  minus:'<path d="M5 12h14"/>',
  shuffle:'<path d="M4 7h4l8 10h4M4 17h4l2.5-3M15 7h5M17 4l3 3-3 3M17 14l3 3-3 3"/>',
  camera:'<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon:'<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  warn:'<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  search:'<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  x:'<path d="M6 6l12 12M18 6L6 18"/>',
  bolt:'<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  wave:'<path d="M2 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0"/>',
  snapshot:'<path d="M12 8v4l3 2"/><circle cx="12" cy="12" r="8.5"/>',
  save:'<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4"/><path d="M8 20v-6h8v6"/>',
  grid:'<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  link:'<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/>',
  chevron:'<path d="M6 9l6 6 6-6"/>',
  external:'<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  copy:'<rect x="8" y="8" width="12" height="12" rx="1.5"/><path d="M16 8V4H4v12h4"/>',
  back:'<path d="M15 5l-7 7 7 7"/>',
  check:'<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  cmd:'<path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z"/>',
  labs:'<rect x="3" y="4" width="18" height="5" rx="1.5"/><rect x="3" y="10" width="18" height="5" rx="1.5"/><rect x="3" y="16" width="18" height="4" rx="1.5"/><path d="M6.5 6.5h.01M6.5 12.5h.01M6.5 18h.01"/>',
  catalog:'<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M14 4h4.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H14z"/><path d="M10 8h4M10 12h4"/>',
  builder:'<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><circle cx="18" cy="6" r="3"/><path d="M9 6h6M6 9v6a3 3 0 0 0 3 3h6"/>',
  manage:'<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2.2"/><circle cx="10" cy="17" r="2.2"/>',
  keyboard:'<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  cpu:'<rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M10 10h4v4h-4zM9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  disk:'<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  mem:'<rect x="3" y="7" width="18" height="10" rx="1.5"/><path d="M7 7v10M11 7v10M15 7v10M3 20h18"/>',
  box:'<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
  route:'<circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="5" r="2.5"/><path d="M8.5 19H15a3.5 3.5 0 0 0 0-7H9a3.5 3.5 0 0 1 0-7h6.5"/>',
  pulse:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  logout:'<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4"/><path d="M10 17l-5-5 5-5M5 12h11"/>',
  key:'<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M14 9l2 2"/>',
  guide:'<path d="M5 4.5h9.5a3 3 0 0 1 3 3V20H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h9.5"/><path d="M9 8h5M9 11h3"/>',
  lay1:'<rect x="4" y="5" width="16" height="14" rx="1.5"/>',
  lay2v:'<rect x="4" y="5" width="16" height="14" rx="1.5"/><path d="M12 5v14"/>',
  lay2h:'<rect x="4" y="5" width="16" height="14" rx="1.5"/><path d="M4 12h16"/>',
  lay4:'<rect x="4" y="5" width="16" height="14" rx="1.5"/><path d="M12 5v14M4 12h16"/>',
  winmin:'<path d="M6 17h12"/>',
  winmax:'<rect x="5" y="5" width="14" height="14" rx="1.5"/><path d="M5 9h14"/>',
  broadcast:'<circle cx="12" cy="12" r="2"/><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8"/>',
  winrestore:'<rect x="5" y="9" width="10" height="10" rx="1.5"/><path d="M9 9V6a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-3"/>'
};
var NS = "http://www.w3.org/2000/svg";
function icon(name){
  var s = document.createElementNS(NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.8");
  s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = P[name] || "";
  if (name === "more" || name === "play" || name === "stop") s.setAttribute("fill", "currentColor");
  return s;
}
function el(t, c, x){ var e = document.createElement(t); if (c) e.className = c; if (x !== undefined && x !== null) e.textContent = x; return e; }

function button(label, cls, onClick, ic){
  var b = el("button", "ub " + (cls || ""));
  b.type = "button";
  if (ic) b.appendChild(icon(ic));
  if (label) b.appendChild(document.createTextNode(label)); else b.classList.add("icon");
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

// ---------- kinds ----------
function isFrrImage(img){ return /(^|\/)frr(outing)?(\/frr)?(:|$)|frrouting\//.test(img || ""); }
function kindClass(k, img){
  k = k || "";
  return /^cisco_(xrd|xrv)/.test(k) ? "k-xr"
       : /^cisco_(c8000v|csr1000v|cat9kv|c8000|iol|vios)/.test(k) ? "k-xe"
       : /n9kv|nxos/.test(k) ? "k-nx"
       : k === "frr" || (k === "linux" && isFrrImage(img)) ? "k-frr"
       : /srl|srlinux/.test(k) ? "k-srl"
       : k === "linux" ? "k-lin" : "k-oth";
}
var KLABEL = {"k-xr":"IOS-XR","k-xe":"IOS-XE","k-nx":"NX-OS","k-frr":"FRR","k-srl":"SR Linux","k-lin":"Linux"};
function kindLabel(k, img){
  var c = kindClass(k, img);
  return KLABEL[c] || String(k || "").replace(/^cisco_|^nokia_|^arista_|^juniper_/, "").replace(/_vrouter$/, "");
}

// ---------- theme ----------
// Dark is the default; the choice is remembered per browser.
function applyTheme(){
  var t = null;
  try { t = localStorage.getItem("clabd-theme"); } catch(e){}
  document.documentElement.setAttribute("data-theme", t === "light" ? "light" : "dark");
}
function isDark(){ return document.documentElement.getAttribute("data-theme") !== "light"; }
function toggleTheme(){
  var next = isDark() ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("clabd-theme", next); } catch(e){}
  document.querySelectorAll("[data-theme-btn]").forEach(function(b){ b.replaceChildren(icon(isDark() ? "sun" : "moon")); });
  document.dispatchEvent(new CustomEvent("themechange"));
}
applyTheme();

// ---------- session ----------
// Every page sits behind the login. When a session expires mid-page, the next
// API call gets 401: send the browser to the login page and come back here.
var authMe = null;
function toLogin(){
  var here = location.pathname + location.search + location.hash;
  location.href = "/login.html?next=" + encodeURIComponent(here);
}
if (window.fetch && !window.fetch.__clabd){
  var _fetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    return _fetch(input, init).then(function(r){
      var url = typeof input === "string" ? input : (input && input.url) || "";
      if (r.status === 401 && url.indexOf("/api/auth/") < 0 && (url[0] === "/" || url.indexOf(location.origin) === 0)) toLogin();
      return r;
    });
  };
  window.fetch.__clabd = true;
}
function loadMe(){
  return fetch("/api/auth/me", {cache:"no-store"}).then(function(r){ return r.json(); })
    .then(function(m){ authMe = m; return m; }).catch(function(){ return null; });
}
function signOut(){
  fetch("/api/auth/logout", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"})
    .finally(function(){ location.href = "/login.html"; });
}
function changePassword(){
  var box = el("div");
  function fld(label, id, ac){
    var f = el("div", "field"); f.style.marginBottom = "10px";
    var l = el("label", null, label); l.htmlFor = id; f.appendChild(l);
    var i = el("input"); i.type = "password"; i.id = id; i.autocomplete = ac; f.appendChild(i);
    box.appendChild(f); return i;
  }
  var cur = fld("Current password", "pw-cur", "current-password");
  var nw = fld("New password (8+ characters)", "pw-new", "new-password");
  var nw2 = fld("New password again", "pw-new2", "new-password");
  setTimeout(function(){ cur.focus(); }, 40);
  confirmDlg("Change password", "Other browsers signed in as " + ((authMe && authMe.user) || "you")
             + " are signed out.", {ok:"Change", icon:"key", extra:box}).then(function(go){
    if (!go) return;
    if (nw.value !== nw2.value){ toast("the new passwords differ", "crit"); return changePassword(); }
    fetch("/api/auth/password", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({current: cur.value, "new": nw.value})})
      .then(function(r){ return r.json().then(function(b){ return {ok:r.ok, b:b}; }); })
      .then(function(res){
        if (res.ok){ toast("password changed", "ok"); if (authMe) authMe.initial_password = false; }
        else { toast(res.b.error || "could not change the password", "crit"); }
      });
  });
}
function accountMenu(anchor){
  var items = [{head: "signed in as " + ((authMe && authMe.user) || "?")},
               {label:"Change password", icon:"key", onClick:changePassword},
               {label:"Sign out", icon:"logout", onClick:signOut}];
  menu(anchor, items);
}

// ---------- shell: rail ----------
var LOGO = '<svg viewBox="0 0 40 40" aria-hidden="true"><defs><linearGradient id="clg" x1="0" y1="0" x2="1" y2="1">'
  + '<stop offset="0" stop-color="var(--accent)"/><stop offset="1" stop-color="var(--accent-2)"/></linearGradient></defs>'
  + '<rect x="1" y="1" width="38" height="38" rx="10" fill="url(#clg)"/>'
  + '<g stroke="var(--on-accent)" stroke-width="2" stroke-linecap="round" fill="none"><path d="M20 13v6M18 21l-5 4M22 21l5 4"/></g>'
  + '<g fill="var(--on-accent)"><circle cx="20" cy="11" r="3.2"/><circle cx="11.5" cy="27" r="3.2"/><circle cx="28.5" cy="27" r="3.2"/></g></svg>';
var NAV = [["labs","Labs","/","labs"],["catalog","Catalog","/catalog.html","catalog"],
           ["builder","Builder","/builder.html","builder"],["manage","Manage","/manage.html","manage"]];
var shellState = null, rail = null, gauge = null;

function buildRail(active){
  if (rail) return;
  rail = el("nav", "rail"); rail.setAttribute("aria-label", "main");
  var logo = el("a", "rail-logo"); logo.href = "/"; logo.title = "Containerlab dashboard"; logo.innerHTML = LOGO;
  rail.appendChild(logo);
  NAV.forEach(function(n){
    var a = el("a", "ri"); a.href = n[2];
    a.appendChild(icon(n[3])); a.appendChild(el("span", null, n[1]));
    if (n[0] === active) a.setAttribute("aria-current", "page");
    if (n[0] === "labs"){ a.appendChild(el("i", "rjob")); a.title = "Labs"; }
    rail.appendChild(a);
  });
  rail.appendChild(el("div", "grow"));
  gauge = el("a", "rgauge"); gauge.href = "/"; gauge.title = "host memory";
  gauge.innerHTML = '<svg viewBox="0 0 36 36"><circle class="trk" cx="18" cy="18" r="14" fill="none" stroke-width="3.5"/>'
    + '<circle class="val" cx="18" cy="18" r="14" fill="none" stroke-width="3.5" stroke-linecap="round" '
    + 'stroke-dasharray="0 88" transform="rotate(-90 18 18)"/><text x="18" y="21.5" text-anchor="middle">–</text></svg><span>MEM</span>';
  rail.appendChild(gauge);
  var k = el("button", "ri"); k.type = "button"; k.title = "command palette (Ctrl+K)";
  k.appendChild(icon("search")); k.appendChild(el("span", null, "Go to"));
  k.addEventListener("click", function(){ palette(); });
  rail.appendChild(k);
  var th = el("button", "ri"); th.type = "button"; th.title = "light / dark";
  var thi = el("span"); thi.setAttribute("data-theme-btn", ""); thi.style.display = "contents";
  thi.appendChild(icon(isDark() ? "sun" : "moon"));
  th.appendChild(thi); th.appendChild(el("span", null, "Theme"));
  th.addEventListener("click", toggleTheme);
  rail.appendChild(th);
  var acct = el("button", "ri"); acct.type = "button"; acct.title = "account"; acct.hidden = true;
  acct.appendChild(icon("user")); acct.appendChild(el("span", null, "Account"));
  acct.addEventListener("click", function(){ accountMenu(acct); });
  rail.appendChild(acct);
  loadMe().then(function(m){
    if (!m || !m.enabled || !m.user) return;
    acct.hidden = false; acct.title = "signed in as " + m.user;
    if (m.initial_password)
      toast("You are still using the generated admin password - change it under Account.", "warn", 9000);
  });
  document.body.insertBefore(rail, document.body.firstChild);
  document.body.classList.add("has-rail");
  pollShell();
}

function pollShell(){
  fetch("/api/state", {cache:"no-store"}).then(function(r){ return r.json(); }).then(function(s){
    shellState = s;
    var h = s.host || {}, pct = Math.max(0, Math.min(100, h.mem_pct || 0));
    var c = gauge.querySelector(".val"), len = 2 * Math.PI * 14;
    c.setAttribute("stroke-dasharray", (len * pct / 100).toFixed(1) + " " + len.toFixed(1));
    c.setAttribute("class", "val" + (pct >= 90 ? " crit" : pct >= 75 ? " warn" : ""));
    gauge.querySelector("text").textContent = Math.round(pct);
    gauge.title = "host memory " + pct + "% used" + (h.hostname ? " on " + h.hostname : "");
    rail.classList.toggle("busy", !!s.busy);
    var hostEl = document.querySelector(".ab-crumbs .abh");
    if (hostEl && h.hostname) hostEl.textContent = "clab@" + h.hostname;
    setTimeout(pollShell, s.busy ? 3000 : 8000);
  }).catch(function(){ setTimeout(pollShell, 15000); });
}

// ---------- shell: app bar ----------
function appbar(host, o){
  o = o || {};
  buildRail(o.active || (o.crumbs ? "labs" : null));
  host.classList.add("appbar");
  if (o.narrow) host.classList.add("narrow");
  host.textContent = "";
  var inn = el("div", "appbar-in"); host.appendChild(inn);
  var cr = el("div", "ab-crumbs");
  cr.appendChild(el("span", "prompt", "❯"));
  cr.appendChild(el("span", "abh", "clab@" + ((shellState && shellState.host && shellState.host.hostname) || location.hostname)));
  cr.appendChild(el("span", "sep hsep", ":"));
  var crumbs = o.crumbs;
  if (!crumbs){
    var nav = NAV.filter(function(n){ return n[0] === o.active; })[0];
    crumbs = [{label: nav ? nav[1].toLowerCase() : "~"}];
    cr.appendChild(el("span", "sep", "~/"));
  } else {
    cr.appendChild(el("span", "sep", "~/"));
  }
  crumbs.forEach(function(c, i){
    if (i) cr.appendChild(el("span", "sep", "/"));
    if (c.href){ var a = el("a", null, String(c.label).toLowerCase()); a.href = c.href; if (c.id) a.id = c.id; cr.appendChild(a); }
    else { var h = el("span", "here", c.label); if (c.id) h.id = c.id; cr.appendChild(h); }
  });
  cr.appendChild(el("span", "cur"));
  if (o.after){ var aw = el("span"); aw.style.cssText = "margin-left:12px;display:inline-flex;align-items:center;gap:6px;flex:none";
    aw.appendChild(o.after); cr.appendChild(aw); }
  inn.appendChild(cr);
  var right = el("div", "ab-right"); inn.appendChild(right);
  if (o.right) right.appendChild(o.right);
  var k = el("button", "ab-k"); k.type = "button"; k.title = "command palette";
  k.appendChild(icon("search")); k.appendChild(el("span", "t", "Search"));
  var kk = el("span", "kbd", navigator.platform.indexOf("Mac") >= 0 ? "⌘K" : "Ctrl K"); k.appendChild(kk);
  k.addEventListener("click", function(){ palette(); });
  right.appendChild(k);
  return right;
}

// ---------- menu ----------
var openMenu = null;
function closeMenu(){
  if (!openMenu) return;
  openMenu.el.remove();
  document.removeEventListener("mousedown", openMenu.out, true);
  document.removeEventListener("keydown", openMenu.key, true);
  window.removeEventListener("resize", closeMenu);
  window.removeEventListener("scroll", openMenu.scroll, true);
  if (openMenu.anchor) openMenu.anchor.setAttribute("aria-expanded", "false");
  openMenu = null;
}
function menu(anchor, items, opts){
  opts = opts || {};
  var again = openMenu && openMenu.anchor === anchor;
  closeMenu();
  if (again) return;
  var m = el("div", "umenu"); m.setAttribute("role", "menu");
  function build(){
    m.textContent = "";
    items.forEach(function(it){
      if (!it) return;
      if (it.sep){ m.appendChild(el("div", "msep")); return; }
      if (it.head){ m.appendChild(el("div", "mhead", it.head)); return; }
      var b = el(it.href ? "a" : "button", "mi" + (it.danger ? " danger" : ""));
      b.setAttribute("role", it.checked !== undefined ? "menuitemcheckbox" : "menuitem");
      if (it.href){ b.href = it.href; if (it.target){ b.target = it.target; b.rel = "noopener"; } }
      if (it.disabled) b.disabled = true;
      if (it.title) b.title = it.title;
      if (it.checked !== undefined){ var c = el("span", "chk" + (it.checked ? " on" : "")); b.appendChild(c); }
      else if (it.icon) b.appendChild(icon(it.icon));
      b.appendChild(document.createTextNode(it.label));
      if (it.hint) b.appendChild(el("span", "hint", it.hint));
      b.addEventListener("click", function(e){
        if (it.disabled) { e.preventDefault(); return; }
        if (it.checked !== undefined){
          it.checked = !it.checked; build();
          if (it.onClick) it.onClick(it.checked);
          return;
        }
        if (!it.href) e.preventDefault();
        closeMenu();
        if (it.onClick) it.onClick(e);
      });
      m.appendChild(b);
    });
  }
  build();
  document.body.appendChild(m);
  var r = anchor.getBoundingClientRect(), mw = m.offsetWidth, mh = m.offsetHeight;
  var left = opts.align === "left" ? r.left : r.right - mw;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  var top = r.bottom + 5;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 5);
  m.style.left = left + "px"; m.style.top = top + "px";
  anchor.setAttribute("aria-expanded", "true");
  openMenu = {el: m, anchor: anchor,
    out: function(e){ if (!m.contains(e.target) && !anchor.contains(e.target)) closeMenu(); },
    key: function(e){
      if (e.key === "Escape"){ e.stopPropagation(); closeMenu(); anchor.focus(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp"){
        var its = Array.prototype.slice.call(m.querySelectorAll(".mi:not(:disabled)"));
        if (!its.length) return;
        e.preventDefault();
        var i = its.indexOf(document.activeElement);
        i = e.key === "ArrowDown" ? (i + 1) % its.length : (i <= 0 ? its.length - 1 : i - 1);
        its[i].focus();
      }
    },
    scroll: function(e){ if (!m.contains(e.target)) closeMenu(); }};
  document.addEventListener("mousedown", openMenu.out, true);
  document.addEventListener("keydown", openMenu.key, true);
  window.addEventListener("resize", closeMenu);
  window.addEventListener("scroll", openMenu.scroll, true);
  var first = m.querySelector(".mi:not(:disabled)"); if (first && opts.focus !== false) first.focus();
}
function moreButton(itemsFn, title, cls){
  var b = button("", "quiet " + (cls || ""), null, "more");
  b.title = title || "more actions";
  b.setAttribute("aria-haspopup", "menu");
  b.addEventListener("click", function(e){ e.stopPropagation(); menu(b, itemsFn()); });
  return b;
}

// ---------- segmented / tabs ----------
function seg(options, value, onChange){
  var w = el("div", "useg"); w.setAttribute("role", "group");
  options.forEach(function(o){
    var b = el("button", null, o[1]); b.type = "button";
    b.setAttribute("aria-pressed", o[0] === value ? "true" : "false");
    b.addEventListener("click", function(){
      Array.prototype.forEach.call(w.children, function(x){ x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      onChange(o[0]);
    });
    w.appendChild(b);
  });
  return w;
}
function tabs(parent, list, selected, onSelect){
  var t = el("div", "utabs"); t.setAttribute("role", "tablist");
  list.forEach(function(x){
    var b = el("button", null, x.label); b.type = "button"; b.setAttribute("role", "tab");
    if (x.n !== undefined && x.n !== null) b.appendChild(el("span", "n", String(x.n)));
    b.setAttribute("aria-selected", x.id === selected ? "true" : "false");
    b.addEventListener("click", function(){ onSelect(x.id); });
    t.appendChild(b);
  });
  parent.appendChild(t);
  return t;
}

// ---------- toasts ----------
var toastBox = null;
function toast(msg, kind, ms){
  if (!toastBox){ toastBox = el("div", "toasts"); toastBox.setAttribute("role", "status"); document.body.appendChild(toastBox); }
  var t = el("div", "toast " + (kind || ""));
  t.appendChild(icon(kind === "crit" ? "warn" : kind === "ok" ? "check" : kind === "warn" ? "warn" : "info"));
  t.appendChild(el("div", null, String(msg)));
  toastBox.appendChild(t);
  var life = ms || Math.min(14000, 3500 + String(msg).length * 45);
  var h = setTimeout(close, life);
  function close(){ clearTimeout(h); t.classList.add("out"); setTimeout(function(){ t.remove(); }, 220); }
  t.addEventListener("click", close);
  while (toastBox.children.length > 5) toastBox.firstChild.remove();
  return close;
}
window.alert = function(msg){
  var s = String(msg == null ? "" : msg);
  toast(s, /(fail|error|refus|could not|cannot|can't|not found|invalid|denied|busy|conflict)/i.test(s) ? "crit" : "info");
};

// ---------- dialogs ----------
function dialog(o){
  return new Promise(function(resolve){
    var scrim = el("div", "scrim"), d = el("div", "dlg" + (o.wide ? " wide" : ""));
    d.setAttribute("role", "dialog"); d.setAttribute("aria-modal", "true");
    var hd = el("header"), ic = el("div", "dlg-ic" + (o.danger ? " danger" : ""));
    ic.appendChild(icon(o.icon || (o.danger ? "warn" : "info"))); hd.appendChild(ic);
    hd.appendChild(el("h3", null, o.title || "Confirm")); d.appendChild(hd);
    var body = el("div", "body");
    if (o.body) String(o.body).split(/\n\n+/).forEach(function(p){ body.appendChild(el("p", null, p)); });
    var inp = null;
    if (o.input !== undefined){
      var f = el("div", "field"); inp = el("input", "mono"); inp.type = "text"; inp.value = o.input || "";
      inp.spellcheck = false; inp.autocomplete = "off"; f.appendChild(inp); body.appendChild(f);
    }
    if (o.extra) body.appendChild(o.extra);
    d.appendChild(body);
    var ft = el("footer");
    var cancel = button(o.cancel || "Cancel", "quiet"), ok = button(o.ok || "OK", o.danger ? "danger" : "primary");
    ft.appendChild(cancel); ft.appendChild(ok); d.appendChild(ft);
    scrim.appendChild(d); document.body.appendChild(scrim);
    var prev = document.activeElement;
    function done(v){
      scrim.remove(); document.removeEventListener("keydown", key, true);
      if (prev && prev.focus) try { prev.focus(); } catch(e){}
      resolve(v);
    }
    function key(e){
      if (e.key === "Escape"){ e.stopPropagation(); e.preventDefault(); done(inp ? null : false); }
      else if (e.key === "Enter" && (inp ? document.activeElement === inp : true) && document.activeElement !== cancel){
        e.preventDefault(); e.stopPropagation(); done(inp ? inp.value : true);
      }
    }
    document.addEventListener("keydown", key, true);
    cancel.addEventListener("click", function(){ done(inp ? null : false); });
    ok.addEventListener("click", function(){ done(inp ? inp.value : true); });
    scrim.addEventListener("mousedown", function(e){ if (e.target === scrim) done(inp ? null : false); });
    setTimeout(function(){ (inp || ok).focus(); if (inp) inp.select(); }, 20);
  });
}
function confirmDlg(title, body, o){
  o = o || {};
  return dialog({title: title, body: body, ok: o.ok || "Confirm", danger: !!o.danger,
                 extra: o.extra, icon: o.icon, wide: o.wide});
}
function promptDlg(title, body, value, o){
  o = o || {};
  return dialog({title: title, body: body, input: value || "", ok: o.ok || "OK", icon: o.icon || "edit"});
}

// ---------- command palette ----------
var pageCmds = [], cmdkOpen = null;
function commands(list){ pageCmds = pageCmds.concat(list || []); }
function fuzzy(q, s){
  // subsequence match; returns score (lower is better) or -1
  q = q.toLowerCase(); s = s.toLowerCase();
  if (!q) return 0;
  var i = s.indexOf(q); if (i >= 0) return i;
  var j = 0, gaps = 0, last = -1;
  for (var k = 0; k < s.length && j < q.length; k++){
    if (s[k] === q[j]){ if (last >= 0) gaps += k - last - 1; last = k; j++; }
  }
  return j === q.length ? 100 + gaps : -1;
}
function hl(text, q){
  var frag = document.createDocumentFragment();
  if (!q){ frag.appendChild(document.createTextNode(text)); return frag; }
  var i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0){ frag.appendChild(document.createTextNode(text)); return frag; }
  frag.appendChild(document.createTextNode(text.slice(0, i)));
  frag.appendChild(el("mark", null, text.slice(i, i + q.length)));
  frag.appendChild(document.createTextNode(text.slice(i + q.length)));
  return frag;
}
function baseCmds(){
  var c = [
    {group:"Go to", label:"Labs", icon:"labs", hint:"g l", run:function(){ location.href = "/"; }},
    {group:"Go to", label:"Catalogue", icon:"catalog", hint:"g c", run:function(){ location.href = "/catalog.html"; }},
    {group:"Go to", label:"Builder - draw a new lab", icon:"builder", hint:"g b", run:function(){ location.href = "/builder.html"; }},
    {group:"Go to", label:"Import a lab (zip / git)", icon:"upload", run:function(){ location.href = "/catalog.html#import"; }},
    {group:"Go to", label:"Manage - images", icon:"box", hint:"g m", run:function(){ location.href = "/manage.html"; }},
    {group:"Go to", label:"Manage - LAN access", icon:"lan", run:function(){ location.href = "/manage.html#lan"; }},
    {group:"Go to", label:"Manage - networks", icon:"link", run:function(){ location.href = "/manage.html#networks"; }},
    {group:"Go to", label:"Manage - trash", icon:"trash", run:function(){ location.href = "/manage.html#trash"; }},
    {group:"Go to", label:"Manage - audit log", icon:"list", run:function(){ location.href = "/manage.html#audit"; }},
    {group:"Preferences", label:"Toggle light / dark theme", icon:"sun", hint:"t", run:toggleTheme},
    {group:"Account", label:"Change password", icon:"key", run:changePassword},
    {group:"Account", label:"Sign out", icon:"logout", run:signOut},
    {group:"Preferences", label:"Keyboard shortcuts", icon:"keyboard", hint:"?", run:showKeys}
  ];
  if (!(authMe && authMe.user)) c = c.filter(function(x){ return x.group !== "Account"; });
  ((shellState && shellState.labs) || []).forEach(function(l){
    var id = encodeURIComponent(l.id), nm = l.name || l.file, drawable = l.node_count > 0 && !l.parse_error;
    var sub = (l.running ? "● running · " : "") + l.node_count + " nodes · " + l.path;
    if (drawable) c.push({group:"Labs", label:nm + " - topology", sub:sub, icon:"map", run:function(){ location.href = "/topology.html?lab=" + id; }});
    c.push({group:"Labs", label:nm + " - edit files", sub:l.path, icon:"file", run:function(){ location.href = "/edit.html?lab=" + id; }});
    if (drawable) c.push({group:"Labs", label:nm + " - convergence tests", sub:l.path, icon:"bolt", run:function(){ location.href = "/convergence.html?lab=" + id; }});
  });
  return c;
}
function palette(){
  if (cmdkOpen) return;
  closeMenu();
  var wrap = el("div", "cmdk"), box = el("div", "cmdk-box");
  var inr = el("div", "cmdk-in"); inr.appendChild(el("span", "p", "❯"));
  var inp = el("input"); inp.placeholder = "jump to a lab, page or action…"; inp.spellcheck = false; inp.autocomplete = "off";
  inp.setAttribute("aria-label", "command"); inr.appendChild(inp); inr.appendChild(el("span", "kbd", "esc"));
  box.appendChild(inr);
  var list = el("div", "cmdk-list"); box.appendChild(list);
  var foot = el("div", "cmdk-foot");
  foot.innerHTML = '<span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> run</span><span><kbd>esc</kbd> close</span>';
  box.appendChild(foot);
  wrap.appendChild(box); document.body.appendChild(wrap);
  var all = pageCmds.map(function(x){ var y = Object.assign({}, x); y.group = y.group || "This page"; return y; }).concat(baseCmds());
  var shown = [], sel = 0;
  function render(){
    var q = inp.value.trim();
    shown = all.map(function(c){
      var sc = fuzzy(q, c.label);
      if (sc < 0 && q && c.sub && c.sub.toLowerCase().indexOf(q.toLowerCase()) >= 0) sc = 60;
      return {c:c, s:sc}; })
               .filter(function(x){ return x.s >= 0; });
    if (q) shown.sort(function(a, b){ return a.s - b.s; });
    shown = shown.slice(0, 60).map(function(x){ return x.c; });
    if (q){ // keep groups together, in first-hit order
      var order = [], by = {};
      shown.forEach(function(c){ if (!by[c.group]){ by[c.group] = []; order.push(c.group); } by[c.group].push(c); });
      shown = [].concat.apply([], order.map(function(g){ return by[g]; }));
    }
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    list.textContent = "";
    if (!shown.length){ list.appendChild(el("div", "cmdk-empty", "no match for “" + q + "”")); return; }
    var g = null;
    shown.forEach(function(c, i){
      if (c.group !== g){ g = c.group; list.appendChild(el("div", "cmdk-g", g)); }
      var it = el("div", "cmdk-it" + (i === sel ? " on" : ""));
      it.appendChild(icon(c.icon || "chevron"));
      var t = el("span", "t"); t.appendChild(hl(c.label, q));
      if (c.sub) t.appendChild(el("small", null, c.sub));
      it.appendChild(t);
      if (c.hint) it.appendChild(el("span", "h", c.hint));
      it.addEventListener("mousemove", function(){ if (sel !== i){ sel = i; mark(); } });
      it.addEventListener("click", function(){ run(i); });
      list.appendChild(it);
    });
  }
  function mark(){
    var its = list.querySelectorAll(".cmdk-it");
    its.forEach(function(x, i){ x.classList.toggle("on", i === sel); });
    if (its[sel]) its[sel].scrollIntoView({block:"nearest"});
  }
  function run(i){ var c = shown[i]; close(); if (c && c.run) c.run(); }
  function close(){ wrap.remove(); cmdkOpen = null; }
  inp.addEventListener("input", function(){ sel = 0; render(); });
  inp.addEventListener("keydown", function(e){
    if (e.key === "ArrowDown"){ e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); mark(); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); sel = Math.max(0, sel - 1); mark(); }
    else if (e.key === "Enter"){ e.preventDefault(); run(sel); }
    else if (e.key === "Escape"){ e.preventDefault(); e.stopPropagation(); close(); }
  });
  wrap.addEventListener("mousedown", function(e){ if (e.target === wrap) close(); });
  cmdkOpen = {close: close};
  render(); inp.focus();
  if (!shellState) fetch("/api/state", {cache:"no-store"}).then(function(r){ return r.json(); })
    .then(function(s){ shellState = s; if (cmdkOpen){ all = all.concat(baseCmds().filter(function(c){ return c.group === "Labs"; })); render(); } })
    .catch(function(){});
}

// ---------- terminal dock ----------
// Every CLI session is a ttyd iframe in a dock at the bottom of the page: one
// tab per session, up to four panes on screen. An iframe reloads - and so drops
// its SSH session - whenever it is moved in the DOM, so a session's pane is
// created once inside the stage grid and afterwards only its grid placement
// changes. The open sessions are remembered for the browser tab, so the dock
// comes back (and reconnects) on the next dashboard page.
var LAYOUTS = {"1":{n:1, c:1}, "2v":{n:2, c:2}, "2h":{n:2, c:1}, "4":{n:4, c:2}};
var LAYOUT_NAMES = {"1":"one pane", "2v":"side by side", "2h":"stacked", "4":"four panes"};
var GROW = {"1":"2v", "2v":"4", "2h":"4"};
var TERM_KEY = "clabd.term", TERM_H = "clabd.term.h";
var TD = null, termSeq = 0, dragId = null;
var TERM_FONT = 13, FONT_MIN = 8, FONT_MAX = 32;     // 13 = install.sh's ttyd fontSize
var TERM_HIST = "clabd.term.hist";

function termUrl(container){ return "/term/?arg=" + encodeURIComponent(container); }
function tib(ic, title, fn){
  var b = el("button", "tib"); b.type = "button"; b.title = title; b.setAttribute("aria-label", title);
  b.appendChild(icon(ic));
  b.addEventListener("click", function(e){ e.stopPropagation(); fn(e); });
  return b;
}
function dock(){
  if (TD) return TD;
  var d = el("section", "tdock"), bar = el("div", "tbar"), grip = el("div", "tgrip");
  d.hidden = true; d.setAttribute("aria-label", "terminals");
  grip.title = "drag to resize";
  var ttl = el("span", "ttl"), cnt = el("span", null, "CLI");
  ttl.appendChild(icon("terminal")); ttl.appendChild(cnt);
  var tabsEl = el("div", "ttabs"), lay = el("div", "tlay"), right = el("div", "tlay");
  tabsEl.setAttribute("role", "tablist");
  TD = {el:d, tabs:tabsEl, count:cnt, lay:lay, stage:el("div", "tstage"),
        sessions:[], slots:[null], layout:"1", manual:false, focus:0, mode:"open", split:{x:.5, y:.5}, bc:false};
  ["1", "2v", "2h", "4"].forEach(function(k){
    var b = tib("lay" + k, "layout: " + LAYOUT_NAMES[k], function(){ setLayout(k, true); });
    b.dataset.layout = k; lay.appendChild(b);
  });
  TD.bcBtn = tib("broadcast", "send a command to several terminals", function(){ setBroadcast(!TD.bc); });
  right.appendChild(TD.bcBtn); right.appendChild(el("span", "tsep"));
  TD.minBtn = tib("winmin", "minimise", function(){ setMode(TD.mode === "min" ? "open" : "min"); });
  TD.maxBtn = tib("winmax", "maximise", function(){ setMode(TD.mode === "max" ? "open" : "max"); });
  right.appendChild(TD.minBtn); right.appendChild(TD.maxBtn);
  right.appendChild(tib("x", "close all sessions", closeAll));
  [grip, ttl, tabsEl, lay, right].forEach(function(x){ bar.appendChild(x); });
  bar.addEventListener("dblclick", function(e){
    if (e.target === bar || e.target === tabsEl) setMode(TD.mode === "max" ? "open" : "max");
  });
  d.appendChild(bar); d.appendChild(cmdBar()); d.appendChild(TD.stage);
  document.body.appendChild(d);
  var h = 0; try { h = +localStorage.getItem(TERM_H) || 0; } catch (e) {}
  if (h) d.style.height = Math.min(h, innerHeight - 60) + "px";
  grip.addEventListener("pointerdown", startResize);
  if (window.ResizeObserver) new ResizeObserver(offset).observe(d);
  window.addEventListener("resize", offset);
  return TD;
}
// The page keeps its content above the dock: body padding, job console and
// toasts all follow --tdock-off.
function offset(){
  var on = !!TD && !TD.el.hidden, root = document.documentElement;
  root.classList.toggle("tdock-on", on);
  root.style.setProperty("--tdock-off", on && TD.mode !== "max" ? TD.el.offsetHeight + "px" : "0px");
}
function startResize(e){
  if (TD.mode !== "open") setMode("open");
  e.preventDefault();
  var grip = e.currentTarget, root = document.documentElement;
  var below = innerHeight - TD.el.getBoundingClientRect().bottom;
  try { grip.setPointerCapture(e.pointerId); } catch (x) {}
  root.classList.add("tdock-drag", "tdock-drag-y");    // iframes would swallow the moves
  function move(ev){
    TD.el.style.height = Math.max(140, Math.min(innerHeight - 60, innerHeight - below - ev.clientY)) + "px";
  }
  function up(){
    root.classList.remove("tdock-drag", "tdock-drag-y");
    grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up);
    try { localStorage.setItem(TERM_H, parseInt(TD.el.style.height, 10)); } catch (x) {}
  }
  grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up);
}
function setMode(m){
  TD.mode = m;
  TD.el.classList.toggle("min", m === "min"); TD.el.classList.toggle("max", m === "max");
  TD.minBtn.title = m === "min" ? "restore" : "minimise";
  TD.maxBtn.replaceChildren(icon(m === "max" ? "winrestore" : "winmax"));
  TD.maxBtn.title = m === "max" ? "restore" : "maximise";
  offset(); save();
  if (m !== "min") focusPane(TD.focus);
}
function byId(id){ return TD.sessions.filter(function(s){ return s.id === id; })[0] || null; }
function background(){ return TD.sessions.filter(function(s){ return TD.slots.indexOf(s.id) < 0; }); }
// Change the number of panes, keeping what is on screen (the focused pane wins
// when there are fewer panes than before).
function resize(k){
  var n = LAYOUTS[k].n, f = TD.slots[TD.focus], keep = TD.slots.slice();
  if (n < keep.length){
    keep = keep.filter(function(x){ return x !== null; });
    var at = keep.indexOf(f);
    if (at >= n){ keep.splice(at, 1); keep.splice(n - 1, 0, f); }
  }
  TD.layout = k; TD.slots = [];
  for (var i = 0; i < n; i++) TD.slots.push(i < keep.length ? keep[i] : null);
  TD.focus = Math.max(0, TD.slots.indexOf(f));
}
function setLayout(k, manual){
  if (manual) TD.manual = true;
  resize(k);
  TD.slots.forEach(function(x, i){ if (x === null){ var b = background()[0]; if (b) TD.slots[i] = b.id; } });
  render(); focusPane(TD.focus);
}
function connect(s, force){
  if (s.live && !force) return;
  s.live = true; s.frame.src = termUrl(s.c);
}
// ttyd is served from the same origin, so the dock can see clicks inside a
// terminal (to move the focus ring) and put the keyboard into xterm.
function hookFrame(s){
  try {
    var w = s.frame.contentWindow;
    w.addEventListener("focus", function(){ focusSession(s); });
    w.document.addEventListener("mousedown", function(){ focusSession(s); });
    // Ctrl +/-/0 and Ctrl+wheel size the text of this terminal only
    w.document.addEventListener("keydown", function(e){
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      var d = e.key === "=" || e.key === "+" ? 1 : e.key === "-" ? -1 : e.key === "0" ? 0 : null;
      if (d === null) return;
      e.preventDefault(); e.stopPropagation(); setFont(s, d);
    }, true);
    w.addEventListener("wheel", function(e){
      if (!e.ctrlKey) return;
      e.preventDefault(); setFont(s, e.deltaY < 0 ? 1 : -1);
    }, {passive: false, capture: true});
  } catch (e) {}
  watchFont(s, w);
}
// xterm lives on ttyd's page as window.term. ttyd applies its own fontSize
// when the socket connects (and on every reconnect) and refits, so the pane's
// size is put back whenever the terminal resizes - plus a few checks while
// it is still connecting.
function termOf(s){ try { return s.frame.contentWindow.term || null; } catch (e) { return null; } }
function watchFont(s, w){
  var tries = 0, hooked = null;
  (function tick(){
    if (!w || s.frame.contentWindow !== w) return;       // reloaded or closed
    var t = termOf(s);
    if (t && t !== hooked && t.onResize){
      hooked = t;
      t.onResize(function(){ setTimeout(function(){ applyFont(s); }, 0); });
    }
    applyFont(s);
    if (++tries < 24) setTimeout(tick, 250);
  })();
}
function applyFont(s){
  var t = termOf(s);
  if (t && s.font && t.options.fontSize !== s.font){
    t.options.fontSize = s.font;
    try { t.fit(); } catch (e) {}
  }
  s.fsEl.textContent = String(t ? t.options.fontSize : s.font || TERM_FONT);
}
function setFont(s, d){
  var t = termOf(s), cur = s.font || (t && t.options.fontSize) || TERM_FONT;
  s.font = d === 0 ? TERM_FONT : Math.max(FONT_MIN, Math.min(FONT_MAX, cur + d));
  applyFont(s); save();
}
function focusSession(s){
  var i = TD.slots.indexOf(s.id);
  if (i >= 0 && i !== TD.focus){ TD.focus = i; mark(); save(); }
}
function focusPane(i){
  var s = byId(TD.slots[i]);
  if (!s || TD.mode === "min") return;
  try { var t = s.frame.contentDocument.querySelector("textarea"); (t || s.frame.contentWindow).focus(); }
  catch (e) { s.frame.focus(); }
}
function add(spec, show){
  dock();
  var s = {id: ++termSeq, c: spec.c, name: spec.name || spec.c, lab: spec.lab || "", meta: spec.meta || "", live: false,
           font: spec.font >= FONT_MIN && spec.font <= FONT_MAX ? spec.font : null, bc: spec.bc !== false};
  var same = TD.sessions.filter(function(x){ return x.c === s.c; }).length;
  s.label = same ? s.name + " (" + (same + 1) + ")" : s.name;
  s.pane = el("div", "tpane"); s.pane.hidden = true;
  var head = el("div", "tphead"), r = el("span", "right");
  head.draggable = true; head.title = "drag onto another pane to move it";
  head.appendChild(el("span", "who", s.label));
  head.appendChild(el("span", "meta", [s.lab, s.meta].filter(Boolean).join(" · ")));
  s.fsEl = el("button", "tfs", String(s.font || TERM_FONT));
  s.fsEl.type = "button"; s.fsEl.title = "text size - click to reset (Ctrl 0)";
  s.fsEl.addEventListener("click", function(e){ e.stopPropagation(); setFont(s, 0); });
  r.appendChild(tib("minus", "smaller text (Ctrl -)", function(){ setFont(s, -1); }));
  r.appendChild(s.fsEl);
  r.appendChild(tib("plus", "larger text (Ctrl +)", function(){ setFont(s, 1); }));
  r.appendChild(el("span", "tsep"));
  r.appendChild(tib("redo", "reconnect", function(){ connect(s, true); focusPane(TD.slots.indexOf(s.id)); }));
  r.appendChild(tib("external", "move to a browser tab", function(){ popout(s); }));
  r.appendChild(tib("x", "close this session", function(){ closeSession(s); }));
  head.appendChild(r);
  dragSource(head, s);
  s.frame = el("iframe"); s.frame.title = "CLI " + s.label; s.frame.setAttribute("allow", "clipboard-write");
  s.frame.addEventListener("load", function(){ hookFrame(s); });
  s.pane.appendChild(head); s.pane.appendChild(s.frame);
  s.pane.addEventListener("mousedown", function(){ focusSession(s); });
  TD.stage.appendChild(s.pane);
  TD.sessions.push(s);
  TD.el.hidden = false;
  if (show){
    var i = TD.slots.indexOf(null);
    if (i < 0 && !TD.manual && innerWidth >= 900 && GROW[TD.layout]){ resize(GROW[TD.layout]); i = TD.slots.indexOf(null); }
    if (i < 0) i = TD.focus;                     // the pane's old session stays open as a tab
    TD.slots[i] = s.id; TD.focus = i;
    if (TD.mode === "min") setMode("open");
  }
  render();
  if (show) focusPane(TD.focus);
  return s;
}
function showSession(s){
  var i = TD.slots.indexOf(s.id);
  if (i < 0){ i = TD.focus; TD.slots[i] = s.id; }
  TD.focus = i;
  if (TD.mode === "min") setMode("open");
  render(); focusPane(i);
}
function closeSession(s){
  s.frame.src = "about:blank"; s.pane.remove();
  TD.sessions.splice(TD.sessions.indexOf(s), 1);
  var i = TD.slots.indexOf(s.id);
  if (i >= 0){ var b = background()[0]; TD.slots[i] = b ? b.id : null; }
  if (!TD.sessions.length){
    TD.el.hidden = true; TD.slots = [null]; TD.layout = "1"; TD.manual = false; TD.focus = 0;
    render(); return;
  }
  if (!TD.manual){
    var n = TD.sessions.length, fit = n >= 3 ? "4" : n === 2 ? (TD.layout === "2h" ? "2h" : "2v") : "1";
    if (LAYOUTS[fit].n < LAYOUTS[TD.layout].n) resize(fit);
  }
  render();
}
function closeAll(){
  var n = TD.sessions.length;
  (n > 1 ? confirmDlg("Close all terminals?", "This ends " + n + " CLI sessions.", {ok:"Close all", danger:true})
         : Promise.resolve(true))
    .then(function(ok){ if (ok) TD.sessions.slice().forEach(closeSession); });
}
function popout(s){
  window.open(termUrl(s.c), "_blank", "noopener");
  closeSession(s);
}
function dragSource(node, s){
  node.addEventListener("dragstart", function(e){
    if (TD.mode === "min"){ e.preventDefault(); return; }
    dragId = s.id; e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", s.label); } catch (x) {}
    setTimeout(dropZones, 0);        // changing the DOM inside dragstart cancels the drag in Chrome
  });
  node.addEventListener("dragend", function(){
    dragId = null;
    var z = TD.stage.querySelector(".tdrop"); if (z) z.remove();
  });
}
// While a tab or pane header is dragged, a layer of drop targets covers the
// panes - the iframes underneath would otherwise eat the drag events.
function dropZones(){
  if (dragId === null) return;
  var z = el("div", "tdrop");
  z.style.gridTemplateColumns = TD.stage.style.gridTemplateColumns;
  z.style.gridTemplateRows = TD.stage.style.gridTemplateRows;
  TD.slots.forEach(function(_, i){
    var c = el("div", null, TD.slots.length > 1 ? "pane " + (i + 1) : "show here");
    c.addEventListener("dragover", function(e){ e.preventDefault(); e.dataTransfer.dropEffect = "move"; c.classList.add("over"); });
    c.addEventListener("dragleave", function(){ c.classList.remove("over"); });
    c.addEventListener("drop", function(e){ e.preventDefault(); drop(i); });
    z.appendChild(c);
  });
  TD.stage.appendChild(z);
}
function drop(i){
  var id = dragId, from = TD.slots.indexOf(id);
  if (id === null) return;
  if (from >= 0) TD.slots[from] = TD.slots[i];   // swap with whatever was there
  TD.slots[i] = id; TD.focus = i;
  render(); focusPane(i);
}
// Pane sizes: split.x is the left column's share of the width, split.y the
// top row's share of the height. Dragging a splitter only changes the grid
// tracks, so the sessions stay up; each ttyd refits to its new size.
function tracks(){
  var L = LAYOUTS[TD.layout];
  function t(n, f){ return n > 1 ? "minmax(0, " + f + "fr) minmax(0, " + (1 - f) + "fr)" : "minmax(0, 1fr)"; }
  TD.stage.style.gridTemplateColumns = t(L.c, TD.split.x);
  TD.stage.style.gridTemplateRows = t(Math.ceil(L.n / L.c), TD.split.y);
  TD.stage.querySelectorAll(".tsplit").forEach(function(g){
    g.style[g.dataset.axis === "x" ? "left" : "top"] = "calc(" + (TD.split[g.dataset.axis] * 100) + "% - 4px)";
  });
}
function splitter(axis){
  var g = el("div", "tsplit " + axis);
  g.dataset.axis = axis;
  g.title = "drag to resize the panes " + (axis === "x" ? "left / right" : "up / down") + " - double-click for equal sizes";
  g.addEventListener("pointerdown", function(e){
    e.preventDefault();
    var root = document.documentElement, r = TD.stage.getBoundingClientRect(), raf = 0;
    var min = Math.min(.45, (axis === "x" ? 200 / r.width : 80 / r.height));
    try { g.setPointerCapture(e.pointerId); } catch (x) {}
    root.classList.add("tdock-drag", "tdock-drag-" + axis); g.classList.add("on");
    function move(ev){
      var v = axis === "x" ? (ev.clientX - r.left) / r.width : (ev.clientY - r.top) / r.height;
      TD.split[axis] = Math.max(min, Math.min(1 - min, v));
      if (!raf) raf = requestAnimationFrame(function(){ raf = 0; tracks(); });
    }
    function up(){
      root.classList.remove("tdock-drag", "tdock-drag-" + axis); g.classList.remove("on");
      g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up);
      tracks(); save();
    }
    g.addEventListener("pointermove", move); g.addEventListener("pointerup", up);
  });
  g.addEventListener("dblclick", function(){ TD.split[axis] = .5; tracks(); save(); });
  return g;
}
function render(){
  if (!TD) return;
  var L = LAYOUTS[TD.layout], shown = 0;
  TD.stage.querySelectorAll(".tempty,.tsplit").forEach(function(x){ x.remove(); });
  if (L.c > 1) TD.stage.appendChild(splitter("x"));
  if (L.n > L.c) TD.stage.appendChild(splitter("y"));
  tracks();
  function at(node, i){ node.style.gridRow = String(Math.floor(i / L.c) + 1); node.style.gridColumn = String(i % L.c + 1); }
  TD.sessions.forEach(function(s){
    var i = TD.slots.indexOf(s.id);
    s.pane.hidden = i < 0;
    if (i >= 0){ at(s.pane, i); connect(s); shown++; }
  });
  TD.slots.forEach(function(id, i){
    if (id !== null) return;
    var e = el("div", "tempty", TD.sessions.length > shown ? "click a tab above, or drag one here"
                                                            : "open another CLI from a lab or the map");
    at(e, i); TD.stage.appendChild(e);
  });
  TD.tabs.replaceChildren();
  TD.sessions.forEach(function(s){
    var i = TD.slots.indexOf(s.id), t = el("button", "ttab"), x = el("span", "x");
    t.type = "button"; t.setAttribute("role", "tab"); t.draggable = true;
    t.classList.toggle("shown", i >= 0); t.classList.toggle("idle", !s.live);
    t.title = [s.c, s.lab, s.meta].filter(Boolean).join(" · ") + (s.live ? "" : "\nconnects when shown");
    if (i >= 0 && L.n > 1) t.appendChild(el("span", "n", String(i + 1)));
    if (TD.bc){
      var bx = el("span", "bcx" + (s.bc ? " on" : ""));
      bx.title = s.bc ? "gets broadcast commands - click to leave out" : "left out of broadcasts - click to include";
      bx.appendChild(icon("check"));
      bx.addEventListener("click", function(e){ e.stopPropagation(); s.bc = !s.bc; render(); });
      t.appendChild(bx);
    }
    t.appendChild(el("span", "l", s.label));
    x.title = "close"; x.appendChild(icon("x"));
    x.addEventListener("click", function(e){ e.stopPropagation(); closeSession(s); });
    t.appendChild(x);
    t.addEventListener("click", function(){ showSession(s); });
    t.addEventListener("auxclick", function(e){ if (e.button === 1){ e.preventDefault(); closeSession(s); } });
    dragSource(t, s);
    s.tab = t; TD.tabs.appendChild(t);
  });
  TD.count.textContent = "CLI " + TD.sessions.length;
  TD.sessions.forEach(function(s){ s.pane.classList.toggle("bc", TD.bc && s.bc); });
  bcTargets();
  TD.lay.querySelectorAll(".tib").forEach(function(b){ b.setAttribute("aria-pressed", String(b.dataset.layout === TD.layout)); });
  mark(); offset(); save();
}
function mark(){
  var multi = TD.slots.length > 1;
  TD.sessions.forEach(function(s){
    var f = TD.slots.indexOf(s.id) === TD.focus;
    s.pane.classList.toggle("focus", f && multi);
    if (s.tab){ s.tab.classList.toggle("focus", f); s.tab.setAttribute("aria-selected", String(f)); }
  });
}
// Broadcast: one command line in the dock types the same text into every
// ticked session, as if typed there - xterm's own input path, so the router
// sees keystrokes (no bracketed paste that would stop bash running it).
function cmdBar(){
  var b = el("div", "tcmd"), inp = el("textarea"), send = el("button", "tsend", "Send"), cc = el("button", "tctl", "Ctrl-C");
  b.hidden = true;
  inp.rows = 1; inp.spellcheck = false; inp.autocomplete = "off";
  inp.placeholder = "command for the ticked terminals - Enter sends, Shift+Enter new line, \u2191 \u2193 history, Esc closes";
  send.type = cc.type = "button";
  cc.title = "send Ctrl-C to the ticked terminals (stop a ping, a debug, ...)";
  var p = el("span", "p", "\u00bb"), tgt = el("span", "tgt"), hist = [], at = 0, draft = "";
  try { hist = JSON.parse(localStorage.getItem(TERM_HIST) || "[]") || []; } catch (e) {}
  function go(){
    var v = inp.value;
    if (!broadcast(v.replace(/\r\n?/g, "\n").split("\n").map(function(l){ return l + "\r"; }))) return;
    if (v.trim() && hist[hist.length - 1] !== v){
      hist.push(v); hist = hist.slice(-50);
      try { localStorage.setItem(TERM_HIST, JSON.stringify(hist)); } catch (e) {}
    }
    at = hist.length; inp.value = ""; grow(); inp.focus();
  }
  function browsing(){ return inp.value.indexOf("\n") < 0 || inp.value === hist[at]; }
  function grow(){ inp.style.height = "auto"; inp.style.height = Math.min(inp.scrollHeight, 130) + "px"; }
  inp.addEventListener("input", grow);
  inp.addEventListener("keydown", function(e){
    if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); go(); }
    else if (e.key === "Escape"){ e.preventDefault(); setBroadcast(false); }
    // history while the text is one line or an unedited entry, so the arrows still move in a typed block
    else if (e.key === "ArrowUp" && at > 0 && browsing()){
      e.preventDefault(); if (at === hist.length) draft = inp.value; inp.value = hist[--at]; grow();
    }
    else if (e.key === "ArrowDown" && at < hist.length && browsing()){
      e.preventDefault(); at++; inp.value = at === hist.length ? draft : hist[at]; grow();
    }
  });
  send.addEventListener("click", go);
  cc.addEventListener("click", function(){ broadcast(["\x03"]); inp.focus(); });
  [p, inp, tgt, cc, send].forEach(function(x){ b.appendChild(x); });
  TD.cmd = {bar:b, input:inp, tgt:tgt, send:send, cc:cc};
  return b;
}
function setBroadcast(on, quiet){
  TD.bc = on;
  TD.cmd.bar.hidden = !on;
  TD.el.classList.toggle("bcast", on);
  TD.bcBtn.setAttribute("aria-pressed", String(on));
  if (on && TD.mode === "min") setMode("open");
  render();
  if (on && !quiet) TD.cmd.input.focus();
}
function bcTargets(){
  if (!TD.cmd) return;
  var n = TD.sessions.filter(function(s){ return s.bc; }).length;
  TD.cmd.tgt.textContent = "to " + n + " of " + TD.sessions.length;
  TD.cmd.send.disabled = TD.cmd.cc.disabled = !n;
}
function broadcast(chunks){
  var sent = 0, missed = [];
  TD.sessions.forEach(function(s){
    if (!s.bc) return;
    var t = s.live && termOf(s);
    if (!t){ missed.push(s.label); return; }
    chunks.forEach(function(d){
      try { t._core.coreService.triggerDataEvent(d, true); }
      catch (e) { t.paste(d.replace(/\r$/, "\n")); }
    });
    sent++;
  });
  if (missed.length) toast("Sent to " + sent + ". Not connected yet: " + missed.join(", ") + " - show its tab first.", "warn", 7000);
  else if (!sent) toast("No terminal is ticked.", "warn");
  return sent > 0;
}
function save(){
  if (!TD) return;
  try {
    if (!TD.sessions.length){ sessionStorage.removeItem(TERM_KEY); return; }
    sessionStorage.setItem(TERM_KEY, JSON.stringify({
      sessions: TD.sessions.map(function(s){ return {c:s.c, name:s.name, lab:s.lab, meta:s.meta, font:s.font, bc:s.bc}; }),
      slots: TD.slots.map(function(id){ return TD.sessions.indexOf(byId(id)); }),
      layout: TD.layout, manual: TD.manual, focus: TD.focus, mode: TD.mode, split: TD.split, bc: TD.bc}));
  } catch (e) {}
}
// Back on another page: same tabs and layout, the visible panes log in again,
// the rest connect when they are shown.
function restore(){
  var st = null;
  try { st = JSON.parse(sessionStorage.getItem(TERM_KEY) || "null"); } catch (e) {}
  if (!st || !Array.isArray(st.sessions) || !st.sessions.length) return;
  var made = st.sessions.map(function(x){ return add(x, false); });
  TD.manual = !!st.manual;
  if (st.bc) setBroadcast(true, true);
  ["x", "y"].forEach(function(a){ var v = st.split && +st.split[a]; if (v > .05 && v < .95) TD.split[a] = v; });
  resize(LAYOUTS[st.layout] ? st.layout : "1");
  TD.slots = TD.slots.map(function(_, i){ var k = (st.slots || [])[i]; return made[k] ? made[k].id : null; });
  TD.focus = Math.min(Math.max(0, st.focus | 0), TD.slots.length - 1);
  render();
  setMode(st.mode === "min" || st.mode === "max" ? st.mode : "open");
}
// Open a CLI. The host's ttyd serves a fixed number of sessions (all users and
// pages together), so ask first rather than show a pane that never connects.
function termOpen(container, o){
  o = o || {};
  return fetch("/api/term/info", {cache:"no-store"})
    .then(function(r){ return r.ok ? r.json() : null; }).catch(function(){ return null; })
    .then(function(info){
      if (info && info.max && info.active >= info.max){
        toast("All " + info.max + " web terminals on this host are in use. Close one first.", "crit", 8000);
        return null;
      }
      return add({c:container, name:o.name, lab:o.lab, meta:o.meta}, true);
    });
}
function termToggle(){
  if (TD && TD.sessions.length) setMode(TD.mode === "min" ? "open" : "min");
}
function bootTerm(){ if (rail) restore(); }
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bootTerm);
else setTimeout(bootTerm, 0);

// ---------- keyboard ----------
var pageKeys = [];
function keys(list){ pageKeys = pageKeys.concat(list || []); }
function showKeys(){
  var dl = el("dl", "keys");
  [["Ctrl K", "command palette - jump anywhere"], ["/", "focus the page's search box"], ["g l", "go to labs"],
   ["g c", "go to the catalogue"], ["g b", "go to the builder"], ["g m", "go to manage"], ["t", "toggle light / dark"],
   ["`", "show / hide the terminal dock"], ["?", "this list"], ["Esc", "close a dialog or menu"]].concat(pageKeys.map(function(k){ return [k.keys, k.label]; }))
  .forEach(function(k){
    var dt = el("dt"); String(k[0]).split(" ").forEach(function(x){ dt.appendChild(el("kbd", null, x)); });
    dl.appendChild(dt); dl.appendChild(el("dd", null, k[1]));
  });
  dialog({title:"Keyboard shortcuts", icon:"keyboard", extra:dl, ok:"Got it"}).then(function(){});
  var c = document.querySelector(".dlg footer .ub.quiet"); if (c) c.remove();
}
function typing(e){
  var t = e.target;
  if (t && t.tagName === "INPUT" && /^(checkbox|radio|button|submit|range|color|file)$/i.test(t.type)) return false;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || (t.closest && t.closest(".CodeMirror,.cm-editor,iframe")));
}
var gPending = 0;
document.addEventListener("keydown", function(e){
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && (e.key === "k" || e.key === "K")){
    if (!rail) return;          // pages without the shell (none today) keep the browser default
    e.preventDefault(); if (cmdkOpen) cmdkOpen.close(); else palette(); return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey || typing(e) || cmdkOpen || !rail) return;
  if (document.querySelector(".scrim:not([hidden]),.te-scrim")) return;
  var now = Date.now();
  if (gPending && now - gPending < 900){
    gPending = 0;
    var dest = {l:"/", c:"/catalog.html", b:"/builder.html", m:"/manage.html"}[e.key];
    if (dest){ e.preventDefault(); location.href = dest; }
    return;
  }
  if (e.key === "g"){ gPending = now; return; }
  if (e.key === "?"){ e.preventDefault(); showKeys(); return; }
  if (e.key === "t"){ toggleTheme(); return; }
  if (e.key === "`"){ termToggle(); return; }
  if (e.key === "/"){
    var s = document.querySelector("[data-search]") || document.querySelector("input[type=search]");
    e.preventDefault();
    if (s){ s.focus(); s.select && s.select(); } else palette();
  }
});

window.UI = {icon: icon, appbar: appbar, menu: menu, closeMenu: closeMenu, moreButton: moreButton,
             button: button, seg: seg, tabs: tabs, toggleTheme: toggleTheme, isDark: isDark,
             toast: toast, confirm: confirmDlg, prompt: promptDlg, dialog: dialog,
             commands: commands, keys: keys, palette: palette,
             kindClass: kindClass, kindLabel: kindLabel, state: function(){ return shellState; },
             term: {open: termOpen, url: termUrl, toggle: termToggle}};
})();
