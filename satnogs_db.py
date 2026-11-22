"""Cliente minimo para la API REST de SatNOGS DB (https://db.satnogs.org/api/).

Endpoints usados:
  /api/satellites/    catalogo de satelites (estado, nombres, NORAD)
  /api/transmitters/  transmisores (frecuencias de bajada/subida, modo, baudios, estado)
  /api/tle/           ultimo TLE de cada satelite (fuente Space-Track, CelesTrak, etc.)
  /api/telemetry/     tramas de telemetria recibidas por la red (requiere token, paginada por cursor)

Autenticacion: cabecera  "Authorization: Token <API_KEY>".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

BASE = "https://db.satnogs.org/api"


def cargar_env(ruta: Path) -> dict:
    datos = {}
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            datos[k.strip()] = v.strip().strip('"').strip("'")
    return datos


def obtener_token(carpeta: Path) -> str | None:
    env = cargar_env(carpeta / ".env")
    for k in ("SATNOGS_API_KEY", "SATGNOS_API_KEY", "SATNOGS_TOKEN"):
        if env.get(k) or os.environ.get(k):
            return env.get(k) or os.environ.get(k)
    return None


class SatnogsDB:
    def __init__(self, token: str | None, cache_dir: Path, cache_horas: float = 6):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "estacion-terrena-satnogs/1.0"
        if token:
            self.s.headers["Authorization"] = f"Token {token}"
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = cache_horas * 3600

    def _get(self, endpoint: str, params: dict | None = None, url: str | None = None):
        p = {"format": "json", **(params or {})}
        for intento in range(4):
            r = self.s.get(url or f"{BASE}/{endpoint}/", params=None if url else p, timeout=180)
            if r.status_code == 429:  # limite de peticiones
                time.sleep(int(r.headers.get("Retry-After", 10 * (intento + 1))))
                continue
            if r.status_code == 401:
                raise RuntimeError("SatNOGS DB respondio 401: revisa la API key en .env")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"SatNOGS DB: demasiados reintentos en {endpoint}")

    def _lista_cacheada(self, nombre: str, forzar: bool) -> list:
        f = self.cache_dir / f"{nombre}.json"
        if not forzar and f.exists() and time.time() - f.stat().st_mtime < self.ttl:
            return json.loads(f.read_text(encoding="utf-8"))
        datos = self._get(nombre)
        f.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        return datos

    def satelites(self, forzar=False) -> list:
        return self._lista_cacheada("satellites", forzar)

    def transmisores(self, forzar=False) -> list:
        return self._lista_cacheada("transmitters", forzar)

    def tles(self, forzar=False) -> list:
        return self._lista_cacheada("tle", forzar)

    def telemetria(self, norad: int, limite: int = 50) -> list:
        """Ultimas tramas de telemetria de un satelite (mas recientes primero)."""
        out, url = [], None
        datos = self._get("telemetry", {"satellite": norad})
        while True:
            out.extend(datos.get("results", []))
            url = datos.get("next")
            if len(out) >= limite or not url:
                return out[:limite]
            datos = self._get("telemetry", url=url)
