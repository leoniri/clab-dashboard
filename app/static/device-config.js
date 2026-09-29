/*
 * Device configuration dialog for running IOS-XE / IOS-XR / NX-OS / FRR / SR Linux nodes.
 *
 *   DeviceConfig.open(lab, nodeName, opts)
 *
 * lab      a lab object from /api/state (only lab.id and lab.name are used)
 * nodeName the node's name in the topology
 * opts     optional {tab: "running" | "push", onSaved: fn(result)}
 *
 * Talks to:
 *   GET  api/device/running?lab=&node=      cleaned running-config
 *   POST api/device/config {lab_id, node, config, save_startup}
 *   POST api/device/save-startup {lab_id, node}
 *
 * Everything the server needs (address, platform, credentials) is resolved on
 * the server from the lab id and node name. Styles are prefixed dc-; colours,
 * fonts and the buttons (.ub) come from ui.css.
 */
(function(){
"use strict";

var css = ""
+ ".dc-scrim{position:fixed;inset:0;z-index:92;background:rgba(3,6,10,.62);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);"
+ "  display:flex;align-items:center;justify-content:center;padding:clamp(8px,2.5vw,24px)}"
+ ".dc-box{background:var(--surface,#0d1117);color:var(--ink,#e6edf3);border:1px solid var(--line-strong,#2b3746);"
+ "  border-radius:14px;box-shadow:0 30px 80px rgba(0,0,0,.5);width:min(1000px,100%);height:min(780px,100%);"
+ "  display:flex;flex-direction:column;overflow:hidden;font-family:var(--font-sans,system-ui,sans-serif);font-size:13px}"
+ ".dc-head{display:flex;align-items:center;gap:12px;padding:14px 16px 12px;flex-wrap:wrap}"
+ ".dc-ic{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;flex:none;"
+ "  background:var(--accent-soft,rgba(43,217,197,.12));color:var(--accent-ink,#6cf0df)}"
+ ".dc-ic svg{width:18px;height:18px}"
+ ".dc-head h3{margin:0;font:600 17px var(--font-display,system-ui);letter-spacing:-.01em}"
+ ".dc-sub{font:11.5px var(--font-mono,monospace);color:var(--muted,#6e7d8e)}"
+ ".dc-ttl{display:flex;flex-direction:column;gap:1px;min-width:0}"
+ ".dc-sp{margin-left:auto}"
+ ".dc-tabs{display:flex;gap:2px;padding:0 16px;border-bottom:1px solid var(--line,#1b2430)}"
+ ".dc-tab{background:none;border:0;border-bottom:2px solid transparent;padding:8px 11px 9px;cursor:pointer;margin-bottom:-1px;"
+ "  font:500 12.5px var(--font-sans,system-ui);color:var(--muted,#6e7d8e)}"
+ ".dc-tab:hover{color:var(--ink,#e6edf3)}"
+ ".dc-tab[aria-selected=true]{color:var(--ink,#e6edf3);border-bottom-color:var(--accent,#2bd9c5);font-weight:600}"
+ ".dc-body{flex:1;display:flex;flex-direction:column;min-height:0;padding:14px 16px 16px;gap:10px}"
+ ".dc-body[hidden]{display:none}"
+ ".dc-pre,.dc-ta,.dc-out{font:12px/1.55 var(--font-mono,monospace);border:1px solid var(--line,#1b2430);border-radius:8px;margin:0}"
+ ".dc-pre{flex:1;overflow:auto;white-space:pre;padding:11px 13px;background:var(--console-bg,#05070a);color:var(--console-ink,#c9d5e0)}"
+ ".dc-ta{flex:1;min-height:120px;resize:none;padding:11px 13px;background:var(--console-bg,#05070a);color:var(--console-ink,#c9d5e0);"
+ "  white-space:pre;tab-size:2;caret-color:var(--accent,#2bd9c5)}"
+ ".dc-ta:focus{outline:none;border-color:var(--accent,#2bd9c5);box-shadow:0 0 0 3px var(--accent-soft,rgba(43,217,197,.12))}"
+ ".dc-out{flex:1;min-height:90px;overflow:auto;white-space:pre-wrap;word-break:break-word;padding:10px 13px;"
+ "  background:var(--console-bg,#05070a);color:var(--console-ink,#c9d5e0)}"
+ ".dc-out .dc-err{color:#ff7b86;font-weight:600}"
+ ".dc-out .dc-okl{color:#4ade80}"
+ ".dc-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}"
+ ".dc-chk{display:flex;gap:8px;align-items:center;font-size:12.5px;cursor:pointer;color:var(--ink-2,#a9b6c4)}"
+ ".dc-msg{font:11.5px var(--font-mono,monospace);padding:8px 11px;border-radius:8px}"
+ ".dc-msg:empty{display:none}"
+ ".dc-msg.dc-e{background:var(--crit-soft);color:var(--crit)}"
+ ".dc-msg.dc-o{background:var(--ok-soft);color:var(--ok)}"
+ ".dc-msg.dc-i{background:var(--accent-soft);color:var(--accent-ink)}"
+ ".dc-hint{font-size:12px;color:var(--muted,#6e7d8e);margin:0;line-height:1.5}";
var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

function el(t,c,x){ var e=document.createElement(t); if(c) e.className=c; if(x!==undefined&&x!==null) e.textContent=x; return e; }
function req(url, body){
  var o = body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}
               : {cache:"no-store"};
  return fetch(url, o).then(function(r){
    return r.json().then(function(j){ if(!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; });
  });
}

var scrim = null, busy = false;
function close(force){
  if (busy && force !== true){
    var ask = window.UI && UI.confirm
      ? UI.confirm("Close the configuration?", "A command is still running on the router. It keeps running if you close this.",
                   {ok:"Close anyway", danger:true})
      : Promise.resolve(window.confirm("A command is still running on the router. Close anyway? (it keeps running)"));
    ask.then(function(ok){ if (ok) close(true); });
    return;
  }
  if (scrim){ scrim.remove(); scrim = null; }
  document.removeEventListener("keydown", onKey, true);
}
function onKey(e){
  // a confirm dialog on top handles its own Escape
  if (e.key === "Escape" && !document.querySelector(".scrim")){ e.stopPropagation(); close(); }
}

function paintOutput(box, text){
  box.textContent = "";
  text.split("\n").forEach(function(line){
    var cls = /^\s*(% |%%|!!%|---.*(fail|reject|abort))/i.test(line) || /Failed to commit|SEMANTIC ERRORS/.test(line)
            ? "dc-err" : /^--- XR reports/.test(line) ? "dc-okl" : null;
    box.appendChild(el("div", cls, line || " "));
  });
  box.scrollTop = box.scrollHeight;
}

function open(lab, node, opts){
  opts = opts || {};
  if (scrim) { scrim.remove(); scrim = null; }
  scrim = el("div","dc-scrim");
  var box = el("div","dc-box"); box.setAttribute("role","dialog"); box.setAttribute("aria-modal","true");
  box.setAttribute("aria-label","Configuration of " + node);
  scrim.appendChild(box);
  scrim.addEventListener("mousedown", function(e){ if (e.target === scrim) close(); });
  document.addEventListener("keydown", onKey, true);

  // header
  var head = el("div","dc-head");
  var ic = el("div","dc-ic"); if (window.UI) ic.appendChild(UI.icon("gear")); head.appendChild(ic);
  var ttl = el("div","dc-ttl");
  ttl.appendChild(el("h3", null, node));
  var sub = el("span","dc-sub", (lab.name || "") + " · configuration");
  ttl.appendChild(sub); head.appendChild(ttl);
  head.appendChild(el("span","dc-sp"));
  var saveBtn = el("button","ub sm","Save running → startup");
  saveBtn.title = "write this router's running-config into its startup-config file, so a redeploy "
                + "brings it back exactly as it is now (the previous file is kept in history)";
  head.appendChild(saveBtn);
  var x = el("button","ub sm quiet","Close"); x.addEventListener("click", function(){ close(); }); head.appendChild(x);
  box.appendChild(head);

  // tabs
  var tabs = el("div","dc-tabs"); tabs.setAttribute("role","tablist");
  var tRun = el("button","dc-tab","Running config"), tPush = el("button","dc-tab","Push config");
  [tRun, tPush].forEach(function(t){ t.setAttribute("role","tab"); t.type = "button"; tabs.appendChild(t); });
  box.appendChild(tabs);

  // running-config pane
  var pRun = el("div","dc-body");
  var runRow = el("div","dc-row");
  var refresh = el("button","ub sm","Refresh"), copy = el("button","ub sm","Copy");
  var runInfo = el("span","dc-sub","");
  runRow.appendChild(refresh); runRow.appendChild(copy); runRow.appendChild(runInfo);
  var pre = el("pre","dc-pre","");
  var runMsg = el("div","dc-msg");
  pRun.appendChild(runRow); pRun.appendChild(runMsg); pRun.appendChild(pre);
  var nd = ((lab.nodes || []).filter(function(n){ return n.name === node; })[0]) || {};
  var plat = nd.kind === "nokia_srlinux" ? "srl"
           : (nd.kind === "frr" || (nd.kind === "linux" && /frr/.test(nd.image || ""))) ? "frr"
           : nd.kind === "cisco_xrd_vrouter" ? "xr" : "ios";
  pRun.appendChild(el("p","dc-hint", plat === "srl"
    ? "Shown as flat `set` commands, the way it is saved to the node's .cli startup file. What "
      + "containerlab's bootstrap owns (management, TLS, gNMI/JSON-RPC, AAA, the factory ACLs) is left out."
    : plat === "frr"
    ? "Shown as it is saved to the frr.conf the topology bind-mounts into the container."
    : "Shown as it would be saved to the startup file: vrnetlab's own "
      + "management settings (management VRF / interface, clab user) and certificates are left out, "
      + "because the router gets those from containerlab at boot."));
  box.appendChild(pRun);

  // push pane
  var pPush = el("div","dc-body");
  pPush.appendChild(el("p","dc-hint", plat === "srl"
    ? "Type flat SR Linux commands, one per line (set / … or delete / …). They go into a private "
      + "candidate that is committed only if every line is accepted. Ctrl+Enter applies."
    : plat === "frr"
    ? "Type vtysh configuration commands, one per line — no 'configure terminal'. FRR applies them "
      + "line by line (like IOS-XE). Tab inserts spaces; Ctrl+Enter applies."
    : "Type configuration-mode commands, one per line — no 'configure terminal', "
      + "'end' or 'commit'. IOS-XR applies them as one commit (all or nothing); IOS-XE applies them line by "
      + "line. Tab inserts spaces; Ctrl+Enter applies."));
  var ta = el("textarea","dc-ta"); ta.spellcheck = false; ta.setAttribute("aria-label","configuration to push");
  ta.placeholder = plat === "srl"
    ? "set / interface lo0 admin-state enable\nset / interface lo0 subinterface 99 ipv4 address 10.99.99.1/32"
    : plat === "frr" ? "interface lo\n ip address 10.99.99.1/32\nexit"
    : "interface Loopback99\n description example\n ip address 10.99.99.1 255.255.255.255";
  pPush.appendChild(ta);
  var pushRow = el("div","dc-row");
  var chkL = el("label","dc-chk"), chk = el("input"); chk.type = "checkbox"; chk.checked = true;
  chkL.appendChild(chk); chkL.appendChild(document.createTextNode("also save to the startup file so a redeploy keeps it"));
  pushRow.appendChild(chkL); pushRow.appendChild(el("span","dc-sp"));
  var apply = el("button","ub primary","Apply"); apply.title = "Ctrl+Enter"; pushRow.appendChild(apply);
  pPush.appendChild(pushRow);
  var pushMsg = el("div","dc-msg"); pPush.appendChild(pushMsg);
  var out = el("div","dc-out"); out.setAttribute("aria-live","polite"); out.textContent = "device session output appears here";
  pPush.appendChild(out);
  box.appendChild(pPush);

  function show(which){
    tRun.setAttribute("aria-selected", which === "running" ? "true" : "false");
    tPush.setAttribute("aria-selected", which === "push" ? "true" : "false");
    pRun.hidden = which !== "running"; pPush.hidden = which !== "push";
    if (which === "push") ta.focus(); else if (!pre.textContent) loadRunning();
  }
  tRun.addEventListener("click", function(){ show("running"); });
  tPush.addEventListener("click", function(){ show("push"); });

  function msg(elm, cls, text){ elm.className = "dc-msg " + (cls || ""); elm.textContent = text || ""; }

  function loadRunning(){
    refresh.disabled = true; msg(runMsg, "dc-i", "fetching running-config from " + node + "…");
    req("api/device/running?lab=" + encodeURIComponent(lab.id) + "&node=" + encodeURIComponent(node))
      .then(function(j){
        pre.textContent = j.text; msg(runMsg, "", "");
        runInfo.textContent = j.platform + " · " + j.text.split("\n").length + " lines · " + new Date().toLocaleTimeString();
      })
      .catch(function(e){ msg(runMsg, "dc-e", e.message); })
      .then(function(){ refresh.disabled = false; });
  }
  refresh.addEventListener("click", loadRunning);
  copy.addEventListener("click", function(){
    if (navigator.clipboard && pre.textContent)
      navigator.clipboard.writeText(pre.textContent).then(function(){ copy.textContent = "Copied";
        setTimeout(function(){ copy.textContent = "Copy"; }, 1500); });
  });

  function saved(r){
    var t = "saved to " + r.path + (r.created ? " (new file)" : "")
          + (r.topology_updated ? " and pointed the topology at it" : "")
          + (r.saved_version ? " · previous version kept in history" : "");
    if (opts.onSaved) try { opts.onSaved(r); } catch(e){}
    return t;
  }

  apply.addEventListener("click", function(){
    if (!ta.value.trim()){ msg(pushMsg, "dc-e", "nothing to apply"); return; }
    busy = true; apply.disabled = true; saveBtn.disabled = true;
    msg(pushMsg, "dc-i", "applying on " + node + (chk.checked ? ", then saving to the startup file" : "")
        + " — IOS-XR commits can take a few seconds…");
    out.textContent = "";
    req("api/device/config", {lab_id: lab.id, node: node, config: ta.value, save_startup: chk.checked})
      .then(function(r){
        paintOutput(out, r.output || "");
        if (r.ok){
          msg(pushMsg, "dc-o", "applied " + r.lines + " line" + (r.lines === 1 ? "" : "s") + " in " + r.seconds + " s"
              + (r.saved ? " · " + saved(r.saved) : ""));
          pre.textContent = "";          // stale now; refetch when the tab is opened
        } else {
          msg(pushMsg, "dc-e", (r.platform === "IOS-XR" || r.platform === "SR Linux"
                                ? "nothing was applied: " : "some lines were rejected: ")
              + (r.errors || []).slice(0, 3).join(" · ") + (r.note ? " — " + r.note : "")
              + (r.saved ? " · " + saved(r.saved) : ""));
        }
      })
      .catch(function(e){ msg(pushMsg, "dc-e", e.message); })
      .then(function(){ busy = false; apply.disabled = false; saveBtn.disabled = false; });
  });
  ta.addEventListener("keydown", function(e){
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey){
      e.preventDefault(); ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
    } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)){ e.preventDefault(); apply.click(); }
  });

  saveBtn.addEventListener("click", function(){
    busy = true; saveBtn.disabled = true; apply.disabled = true;
    var target = pRun.hidden ? pushMsg : runMsg;
    msg(target, "dc-i", "reading the running-config of " + node + " and saving it…");
    req("api/device/save-startup", {lab_id: lab.id, node: node})
      .then(function(r){ msg(target, "dc-o", saved(r)); })
      .catch(function(e){ msg(target, "dc-e", e.message); })
      .then(function(){ busy = false; saveBtn.disabled = false; apply.disabled = false; });
  });

  document.body.appendChild(scrim);
  show(opts.tab === "push" ? "push" : "running");
}

window.DeviceConfig = {open: open, close: close};
})();
