"""Experimento 7.13 (OFICINA.md, «Experimento 7.13»): evolución de seis candidatos independientes.

SIN inferencias reales: las respuestas de los modelos se simulan (un Ollama falso con gemma y qwen) y el sandbox es
real. Las respuestas simuladas son SALIDAS de modelo reutilizadas de las pruebas anteriores (7.7–7.12); ninguna llega a
un pedido como instrucción: los pedidos los arma el módulo y la auditoría comprueba que no contienen la causa, la
referencia ni la batería. Orden fijo: las líneas A…F en secuencia; dentro de cada una, sus generaciones y, en cada
generación, el paso previo de su estrategia (si lo tiene) y el parche.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import reparacion_coordinada as RC
from constructor_ia.oficina import reparacion_coordinada_11 as RC11
from constructor_ia.oficina import reparacion_evolutiva_13 as RE13
from constructor_ia.oficina import reparacion_guiada as RGD
from constructor_ia.oficina import reparacion_torneo_12 as RT12
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import MISIONES, REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_normalizacion_sangria import QWEN
from .test_oficina_parches import IRRELEVANTE, parche
from .test_oficina_reparacion_coordinada_8 import INVERTIR, gemma8
from .test_oficina_reparacion_coordinada_9 import APRUEBA, NO_CAUSA
from .test_oficina_reparacion_coordinada_11 import FUERA_DE_ZONA, INVERTIR_VARIANTE, PLAN_OK as PLAN_11, diag11, plan11
from .test_oficina_reparacion_guiada import INEXISTENTE, ROMPE_OTRA_REGLA
from .test_oficina_reparacion_torneo_12 import QWEN_EXACTO, torneo

F = "conciliar_movimientos"
PREVIO = {"A": None, "B": None, "C": "diagnostico", "D": "localizacion", "E": None, "F": "plan"}
CAIDA = Exception("servidor caído")                 # HTTP 500: la línea se detiene sin inferencia
# Un parche que se aplica y compila pero falla al ejecutar.
ROMPE_EJECUCION = {"original": 'if tipo != "anulacion":', "reemplazo": "if tipo.no_existe():"}


def pg(cambio, marca, revision=None):
    """Un parche simulado con la marca de su línea y generación en la justificación (y en la revisión)."""
    d = json.loads(cambio) if isinstance(cambio, str) else {"cambios": [cambio]}
    d["justificacion"] = f"[{marca}] justificación propia de esta línea y de esta generación"
    if revision:
        d["revision_de_la_hipotesis"] = f"[{marca}-R] {revision}"
    return json.dumps(d, ensure_ascii=False)


def previo(L, n, cambio_de_plan=True):
    """El paso previo simulado de la estrategia de la línea L en la generación n, con su marca."""
    marca = f"[MARCA-{L}{n}-H]"
    if L == "C":
        return gemma8(f"PRES-C{n}", causa=f"{marca} la escritura en aplicados está dentro de la condición del tipo, así "
                                          "que las anulaciones aplicadas no se registran en aplicados")
    if L == "D":
        return diag11(causa=f"{marca} las anulaciones aplicadas no quedan registradas en aplicados")
    return plan11('if tipo != "anulacion":', "quitar la condición para registrar todos los movimientos aplicados",
                  cambio="otro enfoque" if n > 1 and cambio_de_plan else None,
                  estado_a_corregir=f"{marca} aplicados no contiene las anulaciones aplicadas; tendría que contenerlas")


def evolucion(**lineas):
    """Las respuestas en el orden de los pedidos y la etiqueta (línea, generación, etapa) de cada pedido.
    Cada generación es un cambio, una respuesta cruda (str JSON sin tocar si empieza con «=»), CAIDA, o una tupla
    (paso previo, parche | None) para C, D y F. Una línea sin generaciones recibe CAIDA."""
    resp, etiquetas = [], []
    for L in "ABCDEF":
        gens = lineas.get(L) or [CAIDA]
        for n, gen in enumerate(gens, 1):
            pre, cambio = gen if isinstance(gen, tuple) else (None, gen)
            if PREVIO[L] and cambio is not CAIDA:
                resp.append(pre or previo(L, n))
                etiquetas.append((L, n, PREVIO[L]))
            if cambio is CAIDA:
                resp.append(CAIDA)
                etiquetas.append((L, n, PREVIO[L] or "parche"))
                continue
            if cambio is None:                      # el paso previo resultó inválido: no hay pedido de parche
                continue
            if isinstance(cambio, str) and cambio.startswith("="):
                resp.append(cambio[1:])
            else:
                resp.append(pg(cambio, f"MARCA-{L}{n}", "cambio de hipótesis respecto del intento anterior"
                               if n > 1 else None))
            etiquetas.append((L, n, "parche"))
    return resp, etiquetas


# Escenario general: A mejora en la segunda generación; B no progresa en tres; C supera el desarrollo pero no la
# batería; D sale de zona y después supera; E repite su programa dos veces; F supera en la primera.
ESCENARIO = dict(A=[INVERTIR, QWEN], B=[NO_CAUSA, IRRELEVANTE, INVERTIR], C=[ROMPE_OTRA_REGLA],
                 D=[FUERA_DE_ZONA, QWEN_EXACTO], E=[INVERTIR, INVERTIR_VARIANTE, INVERTIR], F=[QWEN])


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class EvolucionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from .test_oficina_revision_existente import Base
        Base.setUpClass()
        cls.ex = Base.ex
        b = Base()
        _, _, cls.origen = b.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INEXISTENTE, INEXISTENTE,
                                     INEXISTENTE], variante="7.3")
        b.doCleanups()

    def correr(self, respuestas, mod=RE13, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        destino = mod.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp713-")), self.origen)
        inf = mod.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def escenario(self, **k):
        resp, etiquetas = evolucion(**ESCENARIO)
        inf, fake, d = self.correr(resp, **k)
        self.assertEqual(len(fake.requests), len(etiquetas))
        return inf, fake, d, etiquetas

    def linea(self, inf, L):
        return next(x for x in inf["lineas"] if x["letra"] == L)

    def cats(self, inf, L):
        return [g["categoria"] for g in self.linea(inf, L)["generaciones"]]

    def test_lines_and_generations_are_independent(self):
        inf, fake, _, etiquetas = self.escenario()
        modelos = {"diagnostico": "gemma4:12b", "localizacion": "gemma4:12b", "plan": "qwen2.5-coder:7b"}
        for (L, n, etapa), r in zip(etiquetas, fake.requests):
            esperado = modelos.get(etapa) or ("gemma4:12b" if L == "E" else "qwen2.5-coder:7b")
            self.assertEqual(r["model"], esperado, (L, n, etapa))
            texto = json.dumps(r["messages"], ensure_ascii=False)
            for otra in "ABCDEF":
                for m in range(1, 4):
                    marca = f"[MARCA-{otra}{m}"
                    if otra != L or m > n:
                        self.assertNotIn(marca, texto, f"{(L, n, etapa)} contiene {marca}")
                    elif m < n:
                        self.assertIn(marca, texto, f"{(L, n, etapa)} no ve su intento {m}")
                    else:
                        # De la generación actual, solo su propio paso previo llega a su parche.
                        self.assertNotIn(marca + "]", texto)
                        if etapa != "parche":
                            self.assertNotIn(marca + "-H]", texto)
            if L != "C":
                self.assertNotIn("PRES-C", texto)
            if L != "E":
                self.assertNotIn("programa_repetido", texto)
        self.assertTrue(inf["auditoria_entre_lineas"]["sin_fugas"], inf["auditoria_entre_lineas"])
        # La primera generación de cada línea es el mismo pedido de parche del 7.12 (sin historial).
        primero = json.dumps(fake.requests[0]["messages"], ensure_ascii=False)
        self.assertNotIn("HISTORIAL", primero)
        f = RC.preparar(self.origen)
        disc = RC.texto_discrepancia(F, RC.discrepancia(self.ex, f))
        self.assertEqual(fake.requests[0]["messages"], RE13.mensajes_primera_generacion(f, disc, RE13.ESTRATEGIAS[0]))
        self.assertIn("HISTORIAL DE TUS INTENTOS ANTERIORES", json.dumps(fake.requests[1]["messages"], ensure_ascii=False))

    def test_each_patch_starts_from_a_clean_copy(self):
        inf, _, d, _ = self.escenario()
        original = inf["huella_del_codigo_original"]
        a2 = (d / "candidato-A/generacion-2/solucion.py").read_text()
        self.assertIn("if True:", a2)
        codigo = RC.preparar(self.origen)["codigo"]
        self.assertEqual(a2.count('if tipo == "anulacion":'), codigo.count('if tipo == "anulacion":'),
                         "el parche de la generación 1 no se arrastra")
        d2 = (d / "candidato-D/generacion-2/solucion.py").read_text()
        self.assertIn("saldos = dict(saldos_iniciales or {})", d2)
        self.assertEqual(RE13._huella(codigo), original)

    def test_categories_best_solutions_and_winners(self):
        inf, _, d, _ = self.escenario()
        self.assertEqual(self.cats(inf, "A"), ["discrepancia_sintetica", "supera_desarrollo"])
        self.assertEqual(self.cats(inf, "B"), ["discrepancia_sintetica"] * 3)
        self.assertEqual(self.cats(inf, "C"), ["supera_desarrollo"])
        self.assertEqual(self.cats(inf, "D"), ["parche_invalido", "supera_desarrollo"])
        self.assertEqual(self.cats(inf, "E"), ["discrepancia_sintetica", "programa_repetido", "programa_repetido"])
        self.assertEqual(self.cats(inf, "F"), ["supera_desarrollo"])
        self.assertIn("se congela", self.linea(inf, "A")["detenida"])
        self.assertEqual(self.linea(inf, "B")["detenida"], "completó 3 generaciones")
        self.assertEqual(self.linea(inf, "A")["mejor"]["generacion"], 2)
        # C supera el desarrollo y falla la batería reservada: no gana.
        c = self.linea(inf, "C")
        self.assertTrue(c["mejor"]["desarrollo"]["caso_corregido"] and c["mejor"]["desarrollo"]["visibles_ok"])
        self.assertFalse(c["bateria_final"]["ok"])
        self.assertIsNone(self.linea(inf, "B")["bateria_final"])
        self.assertIn("no supera el desarrollo", self.linea(inf, "B")["motivo_final"])
        # El mismo programa en A, D y F: la batería corre una vez; desempate sin normalización primero (D).
        self.assertTrue(inf["exito"])
        self.assertEqual(inf["ganadores"], ["D", "A", "F"])
        self.assertEqual(inf["ganador_por_desempate"], "D")
        self.assertEqual({self.linea(inf, L).get("bateria_reutilizada_de") for L in "DF"}, {"A"})
        self.assertEqual((self.linea(inf, "D")["bateria_final"]["aprobados"],
                          self.linea(inf, "D")["bateria_final"]["total"]), (9, 9))
        tabla = {x["candidato"]: x for x in inf["tabla"]}
        self.assertEqual((tabla["E"]["generaciones"], tabla["E"]["programas_distintos"]), (3, 1))
        self.assertEqual(tabla["D"]["resultado"], "ganador")
        self.assertEqual(set(tabla["A"]), {"candidato", "estrategia", "generaciones", "programas_distintos",
                                           "categorias", "mejor_desarrollo", "bateria_final", "detenida", "resultado"})
        self.assertEqual(inf["clasificacion"], RE13.CLASIFICACION_13 + " (ganadores: D, A, F)")
        # Registro por línea y generación.
        for rel in ("presupuesto.json", "informe.json", "candidato-A/linea.json", "candidato-A/generacion-1/generacion.json",
                    "candidato-A/generacion-1/parche/pedido.json", "candidato-A/generacion-1/parche/respuesta.txt",
                    "candidato-A/generacion-2/solucion.py", "candidato-C/generacion-1/diagnostico/pedido.json",
                    "candidato-D/generacion-2/localizacion/respuesta.txt", "candidato-F/generacion-1/plan/pedido.json"):
            self.assertTrue((d / rel).exists(), rel)
        g = json.loads((d / "candidato-A/generacion-2/generacion.json").read_text())
        for k in ("hipotesis", "revision_de_la_hipotesis", "parche", "parche_normalizado", "huella_programa",
                  "huella_solucion", "desarrollo", "feedback", "categoria"):
            self.assertIn(k, g)
        self.assertFalse((d / "candidato-D/generacion-1/solucion.py").exists(), "fuera de zona: no se aplica")

    def test_best_program_is_kept_when_later_generations_are_worse(self):
        resp, _ = evolucion(A=[INVERTIR, FUERA_DE_ZONA, ROMPE_EJECUCION])
        inf, _, _ = self.correr(resp)
        self.assertEqual(self.cats(inf, "A"), ["discrepancia_sintetica", "parche_invalido", "error_de_ejecucion"])
        a = self.linea(inf, "A")
        self.assertEqual(a["mejor"]["generacion"], 1)
        self.assertTrue(a["mejor"]["desarrollo"]["ejecuta"])
        self.assertIn("error de sintaxis o ejecución", a["generaciones"][2]["feedback"])
        self.assertIn("fuera de las zonas autorizadas", a["generaciones"][1]["feedback"])

    def test_cycles_and_duplicates_are_not_reevaluated(self):
        inf, fake, d, etiquetas = self.escenario()
        e = self.linea(inf, "E")
        self.assertEqual([g.get("repite_la_generacion") for g in e["generaciones"]], [None, 1, 1])
        self.assertIn("sin progreso", e["detenida"])
        self.assertNotIn("desarrollo", e["generaciones"][1])
        self.assertEqual(e["mejor"]["generacion"], 1)
        i = etiquetas.index(("E", 3, "parche"))
        self.assertIn("mismo programa que tu intento 1", json.dumps(fake.requests[i]["messages"], ensure_ascii=False))

    def test_revision_of_the_hypothesis_is_required(self):
        sin_revision = "=" + pg(QWEN, "MARCA-A2")
        resp, _ = evolucion(A=[INVERTIR, sin_revision, CAIDA])
        inf, _, _ = self.correr(resp)
        a = self.linea(inf, "A")
        self.assertEqual(a["generaciones"][1]["categoria"], "parche_invalido")
        self.assertIn("revision_de_la_hipotesis", a["generaciones"][1]["feedback"])
        self.assertIn("servidor caído", a["detenida"])
        # El plan de F, desde la segunda generación, tiene que decir qué cambia respecto del anterior.
        resp, _ = evolucion(F=[INVERTIR, (previo("F", 2, cambio_de_plan=False), None), CAIDA])
        inf, fake, _ = self.correr(resp)
        f = self.linea(inf, "F")
        self.assertEqual([g["categoria"] for g in f["generaciones"]], ["discrepancia_sintetica", "hipotesis_invalida",
                                                                       "hipotesis_invalida"])
        self.assertEqual(len(fake.requests), 5 + 2 + 1 + 1, "la generación 2 no pide parche")

    def test_feedback_comes_only_from_development_tests(self):
        llamadas = []
        original = RE._bateria

        def bateria(ex, codigo, mision):
            llamadas.append((len(fake_ref[0].requests), len(mision["spec"]["cases"])))
            return original(ex, codigo, mision)
        fake_ref = [None]
        resp, etiquetas = evolucion(**ESCENARIO)
        fake = FakeOllama(resp, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        fake_ref[0] = fake
        f = RC.preparar(self.origen)
        destino = RE13.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp713-")), self.origen)
        with mock.patch.object(RE, "_bateria", bateria):
            inf = RE13.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                              PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                              PG.modelo_remoto("qwen2.5-coder:7b"))
        # La batería reservada corre recién después de la última inferencia, una vez por programa distinto.
        self.assertEqual(llamadas, [(len(etiquetas), 9), (len(etiquetas), 9)], "A (= D = F) y C")
        self.assertTrue(inf["bateria_despues_de_la_ultima_inferencia"])
        self.assertGreaterEqual(inf["primera_ejecucion_de_la_bateria"], inf["ultima_inferencia"])
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for prohibido in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR, "9/9", "batería",
                          "bateria", "ganador", RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"],
                          RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]):
            self.assertNotIn(prohibido, todo)
        for c in MISIONES[F][0]()["spec"]["cases"]:
            if not c.get("visible", True):
                entrada = json.dumps(c["args"], ensure_ascii=False)[1:-1]
                if len(entrada) > 20:
                    self.assertNotIn(entrada, todo)
                if c.get("categoria"):
                    self.assertNotIn(c["categoria"], todo)
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        h = inf["huellas_de_pruebas"]
        self.assertEqual((h["desarrollo"]["cantidad"], h["bateria_reservada"]["casos"]),
                         (1 + h["bateria_reservada"]["casos"] - h["bateria_reservada"]["ocultos"], 9))
        self.assertNotEqual(h["desarrollo"]["ejemplos_visibles"], h["bateria_reservada"]["huella"])

    def test_cross_line_audit_detects_a_leak(self):
        d = Path(tempfile.mkdtemp(prefix="cia-exp713-auditoria-"))
        texto = "hipótesis de la línea A que no puede aparecer en otra línea"
        (d / "candidato-B/generacion-1/parche").mkdir(parents=True)
        (d / "candidato-B/generacion-1/parche/pedido.json").write_text(json.dumps([{"content": "… " + texto}],
                                                                                 ensure_ascii=False))
        inf = {"lineas": [{"letra": "A", "generaciones": [{"hipotesis": texto}]}, {"letra": "B", "generaciones": []}]}
        r = RE13.auditoria_entre_lineas(inf, SimpleNamespace(destino=d))
        self.assertFalse(r["sin_fugas"])
        self.assertIn("contiene texto de la línea A", r["hallazgos"][0])

    def test_selection_is_deterministic(self):
        def g(n, caso, vis, ejecuta=True, valido=True, lineas=2):
            return {"generacion": n, "lineas_cambiadas": lineas, "desarrollo": {
                "caso_corregido": caso, "visibles_aprobados": vis, "ejecuta": ejecuta, "parche_valido": valido}}
        gens = [g(1, False, 3), g(2, True, 1), g(3, True, 1, lineas=1)]
        self.assertEqual(max(gens, key=RE13.puntaje)["generacion"], 3, "a igual resultado, menos líneas")
        self.assertEqual(max([g(1, True, 1), g(2, True, 1)], key=RE13.puntaje)["generacion"], 1)
        self.assertEqual(max([g(1, False, 2), g(2, False, 3, lineas=9)], key=RE13.puntaje)["generacion"], 2,
                         "las líneas cambiadas solo desempatan")
        self.assertEqual(max([g(1, False, 0, ejecuta=False), g(2, False, 0)], key=RE13.puntaje)["generacion"], 2)
        a, _, _, _ = self.escenario()
        b, _, _, _ = self.escenario()
        self.assertEqual(a["tabla"], b["tabla"])
        self.assertEqual((a["ganadores"], a["ganador_por_desempate"]), (b["ganadores"], b["ganador_por_desempate"]))

    def test_budget_and_exhausted_lines(self):
        p = RE13.presupuesto()
        self.assertEqual((p["candidatos"], p["generaciones_por_candidato"], p["llamadas"], p["parches"]), (6, 3, 27, 18))
        self.assertEqual((p["segundos_totales"], p["segundos_por_linea"], p["tokens_generados_por_linea"]),
                         (3600.0, 600.0, 12000))
        # A agota sus tokens en la primera generación; B sigue y supera.
        resp, _ = evolucion(A=[INVERTIR, QWEN], B=[QWEN])
        resp[0] = {"text": resp[0], "eval_count": RE13.MAX_TOKENS_GENERADOS_POR_LINEA}
        inf, fake, d = self.correr(resp[:1] + resp[2:])
        self.assertIn("agotó sus tokens", self.linea(inf, "A")["detenida"])
        self.assertEqual(len(self.linea(inf, "A")["generaciones"]), 1)
        self.assertEqual(self.cats(inf, "B"), ["supera_desarrollo"])
        self.assertEqual(inf["ganadores"], ["B"])
        self.assertEqual(json.loads((d / "presupuesto.json").read_text())["llamadas"], 27)
        # A agota su tiempo (1 s por línea con 6 s en total); B sigue.
        resp, _ = evolucion(A=[INVERTIR, QWEN], B=[QWEN])
        resp[0] = {"text": resp[0], "header_delay": 1.2}
        inf, _, _ = self.correr(resp[:1] + resp[2:], max_segundos=6)
        self.assertIn("agotó su tiempo", self.linea(inf, "A")["detenida"])
        self.assertEqual(self.cats(inf, "B"), ["supera_desarrollo"])
        # Sin tiempo: nada se infiere y la batería no corre.
        inf, fake, _ = self.correr([], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertTrue(all("límite de tiempo" in x["detenida"] for x in inf["lineas"]))
        self.assertEqual((inf["primera_ejecucion_de_la_bateria"], inf["exito"]), (None, False))
        # Límite global de llamadas.
        with mock.patch.object(RE13, "MAX_LLAMADAS", 2):
            resp, _ = evolucion(A=[INVERTIR, IRRELEVANTE, QWEN])
            inf, fake, _ = self.correr(resp)
        self.assertEqual(len(fake.requests), 2)
        self.assertIn("límite global", self.linea(inf, "B")["detenida"])

    def test_previous_experiments_still_work(self):
        inf, _, _ = self.correr(torneo(INVERTIR, NO_CAUSA, ROMPE_OTRA_REGLA, IRRELEVANTE, FUERA_DE_ZONA, QWEN), mod=RT12)
        self.assertEqual(inf["ganadores"], ["F"])
        inf, _, _ = self.correr([diag11(), PLAN_11, APRUEBA, parche(QWEN)], mod=RC11)
        self.assertTrue(inf["exito"])

    def test_cli(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp713-cli-")) / "oficina-evolutivo-7-13"
        resp, _ = evolucion(**ESCENARIO)
        fake = FakeOllama(resp, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b", "qwen2.5-coder:7b"], permitir_local=True)
        reg.cerrar()
        from constructor_ia.sandbox.executor import Executor
        from constructor_ia.sandbox.limiter import NullLimiter

        def cli_(*extra):
            out, err = StringIO(), StringIO()
            ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
            with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                    mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-evolutivo-13",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        for t in ("--- presupuesto ---", '"segundos_totales": 3600.0', "--- huellas de las pruebas",
                  '"bateria_reservada"', "--- estrategias ---", "ESTRATEGIA:", "27 llamadas y 18 parches"):
            self.assertIn(t, out)
        self.assertNotIn("HISTORIAL", out)
        self.assertEqual(fake.requests, [])
        self.assertFalse((lab / "experimento-7-13").exists())
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("  D ", out)
        self.assertIn("batería final 9/9 · ganador", out)
        self.assertIn("ganadores: D, A, F · por desempate: D", out)
        self.assertIn("Experimento 7.13: ÉXITO", out)


if __name__ == "__main__":
    unittest.main()
