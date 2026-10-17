(function () {
  'use strict';

  var DATA = JSON.parse(document.getElementById('plate-review-data').textContent);
  var takes = DATA.takes || [];
  var STORE_KEY = 'plate-review:' + DATA.bundle_id;
  var DECISIONS = { approved: 'Looks right', changes: 'Needs changes', unsure: 'Not sure' };
  var MAILTO_LIMIT = 1800;

  function $(id) { return document.getElementById(id); }
  var el = {
    meta: $('meta'), takes: $('takes'), pair: $('pair'), imgA: $('img-a'), imgB: $('img-b'),
    wipe: $('wipe'), gate: $('gate'), mismatch: $('mismatch'), viewport: $('viewport'),
    toggle: $('btn-toggle'), zoom: $('btn-zoom'), stage: document.querySelector('.stage'),
    form: $('comments'), reviewer: $('reviewer'), takeNote: $('take-note'), overall: $('overall'),
    legend: $('take-legend'), tally: $('tally'), mail: $('mail'), download: $('download'), foot: $('foot'),
    modes: Array.prototype.slice.call(document.querySelectorAll('[data-mode]')).filter(function (b) { return b.tagName === 'BUTTON'; })
  };

  var state = { i: 0, mode: 'wipe', wipe: 50, zoom: false };
  var notes = { reviewer: '', overall: '', takes: {} };

  function make(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = text; }
    return n;
  }

  // ---- saved draft (localStorage may be blocked on file://; the page works without it)
  function load() {
    try {
      var raw = window.localStorage.getItem(STORE_KEY);
      if (raw) {
        var s = JSON.parse(raw);
        if (s && typeof s === 'object') {
          notes.reviewer = String(s.reviewer || '');
          notes.overall = String(s.overall || '');
          notes.takes = s.takes && typeof s.takes === 'object' ? s.takes : {};
        }
      }
    } catch (e) { /* no saved draft */ }
  }
  function save() {
    try { window.localStorage.setItem(STORE_KEY, JSON.stringify(notes)); } catch (e) { /* ignore */ }
  }

  function takeNotes(label) {
    if (!notes.takes[label]) { notes.takes[label] = { decision: null, note: '' }; }
    return notes.takes[label];
  }

  // ---- badge
  function metricText(first) {
    if (!first) { return ''; }
    var v = first.value;
    var shown = typeof v === 'number' ? (Math.abs(v) >= 100 ? v.toFixed(0) : v.toPrecision(3)) : 'n/a';
    return first.metric_id + ' = ' + shown;
  }

  function pill(badge) {
    var p = make('span', 'pill ' + badge.status, badge.label || String(badge.status).toUpperCase());
    return p;
  }

  function renderRail() {
    el.takes.textContent = '';
    takes.forEach(function (t, i) {
      var b = make('button', 'take');
      b.type = 'button';
      b.setAttribute('data-index', String(i));
      b.appendChild(make('span', 'name', t.name));
      b.appendChild(pill(t.badge));
      b.addEventListener('click', function () { showTake(i); });
      el.takes.appendChild(b);
    });
  }

  function renderGate(t) {
    var g = el.gate;
    g.textContent = '';
    g.appendChild(make('span', 'label', 'Gate'));
    g.appendChild(pill(t.badge));
    var d = make('span', 'detail');
    var b = t.badge;
    if (b.status === 'none') {
      d.textContent = 'This take has not been scored yet.';
    } else if (b.first_failing) {
      var kind = b.first_failing.status === 'fail' ? 'First failing metric: ' : 'First advisory metric: ';
      d.appendChild(document.createTextNode(kind));
      d.appendChild(make('code', null, metricText(b.first_failing)));
      d.appendChild(document.createTextNode(' (' + b.n_checks + ' checks, ' + b.n_fail + ' failed, ' + b.n_advisory + ' advisory)'));
    } else {
      d.textContent = 'All ' + b.n_checks + ' checks passed.';
    }
    g.appendChild(d);
  }

  // ---- viewer
  function applyMode() {
    el.pair.setAttribute('data-mode', state.mode);
    el.modes.forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-mode') === state.mode ? 'true' : 'false');
    });
  }
  function setMode(m) { state.mode = m; applyMode(); }

  function setWipe(p) {
    state.wipe = Math.max(0, Math.min(100, p));
    el.pair.style.setProperty('--wipe', state.wipe + '%');
    el.wipe.value = String(state.wipe);
  }

  function setZoom(on) {
    state.zoom = on;
    el.stage.classList.toggle('zoom100', on);
    el.zoom.setAttribute('aria-pressed', on ? 'true' : 'false');
    var t = takes[state.i];
    if (t) { el.pair.style.setProperty('--nat-w', t.width + 'px'); }
  }

  function showTake(i) {
    if (!takes.length) { return; }
    state.i = Math.max(0, Math.min(takes.length - 1, i));
    var t = takes[state.i];
    el.imgA.src = t.a;
    el.imgB.src = t.b;
    el.pair.style.aspectRatio = t.a_size[0] + ' / ' + t.a_size[1];
    el.mismatch.hidden = !t.size_mismatch;
    Array.prototype.forEach.call(el.takes.children, function (c, k) {
      if (k === state.i) { c.setAttribute('aria-current', 'true'); } else { c.removeAttribute('aria-current'); }
    });
    renderGate(t);
    setZoom(state.zoom);
    renderForm();
  }

  function wipeFromPointer(ev) {
    var r = el.pair.getBoundingClientRect();
    if (r.width > 0) { setWipe(((ev.clientX - r.left) / r.width) * 100); }
  }
  var dragging = false;
  el.pair.addEventListener('pointerdown', function (ev) {
    if (state.mode !== 'wipe' || state.zoom && ev.pointerType === 'touch') { return; }
    dragging = true;
    try { el.pair.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    wipeFromPointer(ev);
  });
  el.pair.addEventListener('pointermove', function (ev) { if (dragging) { wipeFromPointer(ev); } });
  function endDrag() { dragging = false; }
  el.pair.addEventListener('pointerup', endDrag);
  el.pair.addEventListener('pointercancel', endDrag);

  el.wipe.addEventListener('input', function () {
    if (state.mode !== 'wipe') { setMode('wipe'); }
    setWipe(parseFloat(el.wipe.value));
  });
  el.modes.forEach(function (b) {
    b.addEventListener('click', function () { setMode(b.getAttribute('data-mode')); });
  });
  function toggle() { setMode(state.mode === 'a' ? 'b' : 'a'); }
  el.toggle.addEventListener('click', toggle);
  el.zoom.addEventListener('click', function () { setZoom(!state.zoom); });

  document.addEventListener('keydown', function (ev) {
    var tag = ev.target && ev.target.tagName;
    if (tag === 'TEXTAREA' || (tag === 'INPUT' && ev.target.type === 'text') || ev.ctrlKey || ev.metaKey || ev.altKey) { return; }
    var step = ev.shiftKey ? 10 : 2;
    if (ev.key === 'ArrowLeft' && tag !== 'INPUT') { setMode('wipe'); setWipe(state.wipe - step); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight' && tag !== 'INPUT') { setMode('wipe'); setWipe(state.wipe + step); ev.preventDefault(); }
    else if (ev.key === 't' || ev.key === 'T') { toggle(); }
    else if (ev.key === 'z' || ev.key === 'Z') { setZoom(!state.zoom); }
    else if (ev.key === '[') { showTake(state.i - 1); }
    else if (ev.key === ']') { showTake(state.i + 1); }
  });

  // ---- comments
  function commentsObject() {
    return {
      schema: 1,
      bundle_id: DATA.bundle_id,
      title: DATA.title,
      reviewer: notes.reviewer,
      overall: notes.overall,
      created_local: new Date().toISOString(),
      takes: takes.map(function (t) {
        var n = takeNotes(t.label);
        return { take: t.label, name: t.name, decision: n.decision, note: n.note };
      })
    };
  }

  function summary(full) {
    var lines = [];
    if (notes.reviewer) { lines.push('Reviewer: ' + notes.reviewer); lines.push(''); }
    if (notes.overall) { lines.push(notes.overall); lines.push(''); }
    takes.forEach(function (t) {
      var n = takeNotes(t.label);
      if (!n.decision && !n.note) { return; }
      lines.push(t.name + ' (' + t.label + '): ' + (n.decision ? DECISIONS[n.decision] : 'no decision'));
      if (full && n.note) { lines.push(n.note); }
      lines.push('');
    });
    lines.push('Review ' + DATA.bundle_id);
    return lines.join('\n');
  }

  function mailtoHref(body) {
    var to = DATA.reply_to ? encodeURIComponent(DATA.reply_to).replace(/%40/g, '@') : '';
    var subject = 'Review comments: ' + DATA.title;
    return 'mailto:' + to + '?subject=' + encodeURIComponent(subject) +
      '&body=' + encodeURIComponent(body.replace(/\r?\n/g, '\r\n'));
  }

  function refreshMail() {
    var href = mailtoHref(summary(true));
    if (href.length > MAILTO_LIMIT) {
      href = mailtoHref(summary(false) + '\n\nMy full comments are in the comments.json file attached to this email.');
    }
    el.mail.setAttribute('href', href);
  }

  function refreshTally() {
    var done = takes.filter(function (t) { var n = takeNotes(t.label); return n.decision || n.note; }).length;
    el.tally.textContent = done + ' of ' + takes.length + ' takes with a decision or a note';
  }

  function touched() { save(); refreshTally(); refreshMail(); }

  function renderForm() {
    var t = takes[state.i];
    if (!t) { return; }
    var n = takeNotes(t.label);
    el.legend.textContent = t.name;
    el.takeNote.value = n.note || '';
    Array.prototype.forEach.call(el.form.querySelectorAll('input[name="decision"]'), function (r) {
      r.checked = r.value === n.decision;
    });
    el.reviewer.value = notes.reviewer;
    el.overall.value = notes.overall;
    refreshTally();
    refreshMail();
  }

  el.form.addEventListener('submit', function (ev) { ev.preventDefault(); });
  el.reviewer.addEventListener('input', function () { notes.reviewer = el.reviewer.value; touched(); });
  el.overall.addEventListener('input', function () { notes.overall = el.overall.value; touched(); });
  el.takeNote.addEventListener('input', function () {
    takeNotes(takes[state.i].label).note = el.takeNote.value; touched();
  });
  Array.prototype.forEach.call(el.form.querySelectorAll('input[name="decision"]'), function (r) {
    r.addEventListener('change', function () {
      if (r.checked) { takeNotes(takes[state.i].label).decision = r.value; touched(); }
    });
  });

  el.download.addEventListener('click', function () {
    var blob = new Blob([JSON.stringify(commentsObject(), null, 2) + '\n'], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'comments.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  });

  // ---- start
  load();
  el.meta.textContent = takes.length + (takes.length === 1 ? ' take' : ' takes') + ' · watermark ' + DATA.watermark;
  el.foot.textContent = 'Review proxies, watermarked and not for delivery · ' + DATA.bundle_id;
  renderRail();
  applyMode();
  setWipe(50);
  if (takes.length) { showTake(0); }
  window.__plateReview = { state: state, commentsObject: commentsObject, showTake: showTake };
}());
