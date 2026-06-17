# Módulo "Reservados" — Spec de Diseño

**Fecha:** 2026-06-17
**Estado:** Aprobado por usuario, pendiente de implementación

---

## Resumen

Agregar a SIGEMEP_APP_DEV una nueva solapa **"Reservados"** que permite buscar y visualizar (sin descargar) PDFs de la carpeta `C:\Users\SIGEMEP\Desktop\INFORMES RV`. El acceso no depende del rol (ADMIN/JEFE/BRIGADA): es un permiso individual que el ADMIN otorga o revoca por usuario. Los archivos reservados no son visibles desde la búsqueda de memorandos ni desde ningún otro lugar del sistema.

---

## Contexto

El sistema ya indexa y permite buscar memorandos en `C:\Users\SIGEMEP\Desktop\REDCOMPARTIDA` (tabla `memorandos`, FTS5, rutas `/buscar`). Existe además un patrón de visualización sin descarga (`/ver_primera_hoja`, que renderiza una página como PNG con marca de agua) y uno de descarga controlada por rol (`/descargar`, `/ver_pdf_completo`, gateado por `ROLES_DESCARGA_PDF = {ADMIN, JEFE}`).

Reservados reutiliza estos patrones pero con dos diferencias clave:
1. El control de acceso es por usuario individual, no por rol.
2. La visualización nunca entrega el PDF real al navegador — todas las páginas se sirven como imágenes con marca de agua.

---

## Alcance

### Incluido
- Tabla y FTS5 propios (`reservados`, `reservados_fts`), separados de `memorandos`
- Indexación de `INFORMES RV` con botón propio en el panel de ADMIN (rápida/completa, igual patrón que la indexación actual)
- Búsqueda con los mismos filtros que `/buscar` (texto, campo nombre/texto/todo, fecha, páginas), pero acotada a `reservados`
- Visor paginado que renderiza cada página como PNG con marca de agua (usuario, rol, fecha, hora, IP), sin endpoint que entregue el PDF original
- Permiso individual `permiso_reservados` en `usuarios`, otorgable a cualquier rol desde `/admin/usuarios`
- ADMIN tiene acceso automático a Reservados (búsqueda y visor) sin necesitar el permiso explícito
- Ítem de navegación "Reservados" visible solo para quienes tienen acceso
- Auditoría de búsquedas, apertura del visor, cambios de permiso e indexación

### Excluido
- Descarga del PDF original en cualquier forma (ni botón, ni endpoint inline)
- Formatos distintos de PDF
- Cache en disco de páginas distintas a la primera (se generan al vuelo)
- Edición o carga de archivos reservados desde la web (la carpeta se gestiona fuera del sistema, igual que memorandos)
- Botón único combinado de reindexación (memorandos y reservados se reindexan por separado)

---

## Modelo de datos

### Tabla `reservados` (espejo de `memorandos`)

```sql
CREATE TABLE IF NOT EXISTS reservados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre_archivo TEXT NOT NULL,
    ruta_archivo TEXT NOT NULL UNIQUE,
    texto_extraido TEXT,
    cantidad_paginas INTEGER,
    primera_hoja_img TEXT,
    fecha_indexado DATETIME DEFAULT CURRENT_TIMESTAMP,
    activo INTEGER DEFAULT 1,
    tamanio_bytes INTEGER,
    mtime INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS reservados_fts USING fts5(
    nombre_archivo,
    texto_extraido,
    content='reservados',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
```

Migración en `services/db.py`, junto a la creación de `memorandos`/`memorandos_fts`.

### Columna de permiso en `usuarios`

```sql
ALTER TABLE usuarios ADD COLUMN permiso_reservados INTEGER NOT NULL DEFAULT 0;
```

Migración vía `_add_column_if_missing` (igual patrón que las columnas incrementales de `memorandos`).

### Configuración de carpeta

En `config.py`:
```python
DEFAULT_RESERVADOS_DIR = r"C:\Users\SIGEMEP\Desktop\INFORMES RV"
RESERVADOS_BASE_DIR = Path(os.environ.get("SIGEMEP_RESERVADOS_DIR", DEFAULT_RESERVADOS_DIR))
```

En `services/pdf_service.py`, helper análogo a `carpeta_pdf_actual`:
```python
def carpeta_reservados_actual(conn=None) -> Path:
    """Devuelve la carpeta configurada de Reservados (clave 'reservados_dir' en configuracion, o RESERVADOS_BASE_DIR)."""
```
Se inicializa en `init_db()` igual que `pdf_dir`: si no existe la clave `reservados_dir` en `configuracion`, se setea a `DEFAULT_RESERVADOS_DIR`.

---

## Arquitectura del motor (generalización, no duplicación)

Las funciones de `services/pdf_service.py` y `services/search_service.py` que hoy asumen "memorandos" se generalizan para aceptar el nombre de tabla/FTS y la carpeta base como parámetros, con **valores por defecto idénticos al comportamiento actual** (para no romper memorandos):

- `indexar_memorandos(conn, ..., tabla="memorandos", tabla_fts="memorandos_fts", carpeta=None)` — si `carpeta` es `None`, usa `carpeta_pdf_actual(conn)` como hoy.
- `buscar_memorandos(query, ..., tabla="memorandos")` — las consultas FTS5 y SQL usan `tabla`/`tabla_fts` interpolados de una lista cerrada de valores válidos (`{"memorandos", "reservados"}`), nunca con input del usuario, evitando inyección SQL por nombre de tabla.
- `ruta_absoluta_segura(ruta_relativa, conn=None, carpeta_base=None)` — si `carpeta_base` es `None`, usa `carpeta_pdf_actual(conn)` como hoy.
- `imagen_primera_hoja_con_marca(...)` se generaliza a `imagen_pagina_con_marca(ruta_pdf, num_pagina, ruta_preview_base, usuario, rol, ip, cuando=None)`, donde `num_pagina` es **0-indexado** (coincide con `fitz.Document.load_page`): `num_pagina=0` (primera hoja) puede usar el preview cacheado, y `num_pagina>0` siempre renderiza directo del PDF.

**Convención de numeración en la URL:** el parámetro `{n}` de `/reservados/visor/{id}/imagen/{n}` y el query param `?pagina=N` del visor son **1-indexados** (coinciden con lo que ve el usuario: "Página 1 de N"). La ruta resta 1 antes de llamar a `imagen_pagina_con_marca`. Página fuera de rango (`N < 1` o `N > cantidad_paginas`) devuelve 404.

Esto evita duplicar ~250 líneas de lógica de extracción de texto y consulta FTS5, mientras el aislamiento real (qué tabla, qué carpeta, qué usuarios pueden acceder) se mantiene en la capa de datos y rutas.

`app.py` agrega una sección nueva `# ── RESERVADOS ──` con sus propias rutas, sin tocar las rutas de memorandos existentes.

---

## Control de acceso

Nueva dependencia en `app.py`, junto a `require_roles`:

```python
def require_reservados(request: Request, user: Annotated[dict, Depends(require_login)]) -> dict[str, Any]:
    require_password_ok(request, user)
    if user["rol"] != "ADMIN" and not user.get("permiso_reservados"):
        with get_db() as conn:
            registrar_auditoria(
                conn, "ACCESO_DENEGADO", usuario_id=user["id"],
                detalle=f"Ruta: {request.url.path}",
                ip=client_ip(request), equipo=ua(request), resultado="DENEGADO",
            )
        raise HTTPException(status_code=303, headers={"Location": "/acceso_denegado"})
    return user
```

Todas las rutas de Reservados usan `Depends(require_reservados)`.

---

## Rutas nuevas

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/reservados` | Formulario de búsqueda (réplica de `/buscar`) |
| GET | `/reservados/resultados` | Resultados paginados (réplica de `/buscar/resultados`), sin botón de descarga, solo "Visualizar" |
| GET | `/reservados/visor/{id}` | Visor HTML con navegación de páginas (`?pagina=N`) |
| GET | `/reservados/visor/{id}/imagen/{n}` | PNG de la página `n` con marca de agua, generado al vuelo |
| GET | `/admin/reservados/reindexar` | Página de indexación (réplica de `/admin/reindexar`) |
| POST | `/admin/reservados/reindexar/iniciar` | Inicia job de indexación (rápida/completa) |
| GET | `/admin/reservados/reindexar/estado/{job_id}` | Polling de progreso |
| POST | `/admin/usuario/{uid}/permiso_reservados` | Toggle del permiso (solo ADMIN, vía `require_admin`) |

Las rutas de búsqueda y visor (`/reservados`, `/reservados/resultados`, `/reservados/visor/*`) están protegidas con `require_reservados`. Las rutas bajo `/admin/` (indexación y toggle de permiso) están protegidas con `require_admin`, igual que el resto de rutas administrativas del sistema — coherente con que solo ADMIN reindexa memorandos hoy.

---

## Templates nuevos

- `templates/reservados_buscar.html` — réplica de `buscar.html`
- `templates/reservados_resultados.html` — réplica de `resultados.html` sin acciones de descarga, con botón "Visualizar" → `/reservados/visor/{id}`
- `templates/reservados_visor.html` — imagen central + navegación "Anterior / Página X de N / Siguiente"; `oncontextmenu="return false"` en la imagen como disuasivo
- `templates/reservados_reindexar.html` — réplica de `reindexar.html` (nombre sin prefijo `admin_`, igual convención que el resto de templates del proyecto)

### Cambios en templates existentes

- `templates/base.html`: ítem de navegación nuevo, fuera de los bloques `{% if user.rol == ... %}`:
  ```html
  {% if user.rol == 'ADMIN' or user.permiso_reservados %}
  <a href="/reservados" class="nav-item"><i class="fa-solid fa-lock"></i><span>Reservados</span></a>
  {% endif %}
  ```
- `templates/usuarios.html` (plantilla de `/admin/usuarios`): columna nueva "Reservados" con toggle Sí/No por fila, llamando a `POST /admin/usuario/{uid}/permiso_reservados`.

---

## Flujo de datos

```
ADMIN otorga permiso
        │
        ▼
POST /admin/usuario/{uid}/permiso_reservados
        ├─ UPDATE usuarios SET permiso_reservados = NOT permiso_reservados
        └─ registrar_auditoria("PERMISO_RESERVADOS_OTORGADO"|"REVOCADO")

Usuario con permiso busca
        │
        ▼
GET /reservados/resultados?q=...
        ├─ buscar_memorandos(q, tabla="reservados", ...)
        └─ registrar_auditoria("RESERVADO_BUSQUEDA_REALIZADA") [solo página 1]

Usuario hace clic en "Visualizar"
        │
        ▼
GET /reservados/visor/{id}
        ├─ require_reservados()
        ├─ registrar_auditoria("RESERVADO_VISOR_ABIERTO")
        └─ render reservados_visor.html con cantidad_paginas

[el navegador pide cada imagen]
GET /reservados/visor/{id}/imagen/{n}
        ├─ ruta_absoluta_segura(..., carpeta_base=carpeta_reservados_actual())
        └─ imagen_pagina_con_marca(ruta, n, ...) → PNG (sin auditoría por página)
```

---

## Manejo de errores

| Situación | Comportamiento |
|---|---|
| Usuario sin permiso intenta acceder a `/reservados*` | Redirect a `/acceso_denegado`, audita `ACCESO_DENEGADO` |
| Carpeta `INFORMES RV` no accesible al indexar | Igual que hoy: stats con `errores=1`, mensaje "La carpeta no existe: {ruta}" |
| Archivo no encontrado en disco al abrir el visor | 404 |
| Número de página fuera de rango | 404 |

---

## Auditoría

Nuevas acciones en la tabla `auditoria` existente (sin cambios de esquema en `audit_service.py`, solo nuevos valores de `accion`):

- `RESERVADO_BUSQUEDA_REALIZADA`
- `RESERVADO_VISOR_ABIERTO`
- `PERMISO_RESERVADOS_OTORGADO` / `PERMISO_RESERVADOS_REVOCADO`
- `INDEXACION_RESERVADOS_RAPIDA_INICIADA` / `_FINALIZADA`
- `REINDEXACION_RESERVADOS_COMPLETA_INICIADA` / `_FINALIZADA`
- `ERROR_INDEXACION_RESERVADOS`
- `ACCESO_DENEGADO` (reusa el existente; el detalle ya incluye la ruta intentada)

---

## Limitación conocida (a comunicar al usuario)

El visor impide descargar el PDF original desde el navegador (no hay botón, no hay endpoint que lo entregue, clic derecho deshabilitado). **No impide una captura de pantalla** — esa es una limitación inherente a cualquier visor "solo lectura" en un navegador. Por eso cada imagen lleva la marca de agua con usuario, rol, fecha, hora e IP: es el mismo nivel de seguridad que ya usa el sistema hoy en `/ver_primera_hoja`, ahora aplicado a todas las páginas.

---

## Dependencias

Sin dependencias nuevas. Usa exclusivamente `PyMuPDF` (fitz), `Pillow` (ya usada para la marca de agua) y `fastapi`/`jinja2`, todas ya presentes.
