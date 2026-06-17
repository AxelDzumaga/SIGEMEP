"""Inicialización SQLite, migraciones y utilidades de conexión."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from passlib.context import CryptContext

from config import DATABASE_PATH, DEFAULT_PDF_DIR, DEFAULT_RESERVADOS_DIR, LOGS_DIR, PREVIEWS_DIR

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def ensure_directories() -> None:
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    Path(DEFAULT_PDF_DIR).mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    cols = _table_columns(conn, table)
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_config(conn, clave: str, default: str = "") -> str:
    row = conn.execute("SELECT valor FROM configuracion WHERE clave = ?", (clave,)).fetchone()
    return row["valor"] if row else default


def set_config(conn, clave: str, valor: str) -> None:
    conn.execute(
        """
        INSERT INTO configuracion (clave, valor, actualizado_en)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor, actualizado_en = CURRENT_TIMESTAMP
        """,
        (clave, valor),
    )


def rebuild_fts(conn, tabla_fts: str = "memorandos_fts") -> None:
    """Reconstruye el índice FTS5 indicado (memorandos_fts o reservados_fts)."""
    if tabla_fts not in ("memorandos_fts", "reservados_fts"):
        raise ValueError(f"tabla_fts no permitida: {tabla_fts}")
    conn.execute(f"INSERT INTO {tabla_fts}({tabla_fts}) VALUES('rebuild')")


def init_db() -> None:
    ensure_directories()
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT UNIQUE NOT NULL,
                nombre_apellido TEXT NOT NULL,
                dni_legajo TEXT,
                password_hash TEXT NOT NULL,
                rol TEXT NOT NULL CHECK (rol IN ('ADMIN', 'JEFE', 'BRIGADA')),
                estado TEXT NOT NULL DEFAULT 'PENDIENTE'
                    CHECK (estado IN ('PENDIENTE', 'ACTIVO', 'BLOQUEADO', 'RECHAZADO')),
                debe_cambiar_password INTEGER DEFAULT 0,
                creado_en DATETIME DEFAULT CURRENT_TIMESTAMP,
                aprobado_por INTEGER,
                aprobado_en DATETIME,
                ultimo_login DATETIME,
                FOREIGN KEY (aprobado_por) REFERENCES usuarios(id)
            );

            CREATE TABLE IF NOT EXISTS memorandos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre_archivo TEXT NOT NULL,
                ruta_archivo TEXT NOT NULL UNIQUE,
                texto_extraido TEXT,
                cantidad_paginas INTEGER,
                primera_hoja_img TEXT,
                fecha_indexado DATETIME DEFAULT CURRENT_TIMESTAMP,
                activo INTEGER DEFAULT 1
            );

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

            CREATE TABLE IF NOT EXISTS auditoria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER,
                accion TEXT NOT NULL,
                detalle TEXT,
                memorando_id INTEGER,
                fecha_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
                ip TEXT,
                equipo TEXT,
                resultado TEXT,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
                FOREIGN KEY (memorando_id) REFERENCES memorandos(id)
            );

            CREATE TABLE IF NOT EXISTS busquedas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                texto_buscado TEXT NOT NULL,
                cantidad_resultados INTEGER,
                fecha_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
                ip TEXT,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            );

            CREATE TABLE IF NOT EXISTS configuracion (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL,
                actualizado_en DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Migraciones para indexación incremental.
        _add_column_if_missing(conn, "memorandos", "tamano_archivo", "INTEGER")
        _add_column_if_missing(conn, "memorandos", "fecha_modificacion_archivo", "TEXT")
        _add_column_if_missing(conn, "memorandos", "existe_en_disco", "INTEGER DEFAULT 1")
        _add_column_if_missing(conn, "memorandos", "ultima_revision", "DATETIME")

        _add_column_if_missing(conn, "usuarios", "permiso_reservados", "INTEGER NOT NULL DEFAULT 0")

        # Índice FTS5 para búsqueda de texto completo.
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memorandos_fts USING fts5(
                nombre_archivo,
                texto_extraido,
                content='memorandos',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        # Poblar FTS5 la primera vez que se crea.
        if get_config(conn, "fts5_inicializado", "0") == "0":
            conn.execute("INSERT INTO memorandos_fts(memorandos_fts) VALUES('rebuild')")
            set_config(conn, "fts5_inicializado", "1")

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

        if not get_config(conn, "pdf_dir", ""):
            set_config(conn, "pdf_dir", DEFAULT_PDF_DIR)

        if not get_config(conn, "reservados_dir", ""):
            set_config(conn, "reservados_dir", DEFAULT_RESERVADOS_DIR)

        row = conn.execute("SELECT id FROM usuarios WHERE usuario = ?", ("admin",)).fetchone()
        if row is None:
            h = pwd_context.hash("admin123")
            conn.execute(
                """
                INSERT INTO usuarios (
                    usuario, nombre_apellido, dni_legajo, password_hash,
                    rol, estado, debe_cambiar_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("admin", "Administrador SIGEMEP", None, h, "ADMIN", "ACTIVO", 1),
            )
