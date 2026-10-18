-- Explorador 0.4.2: ciclo en dos etapas (investigación → interpretación) y administración del contexto.
-- Interpretación del resultado del paso (etapa B), ya contrastada con la evidencia.
ALTER TABLE explore_steps ADD COLUMN interpretation TEXT;
-- Diagnóstico del contexto: nivel de reducción, tokens estimados, presupuesto y desbordes informados por Ollama.
ALTER TABLE explore_steps ADD COLUMN context_json TEXT;
