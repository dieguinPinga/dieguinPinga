"""Piloto de microtareas (PILOTO.md): banco, ensamblado, localizador, suite, umbral, auditoría y circuito completo.

Los guiones de modelo validan el CIRCUITO (aislamiento del oráculo, autoría, presupuestos, reanudación, lectura
preregistrada), NO la capacidad de un modelo: eso se mide con laboratorio-piloto y Qwen3 real.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from constructor_ia.instrumentos import estatico
from constructor_ia.laboratorio.piloto import banco as B
from constructor_ia.laboratorio.piloto import brazos as BR
from constructor_ia.laboratorio.piloto import informe as PI
from constructor_ia.laboratorio.piloto import localizador as L
from constructor_ia.laboratorio.piloto import parches as P
from constructor_ia.laboratorio.piloto import piloto as PL
from constructor_ia.laboratorio.piloto import verificaciones as V
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg

VERIFICADOR = '''
import datetime

def medir(db, p):
    s = p["salida"]
    ancho = int(p["parametros"].get("ancho_min", 15))
    malos, ejemplos = set(), []
    total = int(db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0])
    if s["cobertura"].get("leidas") != total:
        malos.update(["leidas", "filas_analizadas"])
        ejemplos.append({"salida": s["cobertura"].get("leidas"), "medido": total})
    for w in s["resultado"]["ventanas"]:
        ini = datetime.datetime.strptime(w["inicio"], "%Y-%m-%d %H:%M")
        fin = ini + datetime.timedelta(minutes=ancho)
        r = db.consultar("SELECT COALESCE(SUM(valor), 0) FROM lecturas WHERE valor > 0.5 AND valor < 900 AND "
                         "momento >= '" + ini.strftime("%Y-%m-%d %H:%M:%S") + "' AND momento < '"
                         + fin.strftime("%Y-%m-%d %H:%M:%S") + "'")
        medido = float(r.filas[0][0])
        if abs(medido - w["suma"]) > 0.01:
            malos.update(["inicio", "suma"])
            ejemplos.append({"inicio": w["inicio"], "suma": w["suma"], "medido": medido})
    return {"resultado": {"ok": not malos, "campos": sorted(malos), "contraejemplos": ejemplos[:3]},
            "cobertura": {"filas_analizadas": total}, "limitaciones": []}
'''

CORRECTA_D1 = "inicio = m - datetime.timedelta(minutes=m.minute % ancho)"


def arreglar(codigo: str) -> str:
    """Lo que haría un modelo competente con el programa completo (guion de prueba)."""
    codigo = codigo.replace("% ancho + ancho)", "% ancho)")
    return re.sub(r'    if "cobertura" not in cobertura:\n        cobertura = \{[^\n]*\}\n', "", codigo)


class Guion:
    """Modelo simulado sin HTTP: responde según el tipo de pedido."""

    def __init__(self, responder, nombre="guion", num_ctx=4096):
        import types
        self.responder, self.nombre, self.system, self.pedidos = responder, nombre, "", []
        self.base = types.SimpleNamespace(num_ctx=num_ctx)

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False):
        self.pedidos.append((tipo, prompt))
        text = self.responder(tipo, prompt)
        return text, {"tipo": tipo, "resultado": "completa", "tokens_prompt": len(prompt) // 4,
                      "tokens_generados": len(text or "") // 4, "reloj_ms": 1000}


def competente(tipo, prompt):
    if tipo == "verificador":
        return f"```python\n{VERIFICADOR.strip()}\n```"
    if tipo == "micro":
        bloques = re.split(r"\n(?=\d+\) línea )", prompt.split("Instrucciones candidatas")[1])
        for i, b in enumerate(bloques):
            marcadas = [ln for ln in b.splitlines() if ln.startswith(">>")]
            if any("% ancho + ancho" in ln for ln in marcadas):
                return f"CANDIDATO: {i}\nACCION: reemplazar\n```python\n{CORRECTA_D1}\n```"
            if any('cobertura = {"leidas": 0' in ln for ln in marcadas) and 'if "cobertura" not in' in b or \
                    any('if "cobertura" not in' in ln for ln in marcadas):
                return f"CANDIDATO: {i}\nACCION: eliminar"
        return "CANDIDATO: 1\nACCION: reemplazar\n```python\npass\n```"
    if tipo == "completo":
        code = prompt.split("Instrumento actual:\n```python\n")[1].split("```")[0]
        return f"```python\n{arreglar(code)}```"
    if tipo.startswith(("reparacion", "revision")):    # brazo A (0.6.1)
        code = prompt.split("```python\n")[1].split("```")[0]
        return f'```json\n{{"nombre": "ventanas", "parametros": {json.dumps(B.PARAMS_VISIBLES)}, "causa": "x"}}\n```' \
               f"\n```python\n{arreglar(code)}```"
    if tipo.startswith("evaluacion"):                 # brazo A: confía en lo que ve (no puede saber más)
        return json.dumps({"responde": "si", "motivo": "parece correcto", "respuesta": {}, "mejora": ""})
    return ""


def incompetente(tipo, prompt):
    if tipo == "verificador":
        return "```python\ndef medir(db, p):\n    return 1 / 0\n```"
    if tipo == "micro":
        return "no sé"
    if tipo == "completo":
        return "```python\ndef medir(db, p):\n    return {}\n```"
    if tipo.startswith("evaluacion"):
        return json.dumps({"responde": "si", "motivo": "ok", "respuesta": {}, "mejora": ""})
    return "```python\nimport os\ndef medir(db, p):\n    return {}\n```"


class UnitTests(unittest.TestCase):
    def test_bank_is_valid_and_defects_are_single_statements(self):
        self.assertEqual(estatico.check(B.REFERENCIA), [])
        for d in B.DEFECTOS.values():
            self.assertEqual(estatico.check(d.codigo), [])
            self.assertNotEqual(d.codigo, B.REFERENCIA)
        ms = B.mutantes()
        self.assertGreater(len(ms), 30)
        self.assertEqual(len({m.codigo for m in ms}), len(ms), "mutantes distintos")
        self.assertTrue(all(estatico.check(m.codigo) == [] for m in ms))

    def test_judge_compares_leaves_without_domain_knowledge(self):
        a = {"resultado": {"ventanas": [{"inicio": "x", "suma": 1.0}]}, "cobertura": {"leidas": 3}}
        self.assertEqual(B.comparar(a, a), {"igual": True, "campos": []})
        b = {"resultado": {"ventanas": [{"inicio": "y", "suma": 1.0004}]}, "cobertura": {"leidas": 4}}
        self.assertEqual(B.comparar(b, a)["campos"], ["inicio", "leidas"])

    def test_assembly_separates_model_and_coordinator_faults(self):
        code = B.DEFECTOS["D1"].codigo
        u = P.unidad_en(code, B.lineas_de(code, B.DEFECTOS["D1"].sembrada)[0])
        ok = P.aplicar(code, u, "reemplazar", f"```python\n        {CORRECTA_D1}\n```")
        self.assertIsNone(ok.atribucion, ok.detalle)
        self.assertIn(CORRECTA_D1, ok.codigo)
        self.assertEqual([ln for ln in ok.codigo.splitlines() if ln not in code.splitlines()],
                         ["        " + CORRECTA_D1], "solo cambió la región")
        self.assertEqual(P.aplicar(code, u, "reemplazar", "inicio = (").atribucion, "modelo")
        d2 = B.DEFECTOS["D2"].codigo
        u2 = P.unidad_en(d2, B.lineas_de(d2, B.DEFECTOS["D2"].sembrada)[1])
        borrado = P.aplicar(d2, u2, "eliminar")
        self.assertIsNone(borrado.atribucion)
        self.assertIn('if "cobertura" not in cobertura:\n        pass', borrado.codigo, "bloque vacío → pass")
        u_for = next(x for x in P.instrucciones(code) if x.compuesta and x.nodo.__class__.__name__ == "For")
        self.assertEqual(P.aplicar(code, u_for, "reemplazar", "x = 1").atribucion, "modelo",
                         "el encabezado de una compuesta debe terminar en «:»")

    def test_localizer_needs_field_evidence_for_silent_defects(self):
        d1 = B.DEFECTOS["D1"]
        lineas = B.lineas_de(d1.codigo, d1.sembrada)
        self.assertIsNotNone(L.acierta(L.localizar(d1.codigo, [{"campos": ["inicio"]}]), lineas))
        self.assertIsNone(L.acierta(L.localizar(d1.codigo, [{"campos": []}]), lineas),
                          "sin saber qué campo está mal, el defecto silencioso no aparece (desafío 1 → 2)")
        d2 = B.DEFECTOS["D2"]
        self.assertEqual(L.acierta(L.localizar(d2.codigo, [{"campos": ["filas_analizadas"]}]),
                                   B.lineas_de(d2.codigo, d2.sembrada)), 1)
        self.assertEqual(L.acierta(L.localizar(d2.codigo, [{"lugar": {"linea": 12}}]), (12, 12)), 1,
                         "una excepción va primero")

    def test_gate_accepts_only_improvement_without_regression(self):
        def ev(**c):
            return {"checks": {k: {"ok": v} for k, v in c.items()}}
        self.assertTrue(V.umbral(ev(a=False, b=True), ev(a=True, b=True))[0])
        self.assertFalse(V.umbral(ev(a=False, b=True), ev(a=True, b=False))[0], "regresión")
        self.assertFalse(V.umbral(ev(a=False, b=False), ev(a=False, b=False))[0], "sin mejora")

    def test_trial_and_preregistered_runs_never_mix(self):
        d = Path(tempfile.mkdtemp())
        PL.preparar_salida(d / "ensayo", True)
        (d / "ensayo" / "episodios.jsonl").write_text("{}\n")
        self.assertTrue((d / "ensayo" / PL.MARCA_ENSAYO).exists())
        with self.assertRaises(ValueError):
            PL.preparar_salida(d / "ensayo", False)
        PL.preparar_salida(d / "real", False)
        (d / "real" / "episodios.jsonl").write_text("{}\n")
        with self.assertRaises(ValueError):
            PL.preparar_salida(d / "real", True)
        self.assertEqual(PL.preparar_salida(d / "real", False), d / "real", "reanudar la misma clase sí")
        self.assertGreaterEqual(PL.TIMEOUT_S, 900, "Enmienda 2")

    def test_leak_audit(self):
        d1 = B.DEFECTOS["D1"]
        limpio = [{"partes": [("coordinador", "texto"), ("codigo", CORRECTA_D1)]}]
        self.assertEqual(PL.auditar(limpio, "B", d1), [], "el código del modelo puede contener la corrección")
        fuga = [{"partes": [("coordinador", "pista: " + CORRECTA_D1)]}]
        self.assertTrue(PL.auditar(fuga, "B", d1))
        marca = [{"partes": [("coordinador", BR.MARCA_ORACULO + " línea 19")]}]
        self.assertTrue(PL.auditar(marca, "B", d1))
        self.assertEqual(PL.auditar(marca, "B*", d1), [], "en B* el oráculo es legítimo")


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class PilotCircuitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=30)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-piloto-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="piloto_test",
                          escala={"dias": 3, "por_dia": 300})

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def correr(self, responder, salida, **kw):
        guiones = []

        def crear():
            g = Guion(responder)
            guiones.append(g)
            return g
        res = PL.correr(self.cfg, ent=self.ent, crear_modelo=crear, salida=salida, log=lambda *_: None, **kw)
        return res, guiones

    def test_competent_script_full_pilot(self):
        res, guiones = self.correr(competente, self.dir / "bueno", repeticiones=1)
        ep = {(e["defecto"], e["brazo"]): e for e in res["episodios"]}
        self.assertEqual(len(ep), 10)
        det = res["detecciones"]
        self.assertTrue(det["D1_r1"]["verificadores"][0]["detecta"], "el verificador del modelo ve D1")
        self.assertEqual(sorted(det["D1_r1"]["verificadores"][0]["campos"]), ["inicio", "suma"])
        self.assertFalse(any(not c["ok"] for c in det["D1_r1"]["sistema"].values()),
                         "las invariantes del sistema NO ven D1 (silencioso)")
        self.assertTrue(any(not c["ok"] for c in det["D2_r1"]["sistema"].values()), "el sistema ve D2")
        for b in ("B", "B*", "C", "D"):
            for d in ("D1", "D2"):
                self.assertTrue(ep[(d, b)]["juez"]["corregido"], (d, b, ep[(d, b)]["pasos"]))
        self.assertFalse(ep[("D1", "A")]["juez"]["corregido"], "A (0.6.1) se conforma: su evaluación dice «sí»")
        self.assertTrue(ep[("D2", "A")]["juez"]["corregido"], "A repara D2: 0.6.1 detecta la medición sin evidencia")
        self.assertEqual(ep[("D1", "B")]["pasos"][0]["resultado"], "aceptado")
        self.assertEqual(len(ep[("D1", "B")]["llamadas"]), 1, "una sola microtarea")
        integ = res["lectura"]["integridad"]
        self.assertEqual((integ["fugas"], integ["presupuesto_excedido"]), (0, 0))
        self.assertTrue(integ["apto_para_experimento_completo"])
        cats = {(c["comparacion"], c["defecto"]): c["categoria"] for c in res["lectura"]["comparaciones"]}
        self.assertEqual(cats[("B vs A", "D1")], "a favor")
        self.assertEqual(cats[("B* vs C", "D1")], "empate")
        md = PI.markdown(res, None)
        self.assertLess(md.index("## Contra la hipótesis"), md.index("## Integridad"))
        prompts = [json.loads(x) for x in (self.dir / "bueno" / "prompts.jsonl").read_text().splitlines()]
        self.assertTrue(all(BR.MARCA_ORACULO not in json.dumps(p["partes"], ensure_ascii=False)
                            for p in prompts if p["brazo"] in ("B", "D")))
        self.assertTrue(any(BR.MARCA_ORACULO in json.dumps(p["partes"], ensure_ascii=False)
                            for p in prompts if p["brazo"] == "B*"))
        rev = PI.revisar(self.dir / "bueno")
        self.assertIn("Tipo: corrida preregistrada", rev)
        self.assertIn("Fugas: 0; presupuesto excedido: 0", rev)
        self.assertIn("MariaDB privado borrado: sí", rev)
        self.assertIn("partes: coordinador, evidencia, oraculo", rev, "el oráculo se ve, etiquetado, en B*")
        self.assertIn("autoría de la falla", rev)
        micro = next(p for p in prompts if p["brazo"] == "B" and p["defecto"] == "D1")
        texto = "".join(t for _, t in micro["partes"])
        self.assertNotIn("def medir", texto, "la microtarea no muestra el programa completo")

    def test_incompetent_script_is_reported_against_the_hypothesis_and_resumes(self):
        salida = self.dir / "malo"
        res, _ = self.correr(incompetente, salida, repeticiones=1, brazos=("B", "B*"), defectos=("D1",))
        self.assertFalse(res["detecciones"]["D1_r1"]["verificadores"][0]["valido"], "verificador inválido excluido")
        self.assertTrue(res["lectura"]["senales"]["R3"], "D1 sin detectar")
        self.assertTrue(any("ninguna verificación" in c for c in res["lectura"]["contra_la_hipotesis"]))
        cat = {c["comparacion"]: c["categoria"] for c in res["lectura"]["comparaciones"]}
        self.assertEqual(cat["B vs B*"], "no informativo")
        self.assertTrue(all(not e["juez"]["corregido"] for e in res["episodios"]))
        antes = (salida / "episodios.jsonl").read_text()
        res2, guiones = self.correr(incompetente, salida, repeticiones=1, brazos=("B", "B*"), defectos=("D1",))
        self.assertEqual(guiones, [], "reanudar no repite episodios ni la detección")
        self.assertEqual((salida / "episodios.jsonl").read_text(), antes)
        self.assertEqual(len(res2["episodios"]), 2)

    def test_technical_trial_is_marked_and_excluded(self):
        salida = self.dir / "ensayo"
        res, _ = self.correr(competente, salida, repeticiones=1, brazos=("B", "A"), defectos=("D2",), ensayo=True,
                             presupuesto=PL.Presupuesto(**PL.PRESUPUESTO_ENSAYO))
        self.assertTrue(res["ensayo"])
        self.assertTrue((salida / PL.MARCA_ENSAYO).exists())
        self.assertTrue(PI.markdown(res, None).startswith("# ENSAYO TÉCNICO — EXCLUIDO DEL ANÁLISIS PREREGISTRADO"))
        self.assertTrue(all(len(e["llamadas"]) <= 2 for e in res["episodios"]))

    def test_budget_is_enforced(self):
        def terco(tipo, prompt):
            if tipo == "verificador":
                return f"```python\n{VERIFICADOR.strip()}\n```"
            return "CANDIDATO: 1\nACCION: reemplazar\n```python\npass\n```" if tipo == "micro" else \
                "```python\ndef medir(db, p):\n    return {}\n```"
        res, _ = self.correr(terco, self.dir / "terco", repeticiones=1, brazos=("B",), defectos=("D1",),
                             presupuesto=PL.Presupuesto(llamadas=4, segundos=999, deteccion=1))
        e = res["episodios"][0]
        self.assertLessEqual(len(e["llamadas"]), 4)
        self.assertIn(e["motivo_fin"], ("presupuesto", "sin_progreso", "sin_candidatas"))
        self.assertTrue(all(not p.get("aceptado") for p in e["pasos"]), "nada empeora la versión vigente")
        self.assertFalse(e["juez"]["corregido"])


if __name__ == "__main__":
    unittest.main()


FAKE_SYSTEMCTL = r'''#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
args=" $* "
case "$args" in
  *" is-active "*constructor-ia*)
    if [ "$(cat "$FAKE_STATE")" = active ]; then [[ "$args" == *quiet* ]] || echo active; exit 0
    else [[ "$args" == *quiet* ]] || echo inactive; exit 3; fi ;;
  *" stop constructor-ia.service "*) echo inactive > "$FAKE_STATE" ;;
  *" start constructor-ia.service "*) echo active > "$FAKE_STATE" ;;
esac
exit 0
'''

FAKE_PILOTO = r'''import os, sys, time
log = lambda m: open(os.environ["FAKE_LOG"], "a").write(m + "\n")
estado = open(os.environ["FAKE_STATE"]).read().strip()
log("piloto " + " ".join(sys.argv[1:]) + " servicio=" + estado)
modo = os.environ["FAKE_MODO"]
try:
    if modo == "ok":
        sys.exit(0)
    if modo == "falla":
        sys.exit(3)
    time.sleep(600)
except KeyboardInterrupt:
    log("piloto: limpieza")
    sys.exit(1)
'''


class StagesScriptTests(unittest.TestCase):
    """scripts/piloto_etapas.sh: el servicio constructor-ia SIEMPRE vuelve a quedar como estaba."""

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.ok = shutil.which("timeout") and shutil.which("flock")

    def setUp(self):
        if not self.ok:
            self.skipTest("faltan timeout o flock")
        self.w = Path(tempfile.mkdtemp(prefix="cia-etapas-"))
        (self.w / "bin").mkdir()
        (self.w / "bin" / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.w / "bin" / "systemd-run").write_text('#!/usr/bin/env bash\necho "systemd-run $*" >> "$FAKE_LOG"\n'
                                                    'exit "${FAKE_RUN_RC:-0}"\n')
        (self.w / "piloto.py").write_text(FAKE_PILOTO)
        (self.w / "piloto.sh").write_text(f'#!/usr/bin/env bash\nexec python3 {self.w / "piloto.py"} "$@"\n')
        for f in ("bin/systemctl", "bin/systemd-run", "piloto.sh"):
            (self.w / f).chmod(0o755)

    def correr(self, modo, *, senal=None, inicial="active", limite="20", run_rc="0"):
        import os
        import signal
        import subprocess
        import time
        log, state = self.w / f"log.{modo}", self.w / f"state.{modo}"
        state.write_text(inicial + "\n")
        log.write_text("")
        env = dict(os.environ, PATH=f"{self.w / 'bin'}:{os.environ['PATH']}", PILOTO_SH=str(self.w / "piloto.sh"),
                   CIA_PILOTO_DIR=str(self.w / "base"), PILOTO_UNIDAD="s", FAKE_LOG=str(log), FAKE_STATE=str(state),
                   FAKE_MODO=modo, FAKE_RUN_RC=run_rc)
        script = Path(__file__).resolve().parent.parent / "scripts" / "piloto_etapas.sh"
        p = subprocess.Popen(["bash", str(script), "ensayo", limite], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        if senal:
            for _ in range(100):
                if "piloto ensayo" in log.read_text() or "piloto laboratorio" in log.read_text():
                    break
                time.sleep(0.1)
            p.send_signal(getattr(signal, senal))
        out, _ = p.communicate(timeout=120)
        return p.returncode, state.read_text().strip(), log.read_text(), out

    def test_service_is_restored_on_success_failure_interrupt_hangup_and_time_limit(self):
        for modo, senal, limite in (("ok", None, "20"), ("falla", None, "20"), ("cuelga", "SIGINT", "20"),
                                    ("cuelga", "SIGHUP", "20"), ("cuelga", None, "10")):
            with self.subTest(modo=modo, senal=senal, limite=limite):
                rc, estado, log, out = self.correr(modo, senal=senal, limite=limite)
                self.assertEqual(estado, "active", (out, log))
                self.assertIn("servicio=inactive", log, "el ensayo corrió con el servicio detenido")
                self.assertIn("--on-active=25s" if limite == "20" else "--on-active=15s", log, "respaldo armado")
                self.assertIn("start constructor-ia.service", log)
                self.assertIn("stop cia-piloto-restaurar.timer", log.split("start constructor-ia.service")[-1],
                              "con el servicio de vuelta, el respaldo se desarma")
                if modo == "cuelga":
                    self.assertIn("piloto: limpieza", log, "el ensayo recibió SIGINT y limpió (MariaDB privado)")
                if modo == "ok":
                    self.assertEqual(rc, 0)
                else:
                    self.assertNotEqual(rc, 0)

    def test_service_that_was_stopped_stays_stopped(self):
        rc, estado, log, out = self.correr("ok", inicial="inactive")
        self.assertEqual((rc, estado), (0, "inactive"))
        self.assertNotIn("start constructor-ia.service", log)
        self.assertNotIn("systemd-run", log)

    def test_without_a_backup_timer_nothing_is_stopped(self):
        rc, estado, log, out = self.correr("ok", run_rc="1")
        self.assertNotEqual(rc, 0)
        self.assertEqual(estado, "active")
        self.assertNotIn("stop constructor-ia.service", log)
        self.assertNotIn("piloto laboratorio", log, "el ensayo no corre si no hay respaldo")
