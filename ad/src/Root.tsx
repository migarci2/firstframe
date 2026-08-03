import React from 'react';
import {Composition} from 'remotion';
import {Ad} from './Ad';
import {campaign as C} from './campaign';

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
  </>
);
