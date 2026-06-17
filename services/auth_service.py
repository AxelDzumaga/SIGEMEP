"""Autenticación y gestión de usuarios."""
import random
import re
import sqlite3
from typing import Any, Optional

from services.db import get_db, pwd_context

CUPO_USUARIOS_ROL = {
    "JEFE": 30,
    "BRIGADA": 50,
}


def verificar_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def obtener_usuario_por_login(conn, usuario: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM usuarios WHERE usuario = ?", (usuario.strip(),)
    ).fetchone()


def obtener_usuario_por_id(conn, uid):
    try:
        return conn.execute(
            "SELECT * FROM usuarios WHERE id = ? AND COALESCE(eliminado, 0) = 0 AND UPPER(COALESCE(estado, '')) != 'ELIMINADO'",
            (uid,),
        ).fetchone()
    except Exception:
        return conn.execute(
            "SELECT * FROM usuarios WHERE id = ? AND UPPER(COALESCE(estado, '')) != 'ELIMINADO'",
            (uid,),
        ).fetchone()


def generar_temp_password() -> str:
    return f"SIGEMEP-{random.randint(1000, 9999)}"


def validar_usuario_unico(conn, usuario: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM usuarios WHERE lower(usuario) = lower(?)",
        (usuario.strip(),),
    ).fetchone()
    return row is None


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")


def validar_username(usuario: str) -> Optional[str]:
    u = usuario.strip()
    if not _USERNAME_RE.match(u):
        return "Usuario inválido (3-32 caracteres, letras, números, . _ -)"
    return None


def contar_cupo_rol(conn, rol: str) -> int:
    rol = (rol or "").upper().strip()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE rol = ? AND estado IN ('PENDIENTE', 'ACTIVO') AND COALESCE(eliminado, 0) = 0",
            (rol,),
        ).fetchone()[0]
    except Exception:
        return conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE rol = ? AND estado IN ('PENDIENTE', 'ACTIVO')",
            (rol,),
        ).fetchone()[0]


def puede_registrar_rol(conn, rol: str) -> bool:
    rol = (rol or "").upper().strip()
    maximo = CUPO_USUARIOS_ROL.get(rol)
    if maximo is None:
        return False
    return contar_cupo_rol(conn, rol) < maximo
