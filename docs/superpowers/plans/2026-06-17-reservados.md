# Módulo "Reservados" — Plan de Implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agregar una solapa "Reservados" donde solo los usuarios con permiso individual (otorgado por ADMIN) pueden buscar y visualizar — sin descargar — los PDFs de `C:\Users\SIGEMEP\Desktop\INFORMES RV`.

**Architecture:** Tabla y FTS5 propios (`reservados`/`reservados_fts`), separados de `memorandos`. El motor de indexación (`pdf_service.py`) y de búsqueda (`search_service.py`) se generaliza para aceptar tabla/carpeta como parámetro, sin cambiar el comportamiento por defecto de memorandos. El visor nunca entrega el PDF real: cada página se renderiza como PNG con marca de agua, generada al vuelo.

**Tech Stack:** FastAPI, PyMuPDF (fitz), Pillow, SQLite/FTS5 — todos ya instalados. Sin dependencias nuevas.

## Global Constraints

- No modificar `C:\SIGEMEP_APP` (producción) — todo el trabajo es en `C:\SIGEMEP_APP_DEV`.
- No crear scripts `fix_*.py` — los cambios van directo en los archivos correspondientes.
- No hay suite de tests con pytest en este proyecto; la verificación de cada tarea se hace con scripts `python -c` puntuales (mismo patrón que el plan de memorandos) y, en la tarea final, con pruebas manuales en el navegador.
- `services/pdf_service.py` ya importa `ruta_absoluta_segura` y `app.py` define una función local con el **mismo nombre** que la sobreescribe (line shadowing) — por eso este plan agrega una función con nombre **distinto** (`ruta_absoluta_segura_reservados`) para Reservados, evitando esa colisión por completo. No se toca la función existente `ruta_absoluta_segura` en ningún archivo.
- `static/js/security.js` ya se carga en **todas** las páginas autenticadas vía `templates/base.html:53` (deshabilita clic derecho, F12, Ctrl+P/S/U, Ctrl+Shift+I/J/C). El visor de Reservados hereda esta protección automáticamente — no se necesita JS adicional.
- Todos los nombres de tabla SQL interpolados en f-strings deben validarse contra una lista cerrada (`{"memorandos", "reservados"}`) antes de interpolar, nunca aceptar el nombre de tabla desde input de usuario.

---

## Mapa de archivos

| Archivo | Acción | Responsabilidad |
|---|---|---|
| `config.py` | Modificar | Constantes de carpeta de Reservados |
| `services/db.py` | Modificar | Tabla `reservados`/`reservados_fts`, columna `permiso_reservados`, `rebuild_fts` generalizado |
| `services/pdf_service.py` | Modificar | `carpeta_reservados_actual`, `ruta_absoluta_segura_reservados`, `indexar_memorandos` generalizado, `imagen_pagina_con_marca` |
| `services/search_service.py` | Modificar | `buscar_memorandos` generalizado con parámetro `tabla` |
| `app.py` | Modificar | `require_reservados`, rutas de indexación/búsqueda/visor/permiso |
| `templates/reservados_reindexar.html` | Crear | Página de indexación (admin) |
| `templates/reservados_buscar.html` | Crear | Formulario de búsqueda |
| `templates/reservados_resultados.html` | Crear | Resultados de búsqueda |
| `templates/reservados_visor.html` | Crear | Visor paginado de imágenes |
| `templates/usuarios.html` | Modificar | Columna de toggle de permiso |
| `templates/base.html` | Modificar | Ítem de navegación "Reservados" |

---

## Task 1: Modelo de datos y configuración

**Files:**
- Modify: `config.py`
- Modify: `services/db.py`

**Interfaces:**
- Produces: `config.RESERVADOS_BASE_DIR: Path`, `config.DEFAULT_RESERVADOS_DIR: str`; tabla SQL `reservados` (mismas columnas que `memorandos`) y `reservados_fts`; columna `usuarios.permiso_reservados INTEGER`; clave `configuracion.reservados_dir`.

- [ ] **Step 1: Agregar las constantes de carpeta en `config.py`**

En `config.py`, después de la línea `PDF_BASE_DIR = Path(os.environ.get("SIGEMEP_PDF_DIR", DEFAULT_PDF_DIR))`, agregar:

```python

# Ruta definitiva por defecto para los archivos reservados.
DEFAULT_RESERVADOS_DIR = r"C:\Users\SIGEMEP\Desktop\INFORMES RV"
RESERVADOS_BASE_DIR = Path(os.environ.get("SIGEMEP_RESERVADOS_DIR", DEFAULT_RESERVADOS_DIR))
```

- [ ] **Step 2: Agregar la tabla `reservados` al script de creación en `services/db.py`**

En `services/db.py`, dentro de `init_db()`, en el bloque `conn.executescript("""...""")`, agregar una tabla nueva justo después del cierre de `CREATE TABLE IF NOT EXISTS memorandos (...)` (antes de `CREATE TABLE IF NOT EXISTS auditoria`):

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
```

- [ ] **Step 3: Agregar la columna `permiso_reservados` a `usuarios`**

En `services/db.py`, en la sección de migraciones (después de las líneas `_add_column_if_missing(conn, "memorandos", "ultima_revision", "DATETIME")`), agregar:

```python

        _add_column_if_missing(conn, "usuarios", "permiso_reservados", "INTEGER NOT NULL DEFAULT 0")
```

- [ ] **Step 4: Generalizar `rebuild_fts` y agregar la tabla virtual `reservados_fts`**

Reemplazar la función completa:

```python
def rebuild_fts(conn) -> None:
    """Reconstruye el índice FTS5 desde la tabla memorandos."""
    conn.execute("INSERT INTO memorandos_fts(memorandos_fts) VALUES('rebuild')")
```

por:

```python
def rebuild_fts(conn, tabla_fts: str = "memorandos_fts") -> None:
    """Reconstruye el índice FTS5 indicado (memorandos_fts o reservados_fts)."""
    if tabla_fts not in ("memorandos_fts", "reservados_fts"):
        raise ValueError(f"tabla_fts no permitida: {tabla_fts}")
    conn.execute(f"INSERT INTO {tabla_fts}({tabla_fts}) VALUES('rebuild')")
```

- [ ] **Step 5: Crear la tabla virtual `reservados_fts` y sembrar su config**

En `services/db.py`, justo después de este bloque existente (déjalo intacto):

```python
        if get_config(conn, "fts5_inicializado", "0") == "0":
            conn.execute("INSERT INTO memorandos_fts(memorandos_fts) VALUES('rebuild')")
            set_config(conn, "fts5_inicializado", "1")
```

agregar:

```python

        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS reservados_fts USING fts5(
                nombre_archivo,
                texto_extraido,
                content='reservados',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        if get_config(conn, "reservados_fts5_inicializado", "0") == "0":
            conn.execute("INSERT INTO reservados_fts(reservados_fts) VALUES('rebuild')")
            set_config(conn, "reservados_fts5_inicializado", "1")
```

- [ ] **Step 6: Sembrar la carpeta de Reservados en `configuracion`**

En `services/db.py`, después de:

```python
        if not get_config(conn, "pdf_dir", ""):
            set_config(conn, "pdf_dir", DEFAULT_PDF_DIR)
```

agregar:

```python

        if not get_config(conn, "reservados_dir", ""):
            set_config(conn, "reservados_dir", DEFAULT_RESERVADOS_DIR)
```

y agregar `DEFAULT_RESERVADOS_DIR` al import existente en la cabecera del archivo:

```python
from config import DATABASE_PATH, DEFAULT_PDF_DIR, DEFAULT_RESERVADOS_DIR, LOGS_DIR, PREVIEWS_DIR
```

- [ ] **Step 7: Verificar el modelo de datos**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.db import init_db, get_db, get_config
init_db()
with get_db() as conn:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(usuarios)').fetchall()}
    assert 'permiso_reservados' in cols, 'falta columna permiso_reservados'
    conn.execute(\"SELECT 1 FROM reservados\")
    conn.execute(\"SELECT 1 FROM reservados_fts\")
    print('reservados_dir =', get_config(conn, 'reservados_dir', ''))
print('OK - modelo de datos correcto')
"
```

Esperado: `reservados_dir = C:\Users\SIGEMEP\Desktop\INFORMES RV` seguido de `OK - modelo de datos correcto`, sin excepciones.

- [ ] **Step 8: Confirmar que memorandos sigue intacto**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.db import get_db
with get_db() as conn:
    n = conn.execute('SELECT COUNT(*) FROM memorandos').fetchone()[0]
    print('memorandos activos/inactivos:', n)
print('OK - memorandos no se vio afectado')
"
```

Esperado: el mismo número de memorandos que había antes de este cambio, sin errores.

---

## Task 2: Generalizar el motor de PDFs (`services/pdf_service.py`)

**Files:**
- Modify: `services/pdf_service.py`

**Interfaces:**
- Consumes: `config.RESERVADOS_BASE_DIR` (Task 1), `services.db.rebuild_fts(conn, tabla_fts=...)` (Task 1).
- Produces: `carpeta_reservados_actual(conn=None) -> Path`; `ruta_absoluta_segura_reservados(ruta_relativa, conn=None) -> Optional[Path]`; `indexar_memorandos(conn, admin_id=None, progress_callback=None, force=False, tabla="memorandos", carpeta=None) -> dict[str, int]` (firma extendida, compatible); `imagen_pagina_con_marca(ruta_pdf, num_pagina, ruta_preview_base, usuario, rol, ip, cuando=None) -> bytes`.

- [ ] **Step 1: Agregar `RESERVADOS_BASE_DIR` al import**

Reemplazar:

```python
from config import PDF_BASE_DIR, PREVIEWS_DIR
```

por:

```python
from config import PDF_BASE_DIR, PREVIEWS_DIR, RESERVADOS_BASE_DIR
```

- [ ] **Step 2: Agregar `carpeta_reservados_actual` después de `carpeta_pdf_actual`**

Después de la función `carpeta_pdf_actual` (termina en `return Path(PDF_BASE_DIR)`), agregar:

```python


def carpeta_reservados_actual(conn=None) -> Path:
    """
    Devuelve la carpeta configurada de Reservados.
    Si existe config.reservados_dir en la base, usa esa ruta.
    Si no existe, usa RESERVADOS_BASE_DIR de config.py.
    """
    try:
        if conn is not None:
            row = conn.execute("SELECT valor FROM configuracion WHERE clave = 'reservados_dir'").fetchone()
            if row and row["valor"]:
                return Path(row["valor"])
    except Exception:
        pass

    return Path(RESERVADOS_BASE_DIR)
```

- [ ] **Step 3: Agregar `ruta_absoluta_segura_reservados` después de `ruta_absoluta_segura`**

Después de la función `ruta_absoluta_segura` existente (termina en `return candidate if candidate.is_file() else None`), agregar:

```python


def ruta_absoluta_segura_reservados(ruta_relativa: str, conn=None) -> Optional[Path]:
    """
    Resuelve una ruta de Reservados de forma segura, igual que ruta_absoluta_segura
    pero acotada siempre a la carpeta de Reservados configurada (nunca a la de
    memorandos). Función separada a propósito: app.py define su propia función
    local llamada ruta_absoluta_segura que sobreescribe la importada desde este
    módulo, así que reutilizarla con un parámetro extra no sería seguro.
    """
    base = carpeta_reservados_actual(conn).resolve()

    if not ruta_relativa:
        return None

    raw = Path(str(ruta_relativa))

    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        rel = raw.as_posix().lstrip("/").replace("\\", "/")
        candidate = (base / rel).resolve()

    try:
        candidate.relative_to(base)
    except ValueError:
        return None

    return candidate if candidate.is_file() else None
```

- [ ] **Step 4: Generalizar `_ensure_memorando_columns` con validación de tabla**

Reemplazar:

```python
def _ensure_memorando_columns(conn) -> None:
    """
    Asegura columnas usadas por indexación incremental.
    Si ya existen, ignora el error.
    """
    for sql in [
        "ALTER TABLE memorandos ADD COLUMN tamanio_bytes INTEGER",
        "ALTER TABLE memorandos ADD COLUMN mtime INTEGER",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass
```

por:

```python
_TABLAS_INDEXABLES = {
    "memorandos": "memorandos_fts",
    "reservados": "reservados_fts",
}


def _validar_tabla_indexable(tabla: str) -> str:
    if tabla not in _TABLAS_INDEXABLES:
        raise ValueError(f"Tabla no permitida para indexación: {tabla}")
    return tabla


def _ensure_memorando_columns(conn, tabla: str = "memorandos") -> None:
    """
    Asegura columnas usadas por indexación incremental en la tabla indicada.
    Si ya existen, ignora el error.
    """
    tabla = _validar_tabla_indexable(tabla)
    for columna_sql in ["tamanio_bytes INTEGER", "mtime INTEGER"]:
        try:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna_sql}")
        except Exception:
            pass
```

- [ ] **Step 5: Generalizar la firma e interior de `indexar_memorandos`**

Reemplazar la firma:

```python
def indexar_memorandos(
    conn,
    admin_id: Optional[int] = None,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    force: bool = False,
) -> dict[str, int]:
    """
    Escanea la carpeta PDF configurada y actualiza tabla memorandos.

    force=False:
        Actualización rápida. Si tamaño y modificación no cambiaron, no reextrae.
    force=True:
        Reindexación completa. Reprocesa todos los PDFs.
    """
    base = carpeta_pdf_actual(conn).resolve()
```

por:

```python
def indexar_memorandos(
    conn,
    admin_id: Optional[int] = None,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    force: bool = False,
    tabla: str = "memorandos",
    carpeta: Optional[Path] = None,
) -> dict[str, int]:
    """
    Escanea la carpeta configurada y actualiza la tabla correspondiente
    (memorandos o reservados, según `tabla`).

    force=False:
        Actualización rápida. Si tamaño y modificación no cambiaron, no reextrae.
    force=True:
        Reindexación completa. Reprocesa todos los PDFs.
    """
    tabla = _validar_tabla_indexable(tabla)
    tabla_fts = _TABLAS_INDEXABLES[tabla]
    base = (carpeta if carpeta is not None else carpeta_pdf_actual(conn)).resolve()
```

Reemplazar la línea:

```python
    _ensure_memorando_columns(conn)
```

por:

```python
    _ensure_memorando_columns(conn, tabla=tabla)
```

Reemplazar las seis consultas SQL que referencian `memorandos` directamente dentro del bucle y al final de la función:

```python
                row = conn.execute(
                    "SELECT id, tamanio_bytes, mtime FROM memorandos WHERE ruta_archivo = ?",
                    (rel,)
                ).fetchone()
```
→
```python
                row = conn.execute(
                    f"SELECT id, tamanio_bytes, mtime FROM {tabla} WHERE ruta_archivo = ?",
                    (rel,)
                ).fetchone()
```

```python
                    conn.execute(
                        "UPDATE memorandos SET activo = 1 WHERE id = ?",
                        (row["id"],)
                    )
```
→
```python
                    conn.execute(
                        f"UPDATE {tabla} SET activo = 1 WHERE id = ?",
                        (row["id"],)
                    )
```

```python
                    preview_name = f"m_{hashlib.md5(rel.encode('utf-8')).hexdigest()[:16]}.png"
```
→
```python
                    preview_name = f"{tabla[:1]}_{hashlib.md5(rel.encode('utf-8')).hexdigest()[:16]}.png"
```

```python
                        conn.execute(
                            """
                            UPDATE memorandos SET
                                nombre_archivo = ?,
                                texto_extraido = ?,
                                cantidad_paginas = ?,
                                primera_hoja_img = ?,
                                fecha_indexado = CURRENT_TIMESTAMP,
                                activo = 1,
                                tamanio_bytes = ?,
                                mtime = ?
                            WHERE id = ?
                            """,
```
→
```python
                        conn.execute(
                            f"""
                            UPDATE {tabla} SET
                                nombre_archivo = ?,
                                texto_extraido = ?,
                                cantidad_paginas = ?,
                                primera_hoja_img = ?,
                                fecha_indexado = CURRENT_TIMESTAMP,
                                activo = 1,
                                tamanio_bytes = ?,
                                mtime = ?
                            WHERE id = ?
                            """,
```

```python
                        conn.execute(
                            """
                            INSERT INTO memorandos (
                                nombre_archivo,
                                ruta_archivo,
                                texto_extraido,
                                cantidad_paginas,
                                primera_hoja_img,
                                activo,
                                tamanio_bytes,
                                mtime
                            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                            """,
```
→
```python
                        conn.execute(
                            f"""
                            INSERT INTO {tabla} (
                                nombre_archivo,
                                ruta_archivo,
                                texto_extraido,
                                cantidad_paginas,
                                primera_hoja_img,
                                activo,
                                tamanio_bytes,
                                mtime
                            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                            """,
```

```python
        rows = conn.execute("SELECT id, ruta_archivo FROM memorandos").fetchall()
        for row in rows:
            if row["ruta_archivo"] not in vistos_rel:
                conn.execute("UPDATE memorandos SET activo = 0 WHERE id = ?", (row["id"],))
```
→
```python
        rows = conn.execute(f"SELECT id, ruta_archivo FROM {tabla}").fetchall()
        for row in rows:
            if row["ruta_archivo"] not in vistos_rel:
                conn.execute(f"UPDATE {tabla} SET activo = 0 WHERE id = ?", (row["id"],))
```

Y la llamada final:

```python
    try:
        rebuild_fts(conn)
    except Exception:
        pass
```
→
```python
    try:
        rebuild_fts(conn, tabla_fts=tabla_fts)
    except Exception:
        pass
```

- [ ] **Step 6: Agregar `imagen_pagina_con_marca` y delegar `imagen_primera_hoja_con_marca`**

Reemplazar la función completa:

```python
def imagen_primera_hoja_con_marca(
    ruta_pdf: Path,
    ruta_preview_base: Optional[Path],
    usuario: str,
    rol: str,
    ip: str,
    cuando: Optional[datetime] = None,
) -> bytes:
    """
    Devuelve PNG de primera hoja con marca de agua original.
    Usa preview cache si existe; si no, renderiza directamente desde PDF.
    """
    cuando = cuando or datetime.now()

    if ruta_preview_base and ruta_preview_base.is_file():
        img = Image.open(ruta_preview_base).convert("RGB")
    else:
        doc = fitz.open(ruta_pdf)
        try:
            page = doc.load_page(0)
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()

    final_img = _dibujar_marca_agua(
        img,
        usuario,
        rol,
        ip,
        cuando
    )

    buf = io.BytesIO()
    final_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
```

por:

```python
def imagen_pagina_con_marca(
    ruta_pdf: Path,
    num_pagina: int,
    ruta_preview_base: Optional[Path],
    usuario: str,
    rol: str,
    ip: str,
    cuando: Optional[datetime] = None,
) -> bytes:
    """
    Devuelve PNG de la página `num_pagina` (0-indexada) con marca de agua.
    La página 0 puede usar el preview cacheado; el resto siempre se renderiza
    directo del PDF (no se cachea en disco).
    """
    cuando = cuando or datetime.now()

    if num_pagina == 0 and ruta_preview_base and ruta_preview_base.is_file():
        img = Image.open(ruta_preview_base).convert("RGB")
    else:
        doc = fitz.open(ruta_pdf)
        try:
            page = doc.load_page(num_pagina)
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()

    final_img = _dibujar_marca_agua(
        img,
        usuario,
        rol,
        ip,
        cuando
    )

    buf = io.BytesIO()
    final_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def imagen_primera_hoja_con_marca(
    ruta_pdf: Path,
    ruta_preview_base: Optional[Path],
    usuario: str,
    rol: str,
    ip: str,
    cuando: Optional[datetime] = None,
) -> bytes:
    """Devuelve PNG de primera hoja con marca de agua. Usa preview cache si existe."""
    return imagen_pagina_con_marca(ruta_pdf, 0, ruta_preview_base, usuario, rol, ip, cuando)
```

- [ ] **Step 7: Verificar que memorandos indexa exactamente igual que antes**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.db import get_db, init_db
from services.pdf_service import indexar_memorandos
init_db()
with get_db() as conn:
    stats = indexar_memorandos(conn, force=False)
    print('memorandos:', stats)
print('OK - indexar_memorandos sigue funcionando con los defaults de siempre')
"
```

Esperado: stats con `errores: 0` (o el mismo comportamiento que tenía antes de este cambio), sin excepciones.

- [ ] **Step 8: Verificar la indexación de Reservados con una carpeta de prueba**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from pathlib import Path
import shutil, fitz
from services.db import get_db, init_db
from services.pdf_service import indexar_memorandos, carpeta_reservados_actual

init_db()
tmp = Path('C:/SIGEMEP_APP_DEV/_tmp_reservados_test')
tmp.mkdir(exist_ok=True)
doc = fitz.open()
doc.new_page()
doc.save(str(tmp / 'prueba.pdf'))
doc.close()

with get_db() as conn:
    stats = indexar_memorandos(conn, force=True, tabla='reservados', carpeta=tmp)
    print('reservados:', stats)
    fila = conn.execute(\"SELECT nombre_archivo FROM reservados WHERE ruta_archivo = 'prueba.pdf'\").fetchone()
    assert fila is not None, 'no se indexo el PDF de prueba en la tabla reservados'
    fila_mem = conn.execute(\"SELECT 1 FROM memorandos WHERE nombre_archivo = 'prueba.pdf'\").fetchone()
    assert fila_mem is None, 'el PDF de prueba no debe aparecer en memorandos'

shutil.rmtree(tmp)
print('OK - indexar_memorandos generalizado funciona para reservados y no contamina memorandos')
"
```

Esperado: `OK - indexar_memorandos generalizado funciona para reservados y no contamina memorandos`, sin excepciones.

- [ ] **Step 9: Limpiar el registro de prueba de la tabla `reservados`**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.db import get_db, init_db
init_db()
with get_db() as conn:
    conn.execute(\"DELETE FROM reservados WHERE ruta_archivo = 'prueba.pdf'\")
print('OK - registro de prueba eliminado')
"
```

---

## Task 3: Generalizar el motor de búsqueda (`services/search_service.py`)

**Files:**
- Modify: `services/search_service.py`

**Interfaces:**
- Produces: `buscar_memorandos(query, limit=100, campo="todo", fecha_desde="", fecha_hasta="", paginas_min=0, paginas_max=0, tabla="memorandos") -> list[dict]` (firma extendida, compatible).

- [ ] **Step 1: Agregar la validación de tabla al inicio del archivo**

Después de `from services.db import get_db`, agregar:

```python

_TABLAS_FTS = {
    "memorandos": "memorandos_fts",
    "reservados": "reservados_fts",
}


def _validar_tabla(tabla: str) -> str:
    if tabla not in _TABLAS_FTS:
        raise ValueError(f"Tabla de búsqueda no permitida: {tabla}")
    return tabla
```

- [ ] **Step 2: Generalizar `_buscar_solo_filtros`**

Reemplazar la firma y la consulta SQL:

```python
def _buscar_solo_filtros(
    fecha_desde: str,
    fecha_hasta: str,
    paginas_min: int,
    paginas_max: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Devuelve memorandos por fecha/páginas sin texto de búsqueda."""
    conditions = ["activo = 1"]
```

por:

```python
def _buscar_solo_filtros(
    fecha_desde: str,
    fecha_hasta: str,
    paginas_min: int,
    paginas_max: int,
    limit: int,
    tabla: str = "memorandos",
) -> list[dict[str, Any]]:
    """Devuelve registros por fecha/páginas sin texto de búsqueda."""
    tabla = _validar_tabla(tabla)
    conditions = ["activo = 1"]
```

y reemplazar:

```python
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM memorandos WHERE {' AND '.join(conditions)} ORDER BY fecha_indexado DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]
```

por:

```python
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM {tabla} WHERE {' AND '.join(conditions)} ORDER BY fecha_indexado DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 3: Generalizar `buscar_memorandos`**

Reemplazar la firma:

```python
def buscar_memorandos(
    query: str,
    limit: int = 100,
    campo: str = "todo",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    paginas_min: int = 0,
    paginas_max: int = 0,
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        if fecha_desde or fecha_hasta or paginas_min or paginas_max:
            return _buscar_solo_filtros(fecha_desde, fecha_hasta, paginas_min, paginas_max, limit)
        return []
```

por:

```python
def buscar_memorandos(
    query: str,
    limit: int = 100,
    campo: str = "todo",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    paginas_min: int = 0,
    paginas_max: int = 0,
    tabla: str = "memorandos",
) -> list[dict[str, Any]]:
    tabla = _validar_tabla(tabla)
    tabla_fts = _TABLAS_FTS[tabla]
    q = query.strip()
    if not q:
        if fecha_desde or fecha_hasta or paginas_min or paginas_max:
            return _buscar_solo_filtros(fecha_desde, fecha_hasta, paginas_min, paginas_max, limit, tabla=tabla)
        return []
```

Reemplazar las dos consultas con nombre de tabla fijo dentro del bloque `try`:

```python
            fts_rows = conn.execute(
                "SELECT rowid FROM memorandos_fts WHERE memorandos_fts MATCH ? ORDER BY rank LIMIT 5000",
                (fts_q,),
            ).fetchall()
```
→
```python
            fts_rows = conn.execute(
                f"SELECT rowid FROM {tabla_fts} WHERE {tabla_fts} MATCH ? ORDER BY rank LIMIT 5000",
                (fts_q,),
            ).fetchall()
```

```python
            mem_rows = conn.execute(
                f"SELECT * FROM memorandos WHERE {' AND '.join(conditions)}",
                params,
            ).fetchall()
```
→
```python
            mem_rows = conn.execute(
                f"SELECT * FROM {tabla} WHERE {' AND '.join(conditions)}",
                params,
            ).fetchall()
```

Y el `except`:

```python
        except Exception:
            return _buscar_fallback(q, limit)
```
→
```python
        except Exception:
            return _buscar_fallback(q, limit, tabla=tabla)
```

- [ ] **Step 4: Generalizar `_buscar_fallback`**

Reemplazar:

```python
def _buscar_fallback(query: str, limit: int) -> list[dict[str, Any]]:
    """Búsqueda en Python si FTS5 no está disponible."""
    q_lower = query.lower()
    tokens = query.split()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM memorandos WHERE activo = 1").fetchall()
```

por:

```python
def _buscar_fallback(query: str, limit: int, tabla: str = "memorandos") -> list[dict[str, Any]]:
    """Búsqueda en Python si FTS5 no está disponible."""
    tabla = _validar_tabla(tabla)
    q_lower = query.lower()
    tokens = query.split()
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM {tabla} WHERE activo = 1").fetchall()
```

- [ ] **Step 5: Verificar búsqueda en memorandos (comportamiento sin cambios)**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.search_service import buscar_memorandos
r = buscar_memorandos('a', limit=5)
print('resultados memorandos:', len(r))
print('OK - busqueda de memorandos sigue funcionando')
"
```

Esperado: imprime una cantidad de resultados (puede ser 0 o más, según los datos existentes), sin excepciones.

- [ ] **Step 6: Verificar búsqueda en reservados con un registro de prueba**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.db import get_db, init_db, rebuild_fts
from services.search_service import buscar_memorandos

init_db()
with get_db() as conn:
    conn.execute(
        \"INSERT INTO reservados (nombre_archivo, ruta_archivo, texto_extraido, cantidad_paginas, activo) \"
        \"VALUES ('informe_prueba.pdf', 'informe_prueba.pdf', 'contenido de prueba unico_xyz', 1, 1)\"
    )
    rebuild_fts(conn, tabla_fts='reservados_fts')

resultados = buscar_memorandos('unico_xyz', tabla='reservados')
assert len(resultados) == 1, f'esperaba 1 resultado, obtuve {len(resultados)}'
assert resultados[0]['nombre_archivo'] == 'informe_prueba.pdf'

resultados_mem = buscar_memorandos('unico_xyz', tabla='memorandos')
assert len(resultados_mem) == 0, 'el registro de reservados no debe aparecer en memorandos'

with get_db() as conn:
    conn.execute(\"DELETE FROM reservados WHERE ruta_archivo = 'informe_prueba.pdf'\")
    rebuild_fts(conn, tabla_fts='reservados_fts')

print('OK - busqueda generalizada aisla reservados de memorandos correctamente')
"
```

Esperado: `OK - busqueda generalizada aisla reservados de memorandos correctamente`, sin excepciones.

---

## Task 4: Control de acceso — dependencia `require_reservados`

**Files:**
- Modify: `app.py`

**Interfaces:**
- Consumes: `require_login`, `require_password_ok`, `registrar_auditoria`, `client_ip`, `ua` (ya existentes en `app.py`).
- Produces: `require_reservados(request, user) -> dict[str, Any]` (dependencia FastAPI).

- [ ] **Step 1: Agregar `require_reservados` después de `require_roles`**

En `app.py`, después de la función `require_roles` (termina en `return dep`), agregar:

```python


def require_reservados(request: Request, user: Annotated[dict, Depends(require_login)]) -> dict[str, Any]:
    require_password_ok(request, user)
    if user["rol"] != "ADMIN" and not user.get("permiso_reservados"):
        with get_db() as conn:
            registrar_auditoria(
                conn,
                "ACCESO_DENEGADO",
                usuario_id=user["id"],
                detalle=f"Ruta: {request.url.path}",
                ip=client_ip(request),
                equipo=ua(request),
                resultado="DENEGADO",
            )
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/acceso_denegado"})
    return user
```

- [ ] **Step 2: Verificar que el módulo sigue cargando sin errores**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'C:/SIGEMEP_APP_DEV')
import app
assert hasattr(app, 'require_reservados')
print('OK - require_reservados definido y app.py carga sin errores')
"
```

Esperado: `OK - require_reservados definido y app.py carga sin errores`.

---

## Task 5: Indexación de Reservados (rutas admin)

**Files:**
- Modify: `app.py`
- Create: `templates/reservados_reindexar.html`

**Interfaces:**
- Consumes: `carpeta_reservados_actual`, `indexar_memorandos(tabla="reservados", carpeta=...)` (Task 2); `require_admin`, `_set_index_job`-style helpers (se crean versión propia para no compartir estado con la indexación de memorandos).
- Produces: rutas `GET /admin/reservados/reindexar`, `POST /admin/reservados/reindexar/iniciar`, `GET /admin/reservados/reindexar/estado/{job_id}`.

- [ ] **Step 1: Importar `carpeta_reservados_actual` en `app.py`**

Reemplazar:

```python
from services.pdf_service import (
    carpeta_pdf_actual,
    imagen_primera_hoja_con_marca,
    indexar_memorandos,
    ruta_absoluta_segura,
)
```

por:

```python
from services.pdf_service import (
    carpeta_pdf_actual,
    carpeta_reservados_actual,
    imagen_pagina_con_marca,
    imagen_primera_hoja_con_marca,
    indexar_memorandos,
    ruta_absoluta_segura,
    ruta_absoluta_segura_reservados,
)
```

- [ ] **Step 2: Agregar el estado de jobs de indexación de Reservados**

Después de:

```python
INDEX_JOBS: dict[str, dict[str, Any]] = {}
INDEX_JOBS_LOCK = threading.Lock()
```

agregar:

```python
RESERVADOS_INDEX_JOBS: dict[str, dict[str, Any]] = {}
RESERVADOS_INDEX_JOBS_LOCK = threading.Lock()
```

- [ ] **Step 3: Agregar los helpers y el worker de indexación de Reservados**

Después de la función `_index_worker` (termina en `_set_index_job(job_id, {"estado": "error", "mensaje": str(exc)[:500]})`), agregar:

```python


def _set_reservados_index_job(job_id: str, data: dict[str, Any]) -> None:
    with RESERVADOS_INDEX_JOBS_LOCK:
        job = RESERVADOS_INDEX_JOBS.setdefault(job_id, {})
        job.update(data)
        job["actualizado_en"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _get_reservados_index_job(job_id: str) -> dict[str, Any]:
    with RESERVADOS_INDEX_JOBS_LOCK:
        return dict(RESERVADOS_INDEX_JOBS.get(job_id, {}))


def _index_reservados_worker(job_id: str, admin_id: int, ip: str, equipo: str, force: bool) -> None:
    modo = "completa" if force else "rapida"
    accion_ini = "REINDEXACION_RESERVADOS_COMPLETA_INICIADA" if force else "INDEXACION_RESERVADOS_RAPIDA_INICIADA"
    accion_fin = "REINDEXACION_RESERVADOS_COMPLETA_FINALIZADA" if force else "INDEXACION_RESERVADOS_RAPIDA_FINALIZADA"
    try:
        _set_reservados_index_job(job_id, {
            "estado": "preparando", "modo": modo, "total": 0, "procesados": 0,
            "sin_cambios": 0, "nuevos": 0, "actualizados": 0,
            "no_encontrados": 0, "errores": 0, "archivo_actual": "",
            "mensaje": "Preparando indexación...",
        })
        with get_db() as conn:
            carpeta = carpeta_reservados_actual(conn)
            registrar_auditoria(conn, accion_ini, usuario_id=admin_id, detalle={"job_id": job_id, "carpeta": str(carpeta)}, ip=ip, equipo=equipo, resultado="OK")

            def progress(data: dict[str, Any]) -> None:
                _set_reservados_index_job(job_id, data)

            stats = indexar_memorandos(conn, admin_id, progress_callback=progress, force=force, tabla="reservados", carpeta=carpeta)
            registrar_auditoria(conn, accion_fin, usuario_id=admin_id, detalle={"job_id": job_id, **stats}, ip=ip, equipo=equipo, resultado="OK")
        _set_reservados_index_job(job_id, {"estado": "finalizado", "mensaje": "Indexación finalizada."})
    except Exception as exc:
        logger.error("Error en indexación de reservados %s: %s\n%s", job_id, exc, traceback.format_exc())
        try:
            with get_db() as conn:
                registrar_auditoria(conn, "ERROR_INDEXACION_RESERVADOS", usuario_id=admin_id, detalle={"job_id": job_id, "error": str(exc)[:500]}, ip=ip, equipo=equipo, resultado="ERROR")
        except Exception:
            pass
        _set_reservados_index_job(job_id, {"estado": "error", "mensaje": str(exc)[:500]})
```

- [ ] **Step 4: Agregar las 3 rutas de indexación de Reservados**

Después de la ruta `@app.get("/admin/reindexar/estado/{job_id}")` (termina en `return JSONResponse({"job_id": job_id, **job})`), agregar:

```python


# ── RESERVADOS — INDEXACIÓN (ADMIN) ───────────────────────────────

@app.get("/admin/reservados/reindexar", response_class=HTMLResponse)
def admin_reservados_reindexar_get(request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        reservados_dir = get_config(conn, "reservados_dir", str(carpeta_reservados_actual(conn)))
        n_reservados = conn.execute("SELECT COUNT(*) FROM reservados WHERE activo = 1").fetchone()[0]
        last = conn.execute("SELECT * FROM auditoria WHERE accion IN ('INDEXACION_RESERVADOS_RAPIDA_FINALIZADA','REINDEXACION_RESERVADOS_COMPLETA_FINALIZADA') ORDER BY fecha_hora DESC LIMIT 1").fetchone()
    return templates.TemplateResponse("reservados_reindexar.html", {"request": request, "user": user, "reservados_dir": reservados_dir, "n_reservados": n_reservados, "ultima": dict(last) if last else None})


@app.post("/admin/reservados/reindexar/iniciar")
def admin_reservados_reindexar_iniciar(request: Request, user: dict = Depends(require_admin), modo: str = Query("rapida")):
    force = modo == "completa"
    with RESERVADOS_INDEX_JOBS_LOCK:
        for existing_id, job in RESERVADOS_INDEX_JOBS.items():
            if job.get("estado") in {"preparando", "ejecutando"}:
                return JSONResponse({"job_id": existing_id, **dict(job)})
    job_id = uuid.uuid4().hex[:12]
    _set_reservados_index_job(job_id, {"estado": "preparando", "modo": "completa" if force else "rapida", "total": 0, "procesados": 0, "sin_cambios": 0, "nuevos": 0, "actualizados": 0, "no_encontrados": 0, "errores": 0, "archivo_actual": "", "mensaje": "Iniciando tarea..."})
    t = threading.Thread(target=_index_reservados_worker, args=(job_id, user["id"], client_ip(request), ua(request), force), daemon=True)
    t.start()
    return JSONResponse({"job_id": job_id, **_get_reservados_index_job(job_id)})


@app.get("/admin/reservados/reindexar/estado/{job_id}")
def admin_reservados_reindexar_estado(job_id: str, request: Request, user: dict = Depends(require_admin)):
    job = _get_reservados_index_job(job_id)
    if not job:
        return JSONResponse({"estado": "no_encontrado", "mensaje": "Tarea no encontrada."}, status_code=404)
    return JSONResponse({"job_id": job_id, **job})
```

- [ ] **Step 5: Crear `templates/reservados_reindexar.html`**

```html
{% extends "base.html" %}

{% block title %}Indexación de Reservados · SIGEMEP{% endblock %}
{% block page_title %}Indexación de Reservados{% endblock %}

{% block content %}

<section class="index-hero">
    <div>
        <span class="section-tag">
            <i class="fa-solid fa-lock"></i>
            Base reservada
        </span>

        <h1>Indexación de Reservados</h1>

        <p>
            Actualización rápida y reindexación completa de la carpeta de informes reservados.
        </p>
    </div>

    <a href="/dashboard/admin" class="btn btn-outline">
        <i class="fa-solid fa-arrow-left"></i>
        Volver al panel
    </a>
</section>

<section class="index-grid">

    <article class="index-card">
        <div class="index-card-head">
            <div class="index-icon">
                <i class="fa-solid fa-folder-open"></i>
            </div>

            <div>
                <h2>Carpeta de Reservados</h2>
                <p>Ruta donde SIGEMEP busca los informes reservados.</p>
            </div>
        </div>

        <div class="index-current-path">
            <i class="fa-solid fa-circle-info"></i>
            <span>Ruta definida:</span>
            <strong>{{ reservados_dir or 'No configurada' }}</strong>
        </div>

        {% if ultima %}
        <div class="index-last-run">
            <i class="fa-solid fa-clock-rotate-left"></i>
            <div>
                <span>Última indexación</span>
                <strong>{{ ultima.fecha_hora or ultima.creado_en or '—' }}</strong>
            </div>
        </div>
        {% endif %}
    </article>

    <article class="index-card index-count-card">
        <div class="index-count-icon">
            <i class="fa-solid fa-database"></i>
        </div>

        <div>
            <strong>{{ n_reservados or 0 }}</strong>
            <span>Indexados activos</span>
            <p>Informes reservados disponibles para búsqueda.</p>
        </div>
    </article>

</section>

<section class="index-card">
    <div class="index-card-head">
        <div class="index-icon">
            <i class="fa-solid fa-rotate"></i>
        </div>

        <div>
            <h2>Actualizar base de Reservados</h2>
            <p>
                La actualización rápida procesa PDFs nuevos o modificados. La reindexación completa reprocesa toda la carpeta.
            </p>
        </div>
    </div>

    <div class="index-actions">

        <form method="post" action="/admin/reservados/reindexar/iniciar?modo=rapida">
            <button id="btn-fast" type="submit" class="btn btn-primary">
                <i class="fa-solid fa-bolt"></i>
                Actualización rápida
            </button>
        </form>

        <form method="post" action="/admin/reservados/reindexar/iniciar?modo=completa">
            <button id="btn-full" type="submit" class="btn btn-danger">
                <i class="fa-solid fa-arrows-rotate"></i>
                Reindexación completa
            </button>
        </form>

    </div>

    <div class="index-progress-panel">
        <div class="index-progress-top">
            <div>
                <strong id="idx-estado">Preparado para indexar</strong>
                <span id="idx-porcentaje">0%</span>
            </div>
        </div>

        <div class="index-progress-bar">
            <div id="idx-barra" style="width: 0%;"></div>
        </div>

        <div class="index-progress-grid">
            <div>
                <span>Total</span>
                <strong id="idx-total">0</strong>
            </div>

            <div>
                <span>Procesados</span>
                <strong id="idx-procesados">0</strong>
            </div>

            <div>
                <span>Sin cambios</span>
                <strong id="idx-sin-cambios">0</strong>
            </div>

            <div>
                <span>Nuevos</span>
                <strong id="idx-nuevos">0</strong>
            </div>

            <div>
                <span>Actualizados</span>
                <strong id="idx-actualizados">0</strong>
            </div>

            <div>
                <span>Errores</span>
                <strong id="idx-errores">0</strong>
            </div>
        </div>

        <div class="index-current-file">
            <span>Archivo actual</span>
            <strong id="idx-archivo">—</strong>
        </div>
    </div>

    <div id="idx-mensaje" class="index-message" style="display: none;">
        <i class="fa-solid fa-circle-info"></i>
        <span></span>
    </div>

    <div class="index-warning">
        <i class="fa-solid fa-triangle-exclamation"></i>
        <span>No cierre esta ventana mientras se indexan los PDFs.</span>
    </div>
</section>

{% endblock %}

{% block scripts %}
<script>
(function () {
    const forms = document.querySelectorAll('.index-actions form');
    const btnFast = document.getElementById('btn-fast');
    const btnFull = document.getElementById('btn-full');

    const estado = document.getElementById('idx-estado');
    const porcentaje = document.getElementById('idx-porcentaje');
    const barra = document.getElementById('idx-barra');

    const total = document.getElementById('idx-total');
    const procesados = document.getElementById('idx-procesados');
    const sinCambios = document.getElementById('idx-sin-cambios');
    const nuevos = document.getElementById('idx-nuevos');
    const actualizados = document.getElementById('idx-actualizados');
    const errores = document.getElementById('idx-errores');
    const archivo = document.getElementById('idx-archivo');

    const mensajeBox = document.getElementById('idx-mensaje');
    const mensajeText = mensajeBox ? mensajeBox.querySelector('span') : null;

    function setMensaje(texto) {
        if (!mensajeBox || !mensajeText) return;
        mensajeText.textContent = texto || '';
        mensajeBox.style.display = texto ? 'flex' : 'none';
    }

    function setBotones(disabled) {
        if (btnFast) btnFast.disabled = disabled;
        if (btnFull) btnFull.disabled = disabled;
    }

    function val(data, keys, fallback) {
        for (const k of keys) {
            if (data && data[k] !== undefined && data[k] !== null) {
                return data[k];
            }
        }
        return fallback;
    }

    function renderEstado(data) {
        const pct = Number(val(data, ['porcentaje', 'percent', 'pct'], 0)) || 0;

        estado.textContent = val(data, ['estado', 'status', 'mensaje_estado'], 'Procesando...');
        porcentaje.textContent = pct + '%';
        barra.style.width = Math.max(0, Math.min(100, pct)) + '%';

        total.textContent = val(data, ['total'], 0);
        procesados.textContent = val(data, ['procesados', 'processed'], 0);
        sinCambios.textContent = val(data, ['sin_cambios', 'sinCambios', 'unchanged'], 0);
        nuevos.textContent = val(data, ['nuevos', 'new'], 0);
        actualizados.textContent = val(data, ['actualizados', 'updated'], 0);
        errores.textContent = val(data, ['errores', 'errors'], 0);
        archivo.textContent = val(data, ['archivo_actual', 'archivo', 'current_file'], '—');

        const msg = val(data, ['mensaje', 'message'], '');
        setMensaje(msg);

        const finalizado = Boolean(val(data, ['finalizado', 'done', 'terminado'], false));
        const status = String(val(data, ['estado', 'status'], '')).toLowerCase();

        if (
            finalizado ||
            status.includes('finalizado') ||
            status.includes('completado') ||
            status.includes('terminado') ||
            status.includes('error')
        ) {
            setBotones(false);
            return true;
        }

        return false;
    }

    async function poll(jobId) {
        try {
            const resp = await fetch('/admin/reservados/reindexar/estado/' + encodeURIComponent(jobId), {
                method: 'GET',
                headers: {
                    'Accept': 'application/json'
                }
            });

            if (!resp.ok) {
                setMensaje('No se pudo consultar el estado de la indexación.');
                setBotones(false);
                return;
            }

            const data = await resp.json();
            const finished = renderEstado(data);

            if (!finished) {
                setTimeout(function () {
                    poll(jobId);
                }, 1000);
            }
        } catch (e) {
            setMensaje('Error consultando el estado de indexación.');
            setBotones(false);
        }
    }

    forms.forEach(function (form) {
        form.addEventListener('submit', async function (event) {
            event.preventDefault();

            setBotones(true);
            setMensaje('');
            estado.textContent = 'Iniciando indexación...';
            porcentaje.textContent = '0%';
            barra.style.width = '0%';

            try {
                const resp = await fetch(form.action, {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json'
                    }
                });

                const contentType = resp.headers.get('content-type') || '';

                if (contentType.includes('application/json')) {
                    const data = await resp.json();

                    const jobId = val(data, ['job_id', 'jobId', 'id'], null);

                    if (jobId) {
                        estado.textContent = 'Indexación iniciada...';
                        poll(jobId);
                        return;
                    }

                    renderEstado(data);
                    setBotones(false);
                    return;
                }

                if (resp.redirected) {
                    window.location.href = resp.url;
                    return;
                }

                if (resp.ok) {
                    estado.textContent = 'Indexación iniciada.';
                    setMensaje('La indexación fue iniciada. Actualice la pantalla si no ve progreso.');
                    setBotones(false);
                    return;
                }

                setMensaje('No se pudo iniciar la indexación.');
                setBotones(false);

            } catch (e) {
                setMensaje('Error iniciando la indexación.');
                setBotones(false);
            }
        });
    });
})();
</script>
{% endblock %}
```

- [ ] **Step 6: Verificar que `app.py` carga y la ruta responde**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'C:/SIGEMEP_APP_DEV')
import app
assert hasattr(app, 'admin_reservados_reindexar_get')
assert hasattr(app, '_index_reservados_worker')
print('OK - rutas de indexacion de reservados definidas')
"
```

Esperado: `OK - rutas de indexacion de reservados definidas`.

- [ ] **Step 7: Probar el flujo de indexación en el navegador**

```powershell
Start-Process -NoNewWindow `
  -FilePath "C:\SIGEMEP_APP_DEV\venv\Scripts\uvicorn.exe" `
  -ArgumentList "app:app","--host","0.0.0.0","--port","8001" `
  -WorkingDirectory "C:\SIGEMEP_APP_DEV" `
  -RedirectStandardError "C:\SIGEMEP_APP_DEV\logs\dev_stderr.log"
Start-Sleep -Seconds 5
Get-Content "C:\SIGEMEP_APP_DEV\logs\dev_stderr.log"
```

Esperado: `Application startup complete.` sin errores. Luego, en el navegador: iniciar sesión como `admin`, abrir `http://localhost:8001/admin/reservados/reindexar`, hacer clic en "Actualización rápida" y verificar que la barra de progreso avanza y termina en "finalizado" (la carpeta `INFORMES RV` puede estar vacía; el resultado esperado en ese caso es `total: 0` sin errores).

Detener el servidor al finalizar:

```powershell
Get-Process | Where-Object { $_.Name -like "*uvicorn*" } | Stop-Process -Force
```

---

## Task 6: Búsqueda de Reservados (formulario + resultados)

**Files:**
- Modify: `app.py`
- Create: `templates/reservados_buscar.html`
- Create: `templates/reservados_resultados.html`

**Interfaces:**
- Consumes: `require_reservados` (Task 4), `buscar_memorandos(..., tabla="reservados")` (Task 3), `_sigemep_page_window` (ya existe en `app.py`).
- Produces: rutas `GET /reservados`, `POST /reservados`, `GET /reservados/resultados`.

- [ ] **Step 1: Agregar las rutas de búsqueda después de las rutas de indexación de Reservados**

En `app.py`, después de la ruta `admin_reservados_reindexar_estado` agregada en la Task 5, agregar:

```python


# ── RESERVADOS — BÚSQUEDA ──────────────────────────────────────────

@app.get("/reservados", response_class=HTMLResponse)
def reservados_get(request: Request, user: dict = Depends(require_reservados)):
    return templates.TemplateResponse("reservados_buscar.html", {"request": request, "user": user})


@app.post("/reservados")
def reservados_post(
    request: Request,
    q: str = Form(""),
    campo: str = Form("todo"),
    fecha_desde: str = Form(""),
    fecha_hasta: str = Form(""),
    paginas_min: int = Form(0),
    paginas_max: int = Form(0),
    user: dict = Depends(require_reservados),
):
    require_password_ok(request, user)
    q = (q or "").strip()
    hay_filtros = bool((campo and campo != "todo") or fecha_desde or fecha_hasta or paginas_min or paginas_max)
    if not q and not hay_filtros:
        return RedirectResponse("/reservados", status_code=302)
    params: dict[str, str] = {"q": q, "page": "1"}
    if campo and campo != "todo":
        params["campo"] = campo
    if fecha_desde:
        params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        params["fecha_hasta"] = fecha_hasta
    if paginas_min:
        params["paginas_min"] = str(paginas_min)
    if paginas_max:
        params["paginas_max"] = str(paginas_max)
    return RedirectResponse(f"/reservados/resultados?{urlencode(params)}", status_code=302)


@app.get("/reservados/resultados", response_class=HTMLResponse)
def reservados_resultados_get(
    request: Request,
    user: dict = Depends(require_reservados),
    q: str = Query(""),
    page: int = Query(1),
    campo: str = Query("todo"),
    fecha_desde: str = Query(""),
    fecha_hasta: str = Query(""),
    paginas_min: int = Query(0),
    paginas_max: int = Query(0),
):
    require_password_ok(request, user)
    q = (q or "").strip()
    hay_filtros = bool((campo and campo != "todo") or fecha_desde or fecha_hasta or paginas_min or paginas_max)
    if not q and not hay_filtros:
        return RedirectResponse("/reservados", status_code=302)

    page_size = 20
    page = max(1, int(page or 1))

    todos = list(buscar_memorandos(
        q,
        limit=5000,
        campo=campo or "todo",
        fecha_desde=fecha_desde or "",
        fecha_hasta=fecha_hasta or "",
        paginas_min=paginas_min or 0,
        paginas_max=paginas_max or 0,
        tabla="reservados",
    ))

    total = len(todos)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    resultados = todos[start:start + page_size]

    if page == 1:
        try:
            with get_db() as conn:
                registrar_busqueda(conn, user["id"], q, total, client_ip(request))
                registrar_auditoria(conn, "RESERVADO_BUSQUEDA_REALIZADA", usuario_id=user["id"], detalle={"q": q, "cantidad_resultados": total, "paginacion": True}, ip=client_ip(request), equipo=ua(request), resultado="OK")
        except Exception:
            pass

    filtros_params: dict[str, str] = {}
    if campo and campo != "todo":
        filtros_params["campo"] = campo
    if fecha_desde:
        filtros_params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        filtros_params["fecha_hasta"] = fecha_hasta
    if paginas_min:
        filtros_params["paginas_min"] = str(paginas_min)
    if paginas_max:
        filtros_params["paginas_max"] = str(paginas_max)
    filtros_url = ("&" + urlencode(filtros_params)) if filtros_params else ""

    return templates.TemplateResponse("reservados_resultados.html", {
        "request": request, "user": user, "q": q,
        "resultados": resultados, "total": total, "page": page,
        "page_size": page_size, "total_pages": total_pages,
        "page_numbers": _sigemep_page_window(page, total_pages),
        "campo": campo or "todo",
        "fecha_desde": fecha_desde or "",
        "fecha_hasta": fecha_hasta or "",
        "paginas_min": paginas_min or 0,
        "paginas_max": paginas_max or 0,
        "filtros_url": filtros_url,
        "filtros_activos": bool(filtros_params),
    })
```

- [ ] **Step 2: Crear `templates/reservados_buscar.html`**

Primero copiar el archivo tal cual:

```powershell
Copy-Item "C:\SIGEMEP_APP_DEV\templates\buscar.html" "C:\SIGEMEP_APP_DEV\templates\reservados_buscar.html"
```

Luego, en `templates/reservados_buscar.html`, aplicar estos cuatro cambios de texto (cada uno aparece una sola vez en el archivo):

1. `{% block title %}Buscar memorandos · SIGEMEP{% endblock %}` → `{% block title %}Buscar reservados · SIGEMEP{% endblock %}`
2. `{% block page_title %}Buscar memorandos{% endblock %}` → `{% block page_title %}Buscar reservados{% endblock %}`
3. `<form id="buscar-form" method="post" action="/buscar" class="buscar-form" autocomplete="off">` → `<form id="buscar-form" method="post" action="/reservados" class="buscar-form" autocomplete="off">`
4. `<h1>Buscar <span>memorandos</span></h1>` → `<h1>Buscar <span>reservados</span></h1>`

El resto del archivo (estilos, filtros avanzados, script) queda idéntico — son genéricos y no mencionan "memorandos" en ningún otro lugar funcional.

- [ ] **Step 3: Crear `templates/reservados_resultados.html`**

Primero copiar el archivo tal cual:

```powershell
Copy-Item "C:\SIGEMEP_APP_DEV\templates\resultados.html" "C:\SIGEMEP_APP_DEV\templates\reservados_resultados.html"
```

Luego, en `templates/reservados_resultados.html`, aplicar estos cambios:

1. Línea con `<form method="post" action="/buscar" class="resultados-search">` → `<form method="post" action="/reservados" class="resultados-search">`
2. La línea de acciones por resultado:

```html
<div class="result-actions"><a href="/ver_primera_hoja/{{ r.id }}" class="result-btn"><i class="fa-solid fa-eye"></i> Primera hoja</a>{% if puede_descargar %}<a href="/ver_pdf_completo/{{ r.id }}" class="result-btn primary"><i class="fa-solid fa-file-pdf"></i> Ver PDF completo</a>{% endif %}</div>
```

por:

```html
<div class="result-actions"><a href="/reservados/visor/{{ r.id }}" class="result-btn primary"><i class="fa-solid fa-eye"></i> Visualizar</a></div>
```

3. Reemplazar **todas** las apariciones (son 4: una en el enlace "Limpiar filtros" y tres en el bloque `{% if total_pages > 1 %}` — Anterior, números de página y Siguiente) de la subcadena literal `/buscar/resultados?q=` por `/reservados/resultados?q=`.

- [ ] **Step 4: Verificar que `app.py` carga sin errores**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'C:/SIGEMEP_APP_DEV')
import app
assert hasattr(app, 'reservados_resultados_get')
print('OK - rutas de busqueda de reservados definidas')
"
```

Esperado: `OK - rutas de busqueda de reservados definidas`.

- [ ] **Step 5: Probar la búsqueda en el navegador con un usuario sin permiso y con ADMIN**

Con el servidor corriendo (igual que en la Task 5, Step 7):

1. Iniciar sesión con un usuario BRIGADA o JEFE que **no** tenga `permiso_reservados` → ir a `http://localhost:8001/reservados` directamente → debe redirigir a `/acceso_denegado`.
2. Iniciar sesión como `admin` → ir a `http://localhost:8001/reservados` → debe cargar el formulario sin redirigir (ADMIN tiene bypass).
3. Si la carpeta `INFORMES RV` tiene al menos un PDF ya indexado (Task 5), buscar una palabra que aparezca en ese archivo y verificar que aparece en los resultados con el botón "Visualizar".

---

## Task 7: Visor seguro de Reservados

**Files:**
- Modify: `app.py`
- Create: `templates/reservados_visor.html`

**Interfaces:**
- Consumes: `ruta_absoluta_segura_reservados`, `imagen_pagina_con_marca` (Task 2); `require_reservados` (Task 4).
- Produces: rutas `GET /reservados/visor/{id}`, `GET /reservados/visor/{id}/imagen/{n}`.

- [ ] **Step 1: Agregar las rutas del visor después de las rutas de búsqueda**

En `app.py`, después de la ruta `reservados_resultados_get` agregada en la Task 6, agregar:

```python


# ── RESERVADOS — VISOR SEGURO ──────────────────────────────────────

@app.get("/reservados/visor/{mem_id}", response_class=HTMLResponse)
def reservados_visor(mem_id: int, request: Request, user: dict = Depends(require_reservados), pagina: int = Query(1)):
    require_password_ok(request, user)
    with get_db() as conn:
        m = conn.execute("SELECT * FROM reservados WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
        if not m:
            raise HTTPException(404)
        total_paginas = m["cantidad_paginas"] or 1
        pagina = max(1, min(pagina, total_paginas))
        registrar_auditoria(
            conn,
            "RESERVADO_VISOR_ABIERTO",
            usuario_id=user["id"],
            memorando_id=mem_id,
            detalle={"archivo": m["nombre_archivo"]},
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )
        memorando = dict(m)
    return templates.TemplateResponse("reservados_visor.html", {
        "request": request,
        "user": user,
        "memorando": memorando,
        "pagina": pagina,
        "total_paginas": total_paginas,
    })


@app.get("/reservados/visor/{mem_id}/imagen/{n}")
def reservados_visor_imagen(mem_id: int, n: int, request: Request, user: dict = Depends(require_reservados)):
    with get_db() as conn:
        m = conn.execute("SELECT * FROM reservados WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
        if not m:
            raise HTTPException(404)
        total_paginas = m["cantidad_paginas"] or 1
        if n < 1 or n > total_paginas:
            raise HTTPException(404)
        ruta = ruta_absoluta_segura_reservados(m["ruta_archivo"], conn)
        if not ruta:
            raise HTTPException(404)
        preview = Path(m["primera_hoja_img"]) if m["primera_hoja_img"] else None
        img_bytes = imagen_pagina_con_marca(ruta, n - 1, preview, user["usuario"], user["rol"], client_ip(request))
    return Response(img_bytes, media_type="image/png")
```

- [ ] **Step 2: Crear `templates/reservados_visor.html`**

```html
{% extends "base.html" %}

{% block title %}{{ memorando.nombre_archivo }} · Reservados · SIGEMEP{% endblock %}
{% block page_title %}Visor de Reservados{% endblock %}

{% block head %}
<style>
.rv-hero { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
.rv-hero h1 { margin: 8px 0 0; color: #fff; font-size: 22px; font-weight: 950; word-break: break-word; }
.rv-hero p { margin: 6px 0 0; color: #9ca3af; font-size: 12.5px; }
.rv-frame { background: linear-gradient(145deg, rgba(18,24,28,.96), rgba(7,10,12,.99)); border: 1px solid rgba(255,255,255,.10); border-radius: 22px; padding: 18px; text-align: center; }
.rv-frame img { max-width: 100%; border-radius: 10px; user-select: none; -webkit-user-select: none; }
.rv-nav { display: flex; align-items: center; justify-content: center; gap: 14px; margin-top: 16px; }
.rv-warning { margin-top: 16px; border-radius: 16px; padding: 12px 16px; background: rgba(255,115,0,.10); border: 1px solid rgba(255,115,0,.28); color: #ffcfaa; font-size: 12.5px; display: flex; gap: 10px; align-items: center; }
</style>
{% endblock %}

{% block content %}
<section class="rv-hero">
    <div>
        <span class="section-tag"><i class="fa-solid fa-lock"></i> Documento reservado</span>
        <h1>{{ memorando.nombre_archivo }}</h1>
        <p>Página {{ pagina }} de {{ total_paginas }}</p>
    </div>
    <a href="javascript:history.back()" class="btn btn-outline">
        <i class="fa-solid fa-arrow-left"></i> Volver
    </a>
</section>

<section class="rv-frame">
    <img src="/reservados/visor/{{ memorando.id }}/imagen/{{ pagina }}" alt="Página {{ pagina }}" draggable="false">

    <div class="rv-nav">
        {% if pagina > 1 %}
        <a class="page-btn" href="/reservados/visor/{{ memorando.id }}?pagina={{ pagina - 1 }}"><i class="fa-solid fa-chevron-left"></i> Anterior</a>
        {% endif %}
        <span class="page-info">Página {{ pagina }} de {{ total_paginas }}</span>
        {% if pagina < total_paginas %}
        <a class="page-btn" href="/reservados/visor/{{ memorando.id }}?pagina={{ pagina + 1 }}">Siguiente <i class="fa-solid fa-chevron-right"></i></a>
        {% endif %}
    </div>
</section>

<div class="rv-warning">
    <i class="fa-solid fa-shield-halved"></i>
    <span>Esta imagen incluye marca de agua con usuario, rol, fecha, hora e IP. Toda visualización queda registrada. No está permitido descargar este documento.</span>
</div>
{% endblock %}
```

- [ ] **Step 3: Verificar que `app.py` carga sin errores**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'C:/SIGEMEP_APP_DEV')
import app
assert hasattr(app, 'reservados_visor')
assert hasattr(app, 'reservados_visor_imagen')
print('OK - rutas del visor de reservados definidas')
"
```

Esperado: `OK - rutas del visor de reservados definidas`.

- [ ] **Step 4: Probar el visor en el navegador**

Con el servidor corriendo y al menos un PDF indexado en `reservados` (de la Task 5):

1. Como `admin`, ir a `/reservados/resultados?q=<palabra>` y hacer clic en "Visualizar".
2. Verificar que se muestra la imagen de la página 1 con marca de agua visible (usuario, rol, fecha, hora, IP).
3. Si el documento tiene más de una página, verificar que "Siguiente"/"Anterior" navegan correctamente y la imagen cambia.
4. Hacer clic derecho sobre la imagen → confirmar que el menú contextual no aparece (lo bloquea `security.js`, cargado globalmente desde `base.html`).
5. Verificar en `/admin/auditoria` que aparece un registro `RESERVADO_VISOR_ABIERTO` con el nombre del archivo.

---

## Task 8: Gestión del permiso por usuario

**Files:**
- Modify: `app.py`
- Modify: `templates/usuarios.html`
- Modify: `templates/base.html`

**Interfaces:**
- Produces: ruta `POST /admin/usuario/{uid}/permiso_reservados`.

- [ ] **Step 1: Agregar la ruta de toggle de permiso**

En `app.py`, dentro de la función `admin_cambiar_rol`, localizar este bloque exacto (es el final de la función, justo antes de `@app.post("/admin/usuario/{uid}/password_temp")`):

```python
        else:
            request.session["sigemep_flash"] = "El usuario ya tenía ese rol."

    return RedirectResponse("/admin/usuarios", status_code=302)



@app.post("/admin/usuario/{uid}/password_temp")
```

y reemplazarlo por (agrega la ruta nueva entre el `return` de `admin_cambiar_rol` y la ruta `password_temp`):

```python
        else:
            request.session["sigemep_flash"] = "El usuario ya tenía ese rol."

    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/permiso_reservados")
def admin_usuario_permiso_reservados(uid: int, request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        row = conn.execute("SELECT usuario, permiso_reservados FROM usuarios WHERE id = ?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404)
        nuevo_valor = 0 if row["permiso_reservados"] else 1
        conn.execute("UPDATE usuarios SET permiso_reservados = ? WHERE id = ?", (nuevo_valor, uid))
        registrar_auditoria(
            conn,
            "PERMISO_RESERVADOS_OTORGADO" if nuevo_valor else "PERMISO_RESERVADOS_REVOCADO",
            usuario_id=user["id"],
            detalle={"usuario_afectado": row["usuario"], "uid": uid},
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )
    request.session["sigemep_flash"] = "Permiso de Reservados actualizado."
    return RedirectResponse("/admin/usuarios", status_code=302)
```

- [ ] **Step 2: Agregar la columna "Reservados" en `templates/usuarios.html`**

Reemplazar el encabezado de la tabla:

```html
<section class="users-card"><table class="users-table"><thead><tr><th>ID</th><th>Usuario</th><th>Nombre</th><th>DNI / Legajo</th><th>Rol</th><th>Estado</th><th>Último login</th><th>Cambiar clave</th><th>Acciones</th></tr></thead><tbody>
```

por:

```html
<section class="users-card"><table class="users-table"><thead><tr><th>ID</th><th>Usuario</th><th>Nombre</th><th>DNI / Legajo</th><th>Rol</th><th>Estado</th><th>Último login</th><th>Cambiar clave</th><th>Reservados</th><th>Acciones</th></tr></thead><tbody>
```

Y agregar la celda nueva, justo antes de la celda de Acciones. Reemplazar:

```html
<td>{{ u.ultimo_login or '—' }}</td><td>{% if u.debe_cambiar_password %}<span class="pw-badge pw-yes">Sí</span>{% else %}<span class="pw-badge pw-no">No</span>{% endif %}</td>
<td><div class="user-actions">
```

por:

```html
<td>{{ u.ultimo_login or '—' }}</td><td>{% if u.debe_cambiar_password %}<span class="pw-badge pw-yes">Sí</span>{% else %}<span class="pw-badge pw-no">No</span>{% endif %}</td>
<td>{% if u.usuario == 'admin' %}<span class="pw-badge pw-no">Automático</span>{% else %}<form method="post" action="/admin/usuario/{{ u.id }}/permiso_reservados"><button type="submit" class="mini-btn {{ 'mini-green' if u.permiso_reservados else '' }}"><i class="fa-solid {{ 'fa-lock-open' if u.permiso_reservados else 'fa-lock' }}"></i> {{ 'Sí' if u.permiso_reservados else 'No' }}</button></form>{% endif %}</td>
<td><div class="user-actions">
```

Y actualizar el `colspan` de la fila vacía:

```html
{% else %}<tr><td colspan="9">
```

por:

```html
{% else %}<tr><td colspan="10">
```

- [ ] **Step 3: Agregar el ítem de navegación "Reservados" en `templates/base.html`**

Reemplazar:

```html
{% endif %}
<a href="/cambiar_password" class="nav-item"><i class="fa-solid fa-key"></i><span>Cambiar contraseña</span></a>
</nav>
```

por:

```html
{% endif %}
{% if user.rol == 'ADMIN' or user.permiso_reservados %}
<a href="/reservados" class="nav-item"><i class="fa-solid fa-lock"></i><span>Reservados</span></a>
{% endif %}
<a href="/cambiar_password" class="nav-item"><i class="fa-solid fa-key"></i><span>Cambiar contraseña</span></a>
</nav>
```

- [ ] **Step 4: Verificar que `app.py` carga sin errores**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'C:/SIGEMEP_APP_DEV')
import app
assert hasattr(app, 'admin_usuario_permiso_reservados')
print('OK - ruta de toggle de permiso definida')
"
```

Esperado: `OK - ruta de toggle de permiso definida`.

- [ ] **Step 5: Probar el otorgamiento de permiso en el navegador**

Con el servidor corriendo:

1. Como `admin`, ir a `/admin/usuarios` → verificar que aparece la columna "Reservados" con un botón "No" para cada usuario no-admin.
2. Hacer clic en el botón "No" de un usuario BRIGADA o JEFE → verificar que cambia a "Sí" (botón verde) y que la página recarga con el flash "Permiso de Reservados actualizado."
3. Cerrar sesión e iniciar sesión como ese usuario → verificar que el ítem "Reservados" aparece en el menú lateral y que `/reservados` ya no redirige a `/acceso_denegado`.
4. Volver a `admin/usuarios` como admin y revocar el permiso → verificar que el usuario afectado pierde el acceso a `/reservados`.
5. Verificar en `/admin/auditoria` los registros `PERMISO_RESERVADOS_OTORGADO` y `PERMISO_RESERVADOS_REVOCADO`.

---

## Task 9: Verificación end-to-end completa

- [ ] **Step 1: Iniciar la app**

```powershell
Start-Process -NoNewWindow `
  -FilePath "C:\SIGEMEP_APP_DEV\venv\Scripts\uvicorn.exe" `
  -ArgumentList "app:app","--host","0.0.0.0","--port","8001" `
  -WorkingDirectory "C:\SIGEMEP_APP_DEV" `
  -RedirectStandardError "C:\SIGEMEP_APP_DEV\logs\dev_stderr.log"
Start-Sleep -Seconds 5
Get-Content "C:\SIGEMEP_APP_DEV\logs\dev_stderr.log"
```

Esperado: `Application startup complete.` sin errores.

- [ ] **Step 2: Verificar que las rutas de Reservados requieren autenticación**

```powershell
$r = Invoke-WebRequest "http://localhost:8001/reservados" -MaximumRedirection 0 -ErrorAction SilentlyContinue
"Status: $($r.StatusCode)"
```

Esperado: `Status: 303` (redirige a `/login`).

- [ ] **Step 3: Flujo completo como ADMIN**

1. Login como `admin` → `/admin/reservados/reindexar` → "Actualización rápida" → confirmar que termina sin errores.
2. `/reservados` → buscar un término presente en algún PDF de `INFORMES RV` → confirmar resultados.
3. "Visualizar" → confirmar imagen con marca de agua y navegación de páginas.
4. `/admin/usuarios` → otorgar permiso a un usuario BRIGADA de prueba.

- [ ] **Step 4: Flujo completo como usuario con permiso otorgado**

1. Login como ese usuario BRIGADA → confirmar que "Reservados" aparece en el menú.
2. Repetir búsqueda y visualización → confirmar que funciona igual que para ADMIN.
3. Ir a `/buscar` (memorandos) → confirmar que los archivos de `INFORMES RV` **no** aparecen en los resultados de memorandos, ni viceversa.

- [ ] **Step 5: Flujo como usuario sin permiso**

1. Login como un usuario BRIGADA o JEFE sin `permiso_reservados` → confirmar que "Reservados" **no** aparece en el menú.
2. Navegar manualmente a `/reservados` → confirmar redirección a `/acceso_denegado`.

- [ ] **Step 6: Verificar auditoría completa**

Como `admin` → `/admin/auditoria` → confirmar que aparecen registros de: `INDEXACION_RESERVADOS_RAPIDA_FINALIZADA`, `RESERVADO_BUSQUEDA_REALIZADA`, `RESERVADO_VISOR_ABIERTO`, `PERMISO_RESERVADOS_OTORGADO`, y el `ACCESO_DENEGADO` del Step 5.

- [ ] **Step 7: Detener la app**

```powershell
Get-Process | Where-Object { $_.Name -like "*uvicorn*" } | Stop-Process -Force
```
