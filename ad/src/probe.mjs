// Mide los ficheros que hay en public/clips y public/vo con ffprobe y escribe
// src/timeline.json. Remotion necesita saber la duracion ANTES de renderizar, y
// probar el fichero en el navegador no vale: el render no tiene reloj.
import {execSync} from 'node:child_process';
import {readdirSync, writeFileSync, existsSync} from 'node:fs';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const dur = (f) => {
  try {
    return parseFloat(execSync(
      `ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${f}"`,
    ).toString().trim()) || 0;
  } catch { return 0; }
};
const list = (d) => existsSync(path.join(root, 'public', d))
  ? readdirSync(path.join(root, 'public', d)).filter((f) => !f.startsWith('.')).sort()
  : [];

const sections = [
  {id: 1, title: 'The problem'},
  {id: 2, title: 'First frame'},
  {id: 3, title: 'Reject mid-render'},
  {id: 4, title: 'Failover on camera'},
  {id: 5, title: 'Approve and provenance'},
  {id: 6, title: 'Architecture'},
];

const clips = list('clips'), vos = list('vo');
const pick = (arr, n) => arr.find((f) => f.startsWith(String(n).padStart(2, '0'))) || null;

const out = sections.map((s) => {
  const clip = pick(clips, s.id), vo = pick(vos, s.id);
  const cd = clip ? dur(path.join(root, 'public', 'clips', clip)) : 0;
  const vd = vo ? dur(path.join(root, 'public', 'vo', vo)) : 0;
  // El tramo dura lo que dure la voz + un respiro, o el clip si es mas largo.
  // Si falta la voz, manda el clip. Si falta todo, 3 s de marcador.
  const seconds = Math.max(vd > 0 ? vd + 0.8 : 0, cd, 3);
  return {...s, clip, vo, clipSeconds: +cd.toFixed(2), voSeconds: +vd.toFixed(2),
          seconds: +seconds.toFixed(2)};
});

writeFileSync(path.join(root, 'src', 'timeline.json'), JSON.stringify(out, null, 2));
const total = out.reduce((a, s) => a + s.seconds, 0);
for (const s of out) {
  const mark = s.clip ? '·' : '!';
  console.log(`  ${mark} ${s.id}. ${s.title.padEnd(24)} clip=${(s.clip||'—').padEnd(22)} vo=${(s.vo||'—').padEnd(10)} ${s.seconds}s`);
}
console.log(`\n  total ${Math.floor(total/60)}:${String(Math.round(total%60)).padStart(2,'0')}` +
            (total > 180 ? '  AVISO: pasa de 3:00, recorta' : ''));
