# Estación terrena: SatNOGS DB + Orbitron + WiSPDDE

```
SatNOGS DB (API REST) --TLE + transmisores--> estacion.py --archivos--> Orbitron --DDE--> WiSPDDE --> rotor / radio
                                                  |
                                                  +--> terminal: pases, plan de apuntamiento, Az/El + Doppler en vivo, mapa HTML
```

## Instalación
    pip install -r requirements.txt
La API key va en `.env` como `SATGNOS_API_KEY=...` (también acepta `SATNOGS_API_KEY`).

## Cómo ejecutarlo
Abre una terminal en esta carpeta (en VS Code: *Terminal > New Terminal*) y corre:

    pip install -r requirements.txt     # solo la primera vez
    python estacion.py                  # = python estacion.py reporte

Eso **no necesita la antena**: baja los datos de SatNOGS DB, calcula los pases de las próximas 24 h,
imprime el plan en la terminal y abre `salida/reporte.html` (gráficos) y genera `salida/mapa.html`.
Si WiSPDDE no está abierto, el reporte lo indica como *modo solo datos* y no se mueve nada.
Sin internet usa el último cache de `data/`.

Gráficos del reporte: trayectorias en el cielo (polar Az/El), elevación vs. hora, corrección Doppler,
actividad por hora y la tabla del plan de apuntamiento.

## Flujo diario con la antena
1. Cerrar Orbitron.
2. `python estacion.py actualizar`: descarga TLE/transmisores de hoy, escribe `Tle\satnogs.txt`, las notas
   con frecuencias y Radio.his, y configura Orbitron (satélites objetivo, autoselección al AOS, driver WispDDE).
3. `python estacion.py programa`: qué satélite, a qué hora, hacia dónde apuntar y en qué frecuencia.
4. `python estacion.py orbitron`: abre Orbitron con `RunDriver=WispDDE`; en cada AOS Orbitron cambia al
   satélite que sube y WiSPDDE mueve el rotor y sintoniza el radio.
5. Opcional: `python estacion.py seguir` (vivo por terminal), `python estacion.py dde-monitor` (ver lo que recibe WiSPDDE).

## Configuración (`config.json`)
- `satelites_objetivo`: NORAD en orden de prioridad (resuelve conflictos cuando dos pasan a la vez).
- `bandas_mhz`: bandas que recibe la estación; filtra los ~5000 transmisores de SatNOGS.
- `estacion`: con `lat`/`lon` en null se usa la ubicación guardada en Orbitron.
- `elevacion_minima_deg`, `margen_reapuntado_s`.

## Comandos
| comando | qué hace |
|---|---|
| `ventana` | ventana en tiempo real: lo que Orbitron envía a WiSPDDE vs. lo calculado con SatNOGS (log en `salida/comparacion.csv`) |
| `automatico [--cada H] [--lanzar]` | espera a que no haya pase, cierra Orbitron/WiSPDDE, actualiza y los relanza |
| `programar [--hora 06:00] [--lanzar] [--quitar]` | tarea diaria de Windows que ejecuta `automatico` (log en `salida/automatico.log`) |
| `reporte [--horas 24] [--todos]` | **por defecto**: datos + plan + gráficos + mapa, con o sin antena |
| `actualizar [--forzar]` | SatNOGS → Orbitron |
| `pases [--horas 12] [--objetivo] [--norad N ...] [--csv]` | lista de pases |
| `programa [--horas 24] [--todos]` | plan sin solapes para una antena |
| `ahora [--min-el 0]` | satélites sobre el horizonte ahora mismo |
| `seguir [--dde]` | Az/El/Doppler en vivo; `--dde` (experimental) alimenta WiSPDDE sin Orbitron |
| `mapa [--horas 3] [--abrir]` | `salida/mapa.html` con trazas y huellas |
| `buscar texto` | busca satélites y transmisores |
| `telemetria NORAD [--n 20]` | últimas tramas de la red SatNOGS |

## Notas
- Orbitron (2005) escribe en `%LOCALAPPDATA%\VirtualStore\Program Files (x86)\Orbitron`; ahí escribe también este programa.
  Cada `actualizar` guarda un respaldo `Config\Setup.cfg.bak-*`.
- Los TLE originales de Orbitron son de 2005; usa siempre `Tle\satnogs.txt`.
