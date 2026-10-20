"""Base sintética con comportamiento CONOCIDO, para medir objetivamente a los modelos (Fase 1).

Dominio neutro (estaciones que registran lecturas): no es un sistema de precios ni de trading. Determinista por
semilla. Fenómenos sembrados, con su verdad medible:

* Distribución bimodal: la mayoría de las lecturas ~N(20, 2.5); las estaciones de la zona «alta» ~N(41, 4).
* Relación oculta: la zona (tabla estaciones) explica el segundo modo (tabla lecturas).
* Anomalías: 40 lecturas con valor 999.9 (falla de sensor).
* Estación trabada: la estación 11 registra exactamente 0.0 durante 7 días.
* Huecos: ninguna lectura el día 30 entre las 02:00 y las 09:00; la estación 5 sin lecturas los días 40 a 42.
* Llegadas tardías: «recibido» suele ser segundos después de «momento»; el 2 % llega entre 6 y 48 h tarde, y la
  estación 8 el 15 %.
* Correcciones tardías: tabla correcciones con valores corregidos días después.
* Dinámica: avanzar() agrega un día nuevo, registros tardíos dentro de días ya cerrados y más correcciones.
"""
from __future__ import annotations

import datetime as dt
import random
import secrets

BASE = dt.datetime(2026, 1, 1)
ESTACIONES = 20
ALTA = (3, 8, 13, 18)
TRABADA, TRABADA_DIAS = 11, (20, 27)
HUECO_GLOBAL = (30, 2, 9)          # día, hora desde, hora hasta
HUECO_ESTACION = (5, 40, 43)       # estación, día desde, día hasta (exclusivo)
TARDIA_ESTACION = 8
ANOMALIAS = 40


def zona(eid: int) -> str:
    return "alta" if eid in ALTA else ("norte", "sur", "costa")[eid % 3]


def _lectura(rng: random.Random, lid: int, eid: int, momento: dt.datetime) -> tuple:
    dia = (momento - BASE).days
    if eid == TRABADA and TRABADA_DIAS[0] <= dia < TRABADA_DIAS[1]:
        valor = 0.0
    else:
        valor = rng.gauss(41, 4) if eid in ALTA else rng.gauss(20, 2.5)
    p_tarde = 0.15 if eid == TARDIA_ESTACION else 0.02
    if rng.random() < p_tarde:
        recibido = momento + dt.timedelta(hours=rng.uniform(6, 48))
    else:
        recibido = momento + dt.timedelta(seconds=rng.uniform(0, 120))
    return (lid, eid, momento, round(valor, 3), recibido)


def _momentos(rng: random.Random, dia: int, n: int):
    for _ in range(n):
        m = BASE + dt.timedelta(days=dia, seconds=rng.randrange(86400))
        if dia == HUECO_GLOBAL[0] and HUECO_GLOBAL[1] <= m.hour < HUECO_GLOBAL[2]:
            continue
        yield m


def _estacion(rng: random.Random, dia: int) -> int:
    while True:
        eid = rng.randrange(1, ESTACIONES + 1)
        if not (eid == HUECO_ESTACION[0] and HUECO_ESTACION[1] <= dia < HUECO_ESTACION[2]):
            return eid


def _fmt(v) -> str:
    if isinstance(v, dt.datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def _inserts(table: str, rows: list[tuple], batch: int = 2000) -> str:
    out = []
    for i in range(0, len(rows), batch):
        out.append(f"INSERT INTO {table} VALUES " + ",".join("(" + ",".join(_fmt(x) for x in r) + ")"
                                                           for r in rows[i:i + batch]) + ";")
    return "\n".join(out)


def generar(server, database: str, *, semilla: int = 7, dias: int = 60, por_dia: int = 2000,
            usuario: str = "lab_ro", clave: str | None = None) -> dict:
    """Crea la base en un servidor MariaDB PRIVADO (server.sql como root) y un usuario de solo lectura. La clave es
    desechable (aleatoria por defecto): vale solo para ese servidor privado y nunca se guarda en el repositorio."""
    clave = clave or secrets.token_urlsafe(18)
    rng = random.Random(semilla)
    estaciones = [(e, zona(e), ("A", "B")[e % 2], BASE - dt.timedelta(days=30 + e)) for e in range(1, ESTACIONES + 1)]
    lecturas, lid = [], 0
    for dia in range(dias):
        for m in sorted(_momentos(rng, dia, por_dia)):
            lid += 1
            lecturas.append(_lectura(rng, lid, _estacion(rng, dia), m))
    for i in rng.sample(range(len(lecturas)), ANOMALIAS):
        r = lecturas[i]
        lecturas[i] = (r[0], r[1], r[2], 999.9, r[4])
    correcciones = []
    for cid, i in enumerate(rng.sample(range(len(lecturas)), max(1, len(lecturas) // 400)), 1):
        r = lecturas[i]
        correcciones.append((cid, r[0], r[3], round(r[3] + rng.uniform(-1, 1), 3),
                             r[2] + dt.timedelta(days=rng.uniform(1, 10))))
    esc = database.replace("_", "\\_")
    server.sql(f"""DROP DATABASE IF EXISTS `{database}`; CREATE DATABASE `{database}` CHARACTER SET utf8mb4;
USE `{database}`;
CREATE TABLE estaciones (id INT PRIMARY KEY, zona VARCHAR(20), modelo VARCHAR(5), alta DATETIME);
CREATE TABLE lecturas (id INT PRIMARY KEY, estacion_id INT NOT NULL, momento DATETIME NOT NULL,
  valor DECIMAL(10,3), recibido DATETIME NOT NULL, INDEX(momento), INDEX(estacion_id));
CREATE TABLE correcciones (id INT PRIMARY KEY, lectura_id INT NOT NULL, valor_anterior DECIMAL(10,3),
  valor_nuevo DECIMAL(10,3), corregido_en DATETIME NOT NULL, INDEX(lectura_id));
{_inserts('estaciones', estaciones)}
{_inserts('lecturas', lecturas)}
{_inserts('correcciones', correcciones)}
ANALYZE TABLE estaciones, lecturas, correcciones;
DROP USER IF EXISTS '{usuario}'@'localhost';
CREATE USER '{usuario}'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('{clave}');
GRANT SELECT, SHOW VIEW ON `{esc}`.* TO '{usuario}'@'localhost';""")
    return {"base": database, "semilla": semilla, "dias": dias, "por_dia": por_dia, "lecturas": len(lecturas),
            "correcciones": len(correcciones), "ultimo_id": lid, "ultima_correccion": len(correcciones),
            "usuario": usuario, "clave": clave}


def avanzar(server, info: dict, *, semilla: int = 99) -> dict:
    """Los datos cambian: un día nuevo completo, registros TARDÍOS dentro de los dos últimos días ya existentes y más
    correcciones. Devuelve la info actualizada (el día agregado es info["dias"] - 1)."""
    rng = random.Random(semilla + info["dias"])
    dia = info["dias"]
    rows, lid = [], info["ultimo_id"]
    for m in sorted(_momentos(rng, dia, info["por_dia"])):
        lid += 1
        rows.append(_lectura(rng, lid, _estacion(rng, dia), m))
    for _ in range(max(10, info["por_dia"] // 10)):  # llegan tarde: momento en días ya cerrados, recibido ahora
        lid += 1
        m = BASE + dt.timedelta(days=dia - rng.choice((1, 2)), seconds=rng.randrange(86400))
        eid = _estacion(rng, dia)
        valor = rng.gauss(41, 4) if eid in ALTA else rng.gauss(20, 2.5)
        rows.append((lid, eid, m, round(valor, 3), BASE + dt.timedelta(days=dia, hours=rng.uniform(0, 23))))
    cid = info["ultima_correccion"]
    corr = []
    for r in rng.sample(rows, min(20, len(rows))):
        cid += 1
        corr.append((cid, r[0], r[3], round(r[3] + rng.uniform(-1, 1), 3), r[4] + dt.timedelta(hours=rng.uniform(1, 30))))
    server.sql(f"USE `{info['base']}`;\n{_inserts('lecturas', rows)}\n{_inserts('correcciones', corr)}\n"
               "ANALYZE TABLE lecturas, correcciones;")
    return {**info, "dias": dia + 1, "ultimo_id": lid, "ultima_correccion": cid, "lecturas": info["lecturas"] + len(rows)}


def consultar_verdad(server, database: str, sql: str) -> list[list[str]]:
    """Verdad medida directamente sobre la base privada (como root, fuera del sandbox): filas de texto."""
    out = server.sql(sql, database=database).stdout
    return [line.split("\t") for line in out.splitlines() if line.strip()]
