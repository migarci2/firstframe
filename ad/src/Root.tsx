import React from 'react';
import {Composition} from 'remotion';
import {Ad} from './Ad';
import {Demo, demoDurationInFrames} from './Demo';
import {
  ConceptObjectLock,
  ConceptPipeline,
  ConceptRace,
  conceptSize,
  objectLockDuration,
  pipelineDuration,
  raceDuration,
} from './Concepts';
import {campaign as C} from './campaign';

/** Diagramas del Project Story: mismo lienzo, mismas cifras, sin interfaz. */
const concepts = [
  {id: 'concept-race', component: ConceptRace, durationInFrames: raceDuration},
  {id: 'concept-pipeline', component: ConceptPipeline, durationInFrames: pipelineDuration},
  {id: 'concept-object-lock', component: ConceptObjectLock, durationInFrames: objectLockDuration},
] as const;

/** Un id por formato, todos desde la misma composición y los mismos datos. */
export const RemotionRoot: React.FC = () => (
  <>
    {C.formats.map((f) => (
      <Composition
        key={f.id}
        id={f.id}
        component={Ad}
        durationInFrames={C.durationSeconds * C.fps}
        fps={C.fps}
        width={f.width}
        height={f.height}
      />
    ))}
    <Composition
      id="demo"
      component={Demo}
      durationInFrames={demoDurationInFrames(C.fps)}
      fps={C.fps}
      width={1920}
      height={1080}
    />
    {concepts.map((c) => (
      <Composition
        key={c.id}
        id={c.id}
        component={c.component}
        durationInFrames={c.durationInFrames}
        fps={conceptSize.fps}
        width={conceptSize.width}
        height={conceptSize.height}
      />
    ))}
  </>
);
