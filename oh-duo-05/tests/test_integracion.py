"""Integración con el sandbox REAL (bwrap) y el modelo simulado: control vs intervenida, cadena completa.

  ejecutar.py (OH-DUO-01 duo.py + ejecutor de OH-DUO-05 en bwrap, puente HTTP real, SDK real)
  cadena.py   (OH-DUO-03 observador → OH-DUO-04 coordinador → despachador → buzón de solo lectura → puente)

El modelo simulado repite el bucle de Qwen en P1 y solo cambia de conducta si el mensaje del coordinador llega a
la conversación. Sin inferencia. Necesita bwrap y el venv de oh-smoke ($OH_SMOKE o ~/oh-smoke) y oh-supervisor
($OH_SUPERVISOR o ~/oh-supervisor); si faltan, se saltea. Donde no hay sesión systemd de usuario: SIN_SCOPE=1.
   <venv>/bin/python -I -m unittest discover -s tests -p test_integracion.py -v        (~2 min)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent))
import cadena  # noqa: E402
import ejecutar as X  # noqa: E402
import intervenciones as I  # noqa: E402
import simulado as S  # noqa: E402


def ruta(var: str, nombre: str, archivo: str) -> Path | None:
    for p in (Path(os.environ.get(var, "/nonexistent")), Path.home() / nombre):
        if (p / archivo).exists():
            return p
    return None


OH_SMOKE, OH_SUP = ruta("OH_SMOKE", "oh-smoke", "venv"), ruta("OH_SUPERVISOR", "oh-supervisor", "supervisor.py")
BWRAP = shutil.which("bwrap")
LISTO = bool(OH_SMOKE and OH_SUP and BWRAP)


@unittest.skipUnless(LISTO, "faltan bwrap, el venv de oh-smoke u oh-supervisor")
class Integracion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="oh5-int-"))
        cls.corridas, cls.informes = cls.tmp / "corridas", cls.tmp / "informes"
        cls.oh = {n: X.hermano(n, a) for n, a in (("oh-duo-01", "duo.py"), ("oh-duo-02", "estancamiento.py"),
                                                   ("oh-duo-03", "observador.py"), ("oh-duo-04", "coordinador.py"))}
        cls.E = I.cargar_detector(cls.oh["oh-duo-02"])
        cls.u = cls.E.cargar_umbrales(None)
        cls.res = {n: cls.correr(n, enviar=(n == "intervenida")) for n in ("control", "intervenida")}
        for n in cls.res:
            X.verificar(cls.corridas, n, cls.oh["oh-duo-01"], bwrap=BWRAP)
        cls.an = {n: I.analizar(cls.corridas / n, cls.E, cls.u, cls.oh["oh-duo-01"],
                                cls.informes / n / "despachos.jsonl") for n in cls.res}
        cls.cmp = I.comparar(cls.an["control"], cls.an["intervenida"])
        if destino := os.environ.get("OH5_INFORME"):        # validar.sh conserva la comparación
            d = Path(destino)
            d.mkdir(parents=True, exist_ok=True)
            (d / "comparacion.md").write_text(I.markdown(cls.cmp))
            (d / "comparacion.json").write_text(json.dumps({"comparacion": cls.cmp, "analisis": cls.an,
                                                            "carpeta": str(cls.tmp)}, ensure_ascii=False, indent=1))

    @classmethod
    def correr(cls, nombre: str, enviar: bool) -> dict:
        srv = S.Servidor(S.bucle_p1(), demora=0.2)
        out = {}
        hilo = threading.Thread(target=lambda: out.update(cadena.correr(
            cls.corridas / nombre, cls.informes / nombre, cls.oh["oh-duo-02"], cls.oh["oh-duo-03"],
            cls.oh["oh-duo-04"], enviar, maximo=1, intervalo=0.2)), daemon=True)
        hilo.start()
        env = {"OLLAMA_URL": f"http://127.0.0.1:{srv.puerto}", "MAX_SEG": "300", "GRACIA": "20",
               "SIN_SCOPE": os.environ.get("SIN_SCOPE", "0")}
        try:
            with mock.patch.dict(os.environ, env):
                filas = X.ejecutar(cls.corridas, nombre, ["P1"], ["qwen"], 11, "alterno", OH_SMOKE, OH_SUP,
                                   cls.oh["oh-duo-01"])
            hilo.join(90)
        finally:
            srv.cerrar()
        return {"filas": filas, "cadena": out, "cadena_viva": hilo.is_alive()}

    def ejecucion(self, n):
        (f,) = self.an[n]["ejecuciones"]
        return f

    def resultados(self, n):
        (r,) = sorted((self.corridas / n / "ejecuciones" / "P1__qwen" / "instancia" / "resultados").iterdir())
        return r

    # ---- comunes
    def test_sandbox_y_buzon_de_solo_lectura(self):
        for n in self.res:
            r = self.resultados(n)
            self.assertIn("SANDBOX OK", (r / "control" / "sandbox.txt").read_text(), n)   # incluye /buzon y puente.py
            self.assertEqual(self.ejecucion(n)["originales_intactos"], "ok", n)
            self.assertFalse(self.res[n]["cadena_viva"], f"{n}: la cadena no terminó")
            self.assertTrue((self.corridas / n / X.MARCA).is_file())

    def test_presupuesto_original_de_30(self):
        for n in self.res:
            f = self.ejecucion(n)
            self.assertLessEqual(f["iteraciones"], 30, n)
            self.assertTrue(f["dentro_del_limite"], n)
            res = json.loads((self.resultados(n) / "resultado_final.json").read_text())
            self.assertEqual(res["max_iter"], 30)
        self.assertEqual(self.ejecucion("control")["iteraciones"], 30)        # el bucle agota el límite

    # ---- control
    def test_control_sin_intervencion(self):
        f = self.ejecucion("control")
        self.assertEqual(f["mensajes_del_coordinador"], [])
        self.assertEqual((self.resultados("control") / "buzon" / "mensajes.jsonl").read_text(), "")
        self.assertTrue(f["despachos"], "el coordinador debía decidir (en simulación)")
        self.assertEqual({d["resultado"] for d in f["despachos"]}, {"simulado"})
        self.assertFalse(f["visibles_pasan"])
        self.assertTrue(f["estancamiento"])

    # ---- intervenida
    def test_intervenida_entrega_y_confirma(self):
        f = self.ejecucion("intervenida")
        self.assertEqual([d["resultado"] for d in f["despachos"]][:1], ["escrito_en_buzon"])
        cuenta = f["puente"]["resumen"]["cuenta"]
        self.assertEqual(cuenta.get("entregado"), 1, f["puente"])
        self.assertEqual(cuenta.get("accion_posterior"), 1, f["puente"])
        self.assertEqual(f["puente"]["resumen"]["sin_accion_posterior"], [])
        (m,) = f["mensajes_del_coordinador"]
        self.assertTrue(m["1_en_la_conversacion"])
        self.assertIsNotNone(m["2_accion_posterior"])
        self.assertTrue(m["3_cambio_de_conducta"]["edito_el_codigo_despues"])
        self.assertTrue(m["3_cambio_de_conducta"]["pruebas_aprueban_despues"])
        self.assertLess(m["iteracion_al_recibir"], 30 - 5)

    def test_intervenida_mensaje_solo_de_plantillas(self):
        import despachador as D
        frases, patrones = D.plantillas(self.oh["oh-duo-04"])
        (m,) = self.ejecucion("intervenida")["mensajes_del_coordinador"]
        self.assertEqual(D.validar_texto(m["texto"], frases, patrones), (True, "ok"))
        self.assertNotIn("max(", m["texto"])

    def test_comparacion(self):
        (fila,) = self.cmp["filas"]
        self.assertEqual(fila["intervenciones"], 1)
        self.assertTrue(fila["cambio_de_conducta"])
        self.assertEqual(fila["terminacion"]["intervenida"], "finalizacion_con_finish")
        self.assertNotEqual(fila["terminacion"]["control"], "finalizacion_con_finish")
        self.assertEqual(fila["visibles_pasan"], {"control": False, "intervenida": True})
        self.assertEqual(fila["categoria_con_ocultas"]["intervenida"], "correcta")
        self.assertNotEqual(fila["categoria_con_ocultas"]["control"], "correcta")
        self.assertLess(fila["acciones"]["intervenida"], fila["acciones"]["control"])
        md = I.markdown(self.cmp)
        self.assertIn("P1__qwen", md)
        self.assertEqual(fila["identicas_antes_del_mensaje"]["identicas"], True, fila)
        self.assertEqual(fila["validez"], {"valida": True, "motivos": [], "avisos": [], "informativa": True,
                                           "nota": None})

    def test_sin_fugas_de_ocultas(self):
        for n in self.res:
            v = json.loads((self.corridas / n / "verificacion" / "verificacion.json").read_text())
            self.assertEqual(v["auditoria_fugas"], [], n)
            self.assertTrue(v["sandbox"])


if __name__ == "__main__":
    unittest.main()
