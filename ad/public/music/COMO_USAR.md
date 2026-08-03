# Música

1. Descarga el tema desde el **enlace oficial del canal** (está en la descripción del
   vídeo de YouTube), no del propio YouTube: el enlace oficial trae la línea de
   atribución que evita una reclamación de Content ID.
2. Copia el mp3 aquí, en `ad/public/music/`.
3. Pon su nombre en `ad/src/music.json`:

```json
{ "file": "track.mp3", "volume": 0.16, "duckedVolume": 0.05, "startSeconds": 0 }
```

`startSeconds` sirve para saltarse la entrada: estos temas suelen abrir con un golpe o
un build de dos o tres segundos que pisa la primera frase.

El **ducking es automático**: la música baja a `duckedVolume` en los tramos que tienen
locución y sube a `volume` en los huecos, con fundido de entrada y de salida. Sale de
`timeline.json`, así que no hay que marcar nada a mano.

Y **pega la atribución en la descripción del vídeo de YouTube**. Es la condición de la
licencia y cuesta una línea.
