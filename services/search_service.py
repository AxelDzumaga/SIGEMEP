"""Búsqueda de memorandos con FTS5 (fallback a búsqueda en Python)."""
import re
from typing import Any

from services.db import get_db


_TABLAS_FTS = {
    "memorandos": "memorandos_fts",
    "reservados": "reservados_fts",
}


def _validar_tabla(tabla: str) -> str:
    if tabla not in _TABLAS_FTS:
        raise ValueError(f"Tabla de búsqueda no permitida: {tabla}")
    return tabla


def _preparar_query_fts(texto: str) -> str:
    """Elimina caracteres especiales de FTS5 y devuelve query limpia."""
    texto = re.sub(r'["()*^]', ' ', texto)
    return ' '.join(texto.split())


def _extraer_snippet(texto: str, query: str, max_len: int = 160) -> str:
    """Extrae un fragmento del texto alrededor del primer término encontrado."""
    if not texto:
        return ""
    texto_lower = texto.lower()
    for tok in query.lower().split():
        idx = texto_lower.find(tok)
        if idx != -1:
            start = max(0, idx - 60)
            return texto[start: start + max_len].replace("\n", " ")
    return texto[:max_len].replace("\n", " ")


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
    params: list[Any] = []
    if fecha_desde:
        conditions.append("date(fecha_indexado) >= date(?)")
        params.append(fecha_desde)
    if fecha_hasta:
        conditions.append("date(fecha_indexado) <= date(?)")
        params.append(fecha_hasta)
    if paginas_min > 0:
        conditions.append("cantidad_paginas >= ?")
        params.append(paginas_min)
    if paginas_max > 0:
        conditions.append("cantidad_paginas <= ?")
        params.append(paginas_max)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM {tabla} WHERE {' AND '.join(conditions)} ORDER BY fecha_indexado DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


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

    fts_q = _preparar_query_fts(q)
    if not fts_q:
        return []

    if campo == "nombre":
        fts_q = f"nombre_archivo : ({fts_q})"
    elif campo == "texto":
        fts_q = f"texto_extraido : ({fts_q})"

    with get_db() as conn:
        try:
            fts_rows = conn.execute(
                f"SELECT rowid FROM {tabla_fts} WHERE {tabla_fts} MATCH ? ORDER BY rank LIMIT 5000",
                (fts_q,),
            ).fetchall()

            if not fts_rows:
                return []

            ids = [r[0] for r in fts_rows]
            placeholders = ",".join("?" * len(ids))

            conditions = [f"id IN ({placeholders})", "activo = 1"]
            params: list[Any] = list(ids)

            if fecha_desde:
                conditions.append("date(fecha_indexado) >= date(?)")
                params.append(fecha_desde)
            if fecha_hasta:
                conditions.append("date(fecha_indexado) <= date(?)")
                params.append(fecha_hasta)
            if paginas_min > 0:
                conditions.append("cantidad_paginas >= ?")
                params.append(paginas_min)
            if paginas_max > 0:
                conditions.append("cantidad_paginas <= ?")
                params.append(paginas_max)

            mem_rows = conn.execute(
                f"SELECT * FROM {tabla} WHERE {' AND '.join(conditions)}",
                params,
            ).fetchall()

            mem_by_id = {r["id"]: dict(r) for r in mem_rows}
            resultados = []
            for rid in ids:
                if rid in mem_by_id:
                    d = mem_by_id[rid]
                    d["snippet"] = _extraer_snippet(d.get("texto_extraido", ""), q)
                    resultados.append(d)
            return resultados[:limit]

        except Exception:
            return _buscar_fallback(q, limit, tabla=tabla)


def _buscar_fallback(query: str, limit: int, tabla: str = "memorandos") -> list[dict[str, Any]]:
    """Búsqueda en Python si FTS5 no está disponible."""
    tabla = _validar_tabla(tabla)
    q_lower = query.lower()
    tokens = query.split()
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM {tabla} WHERE activo = 1").fetchall()
    resultados = []
    for row in rows:
        d = dict(row)
        nombre = (d.get("nombre_archivo") or "").lower()
        texto = (d.get("texto_extraido") or "").lower()
        score = 0
        snippet = ""
        if q_lower in nombre:
            score += 5
            snippet = d.get("nombre_archivo", "")
        if q_lower in texto:
            score += 3
            idx = texto.find(q_lower)
            start = max(0, idx - 60)
            snippet = (d.get("texto_extraido") or "")[start: start + 160]
        for tok in tokens:
            tl = tok.lower()
            if tl in nombre:
                score += 2
            if tl in texto:
                score += 1
        if score > 0:
            d["score"] = score
            d["snippet"] = snippet.replace("\n", " ")
            resultados.append(d)
    resultados.sort(key=lambda x: (-x["score"], x.get("nombre_archivo", "")))
    return resultados[:limit]
