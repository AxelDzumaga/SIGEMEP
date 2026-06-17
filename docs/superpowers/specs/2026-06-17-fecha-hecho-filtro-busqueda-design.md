# Filtro de fecha por fecha del hecho — Spec de Diseño

**Fecha:** 2026-06-17
**Estado:** Aprobado por usuario, pendiente de implementación

---

## Resumen

El filtro de fecha en "Filtros avanzados" de `/buscar` compara contra `fecha_indexado` (cuándo el sistema escaneó el PDF), no contra la fecha del hecho descripto en el memorando. Como la indexación arrancó recién en mayo de 2026, cualquier búsqueda por un rango de fechas anterior devuelve siempre cero resultados, sin importar qué memorandos existan. Este cambio agrega una columna `fecha_hecho`, extraída del nombre de archivo durante la indexación, y hace que el filtro de fecha compare contra esa columna en lugar de `fecha_indexado`.

---

## Contexto

Bug reportado por el usuario en producción (`C:\SIGEMEP_APP`): buscar del 20/01/2026 al 25/01/2026 no da resultados. Investigación confirmó que:

- `fecha_indexado` en producción va de 2026-05-16 a 2026-06-17 (hoy) — ningún archivo puede tener una fecha de indexación anterior a mayo.
- La fecha real del hecho está codificada en el nombre de archivo en el 97,3% de los 9800 documentos de producción, bajo dos convenciones:
  - `YYYY-MM-DD` en cualquier posición del nombre (89,7% — incluye archivos `GA-YYYY-MM-DD_...` con la fecha al inicio, repetida varias veces)
  - `DD-MM-YYYY` al final del nombre, cuando no hay coincidencia `YYYY-MM-DD` (7,6%)
  - El 2,7% restante no tiene ningún patrón reconocible (muchos dicen literalmente `SIN-FECHA` o tienen el nombre incompleto)
- `app.py` y 4 archivos de `services/` en producción ya fueron reemplazados hoy a las 08:38 por las versiones de DEV (existen backups `.bak_antes_dev_20260617_083859`), lo que introdujo por primera vez la UI de "filtros avanzados" en producción. El problema de semántica de fecha ya existía en el diseño de DEV — no es una regresión de esa copia.

---

## Alcance

### Incluido
- Función `extraer_fecha_hecho(nombre_archivo) -> str | None` en `services/pdf_service.py`
- Columna nueva `memorandos.fecha_hecho TEXT` (formato `YYYY-MM-DD`)
- Migración idempotente vía `_add_column_if_missing` en `services/db.py`
- Backfill automático: la columna se completa sola la próxima vez que se indexe (incluida la rama "sin cambios"), sin reabrir ningún PDF
- Cambio del filtro de fecha en `services/search_service.py` para comparar `fecha_hecho` en lugar de `fecha_indexado`
- Replicación manual a `C:\SIGEMEP_APP` después de validar en DEV (copiar `services/pdf_service.py` y `services/db.py`, con backup previo)

### Excluido
- Extracción de fecha desde el texto del PDF (descartado: el nombre de archivo ya cubre 97,3%, no justifica el esfuerzo adicional)
- Cambios en la tabla `reservados` (este bug es específico de memorandos; Reservados es una feature nueva sin este problema todavía)
- Opción en la UI para elegir entre "fecha del hecho" y "fecha de indexación" — el filtro pasa a tener un solo significado
- Reprocesar o volver a renderizar los PDFs existentes — el backfill es puramente textual sobre `nombre_archivo`, ya guardado en la base

---

## Extracción de fecha

```python
import re
from datetime import datetime
from typing import Optional

_RE_ISO = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)')
_RE_DMY = re.compile(r'(?<!\d)(\d{2})-(\d{2})-(\d{4})(?!\d)')


def extraer_fecha_hecho(nombre_archivo: str) -> Optional[str]:
    """Extrae la fecha del hecho del nombre de archivo, si existe.

    Intenta primero YYYY-MM-DD (cubre ~90% de los casos, incluye los
    archivos con fecha al inicio del nombre tipo GA-YYYY-MM-DD_...).
    Si no encuentra, intenta DD-MM-YYYY. Devuelve None si no hay
    ningún patrón válido o la fecha extraída no es una fecha real
    (ej. mes 13).
    """
    m = _RE_ISO.search(nombre_archivo)
    if m:
        anio, mes, dia = m.groups()
    else:
        m = _RE_DMY.search(nombre_archivo)
        if not m:
            return None
        dia, mes, anio = m.groups()

    try:
        return datetime(int(anio), int(mes), int(dia)).strftime("%Y-%m-%d")
    except ValueError:
        return None
```

**Nota de implementación:** los límites `(?<!\d)`/`(?!\d)` (lookaround, no `\b`) son necesarios porque `\b` no genera límite entre un dígito y un guion bajo (`_`) en regex de Python — y varios nombres de archivo reales tienen la fecha pegada a un `_` (ej. `GA-2014-08-28_2014-08-28_...`). Usar `\b` ahí causa que esos archivos no matcheen.

---

## Modelo de datos

### Columna nueva en `memorandos`

```sql
ALTER TABLE memorandos ADD COLUMN fecha_hecho TEXT;
```

Vía `_add_column_if_missing(conn, "memorandos", "fecha_hecho", "TEXT")` en `init_db()`, junto a las demás columnas incrementales (`tamano_archivo`, `fecha_modificacion_archivo`, etc.).

### Backfill automático sin reprocesar PDFs

En `services/pdf_service.py`, dentro de `indexar_memorandos`, las tres ramas del bucle (nuevo / actualizado / sin cambios) calculan `fecha_hecho = extraer_fecha_hecho(abs_path.name)` y lo persisten:

- **Sin cambios** (hoy solo hace `UPDATE {tabla} SET activo = 1 WHERE id = ?`): se agrega `fecha_hecho = ?` al UPDATE. Esto es lo que permite que el campo se complete para los 9800 registros existentes la próxima vez que se haga clic en "Actualización rápida" — sin volver a abrir ningún PDF, porque `abs_path.name` ya está disponible en cada iteración del `os.walk` sin necesidad de re-extraer texto.
- **Actualizado**: se agrega `fecha_hecho = ?` al UPDATE existente (junto con `texto_extraido`, `cantidad_paginas`, etc.).
- **Nuevo**: se agrega `fecha_hecho` al INSERT existente.

Esta función solo aplica a la tabla `memorandos` (no a `reservados`, fuera de alcance de este cambio).

---

## Cambio en la búsqueda

En `services/search_service.py` hay 4 apariciones de `date(fecha_indexado)` repartidas en 2 funciones — 2 en `_buscar_solo_filtros` (cuando no hay texto de búsqueda, solo filtros) y 2 en `buscar_memorandos` (rama FTS, cuando sí hay texto). Las 4 cambian la condición:

```sql
-- Antes
date(fecha_indexado) >= date(?)
date(fecha_indexado) <= date(?)

-- Después
date(fecha_hecho) >= date(?)
date(fecha_hecho) <= date(?)
```

Sin cambios en la firma de las funciones ni en los parámetros que reciben desde `app.py` (`fecha_desde`, `fecha_hasta` siguen llamándose igual) — solo cambia qué columna de la tabla se compara.

**Comportamiento con `fecha_hecho` nulo:** un memorando sin fecha extraíble del nombre simplemente no aparece en resultados filtrados por fecha (la comparación SQL contra `NULL` no es verdadera), igual que hoy con cualquier filtro sobre un dato ausente. No hay mensaje de error ni caso especial.

---

## Replicación a producción

Después de validar en `C:\SIGEMEP_APP_DEV` (incluyendo correr "Actualización rápida" sobre la base de datos de DEV y confirmar que `fecha_hecho` se completa y que el filtro de fecha encuentra resultados):

1. Backup de los 2 archivos de producción que cambian, con el mismo patrón de nombre ya usado hoy:
   - `C:\SIGEMEP_APP\services\pdf_service.py` → `pdf_service.py.bak_antes_fecha_hecho_<timestamp>`
   - `C:\SIGEMEP_APP\services\db.py` → `db.py.bak_antes_fecha_hecho_<timestamp>`
2. Copiar los archivos actualizados de DEV a producción (no se crea ningún script `fix_*.py`/`aplicar_*.py` nuevo).
3. Reiniciar el servidor de producción (para que `init_db()` corra la migración `_add_column_if_missing`).
4. Como ADMIN, hacer clic en "Actualización rápida" en `/admin/reindexar` — esto completa `fecha_hecho` en los 9800 registros existentes sin reabrir ningún PDF.
5. Confirmar que la búsqueda del 20/01/2026 al 25/01/2026 (o cualquier rango con datos reales) ahora devuelve resultados.

---

## Manejo de errores

| Situación | Comportamiento |
|---|---|
| Nombre de archivo sin patrón de fecha reconocible | `fecha_hecho = NULL`; el documento no aparece en búsquedas filtradas por fecha |
| Fecha con mes/día inválido (ej. `99-99-2020`) | `extraer_fecha_hecho` devuelve `None` vía el `try/except ValueError` de `datetime(...)` |
| Archivo con ambos patrones (`YYYY-MM-DD` y `DD-MM-YYYY`) en el nombre | Se prioriza siempre `YYYY-MM-DD` (no ambiguo, más común) |

---

## Dependencias

Sin dependencias nuevas. Usa exclusivamente `re` y `datetime` de la librería estándar, ya importados en archivos cercanos.
