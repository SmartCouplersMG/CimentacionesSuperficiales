# Diseño de Cimentaciones Superficiales — v6

Aplicación web interactiva desarrollada con **Streamlit** para el predimensionamiento y diseño estructural de cimentaciones superficiales aisladas y combinadas, incluyendo el diseño de vigas de cimentación (sistemas de enlace).

> **Desarrollada con el modelo de Inteligencia Artificial Claude Opus 4.6 de Anthropic.**
> Base teórica: *Foundation Analysis and Design* — Joseph E. Bowles (5ª Ed.) y *Diseño de Concreto Reforzado* — Jack C. McCormac (8ª Ed.).

---

## ⚠ Advertencias Importantes

- Esta aplicación **no ha sido auditada ni probada exhaustivamente**. Sus resultados pueden contener errores. Verifique siempre con un ingeniero estructural calificado.
- Fue generada con IA. Los modelos de IA pueden producir resultados plausibles pero incorrectos (*alucinaciones*). **No se garantiza la exactitud de ningún cálculo.**
- **Uso exclusivamente didáctico y educativo.** No es válida para uso profesional, proyectos reales, permisos de construcción ni toma de decisiones estructurales.
- Los autores no asumen responsabilidad por daños derivados del uso de esta herramienta.

---

## Índice

1. [Requisitos e Instalación](#requisitos-e-instalación)
2. [Ejecución](#ejecución)
3. [Arquitectura del Proyecto](#arquitectura-del-proyecto)
4. [Flujo de Trabajo (Pasos 0 → 5)](#flujo-de-trabajo)
5. [Módulos y Funciones](#módulos-y-funciones)
   - [app.py — Interfaz Principal](#apppy--interfaz-principal)
   - [engine.py — Orquestador](#enginepy--orquestador)
   - [parser.py — Lectura del Modelo SAP2000](#parserpy--lectura-del-modelo-sap2000)
   - [isolated.py — Diseño de Zapatas Aisladas](#isolatedpy--diseño-de-zapatas-aisladas)
   - [combined.py — Diseño de Zapatas Combinadas](#combinedpy--diseño-de-zapatas-combinadas)
   - [tie_system.py — Sistemas de Enlace](#tie_systempy--sistemas-de-enlace)
   - [export_s2k.py — Exportación a SAP2000](#export_s2kpy--exportación-a-sap2000)
6. [Parámetros de Diseño (Sidebar)](#parámetros-de-diseño-sidebar)
7. [Estructuras de Datos Clave](#estructuras-de-datos-clave)
8. [Normas y Códigos Aplicados](#normas-y-códigos-aplicados)
9. [Constantes y Tolerancias](#constantes-y-tolerancias)

---

## Requisitos e Instalación

**Python requerido:** 3.9 o superior

```bash
# 1. Clonar o copiar la carpeta del proyecto
cd cimentaciones_apptest

# 2. Crear entorno virtual (recomendado)
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Linux/macOS

# 3. Instalar dependencias
pip install -r requirements.txt
```

**Dependencias (`requirements.txt`):**

| Librería | Versión mínima | Uso |
|----------|---------------|-----|
| `streamlit` | ≥ 1.32.0 | Framework de interfaz web |
| `pandas` | ≥ 2.0.0 | Manipulación de tablas y datos |
| `numpy` | ≥ 1.24.0 | Álgebra lineal y cálculo numérico |
| `plotly` | ≥ 5.18.0 | Visualizaciones interactivas |
| `openpyxl` | ≥ 3.1.0 | Exportación a Excel |
| `Pillow` | (via streamlit) | Procesamiento de logo para Excel |

---

## Ejecución

```bash
streamlit run app.py
```

La aplicación queda disponible en `http://localhost:8501`.

---

## Arquitectura del Proyecto

```
cimentaciones_apptest/
│
├── app.py              # Interfaz Streamlit — orquesta los pasos y la UI
├── engine.py           # Motor principal — lectura de modelo y ejecución del diseño
├── parser.py           # Parseo de archivos .s2k de SAP2000
├── isolated.py         # Diseño estructural de zapatas aisladas
├── combined.py         # Diseño de zapatas combinadas (solapamiento)
├── tie_system.py       # Análisis y diseño de vigas de cimentación
├── export_s2k.py       # Exportación del modelo fundacional a SAP2000 (.s2k)
│
├── assets/
│   ├── logo.png        # Logo de Smart Couplers MG
│   └── logo2.png       # Ícono de página
│
└── requirements.txt
```

---

## Flujo de Trabajo

La aplicación guía al usuario en **6 pasos secuenciales**. Cada paso habilita el siguiente.

---

### PASO 0 — Carga y Diagnóstico del Modelo

**Qué hace:**
- Solicita al usuario cargar un archivo `.s2k` (modelo estructural de SAP2000).
- Valida que el archivo sea un modelo SAP2000 válido (verifica la firma `TABLE:`).
- Llama a `engine.read_model()` para parsear el modelo.
- Diagnostica automáticamente la fuente de cargas:
  - **Opción A:** Cargas en nudos (`JOINT LOADS`) → usadas si el modelo fue corrido con cargas directas.
  - **Opción B:** Reacciones en apoyos (`JOINT CONSTRAINTS`) → usadas si el modelo tiene restricciones.
  - **Ambiguo:** Presenta al usuario un selector para elegir.
- Muestra métricas: número de nudos con cargas, restricciones, entidades de diseño detectadas.
- Presenta la vista en planta con las columnas detectadas.

**Pide al usuario:**
- Archivo `.s2k` (drag & drop o botón).
- Si hay ambigüedad de fuente: seleccionar Opción A o B.

---

### PASO 1 — Clasificación de Columnas

**Qué hace:**
- Muestra una tabla editable con todas las columnas/entidades de diseño.
- El usuario asigna a cada columna su **tipo de ubicación** respecto al lote:
  - `concentrica` — zapata centrada, sin restricciones de borde.
  - `medianera` — un borde del lote restringe un lado (X+ / X- / Y+ / Y-).
  - `esquinera` — dos bordes restringen dos lados (X+Y+, X+Y-, etc.).
- Permite ingresar momentos y cortantes adicionales manuales (`Mpx`, `Mpy`, `Vux`, `Vuy`) en la base de la columna, por si el modelo no los incluye directamente.
- Al presionar **"Aplicar clasificación"**:
  - Deduce automáticamente los sistemas de enlace necesarios (`tie_system.deduce_tie_beams()`).
  - Calcula posiciones preliminares de las zapatas.
  - Actualiza la vista en planta con colores por tipo de ubicación.

**Resultado:**
- `st.session_state.classifications`: diccionario de clasificaciones por nudo.
- `st.session_state.tie_beams_table`: propuesta inicial de vigas de enlace.

---

### PASO 2 — Definición de Vigas de Cimentación

**Qué hace:**
- Muestra la vista en planta con zapatas preliminares y vigas de enlace propuestas.
- Permite al usuario editar, eliminar o agregar manualmente vigas de enlace.
- Cada fila de la tabla contiene:
  - Nudo origen, nudo destino, dirección (X o Y).
- Botón **"Agregar viga manual"** para incorporar conexiones no deducidas automáticamente.
- Al confirmar, construye los sistemas de enlace con `tie_system.build_tie_systems()`.

**Resultado:**
- `st.session_state.tie_beams_table`: lista final de conexiones de enlace.

---

### PASO 3 — Ejecución del Diseño

**Qué hace:**
- Llama a `engine.run_design()` que ejecuta:
  1. Optimización de zapatas aisladas (barrido B×L×h).
  2. Detección de solapamientos → diseño de zapatas combinadas.
  3. Análisis y diseño de sistemas de enlace.
- Muestra un resumen rápido: zapatas diseñadas, sistemas de enlace, iteraciones de convergencia.

---

### PASO 4 — Verificación y Ajuste Manual

**Qué hace:**
- Presenta una tabla editable con las dimensiones finales (`B`, `L`, `h`) de cada zapata.
- El usuario puede modificar dimensiones y ejecutar **"Re-verificar"** para recalcular presiones, cortante, punzonamiento y acero con las dimensiones modificadas.
- No re-optimiza; solo verifica con las dimensiones dadas.

---

### PASO 5 — Resultados y Exportación

Organizado en pestañas:

| Pestaña | Contenido |
|---------|-----------|
| **Planta** | Vista interactiva con zapatas, vigas de enlace, colores de estado |
| **Resumen** | Tabla consolidada de dimensiones, presiones, factores de seguridad y acero |
| **Zapatas Aisladas** | Detalle de cada zapata: ADS (presiones) y LRFD (punzonamiento, cortante, flexión) |
| **Zapatas Combinadas** | Diagrama de momentos y cortantes longitudinales; distribución de acero |
| **Sistemas de Enlace** | Diagramas V-M, sección de la viga, refuerzo |
| **Auditorías** | Tablas completas ADS y LRFD por combinación de carga |
| **Exportar** | Generar `.s2k` para SAP2000 y/o informe Excel |

---

## Módulos y Funciones

---

### `app.py` — Interfaz Principal

Archivo principal de Streamlit. Contiene toda la lógica de UI y orquesta las llamadas a los demás módulos.

#### Helpers Visuales

```python
apply_theme(fig, height=500, equal_axes=True)
```
Aplica estilo visual uniforme a todas las figuras Plotly. Configura ejes iguales, fondo blanco, grid suave y altura de figura.

```python
get_preliminary_position(col, cl_info)
```
Calcula la posición conceptual del centro de la zapata dado el tipo de ubicación de la columna.
- `col`: diccionario de columna (`x`, `y`, `bx`, `by`).
- `cl_info`: clasificación (`location`, `side`, `corner`).
- **Retorna:** `(x_zap, y_zap)` — coordenadas del centro tentativo.

#### Conversión de Tablas de Vigas de Enlace

```python
ties_dict_to_table(ties, columns)
```
Convierte el diccionario interno de vigas de enlace (generado por `deduce_tie_beams`) en filas editables para el `st.data_editor`.
- **Retorna:** lista de dicts `{from_joint, to_joint, direction, origin}`.

```python
table_to_ties_dict(tie_rows, columns)
```
Proceso inverso: toma las filas editadas por el usuario y reconstruye el dict interno del motor.
Valida: existencia de nudos, que la conexión sea geométricamente coherente (alineación X o Y), y elimina duplicados.
- **Retorna:** dict de vigas validadas.

#### Constantes de Visualización

```python
LOC_COLORS    # Colores por tipo: concentrica(azul), medianera(naranja), esquinera(rojo)
STATUS_COLORS # Colores por estado: OK(verde), REVISION(amarillo), NO_CUMPLE(rojo)
REBAR_SIZES   # Tamaños de varilla disponibles: #4, #5, #6
SPACINGS      # Espaciados: [30, 25, 20, 17.5, 15, 12.5, 10] cm
```

---

### `engine.py` — Orquestador

Coordina la lectura del modelo y la ejecución secuencial del diseño. No contiene lógica de cálculo estructural directamente.

#### Fase 0 — Lectura del Modelo

```python
read_model(file_path, params)
```
Función principal de diagnóstico. Parsea el archivo `.s2k`, detecta entidades de diseño y determina la fuente de cargas.

**Entradas:**
- `file_path` — ruta al archivo `.s2k`.
- `params` — parámetros de diseño del sidebar.

**Proceso:**
1. Llama a `parser.parse_s2k()` para extraer todas las tablas.
2. Extrae nudos, marcos, secciones, cargas y restricciones.
3. Llama a `parser.inspect_foundation_sources()` para decidir fuente.
4. Construye entidades de diseño (columnas + muros equivalentes).
5. Clasifica combinaciones de carga con `parser.gen_combos()`.

**Retorna:** `model_data` dict con:

| Clave | Descripción |
|-------|-------------|
| `basis_mode` | `"joint_loads"` / `"support_reactions"` / `"ask_user"` / `"invalid_model"` |
| `status_message` | Mensaje narrativo para el usuario |
| `design_entities` | Lista de entidades de diseño (columnas + muros) |
| `columns` | Lista simplificada de columnas para la UI |
| `foundation_entities` | Candidatos a cimentación con sus cargas |
| `ads_combos` | Combinaciones ADS generadas |
| `lrfd_combos` | Combinaciones LRFD generadas |
| `_diag` | Información diagnóstica interna |

```python
resolve_basis_selection(model_data, choice, params)
```
Re-ejecuta `read_model` forzando la selección del usuario cuando `basis_mode == "ask_user"`.
- `choice`: `"A"` (cargas en nudos) o `"B"` (reacciones en apoyos).

#### Fase 3-5 — Ejecución del Diseño

```python
run_design(model_data, classifications, tie_beams, params)
```
Orquesta el diseño completo. Llamada única desde `app.py` al presionar "Ejecutar diseño".

**Proceso interno:**
1. Para cada entidad de diseño: llama a `isolated.optimize_isolated()`.
2. Detecta solapamientos con `combined.check_overlaps()`.
3. Para grupos solapados: llama a `combined.design_combined_footing()`.
4. Construye sistemas de enlace con `tie_system.build_tie_systems()`.
5. Para cada sistema: llama a `tie_system.analyze_tie_system()`.
6. Ensambla `results` con `final_footings` y `tie_systems`.

**Retorna:** `results` dict con:
- `final_footings` — lista de zapatas diseñadas.
- `tie_systems` — lista de sistemas de enlace diseñados.
- `convergence_info` — información de iteraciones.

#### Funciones Auxiliares Internas

```python
_normalize_joint_id(v)
```
Normaliza IDs de nudos a string limpio: `"1.0"` → `"1"`, `1` → `"1"`.

```python
build_jloads_from_sap_reactions_excel(df, foundation_entities)
```
Construye el diccionario `jloads` a partir de una tabla de reacciones SAP2000 importada en Excel.
- Aplica convención de signos: multiplica por `-1.0` (reacciones SAP son opuestas a cargas sobre cimentación).
- Filtra filas con `StepType = MAX` cuando existen.

```python
_supports_to_foundation_entities(supports)
```
Convierte el dict de apoyos (desde restricciones) al formato unificado de entidades de diseño. Preserva sección, coordenadas y advertencias de nudos huérfanos.

```python
_foundation_entities_to_columns(entities)
```
Simplifica entidades para compatibilidad con la UI (extrae `x`, `y`, `bx`, `by`, `section`).

```python
build_wall_foundation_entities(wall_entities, wall_resultants, joints, default_support_width)
```
Construye entidades de diseño para muros lineales. Determina ancho de soporte desde el espesor del muro. Preserva geometría de segmento (x0, y0, x1, y1).

```python
build_wall_as_column_entities(wall_entities, wall_resultants, joints)
```
Convierte un muro a una columna equivalente puntual en el centroide geométrico. La sección equivalente es `longitud × espesor`.

---

### `parser.py` — Lectura del Modelo SAP2000

Módulo de bajo nivel para parseo de archivos `.s2k` y generación de combinaciones de carga.

#### Parseo del Archivo

```python
parse_s2k(fp)
```
Lee el archivo `.s2k` completo y lo divide en tablas por encabezado `TABLE:`.
Maneja continuación de líneas con `_` al final. Retorna `dict[nombre_tabla → lista_de_líneas]`.

```python
pkv(line)
```
Parsea una línea con formato `Clave="valor"` o `Clave=valor`.
Usa regex `(\w+)=("[^"]*"|[^\s]+)`. Coerciona números automáticamente.
Retorna dict de pares clave-valor.

#### Extracción de Geometría

```python
get_joints(T)
```
Extrae coordenadas de todos los nudos de la tabla `"JOINT COORDINATES"`.
Campos: `Joint`, `XorR`, `Y`, `Z`.
Retorna `dict[joint_id → {'x', 'y', 'z'}]`.

```python
get_frames(T)
```
Extrae conectividad de marcos (`"CONNECTIVITY - FRAME"`) y asignaciones de sección (`"FRAME SECTION ASSIGNMENTS"`).
Retorna: `connectivity[frame → {ji, jj}]`, `section_assignment[frame → section_name]`.

```python
get_section_dims(T)
```
Extrae dimensiones de secciones rectangulares de la tabla `"FRAME SECTION PROPERTIES"`.
Campos: `Section`, `t2` (dimensión menor), `t3` (dimensión mayor).
Retorna `dict[section → {'t2', 't3'}]`.

```python
section_to_global(t2, t3, angle_rad)
```
Proyecta dimensiones locales de sección a ejes globales X-Y dada la rotación del marco.
- **Retorna:** `(bx, by)` — dimensiones en dirección global.

```python
_member_dims_for_joint(jid, joints, fc, fs, sd, la, prefer="below")
```
Encuentra la sección del marco conectado al nudo `jid`. Prefiere el marco por debajo (columna hacia la cimentación).
Retorna `(frame_id, section_name, bx, by)`.

```python
_build_support_record(jid, joints, fc, fs, sd, la, source)
```
Construye el registro completo de soporte para un nudo:
- Busca marco hacia abajo → tipo `'pedestal'`.
- Fallback marco hacia arriba → tipo `'above_only'` con advertencia.
- Sin marco → tipo `'orphan'` con advertencia.
Retorna dict `{'type', 'section', 'bx', 'by', 'x', 'y', 'z', 'warning'}`.

#### Extracción de Cargas

```python
get_jloads(T)
```
Extrae cargas en nudos de la tabla `"JOINT LOADS"`.
Campos: `Joint`, `Pattern`, `F1` (cortante X), `F2` (cortante Y), `F3` (axial), `M1`, `M2`, `M3`.
Retorna `dict[joint → {pattern → {'F1','F2','F3','M1','M2','M3'}}]`.

```python
get_lpats(T)
```
Extrae patrones de carga de `"LOAD PATTERNS"`.
Retorna `dict[pattern_name → design_type]` (e.g., `"Dead"`, `"Live"`, `"Quake"`).

```python
get_joint_restraints(T)
```
Extrae restricciones de nudos de `"JOINT CONSTRAINTS"`.
Campos booleanos: `U1`, `U2`, `U3` (translaciones), `R1`, `R2`, `R3` (rotaciones).
Retorna `dict[joint → {'U1',...,'R3'}]`.

#### Diagnóstico de Fuente

```python
identify_supports_from_joint_loads(joints, jloads, fc, fs, sd, la)
```
Filtra nudos con carga vertical descendente (`F3 < 0`) y magnitud significativa.
Construye registro de soporte para cada candidato.
Retorna lista de entidades de diseño.

```python
identify_supports_from_restraints(joints, restraints, fc, fs, sd, la)
```
Filtra nudos con restricción vertical (`U3 = True`).
Construye registro de soporte para cada uno.
Retorna lista de entidades de diseño.

```python
inspect_foundation_sources(tables, joints, jloads, restraints)
```
Determina cuál fuente usar comparando candidatos de ambas opciones.
- Solo cargas → `"joint_loads"`.
- Solo restricciones → `"support_reactions"`.
- Ambas → `"ask_user"`.
- Ninguna → `"invalid_model"`.
Retorna `{'basis_mode', 'status_message', 'candidates_A', 'candidates_B'}`.

#### Clasificación y Combinaciones de Carga

```python
classify(load_patterns)
```
Mapea nombres de patrones a clases NSR-10 usando expresiones regulares:
- `D` → patrones con "dead", "muertas", "DL".
- `L` → "live", "vivas", "LL".
- `S` → "snow", "nieve".
- `W` → "wind", "viento".
- `E` → "quake", "seism", "sismo", "Ex", "Ey".
Retorna `dict[pattern → class_code]`.

```python
gen_combos(cl, R=7.0, ortho=False)
```
Genera todas las combinaciones de carga ADS y LRFD según NSR-10 Título B.

**Combinaciones ADS generadas:**

| Nombre | Fórmula | Grupo |
|--------|---------|-------|
| D+L | 1.0·D + 1.0·L | q1 |
| D+L+0.7Ex | 1.0·D + 1.0·L + 0.7/R·Ex | q3 |
| D+L+0.7Ey | 1.0·D + 1.0·L + 0.7/R·Ey | q3 |
| D+0.75L+0.525Ex | 1.0·D + 0.75·L + 0.525/R·Ex | q3 |
| ... | ... | ... |

**Combinaciones LRFD generadas:**

| Nombre | Fórmula |
|--------|---------|
| 1.4D | 1.4·D |
| 1.2D+1.6L | 1.2·D + 1.6·L |
| 1.2D+1.0L+1.0Ex | 1.2·D + 1.0·L + 1.0/R·Ex |
| 0.9D+1.0Ex | 0.9·D + 1.0/R·Ex |
| ... | ... |

- `ortho=True`: añade 0.3× de la dirección perpendicular en combos sísmicos.
- Retorna `{'ADS': [combo_dict,...], 'LRFD': [combo_dict,...]}`.

```python
compute_forces(jloads_data, combos)
```
Evalúa todas las combinaciones para todos los nudos multiplicando factores de combo por cargas de patrón y sumando linealmente.
Retorna `forces_by_combo[joint][combo_name] = {'P', 'Mx', 'My', 'Vx', 'Vy', 'group'}`.

---

### `isolated.py` — Diseño de Zapatas Aisladas

Núcleo del diseño estructural. Implementa los modelos de Bowles y ACI 318.

#### Inferencia de Posición de Columna

```python
infer_column_axis(x_z, y_z, B, L, bx, by, classification)
```
Determina las coordenadas reales de la columna en función del tipo de ubicación.
- **Concéntrica:** columna en el centro de la zapata.
- **Medianera:** columna desplazada al borde en la dirección indicada (`X+`, `X-`, `Y+`, `Y-`).
- **Esquinera:** columna en la esquina del lote (combinación de dos bordes).
Retorna `{'x_col', 'y_col', 'ex_geo', 'ey_geo', 'abs_ex', 'abs_ey'}`.

#### Modelo de Presiones del Suelo (Método de Bowles)

```python
soil_pressure(P, Mx, My, B, L)
```
Calcula las presiones de contacto en las cuatro esquinas de la zapata rectangular.

**Lógica:**
1. Calcula excentricidades: `ex = My/P`, `ey = Mx/P`.
2. Verifica si está dentro del núcleo central: `|ex| ≤ B/6` y `|ey| ≤ L/6`.
3. **Contacto total:** `qi = P/A ± My·c/Ix ± Mx·c/Iy` para las 4 esquinas.
4. **Contacto parcial:** calcula dimensiones efectivas `B_eff`, `L_eff` reducidas y presión máxima `qmax = 2P/(3·B_eff·L_eff)`.

Tolerancias: `TOL_Q = 0.5 kPa`, `TOL_KERN = 1e-4 m`.
Retorna `(q1, q2, q3, q4, B_eff, L_eff, contact_status)`.

#### Verificación de Cortante y Punzonamiento (ACI 318)

```python
factored_pressure_at_face(P, Mx, My, B, L, cbx, cby, d, direction)
```
Calcula el cortante y momento en la sección crítica a distancia `d` de la cara de la columna.
Integra la distribución de presión trapezoidal en el voladizo.
- `direction`: `'X'` o `'Y'`.
Retorna `{'Vu', 'Mu', 'q_near', 'q_far', 'arm'}`.

```python
punching_with_moment(Pu, Mux, Muy, B, L, cbx, cby, d, fc)
```
Verifica punzonamiento con transferencia de momento no balanceado (ACI 318 §22.6).

**Algoritmo:**
- Perímetro crítico: `bo = 2·(cbx + d + cby + d)`.
- Propiedades polares: `Jx`, `Jy` del perímetro crítico.
- Cortante directo: `vu_d = Vu / (bo·d)`.
- Transferencia de momento: `vu_mx = γv·Mux·c / Jx`, `vu_my = γv·Muy·c / Jy`.
- Cortante máximo: `vu_max = vu_d + |vu_mx| + |vu_my|`.
- Capacidad: `φvc = 0.75 × min(1/3·√fc, (1+2/βc)/6·√fc, (2+40d/bo)/12·√fc) × 1000`.
Retorna `{'vu_max', 'phi_vc', 'punch_ratio', 'bo', 'Jx', 'Jy'}`.

#### Verificación de Estabilidad

```python
overturning_check(P, Mx, My, B, L)
```
Calcula factores de seguridad al volcamiento:
- `FS_volc_x = P·B/2 / |My|`
- `FS_volc_y = P·L/2 / |Mx|`

Retorna `{'fs_volc_x', 'fs_volc_y', 'fs_volc_min'}`.

```python
sliding_check(P, Vx, Vy, mu)
```
Calcula factor de seguridad al deslizamiento:
- Fuerza resistente: `Fr = μ·P`
- Cortante resultante: `Vr = √(Vx² + Vy²)`
- `FS_desl = Fr / Vr`

Retorna `{'fs_desl', 'Vr', 'Fr'}`.

#### Diseño de Refuerzo

```python
propose_rebar(As_cm2, width_m, rec_cm)
```
Selecciona la varilla y espaciado óptimos para una banda de ancho dado.

**Base de datos de varillas:**

| Varilla | Diámetro (cm) | Área (cm²) |
|---------|--------------|-----------|
| #4 | 1.27 | 1.27 |
| #5 | 1.59 | 1.98 |
| #6 | 1.91 | 2.84 |

**Espaciados disponibles (cm):** 30, 25, 20, 17.5, 15, 12.5, 10.

**Algoritmo de selección:**
1. Para cada combinación varilla × espaciado: calcula `As_provisto = (n_barras × area_varilla)`.
2. Filtra combinaciones con `As_provisto ≥ As_requerido`.
3. Prefiere: maximizar espaciado → minimizar diámetro → mínimo sobreacero.
4. Si ninguna combinación sirve: devuelve indicación `"REQUIERE_DOBLE_CAPA"`.

Retorna `{'text': '5#5@15', 'As_prov': ..., 'status': 'ok'|'REQUIERE_DOBLE_CAPA'}`.

```python
calc_as(Mu, b, d, fc, fy, phi_f=0.9)
```
Calcula el área de acero requerida por flexión (diseño directo ACI 318).

**Fórmula:**
```
Rn = Mu / (φ · b · d²)
ρ  = 0.85·fc/fy · (1 - √(1 - 2·Rn / (0.85·fc)))
As = max(ρ, ρ_min) · b · d
```
- `ρ_min = max(0.0018, 1.4/fy)`.
Retorna `As` en cm².

#### Optimización de Dimensiones

```python
optimize_isolated(col, ads_f, lrfd_f, params, classification)
```
Búsqueda en grilla para encontrar las dimensiones mínimas que cumplen todos los criterios.

**Grilla de búsqueda:**
- `B` ∈ `[B_min, B_max]`, paso 0.05 m.
- `L` ∈ `[L_min, L_max]`, paso 0.05 m.
- `h` ∈ `[h_min, h_min + 0.60 m]`, paso 0.05 m.

**Criterios de aceptación:**
- ADS: `qmax ≤ qadm × 1.05` para todos los combos.
- LRFD: `punch_ratio ≤ 1.0` y `shear_ratio ≤ 1.0` para todos los combos.

**Criterio de ordenamiento para minimización:**
- Zapatas medianeras/esquineras: prioriza mínima excentricidad geométrica → luego mínimo volumen.
- Zapatas concéntricas: minimiza directamente el volumen (B×L×h).

Retorna `(B_opt, L_opt, h_opt, geo_eccentricity_data)`.

#### Diseño Completo con Auditoría

```python
full_structural_design(jid, x, y, B, L, h, col_bx, col_by, ads_f, lrfd_f, params, column_forces)
```
Ejecuta el diseño completo para dimensiones fijas. Genera las tablas de auditoría por combinación.

**Para cada combinación ADS:**
- Calcula cargas totales `P_total = P + W_zapata`.
- Llama a `soil_pressure()`.
- Llama a `overturning_check()` y `sliding_check()`.
- Registra ratio `qmax / qadm`, estado de contacto, presiones en esquinas.

**Para cada combinación LRFD:**
- Llama a `punching_with_moment()`.
- Llama a `factored_pressure_at_face()` en X y Y.
- Llama a `calc_as()` para Mx y My de diseño.
- Registra ratios de corte y punzonamiento.

**Asignación de estado:**
| Estado | Condición |
|--------|-----------|
| `PRELIMINAR_OK` | ADS ✓ + LRFD ✓ + contacto total |
| `REVISION_EXCENTRICIDAD` | ADS ✓ + LRFD ✓ + contacto parcial en algún combo |
| `REVISION_COMBINADA` | ADS ✓ + LRFD ✓ + requiere zapata combinada |
| `REVISAR_h` | ADS ✓ pero falla LRFD |
| `NO_CUMPLE` | Falla ADS |

Retorna diccionario completo de auditoría incluyendo `ads_audit[]`, `lrfd_audit[]`, `Asx`, `Asy`, `qmax`, `qmin`, `fs_volc_min`, `fs_desl_min`.

---

### `combined.py` — Diseño de Zapatas Combinadas

Se activa cuando dos o más zapatas aisladas se solapan geométricamente.

#### Detección de Solapamientos

```python
check_overlaps(footings, min_gap=0.10)
```
Detecta rectángulos solapados usando bounding boxes alineados con los ejes con tolerancia `min_gap = 0.10 m`.

**Algoritmo:**
1. Para cada par de zapatas: verifica si `|cx1 - cx2| < (B1+B2)/2 + gap` en X **y** en Y simultáneamente.
2. Construye grafo de solapamientos.
3. Aplica BFS para encontrar componentes conexas (grupos).

Retorna `(overlapping_pairs, groups_list)`.

#### Análisis Longitudinal (Modelo de Bowles)

```python
analyze_combined_longitudinal(x_left, x_right, B_trans, columns, P_cols, q_uniform, d)
```
Modela la zapata combinada como viga sobre reacción uniforme del suelo.

**Proceso:**
1. Genera estaciones a lo largo del eje longitudinal (cada 0.05–0.10 m, con puntos singulares en caras de columnas).
2. Calcula cortante `V(x)` y momento `M(x)` por integración:
   - Reacción del suelo: `w = q_uniform × B_trans` (carga distribuida ascendente).
   - Cargas de columnas: cargas puntuales descendentes en posiciones `x_i`.
3. Busca `Vmax`, `Mmax_pos`, `Mmax_neg`.

Retorna `{'stations', 'V', 'M', 'Vmax', 'Mmax_pos', 'Mmax_neg'}`.

```python
compute_steel_diagram(long_analysis, b_trans, d, fc, fy)
```
Calcula el acero requerido en cada estación del análisis longitudinal.
- Momentos positivos (tracción inferior) → `As_inf`.
- Momentos negativos (tracción superior) → `As_sup`.
Llama a `isolated.calc_as()` en cada punto.
Retorna `{'stations', 'As_inf', 'As_sup', 'As_max_inf', 'As_max_sup'}`.

#### Diseño de Zapata Combinada

```python
design_combined_footing(grp_indices, footings, group_cols, ads_all, lrfd_all, params, cidx)
```
Diseño iterativo completo de una zapata combinada.

**Paso 1 — Consolidación:**
- Une todas las columnas del grupo.
- Centra la zapata en el centroide ponderado por carga `D+L` de las columnas.

**Paso 2 — Dimensionamiento inicial:**
- `B_min`, `L_min` para contener los bordes de todas las columnas con margen `rec + d`.

**Paso 3 — Restricciones heredadas:**
- Si alguna columna del grupo era medianera/esquinera: se hereda la restricción geométrica.
- Ancla el borde de la zapata a la línea de propiedad correspondiente.
- Esquema asignado: `'combinada_restringida'`, `'combinada_esquinera'`, `'combinada_medianera'` o `'combinada'`.

**Paso 4 — Iteración (máx 20 ciclos):**
- Llama a `isolated.full_structural_design()` con las dimensiones actuales.
- Si `NO_CUMPLE`: escala `B` y `L` por 1.1×.
- Si `REVISAR_h`: incrementa `h` en 0.05 m.
- Si `PRELIMINAR_OK` o `REVISION_EXCENTRICIDAD`: acepta.

**Paso 5 — Transporte de fuerzas:**
- Para cada columna `i`: `M'x += Pi × Δyi`, `M'y += Pi × Δxi` (transporte al centroide).

**Paso 6 — Análisis longitudinal:**
- Determina eje dominante (mayor extensión de columnas).
- Resuelve V-M para el combo LRFD de mayor `Pu` total.
- Calcula distribución de acero con `compute_steel_diagram()`.

Retorna zapata combinada con todos los campos de auditoría.

---

### `tie_system.py` — Sistemas de Enlace

#### Deducción Automática de Vigas

```python
deduce_tie_beams(columns, classifications)
```
Recorre todas las columnas y determina qué vigas de enlace son necesarias según su clasificación:

| Clasificación | Acción |
|---------------|--------|
| `concentrica` | Sin viga requerida |
| `medianera X+` | Busca la columna más cercana en dirección −X |
| `medianera Y-` | Busca la columna más cercana en dirección +Y |
| `esquinera X+Y+` | Busca en −X **y** en −Y → dos vigas |

```python
_find_nearest(col, all_cols, direction, sign, tol_ortho=0.20)
```
Localiza la columna más cercana en una dirección dada.
- `tol_ortho = 0.20 m`: tolerancia de alineación ortogonal (desviación máxima en la dirección perpendicular).
- `min_distance = 0.3 m`: descarta columnas demasiado cercanas.
Retorna la columna destino y la distancia.

#### Construcción de Sistemas

```python
build_tie_systems(final_footings, ties)
```
Agrupa las vigas de enlace individuales en **sistemas** coherentes (conjuntos de zapatas conectadas en la misma dirección).

**Algoritmo:**
1. Mapea `joint_id → footing_id` (maneja zapatas combinadas: `"5+8"` → ambas zapatas).
2. Construye grafo de aristas (footing_A — footing_B por cada viga).
3. Aplica BFS por dirección (X y Y por separado).
4. Descarta sistemas con menos de 2 zapatas.

Retorna lista de sistemas `{'system_id', 'direction', 'footings', 'joints'}`.

#### Solvers de Viga

```python
beam_solve_simple_overhangs(supports, point_loads, point_moments)
```
Resuelve viga isostática con dos apoyos y posibles voladizos.

Equilibrio estático:
```
ΣM_A = 0  →  RB = (ΣPi·xi + ΣMi) / L
ΣF   = 0  →  RA = ΣP - RB
```
Genera diagrama V-M con estaciones cada 0.05–0.10 m.
Retorna `{'RA', 'RB', 'stations', 'V', 'M', 'balance_ok'}`.

```python
beam_solve_multi_support(support_xs, point_loads, point_moments, EI=1e8)
```
Resuelve viga continua con N apoyos por el **método de rigidez** (Euler-Bernoulli).

**Algoritmo:**
1. **Coalescencia de nodos** (`NODE_COALESCE_TOL = 0.05 m`): si una carga cae sobre un apoyo, convierte la carga puntual en momento equivalente `M = P·e`.
2. Construye la **matriz de rigidez global** `K` (2 DOF por nodo: deflexión + rotación).
3. Particiona en DOFs libres y restringidos (apoyos = deflexión nula).
4. Resuelve: `Kff · df = Ff` con `numpy.linalg.solve()`.
5. Calcula reacciones: `R = K · d - F`.
6. Genera diagrama V-M por integración.

Retorna `{'reactions', 'stations', 'V', 'M', 'converged', 'balance_error'}`.

#### Análisis Completo del Sistema

```python
analyze_tie_system(system, final_footings, jloads, combos, params)
```
Función principal que diseña completamente un sistema de enlace.

**Fase 1 — Geometría:**
- Extrae posiciones de apoyos (centros de zapatas).
- Extrae posiciones de cargas (centros de columnas).
- Calcula excentricidades `e = x_columna − x_zapata`.
- Detecta coalescencias (`|e| < 0.05 m`).

**Fase 2 — Resolución por patrón:**
Para cada patrón de carga (D, L, Ex, Ey, ...):
- Resuelve la viga con las cargas de ese patrón únicamente.
- Almacena reacciones: `R_pat[pattern][nudo]`.

**Fase 3 — Peso propio:**
- Estima `h_v` inicial desde el momento mayor LRFD.
- Calcula `q_pp = b_v · h_v · γ_c` (kN/m).
- Distribuye peso propio como cargas puntuales en tributación de cada nodo.
- Agrega patrón `'PP_VIGA'`.

**Fase 4 — Combinación lineal:**
Para cada combinación ADS y LRFD:
```
R_combo[nudo] = Σ(factor_i × R_pat[patrón_i])
```
Genera deltas de presión `dP[nudo] = R_combo / A_zapata`.

**Fase 5 — Diagrama V-M del combo de control:**
- Identifica el combo LRFD de mayor demanda total.
- Resuelve la viga completa con ese combo.
- Genera los diagramas `V(x)` y `M(x)`.

**Fase 6 — Dimensionamiento final (iterativo):**
Itera hasta convergencia (máx 15 ciclos):
- Verifica flexión: `As ≤ capacidad de una capa`.
- Verifica cortante: `Vu ≤ φVc` con `φ = 0.75`.
- Si falla: incrementa `h_v` en 0.05 m; si `b_v` llega al límite, incrementa también `b_v`.

**Restricción de ancho:**
- Dirección X: `b_v ≤ min(by de todas las columnas del sistema)`.
- Dirección Y: `b_v ≤ min(bx de todas las columnas del sistema)`.

Retorna sistema completo con diagramas, refuerzo y estado `'ok'` o `'REVISAR_SECCION'`.

---

### `export_s2k.py` — Exportación a SAP2000

Genera un archivo `.s2k` con el modelo completo de cimentación listo para importar en SAP2000.

#### Utilidades de Formato

```python
_guid()        # Genera UUID único para compatibilidad con SAP2000
_fmt(v)        # Formatea valor: bool → "Yes"/"No", float → string limpio
_line(**kwargs) # Formatea línea SAP: "Clave1=val1   Clave2=val2"
```

#### Generación de Secciones

```python
_rect_section_general(section_name, material, t3, t2, color)
```
Genera propiedades de sección rectangular de marco (pedestales, vigas de enlace).
Calcula: `Area = t2·t3`, `I33 = t2·t3³/12`, `I22 = t3·t2³/12`, `J ≈ torsión St. Venant`.

```python
generate_compatible_mesh(footing, pedestal_rects, min_lines)
```
Genera una malla compatible (shell + marco) para la zapata.
- Extrae líneas de característica desde bordes de zapata y posiciones de columnas.
- Subdivide para garantizar mínimo de elementos.
Retorna `(xs, ys)` — coordenadas de la grilla.

#### Función Principal de Exportación

```python
export_foundation_s2k(model_data, results, params, export_cfg=None)
```
Genera el texto completo del archivo `.s2k`.

**Tablas generadas:**

| Tabla SAP2000 | Contenido |
|---------------|-----------|
| `PROGRAM CONTROL` | Unidades (kN, m), versión |
| `PROJECT INFORMATION` | Empresa, título del proyecto |
| `MATERIAL PROPERTIES` | Un material de concreto con `f'c` del proyecto |
| `LOAD PATTERNS` | Patrones heredados del modelo original |
| `LOAD CASES` | Casos de análisis estáticos |
| `LOAD COMBINATIONS` | Combos ADS y LRFD completos |
| `JOINT COORDINATES` | Nudos de zapatas, pedestales, malla shell |
| `CONNECTIVITY - FRAME` | Pedestales (marcos) + vigas de enlace |
| `CONNECTIVITY - AREA` | Losa de zapata (elementos shell) |
| `FRAME SECTION ASSIGNMENTS` | Asignación de secciones a marcos |
| `AREA SECTION ASSIGNMENTS` | Asignación de sección shell a áreas |
| `AREA SPRING ASSIGNMENTS` | Resortes de subrasante en zapatas (Kx, Ky, Kz) |
| `CONSTRAINT DEFINITIONS - BODY` | Rigidez de pedestal (BODY constraint) |
| `JOINT LOADS - FORCE` | Cargas de columnas sobre pedestales |

**Modelo de resortes de subrasante:**
```
Kz = k_subrasante [kN/m³] × Área_elemento [m²]
Kx = Ky = α × Kz    (tipicamente α = 0.35)
```

---

## Parámetros de Diseño (Sidebar)

| Parámetro | Símbolo | Descripción |
|-----------|---------|-------------|
| Factor de reducción sísmica | R | Divisor para fuerzas sísmicas (NSR-10). Típico: 2.5–7.0 |
| Resistencia del concreto | f'c | MPa. Típico: 21, 24, 28 MPa |
| Resistencia del acero | fy | MPa. Estándar: 420 MPa |
| Recubrimiento | rec | cm. Mínimo: 7.5 cm (expuesto al suelo) |
| Profundidad de desplante | Df | m. Distancia de la superficie al fondo de la zapata |
| Peso unitario suelo | γ_suelo | kN/m³. Típico: 18 kN/m³ |
| Peso unitario concreto | γ_c | kN/m³. Típico: 24 kN/m³ |
| Presión admisible D+L | qadm_1 | kPa. Grupo q1 |
| Presión admisible max | qadm_2 | kPa. Combos temporales (grupo q2) |
| Presión admisible sísmica | qadm_3 | kPa. Combos sísmicos (grupo q3) |
| Dimensión mínima zapata | B_min | m. Por defecto: 0.60 m |
| FS volcamiento mínimo | FSv_min | Mínimo aceptable: típico 1.5 |
| FS deslizamiento mínimo | FSd_min | Mínimo aceptable: típico 1.5 |

---

## Estructuras de Datos Clave

### Entidad de Diseño

```python
{
    'id': 'J5',
    'joint': '5',
    'entity_type': 'point',        # 'point' | 'wall'
    'x': 3.0, 'y': 6.0, 'z': 0.0,
    'bx': 0.30, 'by': 0.30,        # Dimensiones de la sección
    'section': 'C30x30',
    'source': 'joint_loads',        # 'joint_loads' | 'restraints'
    'design_family': 'column'       # 'column' | 'wall'
}
```

### Clasificación de Columna

```python
{
    'joint_id': {
        'location': 'medianera',    # 'concentrica' | 'medianera' | 'esquinera'
        'side': 'X+',               # Para medianera: 'X+' | 'X-' | 'Y+' | 'Y-'
        'corner': '',               # Para esquinera: 'X+Y+' | 'X+Y-' | 'X-Y+' | 'X-Y-'
        'mpx': 0.0,                 # Momento adicional manual X
        'mpy': 0.0,                 # Momento adicional manual Y
        'vux': 0.0,                 # Cortante adicional manual X
        'vuy': 0.0                  # Cortante adicional manual Y
    }
}
```

### Zapata Diseñada (final_footings)

```python
{
    'id': 'Z-01',
    'type': 'isolated',            # 'isolated' | 'combined'
    'joint': '5',                   # ID de nudo(s); 'j1+j2' para combinadas
    'x': 3.0, 'y': 6.0,            # Centro de la zapata
    'B': 1.80, 'L': 1.80, 'h': 0.50,
    'A': 3.24,                      # Área (m²)
    'd': 0.385,                     # Peralte efectivo (m)
    'classification': {'location': 'medianera', 'side': 'X+'},
    'scheme': 'medianera_X+',
    'st': 'PRELIMINAR_OK',
    'qmax': 185.3, 'qmin': 42.1,
    'Asx': 8.5, 'Asy': 8.5,         # cm²
    'Pu': 420.0,
    'pr': 0.62,                      # Ratio punzonamiento
    'sr': 0.71,                      # Ratio cortante
    'fs_volc_min': 3.2,
    'fs_desl_min': 2.1,
    'ads_audit': [ ... ],            # Lista de resultados ADS por combo
    'lrfd_audit': [ ... ]            # Lista de resultados LRFD por combo
}
```

### Sistema de Enlace (tie_systems)

```python
{
    'system_id': 'SYS_Z1_Z2_X',
    'direction': 'X',
    'footings': ['Z-01', 'Z-03'],
    'num_nodes': 2,
    'total_length': 5.40,
    'b_viga': 0.25, 'h_viga': 0.50,
    'd_viga': 0.415,
    'As_inf': 3.96, 'As_inf_text': '2#5@—',
    'As_sup': 1.98, 'As_sup_text': '1#5@—',
    'Mu_max_pos': 48.2,
    'Mu_max_neg': -31.7,
    'Vu_max': 62.5,
    'sr_viga': 0.83,
    's_estribo': 0.15,
    'phi_Vc': 75.3,
    'status': 'ok',                # 'ok' | 'REVISAR_SECCION' | 'insuficiente'
    'vm_diagram': { ... },
    'steel_diagram': { ... }
}
```

---

## Normas y Códigos Aplicados

| Código | Aplicación |
|--------|-----------|
| **NSR-10 Título B** | Combinaciones de carga ADS y LRFD para Colombia |
| **ACI 318-19** | Diseño por resistencia: punzonamiento, cortante, flexión |
| **Bowles, 5ª Ed.** | Modelo de presiones de suelo, contacto parcial, estabilidad |
| **McCormac, 8ª Ed.** | Procedimientos de diseño de concreto reforzado |

---

## Constantes y Tolerancias

| Constante | Valor | Descripción |
|-----------|-------|-------------|
| `TOL_Q` | 0.5 kPa | Tolerancia en presión de suelo (permite pequeñas tensiones) |
| `TOL_KERN` | 1×10⁻⁴ m | Tolerancia para verificación del núcleo central |
| `NODE_COALESCE_TOL` | 0.05 m | Distancia mínima carga–apoyo antes de coalescencia en vigas |
| `φ_flexión` | 0.90 | Factor de reducción de resistencia a flexión (ACI 318) |
| `φ_cortante` | 0.75 | Factor de reducción de resistencia a cortante (ACI 318) |
| `μ` (fricción) | 0.40 | Coeficiente de fricción suelo–concreto (verificación deslizamiento) |
| `γ_c` default | 24 kN/m³ | Peso unitario del concreto |
| `γ_s` default | 18 kN/m³ | Peso unitario del suelo de relleno |
| `B_min` default | 0.60 m | Dimensión mínima de zapata |
| `h_min` default | 0.30 m | Peralte mínimo de zapata |
| `rec` default | 7.5 cm | Recubrimiento libre (suelo) |
| Paso grilla `B`, `L` | 0.05 m | Resolución de la búsqueda en optimización |
| Paso grilla `h` | 0.05 m | Resolución de la búsqueda en altura |
| Iter. máx. combinada | 20 | Iteraciones máximas en diseño de zapata combinada |
| Iter. máx. viga enlace | 15 | Iteraciones máximas en dimensionamiento de viga |

---

*Desarrollado por Smart Couplers MG — www.scmgsas.com — +57 323 2849503 — gerencia@scmgsas.com*
