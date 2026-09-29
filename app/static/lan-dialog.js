/*
 * LAN exposure dialog, shared by the lab list and the manage page.
 *
 *   LanDialog.open(lab, onSaved)
 *
 * `lab` is a lab object from /api/state (id, name, nodes, containers, lan).
 * The server proposes addresses (POST /api/lan/suggest): it keeps what the lab
 * already has, fills the rest from the pool, and skips anything that answers
 * on the LAN. The user can edit or untick any node before saving.
 *
 * Built on the shared dialog styles in ui.css (.scrim / .dlg / .utable / .note).
 */
(function(){
"use strict";

var css = ""
+ ".landlg{width:min(680px,100%)}"
+ ".landlg .lead{margin:0 0 14px;color:var(--ink-2);font-size:13px}"
+ ".lanscroll{max-height:46vh;overflow:auto;margin:0 0 12px;border:1px solid var(--line);border-radius:var(--r)}"
+ ".lantbl td{font:12px var(--font-mono);white-space:nowrap;padding:6px 10px}"
+ ".lantbl th{padding:8px 10px}"
+ ".lantbl input[type=text]{width:calc(15ch + 22px);height:28px;padding:0 8px;border-radius:6px;border:1px solid var(--line-strong);"
+ "  background:var(--surface);color:var(--ink);font:12px var(--font-mono)}"
+ ".lantbl input[type=text]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}"
+ ".lantbl td.live{color:var(--ok)}"
+ ".lanmsg{font:11.5px/1.5 var(--font-mono);margin:0 0 12px;padding:8px 11px;border-radius:var(--r);white-space:pre-wrap}"
+ ".lanmsg:empty{display:none}"
+ ".lanmsg.err{background:var(--crit-soft);color:var(--crit)}"
+ ".lanmsg.info{background:var(--accent-soft);color:var(--accent-ink)}"
+ ".lanpre{font:11.5px/1.6 var(--font-mono);background:var(--console-bg);color:var(--console-ink);border:1px solid var(--line);"
+ "  border-radius:var(--r);padding:10px 12px;white-space:pre;overflow:auto;max-height:30vh;margin:0 0 12px}"
+ ".landlg .pool{width:100%;height:34px;padding:0 10px;border-radius:var(--r);border:1px solid var(--line-strong);"
+ "  background:var(--surface);color:var(--ink);font:13px var(--font-mono);margin-bottom:12px}"
+ ".landlg .pool:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}"
+ ".landlg footer .sp{margin-right:auto}";
var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

function el(t,c,x){ var e=document.createElement(t); if(c) e.className=c; if(x!==undefined&&x!==null) e.textContent=x; return e; }
function btn(label, cls, fn){
  var b = el("button", "ub " + (cls||""), label); b.type = "button";
  if (fn) b.addEventListener("click", fn);
  return b;
}
function post(url, body){
  return fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(function(r){ return r.json().then(function(j){ if(!r.ok) throw new Error(j.error||("HTTP "+r.status)); return j; }); });
}
// navigator.clipboard only exists on https / localhost; the dashboard is usually plain http
function copyText(txt){
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(txt);
  return new Promise(function(ok, fail){
    var ta = el("textarea"); ta.value = txt; ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy") ? ok() : fail(); } catch(e){ fail(e); }
    ta.remove();
  });
}

var scrim = null;
function close(){ if(scrim){ scrim.remove(); scrim=null; } }
document.addEventListener("keydown",function(e){ if(e.key==="Escape" && scrim){ e.stopPropagation(); close(); } }, true);

// -> {dlg, body, foot}
function shell(title, sub){
  close();
  scrim = el("div","scrim");
  var d = el("div","dlg wide landlg"); d.setAttribute("role","dialog"); d.setAttribute("aria-modal","true");
  var hd = el("header"), ic = el("div","dlg-ic");
  if (window.UI) ic.appendChild(UI.icon("lan"));
  hd.appendChild(ic);
  var h = el("h3", null, title);
  if (sub){ var s = el("div", null, sub); s.style.cssText = "font:12px var(--font-mono);color:var(--muted);margin-top:3px"; h.appendChild(s); }
  hd.appendChild(h);
  d.appendChild(hd);
  var body = el("div","body"), foot = el("footer");
  d.appendChild(body); d.appendChild(foot);
  scrim.appendChild(d);
  scrim.addEventListener("mousedown",function(e){ if(e.target===scrim) close(); });
  document.body.appendChild(scrim);
  return {dlg:d, body:body, foot:foot};
}

function open(lab, onSaved){
  var name = lab.name || lab.file;
  var s = shell("LAN access", name), m = s.body;
  m.appendChild(el("p","lead","Gives each router's management interface its own address on your LAN. "
    + "The host answers for that address and forwards everything to the node, so you can "
    + "ssh, NETCONF or ping it from any machine on the network. Rules are re-applied "
    + "automatically after redeploys and reboots."));
  var body = el("div",null); body.appendChild(el("div","lanmsg info","asking the host for free addresses…"));
  m.appendChild(body);
  s.foot.appendChild(btn("Cancel","quiet",close));

  post("api/lan/suggest",{lab_id:lab.id}).then(function(sug){
    body.textContent = ""; s.foot.textContent = "";
    if (!sug.pool || !sug.pool.length){ poolSetup(body, s.foot, sug, function(){ open(lab, onSaved); }); return; }
    var cby = {}; (lab.containers||[]).forEach(function(c){ cby[c.short]=c; });
    var cur = (lab.lan && lab.lan.nodes) || {};
    var active = {}; ((lab.lan && lab.lan.active) || []).forEach(function(n){ active[n]=1; });

    var wrap = el("div","lanscroll"), t = el("table","utable lantbl");
    var hr = el("tr");
    var all = el("input"); all.type = "checkbox"; all.title = "all nodes"; all.setAttribute("aria-label","select all nodes");
    var th0 = el("th"); th0.appendChild(all); hr.appendChild(th0);
    ["node","lan address","→ mgmt address","state"].forEach(function(h){ hr.appendChild(el("th",null,h)); });
    var th = el("thead"); th.appendChild(hr); t.appendChild(th);
    var tb = el("tbody"), rows = [];
    (lab.nodes||[]).forEach(function(n){
      var tr = el("tr");
      var cb = el("input"); cb.type="checkbox";
      var ip = sug.nodes[n.name] || "";
      cb.checked = !!ip && (!lab.lan || !lab.lan.enabled || !!cur[n.name] || !Object.keys(cur).length);
      cb.setAttribute("aria-label","expose "+n.name);
      var td0 = el("td"); td0.appendChild(cb); tr.appendChild(td0);
      var nm = el("td",null,n.name); nm.style.fontWeight = "600"; tr.appendChild(nm);
      var inp = el("input"); inp.type="text"; inp.value=ip; inp.spellcheck=false;
      inp.setAttribute("aria-label","LAN address for "+n.name);
      var td2 = el("td"); td2.appendChild(inp); tr.appendChild(td2);
      var c = cby[n.name] || {};
      tr.appendChild(el("td", c.ipv4 ? null : "dim", c.ipv4 || n.mgmt_ipv4 || "at deploy"));
      var state = active[n.name] ? "● live" : (c.state==="running" ? "not exposed" : "lab not running");
      tr.appendChild(el("td", active[n.name] ? "live" : "dim", state));
      tb.appendChild(tr);
      rows.push({node:n.name, cb:cb, inp:inp});
      cb.addEventListener("change", syncAll);
    });
    function syncAll(){
      var n = rows.filter(function(r){ return r.cb.checked; }).length;
      all.checked = n === rows.length && n > 0; all.indeterminate = n > 0 && n < rows.length;
    }
    all.addEventListener("change", function(){ rows.forEach(function(r){ r.cb.checked = all.checked; }); });
    syncAll();
    t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);

    if (sug.unassigned && sug.unassigned.length)
      body.appendChild(el("div","lanmsg err","the pool ran out before " + sug.unassigned.join(", ")
        + " got an address — widen the pool under manage › LAN access, or type one in"));
    var skipped = Object.keys(sug.skipped||{});
    if (skipped.length)
      body.appendChild(el("div","lanmsg info","skipped because something already answers there: "
        + skipped.join(", ")));
    var msg = el("div"); body.appendChild(msg);

    var row = s.foot;
    row.appendChild(btn("Cancel","quiet",close));
    if (lab.lan && lab.lan.enabled){
      var off = btn("Turn off","danger",function(){ save(false); });
      off.title = "remove the LAN addresses from the host; the reservations are kept";
      row.insertBefore(off, row.firstChild); off.classList.add("sp");
    }
    var on = btn(lab.lan && lab.lan.enabled ? "Save" : "Expose on LAN","primary",function(){ save(true); });
    row.appendChild(on);

    function save(enabled){
      var nodes = {};
      rows.forEach(function(r){ if(r.cb.checked && r.inp.value.trim()) nodes[r.node]=r.inp.value.trim(); });
      if (enabled && !Object.keys(nodes).length){
        msg.className="lanmsg err"; msg.textContent="tick at least one node"; return;
      }
      on.disabled = true;
      post("api/lan/lab",{lab_id:lab.id, enabled:enabled, nodes:nodes}).then(function(res){
        if (onSaved) onSaved(res);
        if (!enabled){ close(); return; }
        showResult(lab, nodes, res);
      }).catch(function(e){ on.disabled=false; msg.className="lanmsg err"; msg.textContent=e.message; });
    }
  }).catch(function(e){
    body.textContent=""; body.appendChild(el("div","lanmsg err",e.message));
    s.foot.textContent = ""; s.foot.appendChild(btn("Close","",close));
  });
}

// First use on a host: the dashboard does not know which addresses of this
// LAN are free to take, so it asks - proposing the top of the host's subnet.
function poolSetup(body, foot, sug, done){
  body.appendChild(el("div","lanmsg info","Pick the addresses on your LAN (" + (sug.iface||"?")
    + ") the dashboard may give to lab nodes. Use a range your DHCP server does not hand out"
    + " — anything that already answers is skipped anyway. You can change this later under"
    + " Manage › LAN access."));
  var inp = el("input","pool"); inp.type="text"; inp.spellcheck=false;
  inp.value = sug.pool_hint || ""; inp.placeholder = "192.168.1.200-192.168.1.239";
  inp.setAttribute("aria-label","address range");
  body.appendChild(inp);
  var msg = el("div"); body.appendChild(msg);
  var ok = btn("Use this range","primary",function(){
    var pool = inp.value.split(/[\s,]+/).filter(Boolean);
    if (!pool.length){ msg.className="lanmsg err"; msg.textContent="enter a range"; return; }
    ok.disabled = true;
    post("api/lan/settings",{pool:pool}).then(done)
      .catch(function(e){ ok.disabled=false; msg.className="lanmsg err"; msg.textContent=e.message; });
  });
  inp.addEventListener("keydown",function(e){ if(e.key==="Enter") ok.click(); });
  foot.appendChild(btn("Cancel","quiet",close)); foot.appendChild(ok);
  inp.focus();
}

function showResult(lab, nodes, res){
  var s = shell("LAN access", lab.name||lab.file), m = s.body;
  var running = (lab.containers||[]).some(function(c){ return c.state==="running"; });
  var users = {}; (lab.nodes||[]).forEach(function(n){ users[n.name]=n.user||"clab"; });
  var g = (res && res.gateway) || {};
  var mode = {}, notes = [], dead = [];
  ((res && res.active) || []).forEach(function(a){
    if (a.lab !== (lab.name||lab.file)) return;
    mode[a.node] = a.ssh;
    if (a.note) notes.push(a.node + ": " + a.note);
    if (a.reachable === false) dead.push(a.node);
  });
  var names = Object.keys(nodes).sort();
  var waiting = names.filter(function(n){ return mode[n]==="none"; });
  var viaGw = names.filter(function(n){ return mode[n]==="gateway"; });
  m.appendChild(el("p","lead", !running ? "Saved. The addresses go live as soon as the lab is running:"
    : waiting.length ? "Exposed. Not every node answers on SSH yet:" : "Done. These nodes answer on your LAN now:"));
  var logins = (res && res.logins) || {};
  var lines = names.map(function(n){
    var lg = logins[n], user = lg ? lg[0] : (mode[n]==="gateway" ? g.user : users[n]);
    var note = mode[n]==="gateway" ? " (SSH gateway)"
      : mode[n]==="none" ? " (nothing on port 22 yet - still booting?)" : "";
    return ("ssh " + user + "@" + nodes[n]).padEnd(34) + "# " + n
      + (lg ? "  password " + lg[1] : "") + note;
  });
  m.appendChild(el("div","lanpre", lines.join("\n")));
  if (viaGw.length) m.appendChild(el("div","lanmsg info",
    viaGw.join(", ") + (viaGw.length>1?" run":" runs") + " no SSH server of its own, so port 22 goes to the "
    + "dashboard's SSH gateway: log in as " + g.user + " / " + g.password + " and you land in the node's "
    + "CLI; ssh -t … shell gives a shell, ssh … \"show …\" runs one command."));
  if (waiting.length) m.appendChild(el("div","lanmsg err",
    waiting.join(", ") + ": no SSH server answers yet" + (g.running ? "" : " and the SSH gateway is not running ("
    + (g.error||"?") + ")") + ". Routers that boot a VM take a few minutes; ping works already."));
  if (notes.length) m.appendChild(el("div","lanmsg info", notes.join("\n")));
  if (dead.length) m.appendChild(el("div","lanmsg err", dead.join(", ") + ": the node's own management "
    + "address does not answer, so its LAN address cannot either. Check the node's management "
    + "interface (it may be down or still starting)."));
  if (res && res.last_error) m.appendChild(el("div","lanmsg err", res.last_error));
  var row = s.foot;
  var cfg = btn("ssh config","sp");
  cfg.title = "a ~/.ssh/config snippet with one Host entry per node";
  cfg.addEventListener("click",function(){
    fetch("api/lan/ssh-config?lab="+encodeURIComponent(lab.id)).then(function(r){return r.json();})
      .then(function(j){
        var pre = m.querySelector(".lanpre"); pre.textContent = j.text || j.error;
        cfg.disabled = true;
      });
  });
  var copy = btn("copy","");
  copy.addEventListener("click",function(){
    var txt = m.querySelector(".lanpre").textContent;
    copyText(txt).then(function(){ copy.textContent="copied"; },
                       function(){ copy.textContent="copy failed - select the text"; });
  });
  var ok = btn("Close","primary",close);
  row.appendChild(cfg); row.appendChild(copy); row.appendChild(ok);
}

window.LanDialog = {open: open, close: close};
})();
