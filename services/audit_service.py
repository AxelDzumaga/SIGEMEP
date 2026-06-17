"""Registro de auditoría y búsquedas."""
import json
from typing import Any, Optional

from services.db import get_db


def registrar_auditoria(
    conn,
    accion: str,
    usuario_id: Optional[int] = None,
    detalle: Optional[str] = None,
    memorando_id: Optional[int] = None,
    ip: Optional[str] = None,
    equipo: Optional[str] = None,
    resultado: Optional[str] = None,
) -> None:
    if detalle is not None and not isinstance(detalle, str):
        detalle = json.dumps(detalle, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO auditoria (
            usuario_id, accion, detalle, memorando_id, ip, equipo, resultado
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (usuario_id, accion, detalle, memorando_id, ip, equipo, resultado),
    )


def registrar_busqueda(
    conn,
    usuario_id: int,
    texto: str,
    cantidad: int,
    ip: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO busquedas (usuario_id, texto_buscado, cantidad_resultados, ip)
        VALUES (?, ?, ?, ?)
        """,
        (usuario_id, texto, cantidad, ip),
    )


def listar_auditoria(
    usuario_id: Optional[int] = None,
    accion: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    memorando_id: Optional[int] = None,
    resultado: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    q = """
        SELECT a.*, u.usuario AS usuario_nombre
        FROM auditoria a
        LEFT JOIN usuarios u ON u.id = a.usuario_id
        WHERE 1=1
    """
    params: list[Any] = []
    if usuario_id:
        q += " AND a.usuario_id = ?"
        params.append(usuario_id)
    if accion:
        q += " AND a.accion = ?"
        params.append(accion)
    if fecha_desde:
        q += " AND date(a.fecha_hora) >= date(?)"
        params.append(fecha_desde)
    if fecha_hasta:
        q += " AND date(a.fecha_hora) <= date(?)"
        params.append(fecha_hasta)
    if memorando_id:
        q += " AND a.memorando_id = ?"
        params.append(memorando_id)
    if resultado:
        q += " AND a.resultado = ?"
        params.append(resultado)
    q += " ORDER BY a.fecha_hora DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        cur = conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]


def acciones_auditoria() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT accion FROM auditoria ORDER BY accion"
        ).fetchall()
    return [r[0] for r in rows]
