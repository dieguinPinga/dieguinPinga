#!/usr/bin/env bash
# Validación de OH-DUO-05 en la VivoBook, en un solo comando:   bash ~/oh-duo-05/validar.sh
# SIN inferencia: el modelo es simulado (servidor local en 127.0.0.1) y no se contacta al Ollama real. No toca
# corridas de producción: la integración usa corridas propias en un directorio temporal.
#   1. prerrequisitos (venv, bwrap, oh-smoke, oh-supervisor, oh-duo-01..04, ningún correr.sh activo)
#   2. diagnóstico del SDK (las 10 comprobaciones de la investigación)
#   3. OH-DUO-01..04 idénticos a los de la rama (huellas_oh_duo_01_04.sha256)
#   4. regresión de OH-DUO-03 (18 pruebas, incluye la escritura atómica) y OH-DUO-04 (17, más 30 repeticiones
#      de test_vigilar_en_vivo, que antes fallaba de forma intermitente)
#   5. carrera OH-DUO-03 → OH-DUO-04 con el código real de los dos (40 repeticiones)
#   6. despachador (12 pruebas, sin SDK)
#   7. puente con el SDK real, en proceso (5 pruebas)
#   8. integración con bwrap real y la cadena completa: control vs intervenida (7 pruebas, ~1–2 min)
#   9. ensayo del protocolo (comparar_qwen_p1.sh --simulado): el mismo script que usarán las corridas reales
#  10. herramientas de diagnóstico contra el SDK real: analizar_par.py (stuck y contexto, 4 pruebas) y
#      auditar_b1.py (ediciones reconstruidas, pruebas protegidas, reversiones, pensamientos; 5 pruebas)
#  11. experimento A1: estado de Ollama y comparación de trayectorias (17 pruebas) y ensayo completo
#      reproducibilidad/a1.sh --simulado (3 corridas en bwrap con Ollama simulado; tiene que dar REPRODUCIBLE)
#  12. experimento B1: comprobaciones contra A1 y contrafáctico (11 pruebas) y ensayo completo
#      reproducibilidad/b1.sh --simulado contra el A1 simulado del paso 11 (3 corridas; las 3 tienen que ser válidas)
#  13. experimento E1: pruebas (7) y dos ensayos completos de e1/e1.sh --simulado contra el A1 del paso 11, con
#      modelos simulados cuya etapa de ruptura se conoce de antemano (arregla → ninguna; rechazo → aplicación)
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

huellas() { ( cd "$RAIZ" && sha256sum --quiet -c "$AQUI/huellas_oh_duo_01_04.sha256" ) && echo "$(wc -l < "$AQUI/huellas_oh_duo_01_04.sha256") archivos idénticos"; }

regresion() {
    ( cd "$RAIZ/oh-duo-03" && python3 -I -m unittest discover -s tests 2>&1 | tail -n 3 ) | tee "$SALIDA/regresion_03.txt"
    grep -q "^OK" "$SALIDA/regresion_03.txt" || return 1
    ( cd "$RAIZ/oh-duo-04" && python3 -I -m unittest discover -s tests > "$SALIDA/regresion_04.txt" 2>&1 ); local r=$?
    tail -n 3 "$SALIDA/regresion_04.txt"
    [ $r = 0 ] || return 1
    # la prueba que antes fallaba de forma intermitente, repetida: tiene que pasar todas las veces
    local i
    for i in $(seq 30); do
        ( cd "$RAIZ/oh-duo-04" && python3 -I -m unittest discover -s tests -p test_coordinador.py -k test_vigilar_en_vivo \
            >> "$SALIDA/repeticiones_vigilar_en_vivo.txt" 2>&1 ) || { echo "  test_vigilar_en_vivo falló en la repetición $i"; return 1; }
    done
    echo "  test_vigilar_en_vivo: 30/30"
}

carrera() { ( cd "$AQUI" && python3 -I -m unittest discover -s tests -p test_carrera.py -v ); }

protocolo() {
    OH5_INFORME="$SALIDA/protocolo-simulado" bash "$AQUI/comparar_qwen_p1.sh" --simulado --etiqueta validacion \
        > "$SALIDA/protocolo-simulado.log" 2>&1 || { tail -n 20 "$SALIDA/protocolo-simulado.log"; return 1; }
    local c; c="$(ls "$SALIDA"/protocolo-simulado/*/comparacion.json)"
    cat "$(dirname "$c")/comparacion.md"
    python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1]))["filas"][0]["validez"]; sys.exit(0 if v["valida"] and v["informativa"] else 1)' "$c"
}

despachador() { ( cd "$AQUI" && python3 -I -m unittest discover -s tests -p test_despachador.py -v ); }

puente() { ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_puente.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2); }

diagnostico_par() {
    ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_diagnostico.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2) || return 1
    ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_auditoria.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2)
}

a1() {
    ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_a1.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2) || return 1
    OH5_INFORME="$SALIDA/a1-simulado" bash "$AQUI/reproducibilidad/a1.sh" --simulado --etiqueta validacion \
        > "$SALIDA/a1-simulado.log" 2>&1 || { tail -n 20 "$SALIDA/a1-simulado.log"; return 1; }
    local c; c="$(ls "$SALIDA"/a1-simulado/*/comparacion_a1.json)"
    grep -E "^# |^\| a1-" "$(dirname "$c")/comparacion_a1.md"
    python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r["veredicto"] == "REPRODUCIBLE" and all(c["stuck"].get("disponible") for c in r["corridas"]) else 1)' "$c"
}

b1() {
    ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_b1.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2) || return 1
    local a1dir; a1dir="$(ls -d "$SALIDA"/a1-simulado/a1-p1-s11-validacion 2>/dev/null)"
    [ -n "$a1dir" ] || { echo "falta el A1 simulado del paso 11"; return 1; }
    OH5_INFORME="$SALIDA/b1-simulado" bash "$AQUI/reproducibilidad/b1.sh" --simulado --a1 "$a1dir" --etiqueta validacion \
        > "$SALIDA/b1-simulado.log" 2>&1 || { tail -n 20 "$SALIDA/b1-simulado.log"; return 1; }
    local c; c="$(ls "$SALIDA"/b1-simulado/*/comparacion_b1.json)"
    grep -E "^# |^Corridas válidas|^\| b1-" "$(dirname "$c")/comparacion_b1.md"
    python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); s=r["resumen"]; sys.exit(0 if not r["problemas_globales"] and s["validas"] == s["total"] == 3 and s["con_cambio_de_conducta"] == 3 and s["resueltas"] == 3 else 1)' "$c"
}

e1() {
    ( cd "$AQUI" && "$VENV/bin/python" -I -m unittest discover -s tests -p test_e1.py -v ) 2> >(grep -v "INFO\|^ \{10,\}" >&2) || return 1
    local a1dir; a1dir="$(ls -d "$SALIDA"/a1-simulado/a1-p1-s11-validacion 2>/dev/null)"
    [ -n "$a1dir" ] || { echo "falta el A1 simulado del paso 11"; return 1; }
    local v esperada c
    for v in arregla:ninguna rechazo_sin_recuperacion:aplicacion; do
        esperada="${v#*:}"; v="${v%%:*}"
        OH5_INFORME="$SALIDA/e1-simulado" bash "$AQUI/e1/e1.sh" --simulado --a1 "$a1dir" --variante "$v" --etiqueta "$v" \
            > "$SALIDA/e1-$v.log" 2>&1 || { tail -n 20 "$SALIDA/e1-$v.log"; return 1; }
        c="$SALIDA/e1-simulado/e1-p1-s11-$v/informe_e1.json"
        grep -E "^\| E1-" "$(dirname "$c")/informe_e1.md"
        python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); e=sys.argv[2]
c=r["condiciones"]; ok=set(c)=={"E1-AB","E1-C"} and all(x["etapa_de_ruptura"].startswith(e) and x["etapas"]["0_validez"]["ok"] for x in c.values())
ok=ok and r["a0"]["evaluacion"]["identico_a_la_referencia"]
sys.exit(0 if ok else 1)' "$c" "$esperada" || { echo "  $v: no dio la etapa esperada ($esperada)"; return 1; }
    done
}

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
    paso "3 OH-DUO-01..04 idénticos a la rama" huellas
    paso "4 regresión OH-DUO-03 y OH-DUO-04" regresion
    paso "5 carrera OH-DUO-03 → OH-DUO-04 (escritura atómica)" carrera
    paso "6 despachador" despachador
    paso "7 puente (SDK real)" puente
    paso "8 integración (bwrap real, control vs intervenida)" integracion
    paso "9 ensayo del protocolo (simulado, sin inferencia)" protocolo
    paso "10 herramienta de diagnóstico (SDK real)" diagnostico_par
    paso "11 experimento A1 (simulado, sin inferencia)" a1
    paso "12 experimento B1 (simulado, sin inferencia)" b1
    paso "13 experimento E1 (simulado, sin inferencia)" e1
fi
echo
if [ ${#FALLAS[@]} -eq 0 ]; then
    echo "=== OH-DUO-05 VALIDADO. Registro: $SALIDA"
else
    printf '=== FALLÓ: %s\n' "${FALLAS[@]}"
    echo "Registro: $SALIDA"
    exit 1
fi
