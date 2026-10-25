"""Despachador: solo plantillas de OH-DUO-04, solo corridas de oh-duo-05, sin enlaces, idempotente. Sin SDK.
   python3 -m unittest discover -s tests -v
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import despachador as D  # noqa: E402
from ejecutar import hermano  # noqa: E402

OH4 = hermano("oh-duo-04", "coordinador.py")
OH3 = hermano("oh-duo-03", "observador.py")
CFG = json.loads((OH4 / "coordinador.json").read_text())
T = CFG["plantillas"]["en"]


def decision(texto, tipo="nueva_estrategia", it=9):
    return {"tipo": tipo, "iteracion": it, "mensaje": texto, "mensaje_sha256": hashlib.sha256(texto.encode()).hexdigest()}


class Despachador(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh5-desp-"))
        self.corrida = self.tmp / "corrida"
        (self.corrida / D.MARCA).parent.mkdir(parents=True)
        (self.corrida / D.MARCA).write_text("{}\n")
        self.buzon = self.corrida / "ejecuciones" / "P1__qwen" / "instancia" / "resultados" / "r1" / "buzon" / "mensajes.jsonl"
        self.buzon.parent.mkdir(parents=True)
        self.buzon.write_text("")
        self.frases, self.patrones = D.plantillas(OH4)

    def desp(self, enviar=True, **kw):
        return D.Despachador(self.corrida, OH4, self.tmp / "despachos.jsonl", enviar, **kw)

    def buzon_(self):
        return [json.loads(l) for l in self.buzon.read_text().splitlines()]

    # ---- validación del texto
    def test_acepta_los_mensajes_reales_de_oh_duo_04(self):
        textos = set()
        for f in (OH4 / "informes").rglob("decisiones.jsonl"):
            for l in f.read_text().splitlines():
                d = json.loads(l)
                if d.get("mensaje"):
                    textos.add(d["mensaje"])
        self.assertTrue(textos, "no hay decisiones registradas en oh-duo-04/informes")
        for t in textos:
            self.assertEqual(D.validar_texto(t, self.frases, self.patrones), (True, "ok"), t)

    def test_acepta_combinaciones_de_plantillas(self):
        t = " ".join([T["repeticion"], T["ediciones_sin_efecto"], T["pruebas_fallando"].format(pruebas="test_a, test_b2"),
                      T["pedido"]])
        self.assertTrue(D.validar_texto(t, self.frases, self.patrones)[0])
        self.assertTrue(D.validar_texto(T["advertencia"], self.frases, self.patrones)[0])

    def test_rechaza_texto_libre_codigo_o_solucion(self):
        malos = [
            "Use max(resultado[-1][1], fin) in line 18.",
            T["repeticion"] + " The fix is max(resultado[-1][1], fin).",
            T["pedido"] + "x",
            T["pruebas_fallando"].format(pruebas="max(a, b)"),
            T["pruebas_fallando"].format(pruebas="test_a; rm -rf /"),
            T["repeticion"] + "\n" + T["pedido"],
            "",
        ]
        for t in malos:
            self.assertFalse(D.validar_texto(t, self.frases, self.patrones)[0], t)

    def test_despachar_rechaza_si_no_cierra_la_huella_o_la_plantilla(self):
        d = self.desp()
        mala = decision(T["pedido"])
        mala["mensaje_sha256"] = "0" * 64
        self.assertEqual(d.despachar("P1__qwen", mala)["resultado"], "rechazado")
        self.assertEqual(d.despachar("P1__qwen", decision("Change line 18 to use max.", it=10))["resultado"], "rechazado")
        self.assertEqual(self.buzon_(), [])

    # ---- reglas de envío
    def test_escribe_una_linea_valida_para_el_puente(self):
        r = self.desp().despachar("P1__qwen", decision(T["repeticion"] + " " + T["pedido"]))
        self.assertEqual(r["resultado"], "escrito_en_buzon")
        (m,) = self.buzon_()
        self.assertEqual(m["id"], "P1__qwen-nueva_estrategia-it9")
        self.assertEqual(hashlib.sha256(m["texto"].encode()).hexdigest(), m["sha256"])
        sys.path.insert(0, str(AQUI.parent / "ejecutor"))
        import puente
        self.assertTrue(puente.ID_VALIDO.fullmatch(m["id"]))

    def test_idempotente_tambien_entre_procesos(self):
        dec = decision(T["pedido"])
        self.assertEqual(self.desp().despachar("P1__qwen", dec)["resultado"], "escrito_en_buzon")
        self.assertIsNone(self.desp().despachar("P1__qwen", dec))     # nuevo despachador, mismo registro
        self.assertEqual(len(self.buzon_()), 1)

    def test_simulacion_no_escribe(self):
        r = self.desp(enviar=False).despachar("P1__qwen", decision(T["pedido"]))
        self.assertEqual(r["resultado"], "simulado")
        self.assertEqual(self.buzon.read_text(), "")

    def test_sin_margen_y_tipos(self):
        d = self.desp()
        self.assertEqual(d.despachar("P1__qwen", decision(T["pedido"], it=26))["resultado"], "sin_margen")
        self.assertIsNone(d.despachar("P1__qwen", decision(T["advertencia"], tipo="advertencia")))
        self.assertIsNone(d.despachar("P1__qwen", {"tipo": "sin_intervencion", "iteracion": 3}))
        d2 = self.desp(tipos=("nueva_estrategia", "advertencia"))
        self.assertEqual(d2.despachar("P1__qwen", decision(T["advertencia"], tipo="advertencia", it=4))["resultado"],
                         "escrito_en_buzon")

    def test_sin_buzon_todavia(self):
        r = self.desp().despachar("P2__qwen", decision(T["pedido"]))
        self.assertEqual(r["resultado"], "sin_buzon")

    def test_rechaza_corridas_sin_marca(self):
        (self.corrida / D.MARCA).unlink()
        with self.assertRaises(SystemExit):
            self.desp().despachar("P1__qwen", decision(T["pedido"]))
        self.assertEqual(self.buzon.read_text(), "")

    def test_no_sigue_enlaces(self):
        afuera = self.tmp / "afuera.jsonl"
        afuera.write_text("")
        self.buzon.unlink()
        self.buzon.symlink_to(afuera)
        with self.assertRaises((SystemExit, OSError)):
            self.desp().despachar("P1__qwen", decision(T["pedido"]))
        self.assertEqual(afuera.read_text(), "")
        # ni en un directorio intermedio
        self.buzon.unlink()
        r = self.buzon.parent.parent
        os.rename(r, self.tmp / "r1-real")
        (self.tmp / "r1-real" / "buzon" / "mensajes.jsonl").write_text("")
        r.symlink_to(self.tmp / "r1-real")
        with self.assertRaises(SystemExit):
            self.desp().despachar("P1__qwen", decision(T["pedido"], it=11))

    def test_sigue_decisiones_en_vivo_desde_oh_duo_04(self):
        dec = self.tmp / "decisiones" / "P1__qwen"
        dec.mkdir(parents=True)
        lineas = [json.dumps(decision(T["pedido"], it=8)), json.dumps({"tipo": "sin_intervencion", "iteracion": 9})]
        (dec / "decisiones.jsonl").write_text(lineas[0] + "\n" + lineas[1])     # la última, sin \n todavía
        vueltas = iter(range(3))
        D.seguir(self.tmp / "decisiones", self.desp(), 0.01, termina=lambda: next(vueltas, None) is None)
        self.assertEqual([m["id"] for m in self.buzon_()], ["P1__qwen-nueva_estrategia-it8"])


if __name__ == "__main__":
    unittest.main()
