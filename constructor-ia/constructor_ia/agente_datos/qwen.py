"""Adaptador de Qwen para las microtareas M1–M4 (fase 2).

Reutiliza, sin modificarlos, el modelo del laboratorio (protocolo.Modelo) con los MISMOS parámetros del piloto
(temperatura 0.2, num_ctx 4096, keep_alive por pedido, tope de respuesta corta de 768 tokens, plazo de 900 s) y la
envoltura V2 del piloto: guardia de memoria antes de cada llamada, control de contexto con la reserva de respuesta y
clasificación de fallas del entorno.

Traducción para el coordinador (una falla del ENTORNO nunca se convierte en una respuesta del modelo):
- Ollama cortó o no respondió → un reintento; si vuelve a fallar → Interrumpida;
- la guardia de memoria no dejó llamar, u Ollama no está disponible → Interrumpida;
- el pedido no entra en el contexto → ContextoExcedido (falla del coordinador; no se llama).
"""
from __future__ import annotations

import time

from ..laboratorio import protocolo as PR
from ..laboratorio.piloto import brazos as BR
from ..laboratorio.piloto import v2 as V2
from .coordinador import ContextoExcedido, Interrumpida
from .microtareas import SISTEMA

PARAMETROS_PILOTO = {"num_ctx": 4096, "temperatura": 0.2, "timeout_s": 900, "num_predict_codigo": 2048,
                     "num_predict_json": 768, "keep_alive": "0s", "memoria_minima_mb": 3900}


class AdaptadorQwen:
    def __init__(self, v2modelo):
        self.v2 = v2modelo
        self.v2.system = SISTEMA
        self.nombre = f"ollama:{v2modelo.nombre}"
        self.interrupciones: list[dict] = []

    def cabe(self, prompt: str) -> bool:
        return self.v2.cabe(prompt, True)

    def pedir(self, tipo, prompt, *, json_out=False, cancel=None, corto=True, seed=None):
        for intento in (1, 2):
            try:
                return self.v2.pedir(tipo, prompt, json_out=False, corto=True, seed=seed)
            except V2.EntornoInterrumpido as e:
                self.interrupciones.append({"tipo": tipo, "intento": intento, "error": str(e)[:300], "ts": time.time()})
                if intento == 2:
                    raise Interrumpida(f"Ollama falló dos veces seguidas en {tipo}: {e}") from None
            except V2.MemoriaInsuficiente as e:
                self.interrupciones.append({"tipo": tipo, "guardia": str(e)[:300], "ts": time.time()})
                raise Interrumpida(f"guardia de memoria: {e}") from None
            except V2.ContextoInsuficiente as e:
                raise ContextoExcedido(str(e)) from None
            except (PR.LaboratorioAbortado, BR.Agotado) as e:
                self.interrupciones.append({"tipo": tipo, "error": str(e)[:300], "ts": time.time()})
                raise Interrumpida(str(e)) from None
        raise Interrumpida("sin respuesta")


def crear(cfg, ollama_url: str, modelo: str, *, num_ctx: int = 4096, keep_alive: str = "0s",
          memoria_minima_mb: int = 3900, timeout_s: int = 900) -> AdaptadorQwen:
    from ..ollama import OllamaClient
    ollama = OllamaClient(ollama_url)
    base = PR.Modelo(ollama, cfg.explorer_model, modelo, num_ctx=num_ctx, temperatura=PARAMETROS_PILOTO["temperatura"],
                     timeout_s=timeout_s, num_predict_codigo=PARAMETROS_PILOTO["num_predict_codigo"],
                     num_predict_json=PARAMETROS_PILOTO["num_predict_json"], keep_alive=keep_alive)
    # El presupuesto de llamadas lo controla el coordinador; este solo es un tope de seguridad.
    return AdaptadorQwen(V2.ModeloV2(base, 50, 6 * 3600, V2.Guardia(ollama, minimo_mb=memoria_minima_mb)))
