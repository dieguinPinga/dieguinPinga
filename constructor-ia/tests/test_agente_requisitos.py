"""Requisitos de la pregunta, cobertura de M2, formato y retroalimentación de M3, y habilitación de M2 (AGENTE_DATOS.md,
§13), con las respuestas REALES de la primera M1 con Qwen (tests/datos/llamadas-M1-fase2.jsonl) como regresión.

La reproducción de respuestas reales prueba las DECISIONES del coordinador ante esas respuestas; no dice cómo
respondería Qwen con los prompts nuevos. Los modelos de guion validan el circuito, no a Qwen.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from constructor_ia.agente_datos import experimento as EX
from constructor_ia.agente_datos import generador as G
from constructor_ia.agente_datos import juez as J
from constructor_ia.agente_datos import microtareas as MT
from constructor_ia.agente_datos import requisitos as RQ
from constructor_ia.agente_datos.coordinador import Coordinador
from constructor_ia.agente_datos.guion import Guion
from constructor_ia.agente_datos.memoria import Memoria

from . import mariadb_fixture
from .helpers import make_cfg

DATOS = Path(__file__).resolve().parent / "datos"
REALES = [json.loads(x) for x in (DATOS / "llamadas-M1-fase2.jsonl").read_text().splitlines() if x.strip()]
Q1 = G.PREGUNTAS["Q1"]


class DeducirTests(unittest.TestCase):
    def test_what_the_rule_can_determine(self):
        r = RQ.deducir(Q1)
        self.assertEqual((r["periodo"]["texto"], r["orden"], r["limite"], r["entidad"], r["aclaraciones"]),
                         ("03/2026", "mayor", 1, "producto", []))
        self.assertEqual(RQ.deducir("¿Cuáles son los 3 productos que más se vendieron en marzo de 2026?")["limite"], 3)
        self.assertEqual(RQ.deducir("¿Qué tres clientes gastaron menos en 2026?")["limite"], 3)
        self.assertEqual(RQ.deducir("¿Qué cliente gastó menos en 2026?")["orden"], "menor")
        sin = RQ.deducir("¿Qué cliente gastó más?")
        self.assertEqual((sin["periodo"], sin["aclaraciones"]), (None, []), "sin período: no se exige ninguno")

    def test_what_the_rule_cannot_determine_is_asked_never_invented(self):
        casos = {"¿Qué producto vendió más unidades en marzo?": "falta el año",
                 "¿Qué producto vendió más en marzo de 2026 y en abril de 2026?": "más de un período",
                 "¿Qué producto vendió más la semana pasada?": "pasada, semana",
                 "¿Qué producto vendió más en marzo de 2026 que el año pasado?": "pasado",
                 "¿Qué producto se vendió más y cuál menos en 2026?": "a la vez el mayor y el menor",
                 "¿Qué productos vendieron más de 100 unidades en 2026?": "compara con un valor",
                 "¿Qué productos vendieron más en 2026?": "pide varios",
                 "¿Cuáles 3 o 5 productos vendieron más en 2026?": "varias cantidades"}
        for pregunta, esperado in casos.items():
            with self.subTest(pregunta=pregunta):
                r = RQ.deducir(pregunta)
                self.assertTrue(r["aclaraciones"], r)
                self.assertIn(esperado, " ".join(r["aclaraciones"]))


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class RealM1RegressionTests(unittest.TestCase):
    """Las respuestas REALES de M1 (Qwen3 4B, VivoBook): la de M2 omitió pedidos; las de M3 pusieron la medida en MEDIR."""

    @classmethod
    def setUpClass(cls):
        cls.srv = mariadb_fixture.server()
        cls.d = Path(tempfile.mkdtemp(prefix="cia-req-"))
        cls.verdad = G.crear(cls.srv, "b1_req")
        G.crear_lector(cls.srv, "b1_req", "l_b1_req", "clave-req")
        cls.pw = cls.d / "pw"
        cls.pw.write_text("clave-req\n")
        os.chmod(cls.pw, 0o600)

    def correr(self, modelo, pregunta=Q1, lab=None):
        from constructor_ia.config import ExplorerConnection
        from constructor_ia.explorer.broker import Broker
        cfg = make_cfg()
        lab = lab or Path(tempfile.mkdtemp(dir=self.d))
        mem = Memoria(lab / "agente.db")
        self.addCleanup(mem.cerrar)
        b = Broker(cfg, ExplorerConnection("b1", "b1_req", "l_b1_req", str(self.pw), socket=self.srv.socket), mem.db)
        coord = Coordinador(cfg, b, mem, modelo, conexion="b1", base="b1_req", carpeta=lab / "misiones",
                            log=lambda *_: None)
        inf = coord.ejecutar(pregunta)
        return inf, [(d["regla"], d["porque"]) for d in mem.decisiones(inf["mision"])], coord, lab

    class Reproduccion:
        """Devuelve las respuestas reales por microtarea, en orden; agotadas, repite la última (un modelo terco)."""
        nombre = "reproduccion"

        def __init__(self):
            self.colas = {}
            for r in REALES:
                self.colas.setdefault(r["tipo"], []).append(r["respuesta"])
            self.pedidos = []

        def pedir(self, tipo, prompt, **k):
            mt = tipo.split(":")[1]
            self.pedidos.append((mt, prompt))
            cola = self.colas[mt]
            return (cola.pop(0) if len(cola) > 1 else cola[0]), {"resultado": "completa"}

    def test_real_m1_and_m2_answers_m2_is_rejected_before_advancing(self):
        modelo = self.Reproduccion()
        inf, decisiones, coord, _ = self.correr(modelo)
        prompts_m1 = [p for mt, p in modelo.pedidos if mt == "M1"]
        reales_m1 = ["".join(t for _, t in r["partes"]) for r in REALES if r["tipo"] == "M1"]
        self.assertEqual(prompts_m1, reales_m1, "M1 no cambió: los mismos prompts que en la VivoBook")
        self.assertEqual(len(coord.fichas), 6)
        self.assertEqual({c: r["rol"] for c, r in coord.fichas["pedidos"]["roles"].items()},
                         {"id": "identificador", "cliente_id": "referencia", "fecha": "tiempo", "estado": "categoria"})
        cobertura = [p for r, p in decisiones if r == "m2_cobertura_incompleta"]
        self.assertEqual(len(cobertura), 2, "la respuesta real de M2 (repetida) se rechaza las dos veces")
        self.assertIn("la pregunta pide un período (03/2026), pero ninguna tabla elegida tiene una columna de rol "
                      "«tiempo». Columnas de tiempo en tablas conectadas (orden alfabético; no indica preferencia): "
                      "pedidos.fecha (unida por lineas_pedido.pedido_id → pedidos.id); pedidos_borrador.creado (unida "
                      "por pedidos_borrador.producto_id → productos.id)", cobertura[0])
        self.assertNotIn("M3", [mt for mt, _ in modelo.pedidos], "no se avanzó a M3 con requisitos incompletos")
        self.assertEqual((inf["estado"], inf["nivel"]), ("completa", "abstencion"))
        segundo_m2 = [p for mt, p in modelo.pedidos if mt == "M2"][1]
        self.assertIn("TU RESPUESTA ANTERIOR SE RECHAZÓ: elegiste lineas_pedido, productos; la pregunta pide un "
                      "período (03/2026)", segundo_m2)

    def test_real_m3_answers_with_the_new_rules(self):
        _, _, coord, _ = self.correr(Guion("competente"))
        tablas = ["lineas_pedido", "productos", "pedidos"]
        resolver = coord._resolver(tablas)
        m3 = [r["respuesta"] for r in REALES if r["tipo"] == "M3"]
        # 1.ª: «MEDIR: suma» sin columna: es un problema de CONTENIDO, se sigue rechazando, y la retroalimentación cita.
        with self.assertRaises(MT.Rechazo) as e:
            MT.parsear_m3(m3[0], resolver)
        self.assertEqual(e.exception.motivo, "«suma» necesita una columna")
        retro = MT.retro_m3(m3[0], e.exception.motivo)
        self.assertIn("Tu respuesta fue:\n    MEDIR: suma\n    AGRUPAR: producto_id\n    INTERPRETA: producto = "
                      "lineas_pedido.producto_id\n  Se rechazó: «suma» necesita una columna", retro)
        # 2.ª: la medida en MEDIR se normaliza (registrado); el contenido se valida igual.
        prop = MT.parsear_m3(m3[1], resolver)
        self.assertEqual(prop["plan"]["medida"], {"funcion": "suma", "columnas": [("lineas_pedido", "cantidad")]})
        self.assertEqual(prop["plan"]["normalizaciones"], ["«MEDIR: suma lineas_pedido.cantidad» se leyó como «MEDIDA: "
                                                           "suma lineas_pedido.cantidad»"])
        faltan = RQ.faltantes(coord.req, prop["plan"], coord.schema, tablas, coord.fichas)
        self.assertEqual(faltan, [("falta el FILTRO del período 03/2026 sobre una columna de tiempo",
                                   "agregá «FILTRO: <tabla.columna de tiempo> desde 2026-03-01 hasta 2026-04-01»; "
                                   "columnas de tiempo de las tablas elegidas: pedidos.fecha")])
        # Una columna sin tabla se completa solo si es única entre las elegidas.
        self.assertEqual(resolver("nombre"), "productos.nombre")
        with self.assertRaisesRegex(Exception, "está en varias tablas elegidas"):
            resolver("id")

    def test_m3_prompt_has_no_fixed_line_and_lists_every_option_without_preference(self):
        modelo = Guion("competente")
        inf, _, _, lab = self.correr(modelo)
        llamadas = [json.loads(x) for x in next((lab / "misiones").iterdir()).joinpath("llamadas.jsonl")
                    .read_text().splitlines()]
        p = "".join(t for _, t in next(x for x in llamadas if x["tipo"] == "M3")["partes"])
        self.assertNotIn("MEDIR: agregado", p)
        self.assertIn("- período 03/2026: FILTRO OBLIGATORIO sobre una columna de tiempo, con estas fechas exactas: "
                      "«FILTRO: <una de: pedidos.fecha> desde 2026-03-01 hasta 2026-04-01»", p)
        self.assertIn("columnas de medida de las tablas elegidas (orden alfabético, sin preferencia): "
                      "lineas_pedido.cantidad, lineas_pedido.precio_unitario, productos.precio_lista", p)
        self.assertIn("forma: MEDIDA: suma <una de: lineas_pedido.cantidad | lineas_pedido.precio_unitario | "
                      "productos.precio_lista>", p)
        self.assertIn("- pide el mayor: «ORDEN: mayor» y «LIMITE: 1»", p)
        self.assertNotIn(self.verdad["canario"], p)
        _, _, coord, _ = self.correr(Guion("competente"), "¿Qué cliente gastó más?")
        f = MT.formato_m3(coord.req, ["lineas_pedido", "clientes", "pedidos"], coord.fichas)
        self.assertNotIn("FILTRO OBLIGATORIO", f)
        self.assertIn("desde AAAA-MM-DD hasta AAAA-MM-DD   (incluye desde, EXCLUYE hasta)   (opcional)", f)

    def test_a_model_with_the_real_errors_that_follows_feedback_gets_a_correct_answer(self):
        inf, decisiones, coord, lab = self.correr(Guion("qwen_m1"))
        reglas = [r for r, _ in decisiones]
        for r in ("m2_cobertura_incompleta", "forma_normalizada", "completado_por_regla"):
            self.assertIn(r, reglas)
        normal = next(p for r, p in decisiones if r == "forma_normalizada")
        self.assertIn("«MEDIR: suma lineas_pedido.cantidad» se leyó como «MEDIDA: suma lineas_pedido.cantidad»", normal)
        self.assertIn("«nombre» se completó como «productos.nombre»", normal)
        self.assertIn("ORDEN: mayor, LIMITE: 1 (deducidos de la pregunta", next(p for r, p in decisiones
                                                                               if r == "completado_por_regla"))
        j = J.evaluar(inf, self.verdad, "Q1")
        self.assertEqual((inf["estado"], inf["nivel"], j["correcta"], j["confirmacion_falsa"]),
                         ("completa", "incierta", True, False), "correcta, pero no verificada (respaldo de fichas)")
        self.assertEqual(inf["costo"]["por_microtarea"], {"M1": 2, "M2": 2, "M3": 1, "M4": 1})
        mem = Memoria(lab / "agente.db")
        self.addCleanup(mem.cerrar)
        q = mem.consulta_aprendida("b1", "que producto vendio mas unidades en marzo de 2026")
        # Desde la fase 2c se agrupa por la clave (productos.id); el nombre queda como etiqueta.
        self.assertTrue(q["medir_texto"].startswith("MEDIDA: suma lineas_pedido.cantidad\nAGRUPAR: productos.id"))
        self.assertIn("ORDEN: mayor\nLIMITE: 1", q["medir_texto"], "se guarda la forma canónica, ya completada")
        self.assertEqual(J.auditar_fugas([lab], self.verdad), [])

    def test_a_stubborn_model_abstains_before_m3_and_never_confirms_falsely(self):
        inf, decisiones, _, _ = self.correr(Guion("qwen_m1_terco"))
        j = J.evaluar(inf, self.verdad, "Q1")
        self.assertEqual((inf["nivel"], j["abstencion"], j["confirmacion_falsa"]), ("abstencion", True, False))
        self.assertNotIn("M3", inf["costo"]["por_microtarea"])
        self.assertEqual([r for r, _ in decisiones].count("m2_cobertura_incompleta"), 2)

    def test_an_undeterminable_question_asks_for_clarification_without_choosing_tables(self):
        inf, decisiones, _, _ = self.correr(Guion("competente"), "¿Qué producto vendió más unidades en marzo?")
        self.assertEqual((inf["estado"], inf["nivel"]), ("completa", "abstencion"))
        self.assertIn("aclaración necesaria", inf["dudas_o_motivos"][0])
        self.assertIn("falta el año", inf["dudas_o_motivos"][0])
        self.assertEqual(set(inf["costo"]["por_microtarea"]), {"M1"}, "solo se describieron tablas; no se eligió nada")
        self.assertIn("aclaracion_necesaria", [r for r, _ in decisiones])


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class M2EnablementTests(unittest.TestCase):
    """M2 depende de una respuesta REAL y CORRECTA de M1 según el juez, no solo de «completa»."""

    def resultado(self, carpeta, nivel, correcta, falsa=False):
        (carpeta / "resultados").mkdir(parents=True, exist_ok=True)
        (carpeta / "resultados" / "M1.json").write_text(json.dumps(
            {"coordinador": {"nivel": nivel}, "juez": {"correcta": correcta, "confirmacion_falsa": falsa}}))

    def test_criterion(self):
        d = Path(tempfile.mkdtemp())
        estado = {"M1": {"estado": "completa"}}
        self.resultado(d, "abstencion", None)
        self.assertEqual(EX.habilitacion(d, "M1", estado), "M1 terminó completa pero sin respuesta (nivel: abstencion)")
        self.resultado(d, "incierta", False)
        self.assertIn("incorrecta según el juez", EX.habilitacion(d, "M1", estado))
        self.resultado(d, "verificada", False, falsa=True)
        self.assertIn("CONFIRMACIÓN FALSA", EX.habilitacion(d, "M1", estado))
        self.resultado(d, "incierta", True)
        self.assertIsNone(EX.habilitacion(d, "M1", estado))
        self.resultado(d, "verificada", True)
        self.assertIsNone(EX.habilitacion(d, "M1", estado))
        self.assertIn("estado: interrumpida", EX.habilitacion(d, "M1", {"M1": {"estado": "interrumpida"}}))

    def test_an_abstaining_m1_blocks_m2_and_the_verdict_never_reaches_the_agent(self):
        srv = mariadb_fixture.server()
        from .test_agente_fase2 import ServidorDePrueba
        exp = Path(tempfile.mkdtemp()) / "exp"
        fabrica = lambda d: ServidorDePrueba(srv, "b1_hab")  # noqa: E731
        plan = EX.preparar(exp, base="b1_hab", servidor=fabrica, log=lambda *_: None)
        self.assertIn("correcta según el juez", plan["criterio_para_M2"])
        m1 = EX.mision(exp, "M1", ["--modelo", "guion", "--guion", "qwen_m1_terco"], servidor=fabrica,
                       log=lambda *_: None)
        self.assertEqual((m1["estado"], m1["coordinador"]["nivel"]), ("completa", "abstencion"))
        with self.assertRaisesRegex(EX.ErrorExperimento, "M2 no está habilitada: M1 terminó completa pero sin "
                                                         "respuesta \\(nivel: abstencion\\)"):
            EX.mision(exp, "M2", ["--modelo", "guion"], servidor=fabrica, log=lambda *_: None)
        canario = json.loads((exp / "juez" / "verdad.json").read_text())["canario"].encode()
        for f in (exp / "memoria_m1_m2").rglob("*"):
            if f.is_file():
                datos = f.read_bytes()
                self.assertNotIn(canario, datos, f.name)
                for palabra in (b"confirmacion_falsa", b"\"juez\"", b"sin_veredicto", b"criterio_para_M2"):
                    self.assertNotIn(palabra, datos, f"{f.name}: el veredicto no entra en la memoria del agente")


if __name__ == "__main__":
    unittest.main()
