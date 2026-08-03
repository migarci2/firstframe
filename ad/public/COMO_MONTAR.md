# Montar el vídeo con Remotion

## 1. Copia los ficheros aquí

```
ad/public/clips/     01-problem.mp4  02-first-frame.mp4  03-reject.mp4
                     04-failover.mp4  05-approve.mp4     06-architecture.mp4
ad/public/vo/        01.mp3  02.mp3  03.mp3  04.mp3  05.mp3  06.mp3
```

**Solo importa el prefijo numérico** (`01`, `02`…). El resto del nombre da igual.
Falta un tramo? No pasa nada: sale una tarjeta con su título y el resto se monta igual,
así puedes ir sustituyendo según grabas.

## 2. Mide

```bash
cd ad && node src/probe.mjs
```

Lee cada fichero con ffprobe y escribe `src/timeline.json`. Imprime la tabla y **avisa si
te pasas de 3:00**. Cada tramo dura lo que dure su locución más un respiro, o lo que dure
el clip si es más largo.

## 3. Mira antes de renderizar

```bash
npx remotion studio
```

Abre el navegador. Composición `demo`. Puedes moverte por la línea de tiempo y ver
exactamente lo que va a salir.

## 4. Renderiza

```bash
npx remotion render demo out/demo.mp4
```

## Notas

- **El audio de los clips va silenciado**: manda la locución. Si un clip lleva sonido que
  quieras conservar, quita `muted` de ese `OffthreadVideo` en `src/Demo.tsx`.
- Los rótulos inferiores salen del título de cada tramo en `src/probe.mjs`. Cámbialos ahí.
- Si Remotion falla al bundlear, comprueba que `typescript` sigue en **5.9**: con
  TypeScript 7 el loader muere porque el port en Go no expone `ts.sys`.
