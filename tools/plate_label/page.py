"""The single self-contained HTML page served at GET / (inline CSS and JS, no CDN, no network beyond
the loopback API). New code for PLATE (no seed script). The verdict buttons and the reason-code
checkboxes are generated from codes.py so the UI cannot drift from the fixed table.
"""

from __future__ import annotations

from html import escape

from plate_label.codes import REASON_CODES, REASON_GROUPS, VERDICT_KEYS

VERDICT_LABELS = {"ship": "Ship", "fix": "Fix", "render_it": "Render it"}


def _verdict_buttons() -> str:
    return "\n".join(
        f'<button type="button" class="verdict" data-key="{key}" data-verdict="{verdict}">'
        f"<kbd>{key}</kbd> {escape(VERDICT_LABELS[verdict])}</button>"
        for key, verdict in VERDICT_KEYS.items()
    )


def _reason_groups() -> str:
    parts = []
    for prefix, title in REASON_GROUPS.items():
        rows = [
            f'<label class="reason"><input type="checkbox" value="{code}"> <b>{code}</b> {escape(desc)}</label>'
            for code, desc in REASON_CODES.items()
            if code.startswith(prefix)
        ]
        if rows:
            parts.append(f"<fieldset><legend>{escape(title)}</legend>{''.join(rows)}</fieldset>")
    return "\n".join(parts)


def render_page(nonce: str) -> str:
    """The labelling page; `nonce` goes on the inline style and script for the CSP header."""
    return (
        _TEMPLATE.replace("__NONCE__", nonce)
        .replace("__VERDICTS__", _verdict_buttons())
        .replace("__REASONS__", _reason_groups())
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
  html, body { height:100%; margin:0; background:var(--bg); color:var(--fg);
               font:14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #app { display:grid; grid-template-columns: 1fr 340px; grid-template-rows: auto 1fr auto; height:100%; }
  header { grid-column:1 / 3; display:flex; gap:16px; align-items:baseline; padding:8px 14px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  #counter { font-variant-numeric: tabular-nums; color:var(--acc); }
  #name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #badge { font-size:12px; padding:1px 8px; border-radius:10px; border:1px solid var(--line); color:var(--dim); }
  #badge.done { color:var(--ship); border-color:var(--ship); }
  #stage { position:relative; overflow:hidden; display:flex; align-items:center; justify-content:center;
           background:#08080a; cursor:zoom-in; min-height:0; }
  #stage img { max-width:100%; max-height:100%; object-fit:contain; display:block; }
  #stage.zoom { overflow:auto; display:block; cursor:zoom-out; }
  #stage.zoom img { max-width:none; max-height:none; object-fit:none; }
  aside { overflow:auto; padding:12px 14px; border-left:1px solid var(--line); background:var(--panel); }
  .verdicts { display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; margin-bottom:12px; }
  button { font:inherit; color:var(--fg); background:#20222a; border:1px solid var(--line); border-radius:6px;
           padding:8px 6px; cursor:pointer; }
  button:hover { border-color:var(--acc); }
  button.verdict.on[data-verdict="ship"] { background:var(--ship); color:#06130d; border-color:var(--ship); }
  button.verdict.on[data-verdict="fix"] { background:var(--fix); color:#171102; border-color:var(--fix); }
  button.verdict.on[data-verdict="render_it"] { background:var(--render); color:#1a0606; border-color:var(--render); }
  kbd { font:12px ui-monospace, Menlo, Consolas, monospace; opacity:.8; margin-right:4px; }
  fieldset { border:1px solid var(--line); border-radius:6px; margin:0 0 8px; padding:4px 8px 6px; }
  legend { color:var(--dim); font-size:12px; padding:0 4px; letter-spacing:.04em; text-transform:uppercase; }
  label.reason { display:block; padding:1px 0; cursor:pointer; }
  label.reason b { font:12px ui-monospace, Menlo, Consolas, monospace; color:var(--acc); }
  #note { width:100%; margin:6px 0 8px; padding:7px 8px; color:var(--fg); background:#0d0e12;
          border:1px solid var(--line); border-radius:6px; font:inherit; }
  #submit { width:100%; background:var(--acc); color:#0c0a24; border-color:var(--acc); font-weight:600; }
  #msg { min-height:20px; margin-top:8px; color:var(--dim); }
  #msg.err { color:var(--render); }
  #msg.ok { color:var(--ship); }
  footer { grid-column:1 / 3; padding:5px 14px; border-top:1px solid var(--line); background:var(--panel);
           color:var(--dim); font-size:12px; }
  @media (max-width: 800px) { #app { grid-template-columns: 1fr; } header, footer { grid-column:1; } }
</style>
</head>
<body>
<div id="app">
  <header>
    <span id="counter">- / -</span>
    <span id="name">loading...</span>
    <span id="badge">unlabelled</span>
  </header>
  <div id="stage" title="click: toggle 100% crop at that point"><img id="img" alt=""></div>
  <aside>
    <div class="verdicts">
__VERDICTS__
    </div>
__REASONS__
    <input id="note" type="text" maxlength="1000" placeholder="note (required for O99)" autocomplete="off">
    <button id="submit" type="button">Save (Enter)</button>
    <div id="msg" role="status"></div>
  </aside>
  <footer>S ship &middot; F fix &middot; R render it &middot; Enter save &middot; &larr; &rarr; navigate &middot; click image or Z: 100% crop &middot; Esc: fit</footer>
</div>
<script nonce="__NONCE__">
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var stage = $("stage"), img = $("img"), msg = $("msg");
  var cur = null, verdict = null, busy = false;

  function setMsg(text, cls) { msg.textContent = text || ""; msg.className = cls || ""; }

  function urlFor(rel) { return "/img/" + rel.split("/").map(encodeURIComponent).join("/"); }

  function setVerdict(v) {
    verdict = v;
    document.querySelectorAll("button.verdict").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-verdict") === v);
    });
  }

  function resetForm() {
    setVerdict(null);
    document.querySelectorAll("label.reason input").forEach(function (c) { c.checked = false; });
    $("note").value = "";
  }

  function fit() { stage.classList.remove("zoom"); img.style.width = ""; img.style.height = ""; }

  function zoomAt(fx, fy) {
    if (!img.naturalWidth) { return; }
    var dpr = window.devicePixelRatio || 1;
    stage.classList.add("zoom");
    img.style.width = (img.naturalWidth / dpr) + "px";
    img.style.height = (img.naturalHeight / dpr) + "px";
    stage.scrollLeft = fx * img.naturalWidth / dpr - stage.clientWidth / 2;
    stage.scrollTop = fy * img.naturalHeight / dpr - stage.clientHeight / 2;
  }

  stage.addEventListener("click", function (e) {
    if (stage.classList.contains("zoom")) { fit(); return; }
    var r = img.getBoundingClientRect();
    if (!r.width || !r.height) { return; }
    var fx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    var fy = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
    zoomAt(fx, fy);
  });

  function show(d) {
    cur = d;
    resetForm();
    fit();
    if (!d.image) {
      $("counter").textContent = "0 / 0";
      $("name").textContent = "no images in this folder";
      $("badge").textContent = "-";
      img.removeAttribute("src");
      return;
    }
    $("counter").textContent = (d.index + 1) + " / " + d.total;
    $("name").textContent = d.image;
    var b = $("badge");
    b.textContent = d.labelled ? "labelled" : "unlabelled";
    b.className = d.labelled ? "done" : "";
    img.src = urlFor(d.image);
    if (d.all_done) { setMsg("All " + d.total + " images are labelled.", "ok"); }
    else if (d.remaining !== undefined) { setMsg(d.remaining + " left to label.", ""); }
    fetch("/api/next?after=" + d.index).then(function (r) { return r.json(); }).then(function (n) {
      if (n.image && n.index !== d.index) { new Image().src = urlFor(n.image); }
    }).catch(function () {});
  }

  function load(after, unlabelled) {
    return fetch("/api/next?after=" + after + (unlabelled ? "&unlabelled=1" : ""))
      .then(function (r) { return r.json(); })
      .then(show)
      .catch(function (e) { setMsg("could not load: " + e, "err"); });
  }

  function submit() {
    if (!cur || !cur.image || busy) { return; }
    if (!verdict) { setMsg("choose a verdict first: S, F or R", "err"); return; }
    var reasons = Array.prototype.map.call(
      document.querySelectorAll("label.reason input:checked"), function (c) { return c.value; });
    busy = true;
    fetch("/api/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: cur.image, verdict: verdict, reasons: reasons, note: $("note").value })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        busy = false;
        if (!res.ok || !res.j.ok) { setMsg(res.j.error || "not saved", "err"); return; }
        var n = res.j.count;
        return load(cur.index, true).then(function () {
          if (!cur.all_done) { setMsg("saved (" + n + " labels in file)", "ok"); }
        });
      })
      .catch(function (e) { busy = false; setMsg("not saved: " + e, "err"); });
  }

  document.querySelectorAll("button.verdict").forEach(function (b) {
    b.addEventListener("click", function () { setVerdict(b.getAttribute("data-verdict")); });
  });
  $("submit").addEventListener("click", submit);

  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    var tag = e.target.tagName;
    var typing = (tag === "INPUT" && e.target.type !== "checkbox") || tag === "TEXTAREA";
    if (e.key === "Enter") { e.preventDefault(); submit(); return; }
    if (typing) { return; }
    var k = e.key.length === 1 ? e.key.toUpperCase() : e.key;
    var btn = document.querySelector('button.verdict[data-key="' + k + '"]');
    if (btn) { e.preventDefault(); setVerdict(btn.getAttribute("data-verdict")); return; }
    if (k === "ArrowRight" && cur) { e.preventDefault(); load(cur.index, false); }
    else if (k === "ArrowLeft" && cur) { e.preventDefault(); load(cur.index - 2, false); }
    else if (k === "Z") { stage.classList.contains("zoom") ? fit() : zoomAt(0.5, 0.5); }
    else if (k === "Escape") { fit(); }
  });

  load(-1, true);
})();
</script>
</body>
</html>
"""
