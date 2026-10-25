#!/usr/bin/env python3
"""Ejecutor simulado: mismo contrato que oh-lab/ejecutor/correr.sh, sin Ollama ni bwrap.

Uso: correr.sh <semilla>, desde una instancia. El escenario sale de LAB_PLAN, un JSON
{"<misión>": {"<semilla>": "<escenario>"}}; si no figura, es «stuck». Cada llamada se anota en
$LAB_ESTADO/llamadas.jsonl. Simula lo que haría el agente: lo «resuelve» copiando la referencia SOLO en su
copia de trabajo, sin tocar la instancia.
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
semilla = sys.argv[1]
mision = json.loads((BASE / "mision.json").read_text())
plan = json.loads(os.environ.get("LAB_PLAN", "{}"))
escenario = plan.get(mision["id"], {}).get(semilla, "stuck")
inicio = time.time()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


R = BASE / "resultados" / f"{time.strftime('%Y%m%d-%H%M%S')}-s{semilla}"
R.mkdir(parents=True)
shutil.copytree(BASE / "tarea", R / "work")
(R / "huellas_inicio.txt").write_text(
    "".join(f"{sha(p)}  {p.relative_to(BASE)}\n" for p in sorted((BASE / "tarea").iterdir())))
copia_limpia = all(sha(R / "work" / p.name) == sha(p) for p in (BASE / "tarea").iterdir())
vio = sorted(os.listdir(R / "work"))   # lo que vería el agente
codigo = f"{mision['modulo']}.py"
if escenario == "exito":
    shutil.copy(BASE / "control" / codigo, R / "work" / codigo)
else:
    (R / "work" / codigo).write_text("# intento fallido\n")
if escenario == "toca_instancia":
    (BASE / "tarea" / codigo).write_text("# alterado\n")

ok = escenario in ("exito", "toca_instancia")
crit = {k: True for k in ("1_llamada_a_herramienta_valida", "2_corrio_pruebas_y_vio_fallo",
                          "3_edito_codigo_y_no_las_pruebas", "4_visibles_pasan_y_termino_con_finish",
                          "5_ocultas_pasan", "6_num_ctx_en_cada_pedido")}
estado = "finished"
if not ok:
    crit["4_visibles_pasan_y_termino_con_finish"] = crit["5_ocultas_pasan"] = False
    if escenario == "stuck":
        estado = "stuck"
    elif escenario == "ocultas":
        crit["4_visibles_pasan_y_termino_con_finish"] = True
res = {"estado_final": estado, "termino_con_finish": escenario != "stuck", "llamadas_llm": 5,
       "tokens_prompt": 8000, "tokens_generados": 300, "segundos": 30.0, "acciones": ["terminal", "finish"],
       "originales_intactos": "ok", "agente_exit": "0", "criterios": crit, "detalle": {}}
(R / "resultado_final.json").write_text(json.dumps(res))
print(f"  carpeta: {R}")
with open(Path(os.environ["LAB_ESTADO"]) / "llamadas.jsonl", "a") as f:
    f.write(json.dumps({"mision": mision["id"], "semilla": semilla, "argv": sys.argv, "inicio": inicio,
                        "fin": time.time(), "copia_limpia": copia_limpia, "vio": vio,
                        "mision_json": mision}) + "\n")
