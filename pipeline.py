"""
LaGeoMaga FireChronos — Etapa 8
Pipeline reutilizable para la app web (Streamlit) y para pruebas.

Modos:
  - ejecutar_demo(): datos sintéticos, sin ZIP ni MAP_KEY.
  - ejecutar_real(zips, map_key, modo): ZIP de Etapa 3/4 + NASA FIRMS.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from dataclasses import dataclass, field
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Callable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
from plotly.offline import plot as plotly_div
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import unary_union

COLUMNAS_MINIMAS = {
    "latitude", "longitude", "acq_date", "acq_time", "satellite",
    "confidence", "frp", "daynight", "type",
}
FUENTES = {"MODIS": "MODIS_SP", "VIIRS": "VIIRS_SNPP_SP"}
BBOX = (-69.8, -23.1, -57.0, -9.5)
TRANS = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
INV = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True)

VENTANA_MIN = 60
UMBRAL_MODIS = 80


@dataclass
class Entradas:
    viirs_pre: pd.DataFrame
    modis_pre: pd.DataFrame
    meta3: dict
    meta4: dict
    bolivia: object
    geo: dict
    comparacion_base: pd.DataFrame


@dataclass
class Resultados:
    modo: str
    demo: bool
    fechas: list[str]
    modis: pd.DataFrame
    viirs: pd.DataFrame
    cal10: pd.DataFrame
    cal20: pd.DataFrame
    d10: pd.DataFrame
    d20: pd.DataFrame
    resumen_sensor: pd.DataFrame
    sens: pd.DataFrame
    control_sensibilidad: pd.DataFrame
    bitacora: pd.DataFrame
    fig_calendario: object
    fig_sensibilidad: object
    mapa_fig: go.Figure
    mapa_html: str
    archivos: dict[str, bytes] = field(default_factory=dict)
    metricas: dict = field(default_factory=dict)


def _version(paquete: str) -> str:
    try:
        return pkg_version(paquete)
    except Exception:
        return "desconocida"


def _progreso(callback: Callable[[str, float], None] | None, mensaje: str, fraccion: float) -> None:
    if callback:
        callback(mensaje, fraccion)


# ---------------------------------------------------------------------------
# Entradas desde ZIP
# ---------------------------------------------------------------------------

def _nombres_zip(z: zipfile.ZipFile) -> dict[str, str]:
    return {Path(n).name: n for n in z.namelist() if not n.endswith("/")}


def cargar_entradas(zips: list[tuple[str, bytes]]) -> Entradas:
    """Lee los ZIP de Etapa 3 (VIIRS) y Etapa 4 (MODIS)."""
    paquetes: dict[str, dict[str, bytes]] = {}
    for nombre, contenido in zips:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            nombres = _nombres_zip(z)
            if "VIIRS_Bolivia_septiembre_2024.parquet" in nombres:
                tipo = "VIIRS"
            elif "MODIS_Bolivia_1_10sep2024.parquet" in nombres:
                tipo = "MODIS"
            else:
                raise ValueError(
                    f"ZIP no identificado: {nombre}. Debe contener "
                    "VIIRS_Bolivia_septiembre_2024.parquet o "
                    "MODIS_Bolivia_1_10sep2024.parquet."
                )
            if tipo in paquetes:
                raise ValueError(f"Dos ZIP corresponden a {tipo}; debe haber uno de cada sensor.")
            paquetes[tipo] = {
                Path(n).name: z.read(n) for n in z.namelist() if not n.endswith("/")
            }

    if set(paquetes) != {"VIIRS", "MODIS"}:
        raise ValueError("Faltan ZIP: sube el de Etapa 3 y el de Etapa 4.")

    viirs_pre = pd.read_parquet(
        io.BytesIO(paquetes["VIIRS"]["VIIRS_Bolivia_septiembre_2024.parquet"])
    )
    modis_pre = pd.read_parquet(
        io.BytesIO(paquetes["MODIS"]["MODIS_Bolivia_1_10sep2024.parquet"])
    )
    meta3 = json.loads(paquetes["VIIRS"]["metadata_etapa3.json"])
    meta4 = json.loads(paquetes["MODIS"]["metadata_etapa4.json"])
    geo = json.loads(paquetes["VIIRS"]["limite_bolivia_geoboundaries.geojson"])

    if geo["type"] == "FeatureCollection":
        bolivia = unary_union([shape(f["geometry"]) for f in geo["features"]])
    elif geo["type"] == "Feature":
        bolivia = shape(geo["geometry"])
    else:
        bolivia = shape(geo)
    if not bolivia.is_valid or bolivia.is_empty:
        raise ValueError("El polígono de Bolivia del ZIP no es válido.")

    if len(viirs_pre) != 203868 or len(modis_pre) != 44932:
        raise ValueError(
            "Los ZIP no coinciden con los verificados: "
            f"VIIRS={len(viirs_pre):,} (esperado 203.868), "
            f"MODIS={len(modis_pre):,} (esperado 44.932)."
        )
    if meta3.get("intervalo_fin") != "2024-09-10":
        raise ValueError("La Etapa 3 no termina el 2024-09-10.")
    if meta4.get("periodo_utc") != ["2024-09-01", "2024-09-10"]:
        raise ValueError("La Etapa 4 no cubre 2024-09-01 a 2024-09-10.")

    comparacion_base = pd.read_csv(
        io.BytesIO(paquetes["MODIS"]["coincidencia_diaria_20km.csv"])
    )
    return Entradas(viirs_pre, modis_pre, meta3, meta4, bolivia, geo, comparacion_base)


# ---------------------------------------------------------------------------
# Descarga NASA
# ---------------------------------------------------------------------------

def _consultar_ventana(
    fuente: str,
    inicio: str,
    map_key: str,
    bolivia,
    session: requests.Session,
    max_intentos: int = 3,
) -> tuple[pd.DataFrame, dict]:
    fecha_fin = str((pd.Timestamp(inicio) + pd.Timedelta(days=4)).date())
    bbox_txt = ",".join(map(str, BBOX))
    url = (
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        f"{map_key}/{fuente}/{bbox_txt}/5/{inicio}"
    )
    respuesta = None
    for intento in range(1, max_intentos + 1):
        try:
            r = session.get(url, timeout=(30, 240))
        except requests.RequestException:
            if intento == max_intentos:
                raise RuntimeError(
                    f"No se pudo descargar {fuente} {inicio}; revisa la conexión."
                ) from None
            time.sleep(3 * intento)
            continue
        if r.status_code == 200:
            respuesta = r.content
            break
        if r.status_code in {429, 500, 502, 503, 504} and intento < max_intentos:
            time.sleep(5 * intento)
            continue
        raise RuntimeError(
            f"NASA HTTP {r.status_code} para {fuente} {inicio}. Revisa la MAP_KEY y los límites."
        )

    try:
        q = pd.read_csv(io.BytesIO(respuesta), low_memory=False)
    except Exception:
        raise RuntimeError(f"La respuesta de NASA no es un CSV válido para {fuente} {inicio}.") from None

    if not COLUMNAS_MINIMAS.issubset(q.columns):
        faltan = sorted(COLUMNAS_MINIMAS - set(q.columns))
        raise RuntimeError(f"Columnas insuficientes en {fuente} {inicio}: {faltan}.")

    if q.empty:
        q = q.copy()
        q["fuente_firms"] = fuente
        q["ventana_descarga"] = inicio
        q["origen"] = "NASA FIRMS API / area"
        registro = {
            "fuente": fuente, "inicio": inicio, "fin": fecha_fin,
            "filas_bbox": 0, "filas_bolivia": 0,
            "sha256_csv_descargado": hashlib.sha256(respuesta).hexdigest(),
            "estado": "sin_detecciones_en_bbox",
        }
        return q, registro

    fechas = pd.to_datetime(q.acq_date, errors="coerce")
    if fechas.isna().any() or not fechas.between(
        pd.Timestamp(inicio), pd.Timestamp(fecha_fin)
    ).all():
        raise RuntimeError(f"Fechas fuera de la ventana de {fuente} {inicio}.")

    lat = pd.to_numeric(q.latitude, errors="coerce")
    lon = pd.to_numeric(q.longitude, errors="coerce")
    if (
        lat.isna().any() or lon.isna().any()
        or not lat.between(-90, 90).all()
        or not lon.between(-180, 180).all()
    ):
        raise RuntimeError(f"Coordenadas no válidas en {fuente} {inicio}.")

    filas_bbox = len(q)
    dentro = contains_xy(bolivia, lon.to_numpy(), lat.to_numpy())
    q = q.loc[dentro].copy()
    q["fuente_firms"] = fuente
    q["ventana_descarga"] = inicio
    q["origen"] = "NASA FIRMS API / area"
    estado = "ok" if len(q) else "sin_detecciones_en_bolivia"
    registro = {
        "fuente": fuente, "inicio": inicio, "fin": fecha_fin,
        "filas_bbox": filas_bbox, "filas_bolivia": len(q),
        "sha256_csv_descargado": hashlib.sha256(respuesta).hexdigest(),
        "estado": estado,
    }
    return q, registro


# ---------------------------------------------------------------------------
# Unión y malla
# ---------------------------------------------------------------------------

def _preparar_periodo(
    prev: pd.DataFrame,
    nuevos: list[pd.DataFrame],
    nombre: str,
    fuente: str,
    fuente_previa: str | None,
    fechas: list[str],
    ventanas: list[str],
    bitacora: list[dict],
) -> tuple[pd.DataFrame, int]:
    orig = prev.copy()
    if "fuente_firms" not in orig.columns:
        orig["fuente_firms"] = fuente_previa or fuente
    if "ventana_descarga" not in orig.columns:
        orig["ventana_descarga"] = "2024-09-01_a_10 (archivo anterior)"
    if "origen" not in orig.columns:
        orig["origen"] = "Etapa 3" if nombre == "VIIRS" else "Etapa 4"

    faltan_min = COLUMNAS_MINIMAS - set(orig.columns)
    if faltan_min:
        raise ValueError(f"{nombre}: faltan columnas mínimas en el archivo previo: {sorted(faltan_min)}.")

    if nuevos:
        extras = set(orig.columns) - set(nuevos[0].columns)
        if extras:
            # No es fatal: se conservan como NaN en las filas nuevas.
            print(f"Aviso {nombre}: columnas del archivo previo ausentes en la API: {sorted(extras)}")

    q = pd.concat([orig] + nuevos, ignore_index=True, sort=False)
    q["acq_date"] = pd.to_datetime(q.acq_date, errors="coerce").dt.strftime("%Y-%m-%d")
    q["type"] = pd.to_numeric(q["type"], errors="coerce")
    q["frp"] = pd.to_numeric(q["frp"], errors="coerce")
    if q[["acq_date", "latitude", "longitude", "type", "frp"]].isna().any().any():
        raise ValueError(f"{nombre} tiene nulos en campos críticos.")

    fechas_prev = pd.to_datetime(prev.acq_date, errors="coerce").dt.strftime("%Y-%m-%d")
    if fechas_prev.min() != "2024-09-01" or fechas_prev.max() != "2024-09-10":
        raise ValueError(f"{nombre}: el archivo previo no cubre 2024-09-01 a 2024-09-10.")

    fuera = sorted(set(q.acq_date.unique()) - set(fechas))
    if fuera:
        raise ValueError(f"{nombre}: fechas fuera del periodo esperado: {fuera}.")

    estados_ok = {"ok", "sin_detecciones_en_bbox", "sin_detecciones_en_bolivia"}
    pares_ok = {(r["fuente"], r["inicio"]) for r in bitacora if r.get("estado") in estados_ok}
    faltan_ventanas = [(fuente, i) for i in ventanas if (fuente, i) not in pares_ok]
    if faltan_ventanas:
        raise ValueError(f"{nombre}: ventanas sin cobertura verificada: {faltan_ventanas}.")

    campos_dup = ["acq_date", "acq_time", "latitude", "longitude", "satellite", "frp"]
    dupli = int(q.duplicated(subset=campos_dup).sum())
    q = q.sort_values(["acq_date", "acq_time", "latitude", "longitude"]).reset_index(drop=True)
    return q, dupli


def _puntos_vegetacion(df: pd.DataFrame, nombre: str) -> pd.DataFrame:
    q = df.loc[df.type.eq(0), ["acq_date", "latitude", "longitude"]].copy()
    q["fecha_utc"] = q.acq_date
    q["sensor"] = nombre
    x, y = TRANS.transform(
        q.longitude.astype(float).to_numpy(),
        q.latitude.astype(float).to_numpy(),
    )
    q["x"] = x
    q["y"] = y
    return q[["fecha_utc", "sensor", "x", "y"]]


def _calendario(puntos: pd.DataFrame, lado_m: int) -> pd.DataFrame:
    q = puntos[["fecha_utc", "sensor", "x", "y"]].copy()
    q["ix"] = np.floor(q.x / lado_m).astype("int32")
    q["iy"] = np.floor(q.y / lado_m).astype("int32")
    t = (
        q.groupby(["fecha_utc", "ix", "iy", "sensor"])
        .size()
        .unstack("sensor", fill_value=0)
        .reset_index()
    )
    for campo in ["MODIS", "VIIRS"]:
        if campo not in t.columns:
            t[campo] = 0
    t["clase"] = np.select(
        [t.MODIS.gt(0) & t.VIIRS.gt(0), t.MODIS.gt(0)],
        ["Ambos", "Solo MODIS"],
        default="Solo VIIRS",
    )
    t["lado_km"] = lado_m // 1000
    return t


def _resumen_cal(t: pd.DataFrame, fechas: list[str]) -> pd.DataFrame:
    g = (
        t.groupby("fecha_utc")
        .agg(
            celdas_union=("clase", "size"),
            ambos=("clase", lambda x: int((x == "Ambos").sum())),
            solo_modis=("clase", lambda x: int((x == "Solo MODIS").sum())),
            solo_viirs=("clase", lambda x: int((x == "Solo VIIRS").sum())),
        )
        .reindex(fechas, fill_value=0)
    )
    g["celdas_modis"] = g.ambos + g.solo_modis
    g["celdas_viirs"] = g.ambos + g.solo_viirs
    g["jaccard"] = (g.ambos / g.celdas_union).where(g.celdas_union > 0)
    return g.reset_index(names="fecha_utc")


# ---------------------------------------------------------------------------
# Resumen por sensor y sensibilidad
# ---------------------------------------------------------------------------

def _resumen_sensor(modis: pd.DataFrame, viirs: pd.DataFrame, fechas: list[str]) -> pd.DataFrame:
    partes = []
    for nombre, df in [("MODIS", modis), ("VIIRS", viirs)]:
        tipo0 = df.loc[df.type.eq(0)].copy()
        g = (
            tipo0.groupby("acq_date")
            .agg(detecciones=("frp", "size"), frp_mediana_mw=("frp", "median"))
            .reindex(fechas)
        )
        g["detecciones"] = g["detecciones"].fillna(0).astype(int)
        g["sensor"] = nombre
        g.index.name = "fecha_utc"
        partes.append(g.reset_index())
    return pd.concat(partes, ignore_index=True)


def _minutos(hhmm) -> float:
    try:
        s = str(int(float(hhmm))).zfill(4)
        return int(s[:2]) * 60 + int(s[2:])
    except Exception:
        return np.nan


def _sensibilidad(
    modis: pd.DataFrame,
    viirs: pd.DataFrame,
    fechas: list[str],
    d20: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, plt.Figure]:
    modis_conf = pd.to_numeric(modis["confidence"], errors="coerce")
    viirs_conf = viirs["confidence"].astype(str).str.lower().str.strip()

    modis_alta = modis[modis.type.eq(0) & modis_conf.ge(UMBRAL_MODIS)].copy()
    viirs_alta = viirs[viirs.type.eq(0) & viirs_conf.eq("h")].copy()

    modis_alta["_sat"] = modis_alta["satellite"].astype(str).str.upper().str.strip()
    viirs_alta["_sat"] = viirs_alta["satellite"].astype(str).str.upper().str.strip()

    modis_aqua = modis_alta[modis_alta["_sat"].eq("A")].copy()
    modis_terra = modis_alta[modis_alta["_sat"].eq("T")].copy()
    if modis_aqua.empty:
        modis_aqua = modis_alta[modis_alta["_sat"].str.startswith("AQUA")].copy()
    if modis_terra.empty:
        modis_terra = modis_alta[modis_alta["_sat"].str.startswith("TERRA")].copy()

    if viirs_alta["_sat"].eq("N").any():
        viirs_snpp = viirs_alta[viirs_alta["_sat"].eq("N")].copy()
    else:
        viirs_snpp = viirs_alta.copy()

    control = pd.DataFrame([
        {"sensor": "MODIS", "grupo": "Terra (alta conf.)", "n": int(len(modis_terra))},
        {"sensor": "MODIS", "grupo": "Aqua (alta conf.)", "n": int(len(modis_aqua))},
        {"sensor": "MODIS", "grupo": f"Alta confianza (>={UMBRAL_MODIS})", "n": int(len(modis_alta))},
        {"sensor": "VIIRS", "grupo": "SNPP (alta conf.)", "n": int(len(viirs_snpp))},
        {"sensor": "VIIRS", "grupo": "Alta confianza (h)", "n": int(len(viirs_alta))},
    ])

    if modis_aqua.empty or viirs_snpp.empty:
        sens = pd.DataFrame({"fecha_utc": fechas})
        sens["union_alta_conf"] = 0
        sens["ambos_dia"] = 0
        sens["ambos_60min"] = 0
        sens["jaccard_dia"] = np.nan
        sens["jaccard_60min"] = np.nan
    else:
        def puntos(df: pd.DataFrame, nombre: str) -> pd.DataFrame:
            q = df.loc[df.type.eq(0), ["acq_date", "latitude", "longitude", "acq_time"]].copy()
            q["fecha_utc"] = q.acq_date
            q["sensor"] = nombre
            q["t"] = q.acq_time.apply(_minutos)
            x, y = TRANS.transform(
                q.longitude.astype(float).to_numpy(),
                q.latitude.astype(float).to_numpy(),
            )
            q["x"] = x
            q["y"] = y
            return q[["fecha_utc", "sensor", "t", "x", "y"]]

        def celdas(p: pd.DataFrame, lado_m: int = 20000) -> pd.DataFrame:
            q = p.dropna(subset=["t"]).copy()
            q["ix"] = np.floor(q.x / lado_m).astype("int32")
            q["iy"] = np.floor(q.y / lado_m).astype("int32")
            return q[["fecha_utc", "ix", "iy", "sensor", "t"]]

        a = celdas(puntos(modis_aqua, "MODIS_Aqua"))
        v = celdas(puntos(viirs_snpp, "VIIRS_SNPP"))

        pres_a = a[["fecha_utc", "ix", "iy"]].drop_duplicates()
        pres_v = v[["fecha_utc", "ix", "iy"]].drop_duplicates()
        pres = pres_a.merge(pres_v, how="outer", indicator=True)
        pres["clase"] = pres["_merge"].map(
            {"both": "Ambos", "left_only": "Solo MODIS", "right_only": "Solo VIIRS"}
        )
        resumen_dia = (
            pres.groupby(["fecha_utc", "clase"]).size().unstack(fill_value=0)
            .reindex(fechas, fill_value=0)
        )
        for c in ["Ambos", "Solo MODIS", "Solo VIIRS"]:
            if c not in resumen_dia.columns:
                resumen_dia[c] = 0
        union_dia = resumen_dia.sum(axis=1)

        if a.empty or v.empty:
            ambos_60 = pd.DataFrame(columns=["fecha_utc", "ix", "iy"])
        else:
            a_sorted = a.sort_values("t")
            v_sorted = v.sort_values("t").assign(_match=1)
            matched = pd.merge_asof(
                a_sorted,
                v_sorted[["fecha_utc", "ix", "iy", "t", "_match"]],
                on="t",
                by=["fecha_utc", "ix", "iy"],
                direction="nearest",
                tolerance=VENTANA_MIN,
            )
            ambos_60 = matched.dropna(subset=["_match"])[
                ["fecha_utc", "ix", "iy"]
            ].drop_duplicates()

        ambos60_dia = (
            ambos_60.groupby("fecha_utc").size().reindex(fechas, fill_value=0)
        )
        sens = pd.DataFrame(index=fechas)
        sens["union_alta_conf"] = union_dia
        sens["ambos_dia"] = resumen_dia["Ambos"]
        sens["ambos_60min"] = ambos60_dia
        sens["jaccard_dia"] = (resumen_dia["Ambos"] / union_dia).where(union_dia > 0)
        sens["jaccard_60min"] = (ambos60_dia / union_dia).where(union_dia > 0)
        sens = sens.reset_index(names="fecha_utc")

    fig, ax = plt.subplots(figsize=(13, 5))
    x2 = np.arange(len(d20))
    ax.plot(x2, d20.jaccard, marker="o", lw=1.5, label="Principal: todos, mismo día (20 km)")
    ax.plot(x2, sens.jaccard_dia, marker="s", lw=1.5, label="Alta conf.: Aqua vs SNPP, mismo día")
    ax.plot(x2, sens.jaccard_60min, marker="^", lw=1.5, label="Alta conf.: Aqua vs SNPP, ±60 min")
    ax.set_ylim(0, 1)
    ax.set_ylabel("Jaccard (0–1)")
    ax.set_xlabel("Fecha UTC")
    ax.set_xticks(x2, [d[-2:] for d in d20.fecha_utc], rotation=90, fontsize=8)
    ax.grid(alpha=0.2, axis="y")
    ax.legend(loc="upper left")
    ax.set_title("Sensibilidad: satélite, confianza y ventana temporal")
    fig.text(
        0.01, 0.005,
        "MODIS Aqua = satélite A, confianza >= 80; VIIRS = SNPP, confianza h. "
        "La línea principal usa todos los píxeles y solo el día UTC.",
        fontsize=7, color="#555555",
    )
    plt.tight_layout(rect=(0, 0.04, 1, 1))
    return sens, control, fig


# ---------------------------------------------------------------------------
# Figuras y mapa
# ---------------------------------------------------------------------------

def _fig_calendario(d20: pd.DataFrame, d10: pd.DataFrame, fechas: list[str], demo: bool) -> plt.Figure:
    fig, axs = plt.subplots(2, 1, figsize=(13, 9), sharex=True)
    x = np.arange(len(d20))
    axs[0].bar(x, d20.ambos, label="Ambos", color="#7653A1")
    axs[0].bar(x, d20.solo_viirs, bottom=d20.ambos, label="Solo VIIRS", color="#D98528")
    axs[0].bar(
        x, d20.solo_modis, bottom=d20.ambos + d20.solo_viirs,
        label="Solo MODIS", color="#2979A8",
    )
    axs[0].set_ylabel("Celdas/día con detección")
    axs[0].legend(loc="upper left")
    prefijo = "DEMO · " if demo else ""
    axs[0].set_title(
        f"{prefijo}Bolivia · {fechas[0]} a {fechas[-1]} UTC · celdas 20 km"
    )
    axs[1].plot(x, d10.jaccard, marker="o", lw=1.5, label="10 km")
    axs[1].plot(x, d20.jaccard, marker="s", lw=1.5, label="20 km")
    axs[1].set_ylim(0, 1)
    axs[1].set_ylabel("Jaccard (0–1)")
    axs[1].legend(loc="upper left")
    axs[1].set_xlabel("Fecha UTC")
    axs[1].set_xticks(x, [d[-2:] for d in d20.fecha_utc], rotation=90, fontsize=8)
    for ax in axs:
        ax.grid(alpha=0.2, axis="y")
    fig.text(
        0.01, 0.005,
        "MODIS = Terra + Aqua (1 km); VIIRS = Suomi-NPP (375 m). Presencia observada, "
        "no área quemada. Mismo día UTC, no misma pasada.",
        fontsize=7, color="#555555",
    )
    plt.tight_layout(rect=(0, 0.03, 1, 1))
    return fig


def _mapa(cal20: pd.DataFrame, fechas: list[str], geo: dict, bolivia, demo: bool) -> tuple[go.Figure, str]:
    if geo["type"] == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in geo["features"]]
    elif geo["type"] == "Feature":
        geoms = [shape(geo["geometry"])]
    else:
        geoms = [shape(geo)]

    xline, yline = [], []
    for geom in geoms:
        polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
        for pol in polys:
            if pol.geom_type != "Polygon":
                continue
            xx, yy = TRANS.transform(*pol.exterior.xy)
            xline += list(xx) + [None]
            yline += list(yy) + [None]

    centros = cal20[["ix", "iy"]].drop_duplicates()
    lon_c, lat_c = INV.transform(
        (centros["ix"].to_numpy() + 0.5) * 20000,
        (centros["iy"].to_numpy() + 0.5) * 20000,
    )
    porc_dentro = 100 * contains_xy(
        bolivia, np.asarray(lon_c), np.asarray(lat_c)
    ).mean()
    if porc_dentro < 80:
        raise RuntimeError(
            f"Control cartográfico: solo {porc_dentro:.1f}% de centros dentro de Bolivia."
        )

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xline, y=yline, mode="lines", name="Límite Bolivia",
        line=dict(color="#173e4a", width=1.4), hoverinfo="skip", showlegend=True,
    ))
    colores = {"Ambos": "#7653A1", "Solo VIIRS": "#D98528", "Solo MODIS": "#2979A8"}
    vistos = set()
    for fecha in fechas:
        q = cal20.loc[cal20.fecha_utc.eq(fecha)]
        for cat in ["Ambos", "Solo VIIRS", "Solo MODIS"]:
            s = q.loc[q.clase.eq(cat)]
            xx = (s["ix"].astype(float).to_numpy() + 0.5) * 20000
            yy = (s["iy"].astype(float).to_numpy() + 0.5) * 20000
            fig.add_trace(go.Scattergl(
                x=xx, y=yy, mode="markers", name=cat,
                marker=dict(color=colores[cat], size=9, symbol="square", opacity=0.8),
                customdata=(
                    np.column_stack([s.MODIS.to_numpy(), s.VIIRS.to_numpy()])
                    if len(s) else None
                ),
                hovertemplate=(
                    "Centro de celda<br>MODIS: %{customdata[0]} píxeles"
                    "<br>VIIRS: %{customdata[1]} píxeles<extra></extra>"
                ),
                showlegend=(cat not in vistos),
                visible=fecha == fechas[0],
            ))
            vistos.add(cat)

    botones = []
    for k, f in enumerate(fechas):
        visibles = [True] + [j // 3 == k for j in range(len(fechas) * 3)]
        botones.append(dict(
            label=f, method="update",
            args=[{"visible": visibles}, {"title": f"Bolivia · {f} UTC · malla 20 km"}],
        ))
    fig.update_layout(
        title=f"Bolivia · {fechas[0]} UTC · malla 20 km",
        updatemenus=[dict(
            buttons=botones, direction="down", x=0.01, y=1.15, xanchor="left",
        )],
        xaxis=dict(title="X EPSG:6933 (m)", scaleanchor="y", scaleratio=1),
        yaxis=dict(title="Y EPSG:6933 (m)"),
        margin=dict(l=65, r=20, t=95, b=65), height=750,
        legend=dict(orientation="h", y=-0.13), template="plotly_white",
    )

    div = plotly_div(
        fig, output_type="div", include_plotlyjs=True,
        config={"displaylogo": False, "responsive": True},
    )
    aviso_demo = (
        "<p class='aviso'><strong>DEMO:</strong> datos sintéticos; no representan "
        "incendios reales.</p>" if demo else ""
    )
    html = (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>LaGeoMaga Etapa8</title>"
        "<style>body{max-width:1200px;margin:0 auto;padding:22px;background:#f5f7fa;"
        "color:#1b3446;font-family:Arial}article{background:white;padding:18px;"
        "border-radius:9px}.aviso{background:#fff4e5;border-left:4px solid #D98528;"
        "padding:10px 14px;margin:12px 0}</style></head><body>"
        f"<h1>{'DEMO — ' if demo else ''}LaGeoMaga FireChronos — Calendario de actividad observada</h1>"
        f"<p>Bolivia, {fechas[0]} a {fechas[-1]} UTC. Malla EPSG:6933, 20 km, tipo 0.</p>"
        f"{aviso_demo}"
        "<p class='aviso'><strong>Advertencia metodológica:</strong> MODIS incluye "
        "Terra + Aqua (1 km) y VIIRS solo Suomi-NPP (375 m). Esto muestra presencia "
        "detectada por celda/día, no área quemada ni exactitud de sensor. "
        "\"Ambos\" significa el mismo día UTC, no la misma pasada.</p>"
        f"<article>{div}</article></body></html>"
    )
    return fig, html


# ---------------------------------------------------------------------------
# Exportación
# ---------------------------------------------------------------------------

def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight")
    return buf.getvalue()


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def _parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def _exportar(res: Resultados, modo: str, demo: bool, duplicados: dict) -> dict[str, bytes]:
    sufijo = f"{res.fechas[0]}_{res.fechas[-1]}"
    archivos: dict[str, bytes] = {}
    archivos[f"MODIS_Bolivia_{sufijo}.parquet"] = _parquet(res.modis)
    archivos[f"VIIRS_Bolivia_{sufijo}.parquet"] = _parquet(res.viirs)
    archivos["calendario_10km.parquet"] = _parquet(res.cal10)
    archivos["calendario_20km.parquet"] = _parquet(res.cal20)
    archivos["resumen_diario_10km.csv"] = _csv(res.d10)
    archivos["resumen_diario_20km.csv"] = _csv(res.d20)
    archivos["resumen_por_sensor.csv"] = _csv(res.resumen_sensor)
    archivos["sensibilidad_jaccard.csv"] = _csv(res.sens)
    archivos["sensibilidad_satelite.csv"] = _csv(res.control_sensibilidad)
    archivos["bitacora_descargas.csv"] = _csv(
        res.bitacora.drop(columns=["sha256_csv_descargado"], errors="ignore")
    )
    archivos["calendario_observado.png"] = _png(res.fig_calendario)
    archivos["sensibilidad_satelite_confianza.png"] = _png(res.fig_sensibilidad)
    archivos["mapa_interactivo.html"] = res.mapa_html.encode("utf-8")

    metadata = {
        "etapa": 8,
        "modo": modo,
        "demo": demo,
        "periodo_utc": [res.fechas[0], res.fechas[-1]],
        "bbox": BBOX,
        "crs": "EPSG:6933",
        "fuentes": FUENTES,
        "registros": {
            "MODIS": int(len(res.modis)),
            "VIIRS": int(len(res.viirs)),
        },
        "type0": {
            "MODIS": int(res.modis.type.eq(0).sum()),
            "VIIRS": int(res.viirs.type.eq(0).sum()),
        },
        "duplicados": duplicados,
        "sensibilidad": {
            "umbral_modis": UMBRAL_MODIS,
            "ventana_min": VENTANA_MIN,
            "filas": int(len(res.sens)),
        },
        "fechas": res.fechas,
        "advertencia": (
            ("DEMO: datos sintéticos. " if demo else "")
            + "Presencia observada por celda/día; no área quemada. "
            "MODIS incluye Terra+Aqua; VIIRS solo Suomi-NPP. "
            "\"Ambos\" = mismo día UTC, no misma pasada."
        ),
        "sha256": {
            nombre: hashlib.sha256(data).hexdigest()
            for nombre, data in archivos.items()
        },
        "librerias": {
            p: _version(p)
            for p in ["pandas", "numpy", "pyarrow", "requests",
                      "shapely", "pyproj", "matplotlib", "plotly"]
        },
    }
    archivos["metadata_etapa8.json"] = json.dumps(
        metadata, indent=2, ensure_ascii=False
    ).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, data in archivos.items():
            z.writestr(nombre, data)
    nombre_zip = (
        "LaGeoMaga_Etapa8_DEMO.zip" if demo else "LaGeoMaga_Etapa8.zip"
    )
    archivos[nombre_zip] = buf.getvalue()
    return archivos


# ---------------------------------------------------------------------------
# Resultados comunes
# ---------------------------------------------------------------------------

def _resultados(
    modo: str,
    demo: bool,
    fechas: list[str],
    modis: pd.DataFrame,
    viirs: pd.DataFrame,
    bitacora: pd.DataFrame,
    comparacion_base: pd.DataFrame,
    geo: dict,
    bolivia,
    duplicados: dict,
    verificar: bool,
) -> Resultados:
    puntos = pd.concat(
        [_puntos_vegetacion(modis, "MODIS"), _puntos_vegetacion(viirs, "VIIRS")],
        ignore_index=True,
    )
    cal10 = _calendario(puntos, 10000)
    cal20 = _calendario(puntos, 20000)
    d10 = _resumen_cal(cal10, fechas)
    d20 = _resumen_cal(cal20, fechas)

    if verificar:
        verif = d20[d20.fecha_utc.le("2024-09-10")].merge(
            comparacion_base, on="fecha_utc", suffixes=("", "_anterior"),
            validate="one_to_one",
        )
        if len(verif) != 10:
            raise ValueError("La verificación contra la Etapa 4 no tiene 10 días.")
        for a, b in [
            ("celdas_modis", "celdas_modis_anterior"),
            ("celdas_viirs", "celdas_viirs_anterior"),
            ("ambos", "celdas_ambos"),
        ]:
            if not (verif[a] == verif[b]).all():
                raise ValueError(f"Control de Etapa 4 no coincide: {a}.")
        if not verif[["jaccard", "jaccard_anterior"]].isna().any().any():
            if not np.allclose(verif.jaccard, verif.jaccard_anterior, atol=1e-10):
                raise ValueError("El Jaccard de los primeros 10 días no coincide con la Etapa 4.")

    resumen = _resumen_sensor(modis, viirs, fechas)
    sens, control, fig_sens = _sensibilidad(modis, viirs, fechas, d20)
    fig_cal = _fig_calendario(d20, d10, fechas, demo)
    mapa_fig, mapa_html = _mapa(cal20, fechas, geo, bolivia, demo)

    metricas = {
        "modo": modo,
        "demo": demo,
        "registros_modis": int(len(modis)),
        "registros_viirs": int(len(viirs)),
        "type0_modis": int(modis.type.eq(0).sum()),
        "type0_viirs": int(viirs.type.eq(0).sum()),
        "dias": len(fechas),
        "jaccard_medio": (
            None if d20.jaccard.isna().all() else float(d20.jaccard.mean())
        ),
        "ventanas": int(len(bitacora)),
    }

    res = Resultados(
        modo=modo,
        demo=demo,
        fechas=fechas,
        modis=modis,
        viirs=viirs,
        cal10=cal10,
        cal20=cal20,
        d10=d10,
        d20=d20,
        resumen_sensor=resumen,
        sens=sens,
        control_sensibilidad=control,
        bitacora=bitacora,
        fig_calendario=fig_cal,
        fig_sensibilidad=fig_sens,
        mapa_fig=mapa_fig,
        mapa_html=mapa_html,
        metricas=metricas,
    )
    res.archivos = _exportar(res, modo, demo, duplicados)
    return res


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo_dia(fecha: str, rng: np.random.Generator, n_clusters: int = 14, cero: bool = False) -> pd.DataFrame:
    columnas = [
        "latitude", "longitude", "acq_date", "acq_time", "satellite",
        "confidence", "frp", "daynight", "type", "sensor",
    ]
    if cero:
        return pd.DataFrame(columns=columnas)

    filas = []

    def fila(sensor, sat, conf, t, lat, lon, tipo=0):
        return {
            "latitude": round(float(lat), 5),
            "longitude": round(float(lon), 5),
            "acq_date": fecha,
            "acq_time": int(t),
            "satellite": sat,
            "confidence": conf,
            "frp": round(float(rng.uniform(5, 45)), 2),
            "daynight": "D",
            "type": int(tipo),
            "sensor": sensor,
        }

    for _ in range(n_clusters):
        lat0 = rng.uniform(-19.0, -13.0)
        lon0 = rng.uniform(-67.0, -61.0)
        for _ in range(int(rng.integers(1, 4))):
            filas.append(fila(
                "MODIS", "A", int(rng.integers(70, 101)),
                1330 + int(rng.integers(-25, 26)),
                lat0 + rng.normal(0, 0.08), lon0 + rng.normal(0, 0.08),
                0 if rng.random() > 0.02 else 1,
            ))
        if rng.random() < 0.55:
            filas.append(fila(
                "MODIS", "T", int(rng.integers(70, 101)),
                1030 + int(rng.integers(-25, 26)),
                lat0 + rng.normal(0, 0.08), lon0 + rng.normal(0, 0.08),
            ))
        t_v = 1500 if rng.random() < 0.2 else 1330
        for _ in range(int(rng.integers(1, 5))):
            filas.append(fila(
                "VIIRS", "N",
                str(rng.choice(["l", "n", "h"], p=[0.2, 0.3, 0.5])),
                t_v + int(rng.integers(-10, 11)),
                lat0 + rng.normal(0, 0.08), lon0 + rng.normal(0, 0.08),
                0 if rng.random() > 0.02 else 1,
            ))
    return pd.DataFrame(filas)


def _demo_entradas() -> Entradas:
    rng = np.random.default_rng(20240901)
    geo = {"type": "Polygon", "coordinates": [[
        (-69.5, -11.0), (-68.0, -10.0), (-65.0, -10.0), (-62.0, -12.0),
        (-60.0, -14.0), (-58.5, -16.0), (-58.0, -18.0), (-59.5, -20.0),
        (-62.0, -22.0), (-65.0, -22.5), (-68.0, -21.0), (-69.5, -19.0),
        (-69.5, -15.0), (-69.5, -11.0),
    ]]}
    bolivia = shape(geo)
    dias = [f"2024-09-{d:02d}" for d in range(1, 11)]
    df = pd.concat([_demo_dia(f, rng) for f in dias], ignore_index=True)
    modis_pre = df[df.sensor == "MODIS"].drop(columns=["sensor"]).reset_index(drop=True)
    viirs_pre = df[df.sensor == "VIIRS"].drop(columns=["sensor"]).reset_index(drop=True)
    modis_pre = modis_pre.loc[contains_xy(
        bolivia, modis_pre.longitude.to_numpy(), modis_pre.latitude.to_numpy()
    )].reset_index(drop=True)
    viirs_pre = viirs_pre.loc[contains_xy(
        bolivia, viirs_pre.longitude.to_numpy(), viirs_pre.latitude.to_numpy()
    )].reset_index(drop=True)
    return Entradas(
        viirs_pre=viirs_pre,
        modis_pre=modis_pre,
        meta3={"fuente": "VIIRS_SNPP_SP", "intervalo_fin": "2024-09-10"},
        meta4={"fuente": "MODIS_SP", "periodo_utc": ["2024-09-01", "2024-09-10"]},
        bolivia=bolivia,
        geo=geo,
        comparacion_base=pd.DataFrame(
            columns=["fecha_utc", "celdas_modis", "celdas_viirs", "celdas_ambos", "jaccard"]
        ),
    )


def ejecutar_demo(
    progreso: Callable[[str, float], None] | None = None,
) -> Resultados:
    """Demo completa con datos sintéticos, sin ZIP ni MAP_KEY."""
    _progreso(progreso, "Generando datos sintéticos...", 0.1)
    entradas = _demo_entradas()
    fechas = pd.date_range("2024-09-01", "2024-09-15").strftime("%Y-%m-%d").tolist()
    ventanas = ["2024-09-11"]
    rng = np.random.default_rng(20240902)

    nuevos = {"MODIS": [], "VIIRS": []}
    bitacora = []
    _progreso(progreso, "Simulando ventanas de NASA...", 0.3)
    for inicio in ventanas:
        fin = (pd.Timestamp(inicio) + pd.Timedelta(days=4)).strftime("%Y-%m-%d")
        dias = pd.date_range(inicio, fin).strftime("%Y-%m-%d").tolist()
        df = pd.concat(
            [_demo_dia(f, rng, cero=(f == "2024-09-13")) for f in dias],
            ignore_index=True,
        )
        for nombre, fuente in FUENTES.items():
            sub = df[df.sensor == nombre].drop(columns=["sensor"]).copy()
            sub["latitude"] = pd.to_numeric(sub["latitude"], errors="coerce")
            sub["longitude"] = pd.to_numeric(sub["longitude"], errors="coerce")
            sub = sub.dropna(subset=["latitude", "longitude"])
            filas_bbox = len(sub)
            if filas_bbox:
                dentro = contains_xy(
                    entradas.bolivia, sub.longitude.to_numpy(), sub.latitude.to_numpy()
                )
                sub = sub.loc[dentro].copy()
            sub["fuente_firms"] = fuente
            sub["ventana_descarga"] = inicio
            sub["origen"] = "DEMO sintético"
            nuevos[nombre].append(sub)
            bitacora.append({
                "fuente": fuente, "inicio": inicio, "fin": fin,
                "filas_bbox": filas_bbox, "filas_bolivia": len(sub),
                "sha256_csv_descargado": hashlib.sha256(
                    f"{fuente}{inicio}".encode()
                ).hexdigest(),
                "estado": "ok" if len(sub) else "sin_detecciones_en_bolivia",
            })

    _progreso(progreso, "Uniendo y validando...", 0.5)
    modis, dup_m = _preparar_periodo(
        entradas.modis_pre, nuevos["MODIS"], "MODIS", "MODIS_SP",
        entradas.meta4.get("fuente"), fechas, ventanas, bitacora,
    )
    viirs, dup_v = _preparar_periodo(
        entradas.viirs_pre, nuevos["VIIRS"], "VIIRS", "VIIRS_SNPP_SP",
        entradas.meta3.get("fuente"), fechas, ventanas, bitacora,
    )

    _progreso(progreso, "Construyendo calendario y figuras...", 0.7)
    res = _resultados(
        modo="PRUEBA", demo=True, fechas=fechas, modis=modis, viirs=viirs,
        bitacora=pd.DataFrame(bitacora), comparacion_base=entradas.comparacion_base,
        geo=entradas.geo, bolivia=entradas.bolivia,
        duplicados={"MODIS": dup_m, "VIIRS": dup_v}, verificar=False,
    )
    _progreso(progreso, "Demo lista.", 1.0)
    return res


# ---------------------------------------------------------------------------
# Ejecución real
# ---------------------------------------------------------------------------

def ejecutar_real(
    zips: list[tuple[str, bytes]],
    map_key: str,
    modo: str = "PRUEBA",
    progreso: Callable[[str, float], None] | None = None,
) -> Resultados:
    """Ejecuta el pipeline con ZIP reales y NASA FIRMS."""
    if modo not in {"PRUEBA", "MES_COMPLETO"}:
        raise ValueError("modo debe ser PRUEBA o MES_COMPLETO.")
    if not map_key or len(map_key) < 16 or not map_key.isalnum():
        raise ValueError("La MAP_KEY no tiene el formato esperado.")

    _progreso(progreso, "Leyendo ZIP de entrada...", 0.05)
    entradas = cargar_entradas(zips)
    fechas = (
        pd.date_range("2024-09-01", "2024-09-15").strftime("%Y-%m-%d").tolist()
        if modo == "PRUEBA"
        else pd.date_range("2024-09-01", "2024-09-30").strftime("%Y-%m-%d").tolist()
    )
    ventanas = (
        ["2024-09-11"] if modo == "PRUEBA"
        else ["2024-09-11", "2024-09-16", "2024-09-21", "2024-09-26"]
    )

    session = requests.Session()
    nuevos = {"MODIS": [], "VIIRS": []}
    bitacora = []
    total = len(ventanas) * len(FUENTES)
    hechos = 0
    for nombre, fuente in FUENTES.items():
        for inicio in ventanas:
            hechos += 1
            _progreso(
                progreso,
                f"Descargando {nombre} {inicio} ({hechos}/{total})...",
                0.05 + 0.65 * hechos / total,
            )
            df, registro = _consultar_ventana(
                fuente, inicio, map_key, entradas.bolivia, session
            )
            nuevos[nombre].append(df)
            bitacora.append(registro)

    _progreso(progreso, "Uniendo y validando...", 0.75)
    modis, dup_m = _preparar_periodo(
        entradas.modis_pre, nuevos["MODIS"], "MODIS", "MODIS_SP",
        entradas.meta4.get("fuente"), fechas, ventanas, bitacora,
    )
    viirs, dup_v = _preparar_periodo(
        entradas.viirs_pre, nuevos["VIIRS"], "VIIRS", "VIIRS_SNPP_SP",
        entradas.meta3.get("fuente"), fechas, ventanas, bitacora,
    )

    _progreso(progreso, "Construyendo calendario y figuras...", 0.85)
    res = _resultados(
        modo=modo, demo=False, fechas=fechas, modis=modis, viirs=viirs,
        bitacora=pd.DataFrame(bitacora), comparacion_base=entradas.comparacion_base,
        geo=entradas.geo,
        bolivia=entradas.bolivia,
        duplicados={"MODIS": dup_m, "VIIRS": dup_v}, verificar=True,
    )
    _progreso(progreso, "Listo.", 1.0)
    return res
