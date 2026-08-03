/* FirstFrame — landing.
 *
 * Dos cosas y nada más: la barra superior se marca al hacer scroll y los bloques
 * aparecen al entrar en pantalla. Sin dependencias, sin build, sin CDN.
 */
(function () {
  'use strict';

  // ── barra superior ────────────────────────────────────────────────
  var nav = document.getElementById('nav');
  if (nav) {
    var onScroll = function () {
      nav.classList.toggle('stuck', window.scrollY > 12);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  // ── revelado al entrar en pantalla ────────────────────────────────
  var targets = document.querySelectorAll('.rv');
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (reduced || !('IntersectionObserver' in window)) {
    for (var i = 0; i < targets.length; i++) targets[i].classList.add('in');
    return;
  }

  var io = new IntersectionObserver(function (entries) {
    for (var i = 0; i < entries.length; i++) {
      if (entries[i].isIntersecting) {
        entries[i].target.classList.add('in');
        io.unobserve(entries[i].target);
      }
    }
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

  for (var j = 0; j < targets.length; j++) io.observe(targets[j]);
})();
