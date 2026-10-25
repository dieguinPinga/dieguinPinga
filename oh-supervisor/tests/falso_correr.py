#!/usr/bin/env python3
"""Doble de prueba de oh-smoke/correr.sh: mismo contrato de entrada y salida, sin Ollama ni bwrap.

Uso: correr.sh <semilla>. El escenario de cada semilla sale de FALSO_PLAN (JSON {"semilla": "escenario"}).
Cada llamada se registra en $FALSO_ESTADO/llamadas.jsonl (argv, pid, inicio, fin, copia limpia o no).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
semilla = sys.argv[1]
escenario = json.loads(os.environ["FALSO_PLAN"]).get(semilla, "exito")
estado = Path(os.environ["FALSO_ESTADO"])
inicio = time.time()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


R = BASE / "resultados" / f"{time.strftime('%Y%m%d-%H%M%S')}-s{semilla}"
if escenario == "reusar":
    R = sorted((BASE / "resultados").iterdir())[0]   # una carpeta de un intento anterior
else:
    R.mkdir(parents=True)
    shutil.copytree(BASE / "tarea", R / "work")
    lineas = [f"{sha(p)}  {p.relative_to(BASE)}" for p in sorted((BASE / "tarea").iterdir())]
    if escenario == "sin_copia":
        lineas = [f"{'0' * 64}  tarea/duracion.py"]
    (R / "huellas_inicio.txt").write_text("\n".join(lineas) + "\n")

copia_limpia = all(sha(R / "work" / p.name) == sha(p) for p in (BASE / "tarea").iterdir())
# El «agente» ensucia su copia: el intento siguiente no tiene que verlo.
(R / "work" / "duracion.py").write_text(f"# modificado por el intento de la semilla {semilla}\n")
(R / "work" / "basura.txt").write_text("restos de un intento anterior\n")

todos = {k: True for k in ("1_llamada_a_herramienta_valida", "2_corrio_pruebas_y_vio_fallo",
                           "3_edito_codigo_y_no_las_pruebas", "4_visibles_pasan_y_termino_con_finish",
                           "5_ocultas_pasan", "6_num_ctx_en_cada_pedido")}
res = {"estado_final": "finished", "termino_con_finish": True, "llamadas_llm": 6, "tokens_prompt": 9000,
       "tokens_generados": 400, "segundos": 42.0, "acciones": ["terminal", "file_editor", "terminal", "finish"],
       "vencio_tiempo": False, "corte_forzado": False, "error": None, "originales_intactos": "ok",
       "agente_exit": "0", "criterios": todos, "detalle": {}}
codigo = 0
if escenario == "stuck":
    res.update(estado_final="stuck", termino_con_finish=False)
    res["criterios"] = {**todos, "3_edito_codigo_y_no_las_pruebas": False,
                        "4_visibles_pasan_y_termino_con_finish": False, "5_ocultas_pasan": False}
elif escenario == "texto":
    res.update(termino_con_finish=False, detalle={"mensajes_de_texto_del_agente": 1})
    res["criterios"] = {**todos, "4_visibles_pasan_y_termino_con_finish": False, "5_ocultas_pasan": False}
elif escenario == "ocultas":
    res["criterios"] = {**todos, "5_ocultas_pasan": False}
elif escenario == "alterados":
    res["originales_intactos"] = "ALTERADOS"
elif escenario == "toca_tarea":
    (BASE / "tarea" / "duracion.py").write_text("# alterado\n")
elif escenario == "corte":
    res.update(estado_final="running", agente_exit="124")
    res["criterios"] = {**todos, "4_visibles_pasan_y_termino_con_finish": False, "5_ocultas_pasan": False}
elif escenario == "colgar":
    hijo = subprocess.Popen(["sleep", "300"])
    (estado / f"hijo-{semilla}.pid").write_text(str(hijo.pid))
    time.sleep(300)

if escenario == "control":
    print(f"  control previo FALLÓ: sandbox (ver control/sandbox.txt) — no se usa la GPU. Carpeta: {R}")
    codigo = 1
else:
    (R / "resultado_final.json").write_text(json.dumps(res))
    for k, v in res["criterios"].items():
        print(f"  {'OK ' if v else 'NO '} {k}")
    print(f"  carpeta: {R}")

with open(estado / "llamadas.jsonl", "a") as f:
    f.write(json.dumps({"argv": sys.argv, "semilla": semilla, "escenario": escenario, "pid": os.getpid(),
                        "inicio": inicio, "fin": time.time(), "copia_limpia": copia_limpia}) + "\n")
sys.exit(codigo)
