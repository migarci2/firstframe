/**
 * Tres diagramas animados para el Project Story. NO son capturas: explican la
 * idea sin enseñar una sola pantalla del producto.
 *
 *   concept-race        las dos barras corriendo sobre el mismo reloj
 *   concept-pipeline    brief → scene → segmentos → B2 → player
 *   concept-object-lock el candado que rebota un DELETE y una lifecycle rule
 *
 * REGLA (la misma que en Ad.tsx): todo el movimiento es función de
 * `useCurrentFrame()`. Ni transitions de CSS ni temporizadores — durante el
 * render no existe el reloj del navegador.
 *
 * Las cifras salen de `campaign.ts`. Si el job real cambia, cambian los GIFs.
 */
import React from 'react';
import {AbsoluteFill, Easing, interpolate, Sequence, useCurrentFrame} from 'remotion';
import {Counter} from './Ad';
import {campaign as C} from './campaign';

const T = C.theme;

/** 16:9 cómodo para GIF. Un solo sitio donde cambiarlo. */
export const conceptSize = {width: 1200, height: 675, fps: 24};

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

/** Fundido de entrada por frame absoluto: sirve para revelar piezas sueltas. */
const fadeIn = (f: number, at: number, dur = 8) => interpolate(f, [at, at + dur], [0, 1], clamp);

const Canvas: React.FC<{children: React.ReactNode}> = ({children}) => (
  <AbsoluteFill style={{backgroundColor: T.bg, fontFamily: T.font, color: T.ink}}>
    {children}
  </AbsoluteFill>
);

/** Rótulo mono en versalitas: la única voz tipográfica de los diagramas. */
const Mono: React.FC<{
  children: React.ReactNode;
  size?: number;
  color?: string;
  style?: React.CSSProperties;
}> = ({children, size = 24, color = T.inkFaint, style}) => (
  <div
    style={{
      fontFamily: T.mono,
      fontSize: size,
      letterSpacing: '.1em',
      textTransform: 'uppercase',
      color,
      ...style,
    }}
  >
    {children}
  </div>
);

/* ------------------------------------------------------------------ *
 * 1 · concept-race
 * ------------------------------------------------------------------ */

const RACE_LEAD = 10; // frames quietos antes de arrancar el reloj
const RACE_RUN = 90; // frames que tarda el reloj en llegar a los 65.7 s reales
export const raceDuration = 122;

/**
 * Dos barras sobre el mismo reloj. Cada barra mide su propia tarea, así que
 * "llena" significa "terminada": la de first frame se llena casi al instante y
 * se queda encendida mientras el reloj sigue corriendo para la otra.
 *
 * Es la lógica del componente `Race` de Ad.tsx, reencuadrada para 1200×675 y
 * con el reloj parametrizado por frames en vez de por los 30 fps del anuncio.
 * El contador es literalmente el mismo componente (`Counter`, importado).
 */
export const ConceptRace: React.FC = () => {
  const f = useCurrentFrame();
  const t = interpolate(f, [RACE_LEAD, RACE_LEAD + RACE_RUN], [0, C.proof.fullRenderSeconds], clamp);

  const pFirst = Math.min(1, t / C.proof.firstFrameSeconds);
  const pFull = Math.min(1, t / C.proof.fullRenderSeconds);
  const lit = t >= C.proof.firstFrameSeconds;
  const done = pFull >= 1;

  const Bar: React.FC<{label: string; note: string; p: number; on: boolean}> = ({
    label,
    note,
    p,
    on,
  }) => (
    <div style={{marginTop: 46}}>
      <div style={{display: 'flex', justifyContent: 'space-between', marginBottom: 16}}>
        <Mono size={26} color={on ? T.accent : T.inkDim}>
          {label}
        </Mono>
        <Mono size={26} color={on ? T.accent : T.inkFaint}>
          {note}
        </Mono>
      </div>
      <div style={{height: 18, borderRadius: 9, background: 'rgba(255,255,255,.07)'}}>
        <div
          style={{
            height: '100%',
            width: `${p * 100}%`,
            borderRadius: 9,
            background: on ? T.accent : T.inkFaint,
          }}
        />
      </div>
    </div>
  );

  return (
    <Canvas>
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
        <div style={{width: 880}}>
          <div style={{textAlign: 'center'}}>
            <Mono size={22} style={{marginBottom: 14}}>
              elapsed
            </Mono>
            {/* Mismo contador que el anuncio; el Sequence le da el retardo.
                Antes de arrancar se dibuja parado en 0.0 — si no, el bucle del
                GIF abre medio segundo con el hueco del reloj vacío. */}
            {f < RACE_LEAD ? (
              <Counter to={0} frames={1} size={132} />
            ) : (
              <Sequence from={RACE_LEAD} layout="none">
                <Counter to={C.proof.fullRenderSeconds} frames={RACE_RUN} size={132} />
              </Sequence>
            )}
          </div>

          <Bar
            label="first frame"
            note={lit ? `${C.proof.firstFrameSeconds}s · watchable` : 'generating'}
            p={pFirst}
            on={lit}
          />
          <Bar
            label="full render"
            note={done ? `${C.proof.fullRenderSeconds}s` : 'rendering'}
            p={pFull}
            on={false}
          />

          <div
            style={{
              marginTop: 44,
              textAlign: 'center',
              opacity: fadeIn(f, RACE_LEAD + RACE_RUN + 2, 10),
              fontSize: 30,
              color: T.inkDim,
            }}
          >
            <span style={{color: T.accent, fontFamily: T.mono, fontWeight: 600}}>
              {C.proof.ratio.toFixed(1)}×
            </span>{' '}
            sooner to something you can judge
          </div>
        </div>
      </AbsoluteFill>
    </Canvas>
  );
};

/* ------------------------------------------------------------------ *
 * 2 · concept-pipeline
 * ------------------------------------------------------------------ */

const SEGMENTS = 8;
const SEG_FIRST = 16; // frame en que sale el primer segmento
const SEG_EVERY = 11; // cadencia de producción
const SEG_FLIGHT = 10; // vuelo hasta el bucket; menor que la cadencia, así nunca se cruzan
export const pipelineDuration = 144;

const segArrival = (i: number) => SEG_FIRST + i * SEG_EVERY + SEG_FLIGHT;

const Chip: React.FC<{
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  opacity?: number;
}> = ({x, y, w, h, label, opacity = 1}) => (
  <div
    style={{
      position: 'absolute',
      left: x,
      top: y,
      width: w,
      height: h,
      opacity,
      border: `1px solid ${T.hairline}`,
      borderRadius: 10,
      background: T.surface,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
    }}
  >
    <Mono size={22} color={T.inkDim}>
      {label}
    </Mono>
  </div>
);

/** Línea con punta: la flecha es una forma, no un icono. */
const Arrow: React.FC<{x: number; y: number; w: number; opacity?: number}> = ({
  x,
  y,
  w,
  opacity = 1,
}) => (
  <div style={{position: 'absolute', left: x, top: y, width: w, opacity}}>
    <div style={{height: 1, background: 'rgba(255,255,255,.18)'}} />
    <div
      style={{
        position: 'absolute',
        right: -1,
        top: -4,
        width: 0,
        height: 0,
        borderTop: '4.5px solid transparent',
        borderBottom: '4.5px solid transparent',
        borderLeft: `9px solid rgba(255,255,255,.28)`,
      }}
    />
  </div>
);

/**
 * El camino de un spot. Lo único que hay que ver: el player arranca cuando
 * aterriza el primer segmento, con la fila todavía a medias.
 */
export const ConceptPipeline: React.FC = () => {
  const f = useCurrentFrame();

  // Geometría (todo absoluto sobre 1200×675, así nada se mueve por reflow).
  const rowY = 330;
  const bucket = {x: 540, y: 244, w: 300, h: 196};
  const player = {x: 890, y: 244, w: 268, h: 196};
  const slot = (i: number) => ({
    x: bucket.x + 18 + (i % 4) * 68,
    y: bucket.y + 52 + Math.floor(i / 4) * 46,
  });
  const origin = {x: 372, y: rowY - 5};

  const arrived = Math.max(0, Math.min(SEGMENTS, Math.floor((f - segArrival(0)) / SEG_EVERY) + 1));
  const playing = arrived === 0 ? -1 : Math.min(arrived - 1, Math.floor((f - segArrival(0)) / 12));
  const playProgress = playing < 0 ? 0 : (playing + 1) / SEGMENTS;

  return (
    <Canvas>
      <Mono size={22} style={{position: 'absolute', left: 56, top: 52}}>
        one scene · {SEGMENTS} hls segments
      </Mono>

      <Chip x={56} y={rowY - 30} w={120} h={60} label="brief" opacity={fadeIn(f, 0)} />
      <Arrow x={186} y={rowY} w={28} opacity={fadeIn(f, 2)} />
      <Chip x={222} y={rowY - 30} w={140} h={60} label="scene" opacity={fadeIn(f, 4)} />
      {/* Carril por el que viajan los segmentos, y salida del bucket al player. */}
      <Arrow x={372} y={rowY} w={158} opacity={fadeIn(f, 6)} />
      <Arrow x={848} y={rowY} w={34} opacity={fadeIn(f, 8)} />

      {/* Bucket */}
      <div
        style={{
          position: 'absolute',
          left: bucket.x,
          top: bucket.y,
          width: bucket.w,
          height: bucket.h,
          opacity: fadeIn(f, 6),
          border: `1px solid ${T.hairline}`,
          borderRadius: 12,
          background: 'rgba(255,255,255,.02)',
        }}
      >
        <Mono size={20} style={{position: 'absolute', left: 18, top: 18}}>
          backblaze b2
        </Mono>
      </div>

      {/* Segmentos: salen de la escena y caen en su hueco del bucket. */}
      {Array.from({length: SEGMENTS}, (_, i) => {
        const born = SEG_FIRST + i * SEG_EVERY;
        if (f < born) return null;
        const p = interpolate(f, [born, born + SEG_FLIGHT], [0, 1], {
          ...clamp,
          easing: Easing.inOut(Easing.quad),
        });
        const to = slot(i);
        // Entra por debajo del bucket y sube a su hueco. En línea recta, un
        // segmento en vuelo se solapa con los que ya están posados; por abajo
        // el hueco al que sube siempre está vacío todavía.
        return (
          <div
            key={i}
            style={{
              position: 'absolute',
              left: interpolate(p, [0, 0.55, 1], [origin.x, to.x, to.x]),
              top: interpolate(p, [0, 0.55, 1], [origin.y, bucket.y + bucket.h + 38, to.y]),
              width: 60,
              height: 34,
              opacity: Math.min(1, p * 4),
              border: `1px solid ${p >= 1 ? 'rgba(255,107,94,.5)' : T.accent}`,
              borderRadius: 7,
              background: 'rgba(255,107,94,.14)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontFamily: T.mono,
              fontSize: 18,
              color: T.accent,
            }}
          >
            s{i}
          </div>
        );
      })}

      {/* Player: empieza en cuanto aterriza s0, con la fila a medio escribir. */}
      <div
        style={{
          position: 'absolute',
          left: player.x,
          top: player.y,
          width: player.w,
          height: player.h,
          opacity: fadeIn(f, 8),
          border: `1px solid ${playing >= 0 ? 'rgba(255,107,94,.45)' : T.hairline}`,
          borderRadius: 12,
          background: T.surface,
        }}
      >
        <Mono size={20} style={{position: 'absolute', left: 18, top: 18}}>
          player
        </Mono>
        <div
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            top: 78,
            textAlign: 'center',
            fontFamily: T.mono,
            fontSize: 34,
            color: playing >= 0 ? T.accent : T.inkFaint,
          }}
        >
          {playing >= 0 ? `playing s${playing}` : 'waiting'}
        </div>
        <div
          style={{
            position: 'absolute',
            left: 18,
            right: 18,
            bottom: 26,
            height: 8,
            borderRadius: 4,
            background: 'rgba(255,255,255,.07)',
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${playProgress * 100}%`,
              borderRadius: 4,
              background: T.accent,
            }}
          />
        </div>
      </div>

      <div
        style={{
          position: 'absolute',
          left: 56,
          right: 56,
          bottom: 62,
          display: 'flex',
          justifyContent: 'space-between',
          opacity: fadeIn(f, segArrival(0)),
        }}
      >
        <Mono size={24} color={T.accent}>
          playback starts at s0 · {C.proof.firstFrameSeconds}s
        </Mono>
        <Mono size={24} color={T.inkFaint}>
          the queue is still writing
        </Mono>
      </div>
    </Canvas>
  );
};

/* ------------------------------------------------------------------ *
 * 3 · concept-object-lock
 * ------------------------------------------------------------------ */

const LOCK_AT = 20; // se pone el candado
const DEL_FROM = 44; // el DELETE entra
const DEL_HIT = 62; // impacto
const DEL_BACK = 80; // ya ha rebotado
const LIFE_FROM = 86; // la lifecycle rule empieza a cruzar
const LIFE_TO = 132;
export const objectLockDuration = 142;

/** Un objeto sellado: el DELETE rebota y la lifecycle rule pasa de largo. */
export const ConceptObjectLock: React.FC = () => {
  const f = useCurrentFrame();

  const locked = f >= LOCK_AT;
  const lockPop = interpolate(f, [LOCK_AT, LOCK_AT + 10], [0.82, 1], {
    ...clamp,
    easing: Easing.out(Easing.back(2)),
  });

  // El chip DELETE: entra, choca y sale rebotado. Dos tramos, sin física.
  const delX =
    f < DEL_HIT
      ? interpolate(f, [DEL_FROM, DEL_HIT], [1240, 842], {...clamp, easing: Easing.in(Easing.quad)})
      : interpolate(f, [DEL_HIT, DEL_BACK], [842, 1036], {
          ...clamp,
          easing: Easing.out(Easing.cubic),
        });
  const delOpacity = f < DEL_FROM ? 0 : interpolate(f, [DEL_BACK + 26, DEL_BACK + 40], [1, 0], clamp);

  // Sacudida amortiguada del objeto en el impacto.
  const shake =
    f < DEL_HIT ? 0 : Math.sin((f - DEL_HIT) * 1.3) * Math.exp(-(f - DEL_HIT) / 5) * 7;

  const lifeX = interpolate(f, [LIFE_FROM, LIFE_TO], [-340, 1240], clamp);
  const lifeOn = f >= LIFE_FROM && f <= LIFE_TO;
  // "no-op" sólo mientras la regla pasa por debajo del objeto.
  const lifeUnder = lifeX > 120 && lifeX < 660;

  return (
    <Canvas>
      <Mono size={22} style={{position: 'absolute', left: 56, top: 52}}>
        bucket · firstframe-artifacts
      </Mono>

      {/* Bucket */}
      <div
        style={{
          position: 'absolute',
          left: 80,
          top: 158,
          width: 756,
          height: 292,
          border: `1px solid ${T.hairline}`,
          borderRadius: 14,
          background: 'rgba(255,255,255,.02)',
        }}
      />

      {/* Carril de la lifecycle rule: por debajo del objeto, sin tocarlo. */}
      {lifeOn ? (
        <div
          style={{
            position: 'absolute',
            left: lifeX,
            top: 366,
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            // Sin esto, al salir por la derecha el chip parte la línea en dos.
            whiteSpace: 'nowrap',
          }}
        >
          <div
            style={{
              border: `1px solid ${T.hairline}`,
              borderRadius: 8,
              padding: '12px 18px',
              background: T.surface,
              fontFamily: T.mono,
              fontSize: 21,
              letterSpacing: '.08em',
              textTransform: 'uppercase',
              color: T.inkDim,
            }}
          >
            lifecycle · expire 7d
          </div>
          <Mono size={21} color={T.inkFaint} style={{opacity: lifeUnder ? 1 : 0}}>
            no-op
          </Mono>
        </div>
      ) : null}

      {/* El objeto */}
      <div
        style={{
          position: 'absolute',
          left: 120 + shake,
          top: 214,
          width: 676,
          height: 96,
          border: `1px solid ${locked ? 'rgba(255,107,94,.55)' : T.hairline}`,
          borderRadius: 12,
          background: T.surface,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 28,
          padding: '0 26px',
        }}
      >
        <div style={{fontFamily: T.mono, fontSize: 26, color: T.ink}}>lyra-01/final.mp4</div>
        <div
          style={{
            opacity: locked ? 1 : 0,
            transform: `scale(${lockPop})`,
            border: `1px solid ${T.accent}`,
            borderRadius: 8,
            padding: '10px 16px',
            background: 'rgba(255,107,94,.12)',
            fontFamily: T.mono,
            fontSize: 22,
            letterSpacing: '.08em',
            textTransform: 'uppercase',
            color: T.accent,
          }}
        >
          governance · 30d
        </div>
      </div>

      {/* El intento de borrado */}
      <div
        style={{
          position: 'absolute',
          left: delX,
          top: 236,
          opacity: delOpacity,
          border: `1px solid ${T.inkFaint}`,
          borderRadius: 8,
          padding: '14px 20px',
          background: T.surface,
          fontFamily: T.mono,
          fontSize: 24,
          letterSpacing: '.08em',
          textTransform: 'uppercase',
          color: T.inkDim,
        }}
      >
        delete
      </div>

      {/* El código de error va tal cual lo devuelve S3: no es un rótulo, es la respuesta. */}
      <div
        style={{
          position: 'absolute',
          left: 890,
          top: 318,
          opacity: fadeIn(f, DEL_HIT + 2, 6),
          fontFamily: T.mono,
          fontSize: 26,
          color: T.accent,
        }}
      >
        AccessDenied
      </div>

      <div
        style={{
          position: 'absolute',
          left: 80,
          bottom: 74,
          opacity: fadeIn(f, LIFE_TO - 20, 12),
          fontSize: 30,
          color: T.inkDim,
        }}
      >
        <span style={{color: T.accent}}>Immutable for 30 days</span> — not even we can delete it.
      </div>
    </Canvas>
  );
};
