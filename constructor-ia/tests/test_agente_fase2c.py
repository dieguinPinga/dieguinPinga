"""Fase 2c (AGENTE_DATOS.md, §14): medida semánticamente insuficiente, clave ≠ etiqueta y sin alias internos.

Pruebas NEGATIVAS: distinguir un SQL matemáticamente correcto de uno que responde otra cosa, sin reglas propias de
la base B1. Incluyen la propuesta REAL de Qwen en la segunda M2 (suma de precios unitarios para «gastó»), una base con
nombres OPACOS (t_x, t_y, t_w: nada en el esquema dice qué es cada cosa), nombres repetidos, medidas de la MISMA
magnitud que cambian el ganador (ambigüedad legítima) y medidas de OTRA magnitud (unidades frente a dinero) que
también lo cambian pero responden otra pregunta y no provocan abstención. Los modelos de guion validan el circuito; no
dicen cómo respondería Qwen.
"""
import os
import re
import tempfile
import unittest
from pathlib import Path

from constructor_ia.agente_datos import generador as G
from constructor_ia.agente_datos import identidad as ID
from constructor_ia.agente_datos import juez as J
from constructor_ia.agente_datos import medir as MD
from constructor_ia.agente_datos import microtareas as MT
from constructor_ia.agente_datos import requisitos as RQ
from constructor_ia.agente_datos import sensibilidad as SE
from constructor_ia.agente_datos import verificador as VE
from constructor_ia.agente_datos.coordinador import Coordinador
from constructor_ia.agente_datos.guion import Guion
from constructor_ia.agente_datos.memoria import Memoria

from . import mariadb_fixture
from .helpers import make_cfg

Q1, Q2 = G.PREGUNTAS["Q1"], G.PREGUNTAS["Q2"]
FACTURO = "¿Qué producto facturó más en marzo de 2026?"

# La propuesta real de Qwen en la segunda corrida de M2 (carpeta agente-fase2-b), tal como la escribió.
M3_REAL_M2 = ("MEDIDA: suma lineas_pedido.precio_unitario\nAGRUPAR: clientes.id\n"
              "FILTRO: pedidos.fecha desde 2026-03-01 hasta 2026-04-01\nORDEN: mayor\nLIMITE: 1\n"
              "INTERPRETA: cliente = clientes.id")
M4_REAL_M2 = "RESPUESTA: El cliente g1 gastó 60996.72."
ALIAS = re.compile(r"\b[ge]\d+\b")


class Guionado(Guion):
    """Guion con respuestas fijas para algunas microtareas (en orden); el resto lo responde el guion competente."""

    def __init__(self, m3=None, m2=None, m4=None, filas=None):
        super().__init__("competente")
        self.r3, self.r2, self.r4, self.filas = list(m3 or []), m2, m4, filas or {}

    def m1(self, prompt):
        out = super().m1(prompt)
        for t, fila in self.filas.items():
            out = out.replace(f"FILA: un registro de {t}", f"FILA: {fila}")
        return out

    def m2(self, prompt):
        return self.r2 or super().m2(prompt)

    def m3(self, prompt):
        return self.r3.pop(0) if self.r3 else super().m3(prompt)

    def m4(self, prompt):
        return self.r4 or super().m4(prompt)


class Base:
    """Una base de MariaDB con una cuenta de solo lectura y misiones con el coordinador (en este proceso)."""

    @classmethod
    def preparar(cls, base, crear):
        cls.srv = mariadb_fixture.server()
        cls.d = Path(tempfile.mkdtemp(prefix=f"cia-2c-{base}-"))
        cls.base = base
        cls.verdad = crear(cls.srv, base)
        G.crear_lector(cls.srv, base, f"l_{base}", f"clave-{base}")
        pw = cls.d / "pw"
        pw.write_text(f"clave-{base}\n")
        os.chmod(pw, 0o600)
        cls.pw = str(pw)

    def correr(self, pregunta, modelo, lab=None):
        from constructor_ia.config import ExplorerConnection
        from constructor_ia.explorer.broker import Broker
        cfg = make_cfg()
        lab = lab or Path(tempfile.mkdtemp(dir=self.d))
        mem = Memoria(lab / "agente.db")
        self.addCleanup(mem.cerrar)
        broker = Broker(cfg, ExplorerConnection(self.base, self.base, f"l_{self.base}", self.pw,
                                                socket=self.srv.socket), mem.db)
        inf = Coordinador(cfg, broker, mem, modelo, conexion=self.base, base=self.base, carpeta=lab / "misiones",
                          log=lambda *_: None).ejecutar(pregunta)
        decisiones = [(d["regla"], d["porque"]) for d in mem.decisiones(inf["mision"])]
        return inf, decisiones, mem, lab

    def sin_alias(self, inf):
        texto = (inf.get("respuesta") or {}).get("texto") or ""
        self.assertIsNone(ALIAS.search(texto), texto)


class InterpretacionTests(unittest.TestCase):
    """A1: INTERPRETA obligatoria solo para los conceptos que determinan la medida, y coherente con la MEDIDA."""

    def prop(self, texto):
        return MT.parsear_m3(texto)

    def test_concepts_are_what_remains_after_period_order_and_entity(self):
        self.assertEqual(RQ.deducir(Q2)["conceptos"], ["gastó".replace("ó", "o")])
        self.assertEqual(RQ.deducir(Q1)["conceptos"], ["vendio", "unidades"])
        self.assertEqual(RQ.deducir("¿Qué cliente tuvo más pedidos en 2026?")["conceptos"], ["pedidos"])

    def test_the_real_m2_proposal_lacks_the_interpretation_of_the_measure(self):
        req = RQ.deducir(Q2)
        faltan = RQ.interpretacion(req, self.prop(M3_REAL_M2))
        self.assertEqual(len(faltan), 1)
        self.assertIn("falta INTERPRETA para «gasto» (dice qué se mide)", faltan[0][0])
        self.assertIn("«INTERPRETA: gasto = <la misma MEDIDA: suma lineas_pedido.precio_unitario>»", faltan[0][1])
        self.assertIn("hipótesis", faltan[0][1])
        # «cliente = clientes.id» es la entidad: no la pide A1 (tiene su propio control).
        con = self.prop(M3_REAL_M2 + "\nINTERPRETA: gastó = suma lineas_pedido.precio_unitario")
        self.assertEqual(RQ.interpretacion(req, con), [], "declarada y coherente: pasa (sigue siendo una hipótesis)")

    def test_an_interpretation_that_contradicts_the_plan_is_rejected(self):
        req = RQ.deducir(Q2)
        casos = {
            "INTERPRETA: gastó = suma lineas_pedido.cantidad * lineas_pedido.precio_unitario":
                "cita lineas_pedido.cantidad, que no está en la MEDIDA",
            "INTERPRETA: gastó = promedio lineas_pedido.precio_unitario": "dice promedio y la MEDIDA es «suma»",
            "INTERPRETA: gastó = lo que pagó el cliente": "la MEDIDA usa lineas_pedido.precio_unitario, que ninguna",
        }
        for linea, esperado in casos.items():
            with self.subTest(linea=linea):
                faltan = RQ.interpretacion(req, self.prop(M3_REAL_M2 + "\n" + linea))
                self.assertIn(esperado, " ".join(m for m, _ in faltan))

    def test_a_filter_value_is_not_a_measure_concept(self):
        req = RQ.deducir("¿Qué producto vendió más unidades en Rosario en marzo de 2026?")
        self.assertEqual(req["conceptos"], ["vendio", "unidades", "rosario"])
        plan = ("MEDIDA: suma lineas_pedido.cantidad\nAGRUPAR: productos.id\nFILTRO: clientes.ciudad = Rosario\n"
                "INTERPRETA: vendió = suma lineas_pedido.cantidad\nINTERPRETA: unidades = lineas_pedido.cantidad")
        self.assertEqual(RQ.interpretacion(req, self.prop(plan)), [])
        self.assertEqual(RQ.interpretacion(req, self.prop(plan + "\nINTERPRETA: Rosario = clientes.ciudad")), [])
        sin_filtro = plan.replace("FILTRO: clientes.ciudad = Rosario\n", "")
        self.assertIn("falta INTERPRETA para «rosario»", RQ.interpretacion(req, self.prop(sin_filtro))[0][0],
                      "si el plan no la usa como filtro, la palabra sigue pidiendo su interpretación")

    def test_free_sql_only_needs_the_declaration(self):
        req = RQ.deducir(Q2)
        sql = "SQL: SELECT 1"
        self.assertTrue(RQ.interpretacion(req, self.prop(sql)))
        self.assertEqual(RQ.interpretacion(req, self.prop(sql + "\nINTERPRETA: gastó = cualquier cosa")), [])

    def test_a_question_without_measure_concepts_needs_no_interpretation(self):
        self.assertEqual(RQ.interpretacion(RQ.deducir("¿Qué cliente tuvo más en 2026?"),
                                           self.prop("MEDIDA: conteo\nAGRUPAR: clientes.id")), [])


class PoliticaDeSensibilidadTests(unittest.TestCase):
    """S9: la política aprobada para cada clase, con la medida confirmada o no."""

    def s(self, clase):
        return {"clase": clase, "exploradas": 2, "candidatas": 2, "motivo": "", "alternativas": [],
                "justificacion": "j"}

    def test_policy(self):
        esperado = {"ganador_dependiente": "impide", "valor_dependiente": "no_verificable",
                    "incompleta": "no_verificable", "estable": "no_verificable", "sin_alternativas": "no_verificable",
                    "no_aplica": "ok"}
        for clase, estado in esperado.items():
            with self.subTest(clase=clase):
                c = VE.sensibilidad(self.s(clase), False, "")
                self.assertEqual(c["estado"], estado)
                self.assertEqual(VE.sensibilidad(self.s(clase), True, "")["estado"], "ok",
                                 "con la medida confirmada solo se informa")
        self.assertIn("NO constituye respaldo semántico", VE.sensibilidad(self.s("estable"), False, "")["detalle"])
        self.assertIn("NO implica que la medida sea correcta",
                      VE.sensibilidad(self.s("sin_alternativas"), False, "")["detalle"])
        self.assertIn("no se puede concluir que el ganador sea estable",
                      VE.sensibilidad(self.s("incompleta"), False, "")["detalle"])

    def test_an_impeding_check_is_an_abstention_but_a_failure_still_comes_first(self):
        impide = VE.ch("S9_sensibilidad_medida", "semantica", "impide", "x")
        self.assertEqual(VE.nivel([impide])[0], "abstencion")
        falla = VE.ch("S1_periodo", "semantica", "falla", "y")
        self.assertEqual(VE.nivel([impide, falla])[0], "rechazada_semantica", "un error corregible se reintenta")

    def test_confirmation_needs_schema_support_for_every_concept(self):
        ok = [VE.ch("S4_terminos", "semantica", "ok", "")]
        self.assertTrue(VE.medida_confirmada(ok, "¿Qué producto tiene la mayor cantidad en 2026?"))
        self.assertFalse(VE.medida_confirmada(ok, "¿Qué producto tiene más en 2026?"), "la pregunta no dice qué medir")
        self.assertFalse(VE.medida_confirmada([VE.ch("S4_terminos", "semantica", "no_verificable", "")], Q2))


class Res:
    def __init__(self, filas, status="ok"):
        self.rows, self.status, self.truncated, self.columns = filas, status, False, []


class ExploracionAcotadaTests(unittest.TestCase):
    """A2 sin base: candidatas justificadas por roles y relaciones, tope de consultas y exploración incompleta."""

    def schema(self, n_medidas):
        def col(n, t, k=""):
            return {"nombre": n, "tipo": t, "nulo": False, "clave": k, "extra": "", "comentario": ""}
        return {"tablas": {
            "h": {"columnas": [col("id", "int(11)", "PRI"), col("e_id", "int(11)"), col("nota", "int(11)")]
                  + [col(f"m{i}", "decimal(10,2)") for i in range(n_medidas)], "indices": {}, "claves_foraneas": []},
            "e": {"columnas": [col("id", "int(11)", "PRI"), col("nombre", "varchar(20)"), col("peso", "int(11)")],
                  "indices": {}, "claves_foraneas": []}}}

    REL = [{"tabla": "h", "columna": "e_id", "tabla_ref": "e", "columna_ref": "id", "contencion": True, "unico": True,
            "origen": "convencion"}]

    def compilado(self, n):
        plan = MD.parsear("MEDIDA: suma h.m0\nAGRUPAR: e.id\nORDEN: mayor\nLIMITE: 1")
        plan["etiquetas"] = [("e", "nombre")]
        return MD.compilar(plan, self.schema(n), self.REL)

    def fichas(self, n):
        return {"h": {"roles": {**{f"m{i}": {"rol": "medida"} for i in range(n)}, "nota": {"rol": "otro"},
                                "e_id": {"rol": "referencia"}}},
                "e": {"roles": {"peso": {"rol": "medida"}, "nombre": {"rol": "texto"}}}}

    def test_candidates_are_justified_by_roles_relations_and_magnitude_not_every_numeric_column(self):
        c = self.compilado(2)
        cands, justif, otras = SE.candidatas(c, self.schema(2), self.fichas(2), self.REL)
        self.assertEqual(cands, [(("h", "m1"),)], "solo otra medida continua: la misma magnitud que h.m0")
        self.assertEqual(otras, [(("e", "peso"),), (("e", "peso"), ("h", "m0")), (("e", "peso"), ("h", "m1")),
                                 (("h", "m0"), ("h", "m1"))], "otras magnitudes: se listan, no se comparan")
        self.assertNotIn("nota", str(cands + otras), "un número con rol «otro» no es una medida")
        self.assertNotIn("e_id", str(cands + otras), "una referencia no es una medida")
        self.assertIn("unidas muchos→uno", justif)
        self.assertIn("misma magnitud que la del plan (continua)", justif)

    def test_magnitude_classes(self):
        self.assertEqual([SE.clase("int(11)"), SE.clase("decimal(10,2)"), SE.clase("double"),
                          SE.clase("tinyint(4)", {"min": 1, "max": 1}), SE.clase("int(11)", {"min": 0, "max": 1}),
                          SE.clase("int(11)", {"min": 1, "max": 12})],
                         ["entera", "continua", "continua", "indicador", "indicador", "entera"])

    def test_alternatives_never_add_joins_or_change_rows(self):
        c = self.compilado(2)
        sql = MD.sql_con_agregado(c, "SUM(t0.`m1`)", 2)
        self.assertEqual(sql.split(" FROM ")[1].split(" GROUP BY ")[0], c["sql"].split(" FROM ")[1].split(" GROUP BY ")[0])

    def test_too_many_candidates_leave_the_exploration_incomplete(self):
        c = self.compilado(8)
        llamadas = []

        def run(q, h):
            llamadas.append(q)
            return Res([(1, 10, "uno"), (2, 5, "dos")])
        s = SE.explorar(c, self.schema(8), self.fichas(8), self.REL, run, Res([(1, 10, "uno"), (2, 5, "dos")]))
        self.assertEqual((s["clase"], s["completa"], len(llamadas), s["exploradas"]), ("incompleta", False, 6, 6))
        self.assertGreater(s["candidatas"], SE.MAX_ALTERNATIVAS)
        self.assertIn("tope de 6 consultas", s["motivo"])

    def test_a_failed_query_is_recorded_and_never_read_as_stable(self):
        c = self.compilado(2)
        s = SE.explorar(c, self.schema(2), self.fichas(2), self.REL,
                        lambda q, h: Res([], status="error"), Res([(1, 10, "uno")]))
        self.assertEqual(s["clase"], "incompleta")
        self.assertIn("la consulta falló", s["motivo"])

    def test_same_winner_and_value_is_stable_and_no_candidates_is_reported(self):
        c = self.compilado(2)
        s = SE.explorar(c, self.schema(2), self.fichas(2), self.REL, lambda q, h: Res([(1, 10, "uno")]),
                        Res([(1, 10, "uno")]))
        self.assertEqual(s["clase"], "estable")
        c = self.compilado(1)
        s = SE.explorar(c, self.schema(1), self.fichas(1), self.REL, lambda q, h: Res([(1, 10, "uno")]),
                        Res([(1, 10, "uno")]))
        self.assertEqual(s["clase"], "sin_alternativas")


class FidelidadSinAliasTests(unittest.TestCase):
    def test_the_real_m4_answer_with_an_internal_alias_is_rejected(self):
        cols, filas = ["clientes.nombre", "clientes.id (clave)", "suma(lineas_pedido.precio_unitario)"], \
            [["Lucía Castro", 59, "60996.72"]]
        f = VE.fidelidad(MT.parsear_m4(M4_REAL_M2), cols, filas, 2, ["Lucía Castro"], "60996.72")
        self.assertEqual(f["estado"], "falla")
        self.assertIn("usa nombres internos del coordinador (g1)", f["detalle"])
        self.assertIn("no menciona Lucía Castro", f["detalle"])
        ok = VE.fidelidad("Lucía Castro, con 60996.72.", cols, filas, 2, ["Lucía Castro"], "60996.72")
        self.assertEqual(ok["estado"], "ok")

    def test_a_repeated_label_needs_the_key(self):
        cols, filas = ["clientes.nombre", "clientes.id (clave)", "v"], [["Ana Acosta", 7, "10"]]
        f = VE.fidelidad("Ana Acosta, con 10.", cols, filas, 2, ["Ana Acosta", 7], "10")
        self.assertIn("no menciona 7", f["detalle"])

    def test_m4_never_sees_internal_aliases(self):
        p = "".join(t for _, t in MT.prompt_m4(Q2, ["clientes.nombre", "clientes.id (clave)", "suma(x)"],
                                               [["Lucía Castro", 59, 1]]))
        self.assertIsNone(ALIAS.search(p))


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class B1Tests(Base, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preparar("b1_2c", lambda srv, b: G.crear(srv, b))

    def test_the_real_m2_plan_is_rejected_then_reported_as_uncertain_never_as_confirmed(self):
        """La propuesta real: sin INTERPRETA para «gastó» se rechaza ANTES de ejecutar. Declarada, la suma de precios
        unitarios no tiene otra medida CONTINUA con la que compararse en esas tablas; cantidad × precio es otra magnitud
        y se lista sin compararse. Queda incierta (S4: «gastó» sin respaldo), el M4 real con «g1» se rechaza y la
        respuesta determinista usa nombres reales. El juez la marca incorrecta, sin confirmación falsa."""
        modelo = Guionado(m3=[M3_REAL_M2, M3_REAL_M2 + "\nINTERPRETA: gastó = suma lineas_pedido.precio_unitario"],
                          m4=M4_REAL_M2)
        inf, dec, mem, lab = self.correr(Q2, modelo)
        reglas = [r for r, _ in dec]
        self.assertIn("falta INTERPRETA para «gasto»", next(p for r, p in dec if r == "m3_requisito_incompleto"))
        self.assertEqual(reglas.count("m3_requisito_incompleto"), 1)
        self.assertEqual(inf["nivel"], "incierta")
        self.assertIn("S4_terminos: sin respaldo en el plan: «gast…»", inf["dudas_o_motivos"])
        s = inf["sensibilidad"]
        self.assertEqual(s["clase"], "sin_alternativas")
        self.assertIn("suma(lineas_pedido.cantidad × lineas_pedido.precio_unitario)", s["otras_magnitudes"])
        self.assertEqual(reglas.count("m4_rechazada"), 2)
        self.assertIn("usa nombres internos del coordinador (g1)", next(p for r, p in dec if r == "m4_rechazada"))
        r = inf["respuesta"]
        self.assertEqual(r["texto"], "Resultado: clientes.nombre = Lucía Castro; clientes.id (clave) = 59; "
                                     "suma(lineas_pedido.precio_unitario) = 60996.72")
        self.assertEqual((r["estructurada"]["clave"], r["estructurada"]["etiqueta"], str(r["estructurada"]["valor"])),
                         ([59], ["Lucía Castro"], "60996.72"))
        self.assertFalse(r["estructurada"]["valor_confirmado"])
        self.sin_alias(inf)
        j = J.evaluar(inf, self.verdad, "Q2")
        self.assertEqual((j["correcta"], j["confirmacion_falsa"], j["error_declarado"]), (False, False, True))
        glos = [g for g in mem.glosario(self.base) if g["raiz"] == "gast"]
        self.assertTrue(glos and all(g["origen"] == "declarada" for g in glos), "la declaración queda como hipótesis")
        self.assertEqual(J.auditar_fugas([lab], self.verdad), [])

    def test_positive_control_the_right_measure_is_still_only_uncertain(self):
        inf, _, _, _ = self.correr(Q2, Guion("competente"))
        j = J.evaluar(inf, self.verdad, "Q2")
        self.assertEqual((inf["nivel"], j["correcta"], j["confirmacion_falsa"]), ("incierta", True, False))
        self.assertIn("S4_terminos: sin respaldo en el plan: «gast…»", inf["dudas_o_motivos"])
        self.sin_alias(inf)

    def test_units_compared_with_money_is_not_an_ambiguity_and_m1_completes(self):
        """«Más unidades»: sumar dinero (precio unitario, precio de lista o cantidad × precio) da OTRO ganador (Pincel
        B2, clave 22, frente a Balde B2, clave 34), pero responde otra pregunta. No es una alternativa de la misma
        magnitud: no provoca abstención. La respuesta queda incierta porque «vendió» y «unidades» no tienen respaldo
        verificable, y M1 se completa (es la misión que habilita la evaluación de memoria en M2)."""
        inf, dec, mem, lab = self.correr(Q1, Guion("competente"))
        s = inf["sensibilidad"]
        self.assertEqual((inf["estado"], inf["nivel"], s["clase"], s["confirmada"]),
                         ("completa", "incierta", "sin_alternativas", False))
        self.assertEqual(sorted(s["otras_magnitudes"]), sorted([
            "suma(lineas_pedido.precio_unitario)", "suma(productos.precio_lista)",
            "suma(lineas_pedido.cantidad × lineas_pedido.precio_unitario)",
            "suma(lineas_pedido.cantidad × productos.precio_lista)",
            "suma(lineas_pedido.precio_unitario × productos.precio_lista)"]))
        self.assertEqual(s["consultas"], 0, "las otras magnitudes no se ejecutan")
        self.assertNotIn("abstencion_por_sensibilidad", [r for r, _ in dec])
        j = J.evaluar(inf, self.verdad, "Q1")
        self.assertEqual((j["correcta"], j["confirmacion_falsa"]), (True, False))
        self.assertEqual((inf["respuesta"]["estructurada"]["clave"], inf["respuesta"]["estructurada"]["etiqueta"]),
                         ([34], ["Balde B2"]))
        self.assertIn("no se comparan ni cuentan como ambigüedad",
                      next((lab / "misiones").iterdir()).joinpath("informe.md").read_text())
        self.sin_alias(inf)

    def test_a_same_magnitude_alternative_is_compared(self):
        """«Facturó»: cantidad × precio cobrado y cantidad × precio de lista son la misma magnitud (entera × continua):
        se comparan. El ganador no cambia, el valor sí: incierta, con el valor de cada interpretación."""
        m3 = ("MEDIDA: suma lineas_pedido.cantidad * lineas_pedido.precio_unitario\nAGRUPAR: productos.id\n"
              "FILTRO: pedidos.fecha desde 2026-03-01 hasta 2026-04-01\nORDEN: mayor\nLIMITE: 1\n"
              "INTERPRETA: facturó = suma lineas_pedido.cantidad * lineas_pedido.precio_unitario")
        inf, _, _, _ = self.correr(FACTURO, Guionado(m3=[m3]))
        s = inf["sensibilidad"]
        self.assertEqual((inf["nivel"], s["clase"], s["consultas"]), ("incierta", "valor_dependiente", 1))
        self.assertEqual([a["medida"] for a in s["alternativas"]],
                         ["suma(lineas_pedido.cantidad × productos.precio_lista)"])
        self.assertIn("suma(lineas_pedido.precio_unitario × productos.precio_lista)", s["otras_magnitudes"])
        self.assertIn("ninguna interpretación está confirmada", inf["respuesta"]["texto"])
        self.assertNotIn("M4", inf["costo"]["por_microtarea"], "no se redacta un valor no confirmado")
        self.sin_alias(inf)

    def test_with_a_confirmed_measure_the_sensitivity_is_only_reported_and_m4_cannot_leak_aliases(self):
        """«cantidad» está en el esquema: la medida se confirma, S9 informa sin cambiar el nivel y se pide M4. Un M4
        que insiste en «g1» se rechaza y la respuesta determinista usa nombres reales."""
        inf, dec, _, _ = self.correr("¿Qué producto tiene la mayor cantidad en marzo de 2026?",
                                     Guionado(m4="RESPUESTA: El producto g1 tiene valor 372."))
        cheq = {c["id"]: c for c in inf["chequeos"]}
        self.assertEqual(cheq["S9_sensibilidad_medida"]["estado"], "ok")
        self.assertEqual(inf["nivel"], "incierta", "S5: los borradores también tienen «cantidad»")
        self.assertEqual(inf["costo"]["por_microtarea"]["M4"], 2)
        self.assertIn("usa nombres internos del coordinador (g1)", next(p for r, p in dec if r == "m4_rechazada"))
        self.assertEqual(inf["respuesta"]["texto"], "Resultado: productos.nombre = Balde B2; productos.id (clave) = 34;"
                                                    " suma(lineas_pedido.cantidad) = 372")
        self.sin_alias(inf)

    def test_declared_interpretations_never_become_verified_by_repetition(self):
        lab = Path(tempfile.mkdtemp(dir=self.d))
        # Tres redacciones distintas: ninguna reutiliza la consulta aprendida, cada una vuelve a declarar «gastó».
        for pregunta in (Q2, "¿Cuál cliente gastó más en marzo de 2026?", "¿Qué cliente gastó más plata en marzo de 2026?"):
            inf, _, mem, _ = self.correr(pregunta, Guion("competente"), lab=lab)
            self.assertEqual(inf["nivel"], "incierta")
            self.assertTrue(any(d.startswith("S4_terminos: sin respaldo en el plan: «gast…»")
                                for d in inf["dudas_o_motivos"]), inf["dudas_o_motivos"])
        glos = [g for g in mem.glosario(self.base) if g["raiz"] == "gast"]
        self.assertTrue(glos)
        self.assertEqual({g["origen"] for g in glos}, {"declarada"}, "tres misiones coincidentes no la verifican")
        mem.registrar_termino(self.base, "gast", glos[0]["objeto"], "declarada", "OTRO-AGENTE")
        self.assertEqual({g["origen"] for g in mem.glosario(self.base) if g["raiz"] == "gast"}, {"declarada"},
                         "la coincidencia con otro agente tampoco la verifica")


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class NombresRepetidosTests(Base, unittest.TestCase):
    """B1 con dos clientes del mismo nombre que JUNTOS gastan más que el ganador real."""

    @classmethod
    def setUpClass(cls):
        cls.preparar("b1_dup", lambda srv, b: G.crear(srv, b, variante="nombres_repetidos"))

    def test_grouping_by_the_name_would_answer_another_entity(self):
        v = self.verdad["preguntas"]["Q2"]
        self.assertNotEqual(v["por_nombre_daria"][0], v["respuesta"])
        self.assertGreater(float(v["por_nombre_daria"][1]), float(v["valor"]))

    def test_the_key_keeps_the_identity_and_the_label_is_only_shown(self):
        inf, dec, _, _ = self.correr(Q2, Guion("competente"))      # el guion agrupa por clientes.nombre
        self.assertIn("AGRUPAR clientes.nombre se reemplazó por la clave clientes.id con clientes.nombre como etiqueta",
                      next(p for r, p in dec if r == "identidad_normalizada"))
        e = inf["respuesta"]["estructurada"]
        v = self.verdad["preguntas"]["Q2"]
        self.assertEqual((e["clave"], e["etiqueta"], str(e["valor"])), ([v["clave"]], [v["respuesta"]], v["valor"]))
        self.assertIn(str(v["clave"]), inf["respuesta"]["texto"], "la etiqueta se repite: S6 exige citar la clave")
        cheq = {c["id"]: c for c in inf["chequeos"]}
        self.assertEqual(cheq["S7_identidad"]["estado"], "ok")
        self.assertIn("etiqueta visible: clientes.nombre (la clave se conserva)", cheq["S7_identidad"]["detalle"])
        j = J.evaluar(inf, self.verdad, "Q2")
        self.assertEqual((inf["nivel"], j["correcta"], j["confirmacion_falsa"]), ("incierta", True, False))
        self.sin_alias(inf)

    def test_an_answer_without_the_key_is_rejected_when_the_label_repeats(self):
        inf, dec, _, _ = self.correr(Q2, Guionado(m4="RESPUESTA: Lucía Castro, con 400521.93."))
        self.assertIn("no menciona 59", next(p for r, p in dec if r == "m4_rechazada"))
        self.assertIn("clientes.id (clave) = 59", inf["respuesta"]["texto"])

    def test_the_judge_also_compares_the_key(self):
        inf = {"nivel": "incierta", "respuesta": {"estructurada": {"grupo": [self.verdad["preguntas"]["Q2"]["respuesta"]],
                                                                   "clave": [999], "valor": self.verdad["preguntas"]["Q2"]
                                                                   ["valor"]}}}
        self.assertFalse(J.evaluar(inf, self.verdad, "Q2")["correcta"], "mismo nombre, otra entidad")


OPACA = """
CREATE TABLE t_x (k INT PRIMARY KEY, n VARCHAR(20) NOT NULL) ENGINE=InnoDB;
CREATE TABLE t_y (k INT PRIMARY KEY, rx INT NOT NULL, f DATETIME NOT NULL,
  CONSTRAINT fk_y_x FOREIGN KEY (rx) REFERENCES t_x(k)) ENGINE=InnoDB;
CREATE TABLE t_w (k INT PRIMARY KEY, ry INT NOT NULL, q INT NOT NULL, p DECIMAL(10,2) NOT NULL,
  p2 DECIMAL(10,2) NOT NULL, CONSTRAINT fk_w_y FOREIGN KEY (ry) REFERENCES t_y(k)) ENGINE=InnoDB;
INSERT INTO t_x VALUES (1,'Uno'),(2,'Dos'),(3,'Dos'),(4,'Tres');
INSERT INTO t_y VALUES (1,1,'2026-03-05 10:00:00'),(2,2,'2026-03-06 10:00:00'),(3,2,'2026-03-07 10:00:00'),
  (4,3,'2026-03-08 10:00:00'),(5,4,'2026-03-09 10:00:00'),
  (6,4,'2026-04-05 10:00:00'),(7,2,'2026-04-06 10:00:00'),(8,3,'2026-04-07 10:00:00'),(9,1,'2026-04-08 10:00:00');
INSERT INTO t_w VALUES (1,1,10,100.00,20.00),(2,2,1,300.00,400.00),(3,3,1,300.00,400.00),(4,4,1,500.00,900.00),
  (5,5,2,50.00,60.00),(6,6,5,100.00,104.00),(7,7,3,90.00,95.00),(8,8,3,90.00,95.00),(9,9,1,10.00,10.00);
"""
# Marzo, por clave:  q 1:10 2:2 3:1 4:2 · p 1:100 2:600 3:500 4:50 · p2 1:20 2:800 3:900 4:60
#                    q·p 1:1000 2:600 3:500 4:100 · q·p2 1:200 2:800 3:900 4:120
# p y p2 son la misma magnitud (continua) y cambian el ganador (2 → 3): ambigüedad LEGÍTIMA. q (entera) gana con 1,
# pero es otra magnitud: no cuenta. Abril: la clave 4 gana con toda medida (q·p 500, q·p2 520), pero «Dos» (claves 2 y
# 3) junta q·p 540 y q·p2 570.
FILAS_OPACAS = {"t_x": "un cliente que compra", "t_y": "una compra de un cliente en una fecha",
                "t_w": "un artículo de una compra"}
M2_OPACA = "TABLAS: t_w, t_y, t_x\nPOR_QUE: medida, fecha y entidad"


def crear_opaca(srv, base):
    srv.sql(f"DROP DATABASE IF EXISTS `{base}`; CREATE DATABASE `{base}`; USE `{base}`;\n" + OPACA)
    return {}


def m3_opaca(medida, grupo="t_x.k", mes="03", palabras=("gastó",)):
    hasta = "2026-04-01" if mes == "03" else "2026-05-01"
    return (f"MEDIDA: {medida}\nAGRUPAR: {grupo}\nFILTRO: t_y.f desde 2026-{mes}-01 hasta {hasta}\nORDEN: mayor\n"
            f"LIMITE: 1" + "".join(f"\nINTERPRETA: {w} = {medida}" for w in palabras))


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class BaseOpacaTests(Base, unittest.TestCase):
    """Nombres opacos: el esquema no respalda ninguna palabra de la pregunta. Solo las fichas del modelo dicen qué
    es cada tabla, y eso no verifica nada."""

    @classmethod
    def setUpClass(cls):
        cls.preparar("opaca_2c", crear_opaca)

    def mision(self, m3, pregunta="¿Qué cliente gastó más en marzo de 2026?"):
        return self.correr(pregunta, Guionado(m3=[m3], m2=M2_OPACA, filas=FILAS_OPACAS))

    def test_a_same_magnitude_alternative_that_changes_the_winner_is_an_abstention(self):
        inf, dec, _, _ = self.mision(m3_opaca("suma t_w.p"))
        self.assertEqual(inf["nivel"], "abstencion")
        s = inf["sensibilidad"]
        self.assertEqual((s["clase"], s["confirmada"]), ("ganador_dependiente", False))
        self.assertEqual(s["elegida"]["ganador"], [["2"]])
        self.assertEqual({a["medida"]: a["ganador"] for a in s["alternativas"]}, {"suma(t_w.p2)": [["3"]]})
        self.assertIn("suma(t_w.q)", s["otras_magnitudes"], "q es otra magnitud: no se compara")

    def test_the_right_measure_is_not_distinguishable_either_without_semantic_support(self):
        """La sensibilidad no sabe cuál medida es la correcta: con q·p también se abstiene, porque q·p2 (la misma
        magnitud) da otro ganador y nada confirma cuál precio pide la pregunta."""
        inf, _, _, _ = self.mision(m3_opaca("suma t_w.q * t_w.p"))
        self.assertEqual((inf["nivel"], inf["sensibilidad"]["clase"]), ("abstencion", "ganador_dependiente"))

    def test_another_magnitude_that_changes_the_winner_does_not_abstain(self):
        """Prueba negativa del caso de M1 con nombres opacos: sumar q (entera) da el ganador 1; sumar dinero da 2 o 3.
        Esas son otras preguntas: q no tiene alternativas de su magnitud y la respuesta queda incierta, no abstenida."""
        inf, dec, _, _ = self.mision(m3_opaca("suma t_w.q", palabras=("compró", "unidades")),
                                     "¿Qué cliente compró más unidades en marzo de 2026?")
        s = inf["sensibilidad"]
        self.assertEqual((inf["nivel"], s["clase"], s["consultas"]), ("incierta", "sin_alternativas", 0))
        self.assertEqual(inf["respuesta"]["estructurada"]["clave"], [1])
        self.assertTrue({"suma(t_w.p)", "suma(t_w.p2)"} <= set(s["otras_magnitudes"]))

    def test_a_stable_winner_with_dependent_value_is_uncertain_and_keeps_the_key(self):
        inf, _, _, _ = self.mision(m3_opaca("suma t_w.q * t_w.p", mes="04"),
                                   "¿Qué cliente gastó más en abril de 2026?")
        self.assertEqual((inf["nivel"], inf["sensibilidad"]["clase"]), ("incierta", "valor_dependiente"))
        e = inf["respuesta"]["estructurada"]
        self.assertEqual((e["clave"], e["etiqueta"], str(e["valor"])), ([4], ["Tres"], "500.00"))
        self.assertIn("(clave 4)", inf["respuesta"]["texto"], "la etiqueta t_x.n se repite en la tabla")
        self.sin_alias(inf)

    def test_grouping_by_a_repeated_opaque_name_is_reported_not_trusted(self):
        """Con nombres opacos la regla no puede saber que «cliente» es una fila de t_x: no reagrupa por la clave.
        S7 informa que t_x.n repite valores (y el ganador «Dos» junta dos entidades): incierta, nunca verificada."""
        inf, dec, _, _ = self.mision(m3_opaca("suma t_w.q * t_w.p", grupo="t_x.n", mes="04"),
                                     "¿Qué cliente gastó más en abril de 2026?")
        cheq = {c["id"]: c for c in inf["chequeos"]}
        self.assertEqual(cheq["S7_identidad"]["estado"], "no_verificable")
        self.assertIn("repite valores", cheq["S7_identidad"]["detalle"])
        self.assertEqual((inf["nivel"], inf["respuesta"]["estructurada"]["grupo"]), ("incierta", ["Dos"]))
        self.assertNotIn("identidad_normalizada", [r for r, _ in dec])

    def test_identity_rules_on_opaque_names(self):
        schema = {"tablas": {"t_x": {"columnas": [
            {"nombre": "k", "tipo": "int(11)", "clave": "PRI"}, {"nombre": "n", "tipo": "varchar(20)", "clave": ""}]}}}
        perfiles = {"t_x": {"filas": 4, "columnas": {"k": {"distintos": 4, "no_nulos": 4},
                                                     "n": {"distintos": 3, "no_nulos": 4}}}}
        plan = {"agrupar": [("t_x", "k")]}
        ID.resolver(plan, schema, perfiles, {}, [], "cliente", ["t_x"])
        self.assertEqual((plan["agrupar"], plan["etiquetas"], plan["identidad"][0]["tipo"]),
                         ([("t_x", "k")], [("t_x", "n")], "clave"))


if __name__ == "__main__":
    unittest.main()
