"""Integracion con Orbitron 3.71 y WiSP DDE Client.

Como encajan las piezas:
  SatNOGS DB --(TLE + transmisores)--> Orbitron --(DDE: Orbitron|Tracking|TrackingData)--> WiSPDDE --> rotor / radio

Orbitron es un programa de 2005 sin manifiesto; Windows "virtualiza" sus escrituras en
  %LOCALAPPDATA%\\VirtualStore\\Program Files (x86)\\Orbitron\\...
y esa copia es la que Orbitron lee primero. Por eso escribimos alli (no hacen falta permisos de admin).
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ENC = "cp1252"


class Orbitron:
    def __init__(self, exe: str):
        self.exe = Path(exe)
        self.pf = self.exe.parent
        rel = str(self.pf)[len(self.pf.drive):].lstrip("\\/")
        self.vs = Path(os.environ.get("LOCALAPPDATA", "")) / "VirtualStore" / rel
        # Si ya existe la copia virtualizada, Orbitron la usa: escribimos ahi. Si no, probamos Program Files.
        if (self.vs / "Config" / "Setup.cfg").exists() or not self._puede_escribir(self.pf / "Config"):
            self.base_escritura = self.vs
        else:
            self.base_escritura = self.pf

    @staticmethod
    def _puede_escribir(carpeta: Path) -> bool:
        prueba = carpeta / "_prueba_escritura.tmp"
        try:
            prueba.write_text("x")
            prueba.unlink()
            return True
        except OSError:
            return False

    # ---------- rutas ----------
    def _leer(self, rel: str) -> Path:
        """Ruta efectiva que ve Orbitron (VirtualStore tiene prioridad)."""
        v = self.vs / rel
        return v if v.exists() else self.pf / rel

    def _escribir(self, rel: str) -> Path:
        p = self.base_escritura / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def instalado(self) -> bool:
        return self.exe.exists()

    def corriendo(self) -> bool:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Orbitron.exe", "/NH"],
                             capture_output=True, text=True).stdout
        return "orbitron.exe" in out.lower()

    # ---------- INI simple que preserva orden y comentarios ----------
    @staticmethod
    def _ini_set(texto: str, seccion: str, valores: dict) -> str:
        lineas = texto.splitlines()
        ini = fin = None
        for i, l in enumerate(lineas):
            if l.strip().lower() == f"[{seccion.lower()}]":
                ini = i
            elif ini is not None and l.strip().startswith("[") and i > ini:
                fin = i
                break
        if ini is None:
            lineas += ["", f"[{seccion}]"] + [f"{k}={v}" for k, v in valores.items()]
            return "\r\n".join(lineas) + "\r\n"
        fin = fin if fin is not None else len(lineas)
        pendientes = dict(valores)
        for i in range(ini + 1, fin):
            if "=" in lineas[i]:
                k = lineas[i].split("=", 1)[0].strip()
                if k in pendientes:
                    lineas[i] = f"{k}={pendientes.pop(k)}"
        # insertar las claves nuevas antes de las lineas en blanco del final de la seccion
        pos = fin
        while pos - 1 > ini and not lineas[pos - 1].strip():
            pos -= 1
        lineas[pos:pos] = [f"{k}={v}" for k, v in pendientes.items()]
        return "\r\n".join(lineas) + "\r\n"

    @staticmethod
    def _ini_get(texto: str, seccion: str) -> dict:
        out, dentro = {}, False
        for l in texto.splitlines():
            s = l.strip()
            if s.startswith("["):
                dentro = s.lower() == f"[{seccion.lower()}]"
            elif dentro and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    @staticmethod
    def _limpiar_ini(texto: str) -> str:
        """Quita lineas vacias acumuladas (repara archivos con saltos dobles) y deja una antes de cada seccion."""
        out = []
        for l in texto.replace("\r", "\n").split("\n"):
            if not l.strip():
                continue
            if l.strip().startswith("[") and out:
                out.append("")
            out.append(l.rstrip())
        return "\r\n".join(out) + "\r\n"

    def leer_setup(self) -> str:
        return self._limpiar_ini(self._leer("Config/Setup.cfg").read_text(encoding=ENC, errors="replace"))

    def ubicacion(self) -> dict | None:
        """Ubicacion configurada en Orbitron (Lon/Lat en 1e-4 grados, Alt en cm)."""
        try:
            loc = self._ini_get(self.leer_setup(), "Location")
            return {"nombre": loc.get("Name", "Orbitron"),
                    "lat": int(loc["Lat"]) / 1e4, "lon": int(loc["Lon"]) / 1e4,
                    "alt_m": int(loc.get("Alt", 0)) / 100}
        except (OSError, KeyError, ValueError):
            return None

    # ---------- escritura de datos ----------
    def escribir_tle(self, nombre_archivo: str, sats: list[dict]) -> Path:
        p = self._escribir(f"Tle/{nombre_archivo}")
        lineas = []
        for s in sats:
            lineas += [s["nombre_orbitron"][:24].ljust(24), s["tle1"], s["tle2"]]
        p.write_text("\r\n".join(lineas) + "\r\n", encoding=ENC, errors="replace", newline="")
        return p

    def escribir_notas(self, sats: list[dict]) -> int:
        n = 0
        hoy = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        for s in sats:
            l = [f"NAME: {s['nombre']}", f"STATUS: {s.get('estado', '')}"]
            for t in s["transmisores"]:
                etiqueta = f"{t['modo']} {t.get('desc', '')}".strip()
                if t["dn_hz"]:
                    l.append(f"DNLINK: {t['dn_hz'] / 1e6:.3f} {etiqueta}")
                if t["up_hz"]:
                    l.append(f"UPLINK: {t['up_hz'] / 1e6:.3f} {etiqueta}")
            l += [f"NOTES: Fuente SatNOGS DB (db.satnogs.org) - NORAD {s['norad']}", f"UPDATED: {hoy}"]
            self._escribir(f"Notes/{s['norad']:05d}.txt").write_text(
                "\r\n".join(l) + "\r\n", encoding=ENC, errors="replace", newline="")
            n += 1
        return n

    def escribir_radio(self, sats: list[dict]) -> None:
        """Preselecciona en Orbitron la frecuencia/modo principal de cada satelite (Config/Radio.his)."""
        origen = self._leer("Config/Radio.his")
        texto = (self._limpiar_ini(origen.read_text(encoding=ENC, errors="replace"))
                 if origen.exists() else "[Radio]\r\n")
        valores = {}
        for s in sats:
            t = s.get("principal")
            if not t:
                continue
            k = str(s["norad"])
            if t["dn_hz"]:
                valores[f"{k}_dn"] = f"{t['dn_hz'] / 1e6:.6f}"
                valores[f"{k}_dm"] = t["modo_wisp"]
            if t["up_hz"]:
                valores[f"{k}_up"] = f"{t['up_hz'] / 1e6:.6f}"
                valores[f"{k}_um"] = t["modo_wisp"]
        self._escribir("Config/Radio.his").write_text(self._ini_set(texto, "Radio", valores), encoding=ENC, newline="")

    def configurar(self, archivo_tle: str, seguidos: list[int], activo: int | None,
                   driver: str, autoseleccion: bool, arrancar_driver: bool,
                   ubicacion: dict | None = None, driver_exe: str | None = None) -> Path:
        destino = self._escribir("Config/Setup.cfg")
        texto = self.leer_setup()
        respaldo = destino.with_name(f"Setup.cfg.bak-{datetime.now():%Y%m%d-%H%M%S}")
        respaldo.write_bytes(self._leer("Config/Setup.cfg").read_bytes())
        sat = {"TLEFiles": f"Tle\\{archivo_tle}", "Tracked_1": ";".join(f"{n:05d}" for n in seguidos),
               "All": "0"}
        if activo:
            sat["Active"] = f"{activo:05d}"
        texto = self._ini_set(texto, "Satellites", sat)
        texto = self._ini_set(texto, "System", {"DDEdriver": driver})
        if driver_exe:  # sin esta ruta, RunDriver=WispDDE no abre WiSPDDE y el DDE sale vacio
            texto = self._ini_set(texto, "Drivers", {driver: driver_exe})
        texto = self._ini_set(texto, "Extra", {"AOSSelect": int(autoseleccion),
                                               "AutostartDrv": int(arrancar_driver)})
        if ubicacion:
            texto = self._ini_set(texto, "Location", {
                "Lat": round(ubicacion["lat"] * 1e4), "Lon": round(ubicacion["lon"] * 1e4),
                "Alt": round(ubicacion["alt_m"] * 100)})
        destino.write_text(texto, encoding=ENC, errors="replace", newline="")
        return respaldo

    def lanzar(self, driver: str, minimizado: bool = True) -> None:
        args = [str(self.exe), f"RunDriver={driver}"] + (["Minimized"] if minimizado else [])
        subprocess.Popen(args, cwd=str(self.pf))


# ---------------- DDE ----------------
_RE_DDE = re.compile(r'(SN|AZ|EL|DN|UP|DM|UM|MA|LO|LA|AL|RA)("[^"]*"|\S*)')


def parsear_tracking(texto: str) -> dict:
    """Convierte la cadena DDE de Orbitron (p.ej. 'SN"ISS" AZ123.4 EL12.0 DN145800000 ...') en dict."""
    return {k: v.strip('"') for k, v in _RE_DDE.findall(texto or "")}


def formatear_tracking(nombre: str, az: float, el: float, dn_hz: float | None, up_hz: float | None,
                       dm: str = "", um: str = "") -> str:
    """Mismo formato que Orbitron 3.71 (leido de su DDE real):
    'SNLANDSAT_8 AZ10.4 EL-39.5 DN2282332385 UP144997943 DMFM\\r\\n' (espacios del nombre -> '_')."""
    s = f"SN{nombre.strip().replace(' ', '_')} AZ{az:.1f} EL{el:.1f} DN{int(dn_hz or 0)} UP{int(up_hz or 0)}"
    if dm:
        s += f" DM{dm}"
    if um:
        s += f" UM{um}"
    return s + "\r\n"


def monitor_dde(intervalo: float = 1.0, servicio="Orbitron", topico="Tracking", item="TrackingData"):
    """Lee lo que Orbitron publica para WiSPDDE (generador de (texto, dict))."""
    import win32ui  # noqa: F401  (inicializa MFC, requerido por dde)
    import dde
    srv = dde.CreateServer()
    srv.Create("MonitorEstacion")
    conv = dde.CreateConversation(srv)
    conv.ConnectTo(servicio, topico)
    try:
        while True:
            txt = conv.Request(item)
            yield txt, parsear_tracking(txt)
            time.sleep(intervalo)
    finally:
        srv.Shutdown()


def traer_al_frente(clases=("TApplication", "TFMain"), titulo="Orbitron") -> bool:
    """Restaura y pone al frente la ventana de Orbitron (por si quedo minimizada o detras)."""
    import win32con
    import win32gui
    hs = []

    def cb(h, _):
        if win32gui.GetClassName(h) in clases and titulo.lower() in win32gui.GetWindowText(h).lower():
            hs.append(h)
    win32gui.EnumWindows(cb, None)
    for h in hs:
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(h)
        except Exception:
            pass
    return bool(hs)


def wispdde_corriendo() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq wispdde.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "wispdde.exe" in out.lower()


def lanzar_wispdde(exe: str) -> bool:
    """Abre WiSPDDE (ventana normal) si no esta abierto. Devuelve True si lo abrio."""
    if wispdde_corriendo():
        return False
    subprocess.Popen([exe], cwd=str(Path(exe).parent))
    return True


def servidor_dde(generador, intervalo: float = 1.0, servicio="Orbitron", topico="Tracking",
                 item="TrackingData", al_iniciar=None):
    """Publica datos de seguimiento con el mismo nombre DDE que Orbitron, para que WiSPDDE
    mueva rotor/radio sin Orbitron. `generador` produce cadenas; `al_iniciar` se llama
    cuando el servidor ya existe (p.ej. para abrir WiSPDDE y que se conecte)."""
    import win32gui
    import win32ui  # noqa: F401
    import dde
    srv = dde.CreateServer()
    top = dde.CreateTopic(topico)
    it = dde.CreateStringItem(item)
    top.AddItem(it)
    srv.AddTopic(top)
    srv.Create(servicio)
    try:
        primero = True
        for texto in generador:
            it.SetData(texto)
            if primero and al_iniciar:
                al_iniciar()
            primero = False
            it.SetData(texto)
            fin = time.time() + intervalo
            while time.time() < fin:
                win32gui.PumpWaitingMessages()
                time.sleep(0.05)
    finally:
        srv.Shutdown()
