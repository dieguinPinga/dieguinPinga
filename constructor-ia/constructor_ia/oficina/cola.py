"""Cola persistente de misiones y tareas (etapa 2 de la oficina, OFICINA.md). Sin inferencias.

Una MISIÓN es un grupo de tareas con dependencias (sin ciclos). Cada TAREA pide un rol, un modelo y un permiso
(«inferencia» en el MVP) y lleva una carga JSON opaca para el trabajador. Estados de una tarea:

    pendiente → asignada → en_curso → propuesta → verificada
                    │           │          └→ rechazada por el coordinador → pendiente (si quedan intentos) | fallida
                    └───────────┴→ arriendo vencido o falla del trabajador → pendiente (si quedan intentos) | fallida
    pendiente con una dependencia fallida o cancelada → cancelada

Reglas:
- ASIGNACIÓN ATÓMICA: «tomar» corre en una transacción BEGIN IMMEDIATE de SQLite. Dos trabajadores (o dos
  procesos) nunca reciben la misma tarea, y un trabajador no recibe otra mientras tenga una activa (capacidad 1).
  Solo se asignan tareas cuyas dependencias están VERIFICADAS, con el modelo y el permiso que el trabajador declara y
  con datos de origen sintético (el MVP no manda otra cosa afuera).
- ARRIENDOS: cada asignación lleva un token y un vencimiento (el tiempo máximo de la tarea). El trabajador puede
  renovarlo con «latido». Una entrega con un token viejo o un arriendo vencido se RECHAZA: el resultado tardío de un
  trabajador que se desconectó nunca pisa una reasignación.
- RECUPERACIÓN: «recuperar» (también al arrancar el coordinador) devuelve a la cola los arriendos vencidos, cuenta el
  intento y cancela lo que depende de una tarea fallida. Nada queda «en curso» para siempre.
- VERIFICACIÓN CENTRAL: un trabajador solo entrega una PROPUESTA; la verifica (o rechaza) el coordinador. Una tarea
  solo habilita a sus dependientes cuando está verificada.
- Cada transición queda en «eventos» (trazabilidad).
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from ..db import Database

ESQUEMA = """
CREATE TABLE IF NOT EXISTS misiones (
  id TEXT PRIMARY KEY, titulo TEXT NOT NULL, estado TEXT NOT NULL, creado REAL NOT NULL, actualizado REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tareas (
  id TEXT PRIMARY KEY, mision_id TEXT NOT NULL REFERENCES misiones(id), nombre TEXT NOT NULL, rol TEXT NOT NULL,
  modelo TEXT NOT NULL, permiso TEXT NOT NULL, origen_datos TEXT NOT NULL, carga_json TEXT NOT NULL,
  estado TEXT NOT NULL, intentos INTEGER NOT NULL DEFAULT 0, max_intentos INTEGER NOT NULL,
  tiempo_max_s INTEGER NOT NULL, trabajador_id TEXT, token TEXT, arriendo_vence REAL, propuesta_json TEXT,
  verificacion TEXT, error TEXT, creado REAL NOT NULL, actualizado REAL NOT NULL, orden INTEGER NOT NULL,
  UNIQUE (mision_id, nombre));
CREATE TABLE IF NOT EXISTS dependencias (
  tarea_id TEXT NOT NULL REFERENCES tareas(id), depende_de TEXT NOT NULL REFERENCES tareas(id),
  PRIMARY KEY (tarea_id, depende_de));
CREATE TABLE IF NOT EXISTS eventos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tarea_id TEXT, mision_id TEXT, ts REAL NOT NULL, de TEXT, a TEXT,
  trabajador_id TEXT, detalle TEXT);
CREATE INDEX IF NOT EXISTS tareas_estado ON tareas(estado, orden);
"""
ACTIVOS = ("asignada", "en_curso")
TERMINALES = ("verificada", "fallida", "cancelada")
PERMISOS = ("inferencia",)
ORIGENES = ("sintetico",)
MAX_CARGA = 64_000


class ErrorCola(ValueError):
    pass


class ArriendoInvalido(ErrorCola):
    """Token viejo, arriendo vencido o tarea que ya no está activa: la entrega no cuenta."""


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Cola:
    def __init__(self, ruta: Path, reloj=time.time):
        self.db = Database(Path(ruta))
        self.db.connect().executescript(ESQUEMA)
        self.reloj = reloj

    def cerrar(self):
        self.db.close()

    # Misiones ---------------------------------------------------------------------------------------------------
    def crear_mision(self, titulo: str, tareas: list[dict]) -> str:
        """tareas: [{nombre, rol, modelo, carga, depende_de=[nombres], permiso="inferencia", origen_datos="sintetico",
        max_intentos=3, tiempo_max_s=900}]. Todo o nada; se rechazan ciclos y dependencias desconocidas."""
        if not titulo or not tareas:
            raise ErrorCola("una misión necesita un título y al menos una tarea")
        nombres = [t.get("nombre") for t in tareas]
        if any(not n or not re.fullmatch(r"[\w.-]{1,60}", n) for n in nombres) or len(set(nombres)) != len(nombres):
            raise ErrorCola("cada tarea necesita un nombre único (letras, números, «.», «-», «_»)")
        deps = {t["nombre"]: list(dict.fromkeys(t.get("depende_de") or [])) for t in tareas}
        for n, ds in deps.items():
            malas = [d for d in ds if d not in deps or d == n]
            if malas:
                raise ErrorCola(f"{n}: dependencias desconocidas o propias: {', '.join(malas)}")
        _sin_ciclos(deps)
        for t in tareas:
            if t.get("permiso", "inferencia") not in PERMISOS:
                raise ErrorCola(f"{t['nombre']}: permiso «{t.get('permiso')}» no existe en el MVP (solo inferencia)")
            if t.get("origen_datos", "sintetico") not in ORIGENES:
                raise ErrorCola(f"{t['nombre']}: el MVP solo manda datos de origen sintético a los trabajadores")
            if not t.get("rol") or not t.get("modelo"):
                raise ErrorCola(f"{t['nombre']}: falta rol o modelo")
            if len(_j(t.get("carga") or {})) > MAX_CARGA:
                raise ErrorCola(f"{t['nombre']}: la carga supera {MAX_CARGA} bytes")
            if not 1 <= int(t.get("max_intentos", 3)) <= 10 or not 1 <= int(t.get("tiempo_max_s", 900)) <= 6 * 3600:
                raise ErrorCola(f"{t['nombre']}: max_intentos entre 1 y 10, tiempo_max_s entre 1 y 21600")
        ahora = self.reloj()
        mid = time.strftime("O%Y%m%d-%H%M%S-", time.gmtime(ahora)) + secrets.token_hex(3)
        ids = {n: f"{mid}.{n}" for n in nombres}
        with self.db.tx() as c:
            c.execute("INSERT INTO misiones (id, titulo, estado, creado, actualizado) VALUES (?,?,?,?,?)",
                      (mid, titulo, "pendiente", ahora, ahora))
            for i, t in enumerate(tareas):
                c.execute("INSERT INTO tareas (id, mision_id, nombre, rol, modelo, permiso, origen_datos, carga_json, "
                          "estado, max_intentos, tiempo_max_s, creado, actualizado, orden) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (ids[t["nombre"]], mid, t["nombre"], t["rol"], t["modelo"], t.get("permiso", "inferencia"),
                           t.get("origen_datos", "sintetico"), _j(t.get("carga") or {}), "pendiente",
                           int(t.get("max_intentos", 3)), int(t.get("tiempo_max_s", 900)), ahora, ahora, i))
                for d in deps[t["nombre"]]:
                    c.execute("INSERT INTO dependencias (tarea_id, depende_de) VALUES (?,?)", (ids[t["nombre"]], ids[d]))
            self._evento(c, None, mid, None, "pendiente", None, f"misión creada con {len(tareas)} tareas")
        return mid

    def cancelar_mision(self, mid: str, motivo: str = "cancelada por el operador") -> None:
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM misiones WHERE id=?", (mid,)).fetchone() is None:
                raise ErrorCola(f"la misión {mid} no existe")
            for r in c.execute("SELECT id, estado FROM tareas WHERE mision_id=? AND estado NOT IN "
                               "('verificada','fallida','cancelada')", (mid,)).fetchall():
                self._mover(c, r["id"], r["estado"], "cancelada", None, motivo, limpiar=True)
            self._estado_mision(c, mid)

    # Trabajadores -----------------------------------------------------------------------------------------------
    def tomar(self, trabajador_id: str, modelos: list[str], permisos=PERMISOS, capacidad: int = 1,
              roles: list[str] | None = None) -> dict | None:
        """Asigna atómicamente la primera tarea lista y compatible (orden de creación). None si no hay. roles: si se
        indica, solo tareas de esos roles (por ejemplo, un auditor no toma cálculos)."""
        ahora = self.reloj()
        with self.db.tx() as c:
            activas = c.execute("SELECT COUNT(*) FROM tareas WHERE trabajador_id=? AND estado IN ('asignada','en_curso')",
                                (trabajador_id,)).fetchone()[0]
            if activas >= capacidad:
                return None
            q = ("SELECT t.* FROM tareas t WHERE t.estado='pendiente' AND t.modelo IN ({}) AND t.permiso IN ({}) "
                 "AND t.origen_datos='sintetico' AND NOT EXISTS (SELECT 1 FROM dependencias d JOIN tareas x "
                 "ON x.id=d.depende_de WHERE d.tarea_id=t.id AND x.estado!='verificada') "
                 "{}ORDER BY t.creado, t.orden LIMIT 1").format(
                ",".join("?" * len(modelos)) or "NULL", ",".join("?" * len(permisos)) or "NULL",
                f"AND t.rol IN ({','.join('?' * len(roles))}) " if roles else "")
            r = c.execute(q, (*modelos, *permisos, *(roles or []))).fetchone()
            if r is None:
                return None
            token = secrets.token_hex(12)
            c.execute("UPDATE tareas SET estado='asignada', trabajador_id=?, token=?, arriendo_vence=?, "
                      "intentos=intentos+1, actualizado=? WHERE id=? AND estado='pendiente'",
                      (trabajador_id, token, ahora + r["tiempo_max_s"], ahora, r["id"]))
            self._evento(c, r["id"], r["mision_id"], "pendiente", "asignada", trabajador_id,
                         f"intento {r['intentos'] + 1} de {r['max_intentos']}; arriendo de {r['tiempo_max_s']} s")
            self._estado_mision(c, r["mision_id"])
        return self.tarea(r["id"]) | {"token": token}

    def tomar_para(self, trabajador: dict) -> dict | None:
        """Con un trabajador del registro: solo si está disponible AHORA (latido vigente), con sus modelos, permisos y
        capacidad declarados."""
        if not trabajador.get("disponible_ahora"):
            raise ErrorCola(f"{trabajador.get('id')}: no está disponible ahora (estado {trabajador.get('estado')}, "
                            f"latido {'vigente' if trabajador.get('latido_vigente') else 'vencido o ausente'})")
        return self.tomar(trabajador["id"], trabajador["modelos"], trabajador["permisos"], trabajador["capacidad"])

    def iniciar(self, tid: str, token: str) -> None:
        with self.db.tx() as c:
            r = self._activa(c, tid, token)
            if r["estado"] == "asignada":
                self._mover(c, tid, "asignada", "en_curso", r["trabajador_id"], "el trabajador empezó")

    def latido(self, tid: str, token: str) -> float:
        """Renueva el arriendo por otro tiempo_max_s. Devuelve el nuevo vencimiento."""
        with self.db.tx() as c:
            r = self._activa(c, tid, token)
            vence = self.reloj() + r["tiempo_max_s"]
            c.execute("UPDATE tareas SET arriendo_vence=?, actualizado=? WHERE id=?", (vence, self.reloj(), tid))
        return vence

    def entregar(self, tid: str, token: str, propuesta: dict) -> None:
        """El trabajador entrega una PROPUESTA (no un resultado verificado)."""
        if len(_j(propuesta)) > MAX_CARGA:
            raise ErrorCola(f"la propuesta supera {MAX_CARGA} bytes")
        with self.db.tx() as c:
            r = self._activa(c, tid, token)
            c.execute("UPDATE tareas SET propuesta_json=?, arriendo_vence=NULL WHERE id=?", (_j(propuesta), tid))
            self._mover(c, tid, r["estado"], "propuesta", r["trabajador_id"],
                        "propuesta entregada: " + _j(propuesta)[:300])

    def fallar(self, tid: str, token: str, error: str) -> str:
        """El trabajador informa una falla: vuelve a la cola si quedan intentos. Devuelve el nuevo estado."""
        with self.db.tx() as c:
            r = self._activa(c, tid, token)
            return self._reintentar(c, r, f"falla informada por {r['trabajador_id']}: {error[:300]}")

    # Coordinador ------------------------------------------------------------------------------------------------
    def verificar(self, tid: str, aceptada: bool, detalle: str) -> str:
        """Solo el coordinador: la propuesta pasa a verificada o se rechaza (vuelve a la cola si quedan intentos)."""
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM tareas WHERE id=?", (tid,)).fetchone()
            if r is None or r["estado"] != "propuesta":
                raise ErrorCola(f"{tid}: solo se verifica una tarea en estado «propuesta»")
            c.execute("UPDATE tareas SET verificacion=? WHERE id=?", (detalle[:500], tid))
            if aceptada:
                self._mover(c, tid, "propuesta", "verificada", r["trabajador_id"], f"verificada: {detalle[:300]}")
                self._estado_mision(c, r["mision_id"])
                return "verificada"
            return self._reintentar(c, r, f"propuesta rechazada por el coordinador: {detalle[:300]}")

    def recuperar(self) -> list[str]:
        """Arriendos vencidos → cola (o fallida). Se llama al arrancar y periódicamente. Devuelve las tareas tocadas."""
        ahora = self.reloj()
        tocadas = []
        with self.db.tx() as c:
            for r in c.execute("SELECT * FROM tareas WHERE estado IN ('asignada','en_curso') AND arriendo_vence < ?",
                               (ahora,)).fetchall():
                self._reintentar(c, r, f"arriendo vencido: {r['trabajador_id']} no entregó ni renovó a tiempo "
                                       "(desconexión o interrupción)")
                tocadas.append(r["id"])
        return tocadas

    # Consulta ---------------------------------------------------------------------------------------------------
    def tarea(self, tid: str) -> dict | None:
        r = self.db.one("SELECT * FROM tareas WHERE id=?", (tid,))
        if r is None:
            return None
        d = dict(r)
        d.pop("token", None)
        d["carga"] = json.loads(d.pop("carga_json"))
        d["propuesta"] = json.loads(d.pop("propuesta_json") or "null")
        d["depende_de"] = [x[0] for x in self.db.all("SELECT depende_de FROM dependencias WHERE tarea_id=?", (tid,))]
        return d

    def mision(self, mid: str) -> dict | None:
        r = self.db.one("SELECT * FROM misiones WHERE id=?", (mid,))
        if r is None:
            return None
        return dict(r) | {"tareas": [self.tarea(x[0]) for x in
                                     self.db.all("SELECT id FROM tareas WHERE mision_id=? ORDER BY orden", (mid,))]}

    def misiones(self) -> list[dict]:
        return [self.mision(r[0]) for r in self.db.all("SELECT id FROM misiones ORDER BY creado, id")]

    def eventos(self, tid: str | None = None, mid: str | None = None) -> list[dict]:
        if tid:
            return [dict(r) for r in self.db.all("SELECT * FROM eventos WHERE tarea_id=? ORDER BY id", (tid,))]
        return [dict(r) for r in self.db.all("SELECT * FROM eventos WHERE mision_id=? ORDER BY id", (mid,))]

    # Interno ----------------------------------------------------------------------------------------------------
    def _activa(self, c, tid: str, token: str):
        r = c.execute("SELECT * FROM tareas WHERE id=?", (tid,)).fetchone()
        if r is None or r["estado"] not in ACTIVOS or not token or r["token"] != token:
            raise ArriendoInvalido(f"{tid}: arriendo inválido (la tarea ya no es de este trabajador o no está activa)")
        if r["arriendo_vence"] is not None and r["arriendo_vence"] < self.reloj():
            raise ArriendoInvalido(f"{tid}: el arriendo venció; la tarea vuelve a la cola con «recuperar»")
        return r

    def _reintentar(self, c, r, motivo: str) -> str:
        c.execute("UPDATE tareas SET propuesta_json=NULL WHERE id=?", (r["id"],))   # queda en los eventos
        if r["intentos"] < r["max_intentos"]:
            self._mover(c, r["id"], r["estado"], "pendiente", r["trabajador_id"], motivo, limpiar=True)
            nuevo = "pendiente"
        else:
            self._mover(c, r["id"], r["estado"], "fallida", r["trabajador_id"],
                        f"{motivo}; sin intentos ({r['intentos']} de {r['max_intentos']})", limpiar=True)
            c.execute("UPDATE tareas SET error=? WHERE id=?", (motivo[:500], r["id"]))
            self._cancelar_dependientes(c, r["id"])
            nuevo = "fallida"
        self._estado_mision(c, r["mision_id"])
        return nuevo

    def _cancelar_dependientes(self, c, tid: str):
        for (dep,) in c.execute("SELECT tarea_id FROM dependencias WHERE depende_de=?", (tid,)).fetchall():
            r = c.execute("SELECT estado FROM tareas WHERE id=?", (dep,)).fetchone()
            if r["estado"] not in TERMINALES:
                self._mover(c, dep, r["estado"], "cancelada", None, f"depende de {tid}, que no se pudo completar",
                            limpiar=True)
                self._cancelar_dependientes(c, dep)

    def _mover(self, c, tid, de, a, trabajador, detalle, limpiar=False):
        extra = ", trabajador_id=NULL, token=NULL, arriendo_vence=NULL" if limpiar else ""
        c.execute(f"UPDATE tareas SET estado=?, actualizado=?{extra} WHERE id=?", (a, self.reloj(), tid))
        mid = c.execute("SELECT mision_id FROM tareas WHERE id=?", (tid,)).fetchone()[0]
        self._evento(c, tid, mid, de, a, trabajador, detalle)

    def _evento(self, c, tid, mid, de, a, trabajador, detalle):
        c.execute("INSERT INTO eventos (tarea_id, mision_id, ts, de, a, trabajador_id, detalle) VALUES (?,?,?,?,?,?,?)",
                  (tid, mid, self.reloj(), de, a, trabajador, (detalle or "")[:500]))

    def _estado_mision(self, c, mid):
        estados = [r[0] for r in c.execute("SELECT estado FROM tareas WHERE mision_id=?", (mid,)).fetchall()]
        if all(e == "verificada" for e in estados):
            nuevo = "completa"
        elif all(e in TERMINALES for e in estados) and "cancelada" in estados and "fallida" not in estados:
            nuevo = "cancelada"
        elif all(e in TERMINALES for e in estados):
            nuevo = "fallida"
        elif all(e == "pendiente" for e in estados):
            nuevo = "pendiente"
        else:
            nuevo = "en_curso"
        c.execute("UPDATE misiones SET estado=?, actualizado=? WHERE id=?", (nuevo, self.reloj(), mid))


def _sin_ciclos(deps: dict) -> None:
    visitando, hecho = set(), set()

    def visitar(n, camino):
        if n in hecho:
            return
        if n in visitando:
            raise ErrorCola("las dependencias forman un ciclo: " + " → ".join(camino + [n]))
        visitando.add(n)
        for d in deps[n]:
            visitar(d, camino + [n])
        visitando.discard(n)
        hecho.add(n)
    for n in deps:
        visitar(n, [])


def texto_cola(misiones: list[dict]) -> str:
    if not misiones:
        return "La cola está vacía."
    out = []
    for m in misiones:
        out.append(f"{m['id']} «{m['titulo']}»: {m['estado']}")
        for t in m["tareas"]:
            out.append(f"  - {t['nombre']} [{t['rol']}, {t['modelo']}]: {t['estado']} · intento {t['intentos']} de "
                       f"{t['max_intentos']}" + (f" · {t['trabajador_id']}" if t["trabajador_id"] else "")
                       + (f" · depende de {', '.join(d.split('.', 1)[1] for d in t['depende_de'])}"
                          if t["depende_de"] else "") + (f" · {t['error']}" if t["error"] else ""))
    return "\n".join(out)
