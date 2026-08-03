/* FFEditor — el TIMELINE deja de ser un dibujo y pasa a ser un editor.
 *
 * Tres cosas que antes no se podían hacer:
 *   1. Reordenar escenas arrastrando el clip (SortableJS, MIT, vendorizado).
 *   2. Recortar la duración de un clip arrastrando su borde derecho o izquierdo.
 *   3. Silenciar/saltar un clip sin borrarlo (toggle enable).
 *
 * El estado editable vive en una EDL (edit decision list): una lista de
 * entradas {n, in, out, enabled} en el ORDEN de montaje. La EDL nunca pisa las
 * escenas — el pipeline sigue mandando sobre `scenes`, y la EDL dice cómo se
 * montan. Así una escena que se regenera no pierde el corte que ya le hiciste.
 *
 * MONTAJE
 *   FFEditor.mount(elemento, {
 *     jobId:   'j_abc',            // opcional; sin él no persiste, solo edita en memoria
 *     scenes:  [{n, title, status, seconds, start}],
 *     edl:     [...],              // opcional, si ya la tienes cargada
 *     apiBase: '',                 // prefijo de la API (por defecto, mismo origen)
 *     persist: true,               // false = no llama al backend nunca
 *     onChange:  function (edl, meta) {},   // cada vez que el usuario edita
 *     onSeek:    function (scene, tSec) {}, // click en un clip -> mover el vídeo
 *   })  ->  handle
 *
 * El handle expone:
 *   handle.setScenes(scenes)   // el pipeline trajo escenas nuevas; conserva la EDL
 *   handle.setPlayhead(tSec)   // pinta el playhead en tiempo de MONTAJE
 *   handle.getEDL()            // la EDL actual
 *   handle.duration()          // duración montada, en segundos
 *   handle.reset()             // vuelve al orden y duraciones originales
 *   handle.destroy()
 */
(function (global) {
  'use strict';

  var NOMINAL_SEC = 6;      // lo que asumimos que durará una escena que aún no existe
  var MIN_CLIP    = 0.4;    // por debajo de esto un corte deja de ser un corte
  var PX_EPS      = 3;      // umbral para distinguir un click de un arrastre

  /* ───────────────────────────── utilidades ───────────────────────────── */

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function fmtTC(sec) {
    sec = Math.max(0, sec || 0);
    var m = Math.floor(sec / 60), s = sec - m * 60;
    return m + ':' + (s < 10 ? '0' : '') + (Math.round(s * 10) / 10).toFixed(1).replace(/\.0$/, '');
  }

  /* Duración de origen de una escena. Las que aún no ha renderizado el pipeline
   * no traen `seconds`: se les asigna la nominal para que ocupen sitio en la
   * pista y se puedan reordenar antes de existir. */
  function srcSeconds(scene) {
    if (!scene) return NOMINAL_SEC;
    return (scene.seconds != null && scene.seconds > 0) ? scene.seconds : NOMINAL_SEC;
  }

  /* ───────────────────────────────── EDL ───────────────────────────────── */

  /* out === null significa "hasta el final de la escena, dure lo que dure".
   * Es la diferencia entre un clip que no has tocado y uno que has cortado
   * justo donde ahora acaba: el primero crece cuando el pipeline entrega la
   * escena de verdad, el segundo mantiene tu corte. */
  function outOf(e, s) {
    return e.out == null ? srcSeconds(s) : e.out;
  }

  /* Una EDL virgen: las escenas en su orden natural, enteras y activas. */
  function freshEDL(scenes) {
    return (scenes || []).map(function (s) {
      return { n: s.n, in: 0, out: null, enabled: true };
    });
  }

  /* Reconcilia una EDL con la lista de escenas viva:
   *   - conserva orden y cortes de lo que ya estaba
   *   - añade al final lo que ha aparecido
   *   - tira lo que ya no existe
   *   - reajusta los cortes si la escena cambió de duración al renderizarse
   * Esto es lo que permite editar MIENTRAS el pipeline sigue trabajando. */
  function reconcile(edl, scenes) {
    var byN = {};
    (scenes || []).forEach(function (s) { byN[s.n] = s; });

    var seen = {}, out = [];
    (edl || []).forEach(function (e) {
      var s = byN[e.n];
      if (!s || seen[e.n]) return;
      seen[e.n] = 1;
      var dur = srcSeconds(s);
      var i = clamp(e.in == null ? 0 : e.in, 0, Math.max(0, dur - MIN_CLIP));
      var o = e.out == null ? null : clamp(e.out, i + MIN_CLIP, dur);
      out.push({ n: e.n, in: i, out: o, enabled: e.enabled !== false });
    });

    (scenes || []).forEach(function (s) {
      if (seen[s.n]) return;
      out.push({ n: s.n, in: 0, out: null, enabled: true });
    });
    return out;
  }

  function edlDuration(edl, map) {
    var d = 0;
    edl.forEach(function (e) {
      if (!e.enabled) return;
      d += Math.max(0, outOf(e, map[e.n]) - e.in);
    });
    return d;
  }

  /* ────────────────────────────── el editor ────────────────────────────── */

  function mount(host, opts) {
    if (!host) throw new Error('FFEditor.mount: falta el elemento anfitrión');
    opts = opts || {};

    var state = {
      jobId:   opts.jobId || null,
      apiBase: opts.apiBase || '',
      persist: opts.persist !== false && !!opts.jobId,
      scenes:  (opts.scenes || []).slice(),
      edl:     null,
      sel:     null,      // n de la escena seleccionada
      dirty:   false,
      saving:  false,
      sortable: null,
      dragging: false,
      playT:   null
    };
    state.edl = opts.edl && opts.edl.length
      ? reconcile(opts.edl, state.scenes)
      : freshEDL(state.scenes);

    function byN() {
      var m = {};
      state.scenes.forEach(function (s) { m[s.n] = s; });
      return m;
    }

    /* ── esqueleto ── */
    host.classList.add('ffed');
    clear(host);

    var bar     = el('div', 'ffed-bar');
    var status  = el('span', 'ffed-status');
    var spacer  = el('span', 'ffed-grow');
    var btnReset = el('button', 'ffed-btn', 'Reset');
    btnReset.type = 'button';
    btnReset.title = 'Volver al orden y duraciones originales';
    var btnSave = el('button', 'ffed-btn ffed-btn-key', 'Apply cut');
    btnSave.type = 'button';
    btnSave.title = 'Guardar el montaje — el render final usa esta EDL';
    bar.appendChild(status); bar.appendChild(spacer);
    bar.appendChild(btnReset); bar.appendChild(btnSave);

    var stage  = el('div', 'ffed-stage');
    var ruler  = el('div', 'ffed-ruler');
    var track  = el('div', 'ffed-track');
    var head   = el('div', 'ffed-head');
    head.hidden = true;
    stage.appendChild(ruler); stage.appendChild(track); stage.appendChild(head);

    var hint = el('div', 'ffed-hint');
    hint.appendChild(el('span', null, 'Drag a clip to reorder'));
    hint.appendChild(el('span', null, 'Drag its edges to trim'));
    hint.appendChild(el('span', null, 'Click the dot to skip a scene'));

    host.appendChild(bar); host.appendChild(stage); host.appendChild(hint);

    /* ── pintado ── */
    function render() {
      // Un re-render en medio de un arrastre le arranca a Sortable el nodo que
      // está moviendo. Se aplaza hasta soltar.
      if (state.dragging) return;

      clear(track); clear(ruler);
      var map = byN();
      var total = edlDuration(state.edl, map);

      if (!state.edl.length) {
        track.appendChild(el('div', 'ffed-empty', 'no scenes yet'));
        status.textContent = '';
        head.hidden = true;
        return;
      }

      var active = state.edl.filter(function (e) { return e.enabled; }).length;
      status.textContent = active + '/' + state.edl.length + ' clips · ' + fmtTC(total)
        + (state.dirty ? ' · unsaved' : '');
      status.classList.toggle('is-dirty', state.dirty);

      state.edl.forEach(function (e, idx) {
        var s = map[e.n] || { n: e.n, status: 'pending' };
        var eOut = outOf(e, s);
        var len = Math.max(MIN_CLIP, eOut - e.in);
        var srcLen = srcSeconds(s);
        var trimmed = (e.in > 0.01) || (srcLen - eOut > 0.01);

        var clip = el('div', 'ffed-clip s-' + (s.status || 'pending'));
        clip.dataset.n = String(e.n);
        clip.tabIndex = 0;
        clip.setAttribute('role', 'listitem');
        // El ancho es proporcional a la duración YA RECORTADA: al arrastrar el
        // borde el clip encoge de verdad, no es un adorno. Una escena saltada
        // no ocupa tiempo, pero sigue necesitando sitio para volver a entrar.
        clip.style.flex = e.enabled ? (len + ' 1 0') : '0 0 46px';
        if (!e.enabled) clip.classList.add('is-off');
        if (trimmed) clip.classList.add('is-trimmed');
        if (state.sel === e.n) clip.classList.add('is-sel');
        clip.title = 'Scene ' + e.n + (s.title ? ' — ' + s.title : '')
          + '\n' + fmtTC(e.in) + ' → ' + fmtTC(eOut) + ' de ' + fmtTC(srcLen);

        var body = el('div', 'ffed-body');
        var top  = el('div', 'ffed-top');
        var dot  = el('button', 'ffed-dot');
        dot.type = 'button';
        dot.setAttribute('aria-label', (e.enabled ? 'Skip' : 'Include') + ' scene ' + e.n);
        dot.title = e.enabled ? 'Skip this scene in the cut' : 'Put this scene back in the cut';
        dot.addEventListener('click', function (ev) {
          ev.stopPropagation();
          e.enabled = !e.enabled;
          touch('toggle', e.n);
        });
        top.appendChild(dot);
        top.appendChild(el('span', 'ffed-name', (idx + 1) + '. ' + (s.title || 'Scene ' + e.n)));

        var foot = el('div', 'ffed-foot');
        foot.appendChild(el('span', 'ffed-len', fmtTC(len)));
        if (trimmed) foot.appendChild(el('span', 'ffed-badge', 'TRIM'));
        else if (!e.enabled) foot.appendChild(el('span', 'ffed-badge', 'OFF'));
        else if (s.status && s.status !== 'ready') foot.appendChild(el('span', 'ffed-badge', s.status));

        body.appendChild(top); body.appendChild(foot);
        clip.appendChild(body);

        // Asas de recorte. Solo tienen sentido si la escena existe de verdad:
        // recortar una escena que aún no se ha renderizado sería inventar.
        if (s.seconds != null && s.seconds > 0) {
          clip.appendChild(handle(clip, e, s, 'l'));
          clip.appendChild(handle(clip, e, s, 'r'));
        }

        clip.addEventListener('click', function () {
          state.sel = e.n;
          render();
          if (opts.onSeek) opts.onSeek(s, timelineStartOf(e.n));
        });
        clip.addEventListener('keydown', function (ev) {
          // Reordenar con teclado: arrastrar no puede ser la única forma.
          var d = ev.key === 'ArrowLeft' ? -1 : ev.key === 'ArrowRight' ? 1 : 0;
          if (!d || !ev.altKey) return;
          ev.preventDefault();
          var i = state.edl.indexOf(e), j = i + d;
          if (j < 0 || j >= state.edl.length) return;
          state.edl.splice(i, 1);
          state.edl.splice(j, 0, e);
          state.sel = e.n;
          touch('reorder', e.n);
          var next = track.querySelector('.ffed-clip[data-n="' + e.n + '"]');
          if (next) next.focus();
        });

        track.appendChild(clip);
      });

      drawRuler(total);
      drawHead(total);
    }

    function drawRuler(total) {
      if (!total) return;
      var step = total > 24 ? 5 : total > 8 ? 2 : 1;
      for (var t = 0; t <= Math.floor(total); t++) {
        var pct = t / total * 100;
        var lab = t % step === 0 && pct < 92;
        var tick = el('div', 'ffed-tick' + (lab ? ' lab' : ''));
        tick.style.left = pct + '%';
        if (lab) tick.appendChild(el('span', null, fmtTC(t)));
        ruler.appendChild(tick);
      }
    }

    /* Dónde empieza una escena en el MONTAJE (no en el original). */
    function timelineStartOf(n) {
      var map = byN(), t = 0;
      for (var i = 0; i < state.edl.length; i++) {
        var e = state.edl[i];
        if (e.n === n) return t;
        if (e.enabled) t += Math.max(0, outOf(e, map[e.n]) - e.in);
      }
      return t;
    }

    function drawHead(total) {
      if (state.playT == null || !total) { head.hidden = true; return; }
      var map = byN(), t = 0, x = null;
      var clips = track.children;
      for (var i = 0; i < state.edl.length && i < clips.length; i++) {
        var e = state.edl[i];
        if (!e.enabled) continue;
        var len = Math.max(0, outOf(e, map[e.n]) - e.in);
        if (state.playT >= t && state.playT < t + len && len > 0) {
          x = clips[i].offsetLeft + (state.playT - t) / len * clips[i].offsetWidth;
          break;
        }
        t += len;
      }
      if (x == null) { head.hidden = true; return; }
      head.hidden = false;
      head.style.left = Math.round(x) + 'px';
    }

    /* ── asas de recorte (pointer events; nada de HTML5 drag) ──
     * HTML5 drag-and-drop no da posición fluida ni funciona en táctil, y aquí
     * hace falta ver el clip encoger en tiempo real. */
    function handle(clip, e, s, side) {
      var h = el('div', 'ffed-h ffed-h-' + side);
      h.setAttribute('aria-hidden', 'true');
      h.addEventListener('pointerdown', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var trackW = track.getBoundingClientRect().width;
        var total  = edlDuration(state.edl, byN());
        if (!trackW || !total) return;
        // px → segundos con la escala que se está viendo ahora mismo.
        var secPerPx = total / trackW;
        var x0 = ev.clientX;
        // En cuanto tocas el borde derecho, "hasta el final" se convierte en un
        // corte concreto: eso es exactamente lo que estás decidiendo.
        var srcLen = srcSeconds(s);
        var in0 = e.in, out0 = outOf(e, s);
        var moved = false;

        // La captura es un extra: si el navegador la rechaza seguimos oyendo en
        // window, así que el arrastre no se rompe por salirse del asa.
        try { h.setPointerCapture(ev.pointerId); } catch (_) {}
        host.classList.add('ffed-trimming');

        function move(mv) {
          var d = (mv.clientX - x0) * secPerPx;
          if (Math.abs(mv.clientX - x0) > PX_EPS) moved = true;
          if (side === 'l') e.in  = clamp(in0 + d, 0, out0 - MIN_CLIP);
          else              e.out = clamp(out0 + d, in0 + MIN_CLIP, srcLen);
          // Repintado barato: solo el ancho y la etiqueta del clip que se mueve.
          var cur = outOf(e, s);
          var len = Math.max(MIN_CLIP, cur - e.in);
          clip.style.flex = len + ' 1 0';
          var lenEl = clip.querySelector('.ffed-len');
          if (lenEl) lenEl.textContent = fmtTC(len);
          status.textContent = 'trim scene ' + e.n + ' · ' + fmtTC(e.in) + ' → ' + fmtTC(cur);
        }
        function up() {
          global.removeEventListener('pointermove', move);
          global.removeEventListener('pointerup', up);
          global.removeEventListener('pointercancel', up);
          host.classList.remove('ffed-trimming');
          if (moved) touch('trim', e.n);
          else render();
        }
        global.addEventListener('pointermove', move);
        global.addEventListener('pointerup', up);
        global.addEventListener('pointercancel', up);
      });
      return h;
    }

    /* ── cambios ── */
    function touch(kind, n) {
      state.dirty = true;
      render();
      if (opts.onChange) {
        opts.onChange(state.edl.slice(), {
          kind: kind, scene: n, duration: edlDuration(state.edl, byN())
        });
      }
    }

    /* ── reordenar: SortableJS (MIT) ──
     * Se le da el <div class="ffed-track"> que ya existe; no reemplaza el DOM
     * ni exige ser dueño del panel. Justo lo que hacía falta aquí. */
    function initSortable() {
      if (!global.Sortable) {
        hint.appendChild(el('span', 'ffed-warn', 'Sortable.min.js no cargado — reorder con Alt+←/→'));
        return;
      }
      state.sortable = new global.Sortable(track, {
        animation: 150,
        direction: 'horizontal',
        draggable: '.ffed-clip',
        filter: '.ffed-h, .ffed-dot',       // las asas y el punto no arrastran el clip
        preventOnFilter: false,
        ghostClass: 'ffed-ghost',
        chosenClass: 'ffed-chosen',
        dragClass: 'ffed-drag',
        forceFallback: true,                // el fallback pinta un clon fiel y evita
                                            // el drag-image borroso de HTML5
        fallbackTolerance: 3,
        onStart: function () { state.dragging = true; host.classList.add('ffed-reordering'); },
        onEnd: function (ev) {
          state.dragging = false;
          host.classList.remove('ffed-reordering');
          var from = ev.oldIndex, to = ev.newIndex;
          if (from == null || to == null || from === to) { render(); return; }
          var moved = state.edl.splice(from, 1)[0];
          state.edl.splice(to, 0, moved);
          state.sel = moved.n;
          touch('reorder', moved.n);
        }
      });
    }

    /* ── persistencia ── */
    function url(p) { return state.apiBase + '/api/jobs/' + encodeURIComponent(state.jobId) + p; }

    function save() {
      if (!state.persist) {
        // En la demo no hay backend: se confirma en local y ya.
        state.dirty = false; render();
        return Promise.resolve({ ok: true, local: true });
      }
      if (state.saving) return Promise.resolve({ ok: false, busy: true });
      state.saving = true;
      btnSave.disabled = true;
      btnSave.textContent = 'Saving…';
      return fetch(url('/edl'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edl: state.edl })
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (j) {
        state.dirty = false;
        if (j && j.edl) state.edl = reconcile(j.edl, state.scenes);
        return { ok: true, data: j };
      }).catch(function (err) {
        status.textContent = 'save failed · ' + err.message;
        return { ok: false, error: err };
      }).then(function (res) {
        state.saving = false;
        btnSave.disabled = false;
        btnSave.textContent = 'Apply cut';
        render();
        return res;
      });
    }

    btnSave.addEventListener('click', save);
    btnReset.addEventListener('click', function () {
      state.edl = freshEDL(state.scenes);
      state.sel = null;
      touch('reset', null);
    });

    initSortable();
    render();

    var onResize = function () { drawHead(edlDuration(state.edl, byN())); };
    global.addEventListener('resize', onResize);

    /* ── handle público ── */
    return {
      el: host,
      setScenes: function (scenes) {
        state.scenes = (scenes || []).slice();
        state.edl = reconcile(state.edl, state.scenes);
        render();
      },
      setEDL: function (edl) {
        state.edl = reconcile(edl || [], state.scenes);
        state.dirty = false;
        render();
      },
      getEDL: function () { return state.edl.slice(); },
      duration: function () { return edlDuration(state.edl, byN()); },
      setPlayhead: function (t) {
        state.playT = t;
        drawHead(edlDuration(state.edl, byN()));
      },
      isDirty: function () { return state.dirty; },
      save: save,
      reset: function () {
        state.edl = freshEDL(state.scenes);
        touch('reset', null);
      },
      destroy: function () {
        global.removeEventListener('resize', onResize);
        if (state.sortable) state.sortable.destroy();
        clear(host);
        host.classList.remove('ffed');
      }
    };
  }

  /* Carga la EDL guardada de un job. Devuelve [] si no hay ninguna todavía. */
  function load(jobId, apiBase) {
    return fetch((apiBase || '') + '/api/jobs/' + encodeURIComponent(jobId) + '/edl')
      .then(function (r) { return r.ok ? r.json() : { edl: [] }; })
      .then(function (j) { return (j && j.edl) || []; })
      .catch(function () { return []; });
  }

  global.FFEditor = {
    mount: mount,
    load: load,
    freshEDL: freshEDL,
    reconcile: reconcile,
    outOf: outOf,
    srcSeconds: srcSeconds,
    NOMINAL_SEC: NOMINAL_SEC,
    version: '1.0.0'
  };
})(window);
