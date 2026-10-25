#!/usr/bin/env python3
"""Ejecutor simulado con el contrato de oh-duo-01/ejecutor/correr.sh, sin Ollama ni bwrap.

Escenario por ejecución: DUO_PLAN = {"<problema>": {"<modelo de MODELO>": "<escenario>"}}, por omisión
«incorrecta». Para «resolver» copia la referencia desde DUO_BANCO_PRUEBA, una ruta que solo existe en las
pruebas (la instancia real no tiene referencia). Cada llamada queda en $DUO_ESTADO/llamadas.jsonl.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
semilla = sys.argv[1]
mision = json.loads((BASE / "mision.json").read_text())
modelo = os.environ.get("MODELO", "")
esc = json.loads(os.environ.get("DUO_PLAN", "{}")).get(mision["id"], {}).get(modelo, "incorrecta")
banco = Path(os.environ["DUO_BANCO_PRUEBA"]) / mision["id"]
mod = mision["modulo"]
inicio = time.time()

R = BASE / "resultados" / f"{time.strftime('%Y%m%d-%H%M%S')}-s{semilla}"
(R / "work").mkdir(parents=True)
(R / "estado").mkdir()
(R / "control").mkdir()
shutil.copytree(BASE / "tarea", R / "work", dirs_exist_ok=True)
with open(Path(os.environ["DUO_ESTADO"]) / "llamadas.jsonl", "a") as f:
    f.write(json.dumps({"problema": mision["id"], "modelo": modelo, "semilla": semilla, "inicio": inicio,
                        "instancia": sorted(os.listdir(BASE)), "tarea": {p.name: p.read_text() for p in (BASE / "tarea").iterdir()},
                        "enfoque": (BASE / "enfoque.txt").read_text(), "mision": mision}) + "\n")

if esc == "control":
    print(f"  control previo FALLÓ: sandbox — no se usa la GPU. Carpeta: {R}")
    sys.exit(1)
if esc == "colgar":
    hijo = subprocess.Popen(["sleep", "300"])
    (Path(os.environ["DUO_ESTADO"]) / f"hijo-{mision['id']}.pid").write_text(str(hijo.pid))
    time.sleep(300)

bien = esc in ("correcta", "stuck_bien", "sdk_error_bien", "fuga")
codigo = R / "work" / f"{mod}.py"
if bien:
    shutil.copy(banco / "referencia" / f"{mod}.py", codigo)
elif esc != "sin_cambios":
    codigo.write_text(codigo.read_text() + "\n# intento fallido\nraise RuntimeError('roto')\n")
if esc == "fuga":   # simula que algo de las ocultas llegó a un archivo del agente
    import ast
    nombres = [n.name for n in ast.walk(ast.parse((banco / "ocultas" / "test_ocultas.py").read_text()))
               if isinstance(n, ast.FunctionDef)]
    (R / "work" / "notas.txt").write_text(nombres[0])
if esc == "enlace":
    (R / "work" / "secreto.txt").symlink_to("/etc/hostname")

res = {"estado_final": "finished", "termino_con_finish": True, "llamadas_llm": 4, "tokens_prompt": 9000,
       "tokens_generados": 300, "segundos": 40.0, "acciones": ["terminal", "file_editor", "terminal", "finish"],
       "vencio_tiempo": False, "corte_forzado": False, "error": None, "detalle": {"mensajes_de_texto_del_agente": 0},
       "criterios": {}, "originales_intactos": "ok", "agente_exit": "0"}
if esc in ("stuck_mal", "stuck_bien"):
    res.update(estado_final="stuck", termino_con_finish=False, acciones=["terminal", "terminal"])
elif esc in ("sdk_error", "sdk_error_bien"):
    res.update(estado_final="running", termino_con_finish=False, error="Timeout('litellm.Timeout: 300 s')")
elif esc == "corte":
    res.update(estado_final="running", termino_con_finish=False, agente_exit="124")
elif esc == "texto":
    res.update(termino_con_finish=False, detalle={"mensajes_de_texto_del_agente": 1})
(R / "estado" / "resultado.json").write_text(json.dumps(res))
(R / "resultado_final.json").write_text(json.dumps(res))
(R / "estado" / "eventos.json").write_text(json.dumps([{"tipo": "ActionEvent", "herramienta": "terminal"}]))
(R / "agente.log").write_text("registro del agente\n")
(R / "puente.jsonl").write_text(json.dumps({"ruta": "/api/chat", "permitido": True, "num_ctx": 16384}) + "\n")
(R / "eval.visibles.exit").write_text("0\n" if bien else "1\n")
print(f"  carpeta: {R}")
