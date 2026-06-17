import sys; sys.path.insert(0, ".")
from services.db import get_db
with get_db() as conn:
    rows = conn.execute("SELECT id, usuario, nombre_apellido, rol, estado FROM usuarios ORDER BY rol, usuario").fetchall()
    for r in rows:
        print(dict(r))
