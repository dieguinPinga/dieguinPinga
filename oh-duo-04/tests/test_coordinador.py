"""Pruebas del coordinador (simulación). Sin Ollama ni SDK.   python3 -m unittest discover -s tests -v

Registros: los informes de OH-DUO-03 que están en el repositorio (reproducción de ensayo-p1 y su simulación en
vivo) y alertas sintéticas. Rutas: $OH_DUO_03, ../oh-duo-03 o ~/oh-duo-03 (y lo mismo para 02 y 01).
"""
import builtins
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import coordinador as C  # noqa: E402


def hermano(nombre: str, archivo: str) -> Path | None:
    for p in (Path(os.environ.get(nombre.upper().replace("-", "_"), "/nonexistent")), AQUI.parent.parent / nombre,
              Path.home() / nombre):
        if (p / archivo).exists():
            return p
    return None


OH3, OH2, OH1 = hermano("oh-duo-03", "observador.py"), hermano("oh-duo-02", "estancamiento.py"), hermano("oh-duo-01", "duo.py")
CFG = C.cargar_config(None)
A1, A2, A3, A4, A5, A6 = ("A1_repeticion_equivalente_sin_cambio", "A2_fallos_iguales_sin_cambio_del_codigo",
                          "A3_edicion_de_archivo_protegido", "A4_ediciones_rechazadas_o_sin_efecto",
                          "A5_pruebas_aprueban_solo_tras_editar_lo_protegido", "A6_iteraciones_finales_sin_progreso")


def nueva(regla, it, clave=None, ev=None):
    return {"tipo": "alerta_nueva", "clave": clave or regla, "regla": regla, "iteracion_de_deteccion": it,
            "evento_de_deteccion": ev if ev is not None else 2 * it, "momento": f"t{it}", "veces": 1}


def retirada(clave, it):
    return {"tipo": "alerta_retirada", "clave": clave, "iteracion": it, "evento": 2 * it}


def correr(lineas, cfg=CFG):
    c = C.Coordinador(cfg)
    for l in lineas:
        c.procesar(l)
    return c


def tipos(c):
    return [(d["tipo"], d.get("iteracion")) for d in c.decisiones if d["tipo"] != "sin_intervencion"]


def arbol(raiz: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(raiz.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(raiz)).encode() + p.read_bytes())
    return h.hexdigest()


@unittest.skipIf(OH3 is None, "oh-duo-03 no está al lado")
class Registros(unittest.TestCase):
    """Los registros existentes de OH-DUO-03: reproducción y simulación en vivo de ensayo-p1."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def comprobar_qwen(self, r):
        self.assertEqual(r["intervenciones_enviadas"], 0)
        self.assertEqual((r["advertencias"], r["primera_advertencia_iteracion"]), (1, 3))
        self.assertEqual((r["nuevas_estrategias"], r["primera_estrategia_iteracion"]), (1, 8))
        self.assertLess(r["primera_advertencia_iteracion"], r["primera_estrategia_iteracion"])   # advertir ≠ confirmar
        self.assertEqual(r["anticipacion_iteraciones"], 22)
        estr = next(d for d in r["decisiones"] if d["tipo"] == "nueva_estrategia")
        self.assertEqual(estr["disparada_por"], A4)
        self.assertIn("2 reglas de bucle", estr["motivo"])
        self.assertTrue(all(d["enviado"] is False for d in r["decisiones"] if "enviado" in d))

    def test_reproduccion_qwen_y_gemma(self):
        base = OH3 / "informes" / "repro-p1"
        antes = arbol(base)
        self.comprobar_qwen(C.simular(base / "P1__qwen", self.tmp / "q", CFG))
        g = C.simular(base / "P1__gemma", self.tmp / "g", CFG)
        self.assertEqual((g["advertencias"], g["nuevas_estrategias"], g["decisiones"]), (0, 0, []))
        self.assertEqual(arbol(base), antes)

    def test_simulacion_en_vivo(self):
        base = OH3 / "informes" / "vivo-p1-simulado"
        self.comprobar_qwen(C.simular(base / "P1__qwen", self.tmp / "q", CFG))
        self.assertEqual(C.simular(base / "P1__gemma", self.tmp / "g", CFG)["decisiones"], [])

    def test_salidas(self):
        r = C.simular(OH3 / "informes" / "repro-p1" / "P1__qwen", self.tmp / "q", CFG)
        filas = [json.loads(l) for l in (self.tmp / "q" / "decisiones.jsonl").read_text().splitlines()]
        self.assertEqual(filas, r["decisiones"])
        md = (self.tmp / "q" / "coordinacion.md").read_text()
        self.assertIn("**nueva_estrategia**, iteración 8", md)
        self.assertIn("Intervenciones enviadas: **0**", md)

    @unittest.skipIf(OH2 is None, "oh-duo-02 no está al lado")
    def test_cadena_completa_desde_los_eventos(self):
        """Eventos del SDK → OH-DUO-03 (alertas) → OH-DUO-04: da lo mismo que con los informes guardados."""
        spec = importlib.util.spec_from_file_location("obs", OH3 / "observador.py")
        O = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(O)
        E = O.cargar_detector(OH2)
        O.reproducir(OH2 / "tests" / "fixtures" / "repro-p1" / "ejecuciones" / "P1__qwen", self.tmp / "a", E,
                     E.cargar_umbrales(None))
        r = C.simular(self.tmp / "a", self.tmp / "c", CFG)
        guardado = C.simular(OH3 / "informes" / "repro-p1" / "P1__qwen", self.tmp / "c2", CFG)
        sin_tiempos = lambda ds: [{k: v for k, v in d.items() if not k.startswith("momento")} for d in ds]
        self.assertEqual(sin_tiempos(r["decisiones"]), sin_tiempos(guardado["decisiones"]))

    def test_no_lee_nada_fuera_de_las_alertas(self):
        leidos, abrir = [], builtins.open

        def registrar(ruta, *a, **kw):
            leidos.append(str(ruta))
            return abrir(ruta, *a, **kw)
        with mock.patch("builtins.open", registrar), mock.patch("io.open", registrar):
            C.simular(OH3 / "informes" / "repro-p1" / "P1__qwen", self.tmp / "q", CFG)
        permitidos = (str(OH3 / "informes" / "repro-p1" / "P1__qwen"), str(self.tmp), str(AQUI.parent / "coordinador.json"))
        self.assertEqual([r for r in leidos if not r.startswith(permitidos)], [])

    @unittest.skipIf(OH1 is None, "oh-duo-01 no está al lado")
    def test_los_mensajes_no_contienen_la_solucion(self):
        r = C.simular(OH3 / "informes" / "repro-p1" / "P1__qwen", self.tmp / "q", CFG)
        referencia = (OH1 / "banco" / "P1" / "referencia" / "intervalos.py").read_text()   # solo para comprobar
        for d in r["decisiones"]:
            if "mensaje" in d:
                for linea in (l.strip() for l in referencia.splitlines()):
                    if len(linea) > 12:
                        self.assertNotIn(linea, d["mensaje"])
                self.assertNotIn("max(", d["mensaje"])


class Sinteticos(unittest.TestCase):
    def test_primera_advertencia_no_activa_la_estrategia(self):
        c = correr([nueva(A3, 2), nueva(A5, 3)])
        self.assertEqual(tipos(c), [("advertencia", 2)])

    def test_una_sola_senal_de_bucle_no_alcanza(self):
        c = correr([nueva(A1, 4, f"{A1} :: ejecutar_pruebas :: 3"), nueva(A6, 8)])
        self.assertEqual(tipos(c), [("nueva_estrategia", 8)])
        c = correr([nueva(A1, 4, f"{A1} :: file_editor view x.py :: 3")])
        self.assertEqual(tipos(c), [])
        self.assertEqual(c.decisiones[0]["motivo"], "señal de bucle todavía sin confirmar")

    def test_a2_sola_confirma_y_nombra_las_pruebas(self):
        c = correr([nueva(A2, 10, f"{A2} :: test_contenido|test_otro :: 4")])
        d = c.decisiones[0]
        self.assertEqual((d["tipo"], d["iteracion"]), ("nueva_estrategia", 10))
        self.assertIn("Failing tests: test_contenido, test_otro.", d["mensaje"])
        self.assertIn("You are repeating the same tests with the same failure.", d["mensaje"])
        self.assertEqual(d["datos_variables"], {"pruebas": ["test_contenido", "test_otro"]})

    def test_sin_intervenciones_repetidas_en_el_episodio(self):
        lineas = [nueva(A2, 10, f"{A2} :: t :: 4"), nueva(A6, 11),
                  {"tipo": "alerta_actualizada", "clave": f"{A2} :: t :: 4", "veces": 5, "iteracion": 12},
                  nueva(A1, 13, f"{A1} :: ejecutar_pruebas :: 4"), nueva(A4, 14)]
        self.assertEqual(tipos(correr(lineas)), [("nueva_estrategia", 10)])

    def test_nuevo_episodio_despues_del_progreso(self):
        lineas = [nueva(A2, 10, f"{A2} :: t :: 4"), nueva(A6, 11), retirada(A6, 13), retirada(f"{A2} :: t :: 4", 13),
                  nueva(A2, 14, f"{A2} :: t :: 26")]                     # enfriamiento: 14 - 10 < 5
        c = correr(lineas)
        self.assertEqual(tipos(c), [("nueva_estrategia", 10), ("episodio_cerrado", 13)])
        self.assertIn("enfriamiento", c.decisiones[-1]["motivo"])
        c = correr(lineas + [nueva(A6, 16)])                           # ya pasaron 5 iteraciones
        self.assertEqual(tipos(c)[-1], ("nueva_estrategia", 16))

    def test_limite_de_intervenciones(self):
        lineas = []
        for k in range(4):
            it = 10 + 10 * k
            lineas += [nueva(A2, it, f"{A2} :: t :: {it}"), nueva(A6, it), retirada(A6, it + 1), retirada(f"{A2} :: t :: {it}", it + 1)]
        c = correr(lineas)
        self.assertEqual(sum(t == "nueva_estrategia" for t, _ in tipos(c)), CFG["max_intervenciones"])

    def test_retirada_antes_de_confirmar(self):
        c = correr([nueva(A6, 8), retirada(A6, 9), nueva(A1, 10, f"{A1} :: ejecutar_pruebas :: 9")])
        self.assertEqual(tipos(c), [])

    def test_advertencias_desactivables_e_idioma(self):
        cfg = {**CFG, "advertir_acciones_prohibidas": False, "idioma": "es"}
        c = correr([nueva(A3, 2), nueva(A2, 9, f"{A2} :: test_x :: 2")], cfg)
        self.assertEqual(tipos(c), [("nueva_estrategia", 9)])
        m = c.decisiones[-1]["mensaje"]
        self.assertTrue(m.startswith("Estás repitiendo las mismas pruebas con el mismo fallo."))
        self.assertTrue(m.endswith("planteá una estrategia diferente. No modifiques las pruebas."))
        self.assertIsNone(c.decisiones[-1]["traduccion_es"])

    def test_mensaje_solo_de_plantillas(self):
        # A6 sola no confirma; A2 sí: el mensaje describe lo que está activo en ese momento.
        c = correr([nueva(A3, 2), nueva(A6, 9), nueva(A2, 9, f"{A2} :: test_x :: 2"), nueva(A4, 9)])
        d = next(x for x in c.decisiones if x["tipo"] == "nueva_estrategia")
        t = CFG["plantillas"]["en"]
        esperado = " ".join([t["repeticion"], t["sin_progreso"], t["pruebas_modificadas"],
                             t["pruebas_fallando"].format(pruebas="test_x"), t["pedido"]])
        self.assertEqual(d["mensaje"], esperado)

    def test_config_desconocida(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "c.json"
            p.write_text(json.dumps({"enviar_de_verdad": True}))
            with self.assertRaises(SystemExit):
                C.cargar_config(p)

    def test_vigilar_en_vivo(self):
        """OH-DUO-03 escribe alertas.jsonl de a poco (a veces media línea); el coordinador las sigue."""
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "e"
            d.mkdir()
            lineas = [nueva(A3, 2), nueva(A2, 9, f"{A2} :: test_x :: 2"),
                      {"tipo": "cierre", "terminacion": {"tipo": "agotamiento_de_iteraciones"}, "veredicto": {}}]
            texto = "".join(json.dumps(l) + "\n" for l in lineas)

            def escribir():
                with open(d / "alertas.jsonl", "w") as f:
                    for i in range(0, len(texto), 37):       # trozos que cortan líneas por la mitad
                        f.write(texto[i:i + 37])
                        f.flush()
                        time.sleep(0.01)
                # como OH-DUO-03 (escritura atómica): temporal en el mismo directorio y os.replace
                tmp = d / ".supervision.json.tmp"
                tmp.write_text(json.dumps({"final": {"iteraciones": 30}}))
                os.replace(tmp, d / "supervision.json")
            hilo = threading.Thread(target=escribir)
            hilo.start()
            r = C.vigilar(d, Path(t) / "salida", CFG, intervalo=0.02)
            hilo.join()
        self.assertTrue(r["cierre_recibido"])
        self.assertEqual([x["tipo"] for x in r["decisiones"]], ["advertencia", "nueva_estrategia"])
        self.assertEqual(r["anticipacion_iteraciones"], 21)


if __name__ == "__main__":
    unittest.main()
