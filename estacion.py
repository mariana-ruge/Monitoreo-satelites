"""Estacion terrena: SatNOGS DB + Orbitron + WiSPDDE.

Uso rapido (desde esta carpeta):
  python estacion.py actualizar          # baja TLE/transmisores de SatNOGS y los carga en Orbitron
  python estacion.py pases --horas 12    # pases sobre la estacion
  python estacion.py programa            # plan de apuntamiento (que hacer y cuando)
  python estacion.py ahora               # que hay en el cielo en este momento
  python estacion.py seguir              # seguimiento en vivo (Az/El + Doppler) por terminal
  python estacion.py mapa --abrir        # mapa HTML con trazas y huellas
  python estacion.py orbitron            # abre Orbitron con el driver WiSPDDE
  python estacion.py dde-monitor         # muestra lo que Orbitron envia a WiSPDDE
  python estacion.py buscar landsat      # buscar satelites en SatNOGS DB
  python estacion.py telemetria 25544    # ultimas tramas de telemetria
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skyfield.api import EarthSatellite, load, wgs84

from orbitron import Orbitron, formatear_tracking, lanzar_wispdde, monitor_dde, servidor_dde
from satnogs_db import SatnogsDB, obtener_token

AQUI = Path(__file__).resolve().parent
DATA = AQUI / "data"
SALIDA = AQUI / "salida"
C_KMS = 299792.458
TS = load.timescale()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ======================================================================
# Configuracion
# ======================================================================
def cargar_config() -> dict:
    return json.loads((AQUI / "config.json").read_text(encoding="utf-8"))


def estacion(cfg: dict, orb: Orbitron) -> dict:
    e = dict(cfg["estacion"])
    if e.get("lat") is None or e.get("lon") is None:
        loc = orb.ubicacion()
        if not loc:
            sys.exit("Define lat/lon de la estacion en config.json (no se pudo leer de Orbitron).")
        e.update({k: loc[k] for k in ("lat", "lon", "alt_m")})
    e["alt_m"] = e.get("alt_m") or 0
    return e


def hora_local(t) -> str:
    dt = t if isinstance(t, datetime) else t.utc_datetime()
    return dt.astimezone().strftime("%m-%d %H:%M:%S")


# ======================================================================
# Catalogo desde SatNOGS DB
# ======================================================================
def modo_wisp(modo: str | None) -> str:
    """Traduce el modo de SatNOGS a uno que entienden Orbitron/WiSPDDE (USB LSB CW FM)."""
    m = (modo or "").upper()
    if m.startswith("CW"):
        return "CW"
    if m in ("SSB", "USB") or any(x in m for x in ("BPSK", "QPSK", "LORA", "LINEAR")):
        return "USB"
    if m == "LSB":
        return "LSB"
    return "FM"


def en_bandas(hz, bandas) -> bool:
    return bool(hz) and any(lo <= hz / 1e6 <= hi for lo, hi in bandas)


def construir_catalogo(db: SatnogsDB, cfg: dict, forzar=False) -> list[dict]:
    bandas = cfg["bandas_mhz"]
    objetivo = cfg.get("satelites_objetivo", [])
    sats = {s["norad_cat_id"]: s for s in db.satelites(forzar) if s.get("norad_cat_id")}
    tles = {t["norad_cat_id"]: t for t in db.tles(forzar)}
    tx = defaultdict(list)
    for t in db.transmisores(forzar):
        if not (t.get("alive") and t.get("status") == "active"):
            continue
        dn = t.get("downlink_drifted") or t.get("downlink_low")
        up = t.get("uplink_drifted") or t.get("uplink_low")
        tx[t["norad_cat_id"]].append({
            "desc": (t.get("description") or "")[:60], "dn_hz": dn, "up_hz": up,
            "modo": t.get("mode") or "", "modo_wisp": modo_wisp(t.get("mode")),
            "baud": t.get("baud"), "tipo": t.get("type"), "invertido": t.get("invert"),
            "en_banda": en_bandas(dn, bandas) or en_bandas(up, bandas),
            "sin_confirmar": t.get("unconfirmed", False),
        })
    cat = []
    for norad, tle in tles.items():
        s = sats.get(norad, {})
        if norad > 99999 or s.get("status") not in ("in orbit", None):
            continue
        txs = tx.get(norad, [])
        utiles = [t for t in txs if t["en_banda"]]
        if not utiles and norad not in objetivo:
            continue
        utiles.sort(key=lambda t: (t["sin_confirmar"], not en_bandas(t["dn_hz"], bandas)))
        nombre = s.get("name") or tle["tle0"].lstrip("0 ").strip()
        cat.append({
            "norad": norad, "nombre": nombre, "nombre_orbitron": nombre.replace('"', ""),
            "estado": s.get("status", ""), "tle1": tle["tle1"], "tle2": tle["tle2"],
            "tle_fuente": tle.get("tle_source"), "tle_actualizado": tle.get("updated"),
            "transmisores": utiles or txs, "principal": (utiles or txs or [None])[0],
            "prioridad": (len(objetivo) - objetivo.index(norad)) if norad in objetivo else 0,
        })
    cat.sort(key=lambda s: (-s["prioridad"], s["nombre"]))
    return cat


def abrir_db(cfg) -> SatnogsDB:
    token = obtener_token(AQUI)
    if not token:
        print("Aviso: no hay API key en .env (SATNOGS_API_KEY); la telemetria no funcionara.")
    return SatnogsDB(token, DATA, cfg.get("cache_horas", 6))


# ======================================================================
# Calculo orbital (skyfield / SGP4)
# ======================================================================
def objetos(cat):
    return [(s, EarthSatellite(s["tle1"], s["tle2"], s["nombre"], TS)) for s in cat]


def calcular_pases(cat, est, horas: float, min_el: float, desde: datetime | None = None) -> list[dict]:
    topo = wgs84.latlon(est["lat"], est["lon"], elevation_m=est["alt_m"])
    t0 = TS.from_datetime(desde or datetime.now(timezone.utc))
    t1 = TS.from_datetime(t0.utc_datetime() + timedelta(hours=horas))
    pases = []
    for s, sat in objetos(cat):
        try:
            t, ev = sat.find_events(topo, t0, t1, altitude_degrees=min_el)
        except Exception:
            continue
        if len(ev) == 0:
            alt = (sat - topo).at(t0).altaz()[0].degrees
            if alt > min_el:  # geoestacionario o siempre visible
                pases.append(_pase(s, sat, topo, t0, None, t1, continuo=True))
            continue
        aos = tca = None
        if ev[0] != 0:
            aos = t0  # ya estaba sobre el horizonte
        for ti, e in zip(t, ev):
            if e == 0:
                aos, tca = ti, None
            elif e == 1:
                tca = ti if (tca is None or (sat - topo).at(ti).altaz()[0].degrees >
                             (sat - topo).at(tca).altaz()[0].degrees) else tca
            elif e == 2 and aos is not None:
                pases.append(_pase(s, sat, topo, aos, tca, ti))
                aos = tca = None
        if aos is not None:
            pases.append(_pase(s, sat, topo, aos, tca, t1))
    pases.sort(key=lambda p: p["aos"])
    return pases


def _pase(s, sat, topo, aos, tca, los, continuo=False) -> dict:
    if tca is None:
        tca = TS.tt_jd((aos.tt + los.tt) / 2)
    alt, az, _ = (sat - topo).at(TS.tt_jd([aos.tt, tca.tt, los.tt])).altaz()
    return {"sat": s, "aos": aos.utc_datetime(), "tca": tca.utc_datetime(), "los": los.utc_datetime(),
            "az_aos": az.degrees[0], "az_tca": az.degrees[1], "az_los": az.degrees[2],
            "el_max": alt.degrees[1], "continuo": continuo}


def estado(sat, topo, t, f_dn=None, f_up=None) -> dict:
    """Az/El/distancia y frecuencias corregidas por Doppler en el instante t."""
    pos = (sat - topo).at(t)
    alt, az, dist = pos.altaz()
    rr = pos.frame_latlon_and_rates(topo)[5].km_per_s
    k = rr / C_KMS
    return {"az": az.degrees, "el": alt.degrees, "dist_km": dist.km, "rr_kms": rr,
            "dn": f_dn * (1 - k) if f_dn else None,      # lo que se recibe
            "up": f_up * (1 + k) if f_up else None}      # lo que hay que transmitir


def cruza_norte(sat, topo, p) -> bool:
    n = 40
    ts_ = TS.from_datetimes([p["aos"] + (p["los"] - p["aos"]) * i / n for i in range(n + 1)])
    az = (sat - topo).at(ts_).altaz()[1].degrees
    return any(abs(a - b) > 180 for a, b in zip(az[:-1], az[1:]))


def planificar(pases, margen_s: float, solo_prioritarios: bool) -> list[dict]:
    """Elige pases sin solaparse (una antena): primero prioridad, luego elevacion maxima."""
    cand = [p for p in pases if not p["continuo"] and (p["sat"]["prioridad"] > 0 or not solo_prioritarios)]
    cand.sort(key=lambda p: (-p["sat"]["prioridad"], -p["el_max"]))
    m = timedelta(seconds=margen_s)
    elegidos = []
    for p in cand:
        if all(p["los"] + m <= q["aos"] or q["los"] + m <= p["aos"] for q in elegidos):
            elegidos.append(p)
    return sorted(elegidos, key=lambda p: p["aos"])


def fmt_f(hz) -> str:
    return f"{hz / 1e6:.4f} MHz" if hz else "-"


# ======================================================================
# Comandos
# ======================================================================
def cmd_actualizar(a, cfg, orb):
    db = abrir_db(cfg)
    print("Descargando satelites, transmisores y TLE de SatNOGS DB ...")
    cat = construir_catalogo(db, cfg, forzar=True)
    (DATA / "catalogo.json").write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(cat)} satelites en orbita con transmisores activos en tus bandas {cfg['bandas_mhz']}")
    objetivo = [s for s in cat if s["prioridad"] > 0]
    faltan = set(cfg.get("satelites_objetivo", [])) - {s["norad"] for s in objetivo}
    if faltan:
        print(f"  Aviso: sin TLE en SatNOGS para NORAD {sorted(faltan)}")

    if not orb.instalado():
        print(f"No se encontro Orbitron en {orb.exe}; solo se actualizo el cache en {DATA}")
        return
    if orb.corriendo() and not a.forzar:
        print("Orbitron esta abierto: cierralo y repite (al salir sobrescribe su configuracion), "
              "o usa --forzar.")
        return
    oc = cfg["orbitron"]
    para_orbitron = objetivo if cfg.get("solo_objetivo_en_orbitron") else cat
    p = orb.escribir_tle(oc["archivo_tle"], para_orbitron)
    n = orb.escribir_notas(para_orbitron)
    orb.escribir_radio(para_orbitron)
    seguidos = [s["norad"] for s in objetivo] or [s["norad"] for s in cat[:20]]
    resp = orb.configurar(oc["archivo_tle"], seguidos, seguidos[0] if seguidos else None,
                          oc["driver"], oc.get("autoseleccion_en_aos", True), oc.get("arrancar_driver", True),
                          driver_exe=oc.get("wispdde_exe"))
    print(f"  TLE para Orbitron: {p}  ({len(para_orbitron)} satelites)")
    print(f"  Notas con frecuencias: {n} archivos; frecuencias preseleccionadas en Radio.his")
    print(f"  Setup.cfg actualizado (respaldo: {resp.name}); seguidos: {seguidos}")
    print(f"  Autoseleccion al AOS: {'si' if oc.get('autoseleccion_en_aos', True) else 'no'}; "
          f"driver DDE: {oc['driver']}")
    print("Listo. Ejecuta:  python estacion.py orbitron")


def _catalogo(a, cfg):
    db = abrir_db(cfg)
    cat = construir_catalogo(db, cfg)
    if getattr(a, "norad", None):
        cat = [s for s in cat if s["norad"] in a.norad]
    elif getattr(a, "objetivo", False):
        cat = [s for s in cat if s["prioridad"] > 0]
    return cat


def cmd_pases(a, cfg, orb):
    est = estacion(cfg, orb)
    cat = _catalogo(a, cfg)
    min_el = a.min_el if a.min_el is not None else cfg["elevacion_minima_deg"]
    print(f"Calculando pases de {len(cat)} satelites para {a.horas} h sobre "
          f"{est['lat']:.4f}, {est['lon']:.4f} (el >= {min_el} deg) ...")
    pases = [p for p in calcular_pases(cat, est, a.horas, min_el) if not p["continuo"]]
    pases = [p for p in pases if p["el_max"] >= a.el_max_min]
    print(f"\n{'AOS (local)':15} {'LOS':8} {'ElMax':>5} {'Az AOS>TCA>LOS':>16}  {'NORAD':>6} Satelite / bajada")
    for p in pases[: a.limite]:
        s = p["sat"]
        t = s["principal"] or {}
        marca = "*" if s["prioridad"] else " "
        print(f"{hora_local(p['aos']):15} {hora_local(p['los'])[6:]:8} {p['el_max']:5.1f} "
              f"{p['az_aos']:4.0f}>{p['az_tca']:4.0f}>{p['az_los']:4.0f}  {s['norad']:>6}{marca}"
              f"{s['nombre'][:22]:22} {fmt_f(t.get('dn_hz'))} {t.get('modo', '')}")
    print(f"\n{len(pases)} pases (* = satelite objetivo). Mostrando {min(len(pases), a.limite)}.")
    if a.csv:
        import csv
        SALIDA.mkdir(exist_ok=True)
        f = SALIDA / "pases.csv"
        with f.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["norad", "nombre", "aos_utc", "tca_utc", "los_utc", "el_max", "az_aos", "az_los",
                        "downlink_hz", "modo"])
            for p in pases:
                t = p["sat"]["principal"] or {}
                w.writerow([p["sat"]["norad"], p["sat"]["nombre"], p["aos"].isoformat(), p["tca"].isoformat(),
                            p["los"].isoformat(), round(p["el_max"], 1), round(p["az_aos"]), round(p["az_los"]),
                            t.get("dn_hz"), t.get("modo")])
        print(f"CSV: {f}")


def cmd_programa(a, cfg, orb):
    est = estacion(cfg, orb)
    cat = _catalogo(a, cfg)
    solo_prio = not a.todos and any(s["prioridad"] for s in cat)
    if solo_prio:
        cat = [s for s in cat if s["prioridad"]]
    pases = calcular_pases(cat, est, a.horas, cfg["elevacion_minima_deg"])
    plan = planificar(pases, cfg.get("margen_reapuntado_s", 120), solo_prio)
    topo = wgs84.latlon(est["lat"], est["lon"], elevation_m=est["alt_m"])
    sats = {s["norad"]: o for s, o in objetos(cat)}
    print(f"PLAN DE APUNTAMIENTO - {est.get('nombre', '')} - proximas {a.horas} h "
          f"({'satelites objetivo' if solo_prio else 'todos'}; {len(plan)} pases)\n")
    for i, p in enumerate(plan, 1):
        s, o = p["sat"], sats[p["sat"]["norad"]]
        t = s["principal"] or {}
        e_aos = estado(o, topo, TS.from_datetime(p["aos"]), t.get("dn_hz"), t.get("up_hz"))
        e_tca = estado(o, topo, TS.from_datetime(p["tca"]), t.get("dn_hz"), t.get("up_hz"))
        dur = (p["los"] - p["aos"]).total_seconds() / 60
        print(f"[{i}] {s['nombre']} (NORAD {s['norad']})  duracion {dur:.1f} min, elevacion max {p['el_max']:.1f} deg")
        print(f"    {hora_local(p['aos'] - timedelta(seconds=cfg.get('margen_reapuntado_s', 120)))}"
              f"  PRE-APUNTAR antena a Az {p['az_aos']:.0f} deg, El {cfg['elevacion_minima_deg']} deg")
        if t:
            print(f"                    Sintonizar bajada {fmt_f(e_aos['dn'])} {t['modo_wisp']} "
                  f"(nominal {fmt_f(t.get('dn_hz'))}, {t.get('modo')}"
                  f"{', ' + str(int(t['baud'])) + ' bd' if t.get('baud') else ''})"
                  + (f"; subida {fmt_f(e_aos['up'])}" if t.get("up_hz") else ""))
        print(f"    {hora_local(p['aos'])}  AOS  -> Orbitron/WiSPDDE inician seguimiento")
        print(f"    {hora_local(p['tca'])}  TCA  Az {p['az_tca']:.0f} El {p['el_max']:.0f}  "
              f"bajada {fmt_f(e_tca['dn'])}")
        print(f"    {hora_local(p['los'])}  LOS  Az {p['az_los']:.0f} deg -> liberar antena")
        if cruza_norte(o, topo, p):
            print("    ! La trayectoria cruza el Norte (0/360 deg): revisar flip/tope del rotor.")
        print()


def cmd_ahora(a, cfg, orb):
    est = estacion(cfg, orb)
    cat = _catalogo(a, cfg)
    topo = wgs84.latlon(est["lat"], est["lon"], elevation_m=est["alt_m"])
    t = TS.now()
    visibles = []
    for s, o in objetos(cat):
        tx = s["principal"] or {}
        e = estado(o, topo, t, tx.get("dn_hz"), tx.get("up_hz"))
        if e["el"] >= a.min_el:
            visibles.append((s, e, tx))
    visibles.sort(key=lambda v: (-v[0]["prioridad"], -v[1]["el"]))
    print(f"{hora_local(t)} - {len(visibles)} satelites sobre {a.min_el} deg\n")
    print(f"{'NORAD':>6} {'Satelite':24} {'Az':>6} {'El':>5} {'Dist km':>8} {'Bajada (Doppler)':>18} Modo")
    for s, e, tx in visibles:
        print(f"{s['norad']:>6} {s['nombre'][:24]:24} {e['az']:6.1f} {e['el']:5.1f} {e['dist_km']:8.0f} "
              f"{fmt_f(e['dn']):>18} {tx.get('modo', '')}")


def _objetivo_actual(plan, ahora):
    for p in plan:
        if p["aos"] <= ahora <= p["los"]:
            return p, True
    futuros = [p for p in plan if p["aos"] > ahora]
    return (futuros[0], False) if futuros else (None, False)


def cmd_seguir(a, cfg, orb):
    est = estacion(cfg, orb)
    cat = _catalogo(a, cfg)
    solo_prio = any(s["prioridad"] for s in cat) and not a.todos
    if solo_prio:
        cat = [s for s in cat if s["prioridad"]]
    topo = wgs84.latlon(est["lat"], est["lon"], elevation_m=est["alt_m"])
    sats = {s["norad"]: o for s, o in objetos(cat)}
    margen = cfg.get("margen_reapuntado_s", 120)

    def recalcular():
        ps = calcular_pases(cat, est, 24, cfg["elevacion_minima_deg"],
                            desde=datetime.now(timezone.utc) - timedelta(minutes=20))
        return planificar(ps, margen, solo_prio)

    plan, t_plan = recalcular(), time.time()

    def lecturas():
        nonlocal plan, t_plan
        ultimo = None
        while True:
            if time.time() - t_plan > 3600:
                plan, t_plan = recalcular(), time.time()
            ahora = datetime.now(timezone.utc)
            p, activo = _objetivo_actual(plan, ahora)
            if not p:
                print("Sin pases en las proximas 24 h.")
                yield formatear_tracking("", 0, 0, None, None)
                continue
            s, tx = p["sat"], p["sat"]["principal"] or {}
            e = estado(sats[s["norad"]], topo, TS.from_datetime(ahora), tx.get("dn_hz"), tx.get("up_hz"))
            if s["norad"] != ultimo:
                print(f"\n=== Objetivo: {s['nombre']} (NORAD {s['norad']}) AOS {hora_local(p['aos'])} "
                      f"LOS {hora_local(p['los'])} ElMax {p['el_max']:.0f} ===")
                ultimo = s["norad"]
            if activo:
                linea = (f"{hora_local(ahora)} SEGUIR  Az {e['az']:6.1f}  El {e['el']:5.1f}  "
                         f"bajada {fmt_f(e['dn'])} {tx.get('modo_wisp', '')}"
                         + (f"  subida {fmt_f(e['up'])}" if e["up"] else ""))
                az, el = e["az"], max(e["el"], 0)
            elif a.posicion_real:
                linea = (f"{hora_local(ahora)} BAJO HORIZONTE  Az {e['az']:6.1f}  El {e['el']:5.1f}  "
                         f"AOS en {str(p['aos'] - ahora).split('.')[0]}")
                az, el = e["az"], e["el"]
            else:
                falta = p["aos"] - ahora
                accion = "PRE-APUNTAR" if falta.total_seconds() <= margen else "ESPERAR   "
                linea = (f"{hora_local(ahora)} {accion} AOS en {str(falta).split('.')[0]} -> "
                         f"Az {p['az_aos']:.0f} El 0, sintonizar {fmt_f(tx.get('dn_hz'))}")
                az, el = p["az_aos"], 0
            print(linea, flush=True)
            yield formatear_tracking(s["nombre_orbitron"], az, el, e["dn"], e["up"],
                                     tx.get("modo_wisp", ""), tx.get("modo_wisp", ""))
            if not a.dde:
                time.sleep(a.intervalo)

    try:
        if a.dde:
            if orb.corriendo():
                if not a.cerrar_orbitron:
                    sys.exit("Orbitron esta abierto y ocupa el nombre DDE 'Orbitron'. Cierralo o usa "
                             "--cerrar-orbitron.")
                import subprocess
                subprocess.run(["taskkill", "/IM", "Orbitron.exe", "/F"], capture_output=True)
                time.sleep(2)
                print("Orbitron cerrado.")
            print("Publicando por DDE como 'Orbitron|Tracking|TrackingData' para WiSPDDE.\n"
                  "En WiSPDDE (DDE Link) debe estar: fuente Orbitron, topic Tracking, item TrackingData.\n"
                  "Si WiSPDDE ya estaba abierto y no cambia, cierralo y abrelo de nuevo. Ctrl+C para parar.")
            wexe = cfg["orbitron"].get("wispdde_exe", r"C:\Program Files (x86)\wispdde\wispdde.exe")

            def abrir_wisp():
                if lanzar_wispdde(wexe):
                    print("WiSPDDE abierto: deberia mostrar Az/El en unos segundos.")
                else:
                    print("WiSPDDE ya estaba abierto (si no cambia, cierralo y repite el comando).")

            servidor_dde(lecturas(), a.intervalo, al_iniciar=abrir_wisp)
        else:
            for _ in lecturas():
                pass
    except KeyboardInterrupt:
        print("\nSeguimiento detenido.")


def cmd_mapa(a, cfg, orb):
    est = estacion(cfg, orb)
    cat = _catalogo(a, cfg)
    pases = [p for p in calcular_pases(cat, est, a.horas, cfg["elevacion_minima_deg"]) if not p["continuo"]]
    pases.sort(key=lambda p: (-p["sat"]["prioridad"], -p["el_max"]))
    elegidos, vistos = [], set()
    for p in pases:
        if p["sat"]["norad"] not in vistos:
            vistos.add(p["sat"]["norad"])
            elegidos.append(p)
        if len(elegidos) >= a.max_sats:
            break
    ahora = datetime.now(timezone.utc)
    pasos = [ahora + timedelta(seconds=30 * i) for i in range(-20, 200)]
    tt = TS.from_datetimes(pasos)
    R = 6371.0
    capas = []
    for p in elegidos:
        s = p["sat"]
        o = EarthSatellite(s["tle1"], s["tle2"], s["nombre"], TS)
        sub = wgs84.subpoint_of(o.at(tt))
        alt_km = wgs84.height_of(o.at(TS.from_datetime(ahora))).km
        lat, lon = sub.latitude.degrees.tolist(), sub.longitude.degrees.tolist()
        segs, seg = [], []
        for i in range(len(lat)):
            if seg and abs(lon[i] - seg[-1][1]) > 180:
                segs.append(seg)
                seg = []
            seg.append([round(lat[i], 3), round(lon[i], 3)])
        segs.append(seg)
        tx = s["principal"] or {}
        capas.append({
            "norad": s["norad"], "nombre": s["nombre"], "prio": s["prioridad"] > 0,
            "ahora": [lat[20], lon[20]], "trazas": segs,
            "huella_km": R * math.acos(R / (R + alt_km)), "alt_km": round(alt_km),
            "aos": hora_local(p["aos"]), "los": hora_local(p["los"])[6:], "el_max": round(p["el_max"], 1),
            "bajada": fmt_f(tx.get("dn_hz")), "modo": tx.get("modo", ""),
        })
    datos = {"estacion": est, "generado": hora_local(ahora), "horas": a.horas, "sats": capas}
    html = (AQUI / "mapa_plantilla.html").read_text(encoding="utf-8").replace(
        "/*DATOS*/null", json.dumps(datos, ensure_ascii=False))
    SALIDA.mkdir(exist_ok=True)
    f = SALIDA / "mapa.html"
    f.write_text(html, encoding="utf-8")
    print(f"Mapa con {len(capas)} satelites: {f}")
    if a.abrir:
        os.startfile(f)


def estado_hardware(orb: Orbitron) -> dict:
    """Detecta si la cadena de antena esta activa. Sin WiSPDDE abierto no se mueve nada: modo solo datos."""
    import subprocess
    import winreg
    procs = subprocess.run(["tasklist", "/NH"], capture_output=True, text=True).stdout.lower()
    puertos = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            i = 0
            while True:
                try:
                    puertos.append(winreg.EnumValue(k, i)[1])
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    wisp = "wispdde.exe" in procs
    return {"antena": wisp, "wispdde_abierto": wisp, "orbitron_abierto": "orbitron.exe" in procs,
            "orbitron_instalado": orb.instalado(), "puertos": sorted(puertos)}


def muestrear_pase(o, topo, p, tx, paso_s=15) -> dict:
    n = max(2, int((p["los"] - p["aos"]).total_seconds() // paso_s) + 1)
    dts = [p["aos"] + timedelta(seconds=paso_s * i) for i in range(n)] + [p["los"]]
    e = estado(o, topo, TS.from_datetimes(dts), tx.get("dn_hz"), tx.get("up_hz"))
    f = tx.get("dn_hz")
    return {
        "az": [round(x, 1) for x in e["az"].tolist()], "el": [round(x, 2) for x in e["el"].tolist()],
        "hora": [d.astimezone().strftime("%H:%M:%S") for d in dts],
        "hora_iso": [d.astimezone().strftime("%Y-%m-%dT%H:%M:%S") for d in dts],
        "min_tca": [round((d - p["tca"]).total_seconds() / 60, 2) for d in dts],
        "doppler_khz": [round((x - f) / 1e3, 3) for x in e["dn"].tolist()] if f else None,
    }


def cmd_reporte(a, cfg, orb):
    """Todo sin hardware: datos de SatNOGS, plan, graficos y mapa. Funciona con o sin antena."""
    hw = estado_hardware(orb)
    print("Antena: " + ("WiSPDDE abierto, estacion en linea" if hw["antena"]
                        else "no detectada (WiSPDDE cerrado) -> modo solo datos, no se mueve nada"))
    est = estacion(cfg, orb)
    db = abrir_db(cfg)
    try:
        cat = construir_catalogo(db, cfg)
    except Exception as e:  # sin internet: usar cache aunque este viejo
        print(f"No se pudo consultar SatNOGS DB ({e}); usando el ultimo cache de {DATA}")
        db.ttl = float("inf")
        cat = construir_catalogo(db, cfg)
    objetivo = [s for s in cat if s["prioridad"]]
    min_el = cfg["elevacion_minima_deg"]
    print(f"Calculando pases de {len(cat)} satelites para {a.horas} h (puede tardar ~1 min) ...")
    pases = [p for p in calcular_pases(cat, est, a.horas, min_el) if not p["continuo"]]
    usar = [p for p in pases if p["sat"]["prioridad"]] if objetivo and not a.todos else pases
    plan = planificar(usar, cfg.get("margen_reapuntado_s", 120), bool(objetivo) and not a.todos)

    topo = wgs84.latlon(est["lat"], est["lon"], elevation_m=est["alt_m"])
    objs = {s["norad"]: o for s, o in objetos({p["sat"]["norad"]: p["sat"] for p in plan}.values())}
    margen = timedelta(seconds=cfg.get("margen_reapuntado_s", 120))
    filas = []
    for p in plan:
        s, tx = p["sat"], p["sat"]["principal"] or {}
        o = objs[s["norad"]]
        filas.append({
            "norad": s["norad"], "nombre": s["nombre"],
            "pre": hora_local(p["aos"] - margen), "aos": p["aos"].astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "tca": p["tca"].astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "los": p["los"].astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "el_max": p["el_max"], "az_aos": p["az_aos"], "az_los": p["az_los"],
            "frecuencia": fmt_f(tx.get("dn_hz")), "modo": tx.get("modo", ""),
            "norte": cruza_norte(o, topo, p), **muestrear_pase(o, topo, p, tx),
        })
    orden = [s["norad"] for s in objetivo] if objetivo and not a.todos else []
    orden += [f["norad"] for f in filas if f["norad"] not in orden]

    inicio = datetime.now(timezone.utc).astimezone().replace(minute=0, second=0, microsecond=0)
    por_hora = defaultdict(int)
    for p in pases:
        por_hora[p["aos"].astimezone().replace(minute=0, second=0, microsecond=0)] += 1
    horas = [inicio + timedelta(hours=i) for i in range(int(math.ceil(a.horas)) + 1)]

    datos = {"estacion": est, "generado": hora_local(datetime.now(timezone.utc)), "horas": a.horas,
             "min_el": min_el, "hardware": hw, "plan": filas, "orden_sats": orden,
             "total_pases": len(pases), "satelites": len(cat),
             "por_hora": [{"hora": h.strftime("%Y-%m-%dT%H:%M:%S"), "pases": por_hora.get(h, 0)} for h in horas]}
    SALIDA.mkdir(exist_ok=True)
    (SALIDA / "reporte.json").write_text(json.dumps(datos, ensure_ascii=False, default=str), encoding="utf-8")
    html = (AQUI / "reporte_plantilla.html").read_text(encoding="utf-8").replace(
        "/*DATOS*/null", json.dumps(datos, ensure_ascii=False, default=str))
    f = SALIDA / "reporte.html"
    f.write_text(html, encoding="utf-8")

    print(f"\nPLAN ({len(filas)} pases):")
    for r in filas:
        print(f"  {r['aos'][5:]}  {r['nombre'][:20]:20} ElMax {r['el_max']:4.0f}  Az {r['az_aos']:3.0f}->{r['az_los']:3.0f}"
              f"  {r['frecuencia']} {r['modo']}{'  (cruza Norte)' if r['norte'] else ''}")
    a_mapa = argparse.Namespace(horas=min(a.horas, 3), max_sats=15, abrir=False, norad=None, objetivo=False)
    cmd_mapa(a_mapa, cfg, orb)
    print(f"Reporte con graficos: {f}")
    if not a.no_abrir:
        os.startfile(f)


class _Tee:
    """Duplica la salida de la terminal en un archivo de log (util cuando corre como tarea programada)."""
    def __init__(self, *flujos):
        self.flujos = flujos

    def write(self, s):
        for f in self.flujos:
            try:
                f.write(s)
                f.flush()
            except Exception:
                pass

    def flush(self):
        pass


def _esperar_fuera_de_pase(cfg, orb):
    """No reiniciar Orbitron/WiSPDDE en medio de un pase de un satelite objetivo."""
    est = estacion(cfg, orb)
    cat = [s for s in construir_catalogo(abrir_db(cfg), cfg) if s["prioridad"]]
    margen = timedelta(seconds=cfg.get("margen_reapuntado_s", 120) + 60)
    while True:
        ahora = datetime.now(timezone.utc)
        pases = calcular_pases(cat, est, 1, cfg["elevacion_minima_deg"], desde=ahora - timedelta(minutes=20))
        activo = [p for p in pases if p["aos"] - margen <= ahora <= p["los"]]
        if not activo:
            return
        p = max(activo, key=lambda x: x["los"])
        espera = (p["los"] - ahora).total_seconds() + 30
        print(f"{hora_local(ahora)} Pase en curso/proximo de {p['sat']['nombre']}; "
              f"espero hasta LOS {hora_local(p['los'])} para actualizar.")
        time.sleep(max(espera, 30))


def cmd_automatico(a, cfg, orb):
    import subprocess
    SALIDA.mkdir(exist_ok=True)
    log = open(SALIDA / "automatico.log", "a", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log)
    while True:
        print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} actualizacion automatica =====")
        try:
            _esperar_fuera_de_pase(cfg, orb)
            estaba_abierto = orb.corriendo()
            if estaba_abierto:
                print("Cerrando Orbitron y WiSPDDE para cargar los datos nuevos ...")
                for exe in ("Orbitron.exe", "wispdde.exe"):
                    subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
                time.sleep(3)
            cmd_actualizar(argparse.Namespace(forzar=False), cfg, orb)
            if estaba_abierto or a.lanzar:
                orb.lanzar(cfg["orbitron"]["driver"], minimizado=False)
                print(f"Orbitron relanzado con RunDriver={cfg['orbitron']['driver']}: "
                      "WiSPDDE ya recibe los datos nuevos.")
        except Exception as e:  # sin internet, etc.: reintenta en el siguiente ciclo
            print(f"Error en la actualizacion: {e}")
        if not a.cada:
            break
        print(f"Siguiente actualizacion en {a.cada} h.")
        time.sleep(a.cada * 3600)


def cmd_programar(a, cfg, orb):
    import subprocess
    nombre = "Estacion SatNOGS - actualizar Orbitron"
    if a.quitar:
        r = subprocess.run(["schtasks", "/Delete", "/TN", nombre, "/F"], capture_output=True, text=True)
        print(r.stdout or r.stderr)
        return
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else py
    accion = f'"{exe}" "{AQUI / "estacion.py"}" automatico' + (" --lanzar" if a.lanzar else "")
    r = subprocess.run(["schtasks", "/Create", "/TN", nombre, "/TR", accion, "/SC", "DAILY",
                        "/ST", a.hora, "/F"], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    if r.returncode == 0:
        print(f"Cada dia a las {a.hora}: SatNOGS DB -> Orbitron -> WiSPDDE. Log: {SALIDA / 'automatico.log'}")


def cmd_orbitron(a, cfg, orb):
    from orbitron import traer_al_frente
    oc = cfg["orbitron"]
    if orb.corriendo():
        traer_al_frente()
        print("Orbitron ya estaba abierto: lo traje al frente.")
    else:
        orb.lanzar(oc["driver"], minimizado=a.minimizado)
        print(f"Orbitron iniciado con RunDriver={oc['driver']}.")
        time.sleep(4)
        traer_al_frente()
    time.sleep(2)
    if lanzar_wispdde(oc.get("wispdde_exe", r"C:\Program Files (x86)\wispdde\wispdde.exe")):
        print("WiSPDDE abierto.")
    print("En Orbitron, pestana Rotor/Radio: driver WispDDE, elige el satelite y pulsa el icono de "
          "iniciar driver. Verifica con:  python estacion.py ventana")


def cmd_ventana(a, cfg, orb):
    from ventana import ejecutar
    ejecutar(cfg, orb)


def cmd_dde_monitor(a, cfg, orb):
    try:
        for txt, d in monitor_dde(a.intervalo):
            print(f"{datetime.now():%H:%M:%S}  {txt!r}" + (f"  -> {d}" if a.parsear else ""), flush=True)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"No se pudo leer DDE de Orbitron ({e}). Abre Orbitron y arranca el driver (pestana Rotor/Radio).")


def cmd_buscar(a, cfg, orb):
    db = abrir_db(cfg)
    q = a.texto.lower()
    tx = defaultdict(list)
    for t in db.transmisores():
        if t.get("alive"):
            tx[t["norad_cat_id"]].append(t)
    for s in db.satelites():
        if q in (s.get("name") or "").lower() or q in (s.get("names") or "").lower() or q == str(s.get("norad_cat_id")):
            print(f"\n{s.get('norad_cat_id')}  {s['name']}  [{s['status']}]  {s.get('names') or ''}")
            for t in tx.get(s.get("norad_cat_id"), []):
                dn = t.get("downlink_low")
                print(f"     {fmt_f(dn):>16}  {t.get('mode') or '':10} {t.get('status'):8} "
                      f"{'(en tus bandas) ' if en_bandas(dn, cfg['bandas_mhz']) else ''}{t.get('description', '')[:60]}")


def cmd_telemetria(a, cfg, orb):
    db = abrir_db(cfg)
    tramas = db.telemetria(a.norad, a.n)
    f = DATA / f"telemetria_{a.norad}.json"
    f.write_text(json.dumps(tramas, ensure_ascii=False, indent=1), encoding="utf-8")
    for t in tramas:
        print(f"{t['timestamp']}  {t.get('observer', ''):22} {t['frame'][:64]}...")
    print(f"\n{len(tramas)} tramas guardadas en {f}")


def main():
    cfg = cargar_config()
    orb = Orbitron(cfg["orbitron"]["exe"])
    ap = argparse.ArgumentParser(description="Estacion terrena: SatNOGS DB + Orbitron + WiSPDDE")
    sub = ap.add_subparsers(dest="cmd")

    def filtro(p):
        p.add_argument("--norad", type=int, nargs="+", help="solo estos NORAD")
        p.add_argument("--objetivo", action="store_true", help="solo satelites objetivo de config.json")

    p = sub.add_parser("actualizar", help="descargar SatNOGS DB y cargar en Orbitron")
    p.add_argument("--forzar", action="store_true", help="escribir aunque Orbitron este abierto")
    p.set_defaults(f=cmd_actualizar)

    p = sub.add_parser("pases", help="listar pases")
    filtro(p)
    p.add_argument("--horas", type=float, default=12)
    p.add_argument("--min-el", type=float)
    p.add_argument("--el-max-min", type=float, default=0, help="solo pases con elevacion max >= X")
    p.add_argument("--limite", type=int, default=60)
    p.add_argument("--csv", action="store_true")
    p.set_defaults(f=cmd_pases)

    p = sub.add_parser("programa", help="plan de apuntamiento sin conflictos")
    filtro(p)
    p.add_argument("--horas", type=float, default=24)
    p.add_argument("--todos", action="store_true", help="incluir satelites no objetivo")
    p.set_defaults(f=cmd_programa)

    p = sub.add_parser("ahora", help="satelites visibles ahora")
    filtro(p)
    p.add_argument("--min-el", type=float, default=0)
    p.set_defaults(f=cmd_ahora)

    p = sub.add_parser("seguir", help="seguimiento en vivo por terminal")
    filtro(p)
    p.add_argument("--intervalo", type=float, default=2)
    p.add_argument("--todos", action="store_true")
    p.add_argument("--dde", action="store_true", help="enviar los datos a WiSPDDE (reemplaza a Orbitron)")
    p.add_argument("--cerrar-orbitron", action="store_true", help="con --dde: cerrar Orbitron si esta abierto")
    p.add_argument("--posicion-real", action="store_true",
                   help="enviar Az/El reales aunque el satelite este bajo el horizonte (si no, pre-apunta)")
    p.set_defaults(f=cmd_seguir)

    p = sub.add_parser("mapa", help="mapa HTML de trazas y huellas")
    filtro(p)
    p.add_argument("--horas", type=float, default=3)
    p.add_argument("--max-sats", type=int, default=15)
    p.add_argument("--abrir", action="store_true")
    p.set_defaults(f=cmd_mapa)

    p = sub.add_parser("orbitron", help="abrir Orbitron con el driver DDE")
    p.add_argument("--minimizado", action="store_true")
    p.set_defaults(f=cmd_orbitron)

    p = sub.add_parser("dde-monitor", help="ver lo que Orbitron envia a WiSPDDE")
    p.add_argument("--intervalo", type=float, default=1)
    p.add_argument("--parsear", action="store_true")
    p.set_defaults(f=cmd_dde_monitor)

    p = sub.add_parser("buscar", help="buscar satelite en SatNOGS DB")
    p.add_argument("texto")
    p.set_defaults(f=cmd_buscar)

    p = sub.add_parser("telemetria", help="ultimas tramas de telemetria")
    p.add_argument("norad", type=int)
    p.add_argument("--n", type=int, default=20)
    p.set_defaults(f=cmd_telemetria)

    p = sub.add_parser("reporte", help="(por defecto) datos + plan + graficos + mapa, con o sin antena")
    p.add_argument("--horas", type=float, default=24)
    p.add_argument("--todos", action="store_true", help="planear con todos los satelites, no solo objetivo")
    p.add_argument("--no-abrir", action="store_true")
    p.set_defaults(f=cmd_reporte)

    p = sub.add_parser("ventana", help="ventana en tiempo real: Orbitron/WiSPDDE vs SatNOGS")
    p.set_defaults(f=cmd_ventana)

    p = sub.add_parser("automatico", help="actualizar Orbitron/WiSPDDE sin intervenir (respeta pases en curso)")
    p.add_argument("--cada", type=float, help="repetir cada N horas (deja el proceso corriendo)")
    p.add_argument("--lanzar", action="store_true", help="abrir Orbitron+WiSPDDE aunque estuvieran cerrados")
    p.set_defaults(f=cmd_automatico)

    p = sub.add_parser("programar", help="crear tarea diaria de Windows que ejecuta 'automatico'")
    p.add_argument("--hora", default="06:00")
    p.add_argument("--lanzar", action="store_true")
    p.add_argument("--quitar", action="store_true", help="borrar la tarea programada")
    p.set_defaults(f=cmd_programar)

    a = ap.parse_args(sys.argv[1:] or ["reporte"])
    a.f(a, cfg, orb)


if __name__ == "__main__":
    main()
