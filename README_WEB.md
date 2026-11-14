# App web — LaGeoMaga FireChronos, Etapa 8

**Archivos:**

```text
app.py                  Interfaz Streamlit
pipeline.py             Lógica del pipeline (demo y real)
requirements.txt        Dependencias
.streamlit/config.toml  Tema y límite de subida
```

La app ofrece dos modos:

- **Demo:** datos sintéticos, sin ZIP ni MAP_KEY. Ideal para probar en línea.
- **Real:** sube los ZIP de Etapa 3/4, pega la MAP_KEY de NASA FIRMS y ejecuta.

---

## 1. Ejecutar en local

```powershell
cd C:\Users\Administrador\Desktop\Nor_data
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

streamlit run app.py
```

Se abre en `http://localhost:8501`.

### Modo demo

1. En la barra lateral, deja **Demo (sin credenciales)**.
2. Pulsa **Ejecutar demo**.
3. Revisa las pestañas y descarga el ZIP.

### Modo real

1. Elige **Real (ZIP + MAP_KEY)**.
2. Sube `Etapa3.zip` y `Etapa4.zip`.
3. Pega la MAP_KEY (se usa solo en memoria).
4. Elige `PRUEBA` (1–15 sep) o `MES_COMPLETO` (1–30 sep).
5. Pulsa **Ejecutar pipeline**.

**Contenido esperado de los ZIP:**

```text
Etapa3.zip
├── VIIRS_Bolivia_septiembre_2024.parquet
├── metadata_etapa3.json
└── limite_bolivia_geoboundaries.geojson

Etapa4.zip
├── MODIS_Bolivia_1_10sep2024.parquet
├── metadata_etapa4.json
└── coincidencia_diaria_20km.csv
```

---

## 2. Desplegar en línea

### Opción A — Streamlit Community Cloud (recomendada)

1. Crea un repositorio en GitHub con:
   - `app.py`
   - `pipeline.py`
   - `requirements.txt`
   - `.streamlit/config.toml`
2. Entra a https://share.streamlit.io/ y conecta el repositorio.
3. Selecciona:
   - **Main file:** `app.py`
   - **Python:** 3.11 o 3.12
4. Deploy.

No necesitas configurar secretos: cada usuario introduce su MAP_KEY en la app.

**Límites del plan gratuito:** ~1 GB de RAM y CPU compartida. Para el mes completo
(30 días) conviene ejecutar en local o Colab; en la nube usa `PRUEBA`.

### Opción B — Hugging Face Spaces

1. Crea un Space con SDK **Streamlit**.
2. Sube `app.py`, `pipeline.py`, `requirements.txt` y `.streamlit/config.toml`.
3. El Space instala las dependencias y arranca solo.

### Opción C — Render / Railway / VPS

Comando de arranque:

```bash
streamlit run app.py --server.port $PORT --server.address 0.0.0.0
```

Asegúrate de exponer el puerto y de tener `requirements.txt` en la raíz.

---

## 3. Qué genera

- `MODIS_Bolivia_<periodo>.parquet`
- `VIIRS_Bolivia_<periodo>.parquet`
- `calendario_10km.parquet` y `calendario_20km.parquet`
- `resumen_diario_10km.csv` y `resumen_diario_20km.csv`
- `resumen_por_sensor.csv`
- `sensibilidad_jaccard.csv` y `sensibilidad_satelite.csv`
- `bitacora_descargas.csv`
- `calendario_observado.png`
- `sensibilidad_satelite_confianza.png`
- `mapa_interactivo.html`
- `metadata_etapa8.json`
- `LaGeoMaga_Etapa8.zip` (o `LaGeoMaga_Etapa8_DEMO.zip`)

El ZIP incluye todos los archivos y se descarga desde la pestaña **Descargas**.

---

## 4. Seguridad y privacidad

- La MAP_KEY se pasa a la función y se usa solo en memoria; **no se guarda** en disco
  ni en la sesión del servidor.
- Los ZIP subidos se procesan en memoria; no se escriben en disco.
- La app no registra la clave ni las URLs con clave.
- En un despliegue público, cada usuario debe usar su propia MAP_KEY.
- No compartas capturas con la clave visible.

---

## 5. Pruebas

```powershell
# Lógica del modo real con NASA simulada
python test_pipeline_real.py

# UI en modo demo (sin navegador)
python test_app.py

# Pipeline de demostración
python -c "import pipeline; r=pipeline.ejecutar_demo(); print(r.metricas)"
```

También puedes levantar el servidor y comprobar que responde:

```powershell
streamlit run app.py
```

---

## 6. Limitaciones

- **Memoria:** el mes completo con VIIRS puede ser pesado para el plan gratuito.
  Usa `PRUEBA` en la nube y `MES_COMPLETO` en local.
- **Tiempo:** las descargas de NASA pueden tardar varios minutos; la app muestra
  progreso por ventana.
- **ZIP exactos:** la app valida los conteos 203.868 / 44.932; si tus ZIP son otros,
  fallará con un mensaje claro.
- **Sin persistencia:** si cierras la pestaña, los resultados se pierden; descarga el ZIP.
- **Datos sintéticos:** el modo demo no sirve para conclusiones sobre incendios reales.

---

## 7. Problemas frecuentes

| Error | Causa | Solución |
|---|---|---|
| `ModuleNotFoundError: streamlit` | Entorno sin dependencias | `pip install -r requirements.txt`. |
| `ZIP no identificado` | Nombres internos distintos | Revisa el contenido del ZIP. |
| `Los ZIP no coinciden con los verificados` | Conteos distintos | Usa los ZIP originales. |
| `NASA HTTP 403/429` | MAP_KEY inválida o límite | Revisa la clave o espera. |
| La app se reinicia al cambiar un widget | Comportamiento normal de Streamlit | Vuelve a pulsar Ejecutar. |
| El modo `MES_COMPLETO` falla por memoria | Límite del plan gratuito | Usa `PRUEBA` o ejecuta en local. |
