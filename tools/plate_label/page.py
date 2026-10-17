"""The single self-contained HTML page served at GET / (inline CSS and JS, no CDN, no network beyond
the loopback API). New code for PLATE. The reason-code chips are generated from the code table
(calib/reason_codes.yaml) so the UI cannot drift from it. The page never shows a gate score or verdict:
a rater who sees the gates' opinion is no longer an independent label for calibrating them.
"""

from __future__ import annotations

from html import escape

from plate_label.codes import reason_codes, reason_groups


def _reason_groups() -> str:
    table = reason_codes()
    parts = []
    for prefix, title in reason_groups().items():
        chips = "".join(
            f'<button type="button" class="chip" data-code="{escape(code)}" title="{escape(desc)}">'
            f"<b>{escape(code)}</b> {escape(desc)}</button>"
            for code, desc in table.items()
            if code.startswith(prefix)
        )
        if chips:
            parts.append(f"<fieldset><legend>{escape(title)}</legend>{chips}</fieldset>")
    return "\n".join(parts)


def render_page(nonce: str, rater: str = "") -> str:
    """The labelling page; `nonce` goes on the inline style and script for the CSP header."""
    return (
        _TEMPLATE.replace("__NONCE__", nonce)
        .replace("__REASONS__", _reason_groups())
        .replace("__RATER__", escape(rater))
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PLATE labeller</title>
<style nonce="__NONCE__">
  :root { color-scheme: dark; --bg:#101114; --panel:#17181d; --line:#2a2c35; --fg:#e8e8ec; --dim:#8b8d99;
          --acc:#b9b2ff; --ship:#5fd39a; --fix:#f2c14e; --render:#ff7a7a; }
  * { box-sizing: border-box; }
  [hidden] { display:none !important; }
  html, body { height:100%; margin:0; background:var(--bg); color:var(--fg);
               font:14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #app { display:grid; grid-template-columns: 1fr 360px; grid-template-rows: auto 1fr auto auto; height:100%; }
  header { grid-column:1 / 3; display:flex; gap:16px; align-items:baseline; padding:8px 14px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  #counter { font-variant-numeric: tabular-nums; color:var(--acc); }
  #name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #timer { font-variant-numeric: tabular-nums; color:var(--dim); }
  #badge { font-size:12px; padding:1px 8px; border-radius:10px; border:1px solid var(--line); color:var(--dim); }
  #badge.done { color:var(--ship); border-color:var(--ship); }
  #stage { grid-column:1; grid-row:2; position:relative; overflow:hidden; background:#08080a; min-height:0;
           cursor:zoom-in; display:flex; align-items:center; justify-content:center; }
  #stage.zoom { overflow:auto; display:block; cursor:zoom-out; }
  #frame { position:relative; flex:none; }
  #frame img { position:absolute; left:0; top:0; width:100%; height:100%; object-fit:contain; display:block;
               user-select:none; -webkit-user-drag:none; }
  #line { position:absolute; top:0; bottom:0; width:14px; margin-left:-7px; cursor:ew-resize; z-index:2; }
  #line::after { content:""; position:absolute; left:6px; top:0; bottom:0; width:2px; background:var(--acc);
                 box-shadow:0 0 0 1px rgba(0,0,0,.6); }
  .tag { position:absolute; top:6px; z-index:3; font-size:11px; padding:1px 6px; border-radius:8px;
         background:rgba(0,0,0,.65); color:var(--fg); pointer-events:none; letter-spacing:.04em; }
  #tagL { left:6px; } #tagR { right:6px; }
  #bar { grid-column:1; grid-row:3; display:flex; gap:10px; align-items:center; padding:6px 14px;
         border-top:1px solid var(--line); background:var(--panel); color:var(--dim); font-size:12px; }
  #wipe { flex:1; accent-color: var(--acc); }
  aside { grid-column:2; grid-row:2 / 4; overflow:auto; padding:12px 14px; border-left:1px solid var(--line);
          background:var(--panel); }
  .verdicts { display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; margin-bottom:12px; }
  button { font:inherit; color:var(--fg); background:#20222a; border:1px solid var(--line); border-radius:6px;
           padding:8px 6px; cursor:pointer; }
  button:hover { border-color:var(--acc); }
  button.verdict.on[data-verdict="ship"] { background:var(--ship); color:#06130d; border-color:var(--ship); }
  button.verdict.on[data-verdict="fix"] { background:var(--fix); color:#171102; border-color:var(--fix); }
  button.verdict.on[data-verdict="render_it"] { background:var(--render); color:#1a0606; border-color:var(--render); }
  kbd { font:12px ui-monospace, Menlo, Consolas, monospace; opacity:.8; margin-right:4px; }
  fieldset { border:1px solid var(--line); border-radius:6px; margin:0 0 8px; padding:4px 6px 6px; }
  legend { color:var(--dim); font-size:12px; padding:0 4px; letter-spacing:.04em; text-transform:uppercase; }
  .chip { display:inline-block; margin:2px 3px 2px 0; padding:2px 8px; font-size:12px; border-radius:11px; }
  .chip b { font:12px ui-monospace, Menlo, Consolas, monospace; color:var(--acc); }
  .chip.on { background:var(--acc); color:#0c0a24; border-color:var(--acc); }
  .chip.on b { color:#0c0a24; }
  #codes { width:100%; margin:6px 0 8px; padding:7px 8px; color:var(--fg); background:#0d0e12;
           border:1px solid var(--line); border-radius:6px; font:13px ui-monospace, Menlo, Consolas, monospace; }
  #codes.bad { border-color:var(--render); }
  .row { display:flex; gap:8px; }
  #navrow { margin-top:8px; }
  #rater { color:var(--dim); }
  .row button { flex:1; }
  #save { background:var(--acc); color:#0c0a24; border-color:var(--acc); font-weight:600; }
  #msg { min-height:20px; margin-top:8px; color:var(--dim); }
  #msg.err { color:var(--render); }
  #msg.ok { color:var(--ship); }
  footer { grid-column:1 / 3; padding:5px 14px; border-top:1px solid var(--line); background:var(--panel);
           color:var(--dim); font-size:12px; }
  @media (max-width: 860px) { #app { grid-template-columns: 1fr; } header, footer { grid-column:1; }
    aside { grid-column:1; grid-row:auto; } }
</style>
</head>
<body>
<div id="app">
  <header>
    <span id="counter">- / -</span>
    <span id="name">loading...</span>
    <span id="timer" title="time on this pair">0.0 s</span>
    <span id="badge">unlabelled</span>
    <span id="rater">__RATER__</span>
  </header>
  <div id="stage" title="click: toggle 100% view at that point">
    <div id="frame">
      <img id="plate" alt="plate">
      <img id="cand" alt="candidate">
      <div id="line" hidden></div>
      <span class="tag" id="tagL" hidden>plate</span><span class="tag" id="tagR" hidden>candidate</span>
    </div>
  </div>
  <div id="bar">
    <span>plate</span><input id="wipe" type="range" min="0" max="100" value="50" aria-label="wipe"><span>candidate</span>
  </div>
  <aside>
    <div class="verdicts">
      <button type="button" class="verdict" data-verdict="ship"><kbd>1</kbd> Ship</button>
      <button type="button" class="verdict" data-verdict="fix"><kbd>2</kbd> Fix</button>
      <button type="button" class="verdict" data-verdict="render_it"><kbd>3</kbd> Render it</button>
    </div>
    <div id="picker" hidden>
__REASONS__
      <input id="codes" type="text" maxlength="200" placeholder="reason codes, e.g. A2 P4 (Enter saves)" autocomplete="off" spellcheck="false">
      <div class="row"><button id="save" type="button">Save (Enter)</button></div>
    </div>
    <div class="row" id="navrow">
      <button id="next" type="button"><kbd>n</kbd> Next</button>
      <button id="undo" type="button"><kbd>u</kbd> Undo</button>
    </div>
    <div id="msg" role="status"></div>
  </aside>
  <footer>1 ship &middot; 2 fix &middot; 3 render it, then reason codes and Enter &middot; n next &middot; u undo &middot; z 100% &middot; t flip plate/candidate &middot; [ ] wipe &middot; &larr; &rarr; browse &middot; Esc cancel</footer>
</div>
<script nonce="__NONCE__">
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var stage = $("stage"), frame = $("frame"), plate = $("plate"), cand = $("cand"), msg = $("msg");
  var codesBox = $("codes");
  var cur = null, verdict = null, busy = false, shownAt = 0, wipePct = 50, flipped = null;
  var selected = [];

  function setMsg(text, cls) { msg.textContent = text || ""; msg.className = cls || ""; }
  function imgUrl(i, role, v) { return "/img/" + i + "/" + role + "?v=" + encodeURIComponent(v || "0"); }

  // --- wipe -----------------------------------------------------------------------------
  function applyWipe() {
    var has = cur && cur.has_plate;
    cand.style.clipPath = has ? "inset(0 0 0 " + wipePct + "%)" : "none";
    $("line").style.left = wipePct + "%";
    $("wipe").value = String(Math.round(wipePct));
  }
  function setWipe(p) { wipePct = Math.min(100, Math.max(0, p)); applyWipe(); }
  $("wipe").addEventListener("input", function () { setWipe(Number($("wipe").value)); });
  $("line").addEventListener("pointerdown", function (e) {
    e.preventDefault(); e.stopPropagation();
    var line = $("line");
    line.setPointerCapture(e.pointerId);
    function move(ev) {
      var r = frame.getBoundingClientRect();
      if (r.width) { setWipe(100 * (ev.clientX - r.left) / r.width); }
    }
    function up() { line.removeEventListener("pointermove", move); line.removeEventListener("pointerup", up); }
    line.addEventListener("pointermove", move);
    line.addEventListener("pointerup", up);
  });
  $("line").addEventListener("click", function (e) { e.stopPropagation(); });

  // --- fit / 100% -----------------------------------------------------------------------
  var natW = 0, natH = 0;
  function layout(focus) {
    if (!natW || !natH) { frame.style.width = "0px"; frame.style.height = "0px"; return; }
    var dpr = window.devicePixelRatio || 1, w, h;
    if (stage.classList.contains("zoom")) { w = natW / dpr; h = natH / dpr; }
    else {
      var s = Math.min(stage.clientWidth / natW, stage.clientHeight / natH);
      w = natW * s; h = natH * s;
    }
    frame.style.width = w + "px"; frame.style.height = h + "px";
    if (focus) {
      stage.scrollLeft = focus.x * w - stage.clientWidth / 2;
      stage.scrollTop = focus.y * h - stage.clientHeight / 2;
    }
  }
  function fit() { stage.classList.remove("zoom"); layout(null); }
  function zoomAt(fx, fy) { stage.classList.add("zoom"); layout({ x: fx, y: fy }); }
  stage.addEventListener("click", function (e) {
    if (stage.classList.contains("zoom")) { fit(); return; }
    var r = frame.getBoundingClientRect();
    if (!r.width || !r.height) { return; }
    zoomAt(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
           Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)));
  });
  window.addEventListener("resize", function () { layout(null); });

  // --- verdict + reasons ----------------------------------------------------------------
  function syncCodes() {
    codesBox.value = selected.join(" ");
    codesBox.classList.remove("bad");
    document.querySelectorAll(".chip").forEach(function (c) {
      c.classList.toggle("on", selected.indexOf(c.getAttribute("data-code")) >= 0);
    });
  }
  function setVerdict(v) {
    verdict = v;
    document.querySelectorAll("button.verdict").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-verdict") === v);
    });
    $("picker").hidden = !(v === "fix" || v === "render_it");
    if (!$("picker").hidden) { codesBox.focus(); }
  }
  function resetForm() { selected = []; syncCodes(); setVerdict(null); codesBox.blur(); }

  document.querySelectorAll(".chip").forEach(function (c) {
    c.addEventListener("click", function () {
      var code = c.getAttribute("data-code"), i = selected.indexOf(code);
      if (i >= 0) { selected.splice(i, 1); } else { selected.push(code); }
      syncCodes();
    });
  });
  codesBox.addEventListener("input", function () {
    var valid = {};
    document.querySelectorAll(".chip").forEach(function (c) { valid[c.getAttribute("data-code")] = true; });
    var toks = codesBox.value.toUpperCase().split(/[\\s,]+/).filter(Boolean), ok = true, next = [];
    toks.forEach(function (t) {
      if (valid[t]) { if (next.indexOf(t) < 0) { next.push(t); } } else { ok = false; }
    });
    selected = next;
    document.querySelectorAll(".chip").forEach(function (c) {
      c.classList.toggle("on", selected.indexOf(c.getAttribute("data-code")) >= 0);
    });
    codesBox.classList.toggle("bad", !ok);
  });
  document.querySelectorAll("button.verdict").forEach(function (b) {
    b.addEventListener("click", function () { pick(b.getAttribute("data-verdict")); });
  });

  // --- loading pairs --------------------------------------------------------------------
  function show(d) {
    cur = d;
    resetForm();
    flipped = null;
    $("badge").textContent = d.labelled ? "labelled" : "unlabelled";
    $("badge").className = d.labelled ? "done" : "";
    $("counter").textContent = (d.index + 1) + " / " + d.total;
    $("name").textContent = d.take_id + "  -  " + d.candidate;
    var hasPlate = !!d.has_plate;
    plate.hidden = !hasPlate; $("line").hidden = !hasPlate; $("bar").hidden = !hasPlate;
    $("tagL").hidden = !hasPlate; $("tagR").hidden = !hasPlate;
    natW = 0; natH = 0;
    cand.onload = function () {
      natW = cand.naturalWidth; natH = cand.naturalHeight;
      if (hasPlate && plate.naturalWidth) { natW = plate.naturalWidth; natH = plate.naturalHeight; }
      fit();
    };
    plate.onload = function () {
      if (hasPlate && plate.naturalWidth) { natW = plate.naturalWidth; natH = plate.naturalHeight; fit(); }
    };
    if (hasPlate) { plate.src = imgUrl(d.index, "plate", d.plate_v); } else { plate.removeAttribute("src"); }
    cand.src = imgUrl(d.index, "candidate", d.candidate_v);
    applyWipe();
    shownAt = performance.now();
    if (d.all_done) { setMsg("All " + d.total + " pairs are labelled by you.", "ok"); }
    else if (d.remaining !== undefined) { setMsg(d.remaining + " left to label.", ""); }
    fetch("/api/next?peek=1&after=" + d.index).then(function (r) { return r.json(); }).then(function (n) {
      if (n.take_id !== undefined && n.index !== d.index) {
        new Image().src = imgUrl(n.index, "candidate", n.candidate_v);
        if (n.has_plate) { new Image().src = imgUrl(n.index, "plate", n.plate_v); }
      }
    }).catch(function () {});
  }
  function load(after, unlabelled) {
    return fetch("/api/next?after=" + after + (unlabelled ? "&unlabelled=1" : ""))
      .then(function (r) { return r.json(); })
      .then(show)
      .catch(function (e) { setMsg("could not load: " + e, "err"); });
  }

  // --- saving ---------------------------------------------------------------------------
  function post(path, body) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); });
  }
  function commit() {
    if (!cur || busy) { return; }
    if (!verdict) { setMsg("choose a verdict first: 1 ship, 2 fix, 3 render it", "err"); return; }
    var body = { index: cur.index, take_id: cur.take_id, verdict: verdict,
                 reasons: verdict === "ship" ? [] : selected.slice(),
                 seconds: (performance.now() - shownAt) / 1000 };
    busy = true;
    post("/api/label", body).then(function (res) {
      busy = false;
      if (!res.ok || !res.j.ok) { setMsg(res.j.error || "not saved", "err"); return; }
      var n = res.j.count;
      return load(cur.index, true).then(function () {
        if (!cur.all_done) { setMsg("saved (" + n + " labels in file)", "ok"); }
      });
    }).catch(function (e) { busy = false; setMsg("not saved: " + e, "err"); });
  }
  function pick(v) {
    if (!cur) { return; }
    setVerdict(v);
    if (v === "ship") { commit(); }
  }
  function undo() {
    if (busy) { return; }
    busy = true;
    post("/api/undo").then(function (res) {
      busy = false;
      if (!res.ok || !res.j.ok) { setMsg(res.j.error || "nothing to undo", "err"); return; }
      return load(res.j.index - 1, false).then(function () { setMsg("undone (" + res.j.count + " labels in file)", "ok"); });
    }).catch(function (e) { busy = false; setMsg("undo failed: " + e, "err"); });
  }
  function skip() { if (cur) { load(cur.index, true); } }
  function nextOrCommit() { if (verdict) { commit(); } else { skip(); } }

  $("save").addEventListener("click", commit);
  $("next").addEventListener("click", nextOrCommit);
  $("undo").addEventListener("click", undo);

  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    var typing = e.target === codesBox;
    if (e.key === "Enter") { e.preventDefault(); commit(); return; }
    if (e.key === "Escape") { if (verdict) { resetForm(); } else { fit(); } return; }
    if (typing) { return; }
    var k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (k === "1") { e.preventDefault(); pick("ship"); }
    else if (k === "2") { e.preventDefault(); pick("fix"); }
    else if (k === "3") { e.preventDefault(); pick("render_it"); }
    else if (k === "n") { e.preventDefault(); nextOrCommit(); }
    else if (k === "u") { e.preventDefault(); undo(); }
    else if (k === "z") { stage.classList.contains("zoom") ? fit() : zoomAt(0.5, 0.5); }
    else if (k === "t" && cur && cur.has_plate) {
      if (flipped === null) { flipped = wipePct; setWipe(wipePct >= 50 ? 0 : 100); }
      else { setWipe(flipped); flipped = null; }
    }
    else if (k === "[") { setWipe(wipePct - 5); }
    else if (k === "]") { setWipe(wipePct + 5); }
    else if (k === "ArrowRight" && cur) { e.preventDefault(); load(cur.index, false); }
    else if (k === "ArrowLeft" && cur) { e.preventDefault(); load(cur.index - 2, false); }
  });

  setInterval(function () {
    if (cur) { $("timer").textContent = ((performance.now() - shownAt) / 1000).toFixed(1) + " s"; }
  }, 200);

  load(-1, true);
})();
</script>
</body>
</html>
"""
