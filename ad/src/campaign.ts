/**
 * Toda la campaña vive aquí: copy, cifras, tema y formatos.
 *
 * Las dos cifras NO son de marketing. Salen de un job real que sigue vivo en la
 * instancia desplegada (6 escenas): un juez puede abrir la sala y comprobarlas.
 * Son las mismas que anuncia la landing, a propósito — si un número aparece en
 * dos sitios con dos valores, deja de ser evidencia y pasa a ser adorno.
 */
export const campaign = {
  product: 'FirstFrame',
  url: 'firstframe.migarci2.dev',

  proof: {
    firstFrameSeconds: 9.3,
    fullRenderSeconds: 65.7,
    get ratio() {
      return this.fullRenderSeconds / this.firstFrameSeconds; // 7.1x
    },
  },

  copy: {
    hook: 'Why wait a minute',
    hookLine2: 'to reject a bad take?',
    problem: 'A generative render finishes long after you knew it was wrong.',
    reveal: 'See frame one while the rest is still rendering.',
    demo: 'Reject at second three. The scene re-runs in place.',
    keep: 'Approved work is sealed under Object Lock for 30 days.',
    cta: 'firstframe.migarci2.dev',
  },

  // El sistema de la landing, para que el anuncio y la página sean el mismo producto.
  theme: {
    bg: '#08090a',
    surface: '#0c0d0f',
    ink: '#f4f4f5',
    inkDim: '#9ba1a8',
    inkFaint: '#6b7178',
    accent: '#ff6b5e',
    hairline: 'rgba(255,255,255,.09)',
    radius: 14,
    font: 'Inter, system-ui, sans-serif',
    mono: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  },

  formats: [
    {id: 'landscape', width: 1920, height: 1080},
    {id: 'vertical', width: 1080, height: 1920},
    {id: 'square', width: 1080, height: 1080},
  ],

  fps: 30,
  durationSeconds: 15,
} as const;

export type Campaign = typeof campaign;
