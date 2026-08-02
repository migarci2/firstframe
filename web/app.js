/* FirstFrame — sala de revisión. Frontend vanilla, sin build, sin CDN.
 *
 * Contrato: server/API.md (CONGELADO). Todo el estado llega por EventSource
 * sobre /api/events; las llamadas REST son sólo para el detalle y las acciones.
 */
(function () {
'use strict';

/* ═════════════════════════════════ estado ═════════════════════════════════ */

var S = {
  jobs: [],            // Job[] ordenados, más reciente primero
  sel: null,           // job_id seleccionado
  detail: null,        // { job, provider_events, decisions, objects }
  segs: {},            // job_id -> nº de segment_landed vistos
  feed: [],            // últimos eventos (todos los jobs)
  iters: {},           // job_id -> entradas del AgentLoop
  chaos: {},           // provider -> dead:bool
  health: null,
  hideFailed: false,   // el revisor no quiere ver renders muertos
  nowTimer: null
};

var FEED_MAX = 120;
var CHAOS_PROVIDERS = ['gmicloud', 'nim', 'openai', 'replicate'];

/* ═════════════════════════════════ utilidades ═════════════════════════════ */

function $(id) { return document.getElementById(id); }

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

// "7.1 s" / "2:43" — números pensados para leerse en cámara.
function fmtDur(ms) {
  if (ms == null || isNaN(ms)) return '—';
  var s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + ' s';
  var m = Math.floor(s / 60);
  var r = Math.round(s % 60);
  if (r === 60) { m++; r = 0; }
  return m + ':' + (r < 10 ? '0' : '') + r;
}

function fmtClock(ts) {
  var d = new Date(ts);
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

function fmtTC(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function segLabel(n) { return n + (n === 1 ? ' segmento en B2' : ' segmentos en B2'); }

function jobById(id) {
  for (var i = 0; i < S.jobs.length; i++) if (S.jobs[i].id === id) return S.jobs[i];
  return null;
}

function selJob() { return S.sel ? jobById(S.sel) : null; }

function isLive(job) { return job && (job.status === 'rendering' || job.status === 'queued'); }

// Escena que se está generando ahora mismo (1-indexada).
function currentScene(job) {
  var sc = job.scenes || [];
  for (var i = 0; i < sc.length; i++) if (sc[i].status === 'rendering') return sc[i].n;
  for (var j = 0; j < sc.length; j++) if (sc[j].status === 'pending') return sc[j].n;
  return sc.length ? sc[sc.length - 1].n : 1;
}

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

/* ═════════════════════════════════ toasts ═════════════════════════════════ */

function toast(kind, head, bodyNode, ttl) {
  var t = el('div', 'toast ' + (kind || ''));
  t.appendChild(el('div', 'toast-h', head));
  var b = el('div', 'toast-b');
  if (typeof bodyNode === 'string') b.textContent = bodyNode; else b.appendChild(bodyNode);
  t.appendChild(b);
  var box = $('toasts');
  box.appendChild(t);
  // Como mucho 3 en pantalla: un muro de toasts tapa el vídeo.
  while (box.children.length > 3) box.removeChild(box.firstChild);
  setTimeout(function () {
    t.classList.add('out');
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 280);
  }, ttl || 8000);
  return t;
}

// "pixverse-v5.6 MODEL_ERROR → fallback: seedance-2-0"
function failoverToast(d) {
  var b = el('span');
  b.appendChild(el('span', 'from', (d.model || 'modelo') + ' MODEL_ERROR'));
  b.appendChild(el('span', 'arrow', '→'));
  b.appendChild(el('span', 'to', 'fallback: ' + (d.fallback_model || '—')));
  var head = 'Failover de proveedor' + (d.provider ? ' · ' + d.provider : '') +
             (d.scene ? ' · escena ' + d.scene : '');
  toast('failover', head, b, 11000);
}

/* ═════════════════════════════════ player ═════════════════════════════════ */

var Player = {
  jobId: null,
  engine: null,
  hls: null,
  mse: null,
  frags: 0,
  autoplayed: false,

  video: null,

  init: function () {
    this.video = $('player');
    var v = this.video;
    v.addEventListener('timeupdate', updateTimecode);
    v.addEventListener('durationchange', updateTimecode);
    v.addEventListener('play',  function () { $('btn-play').textContent = 'Pause'; });
    v.addEventListener('pause', function () { $('btn-play').textContent = 'Play'; });
    $('btn-play').addEventListener('click', function () {
      if (v.paused) { v.play().catch(function () {}); } else { v.pause(); }
    });
  },

  note: function (msg, isErr) {
    var n = $('player-note');
    if (!msg) { n.hidden = true; return; }
    n.textContent = msg;
    n.className = 'player-note' + (isErr ? ' err' : '');
    n.hidden = false;
  },

  setEngine: function (name) {
    this.engine = name;
    $('engine-badge').textContent = name;
  },

  teardown: function () {
    if (this.hls) { try { this.hls.destroy(); } catch (e) {} this.hls = null; }
    if (this.mse) { try { this.mse.destroy(); } catch (e) {} this.mse = null; }
    this.frags = 0;
    this.autoplayed = false;
    try { this.video.removeAttribute('src'); this.video.load(); } catch (e) {}
  },

  /** Arranca (o reengancha) el stream de un job. */
  load: function (job, opts) {
    opts = opts || {};
    var url = job.stream_url || ('/stream/' + job.id + '/index.m3u8');
    var self = this;

    if (this.jobId === job.id && this.hls && opts.reattach) {
      // Reject: la playlist ya llevaba ENDLIST, hls.js la trata como VOD.
      // Hay que volver a loadSource() para ver la toma refinada (API.md §decision).
      this.note('Reenganchando al stream: la escena refinada se añade a la misma playlist…');
      this.frags = 0; this.autoplayed = false;
      this.hls.loadSource(url);
      return;
    }

    this.teardown();
    this.jobId = job.id;
    this.note(isLive(job)
      ? 'Esperando el primer segmento en B2… el player arranca con 2 segmentos.'
      : null);

    // ?player=mse|native fuerza el motor (para probar el plan B sin tocar código).
    var forced = (/[?&]player=(\w+)/.exec(location.search) || [])[1];
    if (forced === 'mse') { this.fallbackMse(url); return; }
    if (forced === 'native') {
      this.setEngine('nativo');
      this.video.src = url;
      this.video.play().catch(function () {});
      return;
    }

    // 1) hls.js (vendorizado en web/vendor/, sin CDN).
    if (window.Hls && window.Hls.isSupported()) {
      this.setEngine('hls.js');
      var hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        // La playlist puede tardar un par de segundos en existir tras el POST.
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

      // La playlist crece mientras el backend sube segmentos: el contador
      // refleja lo que hay realmente publicado, no sólo lo que vio el SSE.
      hls.on(window.Hls.Events.LEVEL_UPDATED, function (_e, data) {
        var n = data && data.details && data.details.fragments ? data.details.fragments.length : 0;
        if (self.jobId === S.sel) {
          S.segs[self.jobId] = Math.max(S.segs[self.jobId] || 0, n);
          $('seg-counter').textContent = segLabel(S.segs[self.jobId]);
        }
      });

      hls.on(window.Hls.Events.FRAG_BUFFERED, function () {
        self.frags++;
        // Arrancamos con 2 segmentos de colchón (§9.1 del plan): ni antes
        // (se queda sin buffer) ni al final (perderíamos el argumento).
        if (!self.autoplayed && self.frags >= 2) {
          self.autoplayed = true;
          self.note(null);
          self.video.play().catch(function () {
            self.note('Autoplay bloqueado por el navegador — pulsa Play.');
          });
        }
      });

      hls.on(window.Hls.Events.ERROR, function (_e, data) {
        if (!data.fatal) {
          // bufferStalledError / bufferNudgeOnStall mientras se genera la
          // siguiente escena son NORMALES (API.md): hls.js se recupera solo.
          if (data.details === 'bufferStalledError' || data.details === 'bufferNudgeOnStall') {
            if (isLive(selJob())) self.note('Buffer al día: esperando la siguiente escena…');
            return;
          }
          if (data.details === 'fragLoadError' || data.details === 'levelEmptyError') return;
          return;
        }
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          self.note('Error de red en el stream (' + data.details + '). Reintentando…', true);
          try { hls.startLoad(); } catch (e) {}
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          self.note('Error de media (' + data.details + '). Recuperando…', true);
          try { hls.recoverMediaError(); } catch (e) {}
        } else {
          self.note('hls.js no pudo reproducir (' + data.details + '); pasando a MSE.', true);
          self.fallbackMse(url);
        }
      });
      return;
    }

    // 2) MSE propio (web/mse.js).
    if (window.FFMse && window.FFMse.isSupported()) { this.fallbackMse(url); return; }

    // 3) HLS nativo (Safari / iOS).
    this.setEngine('nativo');
    this.video.src = url;
    this.video.play().catch(function () {});
  },

  fallbackMse: function (url) {
    var self = this;
    if (this.hls) { try { this.hls.destroy(); } catch (e) {} this.hls = null; }
    if (!window.FFMse || !window.FFMse.isSupported()) {
      this.setEngine('nativo');
      this.video.src = url;
      return;
    }
    this.setEngine('mse');
    var p = window.FFMse.create(this.video);
    this.mse = p;

    // Chrome declara soportar 'video/mp2t' pero su demuxer aborta en la frontera
    // de escena ("Parsed buffers not in DTS sequence"): por eso hls.js remuxa a
    // fMP4 y es el motor primario. Aquí lo detectamos por el error del <video>
    // (no llega como evento nuestro) y degradamos con un mensaje visible.
    var onMediaErr = function () {
      if (self.mse !== p) return;
      var why = (self.video.error && self.video.error.message) || 'error de media';
      self.video.removeEventListener('error', onMediaErr);
      self.note('MSE no puede con estos segmentos MPEG-TS (' + why.split(':')[0] +
                '); usando el player nativo.', true);
      try { p.destroy(); } catch (err) {}
      self.mse = null;
      self.setEngine('nativo');
      self.video.src = url;
      self.video.play().catch(function () {});
    };
    this.video.addEventListener('error', onMediaErr);
    p.on('ready', function () {
      self.note(null);
      self.video.play().catch(function () {
        self.note('Autoplay bloqueado por el navegador — pulsa Play.');
      });
    });
    p.on('error', function (e) {
      if (!e.fatal) return;
      // Chrome no acepta MPEG-TS por MSE (por eso hls.js remuxa). Degradamos al
      // player nativo y lo decimos, en vez de quedarnos en negro.
      self.note('MSE no puede con estos segmentos (' + e.reason + '); usando el player nativo.', true);
      try { p.destroy(); } catch (err) {}
      self.mse = null;
      self.setEngine('nativo');
      self.video.src = url;
      self.video.play().catch(function () {});
    });
    p.load(url);
  }
};

function updateTimecode() {
  var v = Player.video;
  var d = isFinite(v.duration) ? v.duration : 0;
  $('timecode').textContent = fmtTC(v.currentTime) + ' / ' + fmtTC(d);
}

/* ═════════════════════════════ render: lista de jobs ══════════════════════ */

function visibleJobs() {
  if (!S.hideFailed) return S.jobs;
  return S.jobs.filter(function (j) { return j.status !== 'failed' || j.id === S.sel; });
}

function renderJobs() {
  var list = $('joblist');
  var scroll = list.scrollTop;   // un job_update cada segundo no debe mover la lista
  clear(list);
  var shown = visibleJobs();
  $('jobs-count').textContent = shown.length === S.jobs.length
    ? String(S.jobs.length)
    : shown.length + '/' + S.jobs.length;

  if (!shown.length) {
    list.appendChild(el('div', 'empty', S.jobs.length
      ? 'Todos los jobs están en estado "failed". Pulsa "todos" para verlos.'
      : 'No hay jobs todavía. Pega un brief arriba y pulsa "New spot".'));
    list.scrollTop = scroll;
    return;
  }

  shown.forEach(function (job) {
    var row = el('button', 'jobrow' + (job.id === S.sel ? ' sel' : ''));
    row.type = 'button';
    row.setAttribute('data-job', job.id);

    var top = el('div', 'jobrow-top');
    top.appendChild(el('span', 'jobrow-id', job.id));
    top.appendChild(el('span', 'pill ' + job.status, job.status.replace('_', ' ')));
    row.appendChild(top);

    row.appendChild(el('div', 'jobrow-title', job.title || job.brief || '(sin título)'));

    var meta = el('div', 'jobrow-meta');
    meta.appendChild(el('span', 'ff', job.first_frame_ms != null ? 'ff ' + fmtDur(job.first_frame_ms) : 'ff —'));
    meta.appendChild(el('span', null, job.total_render_ms != null ? fmtDur(job.total_render_ms) : '·'));
    var bars = el('div', 'minibars');
    (job.scenes || []).forEach(function (s) { bars.appendChild(el('i', 'minibar ' + s.status)); });
    meta.appendChild(bars);
    row.appendChild(meta);

    row.addEventListener('click', function () { select(job.id); });
    list.appendChild(row);
  });
  list.scrollTop = scroll;
}

/* ═════════════════════════════ render: cronómetros ════════════════════════ */

function renderTimers() {
  var job = selJob();
  var box = $('timers');
  if (!job) {
    box.classList.add('pending');
    $('t-first').textContent = '—';
    $('t-total').textContent = '—';
    $('gain-x').textContent = '—';
    $('tb-first').style.width = '0%';
    return;
  }

  var ff = job.first_frame_ms;
  // Mientras renderiza no hay total: enseñamos el cronómetro corriendo, que es
  // justo el contraste que queremos en cámara (7 s fijos vs total subiendo).
  var live = isLive(job);
  var total = job.total_render_ms;
  if (total == null && live && job.created_at) total = Date.now() - job.created_at;

  box.classList.toggle('pending', ff == null);
  $('t-first').textContent = fmtDur(ff);
  $('t-total').textContent = fmtDur(total);
  $('tb-legend-first').textContent = ff != null ? 'primer fotograma · ' + fmtDur(ff) : 'primer fotograma';

  if (ff != null && total) {
    var ratio = total / ff;
    $('gain-x').textContent = (ratio >= 10 ? Math.round(ratio) : ratio.toFixed(1)) + '×';
    $('tb-first').style.width = Math.max(1.5, Math.min(100, (ff / total) * 100)).toFixed(1) + '%';
  } else {
    $('gain-x').textContent = '—';
    $('tb-first').style.width = '0%';
  }
  $('t-gain').style.visibility = (ff != null && total) ? 'visible' : 'hidden';
}

/* ═════════════════════════════ render: escenas + LIVE ═════════════════════ */

function renderScenes() {
  var job = selJob();
  var strip = $('scenestrip');
  clear(strip);
  if (!job) { $('scenes-progress').textContent = '—'; return; }

  var scenes = job.scenes || [];
  var ready = scenes.filter(function (s) { return s.status === 'ready'; }).length;
  $('scenes-progress').textContent = ready + '/' + (job.scene_count || scenes.length);

  scenes.forEach(function (s) {
    var extra = scenes.length > (job.scene_count || scenes.length) && s.n > job.scene_count ? ' refined' : '';
    var c = el('div', 'scene ' + s.status + extra);
    var n = el('div', 'scene-n');
    n.appendChild(el('span', null, 'ESCENA ' + s.n));
    n.appendChild(el('span', 'st', s.status));
    c.appendChild(n);
    c.appendChild(el('div', 'scene-title', s.title || '—'));
    c.appendChild(el('div', 'scene-ms', s.ms != null ? fmtDur(s.ms) : (s.status === 'rendering' ? 'generando…' : '—')));
    strip.appendChild(c);
  });

  // Badge LIVE
  var badge = $('live-badge');
  if (isLive(job)) {
    badge.hidden = false;
    var pend = scenes.filter(function (s) { return s.status !== 'ready'; }).length;
    // Tras un rechazo el job vuelve a "rendering" con todas las escenas ya
    // listas: lo que se está generando es la toma refinada, no una escena nueva.
    $('live-text').textContent = pend === 0
      ? 'LIVE — refinando la toma rechazada'
      : 'LIVE — generando escena ' + currentScene(job) + ' de ' + (job.scene_count || scenes.length);
  } else {
    badge.hidden = true;
  }
}

/* ═════════════════════════════ render: revisión ═══════════════════════════ */

function renderReview() {
  var job = selJob();
  var approve = $('btn-approve'), reject = $('btn-reject');
  var hint = $('review-hint'), lock = $('lockbadge');
  var sel = $('reject-scene');

  clear(sel);
  var opt0 = el('option', null, 'última'); opt0.value = ''; sel.appendChild(opt0);
  if (job) (job.scenes || []).forEach(function (s) {
    var o = el('option', null, 'escena ' + s.n); o.value = String(s.n); sel.appendChild(o);
  });

  if (!job) {
    approve.disabled = reject.disabled = true;
    lock.hidden = true;
    hint.hidden = false;
    hint.textContent = 'Selecciona un job en revisión para aprobarlo o rechazarlo.';
    return;
  }

  var reviewable = job.status === 'in_review' || job.status === 'rejected';
  approve.disabled = !reviewable;
  reject.disabled = !reviewable;

  if (job.lock) {
    lock.hidden = false;
    $('lock-until').textContent = 'retención hasta ' + job.lock.retain_until +
                                  ' · approved/' + job.id + '/final.mp4';
    hint.hidden = true;
  } else {
    lock.hidden = true;
    hint.hidden = false;
    hint.className = 'review-hint' + (job.status === 'failed' ? ' err' : '');
    hint.textContent = reviewable
      ? 'Reject relanza la escena con el AgentLoop; Approve sube el final con Object Lock.'
      : (isLive(job) ? 'Render en curso — puedes ver ya el segundo 0:00 mientras se genera.'
                     : 'Job en estado "' + job.status + '"' + (job.error ? ': ' + job.error : '') + '.');
    hint.title = hint.textContent;
  }
}

/* ═════════════════════════════ render: AgentLoop ══════════════════════════ */

function agentEntries(jobId) { return S.iters[jobId] || (S.iters[jobId] = []); }

function pushIter(jobId, entry) {
  var arr = agentEntries(jobId);
  arr.push(entry);
  if (jobId === S.sel) renderAgentLoop();
}

function renderAgentLoop() {
  var body = $('agentloop-body');
  clear(body);
  var arr = S.sel ? agentEntries(S.sel) : [];
  $('agentloop-count').textContent = arr.length;

  if (!arr.length) {
    body.appendChild(el('div', 'empty',
      'Sin iteraciones del juez todavía.\nRechaza una toma para ver el AgentLoop: el juez de visión puntúa, refina el prompt y relanza la escena.'));
    return;
  }

  arr.slice().reverse().forEach(function (it) {
    var n = el('div', 'iter');
    var top = el('div', 'iter-top');
    top.appendChild(el('span', null, fmtClock(it.at)));
    top.appendChild(el('span', null, it.scene ? 'escena ' + it.scene : 'job'));
    if (it.iteration != null) top.appendChild(el('span', null, 'iter ' + it.iteration));
    if (it.score != null) {
      var sc = el('span', 'iter-score ' + (it.score >= 0.6 ? 'high' : 'low'), it.score.toFixed(2));
      top.appendChild(sc);
    }
    n.appendChild(top);

    if (it.score != null) {
      var bar = el('div', 'iter-bar');
      var fill = el('i', it.score >= 0.6 ? 'high' : '');
      fill.style.width = Math.max(2, Math.min(100, it.score * 100)) + '%';
      bar.appendChild(fill);
      n.appendChild(bar);
    }
    if (it.reason) n.appendChild(el('div', 'iter-reason', it.reason));
    if (it.action) n.appendChild(el('div', 'iter-act', it.action));
    body.appendChild(n);
  });
}

/* ═════════════════════════════ render: provenance ═════════════════════════ */

function renderProvenance() {
  var body = $('provenance-body');
  clear(body);
  var job = selJob();
  $('btn-verify').disabled = !job || job.status !== 'approved';

  if (!job) { body.appendChild(el('div', 'empty', 'Sin job seleccionado.')); return; }

  function row(k, v) {
    var r = el('div', 'prov-row');
    r.appendChild(el('span', 'k', k));
    r.appendChild(el('span', 'v', v));
    body.appendChild(r);
  }

  row('job_id', job.id);
  row('created', job.created_at_iso || '—');
  row('escenas', String(job.scene_count || (job.scenes || []).length));
  row('manifest', job.manifest_url ? 'provenance/' + job.id + '/manifest.json' : '— (aún no)');
  row('lock', job.lock ? job.lock.mode + ' hasta ' + job.lock.retain_until : '— (sin aprobar)');

  // Linaje: cada rechazo encadena una toma nueva con parent_run_id.
  var decisions = (S.detail && S.detail.job && S.detail.job.id === job.id) ? (S.detail.decisions || []) : [];
  var rejects = decisions.filter(function (d) { return d.action === 'reject'; });
  var lin = el('div', 'lineage');
  lin.appendChild(el('div', 'iter-top', 'LINAJE · parent_run_id'));
  var node = el('div', 'lin-node');
  node.appendChild(el('span', 'rail', '●'));
  node.appendChild(el('span', 'tag', 'run ' + job.id + ' · take 1'));
  lin.appendChild(node);
  rejects.forEach(function (d, i) {
    var arrow = el('div', 'lin-node');
    arrow.appendChild(el('span', 'rail', '│'));
    arrow.appendChild(el('span', 'tag', 'reject' + (d.scene ? ' escena ' + d.scene : '') +
                                        (d.note ? ' — "' + d.note + '"' : '')));
    lin.appendChild(arrow);
    var t = el('div', 'lin-node take2');
    t.appendChild(el('span', 'rail', '└─'));
    t.appendChild(el('span', 'tag', 'refine take ' + (i + 2) + ' · parent_run_id=' + job.id));
    lin.appendChild(t);
  });
  body.appendChild(lin);

  // Objetos que hay realmente en el bucket
  var objs = (S.detail && S.detail.job && S.detail.job.id === job.id) ? (S.detail.objects || []) : [];
  if (objs.length) {
    var ob = el('div', 'lineage');
    ob.appendChild(el('div', 'iter-top', 'OBJETOS EN B2 (' + objs.length + ')'));
    objs.slice(0, 8).forEach(function (o) {
      var n = el('div', 'lin-node');
      n.appendChild(el('span', 'rail', '·'));
      n.appendChild(el('span', 'tag', o.key + (o.size ? '  ' + Math.round(o.size / 1024) + ' KiB' : '')));
      ob.appendChild(n);
    });
    body.appendChild(ob);
  }

  // Manifest
  var box = el('div', 'jsonbox', 'cargando manifest…');
  body.appendChild(box);
  if (!job.manifest_url) {
    box.textContent = 'Sin manifest todavía — se escribe cuando termina el render.';
  } else if (job.status !== 'approved') {
    // El manifest se sella al aprobar; pedirlo antes sólo genera 404 en consola.
    box.textContent = 'El manifest se sella al aprobar el job.\nClave prevista: provenance/' +
                      job.id + '/manifest.json';
    var btn = el('button', 'btn btn-mini', 'Cargar manifest igualmente');
    btn.type = 'button';
    btn.style.margin = '0 9px 8px';
    btn.addEventListener('click', function () { fetchManifest(job, box); });
    body.appendChild(btn);
  } else {
    fetchManifest(job, box);
  }
}

function fetchManifest(job, box) {
  box.textContent = 'cargando manifest…';
  var forJob = job.id;
  fetch(job.manifest_url, { cache: 'no-store' }).then(function (r) {
    return r.text().then(function (t) { return { ok: r.ok, status: r.status, t: t }; });
  }).then(function (res) {
    if (S.sel !== forJob || !box.parentNode) return;
    if (!res.ok) {
      box.textContent = 'manifest no disponible (HTTP ' + res.status + ')\n' + res.t.slice(0, 200);
      return;
    }
    try { box.textContent = JSON.stringify(JSON.parse(res.t), null, 1); }
    catch (e) { box.textContent = res.t.slice(0, 1200); }
  }).catch(function (e) {
    if (box.parentNode) box.textContent = 'manifest no disponible: ' + e.message;
  });
}

/* ═════════════════════════════ render: feed ═══════════════════════════════ */

function pushFeed(type, jobId, detail) {
  S.feed.push({ at: Date.now(), type: type, job: jobId, detail: detail });
  if (S.feed.length > FEED_MAX) S.feed.shift();
  renderFeed();
}

function renderFeed() {
  var body = $('feed-body');
  clear(body);
  $('feed-count').textContent = S.feed.length;
  if (!S.feed.length) { body.appendChild(el('div', 'empty', 'Esperando eventos de B2…')); return; }
  S.feed.slice(-60).reverse().forEach(function (f) {
    var r = el('div', 'feedrow');
    r.appendChild(el('span', 't', fmtClock(f.at)));
    r.appendChild(el('span', 'k ' + f.type, f.type));
    r.appendChild(el('span', 'd', (f.job && f.job !== S.sel ? f.job + ' ' : '') + (f.detail || '')));
    body.appendChild(r);
  });
}

/* ═════════════════════════════ selección de job ═══════════════════════════ */

function select(id, opts) {
  opts = opts || {};
  var job = jobById(id);
  if (!job) return;
  var changed = S.sel !== id;
  S.sel = id;

  $('stage-empty').hidden = true;
  $('stage-title').hidden = false;
  $('stage-jobid').textContent = job.id;
  $('stage-jobname').textContent = job.title || job.brief || '';
  $('seg-counter').textContent = segLabel(S.segs[id] || 0);

  if (changed || opts.force) Player.load(job);

  renderJobs(); renderTimers(); renderScenes(); renderReview();
  renderAgentLoop(); renderProvenance();
  loadDetail(id);
}

function loadDetail(id) {
  api('/api/jobs/' + id).then(function (d) {
    if (S.sel !== id) return;
    S.detail = d;
    // Sembramos el AgentLoop con lo que ya está en la base de datos.
    var arr = [];
    (d.provider_events || []).forEach(function (ev) {
      if (ev.kind === 'judge_score') {
        arr.push({ at: ev.at, scene: ev.scene, score: ev.score, reason: ev.detail || null,
                   action: null, iteration: ev.iteration });
      } else if (ev.kind === 'retry') {
        arr.push({ at: ev.at, scene: ev.scene, reason: ev.detail || 'reintento',
                   action: 'escena relanzada' });
      } else if (ev.kind === 'provider_failover') {
        arr.push({ at: ev.at, scene: ev.scene,
                   reason: (ev.model ? ev.model + ' · ' : '') +
                           (ev.detail || 'MODEL_ERROR en el proveedor primario'),
                   action: 'fallback_models → ' + (ev.fallback_model || 'modelo de respaldo') });
      }
    });
    // Conservamos lo llegado por SSE que aún no esté en la BD.
    var live = (S.iters[id] || []).filter(function (x) { return x._live; });
    S.iters[id] = arr.concat(live);
    renderAgentLoop(); renderProvenance();
  }).catch(function (e) {
    if (S.sel !== id) return;
    pushFeed('error', id, 'GET /api/jobs/' + id + ': ' + e.message);
  });
}

/* ═════════════════════════════ upsert + SSE ═══════════════════════════════ */

function upsertJob(job) {
  if (!job || !job.id) return;
  var found = false;
  for (var i = 0; i < S.jobs.length; i++) {
    if (S.jobs[i].id === job.id) { S.jobs[i] = job; found = true; break; }
  }
  if (!found) S.jobs.unshift(job);
  renderJobs();
  if (job.id === S.sel) { renderTimers(); renderScenes(); renderReview(); }
}

function sseStatus(on, label) {
  var n = $('sys-sse');
  n.className = 'sys sys-stream ' + (on ? 'on' : 'off');
  n.querySelector('.sys-sse-label').textContent = label;
}

var esRetry = 0;

function connectSSE() {
  var es;
  try { es = new EventSource('/api/events'); }
  catch (e) { sseStatus(false, 'sin stream'); return; }

  var TYPES = ['hello', 'job_update', 'render_started', 'segment_landed', 'scene_ready',
               'render_complete', 'provider_failover', 'judge_score', 'approved',
               'rejected', 'chaos', 'ping'];

  es.onopen = function () { esRetry = 0; sseStatus(true, 'en vivo'); };

  es.onerror = function () {
    sseStatus(false, 'reconectando');
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
      sseStatus(true, 'en vivo');
      if (!S.sel && S.jobs.length) select(pickDefaultJob().id);
      return;

    case 'job_update':
    case 'scene_ready':
    case 'render_complete':
      if (d.job) upsertJob(d.job);
      if (type === 'scene_ready') {
        pushFeed('scene_ready', jid, 'escena ' + d.scene + ' lista' + (d.ms ? ' · ' + fmtDur(d.ms) : ''));
      } else if (type === 'render_complete') {
        pushFeed('render_complete', jid, 'render total ' + fmtDur(d.total_render_ms));
      } else {
        pushFeed('job_update', jid, (d.job && d.job.status) || '');
      }
      return;

    case 'render_started':
      pushFeed('render_started', jid, 'primer segmento en B2 · escena ' + (d.scene != null ? d.scene : '—'));
      if (d.job) upsertJob(d.job);
      return;

    case 'segment_landed':
      S.segs[jid] = (S.segs[jid] || 0) + 1;
      if (jid === S.sel) $('seg-counter').textContent = segLabel(S.segs[jid]);
      pushFeed('segment_landed', jid, 'b2:ObjectCreated · ' + (d.key || ('seq ' + d.seq)));
      return;

    case 'provider_failover':
      failoverToast(d);
      pushFeed('provider_failover', jid,
               (d.model || '?') + ' → ' + (d.fallback_model || '?') + (d.scene ? ' · escena ' + d.scene : ''));
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene,
                               reason: (d.model || '?') + ' MODEL_ERROR en ' + (d.provider || '?'),
                               action: 'fallback → ' + (d.fallback_model || '?') });
      return;

    case 'judge_score':
      pushFeed('judge_score', jid, 'escena ' + d.scene + ' · score ' + (d.score != null ? d.score.toFixed(2) : '—'));
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene, score: d.score,
                               iteration: d.iteration,
                               reason: d.detail || d.reason || 'juez de visión (llama-3.2-90b-vision)',
                               action: (d.score != null && d.score < 0.6) ? 'prompt refinado · escena relanzada' : null });
      return;

    case 'approved':
      if (d.job) upsertJob(d.job);
      pushFeed('approved', jid, 'Object Lock GOVERNANCE · ' + (d.key || ''));
      toast('good', 'Aprobado · Object Lock GOVERNANCE',
            (d.key || ('approved/' + jid + '/final.mp4')) +
            (d.lock ? '  retención hasta ' + d.lock.retain_until : ''), 10000);
      if (jid === S.sel) { renderReview(); renderProvenance(); }
      return;

    case 'rejected':
      if (d.job) upsertJob(d.job);
      pushFeed('rejected', jid, 'nota: ' + (d.note || '') + (d.scene ? ' · escena ' + d.scene : ''));
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene,
                               reason: 'Rechazo de la productora: "' + (d.note || '') + '"',
                               action: 'toma a rejected/ · AgentLoop relanza la escena' });
      // La playlist ya tenía ENDLIST: hay que reenganchar para ver el refinado.
      if (jid === S.sel) {
        var j = jobById(jid);
        if (j) Player.load(j, { reattach: true });
        renderReview();
      }
      return;

    case 'chaos':
      S.chaos[d.provider] = !!d.dead;
      pushFeed('chaos', jid, d.provider + (d.dead ? ' MUERTO' : ' revivido'));
      toast(d.dead ? 'bad' : 'good', 'Chaos injection',
            d.provider + (d.dead ? ' está muerto — el siguiente MODEL_ERROR disparará fallback_models'
                                 : ' revivido'), 7000);
      renderChaos();
      return;
  }
}

// Job por defecto al abrir: el que más luce (uno vivo; si no, uno en revisión).
function pickDefaultJob() {
  var live = S.jobs.filter(isLive);
  if (live.length) return live[0];
  var rev = S.jobs.filter(function (j) { return j.status === 'in_review'; });
  if (rev.length) return rev[0];
  var app = S.jobs.filter(function (j) { return j.status === 'approved'; });
  if (app.length) return app[0];
  return S.jobs[0];
}

/* ═════════════════════════════ acciones ═══════════════════════════════════ */

function newSpot(ev) {
  ev.preventDefault();
  var brief = $('brief-input').value.trim();
  if (!brief) {
    brief = 'spot de 15s para una zapatilla de running, luz de amanecer, ciudad vacía';
    $('brief-input').value = brief;
  }
  var btn = $('btn-newspot');
  btn.disabled = true; btn.textContent = 'Lanzando…';

  api('/api/jobs', { method: 'POST', body: {
    brief: brief,
    title: brief.slice(0, 60),
    scenes: parseInt($('scene-count').value, 10) || 4
  }}).then(function (d) {
    var job = d.job || { id: d.id };
    upsertJob(job);
    S.sel = null;                       // fuerza la carga del player
    select(job.id, { force: true });
    pushFeed('job_update', job.id, 'job creado · pipeline arrancado');
    toast('good', 'Job creado', job.id + ' — enganchando al stream incremental…', 6000);
  }).catch(function (e) {
    toast('bad', 'No se pudo crear el job', e.message, 10000);
    pushFeed('error', null, 'POST /api/jobs: ' + e.message);
  }).then(function () {
    btn.disabled = false; btn.textContent = 'New spot';
  });
}

function decide(action) {
  var job = selJob();
  if (!job) return;
  var note = $('note-input').value.trim();
  var sceneSel = $('reject-scene').value;
  var body = { action: action, note: note };
  if (action === 'reject' && sceneSel) body.scene = parseInt(sceneSel, 10);

  $('btn-approve').disabled = $('btn-reject').disabled = true;

  api('/api/jobs/' + job.id + '/decision', { method: 'POST', body: body })
    .then(function (d) {
      if (d.job) upsertJob(d.job);
      $('note-input').value = '';
      if (action === 'approve') { renderReview(); renderProvenance(); }
      else {
        pushIter(job.id, { _live: true, at: Date.now(),
                           scene: body.scene || null,
                           reason: 'Rechazo de la productora: "' + (note || 'sin nota') + '"',
                           action: 'AgentLoop: juez de visión → prompt refinado → escena relanzada' });
        var j = jobById(job.id);
        if (j) Player.load(j, { reattach: true });
      }
    })
    .catch(function (e) {
      toast('bad', 'Decisión rechazada por el backend',
            e.message + (e.data && e.data.status ? ' (status: ' + e.data.status + ')' : ''), 9000);
      renderReview();
    })
    .then(function () { renderReview(); });
}

function verify() {
  var job = selJob();
  if (!job) return;
  var btn = $('btn-verify');
  btn.disabled = true; btn.textContent = 'Verificando…';

  var box = el('div', 'verify-out', 'ejecutando `genblaze verify` en el servidor…');
  $('provenance-body').insertBefore(box, $('provenance-body').firstChild);

  api('/api/verify/' + job.id).then(function (d) {
    box.className = 'verify-out ' + (d.verified ? 'ok' : 'bad');
    box.textContent = (d.verified ? '✔ MANIFEST VERIFICADO' : '✖ NO VERIFICADO') +
                      '  (exit ' + d.exit_code + ')\n' + (d.output || '');
    toast(d.verified ? 'good' : 'bad', 'genblaze verify',
          d.verified ? 'Manifest embebido verificado en ' + job.id : 'La verificación falló', 8000);
  }).catch(function (e) {
    box.className = 'verify-out bad';
    box.textContent = 'verify falló: ' + e.message;
  }).then(function () {
    btn.disabled = false; btn.textContent = 'Verify';
  });
}

/* ═════════════════════════════ chaos ══════════════════════════════════════ */

function renderChaos() {
  var body = $('chaos-list');
  clear(body);
  CHAOS_PROVIDERS.forEach(function (p) {
    var dead = !!S.chaos[p];
    var row = el('div', 'chaos-item');
    row.appendChild(el('span', 'nm', p));
    row.appendChild(el('span', 'state ' + (dead ? 'dead' : 'alive'), dead ? 'muerto' : 'vivo'));
    var b = el('button', 'btn btn-mini', dead ? 'Revivir' : 'Matar');
    b.type = 'button';
    b.addEventListener('click', function () {
      b.disabled = true;
      api('/api/chaos', { method: 'POST', body: { provider: p, dead: !dead } })
        .then(function (d) { S.chaos[d.provider] = !!d.dead; renderChaos(); })
        .catch(function (e) { toast('bad', 'Chaos falló', e.message, 7000); b.disabled = false; });
    });
    row.appendChild(b);
    body.appendChild(row);
  });
}

function toggleChaos(show) {
  var m = $('chaos-modal');
  if (show === undefined) show = m.hidden;
  m.hidden = !show;
  if (show) renderChaos();
}

/* ═════════════════════════════ arranque ═══════════════════════════════════ */

function loadHealth() {
  api('/api/health').then(function (h) {
    S.health = h;
    $('sys-mode').innerHTML = 'mode <b>' + (h.mode || '—') + (h.stub ? ' · stub' : '') + '</b>';
    var b2 = !h.b2 ? 'off' : (h.b2_capped ? 'capped' : 'ok');
    $('sys-b2').innerHTML = 'B2 <b>' + b2 + '</b>';
    $('sys-b2').style.color = (b2 === 'ok') ? '' : 'var(--warn)';
    // Estado degradado explícito: si el backend está tocado, se ve por qué.
    if (h.degraded && h.warning && !loadHealth._warned) {
      loadHealth._warned = true;
      toast('failover', 'Backend degradado', h.warning, 14000);
      pushFeed('error', null, 'health: ' + h.warning);
    }
  }).catch(function (e) {
    $('sys-mode').innerHTML = 'mode <b>?</b>';
    $('sys-b2').innerHTML = 'B2 <b>?</b>';
    toast('bad', 'Backend no responde', 'GET /api/health: ' + e.message +
          '. La UI seguirá reintentando el stream de eventos.', 12000);
  });
}

function bootstrapJobs() {
  // El `hello` del SSE ya trae los jobs, pero pedimos la lista igual para que
  // la sala nunca aparezca vacía si el EventSource tarda o falla.
  api('/api/jobs').then(function (d) {
    if (S.jobs.length) return;
    S.jobs = d.jobs || [];
    renderJobs();
    if (!S.sel && S.jobs.length) select(pickDefaultJob().id);
  }).catch(function (e) {
    var list = $('joblist');
    clear(list);
    list.appendChild(el('div', 'empty', 'No se pudo cargar /api/jobs: ' + e.message));
  });
}

function bindKeys() {
  document.addEventListener('keydown', function (e) {
    var t = e.target || {};
    var typing = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT';
    if (e.key === 'Escape') { toggleChaos(false); return; }
    if (typing) return;
    if (e.key === 'k' || e.key === 'K') { e.preventDefault(); toggleChaos(); }
    if (e.key === ' ') {
      e.preventDefault();
      var v = Player.video;
      if (v.paused) v.play().catch(function () {}); else v.pause();
    }
    if (e.key === '?') {
      toast('', 'Atajos', 'k · chaos injection   ␣ · play/pausa   Esc · cerrar', 6000);
    }
  });
}

function start() {
  Player.init();
  $('newspot-form').addEventListener('submit', newSpot);
  $('btn-approve').addEventListener('click', function () { decide('approve'); });
  $('btn-reject').addEventListener('click', function () { decide('reject'); });
  $('btn-verify').addEventListener('click', verify);
  $('btn-filter').addEventListener('click', function () {
    S.hideFailed = !S.hideFailed;
    this.textContent = S.hideFailed ? 'sin fallidos' : 'todos';
    renderJobs();
  });
  $('chaos-close').addEventListener('click', function () { toggleChaos(false); });
  $('chaos-modal').addEventListener('click', function (e) {
    if (e.target === $('chaos-modal')) toggleChaos(false);
  });
  bindKeys();

  renderJobs(); renderTimers(); renderScenes(); renderReview();
  renderAgentLoop(); renderProvenance(); renderFeed();

  loadHealth();
  bootstrapJobs();
  connectSSE();

  // El cronómetro de "render total" corre en vivo mientras el job renderiza.
  S.nowTimer = setInterval(function () {
    var j = selJob();
    if (j && isLive(j)) renderTimers();
  }, 200);

  window.addEventListener('error', function (e) {
    pushFeed('error', null, (e.message || 'error JS') + ' @ ' + (e.filename || '').split('/').pop() + ':' + e.lineno);
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();

})();
