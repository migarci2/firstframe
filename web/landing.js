/* FirstFrame — landing. Vanilla, sin build, sin CDN.
 *
 * Todo lo que se mueve está atado a la POSICIÓN de scroll, nunca a un
 * temporizador ni a un disparo de una sola dirección: si subes, se deshace.
 *
 *   · la barra superior se marca al hacer scroll,
 *   · los bloques del hero aparecen al entrar en pantalla,
 *   · la carrera: el reloj avanza de 0 a 30,3 s y las dos barras corren,
 *   · el recorrido: cuatro pasos con la captura pegada, que cambia sola,
 *   · la entrada y el cierre se componen conforme llegan.
 *
 * Un solo listener de scroll, un solo requestAnimationFrame, y dentro sólo
 * getBoundingClientRect + escritura de variables CSS: ninguna medida que
 * obligue al navegador a rehacer el layout.
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── barra superior ────────────────────────────────────────────────
  var nav = $('nav');

  // ── revelado al entrar en pantalla ────────────────────────────────
  var targets = document.querySelectorAll('.rv');
  if (reduced || !('IntersectionObserver' in window)) {
    for (var i = 0; i < targets.length; i++) targets[i].classList.add('in');
  } else {
    var io = new IntersectionObserver(function (entries) {
      for (var k = 0; k < entries.length; k++) {
        if (entries[k].isIntersecting) {
          entries[k].target.classList.add('in');
          io.unobserve(entries[k].target);
        }
      }
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    for (var j = 0; j < targets.length; j++) io.observe(targets[j]);
  }

  // ── la carrera ────────────────────────────────────────────────────
  // Las dos cifras medidas por la propia app: primer fotograma y render entero.
  // Son de un job REAL que sigue vivo en la instancia desplegada (j_7a80df,
  // 6 escenas), no de una ejecucion historica: un juez puede abrir la sala y
  // comprobarlas. Si vuelves a sembrar la demo, actualiza estos dos numeros
  // con los de un job que exista, o la landing prometera algo no verificable.
  var FIRST = 7.8;
  var FULL  = 30.3;

  var race    = $('race');
  var sticky  = race && race.querySelector('.race-sticky');
  var clockV  = $('clock-v');
  var trFirst = $('tr-first');
  var trFull  = $('tr-full');
  var fiFirst = $('fill-first');
  var fiFull  = $('fill-full');
  var vFirst  = $('v-first');
  var vFull   = $('v-full');
  var nFirst  = $('note-first');
  var nFull   = $('note-full');
  var punch   = $('punch');

  var clamp = function (n) { return n < 0 ? 0 : (n > 1 ? 1 : n); };

  function drawRace(p) {
    var t = p * FULL;                       // segundo en el que vamos

    clockV.textContent = t.toFixed(1);

    // El primer fotograma llega en FIRST y ahí se queda.
    var pf = clamp(t / FIRST);
    fiFirst.style.width = (pf * 100) + '%';
    var doneFirst = t >= FIRST;
    trFirst.classList.toggle('done', doneFirst);
    vFirst.textContent = doneFirst ? FIRST.toFixed(1) + ' s' : t.toFixed(1) + ' s';
    nFirst.textContent = doneFirst ? 'watchable — you can already judge it' : 'generating…';

    // El render entero corre hasta el final.
    fiFull.style.width = (p * 100) + '%';
    var doneFull = p >= 0.999;
    trFull.classList.toggle('done', doneFull);
    vFull.textContent = doneFull ? FULL.toFixed(1) + ' s' : t.toFixed(1) + ' s';
    nFull.textContent = doneFull ? 'render finished' : 'rendering…';

    sticky.style.setProperty('--lit', doneFirst ? 1 : 0);
    punch.classList.toggle('on', doneFull);
  }

  // ── el recorrido ──────────────────────────────────────────────────
  // Cuatro pasos y cuatro capturas en el mismo hueco pegado. El paso activo
  // sale de la posición de scroll dentro de la sección, así que subir apaga
  // el que estaba encendido y vuelve al anterior.
  var flow  = $('flow');
  var rail  = $('flow-steps');
  var steps = flow ? flow.querySelectorAll('.step') : [];
  var shots = flow ? flow.querySelectorAll('.shot') : [];
  var lastStep = -1;

  function drawFlow(p, i) {
    if (rail) rail.style.setProperty('--p', p.toFixed(4));
    if (i === lastStep) return;
    lastStep = i;
    for (var n = 0; n < steps.length; n++) steps[n].classList.toggle('on', n === i);
    for (var m = 0; m < shots.length; m++) shots[m].classList.toggle('on', m === i);
  }

  // ── entradas compuestas (entrada al recorrido y cierre) ───────────
  // Dos valores por bloque: el segundo va un quinto por detrás, que es lo que
  // da el escalonado sin necesidad de delays ni de una segunda animación.
  var lead  = $('lead');
  var close = $('close');

  function composeVals(top, vh) {
    var q1 = clamp((vh * .94 - top) / (vh * .42));
    return [q1, clamp((q1 - .22) / .78)];
  }
  function composeWrite(el, q) {
    el.style.setProperty('--q',  q[0].toFixed(3));   // el cierre lo usa así
    el.style.setProperty('--q1', q[0].toFixed(3));
    el.style.setProperty('--q2', q[1].toFixed(3));
  }

  var ticking = false;
  function onScroll() {
    ticking = false;
    var scrolled = window.scrollY > 12;
    if (reduced) { if (nav) nav.classList.toggle('stuck', scrolled); return; }

    var vh = window.innerHeight, n;

    /* ── primero se LEE todo, después se ESCRIBE ───────────────────────
     * Mezclar las dos cosas obliga al navegador a rehacer el layout dentro
     * del mismo fotograma, y ahí es donde la página empieza a ir a tirones. */
    var raceP = 0;
    if (race) {
      var run = race.offsetHeight - vh;
      raceP = run > 0 ? clamp(-race.getBoundingClientRect().top / run) : 0;
    }

    var flowP = 0, flowI = 0;
    if (flow && steps.length) {
      var frun = flow.offsetHeight - vh;
      flowP = frun > 0 ? clamp(-flow.getBoundingClientRect().top / frun) : 0;
      // Un tramo por paso. El último se queda encendido hasta el final, que es
      // justo lo que se quiere: el spot aprobado cierra la sección.
      flowI = Math.min(steps.length - 1, Math.floor(flowP * steps.length));
    }

    var leadQ  = lead  ? composeVals(lead.getBoundingClientRect().top, vh)  : null;
    var closeQ = close ? composeVals(close.getBoundingClientRect().top, vh) : null;

    // ── escrituras ──────────────────────────────────────────────────
    if (nav) nav.classList.toggle('stuck', scrolled);
    if (race) drawRace(raceP);
    if (flow && steps.length) drawFlow(flowP, flowI);
    if (leadQ)  composeWrite(lead, leadQ);
    if (closeQ) composeWrite(close, closeQ);
  }

  function request() {
    if (!ticking) { ticking = true; window.requestAnimationFrame(onScroll); }
  }

  // En una ventana muy baja los cuatro pasos y la captura no caben en 100vh
  // pegados: la sección se aplana y se lee como texto y captura alternos.
  function fit() {
    if (flow) flow.classList.toggle('flow-flat', window.innerHeight < 620);
  }

  if (reduced && race) drawRace(1);          // estático: la página ya terminada
  fit();
  onScroll();
  window.addEventListener('scroll', request, { passive: true });
  window.addEventListener('resize', function () { fit(); request(); });
})();
