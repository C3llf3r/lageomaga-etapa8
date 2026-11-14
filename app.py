"""
LaGeoMaga FireChronos — Etapa 8
App web (Streamlit) para ejecutar el pipeline en local o en línea.

Ejecutar en local:
    streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from pipeline import ejecutar_demo, ejecutar_real

st.set_page_config(
    page_title="LaGeoMaga FireChronos — Etapa 8",
    page_icon="🔥",
    layout="wide",
)


def _mime(nombre: str) -> str:
    if nombre.endswith(".csv"):
        return "text/csv"
    if nombre.endswith(".json"):
        return "application/json"
    if nombre.endswith(".png"):
        return "image/png"
    if nombre.endswith(".html"):
        return "text/html"
    if nombre.endswith(".zip"):
        return "application/zip"
    if nombre.endswith(".parquet"):
        return "application/octet-stream"
    return "application/octet-stream"


st.title("LaGeoMaga FireChronos — Etapa 8")
st.caption(
    "Calendario de actividad observada MODIS/VIIRS · Bolivia · septiembre 2024 · "
    "malla EPSG:6933"
)

if "resultados" not in st.session_state:
    st.session_state["resultados"] = None

with st.sidebar:
    st.header("Configuración")
    modo_app = st.radio(
        "Modo de ejecución",
        ["Demo (sin credenciales)", "Real (ZIP + MAP_KEY)"],
        index=0,
        help="El modo demo usa datos sintéticos. El modo real descarga NASA FIRMS.",
    )

    if modo_app.startswith("Demo"):
        st.info(
            "Genera datos sintéticos, simula las ventanas de NASA y produce "
            "figuras, mapa y ZIP. No necesita MAP_KEY."
        )
        zips: list[tuple[str, bytes]] = []
        map_key = ""
        periodo = "PRUEBA"
        ejecutar = st.button("Ejecutar demo", type="primary", width="stretch")
    else:
        zip3 = st.file_uploader("Etapa 3 — VIIRS (.zip)", type=["zip"])
        zip4 = st.file_uploader("Etapa 4 — MODIS (.zip)", type=["zip"])
        map_key = st.text_input(
            "MAP_KEY NASA FIRMS",
            type="password",
            help="Se usa solo en memoria para esta ejecución; no se guarda.",
        )
        periodo_label = st.selectbox(
            "Periodo",
            ["PRUEBA — 1–15 sep", "MES_COMPLETO — 1–30 sep"],
            index=0,
        )
        periodo = "PRUEBA" if periodo_label.startswith("PRUEBA") else "MES_COMPLETO"
        st.caption(
            "La clave se usa solo durante la ejecución y no se almacena. "
            "El modo MES_COMPLETO puede tardar varios minutos."
        )
        zips = []
        if zip3 is not None:
            zips.append((zip3.name, zip3.getvalue()))
        if zip4 is not None:
            zips.append((zip4.name, zip4.getvalue()))
        ejecutar = st.button("Ejecutar pipeline", type="primary", width="stretch")

    st.divider()
    st.markdown(
        "**Datos:** NASA FIRMS `MODIS_SP` y `VIIRS_SNPP_SP`  \n"
        "**Salida:** Parquet, CSV, PNG, HTML y ZIP  \n"
        "**Modo demo:** sin claves ni archivos"
    )
    st.caption(
        "La app no guarda archivos subidos ni la MAP_KEY. "
        "Cada ejecución trabaja en memoria."
    )

if ejecutar:
    st.session_state["resultados"] = None
    progreso = st.progress(0.0)
    estado = st.empty()

    def _cb(mensaje: str, fraccion: float) -> None:
        estado.info(mensaje)
        progreso.progress(max(0.0, min(1.0, float(fraccion))))

    try:
        if modo_app.startswith("Demo"):
            resultados = ejecutar_demo(progreso=_cb)
        else:
            if len(zips) != 2:
                raise ValueError("Sube los dos ZIP: Etapa 3 (VIIRS) y Etapa 4 (MODIS).")
            resultados = ejecutar_real(zips, map_key, periodo, progreso=_cb)
        st.session_state["resultados"] = resultados
        progreso.empty()
        estado.empty()
        st.success("Ejecución completada.")
    except Exception as exc:
        progreso.empty()
        estado.empty()
        st.error(f"No se pudo completar: {exc}")
        with st.expander("Ver detalle técnico"):
            st.exception(exc)

resultados = st.session_state.get("resultados")

if resultados is None:
    st.markdown(
        """
### Cómo funciona

1. **Modo demo:** pulsa **Ejecutar demo** en la barra lateral. No necesitas nada más.
2. **Modo real:** sube los ZIP de Etapa 3 y Etapa 4, pega tu MAP_KEY de NASA FIRMS
   y elige el periodo.
3. Al terminar, revisa las pestañas y descarga el ZIP completo.

> El modo demo usa datos **sintéticos**. No sirve para interpretar incendios reales;
> sirve para probar el flujo, las figuras y la exportación.
"""
    )
    st.stop()

res = resultados

if res.demo:
    st.warning(
        "**DEMO:** datos sintéticos. No representan incendios reales de Bolivia."
    )

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Días", res.metricas["dias"])
c2.metric("Registros MODIS", f"{res.metricas['registros_modis']:,}")
c3.metric("Registros VIIRS", f"{res.metricas['registros_viirs']:,}")
c4.metric(
    "Jaccard medio 20 km",
    "—" if res.metricas["jaccard_medio"] is None
    else f"{res.metricas['jaccard_medio']:.3f}",
)
c5.metric("Ventanas", res.metricas["ventanas"])

tabs = st.tabs(["Resumen", "Calendario", "Sensibilidad", "Mapa", "Descargas"])

with tabs[0]:
    st.subheader("Resumen diario · malla 20 km")
    st.dataframe(res.d20, width="stretch", hide_index=True)
    st.subheader("Resumen por sensor")
    st.dataframe(res.resumen_sensor, width="stretch", hide_index=True)
    st.subheader("Bitácora de ventanas")
    st.dataframe(
        res.bitacora.drop(columns=["sha256_csv_descargado"], errors="ignore"),
        width="stretch",
        hide_index=True,
    )

with tabs[1]:
    st.subheader("Calendario de presencia observada")
    st.pyplot(res.fig_calendario, width="stretch")
    st.download_button(
        "Descargar PNG",
        data=res.archivos["calendario_observado.png"],
        file_name="calendario_observado.png",
        mime="image/png",
        key="dl_calendario",
    )
    st.dataframe(res.d10, width="stretch", hide_index=True)

with tabs[2]:
    st.subheader("Sensibilidad: satélite, confianza y ventana temporal")
    st.pyplot(res.fig_sensibilidad, width="stretch")
    st.dataframe(res.sens, width="stretch", hide_index=True)
    st.dataframe(res.control_sensibilidad, width="stretch", hide_index=True)
    st.download_button(
        "Descargar CSV de sensibilidad",
        data=res.archivos["sensibilidad_jaccard.csv"],
        file_name="sensibilidad_jaccard.csv",
        mime="text/csv",
        key="dl_sens",
    )

with tabs[3]:
    st.subheader("Mapa interactivo")
    st.plotly_chart(res.mapa_fig, width="stretch")
    st.download_button(
        "Descargar HTML del mapa",
        data=res.archivos["mapa_interactivo.html"],
        file_name="mapa_interactivo.html",
        mime="text/html",
        key="dl_mapa",
    )
    st.caption(
        "Los cuadrados son centros de celdas con detección, no polígonos de incendios. "
        "«Ambos» significa el mismo día UTC, no la misma pasada."
    )

with tabs[4]:
    st.subheader("Descargar resultados")
    nombre_zip = "LaGeoMaga_Etapa8_DEMO.zip" if res.demo else "LaGeoMaga_Etapa8.zip"
    st.download_button(
        "Descargar ZIP completo",
        data=res.archivos[nombre_zip],
        file_name=nombre_zip,
        mime="application/zip",
        type="primary",
        key="dl_zip",
    )
    st.markdown("**Archivos individuales**")
    for nombre, data in sorted(res.archivos.items()):
        if nombre == nombre_zip:
            continue
        st.download_button(
            f"{nombre}",
            data=data,
            file_name=nombre,
            mime=_mime(nombre),
            key=f"dl_{nombre}",
        )
