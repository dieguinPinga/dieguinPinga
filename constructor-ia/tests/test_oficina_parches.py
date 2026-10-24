"""Experimento 7.3 (OFICINA.md, «Experimento 7.3»): reparación mediante parches mínimos.

SIN inferencias reales: revisor y programador simulados y el sandbox real. El coordinador localiza zonas autorizadas
(citas comprobadas y evidencia sintética, análisis estático), valida el parche JSON y lo aplica de forma determinista
sobre una copia. El criterio de éxito no cambia: discrepancia eliminada y verificación independiente completa.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import parches as PA
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.autocorreccion import sin_comentarios
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR, mision_conciliar_movimientos
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import CASO_DE_ANULACION, A
from .test_oficina_revision_existente import Base, revision

F = "conciliar_movimientos"
SPEC = mision_conciliar_movimientos()["spec"]
CITAS = ['if tipo != "anulacion":']
LITERALES = ["anulacion_de_anulacion", "anulacion_inexistente"]
REGISTRO = '            aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}'
# Quitar la condición que deja afuera a las anulaciones.
QUITAR_CONDICION = {"original": '        if tipo != "anulacion":\n' + REGISTRO, "reemplazo": REGISTRO[4:]}
# El parche que propuso Qwen en la prueba directa: registrar también las anulaciones válidas.
COMO_QWEN = {"original": '            anulados.add(ref["id"])',
             "reemplazo": '            anulados.add(ref["id"])\n' + REGISTRO}
# Un cambio dentro de la zona que no afecta la salida del caso.
IRRELEVANTE = {"original": "        n += 1", "reemplazo": "        n = n + 1"}


def parche(*cambios, justificacion="registrar las anulaciones aplicadas"):
    return json.dumps({"cambios": list(cambios), "justificacion": justificacion}, ensure_ascii=False)


def zonas():
    return PA.localizar(A, F, CITAS, LITERALES)["zonas"]


class LocalizacionTests(unittest.TestCase):
    def test_zones_come_from_verified_citations_and_synthetic_evidence(self):
        loc = PA.localizar(A, F, CITAS, LITERALES)
        self.assertEqual(loc["zonas"], [(27, 37), (41, 46)])
        self.assertEqual(sorted((a["linea"], a["origen"]) for a in loc["anclas"]),
                         [(29, "literal del caso sintético"), (32, "literal del caso sintético"),
                          (44, "cita del revisor")])
        texto = PA.texto_zonas(A, F, loc)
        self.assertIn(" 44|         if tipo != \"anulacion\":", texto)
        self.assertIn("Contexto de SOLO LECTURA", texto)
        self.assertNotIn("for m in sorted(", texto, "fuera de las zonas no se muestra")

    def test_literals_come_only_from_the_synthetic_difference(self):
        comp = RE.comparar_salidas({"inconsistencias": [{"id": 4, "motivo": "anulacion_de_anulacion"}], "saldos": {}},
                                   {"inconsistencias": [{"id": 4, "motivo": "anulacion_inexistente"}], "saldos": {}})
        self.assertEqual(PA.literales_de_la_diferencia(comp), LITERALES)

    def test_without_anchors_the_whole_function_is_authorized_and_recorded(self):
        loc = PA.localizar(A, F, [], [])
        self.assertEqual((loc["zonas"], loc["metodo"]), ([(1, len(A.splitlines()))], "sin anclas: la función completa"))


class ParcheTests(unittest.TestCase):
    def aplicar(self, texto):
        return PA.aplicar(A, PA.interpretar_parche(texto, F), zonas(), SPEC)

    def test_valid_patches_are_applied_deterministically_on_a_copy(self):
        codigo, aplicacion = self.aplicar(parche(QUITAR_CONDICION))
        self.assertEqual(codigo, sin_comentarios(REFERENCIA_CONCILIAR))
        self.assertEqual(aplicacion, [{"cambio": 1, "lineas": [44, 45], "lineas_nuevas": 1}])
        codigo, _ = self.aplicar(parche(COMO_QWEN))
        self.assertIn('anulados.add(ref["id"])\n' + REGISTRO + "\n", codigo)
        self.assertIn("if tipo != \"anulacion\":", A, "el original no se toca")
        # Espacios finales distintos: mismo parche.
        self.assertEqual(PA.clave(PA.interpretar_parche(parche(COMO_QWEN), F)),
                         PA.clave(PA.interpretar_parche(parche(dict(COMO_QWEN, original=COMO_QWEN["original"] + "  ")),
                                                        F)))

    def test_invalid_patches_are_rejected_with_their_reason(self):
        casos = {
            "no es JSON": "no contiene un objeto JSON",
            json.dumps({"justificacion": "x"}): "falta la lista «cambios»",
            parche(): "el parche está vacío",
            parche({"original": "   ", "reemplazo": "x = 1"}): "el fragmento original está vacío",
            parche({"original": "    total = 0", "reemplazo": "    total = 1"}): "no aparece en el código",
            parche({"original": "                continue", "reemplazo": "                pass"}): "aparece 4 veces",
            parche({"original": REGISTRO, "reemplazo": "            aplicados[mid] = {"}): "no es válido",
            parche({"original": "    saldos = dict(saldos_iniciales or {})", "reemplazo": "    saldos = {}"}):
                "fuera de las zonas autorizadas",
            parche({"original": "        n += 1", "reemplazo": "        n += 1"}): "igual al original",
            parche(QUITAR_CONDICION, {"original": REGISTRO, "reemplazo": "            pass"}): "se superponen",
            parche(*[IRRELEVANTE] * 4): "el máximo es 3",
            parche({"original": "        n += 1", "reemplazo": f"def {F}(m):\n    return {{}}"}):
                "reescribe la función completa",
        }
        for texto, motivo in casos.items():
            with self.subTest(motivo=motivo):
                with self.assertRaisesRegex(PA.ParcheRechazado, motivo):
                    self.aplicar(texto)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(Base):
    def correr73(self, respuestas, **conf):
        return self.correr(respuestas, variante="7.3", **conf)

    def test_a_valid_patch_removes_the_discrepancy_and_passes_the_independent_battery(self):
        inf, fake, d = self.correr73([revision(CASO_DE_ANULACION), parche(COMO_QWEN)])
        self.assertTrue(inf["exito"], inf.get("detenido"))
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))
        rep = inf["reparaciones"][0]
        self.assertEqual((rep["discrepancia_sintetica"], rep["parche"]["aplicado"]), ("eliminada", True))
        self.assertEqual(rep["localizacion"]["zonas"], [(27, 37), (41, 46)])
        pedido = fake.requests[1]
        self.assertEqual(pedido["messages"][0]["content"], PA.SISTEMA_PARCHE)
        self.assertEqual(pedido.get("format"), "json")
        usuario = pedido["messages"][1]["content"]
        self.assertIn("ZONA AUTORIZADA 2 (líneas 41–46)", usuario)
        self.assertNotIn("for m in sorted(", usuario, "recibe el fragmento, no el archivo")
        for f in ("pedido.json", "respuesta.txt", "parche.json", "solucion.py", "reparacion.json"):
            self.assertTrue((d / "reparacion-1" / f).exists(), f)
        self.assertEqual(json.loads((d / "reparacion-1/parche.json").read_text())["aplicacion"],
                         [{"cambio": 1, "lineas": [43, 43], "lineas_nuevas": 2}])
        self.assertEqual([ll.get("etapa") for ll in inf["consumo"]["llamadas"]], [None, "parche"])
        self.assertEqual(inf["consumo"]["programador_por_etapa"]["parche"]["llamadas"], 1)
        # Sin filtraciones.
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        for secreto in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR,
                        RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"]):
            self.assertNotIn(secreto, usuario)

    def test_a_rejected_patch_gets_its_reason_and_uses_one_attempt(self):
        inf, fake, d = self.correr73([revision(CASO_DE_ANULACION),
                                      parche({"original": "    total = 0", "reemplazo": "    total = 1"}),
                                      parche(QUITAR_CONDICION)], max_reparaciones=2)
        self.assertTrue(inf["exito"])
        self.assertEqual([r["estado"] for r in inf["reparaciones"]], ["no_ejecutable", "ejecutada"])
        self.assertFalse(inf["reparaciones"][0]["parche"]["aplicado"])
        self.assertFalse((d / "reparacion-1/solucion.py").exists(), "un parche rechazado no produce código")
        self.assertIn("Tu parche fue rechazado: cambio 1: el fragmento original no aparece en el código",
                      fake.requests[2]["messages"][1]["content"])

    def test_a_patch_without_effect_gets_the_7_1_feedback_and_new_zones(self):
        inf, fake, _ = self.correr73([revision(CASO_DE_ANULACION), parche(IRRELEVANTE), parche(COMO_QWEN)],
                                     max_reparaciones=2)
        self.assertTrue(inf["exito"])
        r1 = inf["reparaciones"][0]
        self.assertEqual((r1["discrepancia_sintetica"], r1["misma_salida_que"]), ("persiste", [0]))
        segundo = fake.requests[2]["messages"][1]["content"]
        for texto in ("RESULTADO DE TU INTENTO 1 EN EL CASO DEL REVISOR", "SIGUE DISTINTA en 'inconsistencias'",
                      "SIN EFECTO EN ESTE CASO", "-        n += 1", "+        n = n + 1",
                      "CÓDIGO ACTUAL de solucion.py (tu intento 1)", " 46|         n = n + 1"):
            self.assertIn(texto, segundo)

    def test_repeating_a_patch_stops_after_the_warning(self):
        malo = parche({"original": "    total = 0", "reemplazo": "    total = 1"})
        inf, fake, _ = self.correr73([revision(CASO_DE_ANULACION), malo, malo, malo, parche(COMO_QWEN)],
                                     max_reparaciones=4)
        self.assertEqual(len(fake.requests), 4, "revisor + 3 parches; el cuarto no se pide")
        self.assertEqual([r.get("parche_repetido_de") for r in inf["reparaciones"]], [None, 1, 1])
        self.assertIn("REPETICIÓN: es el mismo parche que tu intento 1", fake.requests[3]["messages"][1]["content"])
        self.assertIn("repitió un parche ya probado después del aviso", inf["detenido"])
        self.assertFalse(inf["exito"])

    def test_an_applied_patch_repeated_on_its_own_result_is_detected(self):
        inf, _, _ = self.correr73([revision(CASO_DE_ANULACION), parche(IRRELEVANTE), parche(IRRELEVANTE),
                                   parche(IRRELEVANTE)], max_reparaciones=3)
        self.assertEqual([r["estado"] for r in inf["reparaciones"]], ["ejecutada", "no_ejecutable", "no_ejecutable"])
        self.assertEqual(inf["reparaciones"][1]["parche_repetido_de"], 1)
        self.assertIn("repitió un parche", inf["detenido"])

    def test_the_budget_limits_patches_too(self):
        inf, fake, _ = self.correr73([revision(CASO_DE_ANULACION), parche(COMO_QWEN)], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertIn("límite de tiempo de inferencia", inf["detenido"])
        llamadas = iter([None, "límite de tiempo de inferencia (simulado)"])
        with mock.patch.object(RE.Experimento, "_sin_tiempo", lambda self: next(llamadas)):
            inf, fake, _ = self.correr73([revision(CASO_DE_ANULACION), parche(COMO_QWEN)])
        self.assertEqual(len(fake.requests), 1, "solo el revisor")
        self.assertFalse(inf["exito"])

    def test_previous_variants_do_not_use_patches(self):
        for variante in ("7.1", "7.2"):
            with self.subTest(variante=variante):
                resp = [revision(CASO_DE_ANULACION)] + ([json.dumps({
                    "causa": "c", "elementos": ["aplicados"], "modificacion": "m", "justificacion": "j",
                    "efectos_secundarios": []})] if variante == "7.2" else []) + [
                    f"```python\n{REFERENCIA_CONCILIAR}```\n"]
                inf, fake, d = self.correr(resp, variante=variante)
                self.assertTrue(inf["exito"])
                self.assertFalse((d / "reparacion-1/parche.json").exists())
                self.assertNotIn("localizacion", inf["reparaciones"][0])
                self.assertNotIn("ZONA AUTORIZADA", fake.requests[-1]["messages"][1]["content"])


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CliTests(unittest.TestCase):
    def test_variant_7_3_from_the_command_line(self):
        d = Path(tempfile.mkdtemp(prefix="cia-exp73-cli-"))
        lab = d / "oficina-revision-7-3"
        fake = FakeOllama([revision(CASO_DE_ANULACION), parche(COMO_QWEN)], models=("gemma4:12b",))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()
        out, err = StringIO(), StringIO()
        ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
        with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
            rc = cli.main(["--config", str(d / "no-existe.toml"), "oficina", "revisar-existente", "--carpeta",
                           str(lab), "--trabajador", "prueba", "--defecto", "anulaciones_no_registradas",
                           "--variante", "7.3", "--autorizo-inferencia-remota"])
        texto = out.getvalue()
        self.assertEqual(rc, 0, texto + err.getvalue())
        self.assertIn("variante 7.3 (parches mínimos", texto)
        self.assertIn("programador (parche 1)", texto)
        self.assertIn("Verificación independiente del intento 1: 9/9 APROBADA", texto)
        self.assertIn("Experimento 7: ÉXITO", texto)


if __name__ == "__main__":
    unittest.main()
