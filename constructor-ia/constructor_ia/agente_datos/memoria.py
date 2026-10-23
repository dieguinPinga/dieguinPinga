"""Memoria persistente del agente de datos (AGENTE_DATOS.md, §5), en un archivo propio: agente.db.

No es el constructor.db productivo. El archivo lleva las tablas de Constructor IA (las migraciones existentes: el
intermediario registra cada consulta en explore_queries y el mapa guarda instantáneas en explore_snapshots) más las
tablas mem_* de este módulo, creadas aquí y no en migrations/ (así el servicio productivo no cambia).

Dos vigencias DISTINTAS (ajuste de la revisión del diseño):
- vigencia del ESQUEMA: huella de la estructura de cada tabla (columnas, tipos, claves, índices). Si cambia, la
  ficha de la tabla pasa a «revisar» y hay que volver a describirla.
- vigencia de los DATOS: huella de los datos de cada tabla (filas y un resumen de todas sus celdas). Si cambia, las
  MEDICIONES (perfiles, contención de relaciones) quedan desactualizadas y se vuelven a medir, aunque el esquema sea
  el mismo. Las consultas aprendidas se vuelven a ejecutar SIEMPRE, cambien o no los datos.

Nada se borra: una ficha nueva reemplaza a la anterior (que queda como «reemplazada»), el glosario solo suma usos y
contradicciones, y cada misión deja su bitácora y sus decisiones. El veredicto del juez NUNCA se escribe aquí: el
agente no puede ver si acertó.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..db import Database

ESQUEMA_MEM = """
CREATE TABLE IF NOT EXISTS mem_meta (clave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS mem_misiones (
  id TEXT PRIMARY KEY, conexion TEXT NOT NULL, pregunta TEXT NOT NULL, estado TEXT NOT NULL, nivel TEXT,
  creada REAL NOT NULL, terminada REAL, informe_json TEXT);
CREATE TABLE IF NOT EXISTS mem_decisiones (
  id INTEGER PRIMARY KEY, mision_id TEXT NOT NULL, etapa TEXT NOT NULL, regla TEXT NOT NULL, porque TEXT NOT NULL,
  datos_json TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mem_tablas (
  id INTEGER PRIMARY KEY, conexion TEXT NOT NULL, tabla TEXT NOT NULL, huella_estructura TEXT NOT NULL,
  huella_datos TEXT, filas INTEGER, perfil_json TEXT NOT NULL, muestra_json TEXT, consultas_json TEXT,
  medida_en REAL NOT NULL, mision_id TEXT NOT NULL, vigente INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS mem_tablas_i ON mem_tablas(conexion, tabla, vigente);
CREATE TABLE IF NOT EXISTS mem_relaciones (
  id INTEGER PRIMARY KEY, conexion TEXT NOT NULL, tabla TEXT NOT NULL, columna TEXT NOT NULL, tabla_ref TEXT NOT NULL,
  columna_ref TEXT NOT NULL, origen TEXT NOT NULL, contencion INTEGER, huerfanos INTEGER, unico INTEGER NOT NULL,
  huellas_datos_json TEXT NOT NULL, consulta_id INTEGER, medida_en REAL NOT NULL, mision_id TEXT NOT NULL,
  vigente INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS mem_fichas (
  id INTEGER PRIMARY KEY, conexion TEXT NOT NULL, tabla TEXT NOT NULL, huella_estructura TEXT NOT NULL,
  fila TEXT NOT NULL, roles_json TEXT NOT NULL, problemas_json TEXT NOT NULL, evidencia_json TEXT NOT NULL,
  estado TEXT NOT NULL, creada_en REAL NOT NULL, mision_id TEXT NOT NULL, confirmaciones INTEGER NOT NULL DEFAULT 0,
  ultima_mision TEXT, motivo_estado TEXT);
CREATE TABLE IF NOT EXISTS mem_glosario (
  id INTEGER PRIMARY KEY, conexion TEXT NOT NULL, raiz TEXT NOT NULL, objeto TEXT NOT NULL, origen TEXT NOT NULL,
  usos INTEGER NOT NULL DEFAULT 1, contradicciones INTEGER NOT NULL DEFAULT 0, misiones_json TEXT NOT NULL,
  creado REAL NOT NULL, ultimo_uso REAL NOT NULL, UNIQUE(conexion, raiz, objeto, origen));
CREATE TABLE IF NOT EXISTS mem_consultas (
  id INTEGER PRIMARY KEY, conexion TEXT NOT NULL, pregunta TEXT NOT NULL, pregunta_norm TEXT NOT NULL,
  tablas_json TEXT NOT NULL, medir_texto TEXT, sql TEXT NOT NULL, huellas_estructura_json TEXT NOT NULL,
  resultado_hash TEXT, nivel TEXT NOT NULL, mision_id TEXT NOT NULL, creada REAL NOT NULL, usos INTEGER NOT NULL DEFAULT 0,
  ultimo_uso REAL);
CREATE TABLE IF NOT EXISTS mem_fallos (
  id INTEGER PRIMARY KEY, conexion TEXT NOT NULL, mision_id TEXT NOT NULL, etapa TEXT NOT NULL, motivo TEXT NOT NULL,
  detalle_json TEXT, ts REAL NOT NULL);
"""


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Memoria:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.db = Database(self.path)
        self.db.migrate()
        self.db.connect().executescript(ESQUEMA_MEM)
        self.db.execute("INSERT OR IGNORE INTO mem_meta VALUES ('version', '1')")

    def cerrar(self):
        self.db.close()

    def integridad(self) -> str:
        return self.db.one("PRAGMA integrity_check")[0]

    # Misiones y decisiones -------------------------------------------------------------------------------------
    def mision_nueva(self, mid: str, conexion: str, pregunta: str):
        self.db.execute("INSERT INTO mem_misiones (id, conexion, pregunta, estado, creada) VALUES (?,?,?,?,?)",
                        (mid, conexion, pregunta, "en_curso", time.time()))

    def terminar_mision(self, mid: str, estado: str, nivel: str | None, informe: dict):
        self.db.execute("UPDATE mem_misiones SET estado=?, nivel=?, terminada=?, informe_json=? WHERE id=?",
                        (estado, nivel, time.time(), _j(informe), mid))

    def decision(self, mid: str, etapa: str, regla: str, porque: str, datos=None):
        self.db.execute("INSERT INTO mem_decisiones (mision_id, etapa, regla, porque, datos_json, ts) "
                        "VALUES (?,?,?,?,?,?)", (mid, etapa, regla, porque, _j(datos), time.time()))

    def decisiones(self, mid: str) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT etapa, regla, porque, datos_json FROM mem_decisiones "
                                             "WHERE mision_id=? ORDER BY id", (mid,))]

    def fallo(self, conexion: str, mid: str, etapa: str, motivo: str, detalle=None):
        self.db.execute("INSERT INTO mem_fallos (conexion, mision_id, etapa, motivo, detalle_json, ts) "
                        "VALUES (?,?,?,?,?,?)", (conexion, mid, etapa, motivo, _j(detalle), time.time()))

    def fallos(self, conexion: str, limite: int = 5) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT mision_id, etapa, motivo FROM mem_fallos WHERE conexion=? "
                                             "ORDER BY id DESC LIMIT ?", (conexion, limite))]

    # Mediciones de tablas (vigencia de los datos) -------------------------------------------------------------
    def medicion(self, conexion: str, tabla: str) -> dict | None:
        r = self.db.one("SELECT * FROM mem_tablas WHERE conexion=? AND tabla=? AND vigente=1 ORDER BY id DESC",
                        (conexion, tabla))
        if r is None:
            return None
        d = dict(r)
        d["perfil"] = json.loads(d.pop("perfil_json"))
        d["muestra"] = json.loads(d.pop("muestra_json") or "null")
        d["consultas"] = json.loads(d.pop("consultas_json") or "[]")
        return d

    def guardar_medicion(self, conexion: str, tabla: str, huella_estructura: str, huella_datos: str | None,
                         perfil: dict, muestra: dict | None, consultas: list, mid: str) -> int:
        with self.db.tx():
            self.db.execute("UPDATE mem_tablas SET vigente=0 WHERE conexion=? AND tabla=? AND vigente=1",
                            (conexion, tabla))
            cur = self.db.execute(
                "INSERT INTO mem_tablas (conexion, tabla, huella_estructura, huella_datos, filas, perfil_json, "
                "muestra_json, consultas_json, medida_en, mision_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (conexion, tabla, huella_estructura, huella_datos, perfil.get("filas"), _j(perfil), _j(muestra),
                 _j(consultas), time.time(), mid))
        return cur.lastrowid

    def retirar_medicion(self, conexion: str, tabla: str):
        self.db.execute("UPDATE mem_tablas SET vigente=0 WHERE conexion=? AND tabla=? AND vigente=1", (conexion, tabla))

    # Relaciones (dependen de los datos: la contención puede cambiar) ------------------------------------------
    def relaciones(self, conexion: str) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM mem_relaciones WHERE conexion=? AND vigente=1 ORDER BY id", (conexion,)):
            d = dict(r)
            d["contencion"] = None if d["contencion"] is None else bool(d["contencion"])
            d["unico"] = bool(d["unico"])
            d["huellas_datos"] = json.loads(d.pop("huellas_datos_json"))
            out.append(d)
        return out

    def guardar_relacion(self, conexion: str, rel: dict, mid: str) -> int:
        with self.db.tx():
            self.db.execute("UPDATE mem_relaciones SET vigente=0 WHERE conexion=? AND tabla=? AND columna=? AND "
                            "tabla_ref=? AND columna_ref=? AND vigente=1",
                            (conexion, rel["tabla"], rel["columna"], rel["tabla_ref"], rel["columna_ref"]))
            cur = self.db.execute(
                "INSERT INTO mem_relaciones (conexion, tabla, columna, tabla_ref, columna_ref, origen, contencion, "
                "huerfanos, unico, huellas_datos_json, consulta_id, medida_en, mision_id) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (conexion, rel["tabla"], rel["columna"], rel["tabla_ref"], rel["columna_ref"], rel["origen"],
                 None if rel.get("contencion") is None else int(rel["contencion"]), rel.get("huerfanos"),
                 int(bool(rel.get("unico"))), _j(rel.get("huellas_datos", {})), rel.get("consulta_id"), time.time(),
                 mid))
        return cur.lastrowid

    def retirar_relacion(self, conexion: str, tabla: str, columna: str, tabla_ref: str, columna_ref: str):
        self.db.execute("UPDATE mem_relaciones SET vigente=0 WHERE conexion=? AND tabla=? AND columna=? AND "
                        "tabla_ref=? AND columna_ref=? AND vigente=1", (conexion, tabla, columna, tabla_ref, columna_ref))

    def retirar_relaciones(self, conexion: str, tabla: str):
        self.db.execute("UPDATE mem_relaciones SET vigente=0 WHERE conexion=? AND (tabla=? OR tabla_ref=?) AND "
                        "vigente=1", (conexion, tabla, tabla))

    # Fichas (interpretaciones del modelo, verificadas en forma; vigencia del esquema) --------------------------
    def ficha(self, conexion: str, tabla: str, estados=("vigente", "revisar")) -> dict | None:
        q = ",".join("?" * len(estados))
        r = self.db.one(f"SELECT * FROM mem_fichas WHERE conexion=? AND tabla=? AND estado IN ({q}) ORDER BY id DESC",
                        (conexion, tabla, *estados))
        return self._ficha(r) if r else None

    def fichas(self, conexion: str, estado: str = "vigente") -> list[dict]:
        return [self._ficha(r) for r in self.db.all("SELECT * FROM mem_fichas WHERE conexion=? AND estado=? "
                                                    "ORDER BY tabla, id", (conexion, estado))]

    @staticmethod
    def _ficha(r) -> dict:
        d = dict(r)
        for k in ("roles", "problemas", "evidencia"):
            d[k] = json.loads(d.pop(k + "_json"))
        return d

    def guardar_ficha(self, conexion: str, tabla: str, huella: str, fila: str, roles: dict, problemas: list,
                      evidencia: dict, mid: str) -> int:
        with self.db.tx():
            self.db.execute("UPDATE mem_fichas SET estado='reemplazada', motivo_estado=? WHERE conexion=? AND "
                            "tabla=? AND estado IN ('vigente', 'revisar')", (f"reemplazada en {mid}", conexion, tabla))
            cur = self.db.execute(
                "INSERT INTO mem_fichas (conexion, tabla, huella_estructura, fila, roles_json, problemas_json, "
                "evidencia_json, estado, creada_en, mision_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (conexion, tabla, huella, fila, _j(roles), _j(problemas), _j(evidencia), "vigente", time.time(), mid))
        return cur.lastrowid

    def marcar_ficha(self, conexion: str, tabla: str, estado: str, motivo: str):
        self.db.execute("UPDATE mem_fichas SET estado=?, motivo_estado=? WHERE conexion=? AND tabla=? AND "
                        "estado IN ('vigente', 'revisar')", (estado, motivo, conexion, tabla))

    def confirmar_ficha(self, ficha_id: int, mid: str):
        self.db.execute("UPDATE mem_fichas SET confirmaciones = confirmaciones + 1, ultima_mision=? WHERE id=?",
                        (mid, ficha_id))

    # Glosario ---------------------------------------------------------------------------------------------------
    def glosario(self, conexion: str) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM mem_glosario WHERE conexion=? ORDER BY raiz, id", (conexion,)):
            d = dict(r)
            d["misiones"] = json.loads(d.pop("misiones_json"))
            out.append(d)
        return out

    def registrar_termino(self, conexion: str, raiz: str, objeto: str, origen: str, mid: str):
        """origen: «verificada» (el término coincidió con el esquema o una ficha en una respuesta verificada) o
        «declarada» (el modelo lo vinculó y no se pudo comprobar). Solo «verificada» cuenta como respaldo."""
        r = self.db.one("SELECT id, misiones_json FROM mem_glosario WHERE conexion=? AND raiz=? AND objeto=? AND "
                        "origen=?", (conexion, raiz, objeto, origen))
        now = time.time()
        if r is None:
            self.db.execute("INSERT INTO mem_glosario (conexion, raiz, objeto, origen, misiones_json, creado, "
                            "ultimo_uso) VALUES (?,?,?,?,?,?,?)", (conexion, raiz, objeto, origen, _j([mid]), now, now))
        else:
            mis = json.loads(r["misiones_json"])
            if mid not in mis:
                mis.append(mid)
            self.db.execute("UPDATE mem_glosario SET usos = usos + 1, misiones_json=?, ultimo_uso=? WHERE id=?",
                            (_j(mis), now, r["id"]))

    # Consultas aprendidas ---------------------------------------------------------------------------------------
    def consulta_aprendida(self, conexion: str, pregunta_norm: str) -> dict | None:
        r = self.db.one("SELECT * FROM mem_consultas WHERE conexion=? AND pregunta_norm=? ORDER BY id DESC",
                        (conexion, pregunta_norm))
        if r is None:
            return None
        d = dict(r)
        d["tablas"] = json.loads(d.pop("tablas_json"))
        d["huellas_estructura"] = json.loads(d.pop("huellas_estructura_json"))
        return d

    def guardar_consulta(self, conexion: str, pregunta: str, pregunta_norm: str, tablas: list, medir_texto: str | None,
                         sql: str, huellas: dict, resultado_hash: str, nivel: str, mid: str) -> int:
        cur = self.db.execute(
            "INSERT INTO mem_consultas (conexion, pregunta, pregunta_norm, tablas_json, medir_texto, sql, "
            "huellas_estructura_json, resultado_hash, nivel, mision_id, creada) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (conexion, pregunta, pregunta_norm, _j(tablas), medir_texto, sql, _j(huellas), resultado_hash, nivel, mid,
             time.time()))
        return cur.lastrowid

    def usar_consulta(self, cid: int):
        self.db.execute("UPDATE mem_consultas SET usos = usos + 1, ultimo_uso=? WHERE id=?", (time.time(), cid))
