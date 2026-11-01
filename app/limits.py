"""Límites globales para una demo de una sola instancia, sin confiar en IPs del cliente."""

from collections import deque
from time import monotonic

from starlette.responses import JSONResponse


TOO_LARGE = "El mensaje es demasiado largo. Reduce el texto o inicia otra conversación."


class RequestLimits:
    """Acota tamaño, concurrencia y frecuencia de todos los POST.

    ``route_limits`` = {ruta: (bytes, mensaje)} da a rutas concretas (p. ej. la subida de
    oficios) su propio tope de cuerpo; la concurrencia y el presupuesto por minuto son los
    mismos para todas.
    """

    def __init__(self, app, max_bytes=65536, concurrent=2, per_minute=30, route_limits=None):
        self.app = app
        self.max_bytes = max_bytes
        self.concurrent = concurrent
        self.per_minute = per_minute
        self.route_limits = dict(route_limits or {})
        self.active = 0
        self.recent = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)

        async def reject(status, message):
            headers = {"Retry-After": "60"} if status == 429 else {}
            await JSONResponse({"message": message}, status_code=status, headers=headers)(scope, receive, send)

        now = monotonic()
        while self.recent and self.recent[0] <= now - 60:
            self.recent.popleft()
        if self.active >= self.concurrent or len(self.recent) >= self.per_minute:
            return await reject(429, "La demo está ocupada. Intenta de nuevo en un minuto.")
        self.active += 1
        self.recent.append(now)
        try:
            max_bytes, too_large = self.route_limits.get(scope.get("path", ""), (self.max_bytes, TOO_LARGE))
            declared = dict(scope.get("headers") or []).get(b"content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                return await reject(413, too_large)  # sin leer un cuerpo que ya sabemos excesivo
            # Lee y acota también cuerpos chunked antes de que el parser JSON los cargue.
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > max_bytes:
                    return await reject(413, too_large)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            # La plaza permanece ocupada hasta terminar o cancelar el stream SSE.
            await self.app(scope, replay, send)
        finally:
            self.active -= 1


def validate_document_data(value, depth=0):
    """Acota campos antes de construir PDFs; evita textos y listas que agotan CPU/disco."""
    if depth > 4:
        raise ValueError("Demasiados niveles en los datos del documento")
    if isinstance(value, str) and (len(value) > 4000 or value.count("\n") > 50):
        raise ValueError("Cada campo admite hasta 4000 caracteres y 50 saltos de línea")
    if isinstance(value, (list, dict)):
        if len(value) > 40:
            raise ValueError("Demasiados campos o elementos en el documento")
        for item in value.values() if isinstance(value, dict) else value:
            validate_document_data(item, depth + 1)
    return value
