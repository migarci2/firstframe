/**
 * El montaje del video de la demo: clips de pantalla + locucion + rotulos.
 *
 * Espera los ficheros en `ad/public/`:
 *     clips/01-problem.mp4 … 06-architecture.mp4    (grabaciones de pantalla)
 *     vo/01.mp3 … 06.mp3                            (locucion de ElevenLabs)
 *
 * El prefijo numerico es lo unico que importa; el resto del nombre da igual.
 * Correr `node src/probe.mjs` despues de copiar ficheros: mide con ffprobe y
 * reescribe `src/timeline.json`, que es de donde sale la duracion de cada tramo.
 * Remotion necesita saberla ANTES de renderizar — en el render no hay reloj.
 *
 * Un tramo sin clip no rompe el montaje: sale una tarjeta con su titulo, asi se
 * puede montar por partes e ir sustituyendo segun se graba.
 */
import React from 'react';
import {
  AbsoluteFill,
  Audio,
  interpolate,
  OffthreadVideo,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {campaign as C} from './campaign';
import timeline from './timeline.json';
import music from './music.json';

const T = C.theme;

type Section = {
  id: number;
  title: string;
  clip: string | null;
  vo: string | null;
  seconds: number;
};

/** Rotulo inferior: aparece al entrar en el tramo y se va solo. */
const Lower: React.FC<{title: string; secs: number}> = ({title, secs}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const inA = interpolate(f, [0, 8], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const out = interpolate(f, [secs * fps - 14, secs * fps - 6], [1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const o = Math.min(inA, out);
  return (
    <div
      style={{
        position: 'absolute',
        left: 64,
        bottom: 56,
        opacity: o,
        transform: `translateY(${(1 - inA) * 10}px)`,
        fontFamily: T.mono,
        fontSize: 26,
        letterSpacing: '.12em',
        textTransform: 'uppercase',
        color: T.ink,
        background: 'rgba(8,9,10,.72)',
        padding: '14px 22px',
        borderLeft: `3px solid ${T.accent}`,
      }}
    >
      {title}
    </div>
  );
};

/** Tarjeta para un tramo que todavia no tiene grabacion. */
const Missing: React.FC<{s: Section}> = ({s}) => (
  <AbsoluteFill
    style={{
      backgroundColor: T.bg,
      color: T.inkFaint,
      alignItems: 'center',
      justifyContent: 'center',
      fontFamily: T.mono,
      fontSize: 34,
      letterSpacing: '.1em',
      textTransform: 'uppercase',
    }}
  >
    {s.id}. {s.title} — sin clip
  </AbsoluteFill>
);

/**
 * Cama musical bajo todo el montaje.
 *
 * Se agacha (ducking) mientras hay locucion: la voz manda siempre. Los tramos
 * con voz salen de `timeline.json`, asi que el ducking se calcula solo — no hay
 * que marcar nada a mano. Y como Remotion renderiza por frame, el volumen es
 * una funcion del frame, no una automatizacion de un editor.
 */
const Music: React.FC<{sections: Section[]}> = ({sections}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  if (!music.file) return null;

  const t = f / fps;
  let at = 0;
  let underVoice = false;
  for (const s of sections) {
    if (s.vo && t >= at && t < at + s.seconds) underVoice = true;
    at += s.seconds;
  }

  const total = sections.reduce((a, s) => a + s.seconds, 0);
  // -18 dB bajo la voz es la regla; en los huecos sube, pero nunca al frente.
  const base = underVoice ? music.duckedVolume : music.volume;
  const fadeIn = interpolate(t, [0, 1.5], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const fadeOut = interpolate(t, [total - 2.5, total], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  return (
    <Audio
      src={staticFile(`music/${music.file}`)}
      volume={base * fadeIn * fadeOut}
      startFrom={Math.round(music.startSeconds * fps)}
    />
  );
};

export const Demo: React.FC = () => {
  const {fps} = useVideoConfig();
  const secs = timeline as Section[];
  let at = 0;

  return (
    <AbsoluteFill style={{backgroundColor: T.bg}}>
      <Music sections={secs} />
      {secs.map((s) => {
        const from = Math.round(at * fps);
        const dur = Math.max(1, Math.round(s.seconds * fps));
        at += s.seconds;
        return (
          <Sequence key={s.id} from={from} durationInFrames={dur}>
            {s.clip ? (
              <OffthreadVideo
                src={staticFile(`clips/${s.clip}`)}
                style={{width: '100%', height: '100%', objectFit: 'contain'}}
                // El audio del clip se silencia: manda la locucion. Si alguna
                // grabacion lleva sonido que quieras conservar, quita esta linea
                // en ese tramo y baja la voz en su lugar.
                muted
              />
            ) : (
              <Missing s={s} />
            )}
            {s.vo ? <Audio src={staticFile(`vo/${s.vo}`)} /> : null}
            <Lower title={s.title} secs={s.seconds} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

/** Duracion total, para que Root.tsx no tenga que recalcularla. */
export const demoDurationInFrames = (fps: number) =>
  Math.max(
    1,
    Math.round((timeline as Section[]).reduce((a, s) => a + s.seconds, 0) * fps),
  );
