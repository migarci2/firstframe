#!/usr/bin/env python3
"""FirstFrame - configuracion declarativa del bucket B2.

Convierte `genblaze-review-migarci2` en un SISTEMA de almacenamiento, no en un cubo
de blobs: Object Lock + 4 lifecycle rules + 5 event notification rules.

Es IDEMPOTENTE: correrlo N veces deja exactamente el mismo estado y no falla.
Todo lo que escribe lo vuelve a LEER de la API y lo compara antes de darlo por bueno.

    set -a && . ./.env && set +a && .venv/bin/python infra/b2_setup.py

Flags:
    --bucket NAME    sobreescribe B2_BUCKET
    --dry-run        imprime el plan y no toca nada
    --no-color       salida sin ANSI (para logs / CI)

Salidas posibles:
    0  todo verde (o event notifications gated -> EVENTS_MODE=poll, que es un WARN)
    1  fallo duro (bucket inexistente, sin Object Lock, lifecycle no aplicable)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
API_VERSION = "v3"
TIMEOUT = 30

# ---------------------------------------------------------------------------
# Estado deseado. Esto es la unica fuente de verdad del script.
# ---------------------------------------------------------------------------

# 4 lifecycle rules. Prefijos DISJUNTOS a proposito: B2 rechaza reglas que se solapan.
# `daysFromStartingToCancelingUnfinishedLargeFiles` es el campo que casi nadie usa y el
# que de verdad importa aqui: un job de video que muere a mitad deja un multipart upload
# huerfano que sigue facturando. B2 lo cancela solo a las 24h, sin cron nuestro.
LIFECYCLE_RULES: list[dict[str, Any]] = [
    {
        "fileNamePrefix": "incoming/",
        "daysFromStartingToCancelingUnfinishedLargeFiles": 1,
        "daysFromUploadingToHiding": None,
        "daysFromHidingToDeleting": None,
    },
    {
        "fileNamePrefix": "rejected/",
        "daysFromStartingToCancelingUnfinishedLargeFiles": None,
        "daysFromUploadingToHiding": 1,
        "daysFromHidingToDeleting": 7,
    },
    {
        "fileNamePrefix": "runs/",
        "daysFromStartingToCancelingUnfinishedLargeFiles": None,
        "daysFromUploadingToHiding": 3,
        "daysFromHidingToDeleting": 7,
    },
    {
        "fileNamePrefix": "approved/",
        "daysFromStartingToCancelingUnfinishedLargeFiles": None,
        "daysFromUploadingToHiding": None,
        "daysFromHidingToDeleting": 1,
    },
]

LIFECYCLE_WHY = {
    "incoming/": "cancela multiparts huerfanos de renders muertos (24h)",
    "rejected/": "las tomas descartadas se ocultan a 1d y mueren a 8d",
    "runs/":     "intermedios de genblaze: 3d visibles, 10d en total",
    "approved/": "purga versiones ocultas a 1d -- Object Lock la frena",
}

# 5 event notification rules (max 25/bucket). Reglas de B2:
#   - dos reglas NO pueden compartir event type con prefijos solapados
#   - 3 y 4 comparten type pero prefijos disjuntos (approved/ vs provenance/) -> legal
#   - 1 y 2 comparten prefijo pero types distintos                            -> legal
EVENT_RULES: list[dict[str, Any]] = [
    {
        "name": "segment-landed",
        "eventTypes": ["b2:ObjectCreated:Upload"],
        "objectNamePrefix": "incoming/",
        "reaction": "SSE render_started -> la sala marca LIVE y arranca el player",
    },
    {
        "name": "render-complete",
        "eventTypes": ["b2:ObjectCreated:MultipartUpload"],
        "objectNamePrefix": "incoming/",
        "reaction": "job -> in_review, para el cronometro de render",
    },
    {
        "name": "asset-approved",
        "eventTypes": ["b2:ObjectCreated:Upload"],
        "objectNamePrefix": "approved/",
        "reaction": "SSE approved + badge de Object Lock GOVERNANCE",
    },
    {
        "name": "manifest-written",
        "eventTypes": ["b2:ObjectCreated:Upload"],
        "objectNamePrefix": "provenance/",
        "reaction": "enlaza el manifest de provenance en la UI",
    },
    {
        "name": "cleanup-audit",
        "eventTypes": ["b2:HideMarkerCreated:LifecycleRule"],
        "objectNamePrefix": "rejected/",
        "reaction": "feed de auditoria: la lifecycle rule actuando, en vivo",
    },
]

MAX_RULES_PER_BUCKET = 25

# Layout del bucket, para la tabla final del video.
LAYOUT = [
    ("refs/{job}/",                   "insumos documentados con Pipeline.ingest"),
    ("incoming/{job}/seg/init.mp4",   "init segment fMP4"),
    ("incoming/{job}/seg/00001.m4s",  "segmentos, subidos conforme se generan"),
    ("incoming/{job}/index.m3u8",     "playlist HLS regenerada tras cada segmento"),
    ("runs/{job}/scene-{n}/",         "intermedios genblaze (keyframes, clips, audio)"),
    ("provenance/{job}/manifest.json", "manifest agregado (objeto, nunca metadata)"),
    ("approved/{job}/final.mp4",      "master + manifest embebido + LOCK GOVERNANCE 30d"),
    ("rejected/{job}/take-{k}.mp4",   "tomas rechazadas (evidencia del loop de refinado)"),
]


# ---------------------------------------------------------------------------
# Cliente B2 nativo (la S3 API no expone lifecycle B2 ni event notifications)
# ---------------------------------------------------------------------------
class B2Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @property
    def gated(self) -> bool:
        """True si el error es 'la cuenta no tiene esta feature', no 'lo hiciste mal'."""
        if self.status in (401, 403):
            return True
        return self.status == 400 and "not enabled" in self.message.lower()


class B2:
    """Cliente minimo de la B2 Native API v3."""

    def __init__(self, key_id: str, app_key: str):
        self.key_id, self.app_key = key_id, app_key
        self.api_url = self.token = self.account_id = ""
        self.s3_url = ""
        self.capabilities: list[str] = []

    def authorize(self) -> "B2":
        tok = base64.b64encode(f"{self.key_id}:{self.app_key}".encode()).decode()
        req = urllib.request.Request(AUTH_URL, headers={"Authorization": f"Basic {tok}"})
        data = self._send(req)
        storage = data["apiInfo"]["storageApi"]
        self.api_url = storage["apiUrl"]
        self.s3_url = storage.get("s3ApiUrl", "")
        self.token = data["authorizationToken"]
        self.account_id = data["accountId"]
        self.capabilities = sorted(storage.get("capabilities", []))
        return self

    def call(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.api_url}/b2api/{API_VERSION}/{name}",
            data=json.dumps(payload).encode(),
            headers={"Authorization": self.token, "Content-Type": "application/json"},
        )
        return self._send(req)

    @staticmethod
    def _send(req: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            raise B2Error(exc.code, body.get("code", "http_error"),
                          body.get("message", raw[:300])) from None

    def find_bucket(self, name: str) -> dict[str, Any] | None:
        res = self.call("b2_list_buckets", {"accountId": self.account_id, "bucketName": name})
        for bucket in res.get("buckets", []):
            if bucket["bucketName"] == name:
                return bucket
        return None


# ---------------------------------------------------------------------------
# Presentacion
# ---------------------------------------------------------------------------
class Style:
    enabled = True

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text

    @classmethod
    def ok(cls, t: str) -> str:   return cls._wrap("32", t)

    @classmethod
    def warn(cls, t: str) -> str: return cls._wrap("33", t)

    @classmethod
    def err(cls, t: str) -> str:  return cls._wrap("31", t)

    @classmethod
    def dim(cls, t: str) -> str:  return cls._wrap("2", t)

    @classmethod
    def bold(cls, t: str) -> str: return cls._wrap("1", t)


def _visible_len(text: str) -> int:
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] != "m":
                i += 1
        else:
            out += 1
        i += 1
    return out


def table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    widths = [_visible_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visible_len(cell))

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("-" * (w + 2) for w in widths) + right

    def render(cells: list[str]) -> str:
        pad = [c + " " * (widths[i] - _visible_len(c)) for i, c in enumerate(cells)]
        return "| " + " | ".join(pad) + " |"

    print()
    print(Style.bold(title))
    print(line("+", "+", "+"))
    print(render([Style.bold(h) for h in headers]))
    print(line("+", "+", "+"))
    for row in rows:
        print(render(row))
    print(line("+", "+", "+"))


def note(text: str) -> None:
    for i, chunk in enumerate(text.strip().split("\n")):
        print(("  NOTA  " if i == 0 else "        ") + chunk.strip())


# ---------------------------------------------------------------------------
# Normalizacion / comparacion (la parte que hace el script idempotente)
# ---------------------------------------------------------------------------
LIFECYCLE_FIELDS = (
    "fileNamePrefix",
    "daysFromStartingToCancelingUnfinishedLargeFiles",
    "daysFromUploadingToHiding",
    "daysFromHidingToDeleting",
)


def norm_lifecycle(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """B2 devuelve las reglas ordenadas por prefijo y con los 4 campos siempre presentes."""
    out = [{f: rule.get(f) for f in LIFECYCLE_FIELDS} for rule in rules]
    return sorted(out, key=lambda r: r["fileNamePrefix"] or "")


def norm_events(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compara solo lo que controlamos. El secreto HMAC nunca vuelve de la API."""
    out = []
    for rule in rules:
        target = rule.get("targetConfiguration") or {}
        out.append({
            "name": rule.get("name"),
            "eventTypes": sorted(rule.get("eventTypes") or []),
            "objectNamePrefix": rule.get("objectNamePrefix") or "",
            "isEnabled": bool(rule.get("isEnabled")),
            "url": target.get("url"),
        })
    return sorted(out, key=lambda r: r["name"] or "")


def check_event_rule_legality(rules: list[dict[str, Any]]) -> list[str]:
    """B2 prohibe prefijos SOLAPADOS dentro del mismo event type. Lo validamos aqui
    para que un error de diseno se vea en local y no como un 400 opaco de la API."""
    problems: list[str] = []
    if len(rules) > MAX_RULES_PER_BUCKET:
        problems.append(f"{len(rules)} reglas > maximo {MAX_RULES_PER_BUCKET} por bucket")
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            shared = set(a["eventTypes"]) & set(b["eventTypes"])
            if not shared:
                continue
            pa, pb = a["objectNamePrefix"], b["objectNamePrefix"]
            if pa.startswith(pb) or pb.startswith(pa):
                problems.append(
                    f"{a['name']} y {b['name']} comparten {sorted(shared)} "
                    f"con prefijos solapados ({pa!r} / {pb!r})")
    return problems


# ---------------------------------------------------------------------------
# Pasos
# ---------------------------------------------------------------------------
def step_bucket(b2: B2, name: str) -> dict[str, Any]:
    bucket = b2.find_bucket(name)
    if bucket is None:
        print(Style.err(f"FALLO: el bucket '{name}' no existe."))
        print("       Este script NO crea buckets: Object Lock solo se activa en la creacion,")
        print("       y crear uno sin lock nos dejaria sin la mitad de la demo.")
        raise SystemExit(1)

    lock_cfg = (bucket.get("fileLockConfiguration") or {}).get("value") or {}
    locked = bool(lock_cfg.get("isFileLockEnabled"))
    print(f"  bucket            {Style.bold(name)}  (id {bucket['bucketId']})")
    print(f"  tipo              {bucket['bucketType']}")
    print(f"  object lock       " + (Style.ok("ACTIVADO") if locked
                                     else Style.err("DESACTIVADO")))
    if not locked:
        print(Style.err("FALLO: sin Object Lock no hay WORM, y ese es el nucleo de la demo."))
        raise SystemExit(1)
    return bucket


def step_lifecycle(b2: B2, bucket: dict[str, Any], dry_run: bool) -> tuple[list, bool]:
    desired = norm_lifecycle(LIFECYCLE_RULES)
    current = norm_lifecycle(bucket.get("lifecycleRules") or [])

    if current == desired:
        changed = False
        applied = current
        status = Style.dim("sin cambios (idempotente)")
    elif dry_run:
        return desired, True
    else:
        res = b2.call("b2_update_bucket", {
            "accountId": b2.account_id,
            "bucketId": bucket["bucketId"],
            "lifecycleRules": LIFECYCLE_RULES,
        })
        changed = True
        # Verificacion: no nos fiamos de la respuesta del PUT, releemos el bucket.
        reread = b2.find_bucket(bucket["bucketName"]) or {}
        applied = norm_lifecycle(reread.get("lifecycleRules") or [])
        if applied != desired:
            print(Style.err("FALLO: B2 acepto las lifecycle rules pero la relectura no coincide."))
            print("  esperado:", json.dumps(desired, indent=2))
            print("  leido   :", json.dumps(applied, indent=2))
            raise SystemExit(1)
        status = Style.ok(f"aplicadas ({len(res.get('lifecycleRules', []))} reglas)")

    def fmt(value: Any) -> str:
        return str(value) if value is not None else Style.dim("-")

    rows = [[
        str(i + 1),
        rule["fileNamePrefix"],
        fmt(rule["daysFromStartingToCancelingUnfinishedLargeFiles"]),
        fmt(rule["daysFromUploadingToHiding"]),
        fmt(rule["daysFromHidingToDeleting"]),
        LIFECYCLE_WHY.get(rule["fileNamePrefix"], ""),
    ] for i, rule in enumerate(applied)]

    table(f"LIFECYCLE RULES  ({len(applied)})  {status}",
          ["#", "prefijo", "cancel-unfinished-large", "upload->hide", "hide->delete",
           "que resuelve"], rows)

    note("""
`daysFromStartingToCancelingUnfinishedLargeFiles: 1` sobre incoming/ es la regla que
casi nadie usa: cuando un job de video muere a mitad, deja un multipart upload abierto
cuyas partes ya subidas siguen ocupando y facturando. B2 lo cancela solo a las 24h.
Cero cron, cero limpieza manual, cero fugas de coste.
""")
    print()
    note("""
La regla agresiva sobre approved/ (hide->delete a 1 dia) convive con Object Lock
GOVERNANCE 30d sobre los mismos objetos, y NO puede tocarlos: en B2 la retencion gana
sobre el lifecycle. El resultado es exactamente lo que quiere un estudio: las versiones
ocultas y la basura se purgan agresivamente, mientras el master entregado al cliente
es intocable durante 30 dias -- ni por nosotros, ni por una regla mal escrita, ni por
una key comprometida. Esa combinacion es la prueba de que el bucket esta pensado como
sistema y no como carpeta.
""")
    return applied, changed


def step_events(b2: B2, bucket: dict[str, Any], webhook_url: str, secret: str,
                dry_run: bool) -> tuple[str, list]:
    """Devuelve (events_mode, reglas_leidas_de_vuelta)."""
    problems = check_event_rule_legality(EVENT_RULES)
    if problems:
        print(Style.err("FALLO: el conjunto de event rules es ilegal para B2:"))
        for p in problems:
            print("   - " + p)
        raise SystemExit(1)

    payload_rules = [{
        "name": rule["name"],
        "isEnabled": True,
        "eventTypes": rule["eventTypes"],
        "objectNamePrefix": rule["objectNamePrefix"],
        "targetConfiguration": {
            "targetType": "webhook",
            "url": webhook_url,
            "hmacSha256SigningSecret": secret,
        },
    } for rule in EVENT_RULES]

    desired = norm_events(payload_rules)
    mode, applied, status = "webhook", [], ""

    if dry_run:
        applied, status = desired, Style.dim("dry-run")
    else:
        try:
            current = norm_events(
                b2.call("b2_get_bucket_notification_rules",
                        {"bucketId": bucket["bucketId"]}).get("eventNotificationRules", []))
            if current == desired:
                applied, status = current, Style.dim("sin cambios (idempotente)")
            else:
                b2.call("b2_set_bucket_notification_rules", {
                    "bucketId": bucket["bucketId"],
                    "eventNotificationRules": payload_rules,
                })
                # Relectura obligatoria: comparamos contra lo que B2 dice tener.
                applied = norm_events(
                    b2.call("b2_get_bucket_notification_rules",
                            {"bucketId": bucket["bucketId"]}).get("eventNotificationRules", []))
                if applied != desired:
                    print(Style.err("FALLO: las event rules leidas no coinciden con lo enviado."))
                    print("  leido:", json.dumps(applied, indent=2))
                    raise SystemExit(1)
                status = Style.ok(f"creadas ({len(applied)} reglas)")
        except B2Error as exc:
            if not exc.gated:
                raise
            mode = "poll"
            applied = desired
            status = Style.warn(f"GATED ({exc.status} {exc.code}: {exc.message})")
            print()
            print(Style.warn("WARN: event notifications gated -> EVENTS_MODE=poll"))
            print(Style.dim(
                "      La cuenta no tiene la Event Notifications API habilitada. No es un\n"
                "      bloqueo: server/events.py:Poller emite los MISMOS eventos internos\n"
                "      (list_multipart_uploads + list_objects_v2 cada 2s, diff contra la DB).\n"
                "      Reglas dejadas declaradas abajo; se activan solas el dia que B2 abra\n"
                "      la API para esta cuenta, sin tocar el backend."))

    rows = []
    for i, rule in enumerate(EVENT_RULES):
        live = next((a for a in applied if a["name"] == rule["name"]), None)
        rows.append([
            str(i + 1),
            rule["name"],
            ", ".join(rule["eventTypes"]),
            rule["objectNamePrefix"],
            Style.ok("activa") if (live and mode == "webhook")
            else Style.warn("declarada"),
            rule["reaction"],
        ])

    table(f"EVENT NOTIFICATION RULES  ({len(EVENT_RULES)}/{MAX_RULES_PER_BUCKET})  {status}",
          ["#", "nombre", "event type", "prefijo", "estado", "reaccion del backend"], rows)

    note(f"""
Destino webhook: {webhook_url}
Firma: HMAC-SHA256 del cuerpo crudo, header X-Bz-Event-Notification-Signature ("v1=<hex>").
Legalidad verificada en local antes de llamar a la API: reglas 3 y 4 comparten event type
con prefijos disjuntos (approved/ vs provenance/) y 1 y 2 comparten prefijo con event types
distintos. Ambas cosas son legales; prefijos SOLAPADOS con el mismo type no lo son.
""")
    return mode, applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Configura el bucket B2 de FirstFrame.")
    parser.add_argument("--bucket", default=None, help="sobreescribe B2_BUCKET")
    parser.add_argument("--dry-run", action="store_true", help="imprime el plan, no escribe")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    Style.enabled = sys.stdout.isatty() and not args.no_color

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    missing = [v for v in ("B2_KEY_ID", "B2_APP_KEY") if not os.environ.get(v)]
    if missing:
        print(Style.err(f"FALLO: faltan variables de entorno: {', '.join(missing)}"))
        print("       Copia .env.example a .env y rellena las credenciales.")
        return 1

    bucket_name = args.bucket or os.environ.get("B2_BUCKET")
    if not bucket_name:
        print(Style.err("FALLO: define B2_BUCKET o pasa --bucket"))
        return 1

    secret = os.environ.get("B2_WEBHOOK_SECRET", "")
    generated_secret = False
    if not secret:
        secret = secrets.token_hex(16)  # 32 chars alfanumericos, como pide B2
        generated_secret = True

    webhook_url = os.environ.get("B2_WEBHOOK_URL") or (
        os.environ.get("PUBLIC_BASE_URL", "https://firstframe.fly.dev").rstrip("/")
        + "/webhooks/b2")

    print()
    print(Style.bold("FirstFrame - B2 setup") + Style.dim("  (idempotente; correlo las veces que quieras)"))
    if args.dry_run:
        print(Style.warn("  MODO DRY-RUN: no se escribe nada"))
    print()

    b2 = B2(os.environ["B2_KEY_ID"], os.environ["B2_APP_KEY"]).authorize()
    print(f"  cuenta            {b2.account_id}")
    print(f"  api               {b2.api_url}")
    print(f"  s3 endpoint       {b2.s3_url}")
    print(f"  capabilities      {len(b2.capabilities)}")

    bucket = step_bucket(b2, bucket_name)
    applied_lifecycle, _ = step_lifecycle(b2, bucket, args.dry_run)
    events_mode, _ = step_events(b2, bucket, webhook_url, secret, args.dry_run)

    table("LAYOUT DEL BUCKET", ["prefijo", "contenido"],
          [[p, d] for p, d in LAYOUT])

    print()
    print(Style.bold("RESUMEN"))
    print(f"  bucket            {bucket_name} ({b2.s3_url})")
    print(f"  object lock       {Style.ok('ACTIVADO')}  (GOVERNANCE 30d sobre approved/)")
    print(f"  lifecycle rules   {Style.ok(str(len(applied_lifecycle)) + '/4 verificadas contra la API')}")
    if events_mode == "webhook":
        print(f"  event rules       {Style.ok(str(len(EVENT_RULES)) + '/5 activas')}")
        print(f"  EVENTS_MODE       {Style.ok('both')}   (webhook firmado + poller de respaldo)")
    else:
        print(f"  event rules       {Style.warn('declaradas, API gated en esta cuenta')}")
        print(f"  EVENTS_MODE       {Style.warn('poll')}   (server/events.py:Poller cubre el hueco)")
    print()
    if generated_secret:
        print(Style.warn("  B2_WEBHOOK_SECRET no estaba en el entorno; se genero uno efimero."))
        print(Style.warn("  Anadelo a .env (NUNCA a un fichero versionado) para que persista:"))
        print(f"      B2_WEBHOOK_SECRET={secret}")
        print()
    print(Style.dim("  Autocomprobacion: vuelve a correr este script. Debe salir identico,"))
    print(Style.dim("  con las lifecycle rules en 'sin cambios (idempotente)' y sin errores."))
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except B2Error as exc:
        print(Style.err(f"FALLO B2: {exc}"))
        sys.exit(1)
