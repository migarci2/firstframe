/**
 * Anuncio de 15 s. Seis tramos, todos deterministas.
 *
 * REGLA: nada de CSS transitions, setTimeout ni requestAnimationFrame. Todo el
 * movimiento sale de `useCurrentFrame()`, porque Remotion renderiza fotograma a
 * fotograma y el reloj del navegador no existe durante el render.
 */
import React from 'react';
import {AbsoluteFill, interpolate, Sequence, useCurrentFrame, useVideoConfig} from 'remotion';
import {campaign as C} from './campaign';

const T = C.theme;

/**
 * Fade + desplazamiento de entrada, en función del frame local del Sequence.
 *
 * `dur` es corto a propósito: con 12 fotogramas y los retardos encadenados,
 * cada corte abría con medio segundo de negro absoluto — seis cortes, dos
 * segundos y medio de los quince en nada. Con 8 el corte respira sin morir.
 */
const useEnter = (delay = 0, dur = 8) => {
  const f = useCurrentFrame() - delay;
  const o = interpolate(f, [0, dur], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const y = interpolate(f, [0, dur], [14, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return {opacity: o, transform: `translateY(${y}px)`};
};

const Stage: React.FC<{children: React.ReactNode}> = ({children}) => (
  <AbsoluteFill
    style={{
      backgroundColor: T.bg,
      fontFamily: T.font,
      color: T.ink,
      alignItems: 'center',
      justifyContent: 'center',
      padding: '0 8%',
      textAlign: 'center',
    }}
  >
    {children}
  </AbsoluteFill>
);

/** Halo radial: se desvanece a transparente, así que no tiene arista que recortar. */
const Glow: React.FC<{intensity?: number}> = ({intensity = 0.32}) => {
  const f = useCurrentFrame();
  const breathe = 1 + 0.04 * Math.sin(f / 22);
  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(ellipse 46% 38% at 50% 46%, rgba(255,107,94,${intensity}), transparent 70%)`,
        transform: `scale(${breathe})`,
      }}
    />
  );
};

const Kicker: React.FC<{children: React.ReactNode; delay?: number}> = ({children, delay = 0}) => (
  <div
    style={{
      ...useEnter(delay),
      fontFamily: T.mono,
      fontSize: 20,
      letterSpacing: '.16em',
      textTransform: 'uppercase',
      color: T.accent,
      marginBottom: 22,
    }}
  >
    {children}
  </div>
);

const Head: React.FC<{children: React.ReactNode; delay?: number; size?: number}> = ({
  children,
  delay = 0,
  size = 92,
}) => (
  <h1
    style={{
      ...useEnter(delay),
      margin: 0,
      fontSize: size,
      lineHeight: 1.04,
      fontWeight: 650,
      letterSpacing: '-.032em',
      maxWidth: '15em',
    }}
  >
    {children}
  </h1>
);

const Sub: React.FC<{children: React.ReactNode; delay?: number}> = ({children, delay = 3}) => (
  <p
    style={{
      ...useEnter(delay),
      margin: '26px 0 0',
      fontSize: 30,
      lineHeight: 1.45,
      color: T.inkDim,
      maxWidth: '24em',
    }}
  >
    {children}
  </p>
);

/** Contador que sube con el frame. El número ES la animación. */
const Counter: React.FC<{to: number; frames: number; suffix?: string; size?: number}> = ({
  to,
  frames,
  suffix = 's',
  size = 190,
}) => {
  const f = useCurrentFrame();
  const v = interpolate(f, [0, frames], [0, to], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <div
      style={{
        fontFamily: T.mono,
        fontSize: size,
        fontWeight: 600,
        letterSpacing: '-.04em',
        fontVariantNumeric: 'tabular-nums',
        lineHeight: 1,
      }}
    >
      {v.toFixed(1)}
      <span style={{fontSize: size * 0.4, color: T.inkFaint}}>{suffix}</span>
    </div>
  );
};

/** Las dos barras corriendo: el argumento entero en una imagen. */
const Race: React.FC = () => {
  const f = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  // En vertical y cuadrado el lienzo es estrecho: 62% deja las barras flotando
  // en el centro de un frame muy alto. Se ensanchan para que llenen la lectura.
  const raceWidth = width >= height ? '62%' : '84%';
  const t = (f / fps) * (C.proof.fullRenderSeconds / 3.2); // 3.2 s de vídeo = 30.3 s reales
  const pFirst = Math.min(1, t / C.proof.firstFrameSeconds);
  const pFull = Math.min(1, t / C.proof.fullRenderSeconds);
  const lit = t >= C.proof.firstFrameSeconds;

  const Bar: React.FC<{label: string; p: number; on: boolean; note: string}> = ({
    label,
    p,
    on,
    note,
  }) => (
    <div style={{width: '100%', marginBottom: 34}}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontFamily: T.mono,
          fontSize: 19,
          letterSpacing: '.1em',
          textTransform: 'uppercase',
          color: on ? T.accent : T.inkFaint,
          marginBottom: 12,
        }}
      >
        <span>{label}</span>
        <span>{note}</span>
      </div>
      <div style={{height: 10, borderRadius: 6, background: 'rgba(255,255,255,.07)'}}>
        <div
          style={{
            height: '100%',
            width: `${p * 100}%`,
            borderRadius: 6,
            background: on ? T.accent : T.inkFaint,
          }}
        />
      </div>
    </div>
  );

  return (
    <div style={{width: raceWidth}}>
      <div style={{marginBottom: 44, opacity: 0.9}}>
        <Counter to={C.proof.fullRenderSeconds} frames={3.2 * 30} size={150} />
      </div>
      <Bar
        label="First frame"
        p={pFirst}
        on={lit}
        note={lit ? `${C.proof.firstFrameSeconds}s — watchable` : 'generating…'}
      />
      <Bar
        label="Full render"
        p={pFull}
        on={false}
        note={pFull >= 1 ? `${C.proof.fullRenderSeconds}s` : 'rendering…'}
      />
    </div>
  );
};

export const Ad: React.FC = () => {
  const {fps, durationInFrames} = useVideoConfig();
  const s = (n: number) => Math.round(n * fps);

  return (
    <AbsoluteFill style={{backgroundColor: T.bg}}>
      {/* 0.0–2.5 · el problema */}
      <Sequence durationInFrames={s(2.5)}>
        <Stage>
          <Head>{C.copy.hook}</Head>
          <Head delay={5}>{C.copy.hookLine2}</Head>
        </Stage>
      </Sequence>

      {/* 2.5–5.0 · por qué duele */}
      <Sequence from={s(2.5)} durationInFrames={s(2.5)}>
        <Stage>
          <Kicker>The old way</Kicker>
          <Sub delay={3}>{C.copy.problem}</Sub>
        </Stage>
      </Sequence>

      {/* 5.0–8.5 · la promesa, con las dos barras */}
      <Sequence from={s(5)} durationInFrames={s(3.5)}>
        <Stage>
          <Glow />
          <Race />
        </Stage>
      </Sequence>

      {/* 8.5–11.0 · el reveal */}
      <Sequence from={s(8.5)} durationInFrames={s(2.5)}>
        <Stage>
          <Glow intensity={0.22} />
          <Head size={78}>{C.copy.reveal}</Head>
        </Stage>
      </Sequence>

      {/* 11.0–13.0 · lo que se queda */}
      <Sequence from={s(11)} durationInFrames={s(2)}>
        <Stage>
          <Kicker>Keep</Kicker>
          <Sub delay={3}>{C.copy.keep}</Sub>
        </Stage>
      </Sequence>

      {/* 13.0–15.0 · cierre */}
      <Sequence from={s(13)} durationInFrames={durationInFrames - s(13)}>
        <Stage>
          <Glow intensity={0.26} />
          <Head size={104}>{C.product}</Head>
          <Sub delay={3}>{C.copy.cta}</Sub>
        </Stage>
      </Sequence>
    </AbsoluteFill>
  );
};
