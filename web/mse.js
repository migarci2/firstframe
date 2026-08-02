/* FirstFrame — reproductor HLS incremental sobre Media Source Extensions.
 *
 * Es el plan B de `web/app.js`: se usa cuando hls.js no está disponible o el
 * navegador no lo soporta, y antes de caer al `<video src>` nativo (que sólo
 * traga HLS en Safari).
 *
 * Qué hace: se engancha a una playlist EVENT que CRECE mientras el backend
 * sigue subiendo segmentos a B2. Sondea el .m3u8 cada POLL_MS, y por cada
 * segmento nuevo hace `SourceBuffer.appendBuffer()`. Arranca la reproducción
 * en cuanto hay START_SEGMENTS segmentos en el buffer, no cuando termina el
 * render — que es toda la tesis del producto.
 *
 * Modo `sequence` en el SourceBuffer: las escenas vienen de ejecuciones de
 * ffmpeg distintas, así que cada una reinicia sus timestamps (por eso el
 * assembler emite #EXT-X-DISCONTINUITY). En `sequence` el navegador encadena
 * cada append detrás del anterior y la discontinuidad deja de importar.
 *
 * Limitación honesta, medida en este Chrome (no leída en docs): con los
 * segmentos MPEG-TS que produce hoy el assembler,
 * `MediaSource.isTypeSupported('video/mp2t')` devuelve **true** pero el demuxer
 * aborta en la primera frontera de escena con
 * `CHUNK_DEMUXER_ERROR_APPEND_FAILED: Parsed buffers not in DTS sequence`
 * (probado con y sin `abort()` + `timestampOffset` en la discontinuidad).
 * Ésa es exactamente la razón por la que hls.js remuxa TS a fMP4, y por la que
 * hls.js es el motor primario de app.js.
 *
 * Con segmentos fMP4 (`.m4s`/`.mp4` + `#EXT-X-MAP`) —el plan B de encode del
 * §5 del plan— este player sí funciona en Chrome sin dependencias. Si el
 * append falla, `app.js` lo detecta por el `error` del `<video>` y degrada al
 * player nativo con un mensaje visible: nunca se queda en negro sin explicar.
 */
(function (global) {
  'use strict';

  var POLL_MS = 1200;          // cada cuánto se re-pide la playlist viva
  var START_SEGMENTS = 2;      // arrancamos con 2 segmentos de colchón (§9.1 del plan)
  var MP4_MIME = 'video/mp4; codecs="avc1.4d401f,mp4a.40.2"';
  var TS_MIME  = 'video/mp2t; codecs="avc1.4d401f,mp4a.40.2"';

  // Fin del contenido ya bufferizado (0 si aún no hay nada).
  function bufferedEnd(sb) {
    try { return sb.buffered.length ? sb.buffered.end(sb.buffered.length - 1) : 0; }
    catch (e) { return 0; }
  }

  function hasMSE() {
    return typeof global.MediaSource !== 'undefined' &&
           typeof global.MediaSource.isTypeSupported === 'function';
  }

  /* ------------------------------------------------------------------ parser */
  // Devuelve { segments:[{uri,duration,disc}], init:string|null, ended:bool, target:number }
  function parsePlaylist(text, baseUrl) {
    var out = { segments: [], init: null, ended: false, target: 4 };
    var lines = text.split(/\r?\n/);
    var dur = 0, disc = false;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim();
      if (!ln) continue;
      if (ln.charAt(0) === '#') {
        if (ln.indexOf('#EXTINF:') === 0) {
          dur = parseFloat(ln.slice(8)) || 0;
        } else if (ln === '#EXT-X-DISCONTINUITY') {
          disc = true;
        } else if (ln.indexOf('#EXT-X-ENDLIST') === 0) {
          out.ended = true;
        } else if (ln.indexOf('#EXT-X-TARGETDURATION:') === 0) {
          out.target = parseFloat(ln.split(':')[1]) || 4;
        } else if (ln.indexOf('#EXT-X-MAP:') === 0) {
          var m = /URI="([^"]+)"/.exec(ln);
          if (m) out.init = resolve(m[1], baseUrl);
        }
        continue;
      }
      out.segments.push({ uri: resolve(ln, baseUrl), duration: dur, disc: disc });
      dur = 0; disc = false;
    }
    return out;
  }

  function resolve(uri, baseUrl) {
    try { return new URL(uri, baseUrl).href; } catch (e) { return uri; }
  }

  function mimeFor(playlist) {
    var first = playlist.segments.length ? playlist.segments[0].uri : (playlist.init || '');
    var path = first.split('?')[0];
    if (/\.(m4s|mp4|cmfv)$/i.test(path) || playlist.init) return MP4_MIME;
    if (/\.ts$/i.test(path)) return TS_MIME;
    return MP4_MIME;
  }

  /* ------------------------------------------------------------------ player */
  function MsePlayer(video) {
    this.video = video;
    this.listeners = {};
    this.destroyed = false;
    this._reset();
  }

  MsePlayer.prototype._reset = function () {
    this.ms = null;
    this.sb = null;
    this.queue = [];
    this.appending = false;
    this.seen = {};          // uri -> true (idempotencia entre sondeos)
    this.loaded = 0;
    this.ended = false;
    this.started = false;
    this.initDone = false;
    this.pollTimer = null;
    this.url = null;
  };

  MsePlayer.prototype.on = function (evt, cb) {
    (this.listeners[evt] = this.listeners[evt] || []).push(cb);
    return this;
  };

  MsePlayer.prototype.emit = function (evt, payload) {
    var ls = this.listeners[evt] || [];
    for (var i = 0; i < ls.length; i++) {
      try { ls[i](payload); } catch (e) { /* un listener roto no tumba el player */ }
    }
  };

  MsePlayer.prototype.load = function (url) {
    var self = this;
    this.stop();
    this.destroyed = false;
    this._reset();
    this.url = url;

    this.ms = new global.MediaSource();
    this.video.src = URL.createObjectURL(this.ms);
    this.ms.addEventListener('sourceopen', function onOpen() {
      self.ms.removeEventListener('sourceopen', onOpen);
      self._poll();
    });
  };

  MsePlayer.prototype._ensureBuffer = function (mime) {
    if (this.sb) return true;
    if (!global.MediaSource.isTypeSupported(mime)) {
      this.emit('error', { fatal: true, reason: 'MSE no soporta ' + mime });
      return false;
    }
    try {
      this.sb = this.ms.addSourceBuffer(mime);
    } catch (e) {
      this.emit('error', { fatal: true, reason: 'addSourceBuffer: ' + e.message });
      return false;
    }
    // Cada escena es un ffmpeg distinto con timestamps que reinician: en modo
    // 'sequence' el navegador encadena los appends y la discontinuidad no rompe.
    try { this.sb.mode = 'sequence'; } catch (e) { /* algunos navegadores lo fijan */ }
    var self = this;
    this.sb.addEventListener('updateend', function () { self._pump(); });
    this.sb.addEventListener('error', function () {
      self.emit('error', { fatal: false, reason: 'SourceBuffer error' });
    });
    return true;
  };

  MsePlayer.prototype._poll = function () {
    var self = this;
    if (this.destroyed) return;

    fetch(this.url, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('playlist HTTP ' + r.status);
        return r.text();
      })
      .then(function (text) {
        if (self.destroyed) return;
        var pl = parsePlaylist(text, new URL(self.url, global.location.href).href);
        if (!self._ensureBuffer(mimeFor(pl))) return;

        if (pl.init && !self.initDone) {
          self.initDone = true;
          self.queue.push({ url: pl.init, disc: false });
        }
        for (var i = 0; i < pl.segments.length; i++) {
          var seg = pl.segments[i];
          if (!self.seen[seg.uri]) {
            self.seen[seg.uri] = true;
            self.queue.push({ url: seg.uri, disc: !!seg.disc });
          }
        }
        self.ended = pl.ended;
        self._pump();

        if (!pl.ended) {
          self.pollTimer = setTimeout(function () { self._poll(); }, POLL_MS);
        }
      })
      .catch(function (err) {
        if (self.destroyed) return;
        self.emit('error', { fatal: false, reason: String(err.message || err) });
        // La playlist puede tardar unos segundos en existir: reintentamos.
        self.pollTimer = setTimeout(function () { self._poll(); }, POLL_MS);
      });
  };

  MsePlayer.prototype._pump = function () {
    var self = this;
    if (this.destroyed || !this.sb || this.appending || this.sb.updating) return;

    if (!this.queue.length) {
      if (this.ended && this.ms && this.ms.readyState === 'open') {
        try { this.ms.endOfStream(); } catch (e) { /* ya cerrado */ }
      }
      return;
    }

    var item = this.queue.shift();
    var url = item.url;
    this.appending = true;

    // Cada escena viene de un ffmpeg distinto y reinicia sus timestamps: sin
    // resetear el parser, Chrome aborta con
    // "Parsed buffers not in DTS sequence" en la frontera de escena.
    // abort() es justo eso: reset del estado del parser + nuevo grupo de frames.
    if (item.disc && this.sb && !this.sb.updating) {
      try { this.sb.abort(); } catch (e) { /* noop */ }
      try { this.sb.timestampOffset = bufferedEnd(this.sb); } catch (e) { /* noop */ }
    }

    fetch(url, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('segmento HTTP ' + r.status);
        return r.arrayBuffer();
      })
      .then(function (buf) {
        if (self.destroyed || !self.sb) return;
        try {
          self.sb.appendBuffer(new Uint8Array(buf));
        } catch (e) {
          self.emit('error', { fatal: false, reason: 'appendBuffer: ' + e.message });
        }
        self.appending = false;
        self.loaded++;
        self.emit('segment', { loaded: self.loaded, url: url });
        if (!self.started && self.loaded >= START_SEGMENTS) {
          self.started = true;
          self.emit('ready', { loaded: self.loaded });
        }
        // updateend continúa la cola; si no hubo append, seguimos aquí.
        if (self.sb && !self.sb.updating) self._pump();
      })
      .catch(function (err) {
        self.appending = false;
        if (self.destroyed) return;
        // Un segmento que aún no aterrizó en B2 se reintenta al final de la cola.
        self.emit('error', { fatal: false, reason: String(err.message || err) });
        setTimeout(function () { self._pump(); }, 400);
      });
  };

  MsePlayer.prototype.stop = function () {
    this.destroyed = true;
    if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
    if (this.ms && this.ms.readyState === 'open') {
      try { this.ms.endOfStream(); } catch (e) { /* noop */ }
    }
    this.ms = null; this.sb = null; this.queue = [];
  };

  MsePlayer.prototype.destroy = function () {
    this.stop();
    try { this.video.removeAttribute('src'); this.video.load(); } catch (e) { /* noop */ }
  };

  /* ------------------------------------------------------------------ API */
  global.FFMse = {
    /** ¿Podemos reproducir esta playlist con MSE en este navegador? */
    isSupported: function (playlistText) {
      if (!hasMSE()) return false;
      if (!playlistText) return global.MediaSource.isTypeSupported(MP4_MIME);
      var pl = parsePlaylist(playlistText, global.location.href);
      return global.MediaSource.isTypeSupported(mimeFor(pl));
    },
    create: function (video) { return new MsePlayer(video); },
    parsePlaylist: parsePlaylist,
    START_SEGMENTS: START_SEGMENTS
  };
})(window);
