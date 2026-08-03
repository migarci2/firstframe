/* FirstFrame — frontend vanilla, sin build, sin CDN.
 *
 * Dos públicos, una app:
 *   · Vista principal → Ana, productora. Escribe un brief, ve el vídeo mientras
 *     se genera, aprueba o pide cambios. Cero jerga, cero identificadores.
 *   · Panel "Detalles técnicos" → la evidencia cruda (B2, procedencia, linaje,
 *     AgentLoop, eventos, chaos). Nada se borra: se esconde.
 *
 * Contrato: server/API.md (CONGELADO).
 */
(function () {
'use strict';

/* ═════════════════════════════ estado ═════════════════════════════ */

var S = {
  jobs: [],
  sel: null,
  detail: null,
  view: 'compose',     // 'compose' | 'spot'
  scenes: 4,
  segs: {},
  feed: [],
  iters: {},
  chaos: {},
  health: null,
  techOpen: false,
  tick: null
};

var FEED_MAX = 140;
var CHAOS_PROVIDERS = ['gmicloud', 'nim', 'openai', 'replicate'];

/* ═════════════════════════════ utilidades ═════════════════════════════ */

function $(id) { return document.getElementById(id); }

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

function jobById(id) {
  for (var i = 0; i < S.jobs.length; i++) if (S.jobs[i].id === id) return S.jobs[i];
  return null;
}
function selJob() { return S.sel ? jobById(S.sel) : null; }
function isLive(j) { return j && (j.status === 'rendering' || j.status === 'queued'); }

function api(path, opts) {
  opts = opts || {};
  return fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    cache: 'no-store'
  }).then(function (r) {
    return r.text().then(function (txt) {
      var data = null;
      try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { _raw: txt }; }
      if (!r.ok) {
        var err = new Error((data && (data.error || data.detail)) || ('HTTP ' + r.status));
        err.status = r.status; err.data = data;
        throw err;
      }
      return data;
    });
  });
}

/* ═════════════════ traducción a lenguaje humano ═════════════════ */

// "6 segundos", "1 minuto y 44 segundos" — se lee en voz alta sin tropezar.
function humanSecs(ms) {
  if (ms == null || isNaN(ms)) return '';
  var s = Math.max(1, Math.round(ms / 1000));
  if (s < 60) return s + (s === 1 ? ' second' : ' seconds');
  var m = Math.floor(s / 60), r = s % 60;
  var out = m + (m === 1 ? ' minute' : ' minutes');
  if (r) out += ' ' + r + (r === 1 ? ' second' : ' seconds');
  return out;
}

function daysLeft(iso) {
  var d = Date.parse(iso);
  if (isNaN(d)) return 30;
  return Math.max(1, Math.round((d - Date.now()) / 86400000));
}

// El identificador del job no pinta nada delante de Ana: se usa el título.
function spotTitle(job) {
  var t = (job.title || job.brief || '').trim();
  if (!t) return 'Untitled spot';
  if (t.length > 70) t = t.slice(0, 68).replace(/[\s,;.]+$/, '') + '…';
  return t.charAt(0).toUpperCase() + t.slice(1);
}

var STATUS = {
  queued:    { label: 'Queued',            tone: 'live'  },
  rendering: { label: 'Generating',        tone: 'live'  },
  in_review: { label: 'Ready for review',  tone: 'ready' },
  approved:  { label: 'Approved',          tone: 'good'  },
  rejected:  { label: 'Reworking',         tone: 'live'  },
  failed:    { label: 'Failed',            tone: 'bad'  }
};
function statusOf(job) {
  if (isRefining(job)) return { label: 'Reworking', tone: 'live' };
  return STATUS[job.status] || { label: job.status, tone: '' };
}

// ¿Está rehaciendo una toma tras un rechazo? (todas las escenas ya están listas)
function isRefining(job) {
  var sc = job.scenes || [];
  return isLive(job) && sc.length > 0 &&
         sc.every(function (s) { return s.status === 'ready'; });
}

/* La frase única del escenario. Un número, no tres: sigue siendo el argumento
 * del producto, pero dicho como se lo dirías a una persona. */
function sayLine(job) {
  var ff = job.first_frame_ms, total = job.total_render_ms;

  if (job.status === 'failed') {
    return { text: 'We could not finish this spot. You can try again with the same brief.', tone: 'bad' };
  }

  if (job.status === 'approved') {
    var d = job.lock ? daysLeft(job.lock.retain_until) : 30;
    return { text: 'Approved and locked: nobody can delete or modify it for ' + d + ' days.', tone: 'good' };
  }

  if (isRefining(job)) {
    return { text: 'We are reworking that part. It will appear here as soon as it is ready.', tone: '' };
  }

  if (isLive(job)) {
    if (ff == null) return { text: 'Generating — playback starts on its own as soon as there are frames.', tone: '' };
    return { text: 'You can already watch the opening: it appeared in ' + humanSecs(ff) + '.', tone: '' };
  }

  // in_review / rejected: aquí es donde se cobra la promesa del producto.
  if (ff != null && total) {
    var ratio = total / ff;
    if (ratio >= 1.8) {
      return { text: 'You could watch it ' + Math.round(ratio) + ' times over before it finished rendering.', tone: '' };
    }
    return { text: 'It was watchable ' + humanSecs(total - ff) + ' before it finished.', tone: '' };
  }
  return { text: 'Ready for review.', tone: '' };
}

/* ═════════════════════════════ toasts ═════════════════════════════ */

function toast(kind, text, sub, ttl) {
  var t = el('div', 'toast ' + (kind || ''));
  t.appendChild(document.createTextNode(text));
  if (sub) t.appendChild(el('small', null, sub));
  var box = $('toasts');
  box.appendChild(t);
  while (box.children.length > 3) box.removeChild(box.firstChild);
  setTimeout(function () {
    t.classList.add('out');
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
  }, ttl || 7000);
  return t;
}

/* ═════════════════════════════ player ═════════════════════════════ */

var Player = {
  jobId: null, engine: '—', hls: null, mse: null, frags: 0, autoplayed: false, video: null,

  init: function () {
    this.video = $('player');
  },

  veil: function (msg) {
    var v = $('veil');
    if (!msg) { v.hidden = true; return; }
    $('veil-text').textContent = msg;
    v.hidden = false;
  },

  setEngine: function (n) { this.engine = n; railChips(); renderTechChips(); },

  teardown: function () {
    if (this.hls) { try { this.hls.destroy(); } catch (e) {} this.hls = null; }
    if (this.mse) { try { this.mse.destroy(); } catch (e) {} this.mse = null; }
    this.frags = 0; this.autoplayed = false;
    try { this.video.removeAttribute('src'); this.video.load(); } catch (e) {}
  },

  load: function (job, opts) {
    opts = opts || {};
    var url = job.stream_url || ('/stream/' + job.id + '/index.m3u8');
    var self = this;

    if (this.jobId === job.id && this.hls && opts.reattach) {
      // Tras un rechazo la playlist ya llevaba ENDLIST y hls.js la trata como VOD:
      // hay que volver a loadSource() para ver la toma refinada (API.md §decision).
      this.frags = 0; this.autoplayed = false;
      this.veil('Reattaching to the new version…');
      this.hls.loadSource(url);
      return;
    }

    this.teardown();
    this.jobId = job.id;
    this.veil(isLive(job) ? 'Preparing the first shot…' : null);

    var forced = (/[?&]player=(\w+)/.exec(location.search) || [])[1];
    if (forced === 'mse') { this.fallbackMse(url); return; }
    if (forced === 'native') {
      this.setEngine('nativo');
      this.video.src = url;
      this.video.play().catch(function () {});
      this.veil(null);
      return;
    }

    // 1) hls.js (vendorizado en web/vendor/, sin CDN). Los segmentos son MPEG-TS.
    if (window.Hls && window.Hls.isSupported()) {
      this.setEngine('hls.js');
      var hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        manifestLoadingMaxRetry: 30,
        manifestLoadingRetryDelay: 700,
        manifestLoadingMaxRetryTimeout: 4000,
        levelLoadingMaxRetry: 30,
        levelLoadingRetryDelay: 700,
        fragLoadingMaxRetry: 12,
        fragLoadingRetryDelay: 500,
        liveSyncDurationCount: 3,
        backBufferLength: 90
      });
      this.hls = hls;
      hls.attachMedia(this.video);
      hls.on(window.Hls.Events.MEDIA_ATTACHED, function () { hls.loadSource(url); });

      hls.on(window.Hls.Events.LEVEL_UPDATED, function (_e, data) {
        var n = (data && data.details && data.details.fragments) ? data.details.fragments.length : 0;
        S.segs[self.jobId] = Math.max(S.segs[self.jobId] || 0, n);
        if (self.jobId === S.sel) { topMeta(); renderTechChips(); }
      });

      hls.on(window.Hls.Events.FRAG_BUFFERED, function () {
        self.frags++;
        // Arrancamos con 2 segmentos de colchón: ni antes (se queda sin buffer)
        // ni al final (perderíamos el argumento del producto).
        if (!self.autoplayed && self.frags >= 2) {
          self.autoplayed = true;
          self.veil(null);
          self.video.play().catch(function () {
            self.veil(null);   // el navegador bloqueó el autoplay: los controles nativos bastan
          });
        }
      });

      hls.on(window.Hls.Events.ERROR, function (_e, data) {
        if (!data.fatal) {
          // bufferStalledError mientras se genera la escena siguiente es NORMAL
          // (API.md): hls.js se recupera solo. No es un error para el usuario.
          return;
        }
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          pushFeed('error', self.jobId, 'hls network: ' + data.details);
          try { hls.startLoad(); } catch (e) {}
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          pushFeed('error', self.jobId, 'hls media: ' + data.details);
          try { hls.recoverMediaError(); } catch (e) {}
        } else {
          pushFeed('error', self.jobId, 'hls fatal: ' + data.details + ' → MSE');
          self.fallbackMse(url);
        }
      });
      return;
    }

    if (window.FFMse && window.FFMse.isSupported()) { this.fallbackMse(url); return; }

    this.setEngine('nativo');
    this.video.src = url;
    this.video.play().catch(function () {});
    this.veil(null);
  },

  fallbackMse: function (url) {
    var self = this;
    if (this.hls) { try { this.hls.destroy(); } catch (e) {} this.hls = null; }
    if (!window.FFMse || !window.FFMse.isSupported()) {
      this.setEngine('nativo');
      this.video.src = url;
      this.veil(null);
      return;
    }
    this.setEngine('mse');
    var p = window.FFMse.create(this.video);
    this.mse = p;

    var toNative = function (why) {
      pushFeed('error', self.jobId, 'mse → nativo: ' + why);
      try { p.destroy(); } catch (e) {}
      self.mse = null;
      self.setEngine('nativo');
      self.video.src = url;
      self.video.play().catch(function () {});
      self.veil(null);
    };

    var onMediaErr = function () {
      if (self.mse !== p) return;
      self.video.removeEventListener('error', onMediaErr);
      toNative((self.video.error && self.video.error.message) || 'media error');
    };
    this.video.addEventListener('error', onMediaErr);

    p.on('ready', function () {
      self.veil(null);
      self.video.play().catch(function () {});
    });
    p.on('error', function (e) { if (e.fatal) toNative(e.reason); });
    p.load(url);
  }
};

/* ═════════════════════════════ vistas ═════════════════════════════ */

function showCompose() {
  S.view = 'compose';
  S.sel = null;
  Player.teardown();
  $('view-compose').hidden = false;
  $('view-spot').hidden = true;
  $('changes').hidden = true;
  renderJobs();
  renderRail();
  renderTech();
  setTimeout(function () { $('brief').focus(); }, 30);
}

function select(id, opts) {
  opts = opts || {};
  var job = jobById(id);
  if (!job) return;
  var changed = S.sel !== id;
  S.sel = id;
  S.view = 'spot';

  $('view-compose').hidden = true;
  $('view-spot').hidden = false;
  if (changed) $('changes').hidden = true;

  if (changed || opts.force) Player.load(job);

  renderJobs();
  renderStage();
  renderRail();
  renderTech();
  loadDetail(id);
}

/* ═════════════════════════ lateral: los spots ═════════════════════════ */

function renderJobs() {
  var list = $('joblist');
  var scroll = list.scrollTop;
  clear(list);

  if (!S.jobs.length) {
    list.appendChild(el('div', 'side-empty', 'You have not created any yet.'));
    return;
  }

  S.jobs.forEach(function (job) {
    var row = el('button', 'jobrow' + (job.id === S.sel ? ' sel' : ''));
    row.type = 'button';
    row.appendChild(el('b', null, spotTitle(job)));
    var st = statusOf(job);
    var s = el('span', 't-' + st.tone);
    s.appendChild(el('i'));
    s.appendChild(document.createTextNode(st.label));
    row.appendChild(s);
    row.addEventListener('click', function () { select(job.id); });
    list.appendChild(row);
  });
  list.scrollTop = scroll;
}

/* ═════════════════════════ escenario ═════════════════════════ */

function renderStage() {
  var job = selJob();
  if (!job || S.view !== 'spot') return;

  $('spot-title').textContent = spotTitle(job);

  var say = sayLine(job);
  var p = $('spot-line');
  p.className = 'say ' + say.tone;
  p.textContent = say.text;

  // Chip sobre el vídeo: sólo mientras algo está pasando.
  var chip = $('chip-live');
  if (isLive(job)) {
    chip.hidden = false;
    $('chip-live-text').textContent = isRefining(job) ? 'Reworking' : 'Generating';
  } else {
    chip.hidden = true;
  }

  // Progreso: una línea de 2 px, sin números ni contadores.
  var scenes = job.scenes || [];
  var total = job.scene_count || scenes.length || 1;
  var ready = scenes.filter(function (s) { return s.status === 'ready'; }).length;
  var bar = $('progress');
  if (isLive(job) && !isRefining(job)) {
    bar.hidden = false;
    $('progress-fill').style.width = Math.round((ready / total) * 100) + '%';
  } else {
    bar.hidden = true;
  }

  topMeta();
  renderActions(job);
  renderNoteScenes(job);
}

// En cada momento, una sola cosa evidente que hacer.
function renderActions(job) {
  var box = $('stage-actions');
  clear(box);

  if (job.status === 'in_review' || job.status === 'rejected') {
    var chg = el('button', 'ghost', 'Request changes');
    chg.type = 'button';
    chg.addEventListener('click', openChanges);
    box.appendChild(chg);

    var ok = el('button', 'primary', 'Approve');
    ok.type = 'button';
    ok.addEventListener('click', function () { ok.disabled = true; decide('approve'); });
    box.appendChild(ok);
    return;
  }

  if (job.status === 'approved') {
    var dl = el('button', 'primary', 'Download');
    dl.type = 'button';
    dl.addEventListener('click', function () { download(job); });
    box.appendChild(dl);
    return;
  }

  if (job.status === 'failed') {
    var retry = el('button', 'primary', 'Try again');
    retry.type = 'button';
    retry.addEventListener('click', function () {
      $('brief').value = job.brief || job.title || '';
      showCompose();
    });
    box.appendChild(retry);
  }
  // Mientras genera no hay nada que decidir: la acción es mirar.
}

function renderNoteScenes(job) {
  var sel = $('note-scene');
  var keep = sel.value;
  clear(sel);
  var o0 = el('option', null, 'the last scene'); o0.value = ''; sel.appendChild(o0);
  (job.scenes || []).forEach(function (s) {
    var o = el('option', null, 'scene ' + s.n);
    o.value = String(s.n);
    sel.appendChild(o);
  });
  sel.value = keep;
}

function openChanges() {
  $('changes').hidden = false;
  $('note').focus();
}

/* ═════════════════════════ acciones ═════════════════════════ */

function createSpot() {
  var brief = $('brief').value.trim();
  if (!brief) {
    brief = 'Un spot de 15 segundos para una zapatilla de running. Luz de amanecer, ciudad vacía.';
    $('brief').value = brief;
  }
  var btn = $('btn-create');
  btn.disabled = true;
  btn.textContent = 'Creating…';

  api('/api/jobs', { method: 'POST', body: {
    brief: brief, title: brief.slice(0, 70), scenes: S.scenes
  }}).then(function (d) {
    var job = d.job || { id: d.id, status: 'queued', title: brief.slice(0, 70) };
    upsertJob(job);
    S.sel = null;
    select(job.id, { force: true });
    $('brief').value = '';
    pushFeed('job_update', job.id, 'job created · pipeline started');
  }).catch(function (e) {
    toast('bad', 'We could not create the spot.', 'Try again in a few seconds.', 9000);
    pushFeed('error', null, 'POST /api/jobs: ' + e.message);
  }).then(function () {
    btn.disabled = false;
    btn.textContent = 'Create spot';
  });
}

function decide(action, note, scene) {
  var job = selJob();
  if (!job) return;
  var body = { action: action, note: note || '' };
  if (action === 'reject' && scene) body.scene = parseInt(scene, 10);

  api('/api/jobs/' + job.id + '/decision', { method: 'POST', body: body })
    .then(function (d) {
      if (d.job) upsertJob(d.job);
      if (action === 'reject') {
        $('changes').hidden = true;
        $('note').value = '';
        toast('', 'Noted.', 'We are reworking that part; it will appear here shortly.', 8000);
        pushIter(job.id, { _live: true, at: Date.now(), scene: body.scene || null,
                           reason: 'Producer rejection: "' + (note || 'sin nota') + '"',
                           action: 'take moved to rejected/ · AgentLoop relaunches the scene' });
        var j = jobById(job.id);
        if (j) Player.load(j, { reattach: true });
      }
      renderStage();
    })
    .catch(function (e) {
      var msg = e.status === 409
        ? 'This spot is still rendering. Wait for it to finish.'
        : 'We could not save your decision. Try again.';
      toast('bad', msg, null, 9000);
      pushFeed('error', job.id, 'decision ' + action + ': ' + e.message);
      renderStage();
    });
}

function download(job) {
  api('/api/download/' + job.id).then(function (d) {
    if (d && d.url) window.open(d.url, '_blank', 'noopener');
    else toast('bad', 'The download is not available right now.', null, 8000);
  }).catch(function (e) {
    toast('bad', 'The download is not available right now.',
          'The video is still stored and locked.', 9000);
    pushFeed('error', job.id, 'download: ' + e.message);
  });
}

/* ═════════════════════════ panel técnico ═════════════════════════ */

function toggleTech(show) {
  if (show === undefined) show = !S.techOpen;
  S.techOpen = show;
  $('tech').hidden = !show;
  $('scrim').hidden = !show;
  $('btn-tech').setAttribute('aria-expanded', show ? 'true' : 'false');
  if (show) renderTech();
}

function renderTech() {
  if (!S.techOpen) return;
  var job = selJob();
  $('tech-sub').textContent = job
    ? 'job ' + job.id + ' · ' + job.status
    : 'sin job seleccionado';
  renderTechChips();
  renderProv();
  renderObjects();
  renderAgentLoop();
  renderTechScenes();
  renderFeed();
  renderChaos();
}

function renderTechChips() {
  if (!S.techOpen) return;
  var box = $('tech-chips');
  clear(box);
  var h = S.health || {};
  var job = selJob();

  function chip(html, cls) {
    var c = el('span', 'tchip' + (cls ? ' ' + cls : ''));
    c.innerHTML = html;
    box.appendChild(c);
  }

  chip('GEN_MODE <b>' + (h.mode || '?') + (h.stub ? ' · stub' : '') + '</b>');
  chip('B2 <b>' + (!h.b2 ? 'off' : (h.b2_capped ? 'capped' : 'ok')) + '</b>',
       h.b2 && !h.b2_capped ? 'ok' : 'warn');
  var tx = h.b2_transactions || {};
  chip('B2 tx <b>' + (tx.total != null ? tx.total : '—') + '</b>');
  chip('events <b>' + (h.events_mode || '?') + '</b>');
  chip('player <b>' + Player.engine + '</b>');
  if (job) chip('segments <b>' + (S.segs[job.id] || 0) + '</b>');
  if (h.degraded) chip('DEGRADED', 'warn');
}

function renderProv() {
  var body = $('g-prov');
  clear(body);
  var job = selJob();
  if (!job) { body.appendChild(el('div', 'tnote', 'Select a spot.')); return; }

  function kv(k, v) {
    var r = el('div', 'kv');
    r.appendChild(el('span', null, k));
    r.appendChild(el('span', null, v));
    body.appendChild(r);
  }
  kv('job_id', job.id);
  kv('created_at', job.created_at_iso || '—');
  kv('scene_count', String(job.scene_count || (job.scenes || []).length));
  kv('first_frame_ms', job.first_frame_ms != null ? String(job.first_frame_ms) : '—');
  kv('total_render_ms', job.total_render_ms != null ? String(job.total_render_ms) : '—');
  kv('stream_url', job.stream_url || '—');
  kv('manifest', job.manifest_url ? 'provenance/' + job.id + '/manifest.json' : '— (aún no)');
  kv('object_lock', job.lock ? job.lock.mode + ' until ' + job.lock.retain_until : '— (not approved)');

  // Linaje: cada rechazo encadena una toma nueva con parent_run_id.
  var det = (S.detail && S.detail.job && S.detail.job.id === job.id) ? S.detail : null;
  var rejects = ((det && det.decisions) || []).filter(function (d) { return d.action === 'reject'; });
  body.appendChild(el('div', 'subhead', 'LINEAGE · parent_run_id'));
  var lin = el('div', 'lin');
  lin.appendChild(el('div', null, '● run ' + job.id + ' · take 1'));
  rejects.forEach(function (d, i) {
    var r = el('div');
    r.appendChild(el('em', null, '│ reject' + (d.scene ? ' scene ' + d.scene : '') +
                                 (d.note ? ' — "' + d.note + '"' : '')));
    lin.appendChild(r);
    lin.appendChild(el('div', null, '└─ refine take ' + (i + 2) + ' · parent_run_id=' + job.id));
  });
  body.appendChild(lin);

  // Manifest + verify
  var vbtn = el('button', 'minibtn', 'genblaze verify');
  vbtn.type = 'button';
  vbtn.disabled = job.status !== 'approved';
  vbtn.addEventListener('click', function () { verify(job, vbtn); });
  body.appendChild(vbtn);

  var box = el('div', 'jsonbox', '');
  if (!job.manifest_url) {
    box.textContent = 'No manifest yet — it is written when the render finishes.';
    body.appendChild(box);
  } else if (job.status !== 'approved') {
    box.textContent = 'The manifest is sealed on approval.\nExpected key: provenance/' +
                      job.id + '/manifest.json';
    var lbtn = el('button', 'minibtn', 'Load manifest anyway');
    lbtn.type = 'button';
    lbtn.addEventListener('click', function () { fetchManifest(job, box); });
    body.appendChild(lbtn);
    body.appendChild(box);
  } else {
    body.appendChild(box);
    fetchManifest(job, box);
  }
}

function fetchManifest(job, box) {
  box.textContent = 'loading manifest…';
  var forJob = job.id;
  fetch(job.manifest_url, { cache: 'no-store' }).then(function (r) {
    return r.text().then(function (t) { return { ok: r.ok, status: r.status, t: t }; });
  }).then(function (res) {
    if (S.sel !== forJob || !box.parentNode) return;
    if (!res.ok) { box.textContent = 'manifest unavailable (HTTP ' + res.status + ')'; return; }
    try { box.textContent = JSON.stringify(JSON.parse(res.t), null, 1); }
    catch (e) { box.textContent = res.t.slice(0, 1400); }
  }).catch(function (e) {
    if (box.parentNode) box.textContent = 'manifest unavailable: ' + e.message;
  });
}

function verify(job, btn) {
  btn.disabled = true;
  var prev = btn.textContent;
  btn.textContent = 'verifying…';
  var box = el('div', 'verify-out', 'running `genblaze verify` on the server…');
  btn.parentNode.insertBefore(box, btn.nextSibling);

  api('/api/verify/' + job.id).then(function (d) {
    box.className = 'verify-out ' + (d.verified ? 'ok' : 'bad');
    box.textContent = (d.verified ? '✔ MANIFEST VERIFIED' : '✖ NOT VERIFIED') +
                      '  (exit ' + d.exit_code + ')\n' + (d.output || '');
  }).catch(function (e) {
    box.className = 'verify-out bad';
    box.textContent = 'verify failed: ' + e.message;
  }).then(function () {
    btn.disabled = false;
    btn.textContent = prev;
  });
}

function renderObjects() {
  var body = $('g-objects');
  clear(body);
  var job = selJob();
  var det = (S.detail && S.detail.job && job && S.detail.job.id === job.id) ? S.detail : null;
  var objs = (det && det.objects) || [];
  $('n-objects').textContent = objs.length;
  if (!objs.length) {
    body.appendChild(el('div', 'tnote', 'No objects listed yet.'));
    return;
  }
  var lin = el('div', 'lin');
  objs.forEach(function (o) {
    lin.appendChild(el('div', null, o.key + (o.size ? '  ' + Math.round(o.size / 1024) + ' KiB' : '')));
  });
  body.appendChild(lin);
}

function agentEntries(id) { return S.iters[id] || (S.iters[id] = []); }

function pushIter(id, entry) {
  agentEntries(id).push(entry);
  if (id === S.sel) renderAgentLoop();
}

function renderAgentLoop() {
  if (!S.techOpen) return;
  var body = $('g-agent');
  clear(body);
  var arr = S.sel ? agentEntries(S.sel) : [];
  $('n-agent').textContent = arr.length;

  if (!arr.length) {
    body.appendChild(el('div', 'tnote',
      'No judge iterations yet. Request changes on a take to see the AgentLoop: ' +
      'the vision judge scores it, refines the prompt and relaunches the scene.'));
    return;
  }

  arr.slice().reverse().forEach(function (it) {
    var n = el('div', 'iter');
    var top = el('div', 'iter-top');
    top.appendChild(el('span', null, fmtClock(it.at)));
    top.appendChild(el('span', null, it.scene ? 'scene ' + it.scene : 'job'));
    if (it.iteration != null) top.appendChild(el('span', null, 'iter ' + it.iteration));
    if (it.score != null) top.appendChild(el('span', 'sc' + (it.score >= 0.6 ? '' : ' low'), it.score.toFixed(2)));
    n.appendChild(top);
    if (it.score != null) {
      var bar = el('div', 'iter-bar');
      var fill = el('i', it.score >= 0.6 ? 'high' : '');
      fill.style.width = Math.max(2, Math.min(100, it.score * 100)) + '%';
      bar.appendChild(fill);
      n.appendChild(bar);
    }
    if (it.reason) n.appendChild(el('div', null, it.reason));
    if (it.action) n.appendChild(el('div', 'act', it.action));
    body.appendChild(n);
  });
}

function renderTechScenes() {
  var body = $('g-scenes');
  clear(body);
  var job = selJob();
  var scenes = (job && job.scenes) || [];
  var ready = scenes.filter(function (s) { return s.status === 'ready'; }).length;
  $('n-scenes').textContent = job ? ready + '/' + (job.scene_count || scenes.length) : '';
  if (!scenes.length) { body.appendChild(el('div', 'tnote', 'No scenes yet.')); return; }
  scenes.forEach(function (s) {
    var r = el('div', 'scenerow ' + s.status);
    r.appendChild(el('span', 'no', String(s.n)));
    r.appendChild(el('span', 'ti', s.title || s.path || '—'));
    r.appendChild(el('span', 'st', s.status + (s.ms != null ? ' · ' + Math.round(s.ms / 1000) + 's' : '')));
    body.appendChild(r);
  });
}

function fmtClock(ts) {
  var d = new Date(ts);
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

function pushFeed(type, jobId, detail) {
  S.feed.push({ at: Date.now(), type: type, job: jobId, detail: detail });
  if (S.feed.length > FEED_MAX) S.feed.shift();
  railFeed();
  renderFeed();
}

function renderFeed() {
  if (!S.techOpen) return;
  var body = $('g-feed');
  clear(body);
  $('n-feed').textContent = S.feed.length;
  if (!S.feed.length) { body.appendChild(el('div', 'tnote', 'Waiting for B2 events…')); return; }
  S.feed.slice(-70).reverse().forEach(function (f) {
    var r = el('div', 'feedrow');
    r.appendChild(el('span', 't', fmtClock(f.at)));
    r.appendChild(el('span', 'k ' + f.type, f.type));
    r.appendChild(el('span', 'd', (f.job && f.job !== S.sel ? f.job + ' ' : '') + (f.detail || '')));
    body.appendChild(r);
  });
}

function renderChaos() {
  if (!S.techOpen) return;
  var body = $('g-chaos');
  clear(body);
  body.appendChild(el('div', 'tnote',
    'Kill a provider live: the next MODEL_ERROR will trigger fallback_models.'));
  CHAOS_PROVIDERS.forEach(function (p) {
    var dead = !!S.chaos[p];
    var row = el('div', 'chaosrow');
    row.appendChild(el('span', 'nm', p));
    row.appendChild(el('span', 'state' + (dead ? ' dead' : ''), dead ? 'down' : 'up'));
    var b = el('button', 'minibtn', dead ? 'Revive' : 'Kill');
    b.type = 'button';
    b.style.marginTop = '0';
    b.addEventListener('click', function () {
      b.disabled = true;
      api('/api/chaos', { method: 'POST', body: { provider: p, dead: !dead } })
        .then(function (d) { S.chaos[d.provider] = !!d.dead; renderChaos(); })
        .catch(function () { b.disabled = false; });
    });
    row.appendChild(b);
    body.appendChild(row);
  });
}


/* ═════════════════════════ raíl de ejecución ═════════════════════════
 * Siempre visible: la sustancia técnica del run sin tener que abrir nada.
 * Es lo que hace que esto se lea como una herramienta y no como una maqueta. */

/* Los datos de la ejecución ya no son pastillas en la cabecera: el estado es el
 * kicker del bloque del spot y el resto una línea de texto mono debajo. */
function topMeta() {
  var box = $('topmeta'), kick = $('spot-kicker');
  if (!box) return;
  clear(box);
  var job = selJob();

  if (kick) {
    var st = job ? statusOf(job) : null;
    kick.textContent = st ? st.label : '';
    kick.className = 'stagekicker' + (st && st.tone ? ' ' + st.tone : '');
  }
  if (!job) return;

  function t(txt, cls) { box.appendChild(el('span', 'tm' + (cls ? ' ' + cls : ''), txt)); }
  t(job.id);
  if (job.first_frame_ms != null) t('first frame ' + (job.first_frame_ms / 1000).toFixed(1) + 's');
  if (job.total_render_ms != null) t('full render ' + (job.total_render_ms / 1000).toFixed(1) + 's');
  if (S.segs[job.id]) t(S.segs[job.id] + ' segments');
}

function railChips() {
  var box = $('r-chips');
  if (!box) return;
  clear(box);
  var h = S.health || {};
  function chip(k, v, cls) {
    var c = el('span', 'tchip' + (cls ? ' ' + cls : ''));
    c.appendChild(document.createTextNode(k + ' '));
    c.appendChild(el('b', null, String(v)));
    box.appendChild(c);
  }
  chip('gen', h.mode || '?');
  chip('b2', !h.b2 ? 'off' : (h.b2_capped ? 'capped' : 'ok'), h.b2 && !h.b2_capped ? 'ok' : 'warn');
  chip('tx', (h.b2_transactions && h.b2_transactions.total != null) ? h.b2_transactions.total : '—');
  chip('events', h.events_mode || '?');
  chip('player', Player.engine || '—');
  if (h.degraded) chip('estado', 'degradado', 'warn');
}

function railScenes() {
  var body = $('r-scenes');
  if (!body) return;
  clear(body);
  var job = selJob();
  var sc = (job && job.scenes) || [];
  var ready = sc.filter(function (x) { return x.status === 'ready'; }).length;
  $('r-n-scenes').textContent = job ? ready + '/' + (job.scene_count || sc.length) : '';
  if (!sc.length) { body.appendChild(el('div', 'tnote', 'No scenes yet.')); return; }
  sc.forEach(function (x) {
    var r = el('div', 'scenerow ' + x.status);
    r.appendChild(el('span', 'no', String(x.n)));
    r.appendChild(el('span', 'ti', x.title || '—'));
    r.appendChild(el('span', 'st', x.status + (x.ms != null ? ' ' + Math.round(x.ms / 1000) + 's' : '')));
    body.appendChild(r);
  });
}

function railProv() {
  var body = $('r-prov'), box = $('r-json');
  if (!body) return;
  clear(body);
  var job = selJob();
  if (!job) { box.textContent = '—'; return; }
  function kv(k, v) {
    var r = el('div', 'kv');
    r.appendChild(el('span', null, k));
    r.appendChild(el('span', null, v));
    body.appendChild(r);
  }
  kv('job_id', job.id);
  kv('bucket', (S.health && S.health.bucket) || 'genblaze-review');
  kv('manifest', job.manifest_url ? 'provenance/' + job.id + '/manifest.json' : '—');
  kv('lock', job.lock ? job.lock.mode : '—');

  // El manifest sellado si existe; si no, el estado real del job como JSON vivo.
  if (job.status === 'approved' && job.manifest_url) {
    if (box.getAttribute('data-for') !== job.id) {
      box.setAttribute('data-for', job.id);
      fetchManifest(job, box);
    }
  } else {
    box.removeAttribute('data-for');
    box.textContent = JSON.stringify({
      id: job.id, status: job.status,
      scene_count: job.scene_count,
      first_frame_ms: job.first_frame_ms,
      total_render_ms: job.total_render_ms,
      scenes: (job.scenes || []).map(function (x) { return { n: x.n, status: x.status, ms: x.ms }; }),
      stream_url: job.stream_url,
      lock: job.lock || null
    }, null, 1);
  }
}

function railFeed() {
  var body = $('r-feed');
  if (!body) return;
  clear(body);
  $('r-n-feed').textContent = S.feed.length;
  if (!S.feed.length) { body.appendChild(el('div', 'tnote', 'Waiting for B2 events…')); return; }
  S.feed.slice(-40).reverse().forEach(function (f) {
    var r = el('div', 'feedrow');
    r.appendChild(el('span', 't', fmtClock(f.at)));
    r.appendChild(el('span', 'k ' + f.type, f.type));
    r.appendChild(el('span', 'd', (f.job && f.job !== S.sel ? f.job + ' ' : '') + (f.detail || '')));
    body.appendChild(r);
  });
}

function renderRail() { topMeta(); railChips(); railScenes(); railProv(); railFeed(); }

/* ═════════════════════════ datos + SSE ═════════════════════════ */

function upsertJob(job) {
  if (!job || !job.id) return;
  var found = false;
  for (var i = 0; i < S.jobs.length; i++) {
    if (S.jobs[i].id === job.id) { S.jobs[i] = job; found = true; break; }
  }
  if (!found) S.jobs.unshift(job);
  renderJobs();
  if (job.id === S.sel) { renderStage(); renderRail(); renderTech(); }
}

function loadDetail(id) {
  api('/api/jobs/' + id).then(function (d) {
    if (S.sel !== id) return;
    S.detail = d;
    var arr = [];
    (d.provider_events || []).forEach(function (ev) {
      if (ev.kind === 'judge_score') {
        arr.push({ at: ev.at, scene: ev.scene, score: ev.score, reason: ev.detail || null,
                   iteration: ev.iteration });
      } else if (ev.kind === 'retry') {
        arr.push({ at: ev.at, scene: ev.scene, reason: ev.detail || 'retry',
                   action: 'scene relaunched' });
      } else if (ev.kind === 'provider_failover') {
        arr.push({ at: ev.at, scene: ev.scene,
                   reason: (ev.model ? ev.model + ' · ' : '') +
                           (ev.detail || 'MODEL_ERROR on the primary provider'),
                   action: 'fallback_models → ' + (ev.fallback_model || 'fallback model') });
      }
    });
    var live = (S.iters[id] || []).filter(function (x) { return x._live; });
    S.iters[id] = arr.concat(live);
    renderRail();
    renderTech();
  }).catch(function (e) {
    if (S.sel !== id) return;
    pushFeed('error', id, 'GET /api/jobs/' + id + ': ' + e.message);
  });
}

var esRetry = 0;

function connectSSE() {
  var es;
  try { es = new EventSource('/api/events'); }
  catch (e) { return; }

  var TYPES = ['hello', 'job_update', 'render_started', 'segment_landed', 'scene_ready',
               'render_complete', 'provider_failover', 'judge_score', 'approved',
               'rejected', 'chaos', 'ping'];

  es.onopen = function () { esRetry = 0; };
  es.onerror = function () {
    try { es.close(); } catch (e) {}
    esRetry = Math.min(esRetry + 1, 6);
    setTimeout(connectSSE, 500 * esRetry);
  };
  TYPES.forEach(function (t) {
    es.addEventListener(t, function (msg) {
      var d = {};
      try { d = JSON.parse(msg.data || '{}'); } catch (e) { return; }
      handleEvent(t, d);
    });
  });
}

function handleEvent(type, d) {
  var jid = d.job_id || (d.job && d.job.id) || null;

  switch (type) {
    case 'ping':
      return;

    case 'hello':
      S.jobs = d.jobs || [];
      renderJobs();
      return;

    case 'job_update':
    case 'scene_ready':
    case 'render_complete':
      if (d.job) upsertJob(d.job);
      if (type === 'scene_ready') {
        pushFeed('scene_ready', jid, 'scene ' + d.scene + ' ready' + (d.ms ? ' · ' + d.ms + ' ms' : ''));
      } else if (type === 'render_complete') {
        pushFeed('render_complete', jid, 'total_render_ms ' + d.total_render_ms);
        if (jid === S.sel) toast('', 'Your spot is ready for review.', null, 7000);
      } else {
        pushFeed('job_update', jid, (d.job && d.job.status) || '');
      }
      return;

    case 'render_started':
      pushFeed('render_started', jid, 'first segment on B2 · scene ' + (d.scene != null ? d.scene : '—'));
      if (d.job) upsertJob(d.job);
      return;

    case 'segment_landed':
      S.segs[jid] = (S.segs[jid] || 0) + 1;
      if (jid === S.sel) { topMeta(); renderTechChips(); }
      pushFeed('segment_landed', jid, 'b2:ObjectCreated · ' + (d.key || ('seq ' + d.seq)));
      return;

    case 'provider_failover':
      // Ana no necesita saber qué modelo era: sólo que no ha perdido el trabajo.
      toast('warn', 'A provider failed; we continued on another one without losing the work.', null, 9000);
      pushFeed('provider_failover', jid,
               (d.model || '?') + ' → ' + (d.fallback_model || '?') + (d.scene ? ' · scene ' + d.scene : ''));
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene,
                               reason: (d.model || '?') + ' MODEL_ERROR en ' + (d.provider || '?'),
                               action: 'fallback → ' + (d.fallback_model || '?') });
      return;

    case 'judge_score':
      pushFeed('judge_score', jid, 'scene ' + d.scene + ' · score ' + (d.score != null ? d.score.toFixed(2) : '—'));
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene, score: d.score,
                               iteration: d.iteration,
                               reason: d.detail || d.reason || 'vision judge (llama-3.2-90b-vision)',
                               action: (d.score != null && d.score < 0.6) ? 'prompt refined · scene relaunched' : null });
      return;

    case 'approved':
      if (d.job) upsertJob(d.job);
      pushFeed('approved', jid, 'Object Lock GOVERNANCE · ' + (d.key || ''));
      if (jid === S.sel) {
        var days = d.lock ? daysLeft(d.lock.retain_until) : 30;
        toast('good', 'Approved and locked.', 'Nobody can delete or modify it for ' + days + ' days.', 9000);
      }
      return;

    case 'rejected':
      if (d.job) upsertJob(d.job);
      pushFeed('rejected', jid, 'note: ' + (d.note || '') + (d.scene ? ' · scene ' + d.scene : ''));
      if (jid === S.sel) {
        var j = jobById(jid);
        if (j) Player.load(j, { reattach: true });
        renderStage();
      }
      return;

    case 'chaos':
      S.chaos[d.provider] = !!d.dead;
      pushFeed('chaos', jid, d.provider + (d.dead ? ' DOWN' : ' back up'));
      toast(d.dead ? 'warn' : 'good',
            d.dead ? 'Provider taken down on purpose.' : 'Provider back up.',
            d.dead ? 'The next attempt will switch to another provider on its own.' : null, 7000);
      renderChaos();
      return;
  }
}

function pickDefaultJob() {
  var live = S.jobs.filter(isLive);
  if (live.length) return live[0];
  var rev = S.jobs.filter(function (j) { return j.status === 'in_review'; });
  if (rev.length) return rev[0];
  return S.jobs[0];
}

/* ═════════════════════════ arranque ═════════════════════════ */

function loadHealth() {
  api('/api/health').then(function (h) {
    S.health = h;
    railChips();
    renderTechChips();
    if (h.degraded && !loadHealth._warned) {
      loadHealth._warned = true;
      // Traducido: nada de cuotas ni transacciones.
      toast('warn', 'Running at reduced capacity.', 'You can keep creating, watching and approving as normal.', 12000);
      pushFeed('error', null, 'health: ' + (h.warning || 'degraded'));
    }
  }).catch(function (e) {
    toast('bad', 'Cannot reach the server.', 'Still retrying — nothing you already have is lost.', 12000);
    pushFeed('error', null, 'GET /api/health: ' + e.message);
  });
}

function bootstrapJobs() {
  api('/api/jobs').then(function (d) {
    S.jobs = d.jobs || [];
    renderJobs();
    // Si ya hay trabajo hecho, se enseña; si no, la pantalla pide un brief.
    if (S.jobs.length) select(pickDefaultJob().id);
    else showCompose();
  }).catch(function () {
    renderJobs();
    showCompose();
  });
}

function bindKeys() {
  document.addEventListener('keydown', function (e) {
    var t = e.target || {};
    var typing = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT';

    if (e.key === 'Escape') {
      if (S.techOpen) { toggleTech(false); return; }
      if (!$('changes').hidden) { $('changes').hidden = true; return; }
      return;
    }

    // Enviar el brief con ⌘/Ctrl+Enter desde el propio textarea.
    if (typing && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      if (t.id === 'brief') { e.preventDefault(); createSpot(); }
      if (t.id === 'note') { e.preventDefault(); sendChanges(); }
      return;
    }
    if (typing) return;

    // El chaos sigue existiendo: ahora vive en la vista técnica.
    if (e.key === 'k' || e.key === 'K') {
      e.preventDefault();
      toggleTech(true);
      $('grp-chaos').open = true;
      $('grp-chaos').scrollIntoView({ block: 'nearest' });
    }
    if (e.key === ' ' && S.view === 'spot') {
      e.preventDefault();
      var v = Player.video;
      if (v.paused) v.play().catch(function () {}); else v.pause();
    }
  });
}

function sendChanges() {
  var note = $('note').value.trim();
  decide('reject', note, $('note-scene').value);
}

function start() {
  Player.init();

  $('btn-create').addEventListener('click', createSpot);
  $('btn-new').addEventListener('click', showCompose);
  // El raíl arranca cerrado: lo primero que se ve es el plano, a ancho completo.
  // La evidencia sigue a un clic, y la profunda vive en el panel técnico.
  $('btn-rail').addEventListener('click', function () {
    var hid = document.body.classList.toggle('no-rail');
    this.setAttribute('aria-expanded', hid ? 'false' : 'true');
  });
  $('btn-tech').addEventListener('click', function () { toggleTech(); });
  $('btn-tech-close').addEventListener('click', function () { toggleTech(false); });
  $('scrim').addEventListener('click', function () { toggleTech(false); });
  $('btn-changes-cancel').addEventListener('click', function () { $('changes').hidden = true; });
  $('btn-changes-send').addEventListener('click', sendChanges);

  $('len-picker').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-scenes]');
    if (!b) return;
    S.scenes = parseInt(b.getAttribute('data-scenes'), 10) || 4;
    Array.prototype.forEach.call(this.querySelectorAll('button'), function (x) {
      x.classList.toggle('on', x === b);
    });
  });

  bindKeys();
  renderJobs();
  loadHealth();
  bootstrapJobs();
  connectSSE();

  // La frase del escenario cambia mientras el job está vivo.
  S.tick = setInterval(function () {
    var j = selJob();
    if (j && isLive(j) && S.view === 'spot') renderStage();
  }, 1000);

  window.addEventListener('error', function (e) {
    pushFeed('error', null, (e.message || 'error JS') + ' @ ' +
             (e.filename || '').split('/').pop() + ':' + e.lineno);
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();

})();
