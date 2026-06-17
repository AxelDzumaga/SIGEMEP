"""Generación de PDFs de memorandos institucionales."""
import re
from datetime import date, datetime
from pathlib import Path

import fitz

_MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}

_CHARS_INVALIDOS = re.compile(r'[\\/:*?<>|]')


def _limpiar(texto: str) -> str:
    texto = texto.replace("\n", " ").replace("\r", " ")
    texto = re.sub(r"\s+", " ", texto)
    texto = _CHARS_INVALIDOS.sub("", texto)
    return texto.strip().strip(".- ").strip()


def nombre_archivo_memorando(campos: dict) -> str:
    """Genera el nombre del archivo: {NRO}-"{HECHO}"-{YYYY-MM-DD}.pdf"""
    nro = str(campos.get("nro", "")).strip() or "SIN-NRO"
    hecho = _limpiar((campos.get("hecho", "") or "SIN RESENA").upper())
    fecha_str = campos.get("fecha_hecho", "") or ""
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        fecha = "SIN-FECHA"
    return f'{nro}-"{hecho}"-{fecha}.pdf'


# ── Layout (puntos; 1 cm ≈ 28.35 pt; A4 = 595 × 842 pt) ──────────
_PW  = 595
_PH  = 842
_ML  = 72    # margen izquierdo
_MR  = 523   # margen derecho
_MT  = 72    # margen superior
_FS_TITULO  = 14
_FS_NORMAL  = 11
_FS_SMALL   = 9
_LH         = 16

# Fuentes built-in de PDF (siempre disponibles, soporte de fitz.get_text_length)
_FN   = "helv"   # Helvetica normal
_FN_B = "hebo"   # Helvetica Bold


def _tw(texto: str, fontname: str, fs: float) -> float:
    return fitz.get_text_length(texto, fontname=fontname, fontsize=fs)


def _texto(page: fitz.Page, y: float, texto: str, fontname: str,
           fs: float, alinear: str = "izq", x: float | None = None) -> float:
    """Inserta una línea y retorna el y siguiente."""
    if alinear == "centro":
        x = (_PW - _tw(texto, fontname, fs)) / 2
    elif alinear == "der":
        x = _MR - _tw(texto, fontname, fs)
    else:
        x = x if x is not None else _ML
    page.insert_text((x, y), texto, fontname=fontname, fontsize=fs, color=(0, 0, 0))
    return y + fs * 1.45


def _bloque(page: fitz.Page, y: float, texto: str, fontname: str,
            fs: float, altura_max: float = 150) -> float:
    """Inserta texto con wrap y retorna el y siguiente."""
    rect = fitz.Rect(_ML, y, _MR, y + altura_max)
    sobrante = page.insert_textbox(
        rect, texto, fontname=fontname, fontsize=fs, color=(0, 0, 0), align=0
    )
    if sobrante < 0:
        return y + altura_max + 3
    lineas = max(1, texto.count("\n") + 1)
    return y + lineas * fs * 1.45 + 3


def generar_pdf_memorando(campos: dict) -> bytes:
    """Genera el PDF del memorando en memoria y devuelve los bytes."""
    doc  = fitz.open()
    page = doc.new_page(width=_PW, height=_PH)

    fn   = _FN
    fn_b = _FN_B

    y = _MT

    # ── ENCABEZADO ────────────────────────────────────────────────
    y = _texto(page, y, "M E M O R A N D O", fn_b, _FS_TITULO, "centro")
    y += 4
    nro_pad = str(campos.get("nro", "")).strip().zfill(3)
    anio    = str(campos.get("anio", date.today().year)).strip()
    y = _texto(page, y, f"920-12-000.{nro_pad}/{anio}.-", fn, _FS_NORMAL, "der")
    iniciales = (campos.get("iniciales") or "").strip()
    if iniciales:
        y = _texto(page, y, iniciales, fn, _FS_SMALL, "der")
    y += _LH

    try:
        d   = datetime.strptime(campos.get("fecha_memo", ""), "%Y-%m-%d")
        mes = _MESES_ES[d.month].capitalize()
        fecha_enc = f"BUENOS AIRES, {d.day:02d} de {mes} de {d.year}.-"
    except (ValueError, KeyError):
        fecha_enc = "BUENOS AIRES, __ de ________ de ____.-"
    y = _texto(page, y, fecha_enc, fn, _FS_NORMAL, "der")
    y += _LH

    # ── DE / A / ASUNTO ──────────────────────────────────────────
    de_val = (campos.get("de") or "Departamento CONTROL DE INTEGRIDAD PROFESIONAL.-").strip()
    y = _bloque(page, y, f"DE: {de_val}", fn, _FS_NORMAL)
    a_val  = (campos.get("a") or "").strip()
    y = _bloque(page, y, f"A: {a_val}", fn, _FS_NORMAL)
    y += _LH / 2
    y = _texto(page, y, 'ASUNTO:    "COMUNICAR NOVEDAD"', fn, _FS_NORMAL)
    y += _LH

    # ── HECHO ────────────────────────────────────────────────────
    hecho = (campos.get("hecho") or "").strip().upper()
    y = _texto(page, y, f'HECHO: "{hecho}"', fn_b, _FS_NORMAL)
    y += _LH / 2

    # ── FECHA Y HORA ─────────────────────────────────────────────
    tipo_fecha  = (campos.get("tipo_fecha") or "FECHA DEL HECHO").strip()
    fecha_hecho = campos.get("fecha_hecho", "")
    try:
        dh        = datetime.strptime(fecha_hecho, "%Y-%m-%d")
        fecha_fmt = f"{dh.day:02d}/{dh.month:02d}/{dh.year}"
    except ValueError:
        fecha_fmt = fecha_hecho
    hora = (campos.get("hora") or "").strip()
    linea_fecha = f"{tipo_fecha}: {fecha_fmt}."
    if hora:
        linea_fecha += f"            HORA: {hora}"
    y = _texto(page, y, linea_fecha, fn, _FS_NORMAL)
    y += _LH / 2

    # ── LUGAR ────────────────────────────────────────────────────
    lugar = (campos.get("lugar") or "").strip()
    if lugar:
        y = _bloque(page, y, f"LUGAR DEL HECHO: {lugar}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── PERSONA ──────────────────────────────────────────────────
    etiqueta = (campos.get("etiqueta_persona") or "DAMNIFICADO").strip()
    persona  = (campos.get("persona") or "").strip()
    if persona:
        y = _bloque(page, y, f"{etiqueta}: {persona}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── IMPUTADO ─────────────────────────────────────────────────
    imputado = (campos.get("imputado") or "").strip()
    if imputado:
        y = _bloque(page, y, f"IMPUTADO/S: {imputado}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── ELEMENTOS ────────────────────────────────────────────────
    sustraidos   = (campos.get("elementos_sustraidos")   or "No hubo.").strip()
    secuestrados = (campos.get("elementos_secuestrados") or "No hubo.").strip()
    y = _texto(page, y, f"ELEMENTOS SUSTRAIDOS: {sustraidos}", fn, _FS_NORMAL)
    y += _LH / 2
    y = _texto(page, y, f"ELEMENTOS SECUESTRADOS: {secuestrados}", fn, _FS_NORMAL)
    y += _LH / 2

    # ── DEPENDENCIA ──────────────────────────────────────────────
    dependencia = (campos.get("dependencia") or "").strip()
    if dependencia:
        y = _bloque(page, y, f"DEPENDENCIA PREVENTORA: {dependencia}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── MAGISTRADO ───────────────────────────────────────────────
    magistrado = (campos.get("magistrado") or "").strip()
    if magistrado:
        y = _bloque(page, y, f"MAGISTRADO INTERVENTOR: {magistrado}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── LÍNEA SEPARADORA ─────────────────────────────────────────
    page.draw_line((_ML, y), (_MR, y), color=(0, 0, 0), width=0.5)
    y += _LH

    # ── BREVE RESEÑA ─────────────────────────────────────────────
    y = _texto(page, y, "BREVE RESENA:", fn_b, _FS_NORMAL)
    y += _LH / 2
    resena = (campos.get("resena") or "").strip()
    if resena:
        _bloque(page, y, resena, fn, _FS_NORMAL, altura_max=_PH - y - _MT)

    buf = doc.tobytes()
    doc.close()
    return buf
