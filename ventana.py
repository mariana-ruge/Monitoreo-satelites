"""Ventana en tiempo real: lo que Orbitron envia a WiSPDDE (DDE) comparado con lo calculado desde SatNOGS DB.

  python estacion.py ventana

- Radar polar: satelites visibles segun SatNOGS (gris; objetivos en azul), posicion que envia
  Orbitron (anillo naranja) y la del mismo satelite calculada con SatNOGS (punto azul).
- Tabla comparativa: Az, El, bajada, subida y modo -> Orbitron/WiSPDDE vs SatNOGS y la diferencia.
- Visibles ahora y proximos pases del plan.
- Cada comparacion se guarda en salida/comparacion.csv.
"""
from __future__ import annotations

import csv
import math
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import ttk

from skyfield.api import wgs84

import estacion as E
from orbitron import lanzar_wispdde, parsear_tracking, traer_al_frente

AZUL, NARANJA, GRIS = "#2a78d6", "#eb6834", "#9a9890"
FONDO, PANEL, BORDE, TXT, TXT2, REJILLA = "#f3f3f1", "#fcfcfb", "#dddcd6", "#0b0b0b", "#52514e", "#e4e3de"
BIEN, ALERTA = "#0d7a32", "#b45309"


class LectorDDE:
    """Cliente DDE de Orbitron|Tracking|TrackingData con reconexion."""

    def __init__(self):
        self.srv = self.conv = None
        self.ultimo_intento = 0.0

    def leer(self) -> tuple[str | None, str]:
        try:
            if self.conv is None:
                if time.time() - self.ultimo_intento < 3:
                    return None, "sin conexión"
                self.ultimo_intento = time.time()
                import win32ui  # noqa: F401
                import dde
                if self.srv is None:
                    self.srv = dde.CreateServer()
                    self.srv.Create("VentanaEstacion")
                c = dde.CreateConversation(self.srv)
                c.ConnectTo("Orbitron", "Tracking")
                self.conv = c
            txt = self.conv.Request("TrackingData")
            return txt, "conectado"
        except Exception:
            self.conv = None
            return None, "sin conexión"


def procesos() -> set[str]:
    out = subprocess.run(["tasklist", "/NH"], capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    return {p for p in ("orbitron.exe", "wispdde.exe") if p in out}


def dif_az(a, b):
    return (a - b + 180) % 360 - 180


class App:
    def __init__(self, root, cfg, orb):
        self.root, self.cfg, self.orb = root, cfg, orb
        self.est = E.estacion(cfg, orb)
        self.topo = wgs84.latlon(self.est["lat"], self.est["lon"], elevation_m=self.est["alt_m"])
        self.dde = LectorDDE()
        self.procs: set[str] = set()
        self.visibles: list = []
        self.plan: list = []
        self.cargando = False
        self._resultado = None
        self.csv = None
        self._ui()
        self.cargar_satnogs(forzar=False)
        self.tick_rapido()
        self.tick_lento()

    # ---------------- datos ----------------
    def cargar_satnogs(self, forzar: bool):
        if self.cargando:
            return
        self.cargando = True
        self.lbl_satnogs.config(text="SatNOGS: descargando…", fg=ALERTA)

        def trabajo():
            try:
                cat = E.construir_catalogo(E.abrir_db(self.cfg), self.cfg, forzar=forzar)
                objs = E.objetos(cat)
                obj = [s for s in cat if s["prioridad"]]
                pases = E.calcular_pases(obj, self.est, 24, self.cfg["elevacion_minima_deg"],
                                         desde=datetime.now(timezone.utc) - timedelta(minutes=20))
                plan = E.planificar(pases, self.cfg.get("margen_reapuntado_s", 120), True)
                cache = E.DATA / "tle.json"
                edad = datetime.fromtimestamp(cache.stat().st_mtime).strftime("%H:%M") if cache.exists() else "?"
                self._resultado = ("ok", (cat, objs, plan, edad))
            except Exception as e:
                self._resultado = ("error", e)
            finally:
                self.cargando = False
        threading.Thread(target=trabajo, daemon=True).start()

    def _datos_listos(self, cat, objs, plan, edad):
        self.cat, self.objs, self.plan = cat, objs, plan
        self.por_nombre = {s["nombre_orbitron"].upper(): (s, o) for s, o in objs}
        self.por_norad = {s["norad"]: (s, o) for s, o in objs}
        self.lbl_satnogs.config(text=f"SatNOGS: {len(cat)} satélites · datos de las {edad}", fg=BIEN)
        self.actualizar_visibles()

    def buscar(self, nombre: str):
        n = (nombre or "").strip().upper().replace("_", " ")  # Orbitron cambia espacios por '_'
        if not n or not hasattr(self, "por_nombre"):
            return None
        if n in self.por_nombre:
            return self.por_nombre[n]
        for k, v in self.por_nombre.items():
            if k.startswith(n) or n.startswith(k):
                return v
        return None

    def objetivo_plan(self):
        ahora = datetime.now(timezone.utc)
        for p in self.plan:
            if p["los"] >= ahora:
                return p
        return None

    # ---------------- ciclos ----------------
    def tick_rapido(self):
        try:
            res, self._resultado = self._resultado, None
            if res and res[0] == "ok":
                self._datos_listos(*res[1])
            elif res:
                self.lbl_satnogs.config(text=f"SatNOGS: error {res[1]}", fg=ALERTA)
            self.comparar()
        finally:
            self.root.after(1000, self.tick_rapido)

    def tick_lento(self):
        try:
            self.procs = procesos()
            self.actualizar_visibles()
            self.actualizar_estado()
        finally:
            self.root.after(5000, self.tick_lento)

    def actualizar_visibles(self):
        if not hasattr(self, "objs"):
            return
        t = E.TS.now()
        vis = []
        for s, o in self.objs:
            tx = s["principal"] or {}
            e = E.estado(o, self.topo, t, tx.get("dn_hz"), tx.get("up_hz"))
            if e["el"] > 0:
                vis.append((s, e, tx))
        vis.sort(key=lambda v: (-v[0]["prioridad"], -v[1]["el"]))
        self.visibles = vis
        self.tv_vis.delete(*self.tv_vis.get_children())
        for s, e, tx in vis[:60]:
            self.tv_vis.insert("", "end", values=(s["norad"], s["nombre"][:22], f"{e['az']:.1f}", f"{e['el']:.1f}",
                                                  E.fmt_f(e["dn"]), tx.get("modo", "")),
                               tags=("obj",) if s["prioridad"] else ())
        self.tv_plan.delete(*self.tv_plan.get_children())
        ahora = datetime.now(timezone.utc)
        for p in self.plan[:12]:
            if p["los"] < ahora:
                continue
            falta = "EN CURSO" if p["aos"] <= ahora else str(p["aos"] - ahora).split(".")[0]
            self.tv_plan.insert("", "end", values=(p["sat"]["nombre"][:18], E.hora_local(p["aos"])[6:], falta,
                                                   f"{p['el_max']:.0f}°", f"{p['az_aos']:.0f}°"))

    def actualizar_estado(self):
        o = "orbitron.exe" in self.procs
        w = "wispdde.exe" in self.procs
        self.lbl_orb.config(text=f"Orbitron: {'abierto' if o else 'cerrado'}", fg=BIEN if o else ALERTA)
        self.lbl_wisp.config(text=f"WiSPDDE: {'abierto' if w else 'cerrado'}", fg=BIEN if w else ALERTA)

    def comparar(self):
        self.lbl_hora.config(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        txt, estado = self.dde.leer()
        d = parsear_tracking(txt) if txt else {}
        if not txt:
            self.lbl_dde.config(text="DDE: sin conexión (abre Orbitron o 'seguir --dde')", fg=ALERTA)
        elif not d.get("SN"):
            self.lbl_dde.config(text="DDE: conectado pero VACÍO → el driver de Orbitron está detenido: pestaña "
                                     "Rotor/Radio, WispDDE, ícono de iniciar (abre WiSPDDE)", fg=ALERTA)
        else:
            self.lbl_dde.config(text=f"DDE: recibiendo · {txt.strip()[:70]}", fg=BIEN)

        par = self.buscar(d.get("SN")) if d.get("SN") else None
        fuente = "Orbitron"
        if not par:  # sin DDE: mostrar el objetivo del plan solo con SatNOGS
            p = self.objetivo_plan()
            par = self.por_norad.get(p["sat"]["norad"]) if (p and hasattr(self, "por_norad")) else None
            fuente = "plan"
        filas, orb_pos, sat_pos, nombre = [], None, None, "—"
        if par:
            s, o = par
            nombre = f"{s['nombre']} (NORAD {s['norad']})"
            dn_o = float(d["DN"]) if d.get("DN", "").replace(".", "").isdigit() and float(d["DN"]) > 0 else None
            up_o = float(d["UP"]) if d.get("UP", "").replace(".", "").isdigit() and float(d["UP"]) > 0 else None
            # transmisor de SatNOGS mas cercano a lo que Orbitron sintoniza (o el principal)
            txs = [t for t in s["transmisores"] if t.get("dn_hz")]
            tx = min(txs, key=lambda t: abs(t["dn_hz"] - dn_o)) if (dn_o and txs) else (s["principal"] or {})
            e = E.estado(o, self.topo, E.TS.now(), tx.get("dn_hz"), tx.get("up_hz"))
            sat_pos = (e["az"], e["el"])
            try:
                az_o, el_o = float(d["AZ"]), float(d["EL"])
                orb_pos = (az_o, el_o)
            except (KeyError, ValueError):
                az_o = el_o = None

            def f(v, fmt):
                return fmt.format(v) if v is not None else "—"
            filas = [
                ("Azimut (°)", f(az_o, "{:.1f}"), f(e["az"], "{:.1f}"),
                 f(dif_az(az_o, e["az"]) if az_o is not None else None, "{:+.2f}°")),
                ("Elevación (°)", f(el_o, "{:.1f}"), f(e["el"], "{:.1f}"),
                 f(el_o - e["el"] if el_o is not None else None, "{:+.2f}°")),
                ("Bajada (Doppler)", E.fmt_f(dn_o), E.fmt_f(e["dn"]),
                 f((dn_o - e["dn"]) / 1e3 if (dn_o and e["dn"]) else None, "{:+.2f} kHz")),
                ("Subida (Doppler)", E.fmt_f(up_o), E.fmt_f(e["up"]),
                 f((up_o - e["up"]) / 1e3 if (up_o and e["up"]) else None, "{:+.2f} kHz")),
                ("Modo", d.get("DM", "—"), f"{tx.get('modo', '—')} → {tx.get('modo_wisp', '')}", ""),
                ("Distancia (km)", "—", f"{e['dist_km']:.0f}", ""),
                ("Transmisor SatNOGS", "", (tx.get("desc") or "")[:34], ""),
            ]
            if orb_pos and d.get("SN"):
                self._guardar_csv(s, az_o, e["az"], el_o, e["el"], dn_o, e["dn"])
        titulo = f"Comparando: {nombre}" + ("" if fuente == "Orbitron" else "   (sin DDE: objetivo del plan, solo SatNOGS)")
        self.lbl_comp.config(text=titulo)
        self.tv_comp.delete(*self.tv_comp.get_children())
        for fila in filas:
            self.tv_comp.insert("", "end", values=fila)
        self.dibujar_radar(orb_pos, sat_pos)

    def _guardar_csv(self, s, az_o, az_s, el_o, el_s, dn_o, dn_s):
        if self.csv is None:
            E.SALIDA.mkdir(exist_ok=True)
            ruta = E.SALIDA / "comparacion.csv"
            nuevo = not ruta.exists()
            self.csv_fh = open(ruta, "a", newline="", encoding="utf-8")
            self.csv = csv.writer(self.csv_fh)
            if nuevo:
                self.csv.writerow(["hora_utc", "norad", "satelite", "az_orbitron", "az_satnogs", "el_orbitron",
                                   "el_satnogs", "dn_orbitron_hz", "dn_satnogs_hz"])
        self.csv.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), s["norad"], s["nombre"],
                           az_o, round(az_s, 2), el_o, round(el_s, 2), dn_o, round(dn_s) if dn_s else ""])
        self.csv_fh.flush()

    # ---------------- radar ----------------
    def xy(self, az, el):
        r = self.R * (90 - max(el, 0)) / 90
        a = math.radians(az)
        return self.cx + r * math.sin(a), self.cy - r * math.cos(a)

    def dibujar_radar(self, orb_pos, sat_pos):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        self.cx, self.cy, self.R = w / 2, h / 2, min(w, h) / 2 - 24
        for el in (0, 30, 60):
            r = self.R * (90 - el) / 90
            c.create_oval(self.cx - r, self.cy - r, self.cx + r, self.cy + r, outline=REJILLA)
            c.create_text(self.cx + 4, self.cy - r + 8, text=f"{el}°", fill=TXT2, anchor="w", font=("Segoe UI", 8))
        c.create_line(self.cx - self.R, self.cy, self.cx + self.R, self.cy, fill=REJILLA)
        c.create_line(self.cx, self.cy - self.R, self.cx, self.cy + self.R, fill=REJILLA)
        for t, az in (("N", 0), ("E", 90), ("S", 180), ("O", 270)):
            x, y = self.xy(az, -8)
            x = self.cx + (self.R + 12) * math.sin(math.radians(az))
            y = self.cy - (self.R + 12) * math.cos(math.radians(az))
            c.create_text(x, y, text=t, fill=TXT2, font=("Segoe UI", 10, "bold"))
        # trayectoria del pase objetivo
        p = self.objetivo_plan()
        if p and hasattr(self, "por_norad") and p["sat"]["norad"] in self.por_norad:
            o = self.por_norad[p["sat"]["norad"]][1]
            n = 40
            tt = E.TS.from_datetimes([p["aos"] + (p["los"] - p["aos"]) * i / n for i in range(n + 1)])
            alt, az, _ = (o - self.topo).at(tt).altaz()
            pts = [self.xy(a, e) for a, e in zip(az.degrees, alt.degrees)]
            c.create_line(*[v for pt in pts for v in pt], fill=AZUL, width=2, dash=(4, 3), smooth=True)
            c.create_text(*pts[0], text=f"AOS {E.hora_local(p['aos'])[6:11]}", fill=TXT2, anchor="sw",
                          font=("Segoe UI", 8))
        for s, e, _ in self.visibles:
            x, y = self.xy(e["az"], e["el"])
            if s["prioridad"]:
                c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=AZUL, outline=PANEL, width=2)
                c.create_text(x + 8, y, text=s["nombre"][:14], fill=TXT, anchor="w", font=("Segoe UI", 8, "bold"))
            else:
                c.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=GRIS, outline="")
        if sat_pos and sat_pos[1] >= 0:  # bajo el horizonte no se dibuja (solo en la tabla)
            x, y = self.xy(*sat_pos)
            c.create_oval(x - 6, y - 6, x + 6, y + 6, fill=AZUL, outline=PANEL, width=2)
        if orb_pos:
            x, y = self.xy(*orb_pos)
            c.create_oval(x - 10, y - 10, x + 10, y + 10, outline=NARANJA, width=3)
        c.create_oval(10, h - 44, 20, h - 34, fill=AZUL, outline="")
        c.create_text(26, h - 39, text="SatNOGS (calculado)", fill=TXT2, anchor="w", font=("Segoe UI", 8))
        c.create_oval(10, h - 24, 20, h - 14, outline=NARANJA, width=2)
        c.create_text(26, h - 19, text="Orbitron → WiSPDDE", fill=TXT2, anchor="w", font=("Segoe UI", 8))

    # ---------------- interfaz ----------------
    def _ui(self):
        r = self.root
        r.title("Estación terrena · tiempo real · Orbitron/WiSPDDE vs SatNOGS")
        r.configure(bg=FONDO)
        r.geometry("1250x680+10+10")
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TXT, rowheight=22,
                     font=("Segoe UI", 9), bordercolor=BORDE)
        st.configure("Treeview.Heading", background=FONDO, foreground=TXT2, font=("Segoe UI", 9, "bold"))
        st.configure("TButton", font=("Segoe UI", 9))

        top = tk.Frame(r, bg=FONDO)
        top.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(top, text=self.est.get("nombre", "Estación"), bg=FONDO, fg=TXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        self.lbl_hora = tk.Label(top, bg=FONDO, fg=TXT2, font=("Segoe UI", 11))
        self.lbl_hora.pack(side="right")

        est = tk.Frame(r, bg=FONDO)
        est.pack(fill="x", padx=12)
        self.lbl_orb = tk.Label(est, bg=FONDO, font=("Segoe UI", 9, "bold"))
        self.lbl_wisp = tk.Label(est, bg=FONDO, font=("Segoe UI", 9, "bold"))
        self.lbl_satnogs = tk.Label(est, bg=FONDO, font=("Segoe UI", 9, "bold"))
        for w in (self.lbl_orb, self.lbl_wisp, self.lbl_satnogs):
            w.pack(side="left", padx=(0, 18))
        ttk.Button(est, text="Recargar SatNOGS", command=lambda: self.cargar_satnogs(True)).pack(side="right")
        ttk.Button(est, text="Abrir WiSPDDE", command=self.abrir_wisp).pack(side="right", padx=4)
        ttk.Button(est, text="Abrir Orbitron", command=self.abrir_orbitron).pack(side="right")
        self.lbl_dde = tk.Label(r, bg=FONDO, anchor="w", font=("Consolas", 9))
        self.lbl_dde.pack(fill="x", padx=12, pady=(2, 6))

        cuerpo = tk.Frame(r, bg=FONDO)
        cuerpo.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        izq = tk.Frame(cuerpo, bg=PANEL, highlightbackground=BORDE, highlightthickness=1)
        izq.pack(side="left", fill="both", expand=True)
        tk.Label(izq, text="Cielo de la estación (centro = cenit)", bg=PANEL, fg=TXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
        self.canvas = tk.Canvas(izq, bg=PANEL, highlightthickness=0, width=440, height=440)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)

        der = tk.Frame(cuerpo, bg=FONDO)
        der.pack(side="left", fill="both", expand=True, padx=(12, 0))
        caja = tk.Frame(der, bg=PANEL, highlightbackground=BORDE, highlightthickness=1)
        caja.pack(fill="x")
        self.lbl_comp = tk.Label(caja, bg=PANEL, fg=TXT, anchor="w", font=("Segoe UI", 10, "bold"))
        self.lbl_comp.pack(fill="x", padx=10, pady=(8, 2))
        self.tv_comp = self._tabla(caja, [("Dato", 130), ("Orbitron / WiSPDDE", 150), ("SatNOGS", 190),
                                          ("Diferencia", 100)], 7)

        abajo = tk.Frame(der, bg=FONDO)
        abajo.pack(fill="both", expand=True, pady=(12, 0))
        c1 = tk.Frame(abajo, bg=PANEL, highlightbackground=BORDE, highlightthickness=1)
        c1.pack(side="left", fill="both", expand=True)
        tk.Label(c1, text="Visibles ahora (SatNOGS)", bg=PANEL, fg=TXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(8, 2))
        self.tv_vis = self._tabla(c1, [("NORAD", 55), ("Satélite", 130), ("Az", 45), ("El", 40), ("Bajada", 105),
                                       ("Modo", 55)], 10)
        self.tv_vis.tag_configure("obj", foreground=AZUL)
        c2 = tk.Frame(abajo, bg=PANEL, highlightbackground=BORDE, highlightthickness=1)
        c2.pack(side="left", fill="both", padx=(12, 0))
        tk.Label(c2, text="Próximos pases del plan", bg=PANEL, fg=TXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(8, 2))
        self.tv_plan = self._tabla(c2, [("Satélite", 100), ("AOS", 62), ("Falta", 70), ("ElMax", 45),
                                        ("Az", 40)], 10)
        self.canvas.bind("<Configure>", lambda e: None)

    def _tabla(self, padre, cols, alto):
        tv = ttk.Treeview(padre, columns=[c for c, _ in cols], show="headings", height=alto)
        for c, w in cols:
            tv.heading(c, text=c, anchor="w")
            tv.column(c, width=w, anchor="w", stretch=True)
        tv.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return tv

    def abrir_orbitron(self):
        if self.orb.corriendo():
            traer_al_frente()
        else:
            self.orb.lanzar(self.cfg["orbitron"]["driver"], minimizado=False)
            self.root.after(4000, traer_al_frente)

    def abrir_wisp(self):
        lanzar_wispdde(self.cfg["orbitron"].get("wispdde_exe", r"C:\Program Files (x86)\wispdde\wispdde.exe"))


def ejecutar(cfg, orb):
    root = tk.Tk()
    App(root, cfg, orb)
    root.mainloop()
