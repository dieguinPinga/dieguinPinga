"""Agente de datos, fase 1 (AGENTE_DATOS.md): arquitectura completa SIN Qwen.

Con MariaDB real (privado y desechable), el intermediario de solo lectura del Explorador y un MODELO DE GUION. Valida el
CIRCUITO: cartógrafo, vigencia del esquema y de los datos, microtareas, verificación técnica y semántica separadas,
memoria persistente entre PROCESOS distintos, juez con verdad oculta y auditoría de fugas. No dice nada sobre la
capacidad de Qwen: el guion está escrito para ejercitar el circuito con la base B1.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from constructor_ia.agente_datos import generador as G
from constructor_ia.agente_datos import juez as J
from constructor_ia.agente_datos import medir as MD
from constructor_ia.agente_datos import microtareas as MT
from constructor_ia.agente_datos import texto as TX
from constructor_ia.agente_datos import verificador as VE
from constructor_ia.agente_datos.coordinador import Coordinador
from constructor_ia.agente_datos.guion import Guion
from constructor_ia.agente_datos.memoria import ESQUEMA_MEM, Memoria

from . import mariadb_fixture
from .helpers import make_cfg

RAIZ = Path(__file__).resolve().parent.parent
Q1, Q2 = G.PREGUNTAS["Q1"], G.PREGUNTAS["Q2"]
Q3 = "¿Qué producto aparece en más pedidos en marzo de 2026?"


def esquema_falso():
    def col(n, t, k=""):
        return {"nombre": n, "tipo": t, "nulo": False, "clave": k, "extra": "", "comentario": ""}
    return {"tablas": {
        "ventas": {"columnas": [col("id", "int(11)", "PRI"), col("orden_id", "int(11)"), col("articulo_id", "int(11)"),
                                col("unidades", "int(11)"), col("precio", "decimal(10,2)")], "indices": {},
                   "claves_foraneas": []},
        "ordenes": {"columnas": [col("id", "int(11)", "PRI"), col("momento", "datetime"), col("dia", "date")],
                    "indices": {}, "claves_foraneas": []},
        "articulos": {"columnas": [col("id", "int(11)", "PRI"), col("titulo", "varchar(40)")], "indices": {},
                      "claves_foraneas": []},
        "suelta": {"columnas": [col("x", "int(11)")], "indices": {}, "claves_foraneas": []}}}


RELS = [{"tabla": "ventas", "columna": "orden_id", "tabla_ref": "ordenes", "columna_ref": "id", "contencion": True,
         "unico": True, "origen": "declarada"},
        {"tabla": "ventas", "columna": "articulo_id", "tabla_ref": "articulos", "columna_ref": "id",
         "contencion": True, "unico": True, "origen": "convencion"}]


class TextoTests(unittest.TestCase):
    def test_question_reading_is_generic(self):
        self.assertEqual(TX.periodo(Q1)["desde"], dt.datetime(2026, 3, 1))
        self.assertEqual(TX.periodo(Q1)["hasta"], dt.datetime(2026, 4, 1))
        self.assertEqual(TX.periodo("¿Cuántas ventas hubo en diciembre de 2025?")["hasta"], dt.datetime(2026, 1, 1))
        self.assertTrue(TX.periodo("entre marzo de 2026 y abril de 2026")["varios"])
        self.assertIsNone(TX.periodo("¿Qué producto es el más caro?"))
        self.assertEqual((TX.entidad(Q1), TX.terminos(Q1), TX.intencion(Q1)["orden"]), ("producto", ["vend", "unid"],
                                                                                        "mayor"))
        self.assertEqual((TX.entidad(Q2), TX.terminos(Q2)), ("cliente", ["gast"]))
        self.assertEqual(TX.intencion("¿Cuántos pedidos hubo?")["agregado"], "conteo")


class MedirTests(unittest.TestCase):
    def plan(self, texto):
        return MD.compilar(MD.parsear(texto), esquema_falso(), RELS)

    def test_joins_follow_only_verified_relations_many_to_one(self):
        c = self.plan("MEDIR: agregado\nMEDIDA: suma ventas.unidades\nAGRUPAR: articulos.titulo\n"
                      "FILTRO: ordenes.momento desde 2026-03-01 hasta 2026-04-01\nORDEN: mayor\nLIMITE: 1")
        self.assertEqual(c["raiz"], "ventas")
        self.assertFalse(c["puede_multiplicar"])
        self.assertIn("JOIN `articulos` t1 ON t0.`articulo_id` = t1.`id`", c["sql"])
        self.assertIn("t2.`momento` >= '2026-03-01 00:00:00' AND t2.`momento` < '2026-04-01 00:00:00'", c["sql"])
        self.assertTrue(c["sql"].endswith("ORDER BY valor DESC, g1 LIMIT 1"))
        sin = [dict(r, contencion=False) if r["tabla_ref"] == "articulos" else r for r in RELS]
        with self.assertRaisesRegex(MD.Aclaracion, "no hay una relación comprobada"):
            MD.compilar(MD.parsear("MEDIDA: suma ventas.unidades\nAGRUPAR: articulos.titulo"), esquema_falso(), sin)
        c = self.plan("MEDIDA: conteo ordenes.id\nAGRUPAR: articulos.titulo")
        self.assertTrue(c["puede_multiplicar"], "ordenes → ventas es uno a muchos")

    def test_names_are_exact_and_contradictions_are_rejected(self):
        with self.assertRaisesRegex(MD.Aclaracion, "la columna «ventas.cantidad» no existe; columnas de ventas: id"):
            self.plan("MEDIDA: suma ventas.cantidad")
        with self.assertRaisesRegex(MD.Aclaracion, "valores distintos"):
            MD.parsear("MEDIDA: suma ventas.unidades\nMEDIDA: suma ventas.precio")
        with self.assertRaisesRegex(MD.Aclaracion, "numérica"):
            self.plan("MEDIDA: suma articulos.titulo")

    def test_effective_intervals_detect_the_classic_datetime_error(self):
        malo = self.plan("MEDIDA: suma ventas.unidades\nFILTRO: ordenes.momento entre 2026-03-01 y 2026-03-31")
        self.assertEqual(malo["intervalos"][0]["fin"], dt.datetime(2026, 3, 31))
        self.assertTrue(malo["intervalos"][0]["fin_incluido"])
        bien = self.plan("MEDIDA: suma ventas.unidades\nFILTRO: ordenes.dia entre 2026-03-01 y 2026-03-31")
        self.assertEqual((bien["intervalos"][0]["fin"], bien["intervalos"][0]["fin_incluido"]),
                         (dt.datetime(2026, 4, 1), False), "en DATE, «entre» abarca días completos")
        fichas = {}
        s1 = {x["id"]: x for x in VE.semantica("¿Cuántas unidades se vendieron en marzo de 2026?", malo,
                                               esquema_falso(), fichas, [], {})}["S1_periodo"]
        self.assertEqual(s1["estado"], "falla")
        self.assertIn("deja afuera casi todo ese día", s1["detalle"])
        s1 = {x["id"]: x for x in VE.semantica("¿Cuántas unidades se vendieron en marzo de 2026?", bien,
                                               esquema_falso(), fichas, [], {})}["S1_periodo"]
        self.assertEqual(s1["estado"], "ok")


class VerifiedOnlyWithSchemaSupportTests(unittest.TestCase):
    """«Verificada» exige respaldo del esquema (o del glosario verificado): una ficha del modelo no alcanza."""

    def setUp(self):
        self.c = MD.compilar(MD.parsear("MEDIDA: suma ventas.unidades\nAGRUPAR: articulos.titulo\n"
                                        "FILTRO: ordenes.momento desde 2026-03-01 hasta 2026-04-01\nORDEN: mayor\n"
                                        "LIMITE: 1"), esquema_falso(), RELS)
        self.perfiles = {"articulos": {"filas": 5, "columnas": {"titulo": {"distintos": 5}}}}
        self.fichas = {"ventas": {"id": 1, "fila": "unidades vendidas de un artículo", "roles": {"unidades": {"rol": "medida"}}},
                       "ordenes": {"id": 2, "fila": "una orden", "roles": {"momento": {"rol": "tiempo"}}},
                       "articulos": {"id": 3, "fila": "un artículo", "roles": {"titulo": {"rol": "texto"}}}}

    def test_schema_support_verifies(self):
        cheq = VE.semantica("¿Qué artículo tiene más unidades en marzo de 2026?", self.c, esquema_falso(), self.fichas,
                            [], self.perfiles, {"limite": 1})
        self.assertEqual(VE.nivel(cheq)[0], "verificada", cheq)

    def test_ficha_only_support_stays_uncertain(self):
        cheq = {x["id"]: x for x in VE.semantica("¿Qué artículo vendió más en marzo de 2026?", self.c, esquema_falso(),
                                                 self.fichas, [], self.perfiles, {"limite": 1})}
        self.assertEqual(cheq["S4_terminos"]["estado"], "no_verificable")
        self.assertIn("respaldados solo por una ficha del modelo: «vend…»", cheq["S4_terminos"]["detalle"])
        self.assertEqual(VE.nivel(list(cheq.values()))[0], "incierta")
        glos = [{"id": 7, "raiz": "vend", "objeto": "ventas.unidades", "origen": "verificada", "misiones": ["M0"]}]
        cheq = VE.semantica("¿Qué artículo vendió más en marzo de 2026?", self.c, esquema_falso(), self.fichas, glos,
                            self.perfiles, {"limite": 1})
        self.assertEqual(VE.nivel(cheq)[0], "verificada", "un término del glosario VERIFICADO sí respalda")
        glos[0]["origen"] = "declarada"
        cheq = VE.semantica("¿Qué artículo vendió más en marzo de 2026?", self.c, esquema_falso(), self.fichas, glos,
                            self.perfiles, {"limite": 1})
        self.assertEqual(VE.nivel(cheq)[0], "incierta", "un vínculo solo declarado por el modelo no respalda")

    def test_limit_is_checked_against_the_question(self):
        cheq = {x["id"]: x for x in VE.semantica("¿Qué artículo tiene más unidades en marzo de 2026?", self.c,
                                                 esquema_falso(), self.fichas, [], self.perfiles, {"limite": 3})}
        self.assertEqual(cheq["S2_intencion"]["estado"], "falla")
        self.assertIn("la pregunta pide 3 resultado(s) y la consulta devuelve 1", cheq["S2_intencion"]["detalle"])


class Res:
    def __init__(self, columns, rows, status="ok", truncated=False):
        self.columns, self.rows, self.status, self.truncated, self.error = columns, rows, status, truncated, ""


class InvariantTests(unittest.TestCase):
    """Las invariantes tienen que ser matemáticamente válidas: una SUMA puede superar la cantidad de filas."""

    def setUp(self):
        self.c = MD.compilar(MD.parsear("MEDIDA: suma ventas.precio\nAGRUPAR: articulos.titulo\nORDEN: mayor\n"
                                        "LIMITE: 1"), esquema_falso(), RELS)

    def correr(self, grupos, total, ganador, filas=(3, 3)):
        def run(sql, herramienta):
            return {"multiplicacion": Res(["filas", "distintas"], [list(filas)]),
                    "grupos": Res(["g1", "valor", "n"], grupos),
                    "total": Res(["valor", "n", "minimo", "maximo"], [total]),
                    "contraprueba": Res(["valor"], [[ganador]])}[herramienta]
        o = Res(["g1", "valor"], [grupos[0][:2]])
        return {x["id"]: x for x in VE.invariantes(run, self.c, o)}

    def test_a_sum_larger_than_the_row_count_is_valid(self):
        r = self.correr([["a", "1500000.00", 2], ["b", "900.50", 1]], ["1500900.50", 3, "900.50", "800000.00"],
                        "1500000.00")
        self.assertEqual({k: v["estado"] for k, v in r.items()},
                         {"multiplicacion": "ok", "particion": "ok", "cotas": "ok", "ranking": "ok", "empate": "ok",
                          "contraprueba": "ok"})

    def test_each_invariant_fails_only_when_the_arithmetic_is_wrong(self):
        r = self.correr([["a", "10", 2], ["b", "5", 1]], ["16", 3, "1", "6"], "10")
        self.assertEqual(r["particion"]["estado"], "falla", "10 + 5 ≠ 16")
        r = self.correr([["a", "30", 2], ["b", "5", 1]], ["35", 3, "1", "6"], "30")
        self.assertEqual(r["cotas"]["estado"], "falla", "2 filas de a lo sumo 6 no suman 30")
        r = self.correr([["a", "10", 2], ["b", "12", 2]], ["22", 4, "1", "6"], "10")
        self.assertEqual(r["ranking"]["estado"], "falla", "el primero informado no es el máximo")
        r = self.correr([["a", "10", 2], ["b", "10", 2]], ["20", 4, "1", "6"], "10")
        self.assertEqual(r["empate"]["estado"], "no_verificable")
        r = self.correr([["a", "10", 2], ["b", "5", 1]], ["15", 3, "1", "6"], "9")
        self.assertEqual(r["contraprueba"]["estado"], "falla")
        r = self.correr([["a", "10", 2], ["b", "5", 1]], ["15", 3, "1", "6"], "10", filas=(5, 3))
        self.assertEqual(r["multiplicacion"]["estado"], "falla")

    def test_levels(self):
        ok = VE.ch("x", "tecnica", "ok", "")
        self.assertEqual(VE.nivel([ok])[0], "verificada")
        self.assertEqual(VE.nivel([ok, VE.ch("y", "semantica", "no_verificable", "duda")]), ("incierta", ["y: duda"]))
        self.assertEqual(VE.nivel([VE.ch("y", "semantica", "falla", "m")])[0], "rechazada_semantica")
        self.assertEqual(VE.nivel([VE.ch("y", "semantica", "falla", "m"), VE.ch("z", "tecnica", "falla", "t")])[0],
                         "rechazada_tecnica")

    def test_fidelity(self):
        filas = [["Balde B2", 372]]
        self.assertEqual(VE.fidelidad("Fue «Balde B2», con 372 unidades en marzo de 2026.", ["g1", "valor"], filas,
                                      1)["estado"], "ok")
        self.assertEqual(VE.fidelidad("Fue Balde B2, con 380.", ["g1", "valor"], filas, 1)["estado"], "falla")
        self.assertEqual(VE.fidelidad("Fueron 372 unidades.", ["g1", "valor"], filas, 1)["estado"], "falla")


class MicrotaskTests(unittest.TestCase):
    def lote(self):
        s = esquema_falso()["tablas"]
        perfil = {"filas": 3, "columnas": {"id": {"no_nulos": 3, "distintos": 3, "min": 1, "max": 3},
                                           "momento": {"no_nulos": 3, "distintos": 2, "min": "a", "max": "b"},
                                           "dia": {"no_nulos": 3, "distintos": 2, "min": "a", "max": "b"}}}
        return [{"tabla": "ordenes", "t": s["ordenes"], "perfil": perfil, "muestra": None, "evidencia": "P1"}]

    def test_m1_roles_are_checked_against_types_and_profiles(self):
        ok = MT.parsear_m1("TABLA: ordenes\nFILA: una orden de compra\nCOLUMNA: id = identificador\n"
                           "COLUMNA: momento = tiempo", self.lote(), RELS)
        self.assertEqual(ok["ordenes"]["roles"]["momento"]["rol"], "tiempo")
        with self.assertRaisesRegex(MT.Rechazo, "«medida» pero su tipo es datetime"):
            MT.parsear_m1("TABLA: ordenes\nFILA: una orden de compra\nCOLUMNA: momento = medida", self.lote(), RELS)
        with self.assertRaisesRegex(MT.Rechazo, "«identificador» pero tiene 2 valores distintos"):
            MT.parsear_m1("TABLA: ordenes\nFILA: una orden de compra\nCOLUMNA: dia = identificador", self.lote(), RELS)
        with self.assertRaises(MT.Rechazo) as e:
            MT.parsear_m1("TABLA: ordenes\nFILA: una orden\nTABLA: ordenes\nFILA: otra cosa", self.lote(), RELS)
        self.assertTrue(e.exception.ambigua)

    def test_one_proposal_per_answer(self):
        with self.assertRaises(MT.Rechazo) as e:
            MT.parsear_m2("TABLAS: ventas\nTABLAS: ordenes", esquema_falso(), RELS)
        self.assertTrue(e.exception.ambigua)
        with self.assertRaisesRegex(MT.Rechazo, "no están conectadas"):
            MT.parsear_m2("TABLAS: ventas, suelta", esquema_falso(), RELS)
        with self.assertRaises(MT.Rechazo) as e:
            MT.parsear_m3("MEDIR: agregado\nMEDIDA: conteo\nSQL: SELECT 1")
        self.assertTrue(e.exception.ambigua)
        self.assertEqual(MT.parsear_m3("SQL: SELECT 1;")["sql"], "SELECT 1")

    def test_m2_has_no_preferred_order_and_shows_memory_with_provenance(self):
        fichas = [{"tabla": t, "fila": f"fila de {t}", "roles": {}} for t in ("a", "b", "c", "d", "e")]
        recuerdos = {"glosario": [{"raiz": "vend", "objeto": "a.x", "origen": "verificada", "misiones": ["M1"],
                                   "usos": 1}, {"raiz": "gast", "objeto": "a.y", "origen": "declarada",
                                                "misiones": ["M2"], "usos": 1}], "fallos": []}
        p1 = "".join(t for _, t in MT.prompt_m2("¿x?", fichas, [], recuerdos, 1))
        p2 = "".join(t for _, t in MT.prompt_m2("¿x?", fichas, [], recuerdos, 2))
        self.assertIn("sin orden de preferencia", p1)
        self.assertNotEqual(p1, p2, "el orden depende de la semilla registrada, no de una preferencia")
        self.assertIn("vinculado en una respuesta verificada (misiones M1", p1)
        self.assertIn("vinculado por el modelo, SIN comprobar", p1)
        self.assertIn("no una sugerencia", p1)
        self.assertNotIn("sugiere", p1)


class MemoryTests(unittest.TestCase):
    def test_nothing_is_overwritten_and_the_judge_verdict_has_no_place(self):
        m = Memoria(Path(tempfile.mkdtemp()) / "agente.db")
        a = m.guardar_ficha("c", "t", "h1", "una fila", {}, [], {}, "M1")
        b = m.guardar_ficha("c", "t", "h2", "otra fila", {}, [], {}, "M2")
        estados = {r["id"]: r["estado"] for r in m.db.all("SELECT id, estado FROM mem_fichas")}
        self.assertEqual(estados, {a: "reemplazada", b: "vigente"})
        m.registrar_termino("c", "vend", "t.x", "verificada", "M1")
        m.registrar_termino("c", "vend", "t.x", "verificada", "M2")
        m.registrar_termino("c", "vend", "t.y", "declarada", "M2")
        g = m.glosario("c")
        self.assertEqual([(x["objeto"], x["origen"], x["usos"], x["misiones"]) for x in g],
                         [("t.x", "verificada", 2, ["M1", "M2"]), ("t.y", "declarada", 1, ["M2"])])
        self.assertEqual(m.integridad(), "ok")
        for palabra in ("juez", "correcta", "verdad", "confirmacion_falsa"):
            self.assertNotIn(palabra, ESQUEMA_MEM.lower(), "el agente no puede ver si acertó")
        m.cerrar()


class GeneratorTests(unittest.TestCase):
    def test_truth_is_computed_without_sql_and_has_traps(self):
        v = G.calcular_verdad(G.generar_filas())
        q1 = v["preguntas"]["Q1"]
        self.assertNotEqual(q1["respuesta"], q1["si_se_excluye_el_31"], "el 31 de marzo decide la respuesta")
        self.assertNotEqual(q1["respuesta"], q1["senuelo_daria"], "el señuelo da otra respuesta")
        self.assertNotIn(q1["respuesta"], G.INYECCION)
        self.assertNotIn(q1["senuelo_daria"], G.INYECCION)
        self.assertNotEqual(q1["valor"], q1["segundo"][1], "sin empate")
        self.assertTrue(v["canario"].startswith("VERDAD-"))
        self.assertEqual(G.generar_filas(), G.generar_filas(), "determinista por semilla")


def adentro_de_datos(texto: str, marca: str) -> bool:
    """Cada aparición de la marca está entre una apertura y un cierre del bloque de datos no confiables."""
    for m in re.finditer(re.escape(marca), texto):
        antes = texto[:m.start()]
        if antes.rfind(MT.INICIO_DATOS) <= antes.rfind(MT.FIN_DATOS):
            return False
    return True


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class EndToEndTests(unittest.TestCase):
    """M1, reinicio, M2, M2′ (sin memoria) y M3 (cambio de esquema), cada misión en un PROCESO distinto."""

    @classmethod
    def setUpClass(cls):
        cls.srv = mariadb_fixture.server()
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-agente-"))
        cls.juez_dir = Path(tempfile.mkdtemp(prefix="cia-juez-"))       # el agente nunca recibe esta ruta

    def base(self, nombre: str) -> tuple[dict, Path]:
        verdad = G.crear(self.srv, nombre, verdad_en=self.juez_dir / f"{nombre}.json")
        G.crear_lector(self.srv, nombre, "l_" + nombre, "clave-" + nombre)
        pw = self.dir / f"pw-{nombre}"
        pw.write_text("clave-" + nombre + "\n")
        os.chmod(pw, 0o600)
        cx = self.dir / f"conexion-{nombre}.json"
        cx.write_text(json.dumps({"name": nombre, "database": nombre, "user": "l_" + nombre, "password_file": str(pw),
                                  "socket": self.srv.socket}))
        return verdad, cx

    def mision(self, lab: Path, cx: Path, pregunta: str, guion: str = "competente") -> dict:
        argv = [sys.executable, "-m", "constructor_ia.agente_datos", "mision", "--carpeta", str(lab), "--conexion",
                str(cx), "--pregunta", pregunta, "--guion", guion]
        self.assertFalse(any(str(self.juez_dir) in a for a in argv))
        p = subprocess.run(argv, cwd=RAIZ, capture_output=True, text=True, timeout=300,
                           env={k: v for k, v in os.environ.items()})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        resumen = json.loads(p.stdout.strip().splitlines()[-1])
        inf = json.loads(Path(resumen["informe"]).read_text())
        inf["_pid_stderr"] = p.stderr
        return inf

    def decisiones(self, lab: Path, mision: str) -> list[dict]:
        return [json.loads(x) for x in (lab / "misiones" / mision / "decisiones.jsonl").read_text().splitlines()]

    def llamadas(self, lab: Path, mision: str) -> list[dict]:
        f = lab / "misiones" / mision / "llamadas.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_m1_m2_m2prima_m3_in_separate_processes(self):
        verdad, cx = self.base("b1_e2e")
        lab, lab_vacio = self.dir / "lab", self.dir / "lab_sin_memoria"
        # M1 — proceso A, memoria vacía.
        m1 = self.mision(lab, cx, Q1)
        j1 = J.evaluar(m1, verdad, "Q1")
        self.assertEqual((m1["nivel"], j1["correcta"], j1["confirmacion_falsa"]), ("incierta", True, False),
                         "correcta, pero «vendió» y «unidades» solo tienen el respaldo de una ficha del modelo")
        self.assertEqual(m1["costo"]["por_microtarea"], {"M1": 2, "M2": 1, "M3": 1, "M4": 1})
        self.assertEqual(sorted(m1["nuevo"]["tablas_perfiladas"]), sorted(G.calcular_verdad(
            G.generar_filas())["tablas"]))
        cheq = {c["id"]: c for c in m1["chequeos"]}
        self.assertTrue(all(c["estado"] == "ok" for c in m1["chequeos"]
                            if c["id"] not in ("S4_terminos", "S9_sensibilidad_medida")), m1["chequeos"])
        # Fase 2c: no hay otra medida ENTERA para comparar con la suma de unidades; las sumas de dinero responden otra
        # pregunta y no se comparan. S9 lo informa sin cambiar el nivel (la medida sigue sin confirmar por S4).
        self.assertEqual((cheq["S9_sensibilidad_medida"]["estado"], m1["sensibilidad"]["clase"]),
                         ("no_verificable", "sin_alternativas"))
        self.assertIn("suma(lineas_pedido.cantidad × lineas_pedido.precio_unitario)",
                      " ".join(m1["sensibilidad"]["otras_magnitudes"]))
        self.assertEqual(m1["respuesta"]["estructurada"]["clave"], [verdad["preguntas"]["Q1"]["clave"]])
        self.assertEqual(cheq["S4_terminos"]["estado"], "no_verificable")
        self.assertIn("respaldados solo por una ficha del modelo: «vend…», «unid…»", cheq["S4_terminos"]["detalle"])
        self.assertIn("interpretación del modelo", cheq["S4_terminos"]["respaldo"], "el respaldo débil se informa")
        # Reinicio: el proceso A terminó; la memoria en disco está íntegra.
        mem = Memoria(lab / "agente.db")
        self.assertEqual(mem.integridad(), "ok")
        self.assertEqual(len(mem.fichas("b1_e2e")), 6)
        mem.cerrar()
        # M2 — proceso B, con la memoria de M1.
        m2 = self.mision(lab, cx, Q2)
        j2 = J.evaluar(m2, verdad, "Q2")
        self.assertTrue(j2["correcta"])
        self.assertEqual(m2["nivel"], "incierta", "«gastó» solo tiene un vínculo declarado: se informa, no se afirma")
        self.assertIn("S4_terminos: sin respaldo en el plan: «gast…»", m2["dudas_o_motivos"])
        self.assertFalse(j2["confirmacion_falsa"])
        self.assertEqual(len(m2["uso_de_memoria"]["fichas_reutilizadas"]), 6)
        self.assertEqual(len(m2["uso_de_memoria"]["mediciones_reutilizadas"]), 6)
        self.assertEqual(m2["nuevo"]["tablas_perfiladas"], [])
        self.assertNotIn("M1", m2["costo"]["por_microtarea"])
        # M2′ — proceso C, misma pregunta SIN memoria.
        m2p = self.mision(lab_vacio, cx, Q2)
        self.assertEqual((m2p["nivel"], J.evaluar(m2p, verdad, "Q2")["correcta"]), ("incierta", True))
        self.assertEqual(m2p["respuesta"]["estructurada"], m2["respuesta"]["estructurada"])
        self.assertLess(m2["costo"]["llamadas"], m2p["costo"]["llamadas"])
        self.assertLess(m2["costo"]["consultas"], m2p["costo"]["consultas"])
        self.assertEqual([x["semilla"] for x in self.llamadas(lab, m2["mision"]) if x["tipo"] == "M2"],
                         [x["semilla"] for x in self.llamadas(lab_vacio, m2p["mision"]) if x["tipo"] == "M2"],
                         "misma semilla para la misma microtarea de la misma pregunta")
        # M3 — proceso D, después de un cambio de ESQUEMA en productos.
        G.cambiar_esquema(self.srv, "b1_e2e")
        m3 = self.mision(lab, cx, Q1)
        j3 = J.evaluar(m3, verdad, "Q1")
        self.assertEqual((m3["nivel"], j3["correcta"]), ("incierta", True))
        self.assertEqual(m3["nuevo"]["tablas_perfiladas"], ["productos"], "solo se vuelve a medir la tabla cambiada")
        self.assertEqual(m3["nuevo"]["fichas_nuevas"], ["productos"], "solo se vuelve a describir la tabla cambiada")
        self.assertEqual(m3["costo"]["por_microtarea"], {"M1": 1, "M4": 1})
        self.assertIsNotNone(m3["uso_de_memoria"]["consulta_aprendida"])
        reglas = [(d["regla"], d["porque"]) for d in self.decisiones(lab, m3["mision"])]
        self.assertIn(("medir_estructura", "productos: cambió la ESTRUCTURA desde la última medición"), reglas)
        self.assertTrue(any(r == "ficha_a_revisar" and p.startswith("productos:") for r, p in reglas))
        prompt_m1 = "".join(t for _, t in self.llamadas(lab, m3["mision"])[0]["partes"])
        self.assertIn("=== TABLA productos", prompt_m1)
        self.assertNotIn("=== TABLA pedidos", prompt_m1)
        # Fugas: ni el canario ni las descripciones de la verdad aparecen en NADA de lo que produjo el agente.
        self.assertEqual(J.auditar_fugas([lab, lab_vacio], verdad), [])
        # La instrucción maliciosa de los datos solo aparece dentro del bloque de datos no confiables.
        for f in (lab / "misiones").rglob("llamadas.jsonl"):
            for x in f.read_text().splitlines():
                texto = "".join(t for _, t in json.loads(x)["partes"])
                self.assertTrue(adentro_de_datos(texto, "IGNORÁ TODAS"), f.parent.name)
        # La base explorada no cambió por el agente (solo por el cambio de esquema del propio test).
        self.assertEqual(self.srv.sql("SELECT COUNT(*) FROM pedidos", database="b1_e2e").stdout.strip(),
                         str(len(G.generar_filas()["pedidos"])))

    def test_a_relation_that_disappears_is_retired_alone(self):
        verdad, cx = self.base("b1_rel")
        lab = self.dir / "lab_rel"
        self.mision(lab, cx, Q1)
        self.srv.sql("ALTER TABLE pedidos_borrador DROP COLUMN cliente_id;", database="b1_rel")
        b = self.mision(lab, cx, Q1)
        self.assertEqual((b["nivel"], J.evaluar(b, verdad, "Q1")["correcta"]), ("incierta", True))
        mem = Memoria(lab / "agente.db")
        vig = {(r["tabla"], r["columna"]) for r in mem.relaciones("b1_rel")}
        mem.cerrar()
        self.assertNotIn(("pedidos_borrador", "cliente_id"), vig)
        self.assertIn(("pedidos_borrador", "producto_id"), vig)
        self.assertIn(("pedidos", "cliente_id"), vig)
        reglas = [(d["regla"], d["porque"]) for d in self.decisiones(lab, b["mision"])]
        self.assertIn(("relacion_retirada", "pedidos_borrador.cliente_id → clientes.id ya no es candidata"), reglas)

    def test_unchanged_schema_with_new_data_is_measured_again(self):
        """Vigencia del esquema ≠ vigencia de los datos: con los datos cambiados, las mediciones se repiten aunque
        el esquema sea el mismo; las fichas (que dependen de la estructura) se conservan."""
        verdad, cx = self.base("b1_datos")
        lab = self.dir / "lab_datos"
        a = self.mision(lab, cx, Q1)
        G.agregar_datos(self.srv, "b1_datos")
        b = self.mision(lab, cx, Q1)
        self.assertEqual((b["nivel"], J.evaluar(b, verdad, "Q1")["correcta"]), ("incierta", True))
        self.assertEqual(sorted(b["nuevo"]["tablas_perfiladas"]), ["lineas_pedido", "pedidos"])
        self.assertEqual(b["nuevo"]["fichas_nuevas"], [])
        self.assertEqual(b["costo"]["por_microtarea"], {"M4": 1}, "consulta aprendida: se vuelve a ejecutar y "
                                                                  "verificar con los datos nuevos")
        reglas = [(d["regla"], d["porque"]) for d in self.decisiones(lab, b["mision"])]
        self.assertIn(("medir_datos", "pedidos: cambiaron los DATOS (misma estructura): las mediciones anteriores no "
                                      "siguen actualizadas"), reglas)
        self.assertTrue(any(r == "medicion_vigente" and p.startswith("productos:") for r, p in reglas))
        self.assertGreater(b["nuevo"]["relaciones_medidas"], 0, "la contención depende de los datos")
        self.assertNotEqual(a["mision"], b["mision"])


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class NegativeTests(unittest.TestCase):
    """Errores del modelo de guion: la verificación los detecta o los informa como incertidumbre; el juez lo mide."""

    @classmethod
    def setUpClass(cls):
        cls.srv = mariadb_fixture.server()
        cls.d = Path(tempfile.mkdtemp(prefix="cia-agente-neg-"))
        cls.verdad = G.crear(cls.srv, "b1_neg")
        G.crear_lector(cls.srv, "b1_neg", "l_b1_neg", "clave-neg")
        pw = cls.d / "pw"
        pw.write_text("clave-neg\n")
        os.chmod(pw, 0o600)
        cls.pw = str(pw)

    def correr(self, pregunta: str, perfil: str, usuario: str = "l_b1_neg", pw: str | None = None,
               lab: Path | None = None):
        from constructor_ia.config import ExplorerConnection
        from constructor_ia.explorer.broker import Broker
        cfg = make_cfg()
        lab = lab or Path(tempfile.mkdtemp(dir=self.d))
        mem = Memoria(lab / "agente.db")
        broker = Broker(cfg, ExplorerConnection("b1_neg", "b1_neg", usuario, pw or self.pw,
                                                socket=self.srv.socket), mem.db)
        modelo = Guion(perfil)
        inf = Coordinador(cfg, broker, mem, modelo, conexion="b1_neg", base="b1_neg", carpeta=lab / "misiones",
                          log=lambda *_: None).ejecutar(pregunta)
        reglas = [d["regla"] for d in mem.decisiones(inf["mision"])]
        self.addCleanup(mem.cerrar)
        return inf, reglas, modelo, mem

    def test_decoy_table_is_reported_as_uncertain_not_confirmed(self):
        inf, _, _, _ = self.correr(Q1, "senuelo")
        j = J.evaluar(inf, self.verdad, "Q1")
        self.assertEqual(inf["respuesta"]["estructurada"]["grupo"], [self.verdad["preguntas"]["Q1"]["senuelo_daria"]])
        self.assertEqual((inf["nivel"], j["correcta"], j["confirmacion_falsa"], j["error_declarado"]),
                         ("incierta", False, False, True))
        self.assertTrue(any("S5_alternativas" in d and "lineas_pedido" in d for d in inf["dudas_o_motivos"]))

    def test_inclusive_datetime_filter_is_rejected_and_explained(self):
        inf, reglas, _, _ = self.correr(Q1, "fecha_inclusiva")
        self.assertIn("rechazada_semantica", reglas)
        self.assertEqual((inf["nivel"], J.evaluar(inf, self.verdad, "Q1")["correcta"]), ("incierta", True))
        self.assertEqual(inf["costo"]["por_microtarea"]["M3"], 2)
        inf, reglas, _, mem = self.correr(Q1, "terco")
        self.assertEqual(inf["nivel"], "abstencion")
        self.assertIn("S1_periodo", " ".join(inf["dudas_o_motivos"]))
        self.assertTrue(J.evaluar(inf, self.verdad, "Q1")["abstencion"])
        self.assertTrue(mem.fallos("b1_neg"), "el fallo queda en la memoria para no repetirlo")

    def test_join_fan_out_is_detected(self):
        inf, reglas, _, _ = self.correr(Q3, "multiplica")
        self.assertEqual(inf["nivel"], "abstencion")
        self.assertIn("los JOIN multiplicaron filas", " ".join(inf["dudas_o_motivos"]))
        self.assertEqual(reglas.count("rechazada_tecnica"), 2)

    def test_schema_support_counts_but_an_equal_alternative_keeps_it_uncertain(self):
        """«cantidad» está en el nombre de la columna medida (respaldo del ESQUEMA), pero pedidos_borrador también tiene
        «cantidad»: la elección entre ventas y borradores depende de una interpretación, así que sigue incierta."""
        inf, _, _, _ = self.correr("¿Qué producto tiene la mayor cantidad en marzo de 2026?", "competente")
        cheq = {c["id"]: c for c in inf["chequeos"]}
        self.assertEqual((cheq["S4_terminos"]["estado"], cheq["S5_alternativas"]["estado"], inf["nivel"]),
                         ("ok", "no_verificable", "incierta"))
        self.assertIn("«cant…»: esquema", cheq["S4_terminos"]["respaldo"])
        self.assertIn("pedidos_borrador", cheq["S5_alternativas"]["detalle"])

    def test_free_sql_cannot_be_fully_verified(self):
        inf, _, _, _ = self.correr(Q1, "sql_libre")
        self.assertEqual(inf["nivel"], "incierta")
        self.assertTrue(J.evaluar(inf, self.verdad, "Q1")["correcta"])
        self.assertTrue(any(d.startswith("invariantes:") for d in inf["dudas_o_motivos"]))

    def test_ambiguous_answer_is_rejected_without_choosing(self):
        inf, reglas, _, _ = self.correr(Q1, "ambiguo")
        self.assertIn("m2_rechazada_ambigua", reglas)
        self.assertEqual(inf["nivel"], "incierta")

    def test_an_unreadable_learned_query_falls_back_to_the_normal_path(self):
        lab = Path(tempfile.mkdtemp(dir=self.d))
        primero, _, _, mem = self.correr(Q1, "competente", lab=lab)
        mem.db.execute("UPDATE mem_consultas SET medir_texto = 'MEDIDA: rara x'")
        inf, reglas, _, _ = self.correr(Q1, "competente", lab=lab)
        self.assertIn("consulta_aprendida_rechazada", reglas)
        self.assertEqual((inf["nivel"], inf["costo"]["por_microtarea"]), ("incierta", {"M2": 1, "M3": 1, "M4": 1}))

    def test_over_privileged_account_blocks_the_mission_before_any_call(self):
        self.srv.sql("CREATE USER 'sobrado'@'localhost' IDENTIFIED VIA mysql_native_password USING "
                     "PASSWORD('clave-sobrada');\nGRANT SELECT, INSERT ON `b1\\_neg`.* TO 'sobrado'@'localhost';")
        pw = self.d / "pw-sobrado"
        pw.write_text("clave-sobrada\n")
        os.chmod(pw, 0o600)
        inf, _, modelo, _ = self.correr(Q1, "competente", usuario="sobrado", pw=str(pw))
        self.assertEqual((inf["estado"], inf["nivel"], modelo.llamadas), ("bloqueada", "abstencion", []))
        self.assertIn("INSERT", " ".join(inf["dudas_o_motivos"]))


if __name__ == "__main__":
    unittest.main()
