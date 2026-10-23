"""Escenario estándar del almacén (OFICINA.md): 20 ventas, misiones A–F y nueve trabajadores simulados.

Sin red, sin modelos y sin otras bases. Comprueba los datos y la verdad (dos fuentes que coinciden), los errores
reproducibles de cada perfil, la verificación central sin revelar la verdad y el ensayo completo.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from constructor_ia import cli
from constructor_ia.oficina import almacen as AL
from constructor_ia.oficina import ensayo_almacen as EA


class DatosTests(unittest.TestCase):
    def test_twenty_records_with_easy_integer_numbers(self):
        self.assertEqual(len(AL.REGISTROS), 20)
        self.assertEqual([r[0] for r in AL.REGISTROS], list(range(1, 21)))
        precios = {p: {r[4] for r in AL.REGISTROS if r[2] == p} for p in ("Arroz", "Fideos", "Yerba", "Aceite")}
        self.assertEqual(precios, {"Arroz": {2}, "Fideos": {3}, "Yerba": {5}, "Aceite": {10}})
        self.assertEqual(sum(r[3] * r[4] for r in AL.REGISTROS), 235)
        self.assertEqual(sum(r[3] for r in AL.REGISTROS), 56)

    def test_the_computed_truth_matches_the_hand_written_table(self):
        v = AL.verdad()
        self.assertTrue(v.pop("_canario").startswith("VERDAD-ALMACEN-"))
        self.assertEqual(v, AL.ESPERADO)

    def test_the_two_rankings_are_inverse(self):
        por = AL.ESPERADO["por_producto"]["por_producto"]
        por_unidades = sorted(por, key=lambda p: -por[p]["unidades"])
        por_importe = sorted(por, key=lambda p: -por[p]["importe"])
        self.assertEqual(por_unidades, ["Arroz", "Fideos", "Yerba", "Aceite"])
        self.assertEqual(por_importe, list(reversed(por_unidades)))

    def test_missions_and_the_audit_has_its_own_cases(self):
        ms = AL.misiones()
        self.assertEqual([t["nombre"] for _, ts in ms for t in ts],
                         ["periodo", "importe_total", "unidades_total", "por_producto", "mas_unidades", "mas_importe",
                          "auditar_220", "auditar_235"])
        auditoria = dict(ms)["F · auditoría ($220 y $235)"]
        self.assertEqual([t["depende_de"] for t in auditoria], [[], []], "F no depende de A")
        self.assertEqual([t["carga"]["propuesta_a_auditar"]["importe"] for t in auditoria], [220, 235])
        self.assertEqual({t["rol"] for t in auditoria}, {"auditor"})

    def test_no_task_load_carries_the_truth(self):
        v = AL.verdad()
        for _, tareas in AL.misiones():
            for t in tareas:
                texto = json.dumps(t, ensure_ascii=False)
                self.assertNotIn(v["_canario"], texto)
                self.assertNotIn('"valor": 235', texto, "el auditor no recibe el valor recalculado")
                if t["rol"] == "auditor":
                    self.assertEqual(t["carga"]["formato"]["dictamen"], "correcto | incorrecto",
                                     "solo el formato, no el dictamen esperado")
                self.assertNotIn("esperado", texto)


class PerfilesTests(unittest.TestCase):
    """Cada perfil comete su error, reproducible, solo con la carga; el coordinador lo rechaza sin revelar el valor."""

    def carga(self, tipo):
        return next(t["carga"] for _, ts in AL.misiones() for t in ts if t["nombre"] == tipo)

    def test_each_profile_gives_its_known_answer(self):
        casos = {("correcto", "importe_total"): {"importe": 235},
                 ("distraido", "importe_total"): {"importe": 220},
                 ("confundido", "importe_total"): {"importe": 100},
                 ("confundido", "unidades_total"): {"unidades": 20},
                 ("mezclador", "importe_total"): {"importe": 56},
                 ("periodo_corrido", "periodo"): {"importe": 102, "unidades": 26},
                 ("periodo_corrido", "importe_total"): {"importe": 235},
                 ("formato_roto", "importe_total"): {"texto": "no pude leer la tabla"},
                 ("complaciente", "auditar_220"): {"dictamen": "correcto", "valor": 220},
                 ("riguroso", "auditar_220"): {"dictamen": "incorrecto", "valor": 235},
                 ("riguroso", "auditar_235"): {"dictamen": "correcto", "valor": 235},
                 ("correcto", "mas_unidades"): {"producto": "Arroz", "valor": 20},
                 ("correcto", "mas_importe"): {"producto": "Aceite", "valor": 90},
                 ("correcto", "periodo"): {"importe": 92, "unidades": 25}}
        for (perfil, tipo), esperado in casos.items():
            with self.subTest(perfil=perfil, tipo=tipo):
                self.assertEqual(EA.resolver(self.carga(tipo), perfil), esperado)

    def test_verification_rejects_without_revealing_the_value(self):
        v = AL.verdad()
        ok, motivo = AL.verificar("importe_total", {"importe": 220}, v)
        self.assertEqual((ok, motivo), (False, "no coincide con el cálculo independiente en: importe"))
        self.assertNotIn("235", motivo)
        self.assertFalse(AL.verificar("importe_total", {"importe": "235"}, v)[0], "un texto no es un entero")
        self.assertFalse(AL.verificar("importe_total", {"importe": True}, v)[0])
        self.assertIn("formato inválido", AL.verificar("importe_total", {"texto": "x"}, v)[1])
        ok, motivo = AL.verificar("por_producto", EA.resolver(self.carga("por_producto"), "distraido"), v)
        self.assertEqual((ok, motivo), (False, "no coincide con el cálculo independiente en: Yerba"))
        self.assertFalse(AL.verificar("auditar_220", {"dictamen": "correcto", "valor": 220}, v)[0],
                         "la auditoría complaciente se rechaza")
        for tipo in AL.ESPERADO:
            self.assertTrue(AL.verificar(tipo, EA.resolver(self.carga(tipo), "correcto"), v)[0], tipo)


class EnsayoAlmacenTests(unittest.TestCase):
    def setUp(self):
        self.r = EA.correr(Path(tempfile.mkdtemp(prefix="cia-almacen-")), log=lambda *_: None)

    def test_all_missions_complete_with_zero_false_verifications(self):
        self.assertTrue(self.r["ok"], EA.texto(self.r))
        self.assertEqual(set(self.r["misiones"].values()), {"completa"})
        self.assertEqual(self.r["verificaciones_falsas"], [])
        self.assertEqual(self.r["resultados"], AL.ESPERADO, "cada resultado verificado es el de la tabla")
        self.assertTrue(self.r["verdad_coincide_con_esperado"])
        self.assertEqual(self.r["fugas"], [])

    def test_every_planned_profile_participated_and_showed_its_behavior(self):
        pp = self.r["por_perfil"]
        self.assertEqual(self.r["sin_participar"], [])
        self.assertEqual(set(pp), set(EA.ORDEN))
        self.assertEqual(len(pp), 9)
        for p in EA.UN_TURNO:
            self.assertEqual((pp[p]["tomadas"], pp[p]["rechazadas"], pp[p]["aceptadas"]), (1, 1, 0), p)
        self.assertEqual((pp["desconectado"]["abandonadas"], pp["desconectado"]["tardias_rechazadas"]), (1, 1))
        self.assertGreaterEqual(pp["riguroso"]["aceptadas"], 1)
        self.assertGreaterEqual(pp["correcto"]["aceptadas"], 1)
        self.assertEqual(pp["correcto"]["rechazadas"], 0)
        self.assertEqual(pp["riguroso"]["rechazadas"], 0)

    def test_errors_retries_disconnections_and_recoveries_are_observable(self):
        r = self.r
        self.assertGreaterEqual(r["rechazos"], 6)
        self.assertGreaterEqual(r["recuperadas"], 1)
        rechazadas = {(v["quien"], v["tarea"]) for v in r["verificaciones"] if v["decision"] == "rechazada"}
        self.assertTrue({("periodo_corrido", "periodo"), ("distraido", "importe_total"),
                         ("confundido", "unidades_total"), ("mezclador", "por_producto"),
                         ("complaciente", "auditar_220")} <= rechazadas, rechazadas)
        reintentadas = [v for v in r["verificaciones"] if v["decision"] == "aceptada" and v["intento"] > 1]
        self.assertTrue(reintentadas, "hay tareas aceptadas en un reintento")
        self.assertTrue(r["solape_entre_misiones"], "varias misiones avanzaron a la vez")

    def test_full_history_of_who_did_what_when_and_why(self):
        h = self.r["historial"]
        self.assertTrue(all({"ts", "de", "a", "detalle", "tarea_id", "mision_id"} <= set(e) for e in h))
        detalles = " ".join(e["detalle"] for e in h)
        for frase in ("misión creada", "propuesta entregada", "verificada: coincide con el cálculo independiente",
                      "propuesta rechazada por el coordinador: no coincide", "arriendo vencido"):
            self.assertIn(frase, detalles)
        self.assertTrue(any(e["a"] == "asignada" and e["trabajador_id"] == "desconectado" for e in h))
        for v in self.r["verificaciones"]:
            self.assertIn("motivo", v)
            self.assertNotIn("235", v["motivo"], "un motivo de rechazo nunca revela la verdad")


class CliAlmacenTests(unittest.TestCase):
    def test_rehearsal_command_writes_its_own_report(self):
        d = Path(tempfile.mkdtemp(prefix="cia-almacen-cli-"))
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(d / "no-existe.toml"), "oficina", "ensayo-almacen", "--carpeta",
                           str(d / "oficina")])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("Resultado: OK", out.getvalue())
        informes = list((d / "oficina" / "ensayos").glob("almacen-*/informe.json"))
        self.assertEqual(len(informes), 1)
        self.assertEqual(json.loads(informes[0].read_text())["verificaciones_falsas"], [])
        self.assertFalse((d / "oficina" / "oficina.db").exists(), "no toca la base real de la oficina")


if __name__ == "__main__":
    unittest.main()
