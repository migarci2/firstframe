/* FirstFrame — frontend vanilla, sin build, sin CDN.
 *
 * Se lee como un programa de edición: cuatro paneles con nombre —PROJECT,
 * MONITOR, TIMELINE, INSPECTOR— y una barra de programa que dice qué hay
 * abierto (`Proyecto — Spot`).
 *
 * Dos públicos, una app:
 *   · Paneles principales → Ana, productora. Elige proyecto, escribe un brief,
 *     ve el vídeo mientras se genera, aprueba o pide cambios.
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
  projects: [],        // [{name, spots}]
  project: null,       // el proyecto activo: donde caen los spots nuevos
  folded: {},          // {nombre: true} — proyectos plegados en el árbol
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
var DEFAULT_PROJECT = 'Untitled Project';

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
function projectOf(j) { return (j && j.project) || DEFAULT_PROJECT; }

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

// "11.6 s", "1 min 03 s" — la cifra exacta, sin unidades de máquina.
function secs(ms) {
  if (ms == null || isNaN(ms)) return '—';
  var s = ms / 1000;
  if (s < 60) return s.toFixed(1) + ' s';
  var m = Math.floor(s / 60), r = Math.round(s % 60);
  return m + ' min ' + (r < 10 ? '0' : '') + r + ' s';
}

// "3 Aug, 18:42" — fecha de persona, no epoch en milisegundos.
function humanDate(ms) {
  if (!ms) return '—';
  var d = new Date(ms);
  if (isNaN(d.getTime())) return '—';
  var mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()];
  var same = d.getFullYear() === new Date().getFullYear();
  return d.getDate() + ' ' + mon + (same ? '' : ' ' + d.getFullYear()) + ', ' +
         String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
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

/* ═════════════════════════ HOME: los proyectos ═════════════════════════
 * Entrar en una herramienta de trabajo es ver el trabajo que hay, no caer
 * dentro del último archivo que alguien abrió. De aquí se sale abriendo un
 * proyecto; el editor (PROJECT/MONITOR/TIMELINE/INSPECTOR) es lo de dentro. */

function showHome(opts) {
  opts = opts || {};
  S.view = 'home';
  S.sel = null;
  Player.teardown();
  document.body.classList.add('at-home');
  $('view-home').hidden = false;
  $('changes').hidden = true;
  $('briefedit').hidden = true;
  if (!opts.keepUrl) go(HOME_PATH, !!opts.replace);
  renderHome();
  renderCrumb();
}

function openProject(name, opts) {
  opts = opts || {};
  S.project = name;
  S.folded[name] = false;
  S.view = 'compose';
  document.body.classList.remove('at-home');
  $('view-home').hidden = true;
  if (!opts.keepUrl) go(projPath(name), !!opts.replace);

  var mine = jobsOf(name);
  // Abrir un proyecto es UN sitio nuevo aunque acabe cayendo en su último spot:
  // el salto al spot corrige la URL en su sitio, no añade otra entrada atrás.
  if (opts.compose || !mine.length) showCompose({ keepUrl: opts.keepUrl });
  else select(mine[0].id, { keepUrl: opts.keepUrl, replace: true });
  renderJobs();
  renderProjectPicker();
  renderCrumb();
}

// Lo que la rejilla sabe de un proyecto sale de los spots que hay en memoria:
// así una miniatura o un estado cambian en el momento, sin esperar al servidor.
function projectMeta(name) {
  var row = null;
  for (var i = 0; i < S.projects.length; i++) {
    if (S.projects[i].name === name) { row = S.projects[i]; break; }
  }
  var mine = jobsOf(name);
  var last = mine.length ? mine[0] : null;
  return {
    name: name,
    spots: mine.length,
    last: last,
    at: last ? last.created_at : (row ? (row.updated_at || row.created_at) : 0)
  };
}

function renderHome() {
  var box = $('home-cards');
  if (!box) return;
  clear(box);

  var names = allProjects();
  var n = names.length, s = S.jobs.length;
  $('home-sub').textContent = n + (n === 1 ? ' project' : ' projects') + ' · ' +
                              s + (s === 1 ? ' spot' : ' spots');
  names.forEach(function (name) { box.appendChild(projectCard(name)); });
}

function projectCard(name) {
  var m = projectMeta(name);
  var card = el('article', 'card');

  // No es un <button>: dentro va a vivir el campo de renombrar, y un input
  // dentro de un botón ni es HTML válido ni recibe el foco.
  var open = el('div', 'card-open');
  open.tabIndex = 0;
  open.setAttribute('role', 'button');

  var th = el('div', 'card-thumb');
  if (m.last) {
    var img = el('img');
    img.alt = '';
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      if (img.parentNode) th.removeChild(img);
      th.classList.add('empty');
      th.appendChild(document.createTextNode('No preview yet'));
    });
    img.src = '/api/jobs/' + m.last.id + '/poster.jpg';
    th.appendChild(img);
  } else {
    th.classList.add('empty');
    th.appendChild(document.createTextNode('No spots yet'));
  }
  open.appendChild(th);

  var body = el('div', 'card-body');
  var h = el('h3', 'card-name', name);
  body.appendChild(h);
  body.appendChild(el('p', 'card-meta',
    m.spots + (m.spots === 1 ? ' spot' : ' spots') + (m.at ? ' · ' + humanDate(m.at) : '')));
  if (m.last) {
    var st = statusOf(m.last);
    body.appendChild(el('span', 'card-status t-' + st.tone, st.label));
  } else {
    body.appendChild(el('span', 'card-status', 'Ready for your first brief'));
  }
  open.appendChild(body);

  function go() { openProject(name); }
  open.addEventListener('click', function (e) {
    if (e.target.tagName === 'INPUT') return;      // renombrando: no navegar
    go();
  });
  open.addEventListener('keydown', function (e) {
    if (e.target.tagName === 'INPUT') return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
  });
  card.appendChild(open);

  var acts = el('div', 'card-acts');
  var ren = el('button', null, 'Rename');
  ren.type = 'button';
  ren.addEventListener('click', function (e) { e.stopPropagation(); renameProject(h, name); });
  acts.appendChild(ren);
  var del = el('button', 'danger', 'Delete');
  del.type = 'button';
  del.addEventListener('click', function (e) {
    e.stopPropagation();
    confirmIn(card, m.spots
      ? 'Delete “' + name + '” and its ' + m.spots + (m.spots === 1 ? ' spot' : ' spots') + '?'
      : 'Delete “' + name + '”?',
      function () { doDeleteProject(name); });
  });
  acts.appendChild(del);
  card.appendChild(acts);

  return card;
}

/* Confirmar donde está la cosa que se va a borrar, no en un alert del navegador. */
function confirmIn(card, text, onYes) {
  var old = card.querySelector('.confirm');
  if (old) card.removeChild(old);
  var box = el('div', 'confirm');
  box.appendChild(el('p', null, text));
  var row = el('div', 'confirm-row');
  var no = el('button', null, 'Cancel');
  no.type = 'button';
  no.addEventListener('click', function (e) {
    e.stopPropagation();
    if (box.parentNode) box.parentNode.removeChild(box);
  });
  var yes = el('button', 'danger', 'Delete');
  yes.type = 'button';
  yes.addEventListener('click', function (e) {
    e.stopPropagation();
    yes.disabled = true;
    onYes();
  });
  row.appendChild(no);
  row.appendChild(yes);
  box.appendChild(row);
  box.addEventListener('click', function (e) { e.stopPropagation(); });
  card.appendChild(box);
}

/* Renombrar en línea: el nombre se convierte en campo y vuelve a ser nombre. */
function renameProject(h, name) {
  var inp = el('input', 'card-edit');
  inp.type = 'text';
  inp.value = name;
  inp.maxLength = 64;
  inp.spellcheck = false;
  h.parentNode.replaceChild(inp, h);
  inp.focus();
  inp.select();

  var done = false;
  function finish(save) {
    if (done) return;
    done = true;
    var v = inp.value.trim();
    if (!save || !v || v === name) { renderHome(); return; }

    api('/api/projects/' + encodeURIComponent(name), { method: 'PATCH', body: { name: v } })
      .then(function (d) {
        var got = (d && d.project && d.project.name) || v;
        // Reflejo local inmediato: la rejilla no se queda esperando al servidor.
        S.jobs.forEach(function (j) { if (projectOf(j) === name) j.project = got; });
        S.projects.forEach(function (p) { if (p.name === name) p.name = got; });
        if (S.project === name) S.project = got;
        if (S.folded[name] != null) { S.folded[got] = S.folded[name]; delete S.folded[name]; }
        // Si la URL nombraba el proyecto viejo, se corrige en su sitio.
        if (location.pathname.indexOf(projPath(name)) === 0) {
          go(location.pathname.replace(projPath(name), projPath(got)), true);
        }
        renderHome();
        renderJobs();
        renderProjectPicker();
        renderCrumb();
        loadProjects();
      })
      .catch(function (e) {
        toast('bad', e.status === 409
          ? 'Another project already uses that name.'
          : 'The project could not be renamed.', null, 8000);
        pushFeed('error', null, 'PATCH /api/projects: ' + e.message,
                 'the project could not be renamed');
        renderHome();
      });
  }
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  inp.addEventListener('blur', function () { finish(true); });
}

function doDeleteProject(name) {
  api('/api/projects/' + encodeURIComponent(name), { method: 'DELETE' })
    .then(function () {
      S.jobs = S.jobs.filter(function (j) { return projectOf(j) !== name; });
      S.projects = S.projects.filter(function (p) { return p.name !== name; });
      if (S.project === name) S.project = null;
      renderHome();
      renderJobs();
      renderProjectPicker();
      loadProjects();
      toast('', 'Project deleted.', null, 6000);
    })
    .catch(function (e) {
      toast('bad', 'The project could not be deleted.', null, 8000);
      pushFeed('error', null, 'DELETE /api/projects: ' + e.message,
               'the project could not be deleted');
      renderHome();
    });
}

function submitHomeProject(e) {
  if (e) e.preventDefault();
  var name = $('home-new-name').value.trim();
  $('home-new').hidden = true;
  if (!name) return;
  api('/api/projects', { method: 'POST', body: { name: name } })
    .then(function () {
      return loadProjects().then(function () { openProject(name, { compose: true }); });
    })
    .catch(function (e2) {
      toast('bad', 'The project could not be created.', null, 8000);
      pushFeed('error', null, 'POST /api/projects: ' + e2.message,
               'the project could not be saved');
    });
}

/* ═════════════════════ rutas ═════════════════════
 * Rutas de verdad, no hash: la URL dice dónde estás, recargar te deja donde
 * estabas y el botón atrás del navegador funciona.
 *
 *   /app/projects              la rejilla
 *   /app/p/<proyecto>          un proyecto abierto
 *   /app/p/<proyecto>/<spot>   un spot abierto
 *
 * El servidor sirve el mismo documento para todo /app/*; el router es esto. */

var HOME_PATH = '/app/projects';

function projPath(name)      { return '/app/p/' + encodeURIComponent(name); }
function spotPath(name, id)  { return projPath(name) + '/' + encodeURIComponent(id); }

// `replace` para el primer pintado y para las correcciones (renombrar, mover):
// esas no son sitios nuevos, son el mismo sitio con otro nombre.
function go(path, replace) {
  if (location.pathname === path) return;
  try { history[replace ? 'replaceState' : 'pushState'](null, '', path + location.search); }
  catch (e) { location.pathname = path; }
}

function readRoute() {
  var p = (location.pathname || '').replace(/\/+$/, '');
  var m = /^\/app\/p\/([^\/]+)(?:\/([^\/]+))?$/.exec(p);
  if (!m) return null;                       // /app, /app/projects → la rejilla
  try {
    return { project: decodeURIComponent(m[1]),
             spot: m[2] ? decodeURIComponent(m[2]) : null };
  } catch (e) { return null; }
}

/* Pinta lo que dice la URL. Se usa al arrancar y en cada `popstate`; nunca
 * vuelve a tocar el historial, o el botón atrás se pelearía consigo mismo. */
function applyRoute() {
  var r = readRoute();
  if (!r) { if (S.view !== 'home') showHome({ keepUrl: true }); return; }
  if (allProjects().indexOf(r.project) === -1) { showHome(); return; }
  if (r.spot && jobById(r.spot)) {
    S.project = r.project;
    if (S.sel !== r.spot) select(r.spot, { keepUrl: true });
    return;
  }
  if (S.project !== r.project || S.view !== 'compose') {
    openProject(r.project, { compose: true, keepUrl: true });
  }
}

/* ═════════════════════════════ vistas ═════════════════════════════ */

function showCompose(opts) {
  opts = opts || {};
  S.view = 'compose';
  S.sel = null;
  Player.teardown();
  if (!opts.keepUrl && S.project) go(projPath(S.project), !!opts.replace);
  $('view-compose').hidden = false;
  $('view-spot').hidden = true;
  $('changes').hidden = true;
  $('briefedit').hidden = true;
  renderJobs();
  renderProjectPicker();
  renderCrumb();
  renderTimeline();
  renderRail();
  renderTech();
  syncCreate();
  setTimeout(function () { $('brief').focus(); }, 30);
}

/* El botón se enciende con el texto: escribir ya es la mitad de la recompensa.
 * Sin brief no hay lanzamiento, y se ve que no lo hay. */
function syncCreate() {
  var ta = $('brief'), btn = $('btn-create'), box = $('view-compose');
  if (!ta || !btn) return;
  var has = !!ta.value.trim();
  btn.disabled = !has;
  if (box) box.classList.toggle('has-brief', has);
  if (!has) markSpark(null);
}

// Marca visualmente el ejemplo elegido (o ninguno, si el texto ya no es suyo).
function markSpark(btn) {
  var box = $('sparks');
  if (!box) return;
  Array.prototype.forEach.call(box.querySelectorAll('button'), function (b) {
    b.classList.toggle('on', b === btn);
  });
}

/* El paso de "escribir" a "ver" tiene un instante de arranque: el monitor y la
 * línea de tiempo cobran vida. Menos de medio segundo — es una herramienta. */
function ignite() {
  var b = document.body;
  b.classList.remove('igniting');
  void b.offsetWidth;                      // reinicia la animación
  b.classList.add('igniting');
  setTimeout(function () { b.classList.remove('igniting'); }, 700);
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
  if (changed) { $('changes').hidden = true; $('briefedit').hidden = true; }

  if (changed || opts.force) Player.load(job);
  // Abrir un spot deja su proyecto como activo: la cabecera del panel y el
  // monitor dicen lo mismo.
  S.project = projectOf(job);
  S.folded[S.project] = false;
  if (!opts.keepUrl) go(spotPath(S.project, job.id), !!opts.replace);

  renderJobs();
  renderStage();
  renderProjectPicker();
  renderRail();
  renderTech();
  loadDetail(id);
}

/* ═════════════════════ panel PROJECT: el navegador ═════════════════════
 * El panel es el proyecto ABIERTO, no el catálogo entero: cabecera con su
 * nombre y, debajo, SOLO sus spots. Estando dentro de un proyecto vacío no
 * tiene ningún sentido ver los spots de los demás — decía justo lo contrario
 * de lo que la pantalla tiene que decir. Para cambiar de proyecto sin salir
 * está el selector; para volver a la rejilla, la miga «Projects /». */

// Todos los proyectos que existen: los declarados en el servidor MÁS los que
// solo aparecen porque algún spot los nombra.
function allProjects() {
  var seen = {}, out = [];
  S.projects.forEach(function (p) {
    if (seen[p.name]) return;
    seen[p.name] = true;
    out.push(p.name);
  });
  S.jobs.forEach(function (j) {
    var n = projectOf(j);
    if (seen[n]) return;
    seen[n] = true;
    out.push(n);
  });
  if (!out.length) out.push(DEFAULT_PROJECT);
  return out;
}

function jobsOf(name) {
  return S.jobs.filter(function (j) { return projectOf(j) === name; });
}

// El proyecto abierto. Nunca null: si aún no hay ninguno, el primero que exista.
function curProject() {
  var all = allProjects();
  if (S.project && all.indexOf(S.project) !== -1) return S.project;
  return all[0];
}

function renderJobs() {
  renderProjSel();

  var tree = $('tree');
  if (!tree) return;
  var scroll = tree.parentNode ? tree.parentNode.scrollTop : 0;
  clear(tree);

  var mine = jobsOf(curProject());
  if (!mine.length) {
    var box = el('div', 'proj-empty');
    box.appendChild(el('p', null, 'No spots in this project yet.'));
    box.appendChild(el('p', 'dim', 'Write a brief and the first one appears here.'));
    tree.appendChild(box);
  } else {
    mine.forEach(function (job) {
      var row = el('button', 'jobrow' + (job.id === S.sel ? ' sel' : ''));
      row.type = 'button';
      row.appendChild(el('b', null, spotTitle(job)));
      var st = statusOf(job);
      var s = el('span', 't-' + st.tone);
      s.appendChild(el('i'));
      s.appendChild(document.createTextNode(st.label));
      row.appendChild(s);
      row.addEventListener('click', function () { select(job.id); });
      tree.appendChild(row);
    });
  }

  if (tree.parentNode) tree.parentNode.scrollTop = scroll;
}

/* — el selector: cambiar de proyecto sin volver a la rejilla — */

function renderProjSel() {
  var nm = $('projsel-name');
  if (!nm) return;
  var name = curProject();
  var n = jobsOf(name).length;
  nm.textContent = name;
  nm.title = name;
  $('projsel-n').textContent = n + (n === 1 ? ' spot' : ' spots');
}

function toggleProjMenu(show) {
  var menu = $('projsel-menu'), btn = $('projsel-btn');
  if (!menu || !btn) return;
  if (show === undefined) show = menu.hidden;

  if (show) {
    clear(menu);
    var cur = curProject();
    allProjects().forEach(function (name) {
      var it = el('button', 'projsel-item' + (name === cur ? ' cur' : ''));
      it.type = 'button';
      it.setAttribute('role', 'menuitem');
      it.appendChild(el('span', 'nm', name));
      var n = jobsOf(name).length;
      it.appendChild(el('span', 'n', String(n)));
      it.addEventListener('click', function () {
        toggleProjMenu(false);
        if (name !== cur) openProject(name);
      });
      menu.appendChild(it);
    });
    var all = el('button', 'projsel-item projsel-all', 'All projects');
    all.type = 'button';
    all.setAttribute('role', 'menuitem');
    all.addEventListener('click', function () { toggleProjMenu(false); showHome(); });
    menu.appendChild(all);
  }

  menu.hidden = !show;
  btn.setAttribute('aria-expanded', show ? 'true' : 'false');
}

function renderProjectPicker() {
  var sel = $('proj-picker');
  if (!sel) return;
  var keep = S.project || sel.value;
  clear(sel);
  allProjects().forEach(function (n) {
    var o = el('option', null, n);
    o.value = n;
    sel.appendChild(o);
  });
  if (keep && jobsOfName(keep)) sel.value = keep;
  S.project = sel.value || allProjects()[0];
}

function jobsOfName(n) {
  return allProjects().indexOf(n) !== -1;
}

function renderCrumb() {
  var job = selJob();
  var p = job ? projectOf(job) : (S.project || DEFAULT_PROJECT);
  $('crumb-project').textContent = p;
  $('crumb-spot').textContent = job ? spotTitle(job)
    : (S.view === 'compose' ? 'new spot' : 'no spot open');
}

/* Crear proyecto: una fila de texto en el propio panel, sin diálogo. */
function openNewProject() {
  var f = $('newproj');
  f.hidden = false;
  $('newproj-name').value = '';
  $('newproj-name').focus();
}

function submitNewProject(e) {
  if (e) e.preventDefault();
  var name = $('newproj-name').value.trim();
  $('newproj').hidden = true;
  if (!name) return;

  // Optimista: el proyecto aparece ya y se convierte en el activo. Si el POST
  // falla, el spot que se cree ahí lo crearía igual en el servidor.
  if (allProjects().indexOf(name) === -1) S.projects.push({ name: name, spots: 0 });
  S.project = name;
  S.folded[name] = false;
  go(projPath(name));
  renderJobs();
  renderProjectPicker();
  renderCrumb();

  api('/api/projects', { method: 'POST', body: { name: name } })
    .then(function () { loadProjects(); })
    .catch(function (err) { pushFeed('error', null, 'POST /api/projects: ' + err.message, 'the project could not be saved'); });
}

function loadProjects() {
  return api('/api/projects').then(function (d) {
    S.projects = d.projects || [];
    if (!S.project) S.project = allProjects()[0];
    renderJobs();
    renderProjectPicker();
    renderCrumb();
    if (S.view === 'home') renderHome();
  }).catch(function () {});
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

  topMeta();
  renderCrumb();
  renderTimeline();
  renderActions(job);
  renderSpotTools(job);
  renderNoteScenes(job);
}

/* ═════════════════ el spot también se edita ═════════════════
 * Renombrar, corregir el brief, moverlo de proyecto y tirarlo. Debajo del
 * título, en gris: están cuando hacen falta y no compiten con Approve. */

function renderSpotTools(job) {
  var box = $('spot-tools');
  if (!box) return;
  clear(box);

  function act(label, cls, fn) {
    var b = el('button', cls || null, label);
    b.type = 'button';
    b.addEventListener('click', fn);
    box.appendChild(b);
    return b;
  }

  act('Rename', null, function () { renameSpot(job); });
  act('Edit brief', null, openBriefEdit);

  box.appendChild(el('span', 'sep', 'in'));
  var sel = el('select');
  sel.setAttribute('aria-label', 'Move to another project');
  allProjects().forEach(function (n) {
    var o = el('option', null, n);
    o.value = n;
    sel.appendChild(o);
  });
  sel.value = projectOf(job);
  sel.addEventListener('change', function () { moveSpot(job, this.value); });
  box.appendChild(sel);

  act('Delete', 'danger', function () { askDeleteSpot(job); });
}

function renameSpot(job) {
  var h = $('spot-title');
  if (!h || h.hidden) return;
  var inp = el('input', 'titleedit');
  inp.type = 'text';
  inp.value = job.title || spotTitle(job);
  inp.maxLength = 120;
  inp.spellcheck = false;
  h.hidden = true;
  h.parentNode.insertBefore(inp, h.nextSibling);
  inp.focus();
  inp.select();

  var done = false;
  function finish(save) {
    if (done) return;
    done = true;
    var v = inp.value.trim();
    if (inp.parentNode) inp.parentNode.removeChild(inp);
    h.hidden = false;
    if (!save || !v || v === (job.title || '')) return;

    job.title = v;                       // optimista: el árbol y la barra ya lo dicen
    renderStage();
    renderJobs();
    api('/api/jobs/' + job.id, { method: 'PATCH', body: { title: v } })
      .then(function (d) { if (d.job) upsertJob(d.job); })
      .catch(function (e) {
        toast('bad', 'The name could not be saved.', null, 8000);
        pushFeed('error', job.id, 'PATCH /api/jobs: ' + e.message,
                 'the name could not be saved');
      });
  }
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  inp.addEventListener('blur', function () { finish(true); });
}

function moveSpot(job, name) {
  if (!name || name === projectOf(job)) return;
  api('/api/jobs/' + job.id, { method: 'PATCH', body: { project: name } })
    .then(function (d) {
      if (d.job) upsertJob(d.job);
      S.project = name;
      S.folded[name] = false;
      go(spotPath(name, job.id), true);   // el spot no se ha movido de sitio: se corrige
      renderJobs();
      renderProjectPicker();
      renderCrumb();
      loadProjects();
      toast('', 'Moved to “' + name + '”.', null, 6000);
    })
    .catch(function (e) {
      toast('bad', 'The spot could not be moved.', null, 8000);
      pushFeed('error', job.id, 'PATCH /api/jobs: ' + e.message,
               'the spot could not be moved');
      renderSpotTools(job);
    });
}

function askDeleteSpot(job) {
  var box = $('spot-tools');
  clear(box);
  box.appendChild(el('span', 'sep', 'Delete this spot and everything generated for it?'));
  var no = el('button', null, 'Cancel');
  no.type = 'button';
  no.addEventListener('click', function () { renderSpotTools(job); });
  box.appendChild(no);
  var yes = el('button', 'danger', 'Delete');
  yes.type = 'button';
  yes.addEventListener('click', function () {
    yes.disabled = true;
    api('/api/jobs/' + job.id, { method: 'DELETE' })
      .then(function () { dropJob(job.id); toast('', 'Spot deleted.', null, 6000); })
      .catch(function (e) {
        toast('bad', 'The spot could not be deleted.', null, 8000);
        pushFeed('error', job.id, 'DELETE /api/jobs: ' + e.message,
                 'the spot could not be deleted');
        renderSpotTools(job);
      });
  });
  box.appendChild(yes);
}

// Quitar un spot del estado local y dejar la vista en un sitio sensato.
function dropJob(id) {
  var was = S.project;
  S.jobs = S.jobs.filter(function (j) { return j.id !== id; });
  delete S.segs[id];
  delete S.iters[id];
  if (S.sel === id) {
    S.sel = null;
    if (S.view !== 'home') {
      var left = was ? jobsOf(was) : [];
      if (left.length) select(left[0].id);
      else showCompose();
    }
  }
  renderJobs();
  if (S.view === 'home') renderHome();
  loadProjects();
}

/* Corregir el brief. Guardar sólo cambia el texto del expediente; guardar y
 * regenerar relanza la generación SOBRE EL MISMO spot: no se pierde el sitio
 * en el árbol ni el historial de decisiones. */
function openBriefEdit() {
  var job = selJob();
  if (!job) return;
  $('changes').hidden = true;
  $('brief-edit').value = job.brief || '';
  $('briefedit').hidden = false;
  $('brief-edit').focus();
}

function saveBrief(regen) {
  var job = selJob();
  if (!job) return;
  var text = $('brief-edit').value.trim();
  if (!text) return;
  $('briefedit').hidden = true;

  if (!regen) {
    api('/api/jobs/' + job.id, { method: 'PATCH', body: { brief: text } })
      .then(function (d) {
        if (d.job) upsertJob(d.job);
        toast('', 'Brief updated.', null, 6000);
      })
      .catch(function (e) {
        toast('bad', 'The brief could not be saved.', null, 8000);
        pushFeed('error', job.id, 'PATCH /api/jobs: ' + e.message,
                 'the brief could not be saved');
      });
    return;
  }

  api('/api/jobs/' + job.id + '/regenerate', { method: 'POST', body: { brief: text } })
    .then(function (d) {
      S.segs[job.id] = 0;
      S.iters[job.id] = [];
      if (d.job) upsertJob(d.job);
      var fresh = jobById(job.id);
      if (fresh) { Player.jobId = null; Player.load(fresh); }
      renderStage();
      pushFeed('job_update', job.id, 'regenerate', 'regenerating with the new brief');
      toast('', 'Regenerating with the new brief.',
            'You will be watching the new version in seconds.', 8000);
    })
    .catch(function (e) {
      toast('bad', 'We could not relaunch the generation.', null, 8000);
      pushFeed('error', job.id, 'POST /api/jobs/regenerate: ' + e.message,
               'the generation could not be relaunched');
    });
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

/* ═══════════════════════ panel TIMELINE ═══════════════════════
 * Las escenas SON la línea de tiempo. El servidor manda `start` y `seconds`
 * por escena, sacados de la duración real de los segmentos HLS: los bloques
 * miden lo que duran de verdad. Lo que aún no existe se dibuja como hueco. */

var NOMINAL_SEC = 4;   // lo que se le supone a una escena que todavía no existe

function renderTimeline() {
  var track = $('tl-track'), ruler = $('tl-ruler'), meta = $('tl-meta');
  if (!track) return;
  clear(track);
  clear(ruler);

  var job = selJob();
  var scenes = (job && job.scenes) || [];

  if (!job || !scenes.length) {
    meta.textContent = '';
    track.appendChild(el('div', 'tl-empty',
      job ? 'no scenes yet' : 'no sequence open'));
    $('tl-head').hidden = true;
    return;
  }

  var ready = 0, dur = 0;
  scenes.forEach(function (s) {
    if (s.status === 'ready') ready++;
    dur += (s.seconds != null ? s.seconds : NOMINAL_SEC);
  });
  var lead = job.lead_seconds || 0;
  meta.textContent = ready + '/' + scenes.length + ' scenes · ' + fmtTC(dur);

  scenes.forEach(function (s) {
    var w = (s.seconds != null && s.seconds > 0) ? s.seconds : NOMINAL_SEC;
    var clip = el('button', 'tl-clip ' + (s.status || 'pending'));
    clip.type = 'button';
    clip.style.flex = w + ' 1 0';
    clip.title = 'Scene ' + s.n + (s.title ? ' — ' + s.title : '');
    clip.appendChild(el('span', 'cn', s.n + '. ' + (s.title || 'Scene ' + s.n)));
    clip.appendChild(el('span', 'cs',
      s.status === 'ready' ? fmtTC(s.seconds != null ? s.seconds : 0)
      : s.status === 'rendering' ? 'generating' : s.status));
    // Clicar un clip mueve el vídeo al principio de esa escena. Es lo que
    // espera cualquiera que haya tocado un timeline.
    clip.addEventListener('click', function () { seekScene(s); });
    track.appendChild(clip);
  });

  // Regla: una marca por segundo y etiqueta cada 5 (o cada 2 si es muy corto).
  var step = dur > 24 ? 5 : 2;
  for (var t = 0; t <= Math.floor(dur); t++) {
    var pct = t / dur * 100;
    var lab = t % step === 0 && pct < 92;   // la última etiqueta se saldría del panel
    var tick = el('div', 'tl-tick' + (lab ? ' lab' : ''));
    tick.style.left = pct + '%';
    if (lab) tick.appendChild(el('span', null, fmtTC(t)));
    ruler.appendChild(tick);
  }

  playhead(lead, dur);
}

function fmtTC(sec) {
  sec = Math.max(0, sec || 0);
  var m = Math.floor(sec / 60), s = sec - m * 60;
  return m + ':' + (s < 10 ? '0' : '') + (Math.round(s * 10) / 10).toFixed(1).replace(/\.0$/, '');
}

function seekScene(s) {
  var v = Player.video;
  if (!v || s.start == null) return;
  try { v.currentTime = s.start + 0.05; v.play().catch(function () {}); } catch (e) {}
}

/* El playhead se mide contra los BLOQUES reales, no contra un porcentaje del
 * ancho: así sigue cuadrando aunque una escena dure el doble que otra. */
function playhead(lead, dur) {
  var head = $('tl-head'), track = $('tl-track');
  var v = Player.video, job = selJob();
  if (!head || !v || !job) return;

  var t = v.currentTime || 0;
  var scenes = job.scenes || [];
  var clips = track.children;
  var x = null;

  for (var i = 0; i < scenes.length && i < clips.length; i++) {
    var s = scenes[i];
    if (s.start == null || !s.seconds) continue;
    if (t >= s.start && t < s.start + s.seconds) {
      x = clips[i].offsetLeft + (t - s.start) / s.seconds * clips[i].offsetWidth;
      break;
    }
  }
  // Antes de la primera escena estamos en la cabecera del leader: el playhead
  // se queda a la izquierda del todo en vez de desaparecer.
  if (x == null && t < lead) x = 0;
  if (x == null) { head.hidden = true; return; }

  head.hidden = false;
  head.style.left = Math.round(x) + 'px';
}

var headRAF = null;
function startPlayhead() {
  if (headRAF) return;
  var loop = function () {
    var v = Player.video, job = selJob();
    if (!v || v.paused || !job || S.view !== 'spot') { headRAF = null; return; }
    playhead(job.lead_seconds || 0, 0);
    headRAF = requestAnimationFrame(loop);
  };
  headRAF = requestAnimationFrame(loop);
}

/* ═════════════════════════ acciones ═════════════════════════ */

function createSpot() {
  var brief = $('brief').value.trim();
  if (!brief) { $('brief').focus(); return; }
  var btn = $('btn-create');
  btn.disabled = true;
  btn.textContent = 'Creating…';
  var project = ($('proj-picker') && $('proj-picker').value) || S.project || DEFAULT_PROJECT;

  api('/api/jobs', { method: 'POST', body: {
    brief: brief, title: brief.slice(0, 70), scenes: S.scenes, project: project
  }}).then(function (d) {
    var job = d.job || { id: d.id, status: 'queued', title: brief.slice(0, 70),
                         project: project };
    if (!job.project) job.project = project;
    upsertJob(job);
    S.sel = null;
    ignite();
    select(job.id, { force: true });
    $('brief').value = '';
    pushFeed('job_update', job.id, 'job created · pipeline started', 'spot created, generation started');
    loadProjects();
  }).catch(function (e) {
    toast('bad', 'We could not create the spot.', 'Try again in a few seconds.', 9000);
    pushFeed('error', null, 'POST /api/jobs: ' + e.message, 'the spot could not be created');
  }).then(function () {
    btn.textContent = 'Create spot';
    syncCreate();
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
      pushFeed('error', job.id, 'decision ' + action + ': ' + e.message, 'your decision was not saved');
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
    pushFeed('error', job.id, 'download: ' + e.message, 'the download is not available right now');
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
  kv('Spot', job.id);
  kv('Created', job.created_at_iso || '—');
  kv('Scenes', String(job.scene_count || (job.scenes || []).length));
  kv('First frame', secs(job.first_frame_ms));
  kv('Full render', secs(job.total_render_ms));
  kv('Stream', job.stream_url || '—');
  kv('Manifest', job.manifest_url ? 'provenance/' + job.id + '/manifest.json' : '— (not yet)');
  kv('Object Lock', job.lock ? job.lock.mode + ' until ' + job.lock.retain_until : '— (not approved)');

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

  // El manifest se LEE, no se vuelca. Antes esto imprimía el JSON entero: dos mil
  // caracteres de llaves que nadie inspecciona y que hacen parecer la herramienta
  // un volcado de depuración. Se enseña lo que dice; el fichero crudo, a un clic.
  var box = el('div', 'manibox');
  body.appendChild(box);
  if (!job.manifest_url) {
    box.appendChild(el('div', 'tnote', 'No manifest yet — it is written when the render ends.'));
  } else {
    fetchManifest(job, box);
    var raw = el('a', 'rawlink', 'View raw manifest');
    raw.href = job.manifest_url;
    raw.target = '_blank';
    raw.rel = 'noopener';
    body.appendChild(raw);
  }
}

function fetchManifest(job, box) {
  clear(box);
  box.appendChild(el('div', 'tnote', 'Reading the manifest…'));
  var forJob = job.id;
  fetch(job.manifest_url, { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (m) { return { ok: r.ok, status: r.status, m: m }; },
                         function () { return { ok: false, status: r.status, m: null }; });
  }).then(function (res) {
    if (S.sel !== forJob || !box.parentNode) return;
    clear(box);
    if (!res.ok || !res.m) {
      box.appendChild(el('div', 'tnote',
        'The manifest is not readable right now (it is written when the render ends).'));
      return;
    }
    renderManifest(res.m, box);
  }).catch(function () {
    if (!box.parentNode) return;
    clear(box);
    box.appendChild(el('div', 'tnote', 'The manifest could not be read right now.'));
  });
}

// El manifest, en filas. Cada línea es un hecho comprobable: qué se generó, con qué
// modo, cuánto tardó y qué objetos lo respaldan en B2.
function renderManifest(m, box) {
  function line(k, v) {
    var r = el('div', 'kv');
    r.appendChild(el('span', null, k));
    r.appendChild(el('span', null, v));
    box.appendChild(r);
  }
  var sc = m.scenes || [], seg = m.segments || [], pev = m.provider_events || [];
  line('Sealed at', m.created_at || '—');
  line('Generation mode', m.mode || '—');
  line('Brief', (m.brief || '—').slice(0, 120));
  line('First frame', secs(m.first_frame_ms));
  line('Full render', secs(m.total_render_ms));
  line('Scenes recorded', sc.length + ' (' +
       sc.filter(function (s) { return s.status === 'ready'; }).length + ' ready)');
  line('Segments in B2', String(seg.length));
  line('Pipeline events', String(pev.length));

  if (seg.length) {
    box.appendChild(el('div', 'subhead', 'OBJECT KEYS'));
    var lin = el('div', 'lin');
    seg.slice(0, 8).forEach(function (s) {
      lin.appendChild(el('div', null, (s.key || 'seq ' + s.seq) +
                                      (s.duration ? '  ' + Number(s.duration).toFixed(1) + 's' : '')));
    });
    if (seg.length > 8) lin.appendChild(el('div', null, '…and ' + (seg.length - 8) + ' more'));
    box.appendChild(lin);
  }
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

/* El feed tiene dos lectores. `detail` es la cadena técnica y se queda en
 * "Technical details", donde es evidencia. `human` es lo mismo contado en
 * castellano llano y es lo único que entra en el inspector: rutas de objeto,
 * claves de B2 y nombres de modelo no le dicen nada a quien produce el anuncio. */
var FEED_HUMAN = {
  scene_ready:       'Scene ready',
  render_started:    'Streaming started',
  render_complete:   'Render finished',
  segment_landed:    'Saved to storage',
  provider_failover: 'Switched provider',
  judge_score:       'Quality check',
  approved:          'Approved',
  rejected:          'Changes requested',
  job_update:        'Updated',
  chaos:             'Provider test',
  error:             'Problem'
};

function pushFeed(type, jobId, detail, human) {
  S.feed.push({ at: Date.now(), type: type, job: jobId, detail: detail, human: human });
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
  var box = $('mon-meta'), kick = $('spot-kicker');
  if (!box) return;
  clear(box);
  var job = selJob();

  if (kick) {
    var st = job ? statusOf(job) : null;
    kick.textContent = st ? st.label : '';
    kick.className = 'stagekicker' + (st && st.tone ? ' ' + st.tone : '');
  }
  var insp = $('insp-meta');
  if (insp) insp.textContent = job ? job.id : '';
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
  // Las siglas y los modos internos viven en "Technical details". Aquí, el estado
  // del servicio dicho como se lo contarías a alguien por teléfono.
  chip('Storage', !h.b2 ? 'local only' : (h.b2_capped ? 'saving locally' : 'Backblaze B2'),
       h.b2 && !h.b2_capped ? 'ok' : 'warn');
  chip('Live updates', h.events_mode && h.events_mode !== 'off' ? 'on' : 'off');
  chip('Playback', Player.engine === 'native' ? 'built-in' : (Player.engine || '—'));
  if (h.degraded) chip('Service', 'reduced capacity', 'warn');
}

function railScenes() {
  var body = $('r-scenes');
  if (!body) return;
  clear(body);
  var job = selJob();
  var sc = (job && job.scenes) || [];
  var ready = sc.filter(function (x) { return x.status === 'ready'; }).length;
  $('r-n-scenes').textContent = job ? ready + '/' + (job.scene_count || sc.length) : '';
  $('rb-scenes').hidden = !sc.length;
  if (!sc.length) return;
  sc.forEach(function (x) {
    var r = el('div', 'scenerow ' + x.status);
    r.appendChild(el('span', 'no', String(x.n)));
    r.appendChild(el('span', 'ti', x.title || '—'));
    r.appendChild(el('span', 'st', x.status + (x.ms != null ? ' ' + Math.round(x.ms / 1000) + 's' : '')));
    body.appendChild(r);
  });
}

/* El expediente del spot, en frases.
 *
 * Aquí vivía un volcado de `JSON.stringify(job)`. Un volcado no es información:
 * es una estructura de datos puesta delante de alguien que no la pidió. Los
 * mismos hechos —y no uno menos, porque son la evidencia de que esto usa B2 y
 * Genblaze de verdad— dichos en el idioma de quien mira. El dato crudo no se
 * pierde: sale por "View raw manifest", que es donde lo va a buscar quien lo
 * quiera. */
function railProv() {
  var body = $('r-prov');
  if (!body) return;
  clear(body);
  var job = selJob();
  $('rb-prov').hidden = !job;
  if (!job) return;

  function row(k, v, sub) {
    var r = el('div', 'kv');
    r.appendChild(el('span', null, k));
    var val = el('span', null);
    val.appendChild(document.createTextNode(v));
    if (sub) val.appendChild(el('em', 'kv-sub', sub));
    r.appendChild(val);
    body.appendChild(r);
  }

  var sc = job.scenes || [];
  var ready = sc.filter(function (x) { return x.status === 'ready'; }).length;
  var total = job.scene_count || sc.length;

  row('Status', statusOf(job).label);
  row('Scenes', total ? ready + ' of ' + total + ' ready' : 'none yet');
  if (job.first_frame_ms != null) row('First frame', 'after ' + secs(job.first_frame_ms));
  if (job.total_render_ms != null) row('Full render', secs(job.total_render_ms));
  row('Created', humanDate(job.created_at));
  row('Stored in', 'Backblaze B2', 'every take, segment and master');

  if (job.lock) {
    row('Protection', 'Locked for ' + daysLeft(job.lock.retain_until) + ' more days',
        'Object Lock, ' + String(job.lock.mode || '').toLowerCase() + ' mode');
  } else {
    row('Protection', 'Applied on approval', 'the master becomes undeletable');
  }

  row('Provenance', job.manifest_url
    ? (job.status === 'approved' ? 'Sealed with the master' : 'Recorded')
    : 'Written when the render ends');

  if (job.manifest_url) {
    var a = el('a', 'rawlink', 'View raw manifest');
    a.href = job.manifest_url;
    a.target = '_blank';
    a.rel = 'noopener';
    body.appendChild(a);
  }
}

function railFeed() {
  var body = $('r-feed');
  if (!body) return;
  clear(body);
  $('r-n-feed').textContent = S.feed.length;
  $('rb-feed').hidden = !S.feed.length;
  if (!S.feed.length) return;
  S.feed.slice(-40).reverse().forEach(function (f) {
    var r = el('div', 'feedrow');
    r.appendChild(el('span', 't', fmtClock(f.at)));
    r.appendChild(el('span', 'k ' + f.type, FEED_HUMAN[f.type] || 'Update'));
    if (f.human) r.appendChild(el('span', 'd', f.human));
    body.appendChild(r);
  });
}

/* Sin spot abierto no hay nada que inspeccionar ni ninguna secuencia que
 * recorrer: el inspector y la línea de tiempo se van, y la pantalla de crear
 * se queda siendo el campo y poco más. Vuelven, ya desplegados, en cuanto hay
 * algo que enseñar. */
function renderRail() {
  var open = !!selJob();
  document.body.classList.toggle('no-spot', !open);
  var b = $('btn-rail');
  if (b) {
    b.disabled = !open;
    b.setAttribute('aria-expanded',
      open && !document.body.classList.contains('no-rail') ? 'true' : 'false');
  }
  topMeta(); railChips(); railScenes(); railProv(); railFeed();
}

/* ═════════════════════════ datos + SSE ═════════════════════════ */

function upsertJob(job) {
  if (!job || !job.id) return;
  var found = false;
  for (var i = 0; i < S.jobs.length; i++) {
    if (S.jobs[i].id === job.id) { S.jobs[i] = job; found = true; break; }
  }
  if (!found) S.jobs.unshift(job);
  renderJobs();
  if (S.view === 'home') renderHome();
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
    pushFeed('error', id, 'GET /api/jobs/' + id + ': ' + e.message, 'could not refresh this spot');
  });
}

var esRetry = 0;
var esFails = 0;

/* Si el SSE no llega a abrirse en varios intentos —detrás de un proxy que no
 * deja pasar el stream, por ejemplo— dejamos de insistir: cada intento fallido
 * escupe un error en la consola del navegador que no podemos silenciar. En su
 * lugar se cae a un sondeo de /api/jobs, que mantiene la sala viva igual. */
function pollJobs() {
  if (pollJobs.on) return;
  pollJobs.on = setInterval(function () {
    api('/api/jobs').then(function (d) {
      S.jobs = d.jobs || [];
      renderJobs();
      if (S.sel) { renderStage(); renderRail(); loadDetail(S.sel); }
    }).catch(function () {});
  }, 4000);
}

function connectSSE() {
  var es;
  try { es = new EventSource('/api/events'); }
  catch (e) { pollJobs(); return; }

  var TYPES = ['hello', 'job_update', 'render_started', 'segment_landed', 'scene_ready',
               'render_complete', 'provider_failover', 'judge_score', 'approved',
               'rejected', 'chaos', 'ping'];

  es.onopen = function () { esRetry = 0; esFails = 0; };
  es.onerror = function () {
    try { es.close(); } catch (e) {}
    if (++esFails >= 5) { pollJobs(); return; }
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
        pushFeed('scene_ready', jid, 'scene ' + d.scene + ' ready' + (d.ms ? ' · ' + d.ms + ' ms' : ''),
                 'scene ' + d.scene + (d.ms ? ' in ' + secs(d.ms) : ''));
      } else if (type === 'render_complete') {
        pushFeed('render_complete', jid, 'total_render_ms ' + d.total_render_ms,
                 'the whole spot took ' + secs(d.total_render_ms));
        if (jid === S.sel) toast('', 'Your spot is ready for review.', null, 7000);
      } else {
        pushFeed('job_update', jid, (d.job && d.job.status) || '',
                 d.job ? statusOf(d.job).label.toLowerCase() : '');
      }
      return;

    case 'render_started':
      pushFeed('render_started', jid, 'first segment on B2 · scene ' + (d.scene != null ? d.scene : '—'),
               'the first seconds are watchable');
      if (d.job) upsertJob(d.job);
      return;

    case 'segment_landed':
      S.segs[jid] = (S.segs[jid] || 0) + 1;
      if (jid === S.sel) { topMeta(); renderTechChips(); }
      pushFeed('segment_landed', jid, 'b2:ObjectCreated · ' + (d.key || ('seq ' + d.seq)),
               'a new piece of video is safe in Backblaze B2');
      return;

    case 'provider_failover':
      // Ana no necesita saber qué modelo era: sólo que no ha perdido el trabajo.
      toast('warn', 'A provider failed; we continued on another one without losing the work.', null, 9000);
      pushFeed('provider_failover', jid,
               (d.model || '?') + ' → ' + (d.fallback_model || '?') + (d.scene ? ' · scene ' + d.scene : ''),
               'one generator failed, another took over — no work lost');
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene,
                               reason: (d.model || '?') + ' MODEL_ERROR en ' + (d.provider || '?'),
                               action: 'fallback → ' + (d.fallback_model || '?') });
      return;

    case 'judge_score':
      pushFeed('judge_score', jid, 'scene ' + d.scene + ' · score ' + (d.score != null ? d.score.toFixed(2) : '—'),
               'scene ' + d.scene + ' reviewed automatically');
      if (jid) pushIter(jid, { _live: true, at: Date.now(), scene: d.scene, score: d.score,
                               iteration: d.iteration,
                               reason: d.detail || d.reason || 'vision judge (llama-3.2-90b-vision)',
                               action: (d.score != null && d.score < 0.6) ? 'prompt refined · scene relaunched' : null });
      return;

    case 'approved':
      if (d.job) upsertJob(d.job);
      pushFeed('approved', jid, 'Object Lock GOVERNANCE · ' + (d.key || ''),
               'the master is now locked against deletion');
      if (jid === S.sel) {
        var days = d.lock ? daysLeft(d.lock.retain_until) : 30;
        toast('good', 'Approved and locked.', 'Nobody can delete or modify it for ' + days + ' days.', 9000);
      }
      return;

    case 'rejected':
      if (d.job) upsertJob(d.job);
      pushFeed('rejected', jid, 'note: ' + (d.note || '') + (d.scene ? ' · scene ' + d.scene : ''),
               d.note ? '\u201c' + d.note + '\u201d' : 'reworking that part');
      if (jid === S.sel) {
        var j = jobById(jid);
        if (j) Player.load(j, { reattach: true });
        renderStage();
      }
      return;

    case 'job_deleted':
      dropJob(d.job_id);
      return;

    case 'projects_changed':
      // Otra pestaña (o el propio servidor) tocó la estructura: se recarga entera.
      api('/api/jobs').then(function (r) {
        S.jobs = r.jobs || [];
        renderJobs();
        if (S.view === 'home') renderHome();
      }).catch(function () {});
      loadProjects();
      return;

    case 'chaos':
      S.chaos[d.provider] = !!d.dead;
      pushFeed('chaos', jid, d.provider + (d.dead ? ' DOWN' : ' back up'),
               d.dead ? 'a generator was taken down on purpose' : 'the generator is back');
      toast(d.dead ? 'warn' : 'good',
            d.dead ? 'Provider taken down on purpose.' : 'Provider back up.',
            d.dead ? 'The next attempt will switch to another provider on its own.' : null, 7000);
      renderChaos();
      return;
  }
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
      pushFeed('error', null, 'health: ' + (h.warning || 'degraded'), 'running at reduced capacity');
    }
  }).catch(function (e) {
    toast('bad', 'Cannot reach the server.', 'Still retrying — nothing you already have is lost.', 12000);
    pushFeed('error', null, 'GET /api/health: ' + e.message, 'the server is not answering');
  });
}

function bootstrapJobs() {
  api('/api/jobs').then(function (d) {
    S.jobs = d.jobs || [];
    renderJobs();
    renderProjectPicker();
    // Se entra por donde diga la URL. Sin ruta útil, la rejilla — y sin dejar
    // rastro en el historial, que este es el primer sitio, no el segundo.
    var r = readRoute();
    if (r && allProjects().indexOf(r.project) !== -1) {
      if (r.spot && jobById(r.spot)) { S.project = r.project; select(r.spot, { keepUrl: true }); }
      else openProject(r.project, { compose: true, replace: true });
    } else {
      showHome({ replace: true });
    }
  }).catch(function () {
    renderJobs();
    showHome({ replace: true });
  });
}

function bindKeys() {
  document.addEventListener('keydown', function (e) {
    var t = e.target || {};
    var typing = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT';

    if (e.key === 'Escape') {
      var pm = $('projsel-menu');
      if (pm && !pm.hidden) { toggleProjMenu(false); return; }
      if (S.techOpen) { toggleTech(false); return; }
      if (!$('changes').hidden) { $('changes').hidden = true; return; }
      if (!$('briefedit').hidden) { $('briefedit').hidden = true; return; }
      if (S.view === 'spot') { showHome(); return; }
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
  $('btn-new-project').addEventListener('click', openNewProject);

  // ── el brief: ejemplos que se pulsan y un botón que se enciende ──
  // Escribir a mano desmarca el ejemplo: el texto ya es del usuario.
  // (Rellenar el textarea por código no dispara `input`, así que el clic en un
  // ejemplo no se auto-desmarca.)
  $('brief').addEventListener('input', function () {
    markSpark(null);
    syncCreate();
  });
  $('sparks').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-brief]');
    if (!b) return;
    var ta = $('brief');
    ta.value = b.getAttribute('data-brief');
    markSpark(b);
    syncCreate();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });
  syncCreate();

  // ── selector de proyecto del panel PROJECT ──
  $('projsel-btn').addEventListener('click', function (e) {
    e.stopPropagation();
    toggleProjMenu();
  });
  document.addEventListener('click', function (e) {
    var m = $('projsel-menu');
    if (m && !m.hidden && !e.target.closest('.projsel')) toggleProjMenu(false);
  });

  // ── HOME ──
  $('crumb-home').addEventListener('click', showHome);
  $('btn-home-new').addEventListener('click', function () {
    $('home-new').hidden = false;
    $('home-new-name').value = '';
    $('home-new-name').focus();
  });
  $('home-new').addEventListener('submit', submitHomeProject);
  $('home-new-cancel').addEventListener('click', function () { $('home-new').hidden = true; });
  $('home-new-name').addEventListener('keydown', function (e) {
    if (e.key === 'Escape') $('home-new').hidden = true;
  });
  // Atrás y adelante del navegador: repintar lo que dice la URL, sin volver a
  // tocar el historial.
  window.addEventListener('popstate', applyRoute);

  // ── edición del spot abierto ──
  $('spot-title').addEventListener('dblclick', function () {
    var j = selJob();
    if (j) renameSpot(j);
  });
  $('btn-brief-cancel').addEventListener('click', function () { $('briefedit').hidden = true; });
  $('btn-brief-save').addEventListener('click', function () { saveBrief(false); });
  $('btn-brief-regen').addEventListener('click', function () { saveBrief(true); });

  $('btn-signout').addEventListener('click', function () {
    api('/api/access/exit', { method: 'POST', body: {} })
      .catch(function () {})
      .then(function () { window.location.replace('/access'); });
  });
  $('newproj').addEventListener('submit', submitNewProject);
  $('newproj-name').addEventListener('blur', function () { $('newproj').hidden = true; });
  $('newproj-name').addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { $('newproj').hidden = true; }
  });
  $('proj-picker').addEventListener('change', function () {
    S.project = this.value;
    renderJobs();
    renderCrumb();
  });

  // El inspector arranca abierto: es un panel del programa, no un extra. Se
  // puede plegar desde la cabecera para dejar el monitor a ancho completo.
  $('btn-rail').addEventListener('click', function () {
    var hid = document.body.classList.toggle('no-rail');
    this.setAttribute('aria-expanded', hid ? 'false' : 'true');
    renderTimeline();
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

  // El playhead sólo corre mientras el vídeo corre: nada de rAF en vacío.
  Player.video.addEventListener('play', startPlayhead);
  Player.video.addEventListener('seeked', function () {
    var j = selJob();
    if (j) playhead(j.lead_seconds || 0, 0);
  });
  Player.video.addEventListener('timeupdate', function () {
    var j = selJob();
    if (j && Player.video.paused) playhead(j.lead_seconds || 0, 0);
  });
  window.addEventListener('resize', function () { if (S.view === 'spot') renderTimeline(); });

  bindKeys();
  renderJobs();
  loadHealth();
  loadProjects();
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
