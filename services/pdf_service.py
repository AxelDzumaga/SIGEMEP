"""Indexación PDF, vistas y marca de agua."""
import concurrent.futures
import hashlib
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

_PDF_TIMEOUT = 120  # segundos máximo por PDF antes de considerarlo colgado

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

from config import PDF_BASE_DIR, PREVIEWS_DIR, RESERVADOS_BASE_DIR
from services.db import rebuild_fts


try:
    _DEFAULT_FONT = ImageFont.truetype("arial.ttf", 22)
    _BIG_FONT = ImageFont.truetype("arial.ttf", 42)
except OSError:
    _DEFAULT_FONT = ImageFont.load_default()
    _BIG_FONT = ImageFont.load_default()


def carpeta_pdf_actual(conn=None) -> Path:
    """
    Devuelve la carpeta configurada de PDFs.
    Si existe config.pdf_dir en la base, usa esa ruta.
    Si no existe, usa PDF_BASE_DIR de config.py.
    """
    try:
        if conn is not None:
            row = conn.execute("SELECT valor FROM configuracion WHERE clave = 'pdf_dir'").fetchone()
            if row and row["valor"]:
                return Path(row["valor"])
    except Exception:
        pass

    return Path(PDF_BASE_DIR)


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


def ruta_absoluta_segura(ruta_relativa: str, conn=None) -> Optional[Path]:
    """
    Resuelve una ruta de PDF de forma segura.
    Acepta:
    - rutas relativas guardadas en DB debajo de la carpeta PDF configurada;
    - rutas absolutas, siempre que estén dentro de la carpeta PDF configurada.
    """
    base = carpeta_pdf_actual(conn).resolve()

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


def rel_path_desde_base(abs_path: Path, conn=None) -> str:
    """Devuelve ruta relativa desde la carpeta PDF configurada."""
    base = carpeta_pdf_actual(conn).resolve()
    return abs_path.resolve().relative_to(base).as_posix()


def extraer_texto_y_meta(ruta: Path) -> Tuple[str, dict[str, Any]]:
    """Extrae texto y metadatos de un PDF."""
    doc = fitz.open(ruta)
    try:
        parts = []
        for i in range(len(doc)):
            parts.append(doc.load_page(i).get_text("text") or "")
        meta = doc.metadata or {}
        return "\n".join(parts), dict(meta)
    finally:
        doc.close()


def renderizar_primera_hoja_base(ruta: Path, salida_png: Path, zoom: float = 2.0) -> None:
    """Genera PNG base de la primera hoja, sin marca de agua."""
    doc = fitz.open(ruta)
    try:
        page = doc.load_page(0)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        salida_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(salida_png.as_posix())
    finally:
        doc.close()


def _dibujar_marca_agua(
    img: Image.Image,
    usuario: str,
    rol: str,
    ip: str,
    cuando: datetime,
) -> Image.Image:
    """
    Marca de agua original:
    - texto diagonal "SIGEMEP - USO INTERNO";
    - datos básicos abajo a la izquierda.
    """
    rgba = img.convert("RGBA")
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = rgba.size

    texto_grande = "SIGEMEP - USO INTERNO"
    bbox = draw.textbbox((0, 0), texto_grande, font=_BIG_FONT)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = w // 2, h // 2
    angle = -35

    txt_layer = Image.new("RGBA", (tw + 40, th + 40), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt_layer)
    td.text((20, 20), texto_grande, font=_BIG_FONT, fill=(180, 0, 0, 90))
    rot = txt_layer.rotate(angle, expand=True)
    ox = cx - rot.size[0] // 2
    oy = cy - rot.size[1] // 2
    overlay.paste(rot, (ox, oy), rot)

    fecha = cuando.strftime("%d/%m/%Y")
    hora = cuando.strftime("%H:%M")

    bloque = [
        f"USUARIO: {usuario}",
        f"ROL: {rol}",
        f"FECHA: {fecha}",
        f"HORA: {hora}",
        f"IP: {ip}",
    ]

    y = h - 24 * len(bloque) - 20

    for linea in bloque:
        draw.text(
            (16, y),
            linea,
            font=_DEFAULT_FONT,
            fill=(20, 20, 20, 230)
        )
        y += 24

    return Image.alpha_composite(rgba, overlay).convert("RGB")


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


def _file_fingerprint(path: Path) -> tuple[int, int]:
    """Huella simple por tamaño y fecha de modificación."""
    st = path.stat()
    return int(st.st_size), int(st.st_mtime)


def _safe_progress(progress_callback: Optional[Callable[[dict[str, Any]], None]], data: dict[str, Any]) -> None:
    if progress_callback:
        try:
            progress_callback(data)
        except Exception:
            pass


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


def _procesar_pdf_contenido(abs_path: Path, preview_path: Path) -> tuple[str, int]:
    """Extrae texto, cuenta páginas y genera preview en un solo fitz.open."""
    doc = fitz.open(abs_path)
    try:
        parts = [doc.load_page(i).get_text("text") or "" for i in range(len(doc))]
        n_pages = len(doc)
        meta = doc.metadata or {}
    finally:
        doc.close()
    meta_lines = [f"{k}: {v}" for k, v in (meta or {}).items() if v]
    texto = "\n".join(parts)
    if meta_lines:
        texto += "\n" + "\n".join(meta_lines)
    renderizar_primera_hoja_base(abs_path, preview_path)
    return texto, n_pages


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

    if not base.is_dir():
        stats = {
            "total": 0,
            "procesados": 0,
            "sin_cambios": 0,
            "nuevos": 0,
            "actualizados": 0,
            "no_encontrados": 0,
            "errores": 1,
        }
        _safe_progress(progress_callback, {
            **stats,
            "estado": "error",
            "porcentaje": 0,
            "archivo_actual": "",
            "mensaje": f"La carpeta PDF no existe: {base}",
        })
        return stats

    _ensure_memorando_columns(conn, tabla=tabla)

    pdfs: list[Path] = []
    for root, _, files in os.walk(base):
        for name in files:
            if name.lower().endswith(".pdf"):
                pdfs.append(Path(root) / name)

    total = len(pdfs)
    procesados = nuevos = actualizados = sin_cambios = errores = 0
    vistos_rel: set[str] = set()

    _safe_progress(progress_callback, {
        "estado": "ejecutando",
        "total": total,
        "procesados": 0,
        "sin_cambios": 0,
        "nuevos": 0,
        "actualizados": 0,
        "no_encontrados": 0,
        "errores": 0,
        "porcentaje": 0,
        "archivo_actual": "",
        "mensaje": "Indexando PDFs...",
    })

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        for abs_path in pdfs:
            rel = ""
            try:
                rel = abs_path.resolve().relative_to(base).as_posix()
                vistos_rel.add(rel)

                size, mtime = _file_fingerprint(abs_path)

                row = conn.execute(
                    f"SELECT id, tamanio_bytes, mtime FROM {tabla} WHERE ruta_archivo = ?",
                    (rel,)
                ).fetchone()

                procesados += 1

                if (
                    row
                    and not force
                    and row["tamanio_bytes"] == size
                    and row["mtime"] == mtime
                ):
                    conn.execute(
                        f"UPDATE {tabla} SET activo = 1 WHERE id = ?",
                        (row["id"],)
                    )
                    sin_cambios += 1
                else:
                    preview_name = f"{tabla[:1]}_{hashlib.md5(rel.encode('utf-8')).hexdigest()[:16]}.png"
                    preview_path = PREVIEWS_DIR / preview_name

                    future = executor.submit(_procesar_pdf_contenido, abs_path, preview_path)
                    try:
                        texto, n_pages = future.result(timeout=_PDF_TIMEOUT)
                    except concurrent.futures.TimeoutError:
                        errores += 1
                        _safe_progress(progress_callback, {
                            "estado": "ejecutando",
                            "total": total,
                            "procesados": procesados,
                            "sin_cambios": sin_cambios,
                            "nuevos": nuevos,
                            "actualizados": actualizados,
                            "no_encontrados": 0,
                            "errores": errores,
                            "porcentaje": int((procesados / total) * 100) if total else 100,
                            "archivo_actual": rel or abs_path.name,
                            "mensaje": f"Timeout ({_PDF_TIMEOUT}s) en: {abs_path.name}",
                        })
                        continue

                    if row:
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
                            (
                                abs_path.name,
                                texto,
                                n_pages,
                                preview_path.as_posix(),
                                size,
                                mtime,
                                row["id"],
                            ),
                        )
                        actualizados += 1
                    else:
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
                            (
                                abs_path.name,
                                rel,
                                texto,
                                n_pages,
                                preview_path.as_posix(),
                                size,
                                mtime,
                            ),
                        )
                        nuevos += 1

                # Commit parcial cada 50 PDFs para no bloquear SQLite indefinidamente.
                if procesados % 50 == 0:
                    conn.commit()

            except Exception:
                errores += 1

            porcentaje = int((procesados / total) * 100) if total else 100
            _safe_progress(progress_callback, {
                "estado": "ejecutando",
                "total": total,
                "procesados": procesados,
                "sin_cambios": sin_cambios,
                "nuevos": nuevos,
                "actualizados": actualizados,
                "no_encontrados": 0,
                "errores": errores,
                "porcentaje": porcentaje,
                "archivo_actual": rel or abs_path.name,
                "mensaje": "Indexando PDFs...",
            })

    no_encontrados = 0
    try:
        rows = conn.execute(f"SELECT id, ruta_archivo FROM {tabla}").fetchall()
        for row in rows:
            if row["ruta_archivo"] not in vistos_rel:
                conn.execute(f"UPDATE {tabla} SET activo = 0 WHERE id = ?", (row["id"],))
                no_encontrados += 1
    except Exception:
        pass

    stats = {
        "total": total,
        "procesados": procesados,
        "sin_cambios": sin_cambios,
        "nuevos": nuevos,
        "actualizados": actualizados,
        "no_encontrados": no_encontrados,
        "errores": errores,
    }

    try:
        rebuild_fts(conn, tabla_fts=tabla_fts)
    except Exception:
        pass

    _safe_progress(progress_callback, {
        **stats,
        "estado": "finalizado",
        "porcentaje": 100,
        "archivo_actual": "",
        "mensaje": "Indexación finalizada.",
    })

    return stats
