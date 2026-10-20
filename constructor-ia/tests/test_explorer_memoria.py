"""Auditoría de la memoria real de 0.4.6 (19 misiones, 64 consultas, 116 hechos) — regresiones de 0.4.7.

Casos reales, reproducidos con datos SINTÉTICOS (ningún nombre ni valor de la base auditada):
* C59, C60, C61 — formas exactas: perfil(tabla, columna A), perfil(tabla, columna B) y frecuentes(tabla, columna A),
  con el SQL que genera el sistema («LIMIT 100000» en una subconsulta). Aquí: A = ticker, B = origen. Para frecuentes, 0.4.6 no conocía las filas analizadas y guardó
  completo=true, tipo=agregado_total y forma.limit=null sobre una tabla de cientos de miles de filas (estimación del motor; aquí, 875 000 sintéticas).
* F106: «inferencia respaldada» que afirmaba que C61 analizó toda la tabla (alcance agregado_total, sin declarar).
* F104: rechazada por «valores_no_encontrados» con NOMBRES de tabla y columna buscados entre los datos agregados.
* F97: 179 caracteres, frase cortada por el límite de longitud, guardada como inferencia.
* Reformulaciones del mismo resultado contadas como conocimiento nuevo; redondeos tomados como diferencias.
Los 116 hechos históricos no se cambian ni se borran: las reevaluaciones se registran aparte.
"""
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from constructor_ia import cli
from constructor_ia.db import MIGRATIONS_DIR, Database, _split_sql
from constructor_ia.explorer import knowledge as K
from constructor_ia.explorer import validation as V
from constructor_ia.explorer.mapstore import MapStore
from constructor_ia.explorer.reevaluate import Reevaluator

CAP = 100000
T, TICK, SRC, PRICE, VOL, WHEN = "cotizaciones", "ticker", "origen", "precio", "volumen", "momento"
TABLES = {T: {"tipo": "tabla", "filas_estimadas": 875000, "columnas": [
    {"nombre": c, "tipo": "varchar(20)"} for c in (TICK, SRC, PRICE, VOL, WHEN)]}}


def system_sql(tool, col):
    """El SQL EXACTO que genera el agente para una herramienta del sistema (con el tope de muestra en 100000)."""
    from types import SimpleNamespace

    from constructor_ia.explorer.agent import Explorer

    ex = Explorer.__new__(Explorer)
    ex.schema, ex.cfg = {"tablas": TABLES}, SimpleNamespace(explorer_sample_rows=CAP)
    sql, err = ex._tool_sql(tool, {"tabla": T, "columna": col})
    assert sql and not err, err
    return sql


SQL_C59 = system_sql("perfil", TICK)       # perfil(tabla, ticker)
SQL_C60 = system_sql("perfil", SRC)        # perfil(tabla, origen)
SQL_C61 = system_sql("frecuentes", TICK)   # frecuentes(tabla, ticker)
PROFILE_COLS = ["filas", "no_nulos", "distintos", "minimo", "maximo"]
C61_ROWS = [[f"T{i:02d}", 9100 - i * 300] for i in range(15)]  # 15 grupos: el LIMIT exterior los recorta


def shape(tool, col=None):
    return K.tool_shape(tool, {"tabla": T, "columna": col})


class ScopeOfExecutedSqlTests(unittest.TestCase):
    """El alcance depende del SQL REALMENTE ejecutado, incluidos los LIMIT interiores."""

    rows = {T: (875000, False)}  # estimación del motor, no exacta

    def test_exact_forms_of_c59_c60_c61(self):
        self.assertEqual(SQL_C59, f"SELECT COUNT(*) AS filas, COUNT(v) AS no_nulos, COUNT(DISTINCT v) AS distintos, "
                                  f"MIN(v) AS minimo, MAX(v) AS maximo FROM (SELECT `{TICK}` AS v FROM `{T}` LIMIT {CAP}) s")
        self.assertIn(f"SELECT `{SRC}` AS v FROM `{T}` LIMIT {CAP}", SQL_C60)
        self.assertEqual(SQL_C61, f"SELECT v AS valor, COUNT(*) AS veces FROM (SELECT `{TICK}` AS v FROM `{T}` LIMIT {CAP})"
                                  " s GROUP BY v ORDER BY veces DESC LIMIT 15")
        for sql in (SQL_C59, SQL_C60):
            self.assertEqual(K.sql_limits(sql), {"exterior": None, "interiores": [CAP]})

    def test_c59_profile_of_the_column_over_capped_subquery_is_a_sample(self):
        analyzed = K.analyzed_rows("perfil", True, PROFILE_COLS, [[CAP, CAP, 812, "T00", "T99"]], SQL_C59)
        self.assertEqual(analyzed, CAP, "100000 filas analizadas = el tope: pudo haber más")
        sc = K.scope(shape("perfil", TICK), 1, False, self.rows, analyzed, sql=SQL_C59)
        self.assertEqual((sc["tipo"], sc["completo"], sc["limite_interior"]), ("agregado_muestra", False, CAP))

    def test_c60_profile_of_another_column_over_capped_subquery_is_a_sample(self):
        analyzed = K.analyzed_rows("perfil", True, PROFILE_COLS, [[CAP, 99950, 4, "api", "ws"]], SQL_C60)
        sc = K.scope(shape("perfil", SRC), 1, False, self.rows, analyzed, sql=SQL_C60)
        self.assertEqual((sc["tipo"], sc["completo"]), ("agregado_muestra", False))
        # con menos filas analizadas que el tope, la cobertura sí está demostrada
        sc = K.scope(shape("perfil", SRC), 1, False, self.rows, 4321, sql=SQL_C60)
        self.assertEqual((sc["tipo"], sc["completo"]), ("agregado_total", True))

    def test_c61_frequencies_without_known_rows_are_not_certified_complete(self):
        # 0.4.6: analyzed=None → capped=False → agregado_total, completo=True, forma.limit=None.
        cols = ["valor", "veces"]
        analyzed = K.analyzed_rows("frecuentes", True, cols, C61_ROWS, SQL_C61)
        self.assertIsNone(analyzed, "15 grupos con LIMIT 15: no se ven todos, la suma no demuestra nada")
        sc = K.scope(shape("frecuentes", TICK), 15, False, self.rows, analyzed, sql=SQL_C61)
        self.assertEqual((sc["tipo"], sc["completo"], sc["cobertura"]), ("agregado_muestra", False, "no_demostrada"))
        self.assertEqual((sc["limite_exterior"], sc["limite_interior"]), (15, CAP))
        self.assertTrue(sc["grupos_recortados"])
        self.assertIn(f"hasta {CAP} filas", sc["descripcion"])
        self.assertIn("~875000", sc["descripcion"])
        self.assertEqual(K.sql_limits(SQL_C61), {"exterior": 15, "interiores": [CAP]})

    def test_coverage_is_certified_only_when_demonstrated(self):
        few = [["T00", 30], ["T01", 12]]  # todos los grupos a la vista y menos filas que el LIMIT interior
        n = K.analyzed_rows("frecuentes", True, ["valor", "veces"], few, SQL_C61)
        self.assertEqual(n, 42)
        sc = K.scope(shape("frecuentes", TICK), 2, False, {T: (None, False)}, n, sql=SQL_C61)
        self.assertEqual((sc["tipo"], sc["completo"]), ("agregado_total", True))
        exactly = [["T00", CAP - 10], ["T01", 10]]  # suma = tope: pudo haber más filas
        n = K.analyzed_rows("frecuentes", True, ["valor", "veces"], exactly, SQL_C61)
        self.assertFalse(K.scope(shape("frecuentes", TICK), 2, False, {}, n, sql=SQL_C61)["completo"])
        # un conteo EXACTO de la tabla menor que el tope también lo demuestra; una estimación, no
        self.assertTrue(K.scope(shape("frecuentes", TICK), 15, False, {T: (500, True)}, None, sql=SQL_C61)["completo"])
        self.assertFalse(K.scope(shape("frecuentes", TICK), 15, False, {T: (500, False)}, None, sql=SQL_C61)["completo"])

    def test_inner_limits_of_the_model_sql(self):
        for sql in (f"SELECT AVG({PRICE}) FROM (SELECT {PRICE} FROM {T} LIMIT {CAP}) s",
                    f"WITH s AS (SELECT {TICK} FROM {T} LIMIT {CAP}) SELECT {TICK}, COUNT(*) FROM s GROUP BY {TICK}",
                    f"SELECT {TICK}, COUNT(*) FROM (SELECT {TICK} FROM {T} LIMIT 0, {CAP}) s GROUP BY {TICK} LIMIT 20"):
            sh = K.sql_shape(sql, TABLES)
            sc = K.scope(sh, 3, False, self.rows, sql=sql)
            self.assertEqual((sc["tipo"], sc["completo"]), ("agregado_muestra", False), sql)
            self.assertEqual(sh["limites_interiores"], [CAP], sql)
        whole = f"SELECT {TICK}, COUNT(*) FROM {T} GROUP BY {TICK} LIMIT 20"
        sc = K.scope(K.sql_shape(whole, TABLES), 3, False, self.rows, sql=whole)
        self.assertEqual((sc["tipo"], sc["completo"]), ("agregado_total", True), "LIMIT exterior: recorta grupos, no filas")
        # un agregado total en la subconsulta con LIMIT exterior no es una muestra (falso negativo de 0.4.6)
        inner_total = f"SELECT * FROM (SELECT {TICK}, COUNT(*) c FROM {T} GROUP BY {TICK}) s LIMIT {CAP}"
        sc = K.scope(K.sql_shape(inner_total, TABLES), 3, False, self.rows, sql=inner_total)
        self.assertTrue(sc["completo"])


class ClaimScopeTests(unittest.TestCase):
    sample = {"tipo": "agregado_muestra", "completo": False, "descripcion": "cálculo sobre una muestra"}

    def test_f106_whole_table_claim_over_c61(self):
        text = f"C61 analizó toda la tabla {T}: T00 es el {TICK} más frecuente con 9100 registros."
        ok, note = K.claim_scope_check(text, None, self.sample)
        self.assertFalse(ok)
        self.assertIn("muestra", note)

    def test_explicit_whole_table_wording_overrides_the_declared_scope(self):
        for text in ("C61 analizó la totalidad de los registros", "Sobre el total de registros, T00 lidera",
                     "La tabla completa tiene 15 tickers"):
            self.assertFalse(K.claim_scope_check(text, "muestra", self.sample)[0], text)
        self.assertTrue(K.claim_scope_check("En la muestra, T00 lidera", "muestra", self.sample)[0])

    def test_undetermined_scope_is_not_certified(self):
        ok, note = K.claim_scope_check("En toda la tabla T00 lidera", None, None)
        self.assertFalse(ok)
        self.assertIn("no está determinado", note)
        self.assertTrue(K.claim_scope_check("T00 aparece 9100 veces en lo observado", None, None)[0])


class IdentifiersAndValuesTests(unittest.TestCase):
    """F104: nombres de tabla y columna no son valores de datos; los alias tampoco."""

    names = {K.fold(x) for x in (T, TICK, SRC, PRICE, VOL, WHEN)}

    def ev(self, sql=SQL_C61, cols=("valor", "veces"), rows=None):
        res = json.dumps({"columns": list(cols), "rows": rows or C61_ROWS})
        cells, columns = V.evidence_cells(res)
        return [{"id": "C61", "tipo": "consulta", "status": "ok", "row_count": 15, "utilidad": "datos",
                 "texto": V.evidence_text(res), "celdas": cells, "columnas": columns, "sql": sql}]

    def test_f104_identifiers_in_an_aggregated_result(self):
        text = f"En «{T}», «{TICK}» tiene como valor más frecuente «T00» con 9100 registros."
        self.assertEqual(V.verify_claim(text, self.ev())[:2], ("sin_verificar", "valores_no_encontrados"),
                         "sin nombres del esquema se comporta como 0.4.6")
        out = {}
        self.assertEqual(V.verify_claim(text, self.ev(), names=self.names, out=out)[:2], ("inferencia", "respaldada"))
        self.assertEqual((out["identificadores"], out["valores"]), ([T, TICK], ["T00"]))
        for quoted in (f"`{T}`.`{TICK}`", f"«{T}.{TICK}»"):
            self.assertEqual(V.verify_claim(f"En {quoted} el más frecuente es «T00» con 9100 registros.", self.ev(),
                                            names=self.names)[0], "inferencia", quoted)

    def test_schema_names_the_evidence_does_not_use_are_not_accepted(self):
        text = f"En «{T}», «{PRICE}» acompaña a «T00» con 9100 registros."
        cert, verdict, detail = V.verify_claim(text, self.ev(), names=self.names)
        self.assertEqual((cert, verdict), ("sin_verificar", "identificadores_ajenos"))
        self.assertIn(PRICE, detail)

    def test_aliases_are_names_never_data(self):
        self.assertEqual(V.verify_claim("La columna «veces» cuenta registros por «T00».", self.ev(),
                                        names=self.names)[0], "inferencia")
        # presentado como VALOR, un alias no está en los datos (0.4.6 lo encontraba en los encabezados)
        self.assertEqual(V.verify_claim("El valor «veces» es el más usado.", self.ev(), names=self.names)[:2],
                         ("sin_verificar", "valores_no_encontrados"))
        # las cifras se buscan en los datos, no en los nombres de las columnas del resultado
        ev = self.ev(sql="SELECT AVG(x) AS p95 FROM t", cols=("p95",), rows=[[3.5]])
        out = {}
        self.assertEqual(V.verify_claim("El percentil 95 vale 3.5", ev, out=out)[0], "inferencia")
        self.assertEqual(out["parametros"], ["95"], "95 es un parámetro del SQL citado, no un dato")
        self.assertEqual(V.verify_claim("El percentil 95 vale 37", ev)[1], "cifras_no_encontradas")
        out = {}
        self.assertEqual(V.verify_claim(f"C61 analizó como máximo {CAP} filas de la muestra.", self.ev(), out=out)[0],
                         "inferencia")
        self.assertEqual(out["parametros"], [str(CAP)])

    def test_configured_limit_is_not_rows_actually_analyzed(self):
        """«LIMIT 100000» demuestra un límite configurado, no que se hayan leído 100000 filas."""
        c61 = self.ev()  # frecuentes: el resultado no informa cuántas filas se analizaron
        for text in (f"C61 tiene un límite configurado de {CAP} filas.", f"C61 analizó como máximo {CAP} filas.",
                     f"La subconsulta de C61 usa un tope de {CAP} registros."):
            out = {}
            self.assertEqual(V.verify_claim(text, c61, names=self.names, out=out)[0], "inferencia", text)
            self.assertEqual(out["parametros"], [str(CAP)], text)
        for text in (f"C61 analizó {CAP} registros.", f"Se leyeron {CAP} filas de {T}.",
                     f"C61 recorrió {CAP} filas; el límite no se alcanzó."):
            cert, verdict, detail = V.verify_claim(text, c61, names=self.names)
            self.assertEqual((cert, verdict), ("sin_verificar", "cifras_no_encontradas"), text)
            self.assertIn("límite configurado no demuestra", detail)
        # con un resultado que SÍ informa las filas analizadas (perfil), la misma frase está respaldada por los datos
        res = json.dumps({"columns": PROFILE_COLS, "rows": [[CAP, CAP, 812, "T00", "T99"]]})
        cells, cols = V.evidence_cells(res)
        c59 = [{"id": "C59", "tipo": "consulta", "status": "ok", "row_count": 1, "utilidad": "datos",
                "texto": V.evidence_text(res), "celdas": cells, "columnas": cols, "sql": SQL_C59}]
        out = {}
        self.assertEqual(V.verify_claim(f"C59 analizó {CAP} registros de «{TICK}».", c59, names=self.names, out=out)[0],
                         "inferencia")
        self.assertNotIn("parametros", out, "100000 es un dato del resultado, no un parámetro")
        res = json.dumps({"columns": PROFILE_COLS, "rows": [[4321, 4321, 4, "api", "ws"]]})
        cells, cols = V.evidence_cells(res)
        small = [{**c59[0], "texto": V.evidence_text(res), "celdas": cells}]
        self.assertEqual(V.verify_claim(f"C59 analizó {CAP} registros.", small)[1], "cifras_no_encontradas",
                         "si se analizaron 4321, decir 100000 es falso aunque el LIMIT sea 100000")


class TruncationTests(unittest.TestCase):
    def test_f97_text_cut_at_the_limit(self):
        f97 = ("Las operaciones registradas en la muestra presentan un precio con dos decimales y un volumen asociado "
               "que crece de forma sostenida a lo largo de las fechas observadas, lo que sugiere que el registro se")
        f97 = f97[:179]
        self.assertEqual(len(f97), 179)
        self.assertTrue(V.is_truncated(f97, 180))
        self.assertFalse(V.is_truncated(f97 + ".", 180), "termina la frase")
        self.assertFalse(V.is_truncated("Frase corta sin punto", 180))
        complete = ("Las operaciones registradas en la muestra presentan un precio con dos decimales y un volumen "
                    "asociado; ninguna fila de las observadas tiene el precio vacío, aunque la muestra no representa "
                    "a toda la tabla ni a las fechas posteriores a las observadas.")
        self.assertLess(len(complete), 280)
        self.assertFalse(V.is_truncated(complete, 280), "con 0.4.7 una frase completa de ~250 caracteres entra")


class NoveltyTests(unittest.TestCase):
    def base(self, **kw):
        return {"id": 1, "values": {"t00", "9100"}, "consulta": 61, "tipo": "agregado_muestra", "observadas": 15,
                "mision": "M1", "ts": 1.0, **kw}

    def new(self, values, consulta=61, key=f"{T}|{TICK}", **kw):
        return {"key": key, "values": set(values), "consulta": consulta, "tipo": "agregado_muestra", "observadas": 15,
                "mision": "M1", "ts": 2.0, **kw}

    def test_restating_the_same_result_is_not_new_knowledge(self):
        self.assertEqual(K.classify(self.new({"9100", "t00"}), [self.base()])[0], "reformula")
        self.assertEqual(K.classify(self.new({"9100", "t00"}, consulta=70), [self.base()])[0], "confirma",
                         "otra consulta con lo mismo: confirma (tampoco es nuevo)")
        self.assertNotIn("reformula", K.KNOWLEDGE)
        self.assertNotIn("redondeo_compatible", K.KNOWLEDGE)

    def test_sharing_columns_is_not_equivalence(self):
        related = [self.base()]
        # misma consulta y ningún valor nuevo: reformula aunque nombre otra columna
        self.assertEqual(K.classify(self.new({"9100", "t00"}, key=f"{T}|{TICK},{WHEN}"), [], related)[0], "reformula")
        # otra consulta: compartir columnas no basta, aunque coincidan valores
        self.assertEqual(K.classify(self.new({"9100", "t00"}, consulta=64, key=f"{T}|{TICK},{WHEN}"), [], related)[0],
                         "nuevo")
        # misma consulta pero con un valor que el otro no tiene: no es una reformulación
        self.assertEqual(K.classify(self.new({"9100", "t00", "t01"}, key=f"{T}|{TICK},{WHEN}"), [], related)[0], "nuevo")

    def test_compatible_rounding_is_neither_equality_nor_difference(self):
        known = [self.base(values={"3.21"}, consulta=62, tipo="agregado_muestra", observadas=1)]
        kind, rel, why = K.classify(self.new({"3.2"}, consulta=63, observadas=1, key=f"{T}|{VOL}"), known)
        self.assertEqual((kind, rel), ("redondeo_compatible", 1))
        self.assertIn("no demuestra igualdad", why)
        # 3.3 no es redondeo de 3.21: sigue siendo una diferencia (aquí, de otro momento)
        self.assertEqual(K.classify(self.new({"3.3"}, consulta=63, observadas=1, mision="M2"), known)[0],
                         "difiere_tiempo")
        self.assertIsNone(K.rounding_of("3.21", {"3.2"}), "un valor más preciso no es redondeo de uno menos preciso")


# --- Memoria sintética en estado 0.4.6 (116 hechos) y su reevaluación ------------------------------

def migrate_to(path: Path, version: int) -> None:
    c = sqlite3.connect(path)
    for f in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if int(f.name[:4]) <= version:
            for stmt in _split_sql(f.read_text(encoding="utf-8")):
                c.execute(stmt)
            c.execute(f"PRAGMA user_version = {int(f.name[:4])}")
    c.commit()
    c.close()


def scope_046(tool, col, sql, rows, tipo, completo, extra=None):
    forma = {**shape(tool, col)}
    sc = {"operacion": forma["operacion"], "tablas": [T], "columnas": [col] if col else [], "filas_observadas": rows,
          "filas_tabla": 875000, "filas_tabla_exactas": False, "completo": completo, "tipo": tipo,
          "descripcion": "cálculo sobre la tabla completa" if completo else "cálculo sobre una muestra limitada de la tabla, no sobre toda",
          **(extra or {})}
    return json.dumps({"forma": forma, **sc})


QUERIES = [  # (id, tool, sql, columns, rows, alcance_json guardado por 0.4.6)
    (59, "perfil", SQL_C59, PROFILE_COLS, [[CAP, CAP, 812, "T00", "T99"]],
     scope_046("perfil", TICK, SQL_C59, 1, "agregado_muestra", False, {"filas_analizadas": CAP})),
    (60, "perfil", SQL_C60, PROFILE_COLS, [[CAP, 99950, 4, "api", "ws"]],
     scope_046("perfil", SRC, SQL_C60, 1, "agregado_muestra", False, {"filas_analizadas": CAP})),
    # C61 tal como lo guardó 0.4.6: completo=true, agregado_total, forma.limit=null
    (61, "frecuentes", SQL_C61, ["valor", "veces"], C61_ROWS, scope_046("frecuentes", TICK, SQL_C61, 15, "agregado_total", True)),
    (62, "sql", f"SELECT AVG({VOL}) AS medio FROM (SELECT {VOL} FROM {T} LIMIT 1000) s", ["medio"], [["3.21"]], None),
    (63, "sql", f"SELECT AVG({VOL}) AS medio FROM (SELECT {VOL} FROM {T} ORDER BY {WHEN} DESC LIMIT 1000) s", ["medio"],
     [["3.2"]], None),
    (64, "sql", f"SELECT {TICK}, MAX({WHEN}) AS ultimo, COUNT(*) AS veces FROM {T} WHERE {WHEN} > '2026-01-01' "
                f"GROUP BY {TICK} LIMIT 20", [TICK, "ultimo", "veces"], [["T00", "2026-03-01 10:00:00", 9100]], None),
]


def claim(text, cert, ev, q, key=None, verdict="respaldada", aporte="nuevo", **extra):
    data = {"verificacion": verdict, "detalle": "x", "clave": key, "valores": sorted(K.claim_values(text)),
            "aporte": aporte if cert == "inferencia" else "sin_respaldo",
            "observacion": {"consulta": q, "tipo": None, "filas": None, "mision": "M20261019-a1", "ts": 1.0}, **extra}
    return ("hallazgo", cert, text, data, ev)


F97 = ("Las operaciones registradas en la muestra presentan un precio con dos decimales y un volumen asociado que "
       "crece de forma sostenida a lo largo de las fechas observadas, lo que sugiere que el registro se")[:179]
SPECIAL = {
    97: claim(F97, "inferencia", ["C60"], 60, f"{T}|{SRC}", alcance="cálculo sobre una muestra"),
    104: claim(f"En «{T}», la columna «{SRC}» tiene 100000 filas analizadas y 99950 con valor.", "sin_verificar", ["C60"],
               60, f"{T}|{SRC}", verdict="valores_no_encontrados"),
    106: claim(f"C61 analizó toda la tabla «{T}»: «T00» es el {TICK} más frecuente con 9100 registros.", "inferencia",
               ["C61"], 61, f"{T}|{TICK}", alcance="cálculo sobre la tabla completa"),
    107: claim(f"«T00» encabeza los valores de «{TICK}» con 9100 registros.", "inferencia", ["C61"], 61, f"{T}|{TICK}"),
    108: claim("Con 9100 registros, «T00» es el ticker más frecuente.", "inferencia", ["C61"], 61, f"{T}|{TICK}"),
    109: claim("El volumen medio de la muestra fue 3.21.", "inferencia", ["C62"], 62, f"{T}|{VOL}"),
    110: claim("El volumen medio de la muestra fue 3.2.", "inferencia", ["C63"], 63, f"{T}|{VOL}", aporte="difiere_tiempo"),
    111: claim(f"«T01» aparece 8800 veces en «{TICK}».", "inferencia", ["C61"], 61, f"{T}|{TICK}", aporte="amplia"),
    112: claim(f"Con 9100 registros desde enero, «T00» lidera según «{WHEN}».", "inferencia", ["C64"], 64,
               f"{T}|{TICK},{WHEN}"),
    113: ("incertidumbre", "hipotesis", ("No queda claro si el volumen asociado a cada operación se registra en la "
                                        "misma unidad para todos los tickers observados en la muestra ni desde cuándo")[:140],
          None, []),
}
EXPECTED = [("consulta", 61, "alcance_consulta", None, None),
            ("hecho", 97, "texto_incompleto", "inferencia", "hipotesis"),
            ("hecho", 104, "verificacion", "sin_verificar", "inferencia"),
            ("hecho", 106, "alcance_afirmacion", "inferencia", "hipotesis"),
            ("hecho", 108, "aporte", "inferencia", "inferencia"),
            ("hecho", 110, "aporte", "inferencia", "inferencia"),
            ("hecho", 113, "texto_incompleto", "hipotesis", "hipotesis")]


def build_046_memory(path: Path) -> None:
    migrate_to(path, 6)
    c = sqlite3.connect(path)
    now = time.time() - 86400
    c.execute("INSERT INTO explore_snapshots(conn, mission_id, fingerprint, schema_json, observed_at) VALUES(?,?,?,?,?)",
              ("cueva", "M20261019-a1", "f", json.dumps({"tablas": TABLES}), now))
    for qid, tool, sql, cols, rows, sc in QUERIES:
        if sc is None:  # SQL del modelo: el alcance que 0.4.6 ya calculaba bien para el SQL propio
            sh = K.sql_shape(sql, TABLES)
            sc = json.dumps({"forma": sh, **K.scope(sh, len(rows), False, {T: (875000, False)})})
        c.execute("INSERT INTO explore_queries(id, conn, mission_id, origin, tool, sql_text, status, row_count, truncated,"
                  " result_json, alcance_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, "cueva", "M20261019-a1", "agente", tool, sql, "ok", len(rows), 0,
                   json.dumps({"columns": cols, "rows": rows}), sc, now))
    for fid in range(1, 117):
        if fid in SPECIAL:
            kind, cert, text, data, ev = SPECIAL[fid]
        elif fid % 3:
            kind, cert, text, data, ev = ("estructura", "comprobado", f"Hecho de estructura número {fid}.", None, ["C1"])
        else:  # hallazgos respaldados que no exceden su evidencia ni están cortados
            kind, cert, text, data, ev = claim(f"C59 contó {CAP} filas en la muestra de {T}" + "." * (fid % 7 + 1),
                                               "inferencia", ["C59"], 59, None, aporte=None)
        c.execute("INSERT INTO explore_facts(id, conn, mission_id, last_mission_id, kind, certainty, subject, text,"
                  " data_json, evidence_json, clave, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (fid, "cueva", "M20261019-a1", "M20261019-a1", kind, cert,
                   f"{kind}:{hashlib.sha1(text.lower().encode()).hexdigest()[:16]}" if kind != "estructura" else f"tabla:{fid}",
                   text,
                   json.dumps(data) if data else None, json.dumps(ev), (data or {}).get("clave"), now + fid, now + fid))
        c.execute("INSERT INTO explore_fact_history(fact_id, conn, mission_id, event, certainty_before, certainty_after,"
                  " text, evidence_json, data_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (fid, "cueva", "M20261019-a1", "creado", None, cert, text, json.dumps(ev),
                   json.dumps(data) if data else None, now + fid))
    c.commit()
    c.close()


def snapshot_facts(path: Path) -> list:
    c = sqlite3.connect(path)
    rows = c.execute("SELECT id, conn, mission_id, last_mission_id, kind, certainty, subject, text, data_json,"
                     " evidence_json, clave, status, created_at, updated_at FROM explore_facts ORDER BY id").fetchall()
    c.close()
    return rows


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReevaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "constructor.db"
        build_046_memory(self.path)
        self.before = snapshot_facts(self.path)
        self.assertEqual(len(self.before), 116)

    def plan_of(self, db):
        return [(it["objeto"], it["id"], it["regla"], it["certeza_anterior"], it["certeza_nueva"])
                for it in Reevaluator(db, "cueva").plan()]

    def test_simulation_reads_only(self):
        h = digest(self.path)
        db = Database(self.path, readonly=True)
        items = Reevaluator(db, "cueva").plan()
        db.close()
        self.assertEqual([(i["objeto"], i["id"], i["regla"], i["certeza_anterior"], i["certeza_nueva"]) for i in items],
                         EXPECTED)
        self.assertEqual(digest(self.path), h, "la simulación no escribe nada (ni la migración 0007)")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["constructor.db"])
        with self.assertRaises(sqlite3.OperationalError):
            Database(self.path, readonly=True).execute("UPDATE explore_facts SET text='x' WHERE id=1")
        by_id = {(i["objeto"], i["id"]): i for i in items}
        c61 = by_id[("consulta", 61)]
        self.assertEqual((c61["antes"]["tipo"], c61["antes"]["completo"], c61["antes"]["limit"]),
                         ("agregado_total", True, None))
        after = c61["despues"]["alcance"]
        self.assertEqual((after["tipo"], after["completo"], after["forma"]["limit"], after["forma"]["limites_interiores"]),
                         ("agregado_muestra", False, 15, [CAP]))
        self.assertIn("muestra", by_id[("hecho", 106)]["detalle"])
        self.assertEqual(by_id[("hecho", 104)]["despues"]["literales"]["identificadores"], [T, SRC])
        self.assertEqual(by_id[("hecho", 104)]["despues"]["aporte"], "nuevo")
        self.assertEqual((by_id[("hecho", 108)]["veredicto"], by_id[("hecho", 108)]["despues"]["relacion_con"]),
                         ("reformula", 107))
        self.assertEqual(by_id[("hecho", 110)]["veredicto"], "redondeo_compatible")

    def test_apply_requires_the_simulated_plan_and_keeps_the_116_facts(self):
        sim = Reevaluator(Database(self.path, readonly=True), "cueva")
        code = sim.code(sim.plan())
        db = Database(self.path)
        self.assertEqual(db.migrate(), 9)  # 0008 (investigador) y 0009 (instrumentos) solo agregan tablas
        rv = Reevaluator(db, "cueva")
        with self.assertRaises(ValueError):
            rv.apply(rv.plan(), "000000000000", "prueba")
        self.assertEqual(db.one("SELECT COUNT(*) FROM explore_reviews")[0], 0, "sin el código del plan no se escribe")
        self.assertEqual(rv.apply(rv.plan(), code, "prueba"), len(EXPECTED))
        self.assertEqual(snapshot_facts(self.path), self.before, "texto, certeza original y procedencia intactos")
        self.assertEqual(db.one("SELECT COUNT(*) FROM explore_reviews")[0], len(EXPECTED))
        maps = MapStore(db)
        facts = {f["id"]: f for f in maps.facts("cueva", limit=500)}
        self.assertEqual(len(facts), 116)
        self.assertEqual((facts[106]["certainty"], facts[106]["certeza_original"]), ("hipotesis", "inferencia"))
        self.assertEqual((facts[97]["certainty"], facts[104]["certainty"]), ("hipotesis", "inferencia"))
        self.assertEqual(facts[106]["reevaluacion"]["regla"], "alcance_afirmacion")
        untouched = [f for fid, f in facts.items() if fid not in {97, 104, 106, 108, 110, 113}]
        self.assertTrue(all(f["certainty"] == f["certeza_original"] and "reevaluacion" not in f for f in untouched))
        hist = maps.history("cueva", 106)
        self.assertEqual([e["event"] for e in hist], ["creado", "reevaluado"])
        self.assertEqual((hist[-1]["certainty_before"], hist[-1]["certainty_after"]), ("inferencia", "hipotesis"))
        self.assertEqual(hist[-1]["data"]["plan"], code)
        self.assertEqual(maps.query_scope("cueva", 61)["tipo"], "agregado_muestra")
        self.assertEqual(json.loads(db.one("SELECT alcance_json FROM explore_queries WHERE id=61")[0])["tipo"],
                         "agregado_total", "el alcance original de la consulta se conserva")
        self.assertEqual(Reevaluator(db, "cueva").plan(), [], "aplicada, la reevaluación no se repite")

    def test_new_evidence_after_a_review_follows_the_effective_certainty(self):
        sim = Reevaluator(Database(self.path, readonly=True), "cueva")
        code = sim.code(sim.plan())
        db = Database(self.path)
        db.migrate()
        rv = Reevaluator(db, "cueva")
        rv.apply(rv.plan(), code, "prueba")
        maps = MapStore(db)
        text = SPECIAL[106][2]
        # La misma afirmación con respaldo de hipótesis: no sube ni baja (la efectiva ya es hipótesis).
        fid, outcome = maps.record_note("cueva", "hallazgo", text, evidence=["C99"], mission_id="M2", certainty="hipotesis")
        self.assertEqual((fid, outcome), (106, "confirmado"))
        # Con una evidencia nueva que sí la respalde, la certeza efectiva sube; la columna original no cambia.
        fid, outcome = maps.record_note("cueva", "hallazgo", text, evidence=["C100"], mission_id="M3",
                                        certainty="inferencia")
        self.assertEqual(outcome, "certeza_subio")
        self.assertEqual(maps.effective(106), "inferencia")
        self.assertEqual(db.one("SELECT certainty, text FROM explore_facts WHERE id=106")[:], ("inferencia", text))

    def test_cli_simulates_by_default_and_applies_only_with_the_plan(self):
        cfgp = self.tmp / "config.toml"
        cfgp.write_text(f'[server]\ndata_dir = "{self.tmp}"\n')
        h = digest(self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(cfgp), "explorer-reevaluar", "cueva"]), 0)
        text = out.getvalue()
        self.assertIn("SIMULACIÓN — no se escribió nada. 7 reevaluaciones", text)
        self.assertIn("F106 [alcance_afirmacion] inferencia → hipotesis", text)
        self.assertEqual(digest(self.path), h)
        code = text.split("Plan ")[1].split(".")[0]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(cfgp), "explorer-reevaluar", "cueva", "--aplicar"]), 2)
        self.assertIn("exige --plan", err.getvalue())
        self.assertEqual(digest(self.path), h, "sin plan no se aplica ni se hace copia")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(cfgp), "explorer-reevaluar", "cueva", "--solo", "F106,C61",
                                       "--aplicar", "--plan", code]), 1, "el código de otro plan no autoriza un subconjunto")
        sub = io.StringIO()
        with contextlib.redirect_stdout(sub):
            cli.main(["--config", str(cfgp), "explorer-reevaluar", "cueva", "--solo", "F106,C61"])
        sub_code = sub.getvalue().split("Plan ")[1].split(".")[0]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(cfgp), "explorer-reevaluar", "cueva", "--solo", "F106,C61",
                                       "--aplicar", "--plan", sub_code]), 0)
        self.assertIn("Aplicadas 2 reevaluaciones", out.getvalue())
        self.assertTrue(list((self.tmp / "backups").glob("constructor-*-pre-reevaluacion.db")), "copia previa")
        self.assertEqual(snapshot_facts(self.path), self.before)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", str(cfgp), "explorer-hecho", "cueva", "106"])
        self.assertIn("F106 [hallazgo · hipotesis (original: inferencia)", out.getvalue())
        self.assertIn("reevaluado (alcance_afirmacion, 0.4.7", out.getvalue())



# --- De punta a punta: el agente real sobre el MariaDB privado de pruebas --------------------------

from . import mariadb_fixture  # noqa: E402
from .helpers import wait_for  # noqa: E402
from .test_explorer_regresion import Base, Script  # noqa: E402

CUT = ("Los registros observados describen operaciones con una referencia y una cantidad asociada, con fechas "
       "distribuidas en varios meses y montos que crecen de manera regular según el identificador, lo que indica una "
       "carga ordenada de datos de prueba y una relación estable entre ambas columnas que conviene medir mejor con "
       "otras herramientas antes de concluir algo sobre su origen")[:280]


def step(tool, busca, **kw):
    return lambda p, i: {"pregunta": f"¿Qué muestra {tool}?", "por_que": "medir", "evidencia": [],
                         "herramienta": tool, "busca": busca, **kw}


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class EndToEndMemoryTests(Base):
    def test_capped_system_tools_identifiers_truncation_and_restatements(self):
        c, r = self.cave.child, self.cave.ref
        rid = f"{r}_id"
        seen = []

        def interpret(prompt, info):
            q = info["q"]
            seen.append(q)
            ev = [f"C{q}"]
            n = len(seen)
            if n == 1:  # «C59»: perfil(tabla, A) — frase cortada en el límite
                return {"resumen": "Conteo sobre la muestra.", "hallazgos": [
                    {"texto": CUT, "tipo": "hallazgo", "evidencia": ev, "alcance": "muestra"}]}
            if n == 2:  # «C60»: perfil(tabla, B) — reformula la estadística que el sistema acaba de calcular
                return {"resumen": "Rango de cantidad en la muestra.", "hallazgos": [
                    {"texto": "En la muestra, cantidad va de 3 a 300 con promedio 151.5.", "tipo": "hallazgo",
                     "evidencia": ev, "alcance": "muestra"}]}
            if n == 3:  # «C61»: frecuentes con LIMIT interior — F106 (toda la tabla) y F104 (identificadores)
                return {"resumen": "Frecuencias de la referencia.", "hallazgos": [
                    {"texto": f"C{q} analizó toda la tabla {c}: los valores de «{rid}» aparecen 20 veces cada uno.",
                     "tipo": "hallazgo", "evidencia": ev},
                    {"texto": f"En la muestra de «{c}», «{rid}» reparte 20 registros por valor.", "tipo": "hallazgo",
                     "evidencia": ev, "alcance": "muestra"}]}
            return {"resumen": "Descripciones de la tabla de referencia.", "hallazgos": [  # cobertura demostrada
                {"texto": f"En toda la tabla {r}, «cat 1» es una de las descripciones.", "tipo": "hallazgo",
                 "evidencia": ev, "alcance": "tabla"},
                {"texto": f"La descripción «cat 1» figura en toda la tabla {r}.", "tipo": "hallazgo", "evidencia": ev,
                 "alcance": "tabla"}]}

        app, fake, spec = self.make(self.cave, [])
        app.cfg.explorer_sample_rows = 100  # la tabla hija tiene 300 filas: el tope interior recorta
        fake.default = Script([
            step("perfil", "valores_distintos", tabla=c, columna=rid, columna_objetivo=rid),  # C59: perfil(tabla, A)
            step("perfil", "rango_nulos", tabla=c, columna="cantidad", columna_objetivo="cantidad"),  # C60: perfil(tabla, B)
            step("frecuentes", "frecuencias", tabla=c, columna=rid, columna_objetivo=rid),  # C61: frecuentes(tabla, A)
            step("frecuentes", "frecuencias", tabla=r, columna="descripcion", columna_objetivo="descripcion"),
            lambda p, i: {"pregunta": "fin", "por_que": "listo", "evidencia": [], "herramienta": "terminar",
                          "busca": "otro"}], report=None, interpret=interpret)
        mid = self.start_mission(app, spec)
        wait_for(lambda: app.missions.get(mid)["state"] in ("completada", "fallida", "cancelada"), timeout=120)
        self.app, self.mid = app, mid
        qs = [dict(x) for x in app.db.all("SELECT id, tool, sql_text, alcance_json FROM explore_queries WHERE"
                                          " mission_id=? AND origin='agente' ORDER BY id", (mid,))]
        self.assertEqual([q["tool"] for q in qs], ["perfil", "perfil", "frecuentes", "frecuentes"])
        sc = [json.loads(q["alcance_json"]) for q in qs]
        self.assertEqual([(x["tipo"], x["completo"]) for x in sc[:3]], [("agregado_muestra", False)] * 3,
                         "C59, C60 y C61: el LIMIT interior del SQL ejecutado impide certificar la tabla completa")
        self.assertEqual((sc[2]["forma"]["limit"], sc[2]["forma"]["limites_interiores"]), (15, [100]),
                         "0.4.6 guardaba forma.limit=null para frecuentes")
        self.assertIn("LIMIT 100", qs[2]["sql_text"])
        self.assertEqual((sc[3]["tipo"], sc[3]["completo"], sc[3]["filas_analizadas"]), ("agregado_total", True, 5),
                         "con todos los grupos a la vista y menos filas que el tope, la cobertura está demostrada")
        facts = {f["text"]: f for f in self.facts()}
        cut = facts[" ".join(CUT.split())]  # se guarda con los espacios normalizados
        self.assertEqual((cut["certainty"], cut["data"]["verificacion"]), ("hipotesis", "texto_incompleto"))
        f106 = next(f for t, f in facts.items() if "analizó toda la tabla" in t)
        self.assertEqual((f106["certainty"], f106["data"]["verificacion"]),
                         ("hipotesis", "generaliza_mas_alla_de_la_evidencia"))
        f104 = next(f for t, f in facts.items() if "reparte 20 registros" in t)
        self.assertEqual(f104["certainty"], "inferencia", f104["data"])
        self.assertEqual(sorted(f104["data"]["literales"]["identificadores"]), sorted([c, rid]))
        restated = next(f for t, f in facts.items() if "va de 3 a 300" in t)
        self.assertEqual(restated["data"]["aporte"], "reformula")
        whole = next(f for t, f in facts.items() if t.startswith("En toda la tabla"))
        self.assertEqual((whole["certainty"], whole["data"]["aporte"]), ("inferencia", "nuevo"))
        again = next(f for t, f in facts.items() if t.startswith("La descripción"))
        self.assertEqual((again["data"]["aporte"], again["data"]["relacion_con"]), ("reformula", whole["id"]))
        mt = json.loads(app.db.one("SELECT metrics_json FROM explore_runs WHERE mission_id=?", (mid,))[0])
        # unidades: la estadística nueva del sistema (reformulada), F104 y la descripción (reformulada una vez)
        self.assertEqual((mt["hechos_nuevos"], mt["reformulaciones"], mt["afirmaciones_truncadas"]), (3, 2, 1))
        informe = app.files.read_rel(mid, "exploracion/informe.md")
        self.assertIn("Reformulaciones y confirmaciones (no suman conocimiento nuevo)", informe)
        self.assertIn("[texto cortado]", informe)


if __name__ == "__main__":
    unittest.main()
