#!/usr/bin/env bash
# Validación de OH-DUO-05 en la VivoBook, en un solo comando:   bash ~/oh-duo-05/validar.sh
# SIN inferencia: el modelo es simulado (servidor local en 127.0.0.1) y no se contacta al Ollama real. No toca
# corridas de producción: la integración usa corridas propias en un directorio temporal.
#   1. prerrequisitos (venv, bwrap, oh-smoke, oh-supervisor, oh-duo-01..04, ningún correr.sh activo)
#   2. diagnóstico del SDK (las 10 comprobaciones de la investigación)
#   3. OH-DUO-01..04 idénticos a los de la rama (huellas_oh_duo_01_04.sha256)
#   4. despachador (12 pruebas, sin SDK)
#   5. puente con el SDK real, en proceso (5 pruebas)
#   6. integración con bwrap real y la cadena completa: control vs intervenida (7 pruebas, ~1–2 min)
# Registro y comparación: informes/validacion-<fecha>/.
# Variables: VENV (~/oh-smoke/venv), OH_SMOKE (~/oh-smoke), OH_SUPERVISOR (~/oh-supervisor), SIN_SCOPE (0).
set -u
AQUI="$(cd "$(dirname "$0")" && pwd)"
export OH_SMOKE="${OH_SMOKE:-$HOME/oh-smoke}" OH_SUPERVISOR="${OH_SUPERVISOR:-$HOME/oh-supervisor}"
VENV="${VENV:-$OH_SMOKE/venv}"
export SIN_SCOPE="${SIN_SCOPE:-0}"
SALIDA="$AQUI/informes/validacion-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SALIDA"
exec > >(tee "$SALIDA/validacion.log") 2>&1
export OH5_INFORME="$SALIDA"

FALLAS=()
paso() { local nombre="$1"; shift; echo; echo "=== $nombre"; if "$@"; then echo "--- OK: $nombre"; else echo "--- FALLÓ: $nombre"; FALLAS+=("$nombre"); fi; }

hermanos() {     # la misma búsqueda que ejecutar.py: ../oh-duo-0X o ~/oh-duo-0X
    for m in oh-duo-01 oh-duo-02 oh-duo-03 oh-duo-04; do
        if [ -d "$AQUI/../$m" ]; then echo "$(cd "$AQUI/.." && pwd)"; return; fi
        if [ -d "$HOME/$m" ]; then echo "$HOME"; return; fi
    done
}
RAIZ="$(hermanos)"

prerrequisitos() {
    local ok=0
    [ -x "$VENV/bin/python" ] || { echo "falta $VENV/bin/python"; ok=1; }
    command -v bwrap > /dev/null || { echo "falta bwrap"; ok=1; }
    [ -f "$OH_SMOKE/relay.py" ] || { echo "falta $OH_SMOKE/relay.py"; ok=1; }
    [ -f "$OH_SUPERVISOR/supervisor.py" ] || { echo "falta $OH_SUPERVISOR/supervisor.py"; ok=1; }
    for m in oh-duo-01 oh-duo-02 oh-duo-03 oh-duo-04; do
        [ -n "$RAIZ" ] && [ -d "$RAIZ/$m" ] || { echo "falta $m (junto a oh-duo-05 o en ~)"; ok=1; }
    done
    if pgrep -f "/correr.sh" > /dev/null; then echo "hay un correr.sh en ejecución: esperar a que termine"; ok=1; fi
    echo "venv=$VENV raiz=$RAIZ bwrap=$(command -v bwrap) SIN_SCOPE=$SIN_SCOPE"
    return $ok
}

diagnostico() {
    bash "$AQUI/investigacion/diagnostico_vivobook.sh" "$VENV" | tee "$SALIDA/diagnostico.txt"
    [ "$(grep -c '^  OK ' "$SALIDA/diagnostico.txt")" = 10 ]
}

huellas() { ( cd "$RAIZ" && sha256sum --quiet -c "$AQUI/huellas_oh_duo_01_04.sha256" ) && echo "136 archivos idénticos"; }

despachador() { ( cd "$AQUI" && python3 -I -m unittest discover -s tests -p test_despachador.py -v ); }

puente() { ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_puente.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2); }

integracion() {
    ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_integracion.py -v ) > "$SALIDA/integracion.txt" 2>&1
    local r=$?
    grep -E "^(test_|Ran |OK|FAILED|== )|skipped|Error|assert" "$SALIDA/integracion.txt"
    grep -q "skipped" "$SALIDA/integracion.txt" && { echo "la integración se salteó: no cuenta como validada"; return 1; }
    [ -f "$SALIDA/comparacion.md" ] && cat "$SALIDA/comparacion.md"
    return $r
}

echo "OH-DUO-05 · validación $(date -Is) · $(hostname)"
paso "1 prerrequisitos" prerrequisitos || true
if [ ${#FALLAS[@]} -eq 0 ]; then
    paso "2 diagnóstico del SDK" diagnostico
    paso "3 OH-DUO-01..04 sin cambios" huellas
    paso "4 despachador" despachador
    paso "5 puente (SDK real)" puente
    paso "6 integración (bwrap real, control vs intervenida)" integracion
fi
echo
if [ ${#FALLAS[@]} -eq 0 ]; then
    echo "=== OH-DUO-05 VALIDADO. Registro: $SALIDA"
else
    printf '=== FALLÓ: %s\n' "${FALLAS[@]}"
    echo "Registro: $SALIDA"
    exit 1
fi
