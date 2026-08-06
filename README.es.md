# JobRadar

> [中文](README.zh.md) · [English](README.md) · **Español**

Busca automáticamente ofertas de trabajo en todo el mundo basándose en tu CV, puntúa coincidencias con LLM y agrega resultados de múltiples fuentes.

## Inicio Rápido

```bash
# Instala uv (omite si ya está instalado)
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Windows (PowerShell): irm https://astral.sh/uv/install.ps1 | iex

git clone https://github.com/sangowu/JobRadar.git
cd JobRadar
uv sync
uv run jobradar serve       # Lanza la Web UI (http://127.0.0.1:8765)
# Abre el navegador y configura las API Keys en la página "Config. API"
# O configura manualmente via .env:
cp .env.example .env         # Rellena tus API Keys
uv run jobradar find cv.docx  # Modo CLI
```

## Comandos

| Comando | Descripción |
|---|---|
| `uv run jobradar serve` | Lanza la Web UI |
| `uv run jobradar serve --mock` | Modo test (BD aislada, no afecta la caché real) |
| `uv run jobradar find cv.docx` | CLI: analiza CV → descubre títulos → extrae → evalúa |
| `uv run jobradar find cv.docx --refresh` | Fuerza nueva búsqueda ignorando la caché |
| `uv run jobradar results` | Muestra los resultados en caché de la última búsqueda |
| `uv run jobradar assess` | Reejecuta la evaluación LLM sobre JDs en caché |
| `uv run jobradar model` | Selecciona interactivamente el proveedor y modelo LLM |
| `uv run jobradar cache clear` | Limpia toda la caché |
| `uv run jobradar --version` | Muestra la versión actual |

## Visión General del Pipeline

```
Archivo CV
  │
  ▼ ① Análisis de CV (LLM → CVProfile)  ← caché permanente SHA-256
         bandas estructuradas de seniority + extracción explícita de idiomas
  ▼ ② El usuario revisa y confirma la lista de títulos
  ▼ ③ Extracción (Indeed + LinkedIn, JobSpy, sin navegador)
         fuentes concurrentes; cada lote de rol completado se deduplica inmediatamente
  ▼    prefiltro Python determinista + checkpoint persistido
         gates de seniority / cierre / diferencia de experiencia → filtered list → search_candidates (SQLite)
         los mismos objetos entran después en una cola en memoria
  ▼ ④ Coordinador de evaluación por lotes (solapado con el scraping posterior)
         title relevance gate LLM previo al JD
         filtro semántico conservador solo con el título; keep=true por defecto y rechazo solo si la ruta profesional es claramente distinta
  ▼    coarse filter LLM por lotes
         keep/reject a nivel de tarjeta usando title + location + snippet
  ▼ ⑤ Pool acotado de evaluación (5 workers en cloud; 1 para modelos locales)
         distintos puestos se procesan en paralelo; cada puesto mantiene JD Profile → CV Match
         el coordinador confirma SQLite en serie y emite SSE solo después del commit
  ▼ ⑥ Extracción de JD Profile
         required/preferred skills estructurados, must-haves, años, conflicto de seniority, work mode y requisitos de idioma
  ▼ ⑦ Matching explicable CV↔JD
         puntuación por rúbrica → score ponderado programático → recommendation
         reubicación entre ciudades / asistencia a oficina cuentan como riesgo, no como penalización de location_score
  ▼ ⑧ Generación de artifacts
          interview prep / cover letter / CV optimization
  ▼ ⑨ Estadísticas de búsqueda y caché
          métricas históricas, informes, filter events, Web UI / terminal
```

Embudo real (datos reales):
```
Indeed 741 + LinkedIn 255 = 996 extraídos
  → Filtro título LLM  996 → 689  (30.8% eliminados)
  → Embudo pre-filtro  689 → 76   (antigüedad / dedup / habilidades, etc.)
  → Evaluación LLM      76 → 54 guardados  (tasa aprobación 71.1%)
  → Tasa de filtrado total: 94.6%  (solo 54 de 996 requieren revisión humana)
```

## Variables de Entorno

```env
# Proveedor LLM (configura al menos uno)
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=

# Modelos locales
LLAMACPP_BASE_URL=http://localhost:8080/v1
LOCAL_LLM_BASE_URL=http://localhost:1234/v1

# Modelo predeterminado (escrito automáticamente por `jobradar model`)
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-3.5-flash-lite
```

## Funciones de la Web UI

- **Progreso en tiempo real**: ofertas enviadas carta a carta vía SSE durante la búsqueda
- **Estadísticas del embudo**: desglose por etapa tras cada búsqueda (extraídos → filtro título LLM → pre-filtro → evaluación LLM → guardados / tasa de filtrado)
- **Diseño de tres columnas**: lista de trabajos + detalle + panel de subida de CV/búsqueda
- **Agregación multi-fuente**: las ofertas que aparecen en Indeed y LinkedIn se fusionan automáticamente; las insignias de fuente son enlaces clicables; el botón Apply se convierte en menú desplegable cuando hay varias URLs
- **Historial de búsquedas**: cada registro tiene un botón 📊 para expandir el embudo completo, con desglose por fuente (Indeed / LinkedIn)
- **Métricas normalizadas del historial**: cada búsqueda guarda total extraído, total tras deduplicación, total filtrado, nuevos puestos guardados y consumo de tokens
- **Resumen benchmark del embudo**: el historial guarda versiones del pipeline/prompt y muestra métricas derivadas como tasa post-filtro, rendimiento de nuevos puestos y tokens por puesto nuevo
- **Benchmark reproducible de planificación**: [`docs/pipeline-benchmark.md`](docs/pipeline-benchmark.md) explica la captura de lotes congelados, la reproducción con SQLite aislado y la comparación emparejada serial/streaming sin escribir en la caché de producción ni en SSE
- **Filtro semántico previo por título**: antes del JD assessment, los títulos pasan por un gate LLM conservador; `skip_irrelevant` aparece en el embudo
- **Persistencia de eventos de filtrado**: cada búsqueda guarda `run_id / stage / title / reason / details` en `filter_events`
- **Filtro dinámico de seniority en el título**: usa los niveles eligible/stretch/blocked del CV para quitar desajustes claros antes del JD matching
- **Corte duro por diferencia de experiencia**: el pipeline registra `skip_exp`, y `jd_assessment` marca directamente `relevant=false` cuando la diferencia explícita de años es mayor a 3
- **Matching explicable**: el detalle del JD muestra desglose de puntuación, riesgos, coincidencias de habilidades y recommendation
- **Reubicación / asistencia a oficina solo como riesgo**: la reubicación entre ciudades dentro del mismo país objetivo y requisitos como `hybrid` / `onsite` / días presenciales semanales entran en `risks / risk_penalty`, no en `location_score`
- **Artifact hub**: genera y reutiliza Interview Prep, Cover Letter y CV Optimization desde el panel de detalle
- **Panel de logs**: filtrado por nivel, resaltado de palabras clave, actualización automática
- **Página de configuración**: gestiona API Keys de LLM, selecciona modelo por defecto, limpia caché — los nuevos usuarios pueden completar toda la configuración sin editar `.env`
- **Multilingüe**: la interfaz soporta 中文 / English / Español

## Informes de Estadísticas

Tras cada búsqueda se escriben automáticamente en el directorio `reports/`:

| Archivo | Descripción |
|---|---|
| `pipeline_stats.jsonl` | Log de solo añadir — una línea JSON por búsqueda, historial completo |
| `pipeline_stats_latest.json` | Siempre sobreescrito con el informe de la búsqueda más reciente |

## Benchmark, run_id y eventos de filtrado

- Cada fila del historial guarda metadatos de versión y también `run_id`.
- `GET /api/stats` devuelve el historial con ese `run_id`.
- `GET /api/filter-events?run_id=<run_id>` devuelve los eventos persistidos de filtrado por título para esa búsqueda.
- En modo mock, al limpiar la caché también se eliminan `search_stats` y `filter_events` de la BD mock.

## Script de inspección

Usa `scripts/show_filter_events.py` para leer `data/jobradar_test_cache.db` sin rerun de la búsqueda.

```bash
python scripts/show_filter_events.py
python scripts/show_filter_events.py --stage jd_assessment --out reports/filter_report.md
python scripts/show_filter_events.py --run-id <run_id> --json --out reports/filter_report.json
```

Opciones útiles:

- `--run-id`: inspeccionar una búsqueda concreta
- `--stage`: filtrar por `title_relevance`, `coarse_filter`, `experience_gap`, `jd_assessment` o `final_match`
- `--md`: imprimir Markdown en terminal
- `--out`: guardar `.md` o `.json`

## Script de comparación

`scripts/compare_title_gate.py` ejecuta un A/B controlado entre `baseline_gate_off` y `title_gate_on`.

- Títulos fijos: `AI Engineer`, `Machine Learning Engineer`, `LLM Engineer`, `Software Engineer`, `Backend Engineer`
- `30` ofertas por título
- `168` horas (`7` días)
- Solo `Indeed`
- Ubicación fija: `Ireland`

## Seguimiento de Solicitudes por Correo

La página **Seguimiento de solicitudes** se conecta a Gmail mediante Google OAuth y clasifica los correos de contratación como solicitud enviada, evaluación, entrevista, oferta, rechazo, retirada o pendiente de revisión. La cronología resultante se guarda en la misma base de datos SQLite local.

1. Crea un cliente OAuth 2.0 de tipo aplicación web en Google Cloud Console.
2. Habilita la API de Gmail.
3. Añade `http://127.0.0.1:8765/api/email/google/callback` como URI de redirección autorizada.
4. Configura `.env`:

```env
GOOGLE_OAUTH_CLIENT_ID=your_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret
EMAIL_SYNC_INTERVAL_SECONDS=900
EMAIL_SYNC_MAX_MESSAGES=5000
EMAIL_SYNC_FETCH_WORKERS=8
EMAIL_SYNC_ANALYSIS_WORKERS=3
EMAIL_LLM_CLASSIFICATION_ENABLED=1
```

Inicia `jobradar serve`, abre **Seguimiento de solicitudes** y selecciona **Iniciar sesión con Google**. JobRadar solicita acceso de solo lectura a Gmail. El token OAuth se guarda localmente en `data/google_gmail_token.json` y está excluido de Git.

La primera sincronización lee los últimos 30 días y guarda un cursor de Gmail History; las siguientes procesan solo mensajes nuevos. Los cuerpos se descargan con 8 workers por defecto (`EMAIL_SYNC_FETCH_WORKERS`, rango 1-16). Las reglas y la clasificación selectiva con LLM usan 3 workers (`EMAIL_SYNC_ANALYSIS_WORKERS`, rango 1-8). La fusión y escritura en SQLite siguen siendo cronológicas y de un solo hilo.

Las reglas locales procesan los rechazos claros, alertas, recomendaciones y suscripciones ATS. Solo los estados ambiguos, identidades incompletas o conflictos con cabeceras de envío masivo se envían al LLM configurado. Valores como `unknown`, `N/A` y `not specified` se normalizan como ausentes para que un correo posterior pueda completar la empresa o el puesto. Los cuerpos de los correos no se guardan.

La UI permite sincronización manual, pausa de la sincronización programada, borrado de datos, nuevo análisis en segundo plano con progreso, historial expandible, enlaces directos a Gmail y acciones de confirmar/descartar.

## Privacidad

- **El contenido del CV** se envía a la API LLM que hayas configurado (Anthropic / Google / OpenAI, etc.) para su análisis y evaluación. Asegúrate de confiar en la política de datos de tu proveedor elegido.
- **Persistencia local**: los perfiles de CV, ofertas de trabajo y observaciones de clasificación de correo se guardan en una base de datos SQLite local (`data/jobradar_cache.db`). Los correos ambiguos pueden enviarse al proveedor LLM configurado para su clasificación, pero sus cuerpos no se guardan en la tabla de observaciones.
- **El archivo de log** (`logs/jobradar.log`) solo registra términos de búsqueda y marcas de tiempo — no contiene datos personales del CV ni API Keys, y está excluido de git mediante `.gitignore`.

## Limitaciones Conocidas

Este es un proyecto personal mantenido en tiempo libre. Algunas funciones — en particular el **filtrado por ubicación** — pueden producir resultados inconsistentes según la fuente de empleo.

**Soporte de proveedores LLM**: se han integrado 17 proveedores, pero no todos han sido probados de forma completa. Si encuentras un error con algún proveedor o modelo, por favor [abre un issue](https://github.com/sangowu/JobRadar/issues) indicando el nombre del proveedor, el modelo y el mensaje de error.

## Aviso Legal

Esta herramienta extrae datos públicos de empleo de Indeed y otras plataformas a través de [python-jobspy](https://github.com/cullenwatson/JobSpy).

> **Aviso importante:** El web scraping puede vulnerar los Términos de Servicio (ToS) de los sitios web afectados. Esta herramienta está destinada **únicamente para búsqueda de empleo personal, aprendizaje e investigación**. Los usuarios son los únicos responsables de garantizar el cumplimiento de los términos aplicables. El autor no acepta ninguna responsabilidad por un uso indebido. Por favor, raspa de forma responsable y evita un uso de alta frecuencia o comercial.
