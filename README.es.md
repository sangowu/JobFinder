# JobRadar

> [中文](README.zh.md) · [English](README.md) · **Español**

Busca automáticamente ofertas de trabajo en todo el mundo basándose en tu CV, puntúa coincidencias con LLM y agrega resultados de múltiples fuentes.

## Inicio Rápido

```bash
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
  ▼ ② Descubrimiento de títulos (Adzuna API + LLM)  ← caché 7 días
  ▼    El usuario revisa y confirma la lista de títulos
  ▼ ③ Extracción (Indeed + LinkedIn, JobSpy, sin navegador)
         Pre-filtro LLM de títulos → serie limitada (Indeed 2s / LinkedIn 3s) → dedup
  ▼ ④ Embudo de filtros: antigüedad → relevancia → caché URL → cerrada → exp → habilidades
  ▼ ⑤ Evaluación LLM por lotes (score / strengths / weaknesses / matched_keywords)
  ▼ ⑥ Estadísticas escritas en reports/pipeline_stats.jsonl
  ▼    Web UI / terminal
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

# Adzuna (descubrimiento de títulos, registro gratuito: developer.adzuna.com)
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# Modelo predeterminado (escrito automáticamente por `jobradar model`)
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

## Funciones de la Web UI

- **Progreso en tiempo real**: ofertas enviadas carta a carta vía SSE durante la búsqueda
- **Estadísticas del embudo**: desglose por etapa tras cada búsqueda (extraídos → filtro título LLM → pre-filtro → evaluación LLM → guardados / tasa de filtrado)
- **Diseño de tres columnas**: lista de trabajos + detalle + panel de subida de CV/búsqueda
- **Agregación multi-fuente**: las ofertas que aparecen en Indeed y LinkedIn se fusionan automáticamente; las insignias de fuente son enlaces clicables; el botón Apply se convierte en menú desplegable cuando hay varias URLs
- **Historial de búsquedas**: cada registro tiene un botón 📊 para expandir el embudo completo, con desglose por fuente (Indeed / LinkedIn)
- **Panel de logs**: filtrado por nivel, resaltado de palabras clave, actualización automática
- **Página de configuración**: gestiona API Keys de LLM y API de búsqueda Adzuna, selecciona modelo por defecto, limpia caché — los nuevos usuarios pueden completar toda la configuración sin editar `.env`
- **Multilingüe**: la interfaz soporta 中文 / English / Español

## Informes de Estadísticas

Tras cada búsqueda se escriben automáticamente en el directorio `reports/`:

| Archivo | Descripción |
|---|---|
| `pipeline_stats.jsonl` | Log de solo añadir — una línea JSON por búsqueda, historial completo |
| `pipeline_stats_latest.json` | Siempre sobreescrito con el informe de la búsqueda más reciente |

## Privacidad

- **El contenido del CV** se envía a la API LLM que hayas configurado (Anthropic / Google / OpenAI, etc.) para su análisis y evaluación. Asegúrate de confiar en la política de datos de tu proveedor elegido.
- **Todos los datos se almacenan localmente**: los perfiles de CV analizados y las ofertas de trabajo se guardan en una base de datos SQLite local (`jobradar_cache.db`) y nunca se suben a ningún servidor externo.
- **El archivo de log** (`jobradar.log`) solo registra términos de búsqueda y marcas de tiempo — no contiene datos personales del CV ni API Keys, y está excluido de git mediante `.gitignore`.

## Limitaciones Conocidas

Este es un proyecto personal mantenido en tiempo libre. Algunas funciones — en particular el **filtrado por ubicación** — pueden producir resultados inconsistentes según la fuente de empleo.

**Soporte de proveedores LLM**: se han integrado 17 proveedores, pero no todos han sido probados de forma completa. Si encuentras un error con algún proveedor o modelo, por favor [abre un issue](https://github.com/sangowu/JobRadar/issues) indicando el nombre del proveedor, el modelo y el mensaje de error.

## Aviso Legal

Esta herramienta extrae datos públicos de empleo de Indeed y otras plataformas a través de [python-jobspy](https://github.com/cullenwatson/JobSpy).

> **Aviso importante:** El web scraping puede vulnerar los Términos de Servicio (ToS) de los sitios web afectados. Esta herramienta está destinada **únicamente para búsqueda de empleo personal, aprendizaje e investigación**. Los usuarios son los únicos responsables de garantizar el cumplimiento de los términos aplicables. El autor no acepta ninguna responsabilidad por un uso indebido. Por favor, raspa de forma responsable y evita un uso de alta frecuencia o comercial.
