/* FirstFrame — landing. Vanilla, sin build, sin CDN.
 *
 * Dos modelos de movimiento, a propósito distintos:
 *   · El portal va atado a la POSICIÓN de scroll, así que se cierra otra vez
 *     al subir. Nada de temporizadores.
 *   · Las entradas del resto de la página se disparan una vez y no se deshacen.
 *
 * Todo el movimiento vive bajo la clase .motion, que sólo se añade si el
 * usuario no ha pedido menos movimiento: el render estático ya es la página
 * terminada.
 */
(function () {
'use strict';

var $ = function (id) { return document.getElementById(id); };
var clamp01 = function (v) { return v < 0 ? 0 : v > 1 ? 1 : v; };
var lerp = function (a, b, t) { return a + (b - a) * t; };

var REDUCED = window.matchMedia &&
              window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ═════════════════════════ baraja ═════════════════════════ */

// Catálogo real: briefs y tiempos leídos de los manifest.json de runs/demo-…
var SPOTS = [
  { img: '/assets/cafe.jpg',    t: 'Café de especialidad',
    b: 'una bolsa de cafe de especialidad de tueste artesanal, sobre madera oscura',
    e: '3 escenas', ms: '43,7 s' },
  { img: '/assets/sneaker.jpg', t: 'Zapatilla de running',
    b: 'una zapatilla de running ligera de una marca nueva, sobre asfalto mojado',
    e: '3 escenas', ms: '2:14' },
  { img: '/assets/botella.jpg', t: 'Botella térmica',
    b: 'una botella termica de acero inoxidable de una marca nueva, sin logotipos, sobre asfalto mojado de noche',
    e: '3 escenas', ms: '44,3 s' },
  { img: '/assets/serum.jpg',   t: 'Serum facial',
    b: 'un frasco de serum facial de una marca DTC, sobre marmol blanco, luz de manana',
    e: '3 escenas', ms: '20,7 s' }
];

var deck = $('deck');
var dotsBox = $('dots');
var order = SPOTS.map(function (_, i) { return i; });
var cards = [];
var busy = false;

function buildDeck() {
  if (!deck) return;
  SPOTS.forEach(function (s) {
    var c = document.createElement('div');
    c.className = 'card';

    var im = document.createElement('img');
    im.src = s.img;
    im.alt = 'Fotograma generado: ' + s.t;
    im.draggable = false;
    c.appendChild(im);

    var m = document.createElement('div');
    m.className = 'meta';
    var h = document.createElement('h3'); h.textContent = s.t; m.appendChild(h);
    var p = document.createElement('p');  p.textContent = '“' + s.b + '”'; m.appendChild(p);
    var f = document.createElement('div');
    f.className = 'foot';
    f.appendChild(document.createTextNode(s.e));
    var b = document.createElement('b'); b.textContent = s.ms; f.appendChild(b);
    m.appendChild(f);
    c.appendChild(m);

    deck.appendChild(c);
    cards.push(c);
  });

  SPOTS.forEach(function () {
    dotsBox.appendChild(document.createElement('i'));
  });
  restack();
}

/* La pila se lee como algo físico: cada carta un poco desplazada, más pequeña
   y girada respecto a la de encima. */
function restack(skip) {
  order.forEach(function (idx, pos) {
    var c = cards[idx];
    if (c === skip) return;
    c.style.transition = 'transform .42s cubic-bezier(.2,.7,.3,1), opacity .3s';
    c.style.transform = 'translate3d(' + (pos * 11) + 'px,' + (pos * -7) + 'px,0) ' +
                        'scale(' + (1 - pos * 0.045) + ') rotate(' + (pos * -1.6) + 'deg)';
    c.style.zIndex = String(SPOTS.length - pos);
    c.style.opacity = pos > 2 ? '0' : '1';
  });
  Array.prototype.forEach.call(dotsBox.children, function (d, i) {
    d.classList.toggle('on', i === order[0]);
  });
}

function throwTop(dir) {
  if (busy || order.length < 2) return;
  busy = true;
  var top = cards[order[0]];
  var w = deck.getBoundingClientRect().width || 400;
  top.style.transition = 'transform .5s cubic-bezier(.3,.6,.3,1), opacity .5s';
  top.style.transform = 'translate3d(' + (dir * w * 1.15) + 'px,-60px,0) rotate(' + (dir * 22) + 'deg)';
  top.style.opacity = '0';
  order.push(order.shift());
  restack(top);
  setTimeout(function () {
    top.style.transition = 'none';
    restack();
    // Un frame sin transición para recolocarla al fondo sin que se vea volver.
    requestAnimationFrame(function () { busy = false; });
  }, 460);
}

function bindDrag() {
  if (!deck) return;
  var sx = 0, sy = 0, dragging = false, cur = null;

  deck.addEventListener('pointerdown', function (e) {
    if (busy) return;
    cur = cards[order[0]];
    dragging = true;
    sx = e.clientX; sy = e.clientY;
    cur.style.transition = 'none';
    deck.setPointerCapture(e.pointerId);
  });

  deck.addEventListener('pointermove', function (e) {
    if (!dragging || !cur) return;
    var dx = e.clientX - sx, dy = e.clientY - sy;
    cur.style.transform = 'translate3d(' + dx + 'px,' + dy + 'px,0) ' +
                          'rotate(' + (dx * 0.06) + 'deg) scale(1.02)';
  });

  var end = function (e) {
    if (!dragging || !cur) return;
    dragging = false;
    try { deck.releasePointerCapture(e.pointerId); } catch (err) {}
    var dx = e.clientX - sx;
    var w = deck.getBoundingClientRect().width || 400;
    if (Math.abs(dx) > w * 0.1) throwTop(dx > 0 ? 1 : -1);
    else restack();
    cur = null;
  };
  deck.addEventListener('pointerup', end);
  deck.addEventListener('pointercancel', end);

  // Sin ratón también se pasa: la baraja es foco y flechas.
  deck.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight') { e.preventDefault(); throwTop(1); }
    if (e.key === 'ArrowLeft')  { e.preventDefault(); throwTop(-1); }
  });
}

/* ═════════════════════════ portal ═════════════════════════ */

var portal = document.querySelector('.portal');
var els = {
  img: $('ph-img'), duo: $('ph-duo'),
  pl: $('p-l'), pr: $('p-r'),
  da: $('dot-a'), db: $('dot-b'),
  title: $('ptitle'), t1: $('t1'), t2: $('t2'), atmo: $('atmo'),
  orb: $('orb')
};

function drawPortal() {
  if (!portal || !els.title) return;
  var r = portal.getBoundingClientRect();
  var travel = portal.offsetHeight - window.innerHeight;
  var p = clamp01(travel > 0 ? (-r.top) / travel : 0);

  // Las hojas despejan el encuadre en el primer 72% del recorrido.
  var pOpen = clamp01(p / 0.72);
  els.pl.style.transform = 'translate3d(' + (-105 * pOpen) + '%,0,0)';
  els.pr.style.transform = 'translate3d(' + (105 * pOpen) + '%,0,0)';

  // La imagen se asienta de un ligero sobreescalado a 1.
  var pImg = clamp01(p / 0.85);
  els.img.style.transform = 'scale(' + lerp(1.12, 1, pImg) + ')';
  els.duo.style.opacity = String(lerp(0, 0.14, clamp01(p / 0.6)));
  // La atmósfera se disuelve y deja el fotograma. Ligado al scroll: reversible.
  if (els.atmo) els.atmo.style.opacity = String(1 - clamp01(p / 0.62));

  // Crece Y aprieta a la vez: es lo que hace que se lea como un título que se
  // abre y no como un zoom. Los extremos viajan media anchura hacia fuera.
  els.title.style.transform = 'scale(' + lerp(1, 1.28, p) + ')';
  els.title.style.letterSpacing = lerp(-0.02, -0.055, p).toFixed(4) + 'em';
  els.t1.style.transform = 'translate3d(' + (-50 * p) + '%,0,0)';
  els.t2.style.transform = 'translate3d(' + (50 * p) + '%,0,0)';

  // Los dos puntos salen hacia esquinas opuestas.
  var pDot = clamp01(p / 0.8);
  els.da.style.transform = 'translate3d(' + (-30 * pDot) + 'vw,' + (-22 * pDot) + 'vh,0)';
  els.db.style.transform = 'translate3d(' + (30 * pDot) + 'vw,' + (22 * pDot) + 'vh,0)';
}

function drawOrb() {
  if (!els.orb) return;
  var r = els.orb.getBoundingClientRect();
  var q = (window.innerHeight - r.top) / (window.innerHeight + r.height);
  els.orb.style.transform = 'translate3d(0,' + lerp(60, -60, clamp01(q)) +
                            'px,0) rotate(' + lerp(-8, 8, clamp01(q)) + 'deg)';
}

var ticking = false;
function onScroll() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(function () {
    drawPortal();
    drawOrb();
    ticking = false;
  });
}

/* ═════════════════════════ entradas ═════════════════════════ */

function bindReveals() {
  var items = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    Array.prototype.forEach.call(items, function (n) { n.classList.add('in'); });
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;      // una vez y no se deshace
      en.target.classList.add('in');
      io.unobserve(en.target);
    });
  }, { rootMargin: '0px 0px -12% 0px', threshold: 0.08 });
  Array.prototype.forEach.call(items, function (n) { io.observe(n); });
}

/* ═════════════════════════ arranque ═════════════════════════ */

buildDeck();
bindDrag();

if (!REDUCED) {
  document.documentElement.classList.add('motion');
  bindReveals();
  drawPortal();
  drawOrb();
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
}

// Sonda para comprobar que el hero viaja de verdad (se usa en QA).
window.__ffPortal = function () {
  return els.title ? els.title.getBoundingClientRect() : null;
};

})();
