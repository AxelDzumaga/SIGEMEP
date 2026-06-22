"""Configuración central SIGEMEP."""
import os
import platform
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Rutas por defecto de las carpetas de PDF, dependientes del sistema operativo.
# Se pueden (y se recomienda en producción) sobrescribir con las variables de
# entorno SIGEMEP_PDF_DIR / SIGEMEP_RESERVADOS_DIR, que funcionan igual en
# Windows y Linux porque pathlib.Path normaliza el separador de rutas.
if platform.system() == "Windows":
    _DEFAULT_PDF_DIR = Path(r"C:\Users\SIGEMEP\Desktop\REDCOMPARTIDA")
    _DEFAULT_RESERVADOS_DIR = Path(r"C:\Users\SIGEMEP\Desktop\INFORMES RV")
else:
    _DEFAULT_PDF_DIR = BASE_DIR / "data" / "REDCOMPARTIDA"
    _DEFAULT_RESERVADOS_DIR = BASE_DIR / "data" / "INFORMES_RV"

DEFAULT_PDF_DIR = os.environ.get("SIGEMEP_PDF_DIR", str(_DEFAULT_PDF_DIR))
PDF_BASE_DIR = Path(DEFAULT_PDF_DIR)

DEFAULT_RESERVADOS_DIR = os.environ.get("SIGEMEP_RESERVADOS_DIR", str(_DEFAULT_RESERVADOS_DIR))
RESERVADOS_BASE_DIR = Path(DEFAULT_RESERVADOS_DIR)

DATABASE_PATH = BASE_DIR / "database.db"
PREVIEWS_DIR = BASE_DIR / "previews" / "primeras_hojas"
LOGS_DIR = BASE_DIR / "logs"

# El secreto de sesión NUNCA se hardcodea. Se toma de la variable de entorno
# SIGEMEP_SESSION_SECRET (obligatorio en producción). Si no está definida
# (por ejemplo en un entorno de desarrollo recién clonado), se genera un
# valor aleatorio una sola vez y se persiste en un archivo local fuera del
# control de versiones, para no romper el flujo de trabajo existente sin
# dejar secretos en el código fuente.
SESSION_SECRET = os.environ.get("SIGEMEP_SESSION_SECRET")
if not SESSION_SECRET:
    _secret_file = BASE_DIR / ".session_secret"
    try:
        SESSION_SECRET = _secret_file.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        SESSION_SECRET = None
    if not SESSION_SECRET:
        SESSION_SECRET = secrets.token_hex(32)
        _secret_file.write_text(SESSION_SECRET, encoding="utf-8")
        try:
            os.chmod(_secret_file, 0o600)
        except OSError:
            pass

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

# Si el servidor queda detrás de un proxy/balanceador con TLS (HTTPS), poner
# SIGEMEP_COOKIE_SECURE=1 para que la cookie de sesión solo viaje por HTTPS.
# Por defecto queda en False para no romper despliegues actuales en HTTP plano.
SESSION_COOKIE_SECURE = os.environ.get("SIGEMEP_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes")

CUPO_JEFE = 4
CUPO_BRIGADA = 10

ROLES = ("ADMIN", "JEFE", "BRIGADA")
ESTADOS = ("PENDIENTE", "ACTIVO", "BLOQUEADO", "RECHAZADO")

# Seguridad de autenticación.
MAX_INTENTOS_LOGIN = 5
SESSION_INACTIVITY_TIMEOUT_MINUTOS = 30

# Límite de tamaño para la subida manual de PDF (mismo valor que ya se
# validaba solo del lado del cliente en insertar_memorando.html).
MAX_UPLOAD_MB = 50
