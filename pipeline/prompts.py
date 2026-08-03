"""Construccion del prompt de keyframe a partir del brief del usuario.

Por que existe este fichero
---------------------------
La version anterior metia el brief CRUDO al principio del prompt:

    "serum facial premium, marmol blanco, luz de manana. the whole product
     standing on a table ... no people. Warm neutral palette, ..."

y el generador devolvia un PRIMER PLANO DE LA CARA DE UNA MUJER. Dos fallos a
la vez, los dos explicables:

1. **El sujeto no era un objeto.** El unico sustantivo concreto que el text
   encoder reconocia era `facial`; `serum`, `marmol` y `manana` estan en
   castellano y no significan nada para un CLIP entrenado en ingles. Asi que
   "facial" ganaba y salia una cara. Encima `no people` es una NEGACION: los
   text encoders tipo CLIP no tienen operador de negacion, asi que "no people"
   aporta el embedding de *people*. Estabamos pidiendo la cara dos veces.
2. **La fidelidad se perdia.** "marmol blanco" y "luz de manana" no llegaban al
   modelo en ningun idioma que entendiera, asi que no aparecian nunca.

La solucion es la misma para los dos: **traducir el brief a una escena en
ingles antes de generarla**. Aqui se hace en tres pasos:

    parse_brief()  ->  BriefSpec(subject, surface, light, palette, extras)
    style_anchor() ->  la frase de estilo que COMPARTEN las 3 escenas
    scene_prompt() ->  sujeto + plano del beat + ancla + suffix

Decisiones que se tomaron mirando imagenes generadas, no teorizando
------------------------------------------------------------------
- **El sujeto es siempre un objeto fisico nombrado.** `_PRODUCTS` mapea la
  categoria del brief a un sintagma concreto en ingles ("a small amber glass
  dropper bottle of cosmetic serum"). Un sustantivo concreto al principio del
  prompt es lo unico que impide que el modelo invente el sujeto.
- **Cero negaciones para lo humano.** En vez de "no people" se afirma lo que SI
  queremos: `still life product photography, objects only, empty uninhabited
  scene`. Las negaciones van al `negative_prompt`, que es donde si restan.
- **El ancla sale del brief, no de un hash.** Superficie, luz y paleta se
  extraen del brief y se repiten IDENTICAS en las 3 escenas: eso es lo que hace
  que parezcan el mismo spot y a la vez fieles a lo que pidio el usuario.
- **Sin nombres de marca ni numeros de escena.** El modelo intenta ESCRIBIR
  cualquier texto que le llegue y sale un garabato.

Autocomprobacion (sin red):

    .venv/bin/python -m pipeline.prompts
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "BriefSpec",
    "NEGATIVE_PROMPT",
    "beat_role",
    "keyframe_prompt",
    "parse_brief",
    "scene_prompt",
    "style_anchor",
]

# --- Normalizacion -----------------------------------------------------------


def _fold(text: str) -> str:
    """minusculas sin acentos: 'Mármol Blanco' -> 'marmol blanco'.

    Todo el matching de abajo se hace contra esta forma, asi el mismo brief
    escrito con o sin tildes (y el usuario escribe de las dos maneras) da
    exactamente el mismo prompt — y por tanto el mismo acierto de cache.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


def _hit(text: str, cues: tuple[str, ...]) -> bool:
    """True si alguna pista aparece como palabra (o prefijo de palabra).

    Prefijo y no substring: 'te' (te/tea) no puede casar dentro de
    'detergente', y 'pan' (sarten) no puede casar dentro de 'pantalon'.
    """
    return any(re.search(rf"\b{re.escape(cue)}", text) for cue in cues)


# --- Sujeto: categoria del brief -> objeto fisico en ingles -------------------
# Orden = prioridad: la primera entrada que casa gana, asi que lo especifico va
# antes que lo generico ("aceite de oliva" antes que "aceite").
#
# El sintagma describe SIEMPRE un objeto inanimado, cerrado y fotografiable, y
# nunca su uso. "crema facial" no se traduce como "face cream" (que arrastra la
# cara) sino como el TARRO de crema: el producto, no la parte del cuerpo.
_PRODUCTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # --- cosmetica: la familia que mas se desvia a caras --------------------
    ("a slim dark glass bottle of olive oil with a pouring spout",
     ("aceite de oliva", "olive oil", "oliva")),
    ("a small amber glass dropper bottle of cosmetic serum",
     ("serum", "suero", "ampolla", "elixir")),
    ("a round frosted ceramic jar of cosmetic cream, lid resting beside it",
     ("crema", "cream", "hidratante", "moisturi", "balsamo", "unguento")),
    # "faceted" NO: el tokenizer lo parte en "face" + "ted" y el perfume era
    # justo una de las categorias que se desviaban a retratos.
    ("a heavy cut glass perfume bottle with a polished metal cap",
     ("perfume", "fragancia", "colonia", "eau de", "fragrance", "cologne")),
    ("an upright metal lipstick bullet, cap lying next to it",
     ("labial", "lipstick", "pintalabios", "carmin")),
    ("a matte pump bottle of shampoo standing upright",
     ("champu", "shampoo", "acondicionador", "conditioner", "gel de ducha")),
    ("a folded sheet face mask sachet and a ceramic dish",
     ("mascarilla", "sheet mask")),
    ("a slim glass bottle of botanical oil with a wooden cap",
     ("aceite", "oil", "esencia", "essential")),
    ("a matte tube of sunscreen lying on its side",
     ("solar", "sunscreen", "spf", "proteccion solar")),
    ("a compact of pressed powder, open, beside a brush",
     ("maquillaje", "makeup", "polvos", "base de", "foundation")),
    # --- bebida y alimentacion ---------------------------------------------
    ("a white porcelain espresso cup on a saucer, roasted coffee beans "
     "scattered beside it",
     ("cafe", "coffee", "espresso", "cappuccino", "arabica")),
    ("a tin of loose leaf tea, open, beside a clear glass cup of tea",
     ("te ", "tea", "infusion", "matcha", "rooibos")),
    ("a cold amber beer bottle beaded with condensation beside a filled glass",
     ("cerveza", "beer", "lager", "ipa", "birra")),
    ("a dark wine bottle beside a half filled wine glass",
     ("vino", "wine", "tinto", "cava", "champagne", "champan")),
    ("a tall spirits bottle with a heavy glass base beside a tumbler with ice",
     ("gin", "vodka", "whisky", "whiskey", "ron ", "rum", "tequila", "licor",
      "mezcal", "destilado")),
    ("a chilled glass bottle of fruit juice, droplets on the glass",
     ("zumo", "juice", "refresco", "soda", "limonada", "smoothie", "batido")),
    ("a slim stainless steel insulated water bottle standing upright, matte "
     "finish, no markings",
     ("termica", "termo", "thermos", "cantimplora", "botella de agua",
      "water bottle", "flask", "insulated bottle")),
    ("a slim glass bottle of still water beaded with condensation",
     ("agua", "water", "mineral")),
    ("a wrapped chocolate bar with a few squares broken off beside it",
     ("chocolate", "cacao", "bombon")),
    ("a glass jar of honey with a wooden dipper resting across the rim",
     ("miel", "honey")),
    ("a rustic loaf of bread on a wooden board, one slice cut",
     ("pan ", "bread", "hogaza", "panaderia", "bakery")),
    ("a matte tub of protein powder with the scoop lying beside it",
     ("proteina", "protein", "suplemento", "supplement", "vitamina", "vitamin",
      "colageno", "creatina")),
    ("an open cardboard box of artisan snacks arranged on a board",
     ("snack", "galleta", "cookie", "cereal", "barrita")),
    # --- moda y accesorios --------------------------------------------------
    ("a single empty unworn running sneaker in three quarter view, knit upper "
     "and thick rubber sole",
     ("zapatilla", "sneaker", "running", "deportiva", "trainer", "calzado",
      "zapato", "shoe", "bota", "boot", "sandalia")),
    ("an empty structured leather shoulder bag standing upright, strap in a "
     "soft curve",
     ("bolso", "handbag", "cartera", "clutch", "tote")),
    ("an empty canvas backpack standing upright, straps hanging loose",
     ("mochila", "backpack", "rucksack")),
    ("an unworn wristwatch with a leather strap lying flat, dial catching the "
     "light",
     ("reloj", "watch", "cronografo")),
    ("a pair of folded sunglasses resting on their temples",
     ("gafas", "sunglasses", "lentes", "eyewear")),
    ("a neatly folded empty knitted garment stacked on a wooden surface",
     ("camiseta", "jersey", "sudadera", "chaqueta", "abrigo", "ropa", "prenda",
      "textil", "shirt", "hoodie", "jacket", "garment", "moda")),
    ("a fine gold ring and a thin chain arranged on a velvet tray",
     ("joya", "anillo", "collar", "pulsera", "jewel", "ring", "necklace")),
    # --- tecnologia ---------------------------------------------------------
    ("a pair of matte over ear headphones resting on their earcups",
     ("auricular", "headphone", "casco", "earbud", "airpod", "audifono")),
    ("a compact fabric wrapped bluetooth speaker standing upright",
     ("altavoz", "speaker", "soundbar", "bafle")),
    ("an open thin laptop on a desk, screen showing a soft abstract gradient",
     ("portatil", "laptop", "ordenador", "notebook", "macbook", "pc ")),
    ("a smartphone lying face up on a desk, screen showing a soft abstract "
     "gradient",
     ("movil", "telefono", "smartphone", "phone", "iphone")),
    ("a compact mirrorless camera with a prime lens, standing on its base",
     ("camara", "camera", "objetivo", "lente fotogr")),
    ("a low profile mechanical keyboard seen at a shallow angle",
     ("teclado", "keyboard", "raton", "mouse")),
    ("a matte drone resting on a flat surface, rotor arms folded out",
     ("dron", "drone")),
    ("a slim matte payment card standing on its edge, blank unembossed surface",
     ("tarjeta", "card", "banco", "bank", "fintech", "pago", "payment")),
    ("a thin tablet device on a desk beside a stylus, screen showing a soft "
     "abstract gradient",
     ("tablet", "ipad")),
    ("a sleek matte device on a clean desk beside a closed notebook, small "
     "status light glowing",
     ("app", "software", "saas", "plataforma", "platform", "dashboard",
      "startup", "digital", "ia ", "inteligencia artificial")),
    # --- hogar, movilidad y varios -----------------------------------------
    ("a lit scented candle in a heavy glass jar, wax pooled around the wick",
     ("vela", "candle", "ambientador", "difusor", "diffuser")),
    ("a sculptural lounge chair seen in three quarter view",
     ("silla", "chair", "sillon", "sofa", "butaca", "mueble", "furniture")),
    ("a table lamp switched on, warm bulb glowing behind a linen shade",
     ("lampara", "lamp", "luminaria")),
    ("a glazed ceramic mug seen at a low angle, faint steam rising",
     ("taza", "mug", "cup ", "vajilla", "ceramica")),
    ("a chef knife resting on a wooden cutting board, blade catching the light",
     ("cuchillo", "knife", "cuchilla")),
    ("a heavy cast iron pan seen from a low angle on a stove top",
     ("sarten", "olla", "cazuela", "pan de hierro", "cookware", "cacerola")),
    ("a leafy potted plant in a matte ceramic planter",
     ("planta", "plant", "maceta", "flor", "flower", "jardin")),
    ("a hardcover book lying closed on a table, plain unmarked cover",
     ("libro", "book", "revista", "cuaderno", "notebook de papel")),
    ("a matte spray bottle of household cleaner standing upright",
     ("limpiador", "detergente", "cleaner", "limpieza", "jabon", "soap")),
    ("a city bicycle standing on its kickstand, seen from the side",
     ("bici", "bicycle", "bike", "ciclismo")),
    ("a compact electric car parked alone, seen in three quarter view",
     ("coche", "car ", "vehiculo", "automovil", "suv", "electrico")),
    ("a rolling cabin suitcase standing upright, handle extended",
     ("maleta", "suitcase", "equipaje", "luggage", "viaje", "travel")),
    ("a rubber yoga mat rolled up and standing on end beside a cork block",
     ("yoga", "pilates", "esterilla", "fitness", "gimnasio", "gym")),
    ("a pair of matte dumbbells resting on a rubber floor",
     ("mancuerna", "pesa", "dumbbell", "musculacion")),
    ("a toy wooden building block set arranged in a small stack",
     ("juguete", "toy", "peluche", "infantil")),
    ("a bag of pet food beside a ceramic bowl",
     ("mascota", "pet", "perro", "gato", "dog", "cat ", "pienso")),
)

# Sujeto por defecto si el brief no cae en ninguna categoria conocida. Nunca se
# deja vacio ni se deja el brief crudo mandando: sin un objeto nombrado el
# modelo inventa el sujeto, y lo que inventa suele ser una persona.
_FALLBACK_SUBJECT = "a single unbranded product package standing upright"


def _subject(folded: str) -> str:
    for phrase, cues in _PRODUCTS:
        if _hit(folded, cues):
            return phrase
    return _FALLBACK_SUBJECT


# --- Superficie / entorno ----------------------------------------------------
_SURFACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("black marble", ("marmol negro", "black marble")),
    ("white marble", ("marmol", "marble", "travertino", "alabastro")),
    ("pale travertine stone", ("piedra", "stone", "pizarra", "slate", "granito")),
    ("dark walnut wood", ("madera oscura", "dark wood", "nogal", "walnut",
                          "wenge", "ebano")),
    ("warm oak wood", ("madera", "wood", "roble", "oak", "bambu", "teca")),
    ("raw polished concrete", ("hormigon", "concrete", "cemento", "microcemento")),
    ("natural linen cloth", ("lino", "linen", "tela", "fabric", "algodon",
                             "cotton", "seda", "silk")),
    ("pale beach sand", ("arena", "sand", "playa", "beach", "duna")),
    ("wet asphalt", ("asfalto", "asphalt", "calle", "street", "urbano",
                     "urban", "ciudad", "city")),
    ("mossy forest stone", ("bosque", "forest", "musgo", "montana", "mountain",
                            "naturaleza", "nature", "selva")),
    ("dark slate with water", ("agua", "water", "mar ", "ocean", "rio",
                               "piscina", "pool")),
    # Los ultimos a proposito: ver el comentario de arriba.
    ("brushed stainless steel", ("acero", "steel", "aluminio", "aluminium",
                                 "metal", "titanio", "cromo")),
    ("clear glass and mirror", ("cristal", "glass", "espejo", "mirror",
                                "vidrio", "metacrilato")),
)
_DEFAULT_SURFACE = "warm neutral stone"


def _surface(folded: str) -> str:
    for phrase, cues in _SURFACES:
        if _hit(folded, cues):
            return phrase
    return _DEFAULT_SURFACE


# --- Luz ---------------------------------------------------------------------
# (frase de luz, paleta implicita, pistas). La paleta implicita solo se usa si
# el brief no nombra un color explicito.
_LIGHTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("soft low morning window light, long gentle shadows",
     "warm neutral palette",
     ("manana", "morning", "amanecer", "dawn", "sunrise", "alba", "desayuno")),
    ("low golden hour sunlight, amber highlights",
     "warm amber palette",
     ("atardecer", "sunset", "golden", "dorada", "ocaso", "crepusculo",
      "hora dorada", "golden hour")),
    ("one warm key light in near darkness, deep falloff",
     "dark charcoal palette",
     ("noche", "night", "nocturno", "oscuro", "dark", "moody", "medianoche",
      "neon", "bar ")),
    ("clean diffused studio softbox lighting",
     "clean neutral palette",
     ("estudio", "studio", "editorial", "minimal", "packshot", "catalogo")),
    ("bright overcast daylight through a wide window",
     "bright airy palette",
     ("ventana", "window", "dia", "daylight", "luz natural", "natural light",
      "nublado", "overcast")),
    ("hard bright midday sun, crisp shadows",
     "sun bleached palette",
     ("sol", "sunny", "soleado", "verano", "summer", "mediodia", "playa",
      "beach")),
)
_DEFAULT_LIGHT = ("soft directional daylight from one side",
                  "warm neutral palette")


def _light(folded: str) -> tuple[str, str]:
    for phrase, palette, cues in _LIGHTS:
        if _hit(folded, cues):
            return phrase, palette
    return _DEFAULT_LIGHT


# --- Paleta explicita --------------------------------------------------------
_PALETTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("off white and pale cream palette", ("blanco", "white", "marfil", "ivory",
                                          "crudo", "nacar")),
    ("deep charcoal and black palette", ("negro", "black", "carbon", "onyx")),
    ("cool blue and slate palette", ("azul", "blue", "indigo", "cobalto",
                                     "marino", "navy")),
    ("muted sage green palette", ("verde", "green", "sage", "oliva", "menta",
                                  "esmeralda")),
    ("warm gold and brass palette", ("dorado", "gold", "oro ", "laton",
                                     "brass", "champan")),
    ("dusty rose and nude palette", ("rosa", "pink", "nude", "rose", "malva",
                                     "coral")),
    ("deep burgundy and terracotta palette", ("rojo", "red", "burdeos",
                                              "terracota", "granate", "vino")),
    ("soft pastel palette", ("pastel", "suave", "candy")),
    ("warm sand and beige palette", ("beige", "arena", "camel", "taupe",
                                     "tostado")),
)


def _palette(folded: str, implied: str) -> str:
    for phrase, cues in _PALETTES:
        if _hit(folded, cues):
            return phrase
    return implied


# --- BriefSpec ---------------------------------------------------------------


@dataclass(frozen=True)
class BriefSpec:
    """El brief del usuario traducido a los ejes que entiende el generador."""

    subject: str      # objeto fisico nombrado, en ingles
    surface: str      # sobre que se apoya / de que es el entorno
    light: str        # de donde viene la luz y como cae
    palette: str      # gama cromatica
    generic: bool     # True si el sujeto salio del fallback y no del lexico

    @property
    def anchor(self) -> str:
        """Lo que COMPARTEN las 3 escenas. Ver `style_anchor`."""
        return style_anchor(self)


def parse_brief(brief: str) -> BriefSpec:
    """Brief libre (castellano o ingles) -> `BriefSpec`.

    Deterministico y sin red: el mismo brief da siempre el mismo spec, y por
    tanto el mismo prompt, y por tanto acierto en el corpus de keyframes.
    """
    folded = _fold(brief)
    light, implied = _light(folded)
    subject = _subject(folded)
    return BriefSpec(
        subject=subject,
        surface=_surface(folded),
        light=light,
        palette=_palette(folded, implied),
        generic=subject == _FALLBACK_SUBJECT,
    )


def style_anchor(spec: BriefSpec | str, *, studio: bool = False) -> str:
    """Frase de estilo compartida por las 3 escenas, DERIVADA del brief.

    Antes se elegia por hash entre cuatro anclas fijas, asi que un brief que
    pedia "marmol blanco, luz de manana" podia acabar con una paleta azul
    acero: el ancla peleaba contra el brief. Aqui sale del propio brief, asi que
    fidelidad y coherencia dejan de ser objetivos opuestos.

    El tratamiento de camara (35mm, grano, DOF corta) es fijo a proposito: es
    la parte que no depende del brief y la que hace que las 3 escenas parezcan
    salidas de la misma camara.

    `studio=True` (solo el plano heroe) cambia la luz de entorno por luz de
    plato y MANTIENE paleta, superficie y tratamiento. No es una excepcion
    gratuita: con la luz de ventana del brief dentro, el "seamless backdrop" y
    el "plinth" del heroe se los comia el modelo y las 3 escenas salian a la
    misma distancia — no habia arco. Lo que sostiene la coherencia es la paleta
    y el tratamiento, no la luz, que en un spot real tambien cambia entre el
    plano de ambiente y el packshot.
    """
    if isinstance(spec, str):
        spec = parse_brief(spec)
    light = "controlled studio lighting, strong rim light" if studio else spec.light
    return f"{spec.palette}, {light}, 35mm film, shallow depth of field, fine grain"


# --- Beats -------------------------------------------------------------------
# Mini arco de 3 tiempos: entorno -> textura -> heroe. El TIPO DE PLANO va
# primero y el sujeto justo detras; los modelos de difusion pesan mucho mas los
# primeros tokens, y con el tipo de plano al final (como estaba) las 3 escenas
# salian a la misma distancia y no habia arco.
#
# Calibrado mirando generaciones, no de cabeza:
#   - "camera far back, sits small on a distant table" en la apertura se pasa de
#     frenada: sale una habitacion preciosa con el producto convertido en un
#     borron irreconocible. El plano que funciona es producto en primer termino
#     con el entorno detras y desenfocado — que es exactamente el frame de
#     referencia del anuncio real.
#   - "tight close-up" sale casi igual que la apertura y el arco se cae;
#     "Macro 100mm photograph, extreme close-up ... edge to edge" si separa.
#     La distancia focal es la palabra que de verdad mueve el encuadre.
#   - "plinth + plain seamless backdrop + rim light + vignette" es lo unico que
#     produce un heroe de verdad; "hero photograph" a secas lo ignora el modelo.
#     Y solo funciona si la luz de ventana del brief NO viaja en el mismo
#     prompt (ver `style_anchor(studio=True)`).
_BEAT_SHOTS: dict[str, str] = {
    "open": ("Wide angle establishing product photograph: {subject} in the "
             # "interior" no: con una superficie de exterior (asfalto, arena)
             # el modelo tiene que elegir y el plano se rompe. "setting" deja
             # que el entorno lo decida la superficie del brief.
             "foreground on {surface}, a calm empty setting soft out of "
             "focus behind it"),
    "detail": ("Macro 100mm photograph, extreme close-up: {subject} fills the "
               "whole frame edge to edge, surface texture and material razor "
               "sharp"),
    "hero": ("Studio packshot, 85mm lens: {subject} isolated dead centre on a "
             "{surface} plinth against a plain seamless backdrop, strong rim "
             "light, mirror floor reflection, vignette"),
}

# Titulos que produce `runner._BEATS` (y los que suele inventar el plan por LLM)
# -> beat visual. Si el titulo no se reconoce se cae a la POSICION, que es lo
# que garantiza el arco aunque el plan venga de un modelo de chat.
_TITLE_ROLES: dict[str, str] = {
    "apertura": "open", "opening": "open", "establish": "open", "wide": "open",
    "detalle": "detail", "detail": "detail", "macro": "detail",
    "materia": "detail", "textura": "detail", "beneficio": "detail",
    "contexto": "detail", "producto": "detail",
    # Los titulos que produce `runner._BEATS` desde que son en ingles. Los de
    # arriba se quedan: un plan por LLM todavia puede devolverlos en castellano.
    "context": "detail", "materials": "detail", "result": "detail",
    "cierre": "hero", "closing": "hero", "hero": "hero", "final": "hero",
}

# Sufijo global. Todo en POSITIVO: describe la foto que queremos (bodegon de
# producto, escena vacia, objetos) en vez de prohibir la que no queremos.
#
# Esto NO es una preferencia de estilo, es el unico mecanismo que funciona:
# `negative_prompt` esta MEDIDO como inerte en Pollinations (ver NEGATIVE_PROMPT).
# Todo lo que evita que salga una persona tiene que estar en este texto.
_SUFFIX = ("Still life product photography, objects only, empty scene, "
           "unbranded blank packaging, cinematic advertising still, "
           "16:9 widescreen, photorealistic, sharp focus")

# Negacion pura, para el campo `negative_prompt` del Step.
#
# MEDIDO (2026-08-03): Pollinations ACEPTA el parametro y lo IGNORA. Prueba de
# control: el mismo prompt "a bright red sports car" con seed fija y
# `negative_prompt="red, crimson, scarlet, red paint"` devuelve el coche igual
# de rojo, visualmente identico al que sale sin negativo. Por eso el trabajo
# anti-persona vive entero en `_SUFFIX` y en `_PRODUCTS`, y no aqui.
#
# Se manda igualmente porque (a) no cuesta nada, (b) los conectores del modo
# `real` (NIM, SDXL) SI lo honran, y (c) entra en la clave del corpus, asi que
# documenta con que se genero cada imagen. Lo que NO se hace es depender de el.
NEGATIVE_PROMPT = (
    "person, people, human, man, woman, face, portrait, headshot, model, "
    "skin, hands, fingers, arms, body, crowd, mannequin, doll, "
    "text, letters, words, typography, caption, watermark, signature, logo, "
    "brand name, ugly, deformed, disfigured, extra limbs, plastic skin, "
    "cgi render, 3d render, illustration, cartoon, painting, "
    "low quality, blurry, jpeg artifacts, oversaturated, "
    "collage, split screen, picture frame, border, cropped"
)

# El refinado (juez de vision o nota del revisor) que arrastra `refine_scene`
# pegado al final de `Scene.keyframe_prompt`. Se rescata para no perderlo al
# reconstruir el prompt desde el brief.
_REVIEWER_RE = re.compile(r"(Reviewer rejected the previous take:.*)$",
                          re.IGNORECASE | re.DOTALL)


def beat_role(n: int, title: str = "", total: int = 3) -> str:
    """Que beat visual le toca a esta escena: open | detail | hero.

    Por titulo si se reconoce (el plan por plantilla los nombra), por POSICION
    si no (el plan por LLM inventa los titulos). La primera abre y la ultima
    cierra siempre: eso es el arco.
    """
    key = _fold(title)
    for cue, role in _TITLE_ROLES.items():
        if cue in key:
            return role
    if n <= 1:
        return "open"
    if total > 1 and n >= total:
        return "hero"
    return "detail"


def scene_prompt(spec: BriefSpec, role: str) -> str:
    """Prompt visual de una escena: sujeto -> plano -> ancla -> sufijo."""
    shot = _BEAT_SHOTS.get(role, _BEAT_SHOTS["detail"])
    shot = shot.format(subject=spec.subject, surface=spec.surface)
    return f"{shot}. {style_anchor(spec, studio=role == 'hero')}. {_SUFFIX}."


def keyframe_prompt(brief: str, *, n: int = 1, title: str = "", total: int = 3,
                    extra: str = "", scene_prompt_hint: str = "") -> str:
    """Prompt final del keyframe de una escena, listo para el provider.

    Args:
        brief: el brief libre del usuario, tal cual lo escribio.
        n / title / total: para elegir el beat (ver `beat_role`).
        extra: refinado del juez de vision, ya formateado.
        scene_prompt_hint: `Scene.keyframe_prompt`. NO se usa como prompt (por
            eso "hint"): solo se le rescata la nota del revisor que
            `runner.refine_scene` le pega al final, que si tiene que llegar al
            modelo. El resto se descarta a proposito — es lo que arrastraba el
            brief crudo y las anclas por hash.
    """
    text = scene_prompt(parse_brief(brief), beat_role(n, title, total))
    note = _REVIEWER_RE.search(scene_prompt_hint or "")
    if note:
        text = f"{text} {note.group(1).strip()}"
    return f"{text}{extra}"


# --- Autocomprobacion --------------------------------------------------------


def demo() -> None:
    """Comprueba fidelidad, ausencia de personas, coherencia y arco. Sin red."""
    # 1. El brief que fallaba: pedia serum y salia una cara.
    bad = "serum facial premium, marmol blanco, luz de manana"
    spec = parse_brief(bad)
    assert "dropper bottle" in spec.subject, spec.subject
    assert spec.surface == "white marble", spec.surface
    assert "morning" in spec.light, spec.light
    p1 = keyframe_prompt(bad, n=1, title="apertura")
    assert "facial" not in p1.lower(), p1
    assert "white marble" in p1 and "morning" in p1, p1

    # 2. Ni una negacion de persona en el prompt positivo (CLIP no niega), y
    #    todas en el negativo.
    for brief in ("crema facial antiedad", "perfume de noche", "zapatilla de "
                  "running", "cafe de especialidad"):
        for n, title in ((1, "opening"), (2, "detail"), (3, "closing")):
            text = keyframe_prompt(brief, n=n, title=title).lower()
            assert " no people" not in text and "without people" not in text, text
            # \b: no basta con `not in` — "faceted" contiene "face" y el
            # tokenizer lo parte igual. Se comprueba por inicio de palabra.
            for word in ("woman", "man", "face", "portrait", "model", "person",
                         "girl", "hand", "skin", "people"):
                assert not re.search(rf"\b{word}", text), (word, text)
    for word in ("person", "face", "hands", "text", "logo", "blurry"):
        assert word in NEGATIVE_PROMPT, word

    # 3. Coherencia: las 3 escenas comparten sujeto y ancla exactos.
    briefs = ["serum facial premium, marmol blanco, luz de manana",
              "zapatilla de running ligera para ciudad, asfalto mojado, noche",
              "perfume unisex en frasco de cristal, luz dorada de atardecer",
              "cafe de especialidad tostado, madera y luz de ventana"]
    for brief in briefs:
        spec = parse_brief(brief)
        prompts = [keyframe_prompt(brief, n=i, title=t, total=3)
                   for i, t in ((1, "opening"), (2, "detail"), (3, "closing"))]
        assert all(spec.subject in p for p in prompts), brief
        # Paleta y tratamiento IDENTICOS en las 3 (eso es la coherencia); la
        # luz la comparten apertura y detalle, el heroe la cambia por plato.
        assert all(spec.palette in p for p in prompts), brief
        assert all("35mm film" in p for p in prompts), brief
        assert all(spec.surface in p for p in (prompts[0], prompts[2])), brief
        assert style_anchor(spec) in prompts[0] and style_anchor(spec) in prompts[1]
        assert style_anchor(spec, studio=True) in prompts[2], brief
        # 4. ...y a la vez cuentan un arco: tres planos distintos.
        assert len({p.split(".")[0] for p in prompts}) == 3, brief
        assert "Wide angle establishing" in prompts[0], brief
        assert "Macro 100mm" in prompts[1], brief
        assert "Studio packshot" in prompts[2], brief

    # 5. Briefs distintos -> spots distintos (si no, la demo sale clonada).
    assert len({parse_brief(b).subject for b in briefs}) == 4
    assert len({style_anchor(b) for b in briefs}) == 4

    # 6. Acentos, mayusculas y espacios no cambian el prompt (= cache estable).
    assert (keyframe_prompt("Sérum   FACIAL, Mármol Blanco, luz de mañana")
            == keyframe_prompt("serum facial, marmol blanco, luz de manana"))

    # 7. Un brief de categoria desconocida sigue dando un objeto, no una persona.
    odd = parse_brief("un chisme rarisimo que no esta en el lexico")
    assert odd.generic and "package" in odd.subject, odd

    # 8. La nota del revisor sobrevive a reconstruir el prompt desde el brief.
    hint = ("lo que sea. Reviewer rejected the previous take: la etiqueta no se "
            "lee. Fix exactly that.")
    out = keyframe_prompt(bad, n=2, scene_prompt_hint=hint)
    assert "la etiqueta no se lee" in out, out
    assert "lo que sea" not in out, out

    # 9. Los beats se eligen por posicion cuando el titulo lo inventa un LLM.
    assert beat_role(1, "Golden Dawn", 3) == "open"
    assert beat_role(2, "Golden Dawn", 3) == "detail"
    assert beat_role(3, "Golden Dawn", 3) == "hero"

    print("prompts OK: sujeto siempre objeto, ancla derivada del brief y "
          "compartida por las 3 escenas, arco open/detail/hero, sin negaciones "
          "de persona en el prompt positivo y fidelidad al brief (superficie, "
          "luz y paleta).")


if __name__ == "__main__":
    demo()
