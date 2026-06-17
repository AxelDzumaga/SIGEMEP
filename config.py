"""Configuración central SIGEMEP."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Ruta definitiva por defecto para los PDF.
DEFAULT_PDF_DIR = r"C:\Users\SIGEMEP\Desktop\REDCOMPARTIDA"
PDF_BASE_DIR = Path(os.environ.get("SIGEMEP_PDF_DIR", DEFAULT_PDF_DIR))

# Ruta definitiva por defecto para los archivos reservados.
DEFAULT_RESERVADOS_DIR = r"C:\Users\SIGEMEP\Desktop\INFORMES RV"
RESERVADOS_BASE_DIR = Path(os.environ.get("SIGEMEP_RESERVADOS_DIR", DEFAULT_RESERVADOS_DIR))

DATABASE_PATH = BASE_DIR / "database.db"
PREVIEWS_DIR = BASE_DIR / "previews" / "primeras_hojas"
LOGS_DIR = BASE_DIR / "logs"

SESSION_SECRET = os.environ.get(
    "SIGEMEP_SESSION_SECRET",
    "cambiar-este-secreto-en-produccion-sigemep-2026",
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

CUPO_JEFE = 4
CUPO_BRIGADA = 10

ROLES = ("ADMIN", "JEFE", "BRIGADA")
ESTADOS = ("PENDIENTE", "ACTIVO", "BLOQUEADO", "RECHAZADO")
