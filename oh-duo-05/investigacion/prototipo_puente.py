"""PROTOTIPO AISLADO del puente propuesto (buzón → send_message), con SDK 1.58.1 y LLM simulado. Sin inferencia.

No está integrado en ningún ejecutor: sirve para verificar el diseño antes de implementarlo.
Uso: <venv>/bin/python -I prototipo_puente.py
"""
import json, os, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import experimento_mecanismos as X             # reutiliza el LLM simulado y conversacion()
from openhands.sdk.conversation.state import ConversationExecutionStatus as S
from openhands.sdk.event import MessageEvent, ActionEvent

def puente(conv, buzon: Path, registro: list, fin: threading.Event):
    leido, entregados = 0, set()
    while not fin.is_set():
        if buzon.is_file():
            with open(buzon, encoding="utf-8") as f:
                f.seek(leido); datos = f.read(); leido = f.tell()
            for linea in filter(None, datos.splitlines()):
                m = json.loads(linea)
                if m["id"] in entregados:
                    registro.append(("duplicado_ignorado", m["id"])); continue
                with conv.state:                                  # mismo FIFOLock que run(): atómico
                    estado = conv.state.execution_status
                    if estado != S.RUNNING:
                        registro.append(("descartado", m["id"], estado.value)); entregados.add(m["id"]); continue
                    conv.send_message(m["texto"], sender=f"oh-duo-04:{m['id']}")
                    idx = len(conv.state.events) - 1
                entregados.add(m["id"]); registro.append(("entregado", m["id"], idx))
        fin.wait(0.1)

def caso(nombre, respuesta, escribir_en, max_iter=8, demora=0.5, repetir=False):
    X.PEDIDOS.clear(); X.H.demora = demora; X.H.respuesta = staticmethod(respuesta)
    conv = X.conversacion(max_iter); buzon = Path(tempfile.mkdtemp()) / "mensajes.jsonl"
    reg, fin = [], threading.Event()
    threading.Thread(target=puente, args=(conv, buzon, reg, fin), daemon=True).start()
    conv.send_message("tarea inicial")
    def coordinador():
        time.sleep(escribir_en)
        m = json.dumps({"id": "e1-a", "texto": "REVISA TU ESTRATEGIA"}) + "\n"
        with open(buzon, "a") as f: f.write(m + (m if repetir else ""))
    threading.Thread(target=coordinador, daemon=True).start()
    conv.run(); time.sleep(1.5); fin.set()
    evs = list(conv.state.events)
    k = next((i for i, e in enumerate(evs) if isinstance(e, MessageEvent) and (e.sender or "").startswith("oh-duo-04")), None)
    sig = next((i for i, e in enumerate(evs) if i > (k or 1e9) and isinstance(e, ActionEvent)), None)
    acciones = sum(isinstance(e, ActionEvent) for e in evs)
    print(f"{nombre}: registro={reg}; mensaje en evento {k}, primera acción posterior en evento {sig}; "
          f"acciones={acciones}/{max_iter}; estado final={conv.state.execution_status.value}")

caso("1 durante run()", lambda n: X.CMD(n), escribir_en=1.2)
caso("2 duplicado en el buzón", lambda n: X.CMD(n), escribir_en=1.2, repetir=True)
caso("3 llega después de finish", lambda n: X.FIN if n >= 2 else X.CMD(n), escribir_en=0.9, demora=0.2)
X.srv.shutdown()
