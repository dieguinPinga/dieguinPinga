#!/usr/bin/env python3
"""
Dashboard para sh_etp_v2_local.TiposDeMovimiento
Solo lectura. Actualización cada 10 segundos.
"""

import sys
import os
import json
import subprocess
import urllib.request
import urllib.error
import threading
import time
import socket
import re
from datetime import datetime, timedelta, date
from decimal import Decimal
from flask import Flask, jsonify, send_file, Response, request
import pymysql
from pymysql.cursors import DictCursor

app = Flask(__name__)
IA_REASONING_LOG = []
IA_AUTONOMOUS_CONCLUSIONS = []
TELEGRAM_SENT_HALLAZGO_FIRMAS = set()
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b"
AUTO_IA_ENABLED = True
AUTO_IA_INTERVAL_SECONDS = int(os.environ.get("AUTO_IA_INTERVAL_SECONDS", "300"))
# Modo drain: si al terminar una cadena queda cola de curiosidad pendiente,
# lanzar otra cadena tras una espera corta en vez del intervalo largo.
AUTO_COLA_DRAIN_SLEEP_SECONDS = int(os.environ.get("AUTO_COLA_DRAIN_SLEEP_SECONDS", "5"))
AUTO_COLA_DRAIN_MAX_CADENAS = int(os.environ.get("AUTO_COLA_DRAIN_MAX_CADENAS", "20"))
AUTO_COLA_DRAIN_MAX_MINUTES = int(os.environ.get("AUTO_COLA_DRAIN_MAX_MINUTES", "60"))
AUTO_IA_CHAIN_DELAY_SECONDS = 5
AUTO_IA_MAX_CHAIN_CYCLES = 10
AUTO_IA_MIN_CONFIDENCE = 0.55
AUTO_OBSERVADORES_ENABLED = os.environ.get("AUTO_OBSERVADORES_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
AUTO_OBSERVADOR_INTERVAL_SECONDS = int(os.environ.get("AUTO_OBSERVADOR_INTERVAL_SECONDS", "1800"))
AUTO_DICCIONARIO_ENABLED = os.environ.get("AUTO_DICCIONARIO_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
AUTO_DICCIONARIO_INTERVAL_SECONDS = int(os.environ.get("AUTO_DICCIONARIO_INTERVAL_SECONDS", "21600"))
AUTO_RELACIONES_ENABLED = os.environ.get("AUTO_RELACIONES_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
AUTO_RELACIONES_INTERVAL_SECONDS = int(os.environ.get("AUTO_RELACIONES_INTERVAL_SECONDS", "21600"))
FASE2_MAX_PASOS = int(os.environ.get("FASE2_MAX_PASOS", "4"))
FASE2_SCHEMA_TXT = """
Tabla principal: TiposDeMovimiento
Columnas seguras:
`idTiposDeMovimiento`, `NroLote`, `TiposDeMovimiento`, `UnidadMedida`, `kilos`, `FechaHora`, `Fecha`, `Hora`, `Descuento`, `kilosNetos`, `Proveedor`, `Materiales`, `Description`, `Operador`, `Maquina`, `Cat10`, `Cat11`, `Cat12`, `NroEnsayo`, `KgsRedond`, `KgsRedondMenos`, `Lote`, `SegsEmbud`, `Precio`, `FechaEnsayo`, `ML`, `MLmenos`, `DescCorta`, `IDMateriaPrima`, `IDOperador`, `IDMaquina`, `User_Bot`, `ID_Bolson`, `Descuento2`
Reglas SQL:
- Usar solo la tabla `TiposDeMovimiento`, salvo que se indique lo contrario.
- No usar `SHOW TABLES`, `DESCRIBE` ni `SELECT *`; el esquema ya esta arriba.
- Usar siempre backticks en nombres de columnas.
- `FechaMovimiento` no existe; usar `FechaHora` o `Fecha`.
- Consultas maximo 20 filas y preferir agregaciones con COUNT/SUM/AVG.
- `kilosNetos` es NULL en la mayoria de las filas (solo se completa en movimientos con ensayo/ajuste); para sumarla usar `WHERE kilosNetos IS NOT NULL` o `COALESCE(kilosNetos, 0)`, si no el SUM da NULL para todos los grupos. Para volumen general usar `kilos`.
"""

FASE2_SCHEMA_MIN_TXT = """La UNICA tabla que existe en esta base es TiposDeMovimiento. No hay tablas llamadas ventas, clientes, pedidos ni ninguna otra: si inventas un nombre de tabla la query sera rechazada. Columnas clave: TiposDeMovimiento, FechaHora, Fecha, Proveedor, Materiales, Description, Operador, Maquina, kilos, kilosNetos, Descuento, Descuento2, Precio, IF. Si buscas ventas, despachos o salidas: NO son una tabla aparte, son un valor dentro de la columna TiposDeMovimiento de esta misma tabla. Los valores de despacho reales empiezan con 'Despacho' (ej. WHERE TiposDeMovimiento LIKE 'Despacho%'; el mas comun es 'Despacho_PT'; tambien existen 'Despacho FASON PROD.', 'Despacho Devolucion', 'Despacho_FARDOS', entre otros). Para produccion el valor es 'PRODUCCION'. Antes de concluir un total de kilos de un periodo, preguntate "kilos de que tipo de movimiento?": un SUM(kilos) de toda la tabla sin desglosar no es una conclusion util. Primero corre GROUP BY TiposDeMovimiento (con SUM(kilos) y COUNT(*)) acotado al periodo para ver cuales son los tipos principales de ese mes (vas a ver nombres como PRODUCCION, variantes de 'Despacho...' que son salidas, variantes de 'ING ...' que son entradas/compras, entre otros: el significado de cada tipo se infiere de su nombre y de su patron de uso, no esta fijo). Concluir sobre 1-2 tipos principales del periodo (cuanto, que tipo) es mucho mas util que un total ciego. Si ya tenes el total de un mes anterior para el mismo tipo, compara y decilo (subio/bajo y cuanto). Reglas: solo SELECT sobre TiposDeMovimiento (FROM TiposDeMovimiento, sin excepcion); no SELECT *; usar LIMIT 5; preferir COUNT/SUM/AVG y GROUP BY. kilosNetos suele ser NULL salvo en movimientos con ensayo/ajuste: si la sumas, usa WHERE kilosNetos IS NOT NULL o COALESCE(kilosNetos,0); para volumen general usa kilos."""
ACCIONES_JSON_EJEMPLO = """{"action":"query","query":"SELECT ... LIMIT 5","razonamiento":"breve","que_espero_encontrar":"breve"}
{"action":"conclude","conclusion":"breve","confianza":0.7,"evidencia":"breve","periodo_metrica":"opcional: ej ingresos_kg","periodo_valor":"opcional: numero","periodo_label":"opcional: ej mayo 2026"}
{"action":"hypothesis","hipotesis_nueva":"breve","pregunta_para_diego":"breve","confianza":0.5}"""
ACCIONES_ESTRUCTURALES_JSON_EJEMPLO = """{"action":"conclude","conclusion":"lectura estructural breve","confianza":0.68,"evidencia":"dato concreto de la ficha"}
{"action":"hypothesis","hipotesis_nueva":"hipotesis o limitacion breve","pregunta_para_diego":"pregunta concreta","confianza":0.55}"""
FASE2_PROMPT_MAX_CHARS = int(os.environ.get("FASE2_PROMPT_MAX_CHARS", "2800"))
FASE2_OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("FASE2_OLLAMA_TIMEOUT_SECONDS", "120"))
FASE2_OLLAMA_NUM_PREDICT = int(os.environ.get("FASE2_OLLAMA_NUM_PREDICT", "180"))
PENDIENTE_OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("PENDIENTE_OLLAMA_TIMEOUT_SECONDS", "180"))
PENDIENTE_OLLAMA_TIMEOUT_MAX_SECONDS = 180
PENDIENTE_OLLAMA_NUM_PREDICT = int(os.environ.get("PENDIENTE_OLLAMA_NUM_PREDICT", "110"))
AUTO_IA_LOCK = threading.Lock()
AUTO_IA_LAST_RUN = None
AUTO_IA_LAST_ERROR = None
AUTO_IA_IN_PROGRESS = False
AUTO_IA_CURRENT_START = None
AUTO_IA_LAST_FINISH = None
AUTO_IA_LAST_RESULT = None
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_TIMEOUT_SECONDS", "12"))
TELEGRAM_STARTUP_NOTIFY = os.environ.get("TELEGRAM_STARTUP_NOTIFY", "1").strip().lower() not in ("0", "false", "no", "off")
TELEGRAM_NOTIFY_REASONING = os.environ.get("TELEGRAM_NOTIFY_REASONING", "1").strip().lower() in ("1", "true", "yes", "on")
TELEGRAM_REASONING_MODE = os.environ.get("TELEGRAM_REASONING_MODE", "interesting").strip().lower()
TELEGRAM_NOTIFY_LOW_INTEREST = os.environ.get("TELEGRAM_NOTIFY_LOW_INTEREST", "0").strip().lower() in ("1", "true", "yes", "on")
TELEGRAM_INCLUDE_EVIDENCE = os.environ.get("TELEGRAM_INCLUDE_EVIDENCE", "1").strip().lower() in ("1", "true", "yes", "on")
TELEGRAM_COMPACT_HEADERS = os.environ.get("TELEGRAM_COMPACT_HEADERS", "1").strip().lower() in ("1", "true", "yes", "on")
TELEGRAM_STARTUP_ACCESS_HINTS = os.environ.get("TELEGRAM_STARTUP_ACCESS_HINTS", "1").strip().lower() in ("1", "true", "yes", "on")
TELEGRAM_REASONING_TYPES = {
    "autonomia",
    "autonomia_error",
    "observacion",
    "explorador_inicio",
    "explorador_consulta_modelo",
    "explorador_query",
    "explorador_resultado",
    "explorador_error_ollama",
    "explorador_error_parse",
    "explorador_sin_query",
    "explorador_concluye",
    "explorador_conclusion_final",
    "explorador_patrones",
    "explorador_anomalias",
    "explorador_hipotesis",
    "explorador_sin_conclusion",
    "explorador_persistida",
    "explorador_persistencia_error",
    "relaciones_interesantes",
}
TELEGRAM_INTERESTING_REASONING_TYPES = {
    "autonomia_error",
    "explorador_error_ollama",
    "explorador_error_parse",
    "explorador_sin_query",
    "explorador_hipotesis",
    "explorador_persistencia_error",
    "relaciones_interesantes",
    "explorador_periodo_confirmado",
    "explorador_periodo_error",
}
# Errores/no-conclusiones del explorador no van a Telegram salvo debug explícito
TELEGRAM_DEBUG_EXPLORADOR = os.environ.get("TELEGRAM_DEBUG_EXPLORADOR", "0").strip().lower() in ("1", "true", "yes", "on")
if not TELEGRAM_DEBUG_EXPLORADOR:
    for _t in ("explorador_error_ollama", "explorador_sin_conclusion"):
        TELEGRAM_REASONING_TYPES.discard(_t)
        TELEGRAM_INTERESTING_REASONING_TYPES.discard(_t)
# Dedupe de reportes operativos de microtareas y umbral de alerta kg-primero
TELEGRAM_DEDUPE_KG_DELTA = float(os.environ.get("TELEGRAM_DEDUPE_KG_DELTA", "2000"))
TELEGRAM_DEDUPE_PCT_DELTA = float(os.environ.get("TELEGRAM_DEDUPE_PCT_DELTA", "10"))
TELEGRAM_ALERTA_KG_MINIMO = float(os.environ.get("TELEGRAM_ALERTA_KG_MINIMO", "2000"))
# Explorador libre con prompt largo: apagado por defecto; trabajo determinístico primero
EXPLORADOR_LIBRE_ENABLED = os.environ.get("EXPLORADOR_LIBRE_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
FASE2_FOCO_INDEX = 0  # Rota el ángulo de análisis en cada ciclo de fase 2

# Configuración de base de datos
DB_CONFIG = {
    'host': 'localhost',
    'user': 'crowdbot_sync',
    'password': 'T1HB7GtE7T4KarXM8cthPD6jVJTgpF2dp7EjJbrVuLY',
    'database': 'sh_etp_v2_local',
    'charset': 'utf8mb4',
    'cursorclass': DictCursor,
}

def get_db_connection():
    """Abre conexión a la base de datos."""
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        print(f"Error conectando a BD: {e}", file=sys.stderr)
        return None

def get_stats():
    """Obtiene estadísticas de la tabla."""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        
        # Total de registros
        cursor.execute("SELECT COUNT(*) as total FROM TiposDeMovimiento")
        total = cursor.fetchone()['total']
        
        # ID máximo
        cursor.execute("SELECT MAX(idTiposDeMovimiento) as max_id FROM TiposDeMovimiento")
        max_id = cursor.fetchone()['max_id']
        
        # FechaHora del último movimiento ingresado, según ID máximo
        cursor.execute("""
            SELECT FechaHora AS last_fecha
            FROM TiposDeMovimiento
            ORDER BY idTiposDeMovimiento DESC
            LIMIT 1
        """)
        last_fecha = cursor.fetchone()['last_fecha']
        
        # Cantidad y suma de kilos por TiposDeMovimiento
        cursor.execute("""
            SELECT 
                TiposDeMovimiento,
                COUNT(*) as cantidad,
                ROUND(SUM(kilos), 2) as sum_kilos
            FROM TiposDeMovimiento
            GROUP BY TiposDeMovimiento
            ORDER BY cantidad DESC
        """)
        tipos_data = cursor.fetchall()
        
        # Últimos 10 movimientos
        cursor.execute("""
            SELECT 
                idTiposDeMovimiento,
                FechaHora,
                TiposDeMovimiento,
                Description,
                Proveedor,
                Maquina,
                kilos
            FROM TiposDeMovimiento
            ORDER BY idTiposDeMovimiento DESC
            LIMIT 10
        """)
        ultimos = cursor.fetchall()
        ultimos.reverse()  # Mostrar en orden ascendente (de viejo a nuevo)
        
        cursor.close()
        conn.close()
        
        return {
            'total': total,
            'max_id': max_id,
            'last_fecha': str(last_fecha) if last_fecha else 'N/A',
            'tipos': tipos_data,
            'ultimos': ultimos,
            'timestamp': datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"Error en get_stats: {e}", file=sys.stderr)
        return None

def get_service_status():
    """Obtiene estado del servicio systemd."""
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'is-active', 'crowdbot-sync-tipos'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {str(e)}"


def get_ia_connection():
    """Conexión a crowdbot_lab, base editable IA."""
    try:
        return pymysql.connect(
            host='localhost',
            user='crowdbot_lab_sync',
            password='LabSync_2026_local',
            database='crowdbot_lab',
            charset='utf8mb4',
            cursorclass=DictCursor,
        )
    except Exception as e:
        print(f"Error conectando a crowdbot_lab: {e}", file=sys.stderr)
        return None


def get_ia_estado():
    conn = get_ia_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT clave, valor_json, actualizado_en
            FROM estado_observado
            WHERE clave = 'planta_actual'
            LIMIT 1
        """)
        estado = cur.fetchone()

        cur.execute("""
            SELECT id, tipo, confianza, texto, creada_en, vigente
            FROM inferencias
            ORDER BY id DESC
            LIMIT 10
        """)
        inferencias = cur.fetchall()

        cur.execute("""
            SELECT id, modelo, salida_json, creada_en, vigente
            FROM conclusiones_modelo
            WHERE vigente = 1
            ORDER BY id DESC
            LIMIT 1
        """)
        modelo_row = cur.fetchone()

        conclusiones_modelo = []
        if modelo_row and modelo_row.get("salida_json"):
            try:
                sm = modelo_row["salida_json"]
                if isinstance(sm, str):
                    sm = json.loads(sm)
                conclusiones_modelo = sm.get("conclusiones", [])
            except Exception:
                conclusiones_modelo = []

        cur.execute("""
            SELECT id, pregunta, motivo, creada_en, estado
            FROM dudas_modelo
            WHERE estado = 'abierta'
            ORDER BY id DESC
            LIMIT 10
        """)
        dudas_modelo = cur.fetchall()

        cur.execute("""
            SELECT id, columna, hipotesis, pregunta_usuario, confianza, creada_en, estado
            FROM hipotesis_esquema
            WHERE vigente = 1
              AND tipo = 'duda'
              AND estado = 'requiere_usuario'
            ORDER BY id ASC
            LIMIT 20
        """)
        preguntas_esquema = cur.fetchall()

        cur.execute("""
            SELECT id, evento, severidad, descripcion, detectado_en, resuelto
            FROM eventos_detectados
            ORDER BY id DESC
            LIMIT 20
        """)
        eventos = cur.fetchall()

        cur.execute("""
            SELECT id, tipo, prioridad, texto, creada_en, vigente
            FROM conclusiones
            WHERE vigente = 1
            ORDER BY FIELD(prioridad, 'critica', 'alta', 'media', 'baja'), id DESC
            LIMIT 20
        """)
        conclusiones = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) AS total FROM mqtt_inbox
        """)
        mqtt_total = cur.fetchone()["total"]

        cur.close()
        conn.close()

        estado_valor = None
        if estado and estado.get("valor_json"):
            try:
                estado_valor = json.loads(estado["valor_json"])
            except Exception:
                estado_valor = estado["valor_json"]

        contexto_path = "/home/plapopepo/openclaw-lab/contextos_ia/contexto_operacional_actual.json"
        contexto_operacional = {}
        try:
            with open(contexto_path, "r", encoding="utf-8") as f:
                contexto_operacional = json.load(f)
        except Exception as e:
            contexto_operacional = {"error": str(e), "path": contexto_path}

        return {
            "estado": estado_valor,
            "estado_actualizado_en": str(estado["actualizado_en"]) if estado else None,
            "inferencias": inferencias,
            "conclusiones": conclusiones,
            "conclusiones_modelo": conclusiones_modelo,
            "modelo_ia": modelo_row,
            "dudas_modelo": dudas_modelo,
            "preguntas_esquema": preguntas_esquema,
            "eventos": eventos,
            "contexto_operacional": contexto_operacional,
            "mqtt_total": mqtt_total,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        print(f"Error en get_ia_estado: {e}", file=sys.stderr)
        return None


def get_table_columns(cursor, table_name):
    """Devuelve las columnas disponibles para una tabla de crowdbot_lab."""
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
    """, (table_name,))
    return {row["COLUMN_NAME"] for row in cursor.fetchall()}


def scalar_count(cursor, query, params=None):
    cursor.execute(query, params or ())
    row = cursor.fetchone()
    if not row:
        return 0
    return list(row.values())[0] or 0


def shorten_text(text, max_len=180):
    if text is None:
        return ""
    clean = " ".join(str(text).split())
    if len(clean) <= max_len:
        return clean
    return clean[:max_len - 1].rstrip() + "..."


def shorten_multiline(text, max_len=1200):
    if text is None:
        return ""
    clean_lines = [line.rstrip() for line in str(text).splitlines()]
    clean = "\n".join(clean_lines).strip()
    if len(clean) <= max_len:
        return clean
    return clean[:max_len - 1].rstrip() + "..."


def formatear_numero_telegram(valor, unidad=None):
    try:
        numero = float(str(valor).replace(",", "."))
    except Exception:
        return str(valor)

    if abs(numero) >= 1000:
        decimales = 0
    elif abs(numero - round(numero)) < 0.000001:
        decimales = 0
    else:
        decimales = 1

    texto = f"{numero:,.{decimales}f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    if unidad:
        texto = f"{texto} {unidad}"
    return texto


def formatear_valor_muestra(valor, columna=None):
    texto = str(valor).strip()
    col_l = str(columna or "").lower()
    if texto in ("", "None", "NULL", "null", "none"):
        if "proveedor" in col_l:
            return "sin proveedor"
        if "operador" in col_l:
            return "sin operador"
        if "maquina" in col_l or "máquina" in col_l:
            return "sin maquina"
        return "sin dato"

    unidad = "kg" if "kilo" in col_l or col_l in ("kg", "kgs") else None
    try:
        float(texto.replace(",", "."))
        es_numero = True
    except Exception:
        es_numero = False
    if es_numero:
        return formatear_numero_telegram(texto, unidad=unidad)
    return texto


def formatear_fila_muestra(fila, columnas=None):
    partes = [p.strip() for p in str(fila).split("|")]
    if len(partes) <= 1:
        return shorten_text(fila, 150)

    formateadas = []
    for idx, parte in enumerate(partes):
        columna = columnas[idx] if columnas and idx < len(columnas) else None
        formateadas.append(formatear_valor_muestra(parte, columna))

    if all(p == "-" for p in formateadas):
        return ""
    return " | ".join(formateadas)


def normalizar_numeros_texto_telegram(texto):
    def repl(match):
        valor = match.group(0)
        limpio = valor.lstrip("+-")
        if "." not in limpio and len(limpio) <= 4:
            return valor
        try:
            numero = float(valor)
        except Exception:
            return valor
        if abs(numero) < 1000 and len(valor) < 8:
            return valor
        return formatear_numero_telegram(valor)

    return re.sub(r"(?<![\w])[-+]?\d{4,}(?:\.\d+)?(?![\w])", repl, str(texto or ""))


def time_label(value):
    if not value:
        return "--:--"
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except Exception:
        return str(value)[11:16] if len(str(value)) >= 16 else str(value)


def get_local_ips():
    ips = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = item[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except Exception:
        pass
    return ips


def get_tailscale_ips():
    ips = []
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                ip = line.strip()
                if ip and ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


def get_access_ips():
    lan_ips = [ip for ip in get_local_ips() if not ip.startswith("100.")]
    tailscale_ips = get_tailscale_ips()
    for ip in get_local_ips():
        if ip.startswith("100.") and ip not in tailscale_ips:
            tailscale_ips.append(ip)
    return {
        "lan": lan_ips,
        "tailscale": tailscale_ips,
        "all": lan_ips + [ip for ip in tailscale_ips if ip not in lan_ips],
    }


def telegram_debe_enviar_razonamiento(tipo):
    tipo = str(tipo or "")
    if tipo.startswith("pendiente_") or tipo in (
        "explorador_error_parse",
        "explorador_error_ollama",
        "microtarea_timeout_cooldown",
    ):
        return False
    if not TELEGRAM_NOTIFY_REASONING:
        return False
    if TELEGRAM_REASONING_MODE == "all":
        return tipo in TELEGRAM_REASONING_TYPES
    return tipo in TELEGRAM_INTERESTING_REASONING_TYPES


def nivel_interes_conclusion(item):
    anomalias = item.get("anomalias") or []
    anomalias_txt = " ".join(str(x) for x in anomalias)
    texto = (item.get("conclusion") or "") + " " + (item.get("regla_aprendible") or "") + " " + anomalias_txt
    texto_l = texto.lower()
    conf = confianza_float(item.get("confianza")) or 0

    sin_hallazgo = any(x in texto_l for x in [
        "no se observaron", "no se observan", "no se identificaron",
        "no se han identificado", "no se encontraron", "no se han encontrado",
        "no se ha encontrado", "no se encontro", "no se encontró",
        "no hay evidencia", "sin evidencia",
        "sin anomal", "no hay anomal", "sin errores", "no hay errores",
        "no requiere una nueva consulta", "no requieran una nueva consulta",
        "es consistente", "consistente con los datos", "distribución normal",
        "distribucion normal",
    ])
    generico = any(x in texto_l for x in [
        "resumen valioso", "información valiosa", "informacion valiosa",
        "máquinas más activas", "maquinas mas activas",
        "más activos en términos", "mas activos en terminos",
        "alta frecuencia y volumen", "priorizar la atención",
        "priorizar la atencion", "optimización o mantenimiento",
        "optimizacion o mantenimiento", "mayor número de movimientos",
        "mayor numero de movimientos", "realizan el mayor número",
        "realizan el mayor numero", "mantenimiento más frecuente",
        "mantenimiento mas frecuente", "requieren mantenimiento",
        "supervisión adicional", "supervision adicional",
        "principales proveedores", "principales maquinas",
        "principales máquinas",
    ])
    tiene_aprendizaje_fuerte = bool(item.get("regla_aprendible") or item.get("hipotesis_refinada"))
    if (sin_hallazgo or generico) and not anomalias and not tiene_aprendizaje_fuerte:
        return "baja"

    alta = any(x in texto_l for x in [
        "error", "nulo", "null", "faltante", "inconsisten",
        "alert", "fuera", "extremo", "negativo", "imposible",
    ])
    if alta:
        return "alta"
    if anomalias:
        return "media"
    if conf >= 0.9:
        return "media"
    return "media" if conf >= 0.75 else "baja"


def texto_sin_hallazgo_operativo(texto):
    texto_l = str(texto or "").lower()
    frases_sin_hallazgo = [
        "no se observaron", "no se observan", "no se identificaron",
        "no se han identificado", "no se encontraron", "no se han encontrado",
        "no se ha encontrado", "no se encontro", "no se encontró",
        "no hay evidencia", "sin evidencia",
        "sin anomal", "no hay anomal", "sin errores", "no hay errores",
        "no requiere una nueva consulta", "no requieran una nueva consulta",
        "es consistente", "consistente con los datos",
    ]
    frases_genericas = [
        "visión general", "vision general", "análisis inicial", "analisis inicial",
        "información valiosa", "informacion valiosa", "lista de los",
        "puede ser útil", "puede ser util", "proporciona información",
        "proporciona informacion", "resumen valioso", "maquinas mas activas",
        "máquinas más activas", "más activos en términos", "mas activos en terminos",
        "alta frecuencia y volumen", "priorizar la atención", "priorizar la atencion",
        "mayor número de movimientos", "mayor numero de movimientos",
        "realizan el mayor número", "realizan el mayor numero",
        "mantenimiento más frecuente", "mantenimiento mas frecuente",
        "requieren mantenimiento", "supervisión adicional", "supervision adicional",
        "principales proveedores", "principales maquinas", "principales máquinas",
    ]
    tiene_numero = any(ch.isdigit() for ch in texto_l)
    return any(x in texto_l for x in frases_sin_hallazgo) or (
        any(x in texto_l for x in frases_genericas) and not tiene_numero
    )


def firma_tematica_hallazgo(item):
    texto = " ".join([
        str(item.get("columna") or ""),
        str(item.get("conclusion") or ""),
        str(item.get("hipotesis_refinada") or ""),
        str(item.get("regla_aprendible") or ""),
        str(item.get("anomalias") or ""),
    ]).lower()

    if any(x in texto for x in ["error_registro", "error registro"]):
        return "errores_registro"
    if any(x in texto for x in ["descuento", "ajuste"]):
        return "descuentos_ajustes"
    if any(x in texto for x in ["proveedor", "proveedores", "suministro", "cadena de suministro"]):
        if any(x in texto for x in ["maquina", "máquina", "linea lavado", "extrusora", "hartig", "molino", "coloidal"]):
            return "maquinas_proveedores"
        return "proveedores"
    if any(x in texto for x in ["operador", "operadores"]):
        return "operadores"
    if any(x in texto for x in ["maquina", "máquina", "linea lavado", "extrusora", "hartig", "molino", "coloidal"]):
        return "maquinas_top"
    if any(x in texto for x in ["fecha", "hora", "diaria", "dia", "día", "temporal", "pico"]):
        return "ritmo_temporal"
    if any(x in texto for x in ["lote", "lotes", "nrolote", "trazabilidad"]):
        return "trazabilidad_lotes"
    limpio = " ".join(ch for ch in texto.split()[:12])
    return limpio[:120] or str(item.get("id") or "sin_firma")


def telegram_firma_repetida_reciente(firma, limite=20):
    if not firma:
        return False
    if firma in TELEGRAM_SENT_HALLAZGO_FIRMAS:
        return True

    conn = get_ia_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cols = get_table_columns(cur, "ia_conclusiones_autonomas")
        if not cols:
            return False
        id_col = "id" if "id" in cols else "creada_en"
        conclusion_col = "conclusion" if "conclusion" in cols else "NULL AS conclusion"
        hip_col = "hipotesis_refinada" if "hipotesis_refinada" in cols else "NULL AS hipotesis_refinada"
        regla_col = "regla_aprendible" if "regla_aprendible" in cols else "NULL AS regla_aprendible"
        salida_col = "salida_json" if "salida_json" in cols else "NULL AS salida_json"
        cur.execute(f"""
            SELECT {conclusion_col}, {hip_col}, {regla_col}, {salida_col}
            FROM ia_conclusiones_autonomas
            ORDER BY {id_col} DESC
            LIMIT %s
        """, (limite,))
        total = 0
        for row in cur.fetchall():
            salida = row.get("salida_json")
            salida_data = None
            if salida:
                try:
                    salida_data = json.loads(salida) if isinstance(salida, str) else salida
                except Exception:
                    salida_data = None
            if isinstance(salida_data, dict):
                row["conclusion"] = row.get("conclusion") or primer_texto(
                    salida_data.get("conclusion"),
                    salida_data.get("conclusion_principal"),
                    salida_data.get("resumen"),
                )
                row["anomalias"] = salida_data.get("anomalias") or []
            if firma_tematica_hallazgo(row) == firma:
                total += 1
                if total >= 2:
                    return True
        return False
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def limpiar_texto_hallazgo(texto):
    texto = " ".join(str(texto or "").replace("\n", " ").split())
    prefijos = [
        "Se ha identificado que ",
        "Se ha identificado ",
        "Se han identificado ",
        "Se ha obtenido ",
        "Los resultados indican que ",
        "El resultado muestra que ",
        "La consulta realizada proporciona ",
    ]
    for prefijo in prefijos:
        if texto.startswith(prefijo):
            texto = texto[len(prefijo):]
            break
    return texto.strip()


def extraer_lineas_concretas(texto, max_lineas=3):
    texto = limpiar_texto_hallazgo(texto)
    if not texto:
        return []

    marcadores = [
        " kg", "kilo", "%", "linea", "extrusora", "molino", "hartig",
        "maquina", "proveedor", "operador", "lote", "null", "none",
        "nulo", "faltante", "error", "anom", "inconsisten",
    ]
    partes = []
    for separador in [". ", "; "]:
        if separador in texto:
            partes = [p.strip(" .;") for p in texto.split(separador)]
            break
    if not partes:
        partes = [texto.strip(" .;")]

    concretas = []
    for parte in partes:
        parte_l = parte.lower()
        tiene_numero = any(ch.isdigit() for ch in parte)
        tiene_marcador = any(m in parte_l for m in marcadores)
        if parte and (tiene_numero or tiene_marcador):
            concretas.append(shorten_text(parte, 220))
        if len(concretas) >= max_lineas:
            break

    if not concretas and texto:
        concretas.append(shorten_text(texto, 220))
    return concretas


def lineas_evidencia_telegram(conclusion):
    if not TELEGRAM_INCLUDE_EVIDENCE:
        return []
    evidencia = conclusion.get("evidencia") or {}
    if not isinstance(evidencia, dict):
        return []

    lineas = []
    resumen = evidencia.get("ultimo_resumen")
    total_queries = evidencia.get("historial_queries")
    if resumen or total_queries:
        partes = []
        if total_queries is not None:
            partes.append(f"{total_queries} query(s)")
        if resumen:
            partes.append(str(resumen))
        lineas.append("Evidencia: " + " | ".join(partes))

    ultimo_resultado = evidencia.get("ultimo_resultado") or ""
    if ultimo_resultado:
        filas = [f.strip() for f in str(ultimo_resultado).splitlines() if f.strip()]
        utiles = [f for f in filas if not set(f) <= {"-"}]
        if utiles:
            lineas.append("Muestra:")
            columnas = [p.strip() for p in utiles[0].split("|")] if utiles else []
            agregadas = 0
            for idx, fila in enumerate(utiles):
                muestra = formatear_fila_muestra(fila, columnas if idx else None)
                if not muestra:
                    continue
                lineas.append("- " + shorten_text(muestra, 150))
                agregadas += 1
                if agregadas >= 4:
                    break
    return lineas


def formatear_anomalia_telegram(item):
    if isinstance(item, dict):
        nombre = item.get("nombre") or item.get("tipo") or item.get("titulo")
        detalle = (
            item.get("descripcion")
            or item.get("detalles")
            or item.get("detalle")
            or item.get("razon")
            or item.get("motivo")
        )
        if nombre and detalle:
            return f"{nombre}: {detalle}"
        return nombre or detalle or json.dumps(item, ensure_ascii=False)
    return str(item)


def add_reasoning_step(tipo, texto, detalle=None):
    step = {
        "fecha": datetime.now().isoformat(),
        "hora": datetime.now().strftime("%H:%M:%S"),
        "tipo": tipo,
        "texto": texto,
        "detalle": detalle,
    }
    IA_REASONING_LOG.insert(0, step)
    del IA_REASONING_LOG[80:]
    if telegram_debe_enviar_razonamiento(tipo):
        texto_msg = shorten_text(texto, 500)
        detalle_msg = shorten_text(detalle, 500) if detalle else ""
        if tipo in ("explorador_concluye", "explorador_conclusion_final"):
            lineas = extraer_lineas_concretas(f"{texto or ''}. {detalle or ''}", max_lineas=2)
            if lineas:
                texto_msg = "Dato concreto:"
                detalle_msg = "\n".join(f"- {linea}" for linea in lineas)
        mensaje = "\n".join([
            f"IA | {tipo} | {step['hora']}",
            texto_msg,
            detalle_msg,
        ]).strip()
        threading.Thread(target=enviar_telegram, args=(mensaje,), daemon=True).start()
    return step


def telegram_configurado():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def enviar_telegram(mensaje):
    if not telegram_configurado():
        return {"ok": False, "disabled": True}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_SECONDS) as resp:
            respuesta = json.loads(resp.read().decode("utf-8"))
            return {"ok": bool(respuesta.get("ok")), "response": respuesta}
    except Exception as e:
        print(f"Error enviando Telegram: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def formatear_conclusion_telegram(conclusion, origen="ia_local"):
    columna = conclusion.get("columna") or origen
    estado = conclusion.get("estado") or "conocimiento_nuevo"
    confianza = conclusion.get("confianza")
    texto = conclusion.get("conclusion") or conclusion.get("conclusion_principal") or ""
    regla = conclusion.get("regla_aprendible") or conclusion.get("regla_aprendida") or ""
    siguiente = conclusion.get("siguiente_analisis") or ""
    anomalias = conclusion.get("anomalias") or []
    nivel = conclusion.get("nivel_interes") or nivel_interes_conclusion(conclusion)
    lineas_concretas = extraer_lineas_concretas(texto)

    partes = [
        f"Hallazgo IA ({nivel})",
        f"{columna} · {estado}",
    ]
    if confianza is not None:
        partes.append(f"Confianza: {confianza}")

    if lineas_concretas:
        partes.append("Dato concreto:")
        partes.extend(f"- {normalizar_numeros_texto_telegram(linea)}" for linea in lineas_concretas)
    elif texto:
        partes.append(f"Dato concreto: {shorten_text(normalizar_numeros_texto_telegram(limpiar_texto_hallazgo(texto)), 260)}")

    if regla:
        partes.append(f"Regla tentativa: {shorten_text(normalizar_numeros_texto_telegram(limpiar_texto_hallazgo(regla)), 220)}")
    if anomalias:
        partes.append("Anomalia/atencion:")
        for item in anomalias[:2]:
            detalle = formatear_anomalia_telegram(item)
            partes.append(f"- {shorten_text(normalizar_numeros_texto_telegram(detalle), 220)}")
    partes.extend(lineas_evidencia_telegram(conclusion))
    if siguiente:
        partes.append(f"Siguiente: {shorten_text(siguiente, 180)}")
    return "\n".join(partes)


def notificar_telegram_conclusion(conclusion, origen="ia_local"):
    nivel = nivel_interes_conclusion(conclusion)
    conclusion["nivel_interes"] = nivel
    firma = firma_tematica_hallazgo(conclusion)
    conclusion["firma_tematica"] = firma
    if nivel == "baja" and not TELEGRAM_NOTIFY_LOW_INTEREST:
        add_reasoning_step(
            "telegram_filtrado",
            "No envie por Telegram una conclusion de baja prioridad.",
            shorten_text(conclusion.get("conclusion"), 180)
        )
        return {"ok": False, "filtered": True, "nivel_interes": nivel}
    if telegram_firma_repetida_reciente(firma):
        add_reasoning_step(
            "telegram_filtrado",
            "No envie por Telegram una conclusion repetida por tema.",
            firma
        )
        return {"ok": False, "filtered": True, "nivel_interes": nivel, "firma_tematica": firma}
    mensaje = formatear_conclusion_telegram(conclusion, origen=origen)
    resultado = enviar_telegram(mensaje)
    if resultado.get("ok"):
        TELEGRAM_SENT_HALLAZGO_FIRMAS.add(firma)
        add_reasoning_step(
            "telegram",
            "Envié una conclusión por Telegram.",
            conclusion.get("columna") or origen
        )
    elif not resultado.get("disabled"):
        add_reasoning_step(
            "telegram_error",
            "No pude enviar la conclusión por Telegram.",
            resultado.get("error") or str(resultado)
        )
    return resultado


def notificar_telegram_inicio():
    if not TELEGRAM_STARTUP_NOTIFY:
        return {"ok": False, "disabled": True}

    # Esperar hasta 30s a que tailscaled termine de levantar tras el reboot.
    # Si aparece una IP 100.x antes, salimos del loop inmediatamente.
    _inicio = time.time()
    _timeout = 30
    while True:
        access_ips = get_access_ips()
        tailscale_ips = access_ips["tailscale"]
        if tailscale_ips or (time.time() - _inicio) >= _timeout:
            break
        time.sleep(2)

    lan_ips = access_ips["lan"]
    ips = access_ips["all"]
    principal_ip = tailscale_ips[0] if tailscale_ips else (lan_ips[0] if lan_ips else "127.0.0.1")
    urls = [f"http://{principal_ip}:8080/ia"]
    if TELEGRAM_COMPACT_HEADERS:
        partes = [
            "Dashboard IA conectado",
            f"{OLLAMA_MODEL} | autonomia {'activa' if AUTO_IA_ENABLED else 'inactiva'}",
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if tailscale_ips:
            partes.append("Tailscale: " + ", ".join(tailscale_ips))
        if lan_ips:
            partes.append("LAN: " + ", ".join(lan_ips))
        partes.append("Dashboard: " + urls[0])
        if TELEGRAM_STARTUP_ACCESS_HINTS:
            partes.append(f"SSH: ssh plapopepo@{principal_ip}")
            partes.append(f"VNC Viewer: {principal_ip}:5900")
        mensaje = "\n".join(partes)
    else:
        mensaje = "\n".join([
            "Ecotecnica IA Local",
            "Dashboard IA conectado.",
            f"Modelo: {OLLAMA_MODEL}",
            f"Autonomia: {'activa' if AUTO_IA_ENABLED else 'inactiva'}",
            f"Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "Tailscale: " + (", ".join(tailscale_ips) if tailscale_ips else "no detectado"),
            "LAN: " + (", ".join(lan_ips) if lan_ips else "sin IP LAN detectada"),
            "URL: " + urls[0],
            f"SSH: ssh plapopepo@{principal_ip}",
            f"VNC Viewer: {principal_ip}:5900",
        ])
    resultado = enviar_telegram(mensaje)
    if resultado.get("ok"):
        add_reasoning_step(
            "telegram_inicio",
            "Avise por Telegram que el dashboard IA se conecto.",
            "startup"
        )
    elif not resultado.get("disabled"):
        add_reasoning_step(
            "telegram_inicio_error",
            "No pude enviar el aviso de inicio por Telegram.",
            resultado.get("error") or str(resultado)
        )
    return resultado


def notificar_telegram_inicio_async():
    t = threading.Thread(target=notificar_telegram_inicio, daemon=True)
    t.start()
    return t


_MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _nombre_mes_es(periodo):
    """'2026-07' -> 'julio 2026'; devuelve el periodo tal cual si no parsea."""
    try:
        p = str(periodo)
        return f"{_MESES_ES[int(p[5:7]) - 1]} {p[:4]}"
    except Exception:
        return str(periodo)


def _bio_proveedor_frase(nombre_prov, bio, kg_mes=None, periodo=None):
    """Frase corta determinística sobre un proveedor, kg primero.
    bio: fila normalizada de ia_biografia_protagonistas o None."""
    pre = f"{nombre_prov}: "
    if kg_mes is not None:
        pre += f"{int(kg_mes):,} kg" + (f" en {periodo}" if periodo else "") + "; "
    if not bio or int(bio.get("meses_con_actividad") or 0) == 0:
        return pre + "sin historial unido todavía"
    n = int(bio.get("meses_con_actividad") or 0)
    kg_prom = float(bio.get("kg_promedio_meses_activos") or 0)
    etiqueta = {
        "fuerte": "habitual fuerte",
        "media": "habitual",
        "debil": "historial débil",
        "antecedente_unico": "un solo mes previo",
    }.get(bio.get("confiabilidad"), "historial débil")
    return pre + f"historial unido: {n} meses activos ({etiqueta}); promedio histórico {int(kg_prom):,} kg/mes"


def _mapa_alias_proveedores_liviano():
    """Mapa de alias de proveedores con conexión propia; {} si no se puede leer.
    Si la tabla no existe todavía (p.ej. antes del primer recálculo de
    derivadas), la crea con su seed y reintenta."""
    try:
        conn = get_ia_connection()
        if not conn:
            return {}
        cur = conn.cursor()
        mapa, _ = cargar_alias_proveedores(cur)
        if not mapa:
            try:
                _ensure_alias_proveedores(cur)
                conn.commit()
                mapa, _ = cargar_alias_proveedores(cur)
            except Exception:
                pass
        cur.close()
        conn.close()
        return mapa
    except Exception:
        return {}


def _maquina_es_habitual_normalidad(maquina):
    """True/False según normalidad mensual de la máquina; None si no se puede saber."""
    try:
        conn = get_ia_connection()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute("""
            SELECT meses_con_actividad
            FROM ia_normalidad_mensual_produccion_maquina
            WHERE entidad = %s AND criterio_version = %s
            LIMIT 1
        """, (maquina, DERIVADAS_VERSION))
        fila = cur.fetchone()
        cur.close()
        conn.close()
        if not fila:
            return False
        return int(fila.get("meses_con_actividad") or 0) >= 3
    except Exception:
        return None


def construir_mensaje_telegram_inteligente(tipo_microtarea, evidencia, cerebro):
    """Arma un mensaje Telegram masticado y accionable para las microtareas de
    estado (produccion/ingresos del mes actual). Devuelve None si el mensaje
    solo podría decir total/vs esperado (nada enriquecible con protagonistas,
    biografías o normalidad)."""
    evidencia = evidencia or {}
    cerebro = cerebro or {}
    periodo = evidencia.get("periodo") or "?"
    total_kg = float(evidencia.get("total_kg") or 0)
    avance_pct = evidencia.get("avance_mes_pct")
    vs_esp = evidencia.get("actual_vs_esperado_pct")

    es_ingresos = "ingresos" in str(tipo_microtarea)
    es_produccion = "produccion" in str(tipo_microtarea)
    if not (es_ingresos or es_produccion):
        return None

    # Encabezado kg-primero: los kilos absolutos lideran; vs esperado se omite
    # (el % de participación y las proyecciones son datos secundarios).
    titulo = "📦 Ingresos" if es_ingresos else "🏭 Producción"
    encabezado = f"{titulo} {_nombre_mes_es(periodo)}: {int(total_kg):,} kg registrados"
    extras = []
    if str(evidencia.get("estado_periodo") or "") == "abierto":
        extras.append("periodo abierto")
    if avance_pct is not None:
        extras.append(f"{avance_pct}% del mes")
    if extras:
        encabezado += " (" + ", ".join(extras) + ")"

    senales = []   # avisos prioritarios (⚠️), en orden de prioridad
    lineas = []    # contexto masticado

    if es_ingresos:
        top = evidencia.get("top_proveedores") or []
        alias_tg = _mapa_alias_proveedores_liviano()
        bios = {}
        for b in ((cerebro.get("biografias_protagonistas") or {}).get("proveedores") or []):
            nombre_b = canonizar_proveedor(b.get("proveedor") or b.get("entidad"), alias_tg)
            if nombre_b:
                bios[nombre_b] = b

        # Canonizar el top por alias y re-agrupar (Sandro mengar -> Sandro Melgar)
        kg_por_canon = {}
        for t in top:
            prov = canonizar_proveedor(t.get("Proveedor"), alias_tg) or "?"
            kg_por_canon[prov] = kg_por_canon.get(prov, 0.0) + float(t.get("kilos_total") or 0)
        items = []
        for prov in sorted(kg_por_canon, key=lambda p: -kg_por_canon[p])[:3]:
            kg = kg_por_canon[prov]
            pct = round(kg / total_kg * 100, 1) if total_kg > 0 else 0
            items.append({"proveedor": prov, "kg": kg, "pct": pct})
        if items:
            # kg como magnitud principal, sin % destacado
            lineas.append("Principales kilos: " + "; ".join(
                f"{i['proveedor']} {int(i['kg']):,} kg" for i in items))

        avance_frac = float(avance_pct or 100) / 100.0
        frases_bio = []
        for i in items:
            bio = bios.get(i["proveedor"])
            sin_historia = (not bio) or int(bio.get("meses_con_actividad") or 0) == 0
            # Señal prioritaria 1 (kg primero): volumen relevante sin historial unido
            if sin_historia and i["kg"] >= TELEGRAM_ALERTA_KG_MINIMO:
                senal = f"⚠️ {i['proveedor']}: {int(i['kg']):,} kg registrados; sin historial unido todavía"
                if i is items[0]:
                    # Señal prioritaria 3: ranking inusual (líder del mes sin historia)
                    senal += " y lidera el mes"
                senales.append(senal)
                continue
            # Señal prioritaria 2: cambio fuerte vs histórico EN KG
            # (comparado contra el ritmo esperado según el avance del mes)
            if bio:
                kg_prom = float(bio.get("kg_promedio_meses_activos") or 0)
                n_meses = int(bio.get("meses_con_actividad") or 0)
                kg_ritmo = kg_prom * (avance_frac if avance_frac > 0 else 1)
                if kg_ritmo > 0 and (i["kg"] >= kg_ritmo * 1.5 or i["kg"] <= kg_ritmo * 0.5):
                    direccion = "muy por encima" if i["kg"] >= kg_ritmo * 1.5 else "muy por debajo"
                    senales.append(
                        f"⚠️ {i['proveedor']}: {int(i['kg']):,} kg en {periodo}; {direccion} de su "
                        f"ritmo histórico (promedio {int(kg_prom):,} kg/mes en {n_meses} meses)"
                    )
                    continue
            frases_bio.append(_bio_proveedor_frase(i["proveedor"], bio, kg_mes=i["kg"], periodo=periodo))
        if frases_bio:
            lineas.append("; ".join(frases_bio))

    else:  # producción
        top_maq = evidencia.get("top_maquinas") or []
        if top_maq:
            lider = top_maq[0]
            maquina = lider.get("Maquina") or "?"
            kg_maq = float(lider.get("kilos_total") or 0)
            pct_maq = round(kg_maq / total_kg * 100, 1) if total_kg > 0 else 0
            linea = f"Máquina líder: {maquina} ({pct_maq}% del total, {int(kg_maq):,} kg)"
            habitual = _maquina_es_habitual_normalidad(maquina)
            if habitual is True:
                linea += ", habitual según su normalidad"
            elif habitual is False:
                # Señal prioritaria: líder sin normalidad previa = ranking inusual
                senales.append(f"⚠️ {maquina} lidera producción sin normalidad previa (máquina nueva o poco vista)")
            lineas.append(linea)

    # Señal prioritaria 4: ficha operativa interesante del periodo
    try:
        for f in ((cerebro.get("fichas_operativas") or {}).get("recientes") or [])[:3]:
            sen = f.get("senales_json") or {}
            if f.get("periodo") == periodo and (
                sen.get("monoproveedor") or sen.get("dominante_concentra_mas_50pct")
                or sen.get("es_lider_mes") or not sen.get("tiene_biografia", True)
            ):
                lineas.append(f"Ficha: {f.get('titulo')}")
                break
    except Exception:
        pass

    # Sin protagonistas, biografías ni normalidad: el mensaje sería solo
    # total/vs esperado -> no accionable, no enviar.
    if not senales and not lineas:
        return None

    return "\n".join([encabezado] + senales + lineas)


_TELEGRAM_REPORTES_PREVIOS = {}


def _firma_reporte_operativo(nombre, periodo, evidencia, mensaje):
    """Firma del reporte operativo para dedupe: tipo + periodo + top 3 de
    proveedores/máquinas + diagnósticos principales (⚠️) sin números.
    Devuelve (firma, total_kg)."""
    ev = evidencia or {}
    top = ev.get("top_proveedores") or ev.get("top_maquinas") or []
    nombres_top = tuple(
        str(t.get("Proveedor") or t.get("Maquina") or "?") for t in top[:3]
    )
    diagnosticos = tuple(sorted(
        re.sub(r"[\d.,:%()+\-]", "", ln).strip()
        for ln in str(mensaje or "").split("\n") if ln.startswith("⚠️")
    ))
    firma = (str(nombre), str(periodo), nombres_top, diagnosticos)
    return firma, float(ev.get("total_kg") or 0)


def notificar_telegram_microtarea(nombre, periodo, avance_pct, total_kg,
                                   vs_esperado_pct, confianza, conclusion_txt,
                                   evidencia=None, cerebro=None):
    """Envía notificación Telegram para observación de microtarea si califica.
    Regla: (abs(vs_esperado_pct) >= 25 o confianza >= 0.75) Y además el mensaje
    se puede enriquecer con protagonistas/biografía/normalidad; un mensaje que
    solo diría total/vs esperado no se envía.
    Registra reasoning_step tanto si filtra como si envía o falla."""
    califica = (abs(vs_esperado_pct or 0) >= 25) or (confianza >= 0.75)
    if not califica:
        add_reasoning_step(
            "telegram_microtarea_filtrado",
            f"{nombre}: no notifica (vs_esp={vs_esperado_pct} conf={confianza})",
            None
        )
        return

    mensaje = construir_mensaje_telegram_inteligente(nombre, evidencia, cerebro)
    if mensaje is None:
        add_reasoning_step(
            "telegram_microtarea_filtrado",
            f"{nombre}: no notifica",
            "sin lectura accionable"
        )
        return

    # Dedupe: no reenviar si el reporte es esencialmente igual al anterior
    # (mismo top 3 y diagnósticos, y kilos sin cambio material). Cambios de
    # avance_mes_pct / vs_esperado por paso del tiempo no cuentan como cambio.
    firma, kg_reporte = _firma_reporte_operativo(nombre, periodo, evidencia, mensaje)
    previo = _TELEGRAM_REPORTES_PREVIOS.get((nombre, periodo))
    if previo and previo.get("firma") == firma:
        delta_kg = abs(kg_reporte - float(previo.get("total_kg") or 0))
        pct_delta = (delta_kg / previo["total_kg"] * 100) if previo.get("total_kg") else 100.0
        if delta_kg < TELEGRAM_DEDUPE_KG_DELTA and pct_delta < TELEGRAM_DEDUPE_PCT_DELTA:
            add_reasoning_step(
                "telegram_dedupe_skip",
                f"{nombre}: reporte equivalente al anterior; no reenvío a Telegram.",
                f"periodo={periodo} delta_kg={delta_kg:,.0f} ({pct_delta:.1f}%); mismo top y diagnósticos"
            )
            return
    _TELEGRAM_REPORTES_PREVIOS[(nombre, periodo)] = {"firma": firma, "total_kg": kg_reporte}

    def _enviar():
        try:
            resultado = enviar_telegram(mensaje)
            if resultado and resultado.get("ok"):
                add_reasoning_step(
                    "telegram_microtarea_enviado",
                    f"{nombre}: Telegram enviado",
                    f"periodo={periodo} vs_esp={vs_esperado_pct} conf={confianza}"
                )
            else:
                add_reasoning_step(
                    "telegram_microtarea_error",
                    f"{nombre}: Telegram falló",
                    str(resultado)[:200] if resultado else "sin respuesta"
                )
        except Exception as ex:
            add_reasoning_step(
                "telegram_microtarea_error",
                f"{nombre}: excepción al enviar Telegram",
                str(ex)[:200]
            )

    threading.Thread(target=_enviar, daemon=True).start()


def get_column_evidence(columna):
    if not columna:
        return None

    conn = get_db_connection()
    if not conn:
        return {"error": "No se pudo conectar a sh_etp_v2_local"}

    try:
        cur = conn.cursor()
        columnas = get_table_columns(cur, "TiposDeMovimiento")
        if columna not in columnas:
            return {"error": f"La columna {columna} no existe en TiposDeMovimiento"}

        quoted = "`" + columna.replace("`", "``") + "`"
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN {quoted} IS NULL OR {quoted} = '' THEN 1 ELSE 0 END) AS vacios,
                COUNT(DISTINCT {quoted}) AS distintos
            FROM TiposDeMovimiento
        """)
        resumen = cur.fetchone() or {}

        cur.execute(f"""
            SELECT {quoted} AS valor, COUNT(*) AS cantidad
            FROM TiposDeMovimiento
            WHERE {quoted} IS NOT NULL
              AND {quoted} <> ''
            GROUP BY {quoted}
            ORDER BY cantidad DESC
            LIMIT 8
        """)
        frecuentes = cur.fetchall()

        cur.execute(f"""
            SELECT
                TiposDeMovimiento AS tipo_movimiento,
                CASE WHEN {quoted} IS NULL OR {quoted} = '' THEN 'vacio' ELSE 'con_valor' END AS estado_columna,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 2) AS kilos
            FROM TiposDeMovimiento
            GROUP BY TiposDeMovimiento, estado_columna
            ORDER BY registros DESC
            LIMIT 20
        """)
        contexto_movimiento = cur.fetchall()

        cur.execute(f"""
            SELECT
                TiposDeMovimiento AS tipo_movimiento,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 2) AS kilos,
                ROUND(AVG(CASE WHEN {quoted} IS NULL OR {quoted} = '' THEN 1 ELSE 0 END) * 100, 1) AS pct_vacio
            FROM TiposDeMovimiento
            GROUP BY TiposDeMovimiento
            ORDER BY registros DESC
            LIMIT 12
        """)
        resumen_por_movimiento = cur.fetchall()

        cur.close()
        conn.close()
        return {
            "total": resumen.get("total") or 0,
            "vacios": resumen.get("vacios") or 0,
            "distintos": resumen.get("distintos") or 0,
            "frecuentes": frecuentes,
            "contexto_movimiento": contexto_movimiento,
            "resumen_por_movimiento": resumen_por_movimiento,
        }

    except Exception as e:
        print(f"Error en get_column_evidence: {e}", file=sys.stderr)
        return {"error": str(e)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def parse_model_json(text):
    if not text:
        return None
    raw = text.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            return None
    return None


def extraer_json_modelo(text):
    """Devuelve {ok,data,error,raw,fragment}; no inventa JSON si esta incompleto."""
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "data": None, "error": "respuesta_vacia", "raw": raw, "fragment": ""}
    try:
        return {"ok": True, "data": json.loads(raw), "error": None, "raw": raw, "fragment": raw}
    except Exception as e_full:
        full_error = str(e_full)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        fragment = raw[start:end + 1]
        try:
            return {"ok": True, "data": json.loads(fragment), "error": None, "raw": raw, "fragment": fragment}
        except Exception as e_fragment:
            return {"ok": False, "data": None, "error": f"json_fragmento_invalido: {e_fragment}", "raw": raw, "fragment": fragment}
    if start >= 0 and end < start:
        return {"ok": False, "data": None, "error": "json_incompleto_sin_llave_cierre", "raw": raw, "fragment": raw[start:]}
    return {"ok": False, "data": None, "error": f"sin_objeto_json: {full_error}", "raw": raw, "fragment": ""}


def extraer_decision_parcial_estructural(text):
    """Recupera una conclusion util cuando Ollama corta un JSON casi valido."""
    raw = str(text or "").strip()
    if not raw:
        return None

    def _campo_texto(*nombres):
        for nombre in nombres:
            m = re.search(
                rf'"{nombre}"\s*:\s*"(?P<v>(?:\\.|[^"\\])*)',
                raw,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m:
                try:
                    return m.group("v").encode("utf-8", "ignore").decode("unicode_escape", "ignore").strip()
                except Exception:
                    return m.group("v").strip()
        return None

    conclusion = _campo_texto("conclusion", "conclusion_principal", "interpretacion", "resumen", "hipotesis_nueva", "hipotesis")
    if not conclusion:
        # Fallback texto plano: usar la primera frase razonable si el modelo obedecio a medias.
        limpio = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
        limpio = re.sub(r'^\s*\{?\s*"?(action|conclusion|confianza|evidencia)"?\s*:?', "", limpio, flags=re.IGNORECASE).strip()
        conclusion = shorten_text(limpio, 260) if len(limpio) >= 30 else None
    if not conclusion:
        return None

    conf = None
    m_conf = re.search(r'"confianza"\s*:\s*(?P<v>0(?:\.\d+)?|1(?:\.0+)?)', raw, flags=re.IGNORECASE)
    if m_conf:
        conf = confianza_float(m_conf.group("v"))
    if conf is None:
        conf = 0.58
    conf = max(0.50, min(0.62, conf))
    return {
        "action": "conclude",
        "conclusion": shorten_text(conclusion, 320),
        "confianza": conf,
        "parse_parcial": True,
        "evidencia": "recuperado de JSON parcial del modelo",
    }


def texto_modelo_util_no_json(text):
    t = str(text or "").strip()
    if len(t) < 20:
        return False
    pistas = ["hipotes", "conclusion", "razon", "anom", "error", "falt", "precio", "proveedor", "maquina", "descuento", "produccion"]
    tl = t.lower()
    return any(p in tl for p in pistas)




def normalizar_interpretacion_ia(data):
    if not isinstance(data, dict):
        return None
    return {
        "interpretacion": shorten_text(data.get("interpretacion") or "", 320),
        "hipotesis_refinada": shorten_text(data.get("hipotesis_refinada") or "", 320),
        "conclusion_autonoma": shorten_text(data.get("conclusion_autonoma") or "", 420),
        "preguntas_mejores": [
            shorten_text(x, 180)
            for x in (data.get("preguntas_mejores") or [])
            if x
        ][:3],
        "regla_aprendible": shorten_text(data.get("regla_aprendible") or "", 320),
        "incertidumbres": [
            shorten_text(x, 180)
            for x in (data.get("incertidumbres") or [])
            if x
        ][:3],
        "siguiente_analisis": shorten_text(data.get("siguiente_analisis") or "", 260),
        "confianza_sugerida": data.get("confianza_sugerida"),
    }


def llamar_ollama(prompt, model=OLLAMA_MODEL, timeout=180, num_predict=700):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_ctx": 4096,
            "num_predict": num_predict,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            return {
                "ok": True,
                "model": out.get("model") or model,
                "texto": out.get("response", ""),
                "total_duration": out.get("total_duration"),
            }
    except Exception as e:
        return {
            "ok": False,
            "model": model,
            "error": str(e),
        }


def confianza_float(value):
    try:
        return float(value)
    except Exception:
        return None


def primer_texto(*valores):
    for valor in valores:
        if valor is None:
            continue
        if isinstance(valor, str):
            texto = valor.strip()
        else:
            texto = str(valor).strip()
        if texto:
            return texto
    return None


def texto_tiene_senal_operativa_util(texto):
    texto_l = str(texto or "").lower()
    senales = [
        "vs", "compar", "temporal", "hoy", "ayer", "semana", "mes", "cambio", "aumento", "baja", "dismin",
        "anom", "error", "falt", "ausen", "nulo", "null", "sin proveedor", "sin operador", "sin maquina",
        "precio", "material", "descuento", "kilosnetos", "kilos netos", "contradic", "inconsisten",
        "relacion", "co-apare", "coapare", "concentr", "casi no aparece", "aparece principalmente",
        "hipotesis", "requiere investigacion", "fuera de", "extremo",
    ]
    return any(x in texto_l for x in senales)


def resultado_fase2_es_ranking_generico(resultado):
    if not isinstance(resultado, dict):
        return True
    partes = [
        resultado.get("conclusion_principal"), resultado.get("conclusion"), resultado.get("conclusion_autonoma"),
        resultado.get("interpretacion"), resultado.get("resumen"), resultado.get("regla_aprendida"),
        resultado.get("regla_aprendible"), resultado.get("hipotesis_nueva"), resultado.get("hipotesis_refinada"), resultado.get("hipotesis"),
    ]
    texto = " ".join(str(x or "") for x in partes).lower()
    ranking = any(x in texto for x in [
        "ranking", "top ", "principales", "mÃ¡s frecuente", "mas frecuente", "mayor cantidad",
        "mayor numero", "mayor nÃºmero", "mas activos", "mÃ¡s activos", "mueve mucho",
        "maquinas mas activas", "mÃ¡quinas mÃ¡s activas", "proveedores mas activos", "proveedores mÃ¡s activos",
    ])
    if not ranking:
        return False
    return not texto_tiene_senal_operativa_util(texto)


def resultado_fase2_tiene_conocimiento(resultado):
    if not isinstance(resultado, dict):
        return False
    action = (resultado.get("action") or "").strip().lower()
    if action == "query":
        return False
    texto = primer_texto(
        resultado.get("conclusion_principal"),
        resultado.get("conclusion"),
        resultado.get("conclusion_autonoma"),
        resultado.get("interpretacion"),
        resultado.get("resumen"),
    )
    anomalias = resultado.get("anomalias") or []
    patrones = resultado.get("patrones") or resultado.get("patrones_detectados") or []
    if resultado_fase2_es_ranking_generico(resultado):
        return False
    if texto and texto_sin_hallazgo_operativo(texto) and not anomalias and not patrones:
        return False
    if texto and not texto_tiene_senal_operativa_util(texto) and not anomalias:
        hip_o_regla = primer_texto(
            resultado.get("hipotesis_nueva"), resultado.get("hipotesis_refinada"), resultado.get("hipotesis"),
            resultado.get("regla_aprendida"), resultado.get("regla_aprendible"), resultado.get("regla"),
        )
        if not hip_o_regla or not texto_tiene_senal_operativa_util(hip_o_regla):
            return False
    if primer_texto(
        texto,
        resultado.get("hipotesis_nueva"),
        resultado.get("hipotesis_refinada"),
        resultado.get("hipotesis"),
        resultado.get("regla_aprendida"),
        resultado.get("regla_aprendible"),
        resultado.get("regla"),
    ):
        return True
    return bool(patrones or anomalias)


def ensure_ia_hipotesis_autonomas(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_hipotesis_autonomas (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            origen VARCHAR(128) NOT NULL DEFAULT 'explorador_fase2',
            tabla_nombre VARCHAR(128) NOT NULL DEFAULT 'TiposDeMovimiento',
            columna VARCHAR(128) NULL,
            hipotesis TEXT NOT NULL,
            pregunta TEXT NULL,
            evidencia_json JSON NULL,
            estado VARCHAR(64) DEFAULT 'abierta',
            confianza DECIMAL(5,3) NULL,
            modelo VARCHAR(128) NULL,
            creada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            actualizada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_estado (estado),
            KEY idx_creada (creada_en)
        ) CHARACTER SET utf8mb4
    """)


def persistir_hipotesis_autonoma(resultado, evidencia=None, origen="explorador_fase2"):
    hipotesis = primer_texto(
        resultado.get("hipotesis_nueva") if isinstance(resultado, dict) else None,
        resultado.get("hipotesis_refinada") if isinstance(resultado, dict) else None,
        resultado.get("hipotesis") if isinstance(resultado, dict) else None,
    )
    if not hipotesis:
        return {"ok": False, "skipped": "sin_hipotesis"}

    pregunta = (resultado.get("pregunta_para_diego") or "") if isinstance(resultado, dict) else ""
    estado = "requiere_contexto" if pregunta else "abierta"
    confianza = confianza_float(resultado.get("confianza") if isinstance(resultado, dict) else None)
    columna = (resultado.get("columna") or "operativo_planta") if isinstance(resultado, dict) else "operativo_planta"

    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_lab", "hipotesis": hipotesis}
    try:
        cur = conn.cursor()
        ensure_ia_hipotesis_autonomas(cur)
        evidencia_payload = evidencia or {}
        cur.execute("""
            SELECT id
            FROM ia_hipotesis_autonomas
            WHERE origen = %s
              AND tabla_nombre = %s
              AND COALESCE(columna, '') = COALESCE(%s, '')
              AND hipotesis = %s
            LIMIT 1
        """, (origen, "TiposDeMovimiento", columna, hipotesis))
        existente = cur.fetchone() or {}
        evidencia_json = json.dumps(evidencia_payload, ensure_ascii=False, default=str)
        if existente.get("id"):
            hip_id = existente.get("id")
            duplicada = True
            cur.execute("""
                UPDATE ia_hipotesis_autonomas
                SET pregunta = COALESCE(NULLIF(%s, ''), pregunta),
                    evidencia_json = %s,
                    confianza = COALESCE(%s, confianza),
                    modelo = %s,
                    actualizada_en = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (pregunta, evidencia_json, confianza, OLLAMA_MODEL, hip_id))
        else:
            cur.execute("""
                INSERT INTO ia_hipotesis_autonomas (
                    origen, tabla_nombre, columna, hipotesis, pregunta,
                    evidencia_json, estado, confianza, modelo
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                origen,
                "TiposDeMovimiento",
                columna,
                hipotesis,
                pregunta,
                evidencia_json,
                estado,
                confianza,
                OLLAMA_MODEL,
            ))
            hip_id = cur.lastrowid
            duplicada = False
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True, "id": hip_id, "duplicada": duplicada, "estado": estado, "hipotesis": hipotesis}
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e), "hipotesis": hipotesis}


def ensure_autonomous_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_conclusiones_autonomas (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            hipotesis_id BIGINT NULL,
            columna VARCHAR(255) NULL,
            conclusion TEXT,
            hipotesis_refinada TEXT,
            regla_aprendible TEXT,
            confianza DECIMAL(5,3) NULL,
            estado VARCHAR(64) DEFAULT 'provisional_autonoma',
            modelo VARCHAR(128) NULL,
            evidencia_json JSON NULL,
            salida_json JSON NULL,
            creada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4
    """)


def _insert_conclusion_autonoma(cur, data_dict):
    """INSERT dinamico en ia_conclusiones_autonomas segun columnas reales."""
    real_cols = get_table_columns(cur, "ia_conclusiones_autonomas")

    def json_default(obj):
        if isinstance(obj, (datetime,)):
            return obj.isoformat(sep=" ")
        if isinstance(obj, Decimal):
            return float(obj)
        return str(obj)

    candidates = {
        "hipotesis_id":       data_dict.get("hipotesis_id"),
        "columna":            data_dict.get("columna"),
        "tabla_nombre":       data_dict.get("tabla_nombre", "TiposDeMovimiento"),
        "conclusion":         data_dict.get("conclusion"),
        "hipotesis_refinada": data_dict.get("hipotesis_refinada"),
        "regla_aprendible":   data_dict.get("regla_aprendible"),
        "confianza":          confianza_float(data_dict.get("confianza")),
        "estado":             data_dict.get("estado", "provisional_autonoma"),
        "modelo":             OLLAMA_MODEL,
        "evidencia_json":     json.dumps(data_dict.get("evidencia"), ensure_ascii=False, default=json_default) if data_dict.get("evidencia") is not None else None,
        "salida_json":        json.dumps(data_dict.get("salida_json"), ensure_ascii=False, default=json_default) if data_dict.get("salida_json") is not None else None,
    }
    filtered = {k: v for k, v in candidates.items() if not real_cols or k in real_cols}
    if not filtered:
        return
    cols = ", ".join(filtered.keys())
    placeholders = ", ".join(["%s"] * len(filtered))
    cur.execute(
        f"INSERT INTO ia_conclusiones_autonomas ({cols}) VALUES ({placeholders})",
        list(filtered.values())
    )


def persistir_conclusion_autonoma(conclusion, interpretacion):
    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_lab"}

    try:
        cur = conn.cursor()
        try:
            ensure_autonomous_tables(cur)
        except Exception:
            pass
        _insert_conclusion_autonoma(cur, {
            "hipotesis_id":     conclusion.get("hipotesis_id"),
            "columna":          conclusion.get("columna"),
            "conclusion":       conclusion.get("conclusion"),
            "hipotesis_refinada": conclusion.get("hipotesis_refinada"),
            "regla_aprendible": conclusion.get("regla_aprendible"),
            "confianza":        conclusion.get("confianza"),
            "estado":           conclusion.get("estado"),
            "evidencia":        conclusion.get("evidencia"),
            "salida_json":      interpretacion,
        })
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True}
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def marcar_hipotesis_autonoma(hipotesis_id, confianza):
    if not hipotesis_id:
        return False
    conf = confianza_float(confianza)
    if conf is None or conf < AUTO_IA_MIN_CONFIDENCE:
        return False

    conn = get_ia_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        hip_cols = get_table_columns(cur, "hipotesis_esquema")
        updates = ["estado = 'autonoma_provisional'"]
        if "vigente" in hip_cols:
            updates.append("vigente = 0")
        if "actualizada_en" in hip_cols:
            updates.append("actualizada_en = CURRENT_TIMESTAMP")
        elif "actualizado_en" in hip_cols:
            updates.append("actualizado_en = CURRENT_TIMESTAMP")

        cur.execute(f"""
            UPDATE hipotesis_esquema
            SET {", ".join(updates)}
            WHERE id = %s
        """, (hipotesis_id,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error marcando hipotesis autonoma: {e}", file=sys.stderr)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def guardar_conclusion_autonoma(columna, interpretacion, evidencia, hipotesis_id=None):
    frecuentes = evidencia.get("frecuentes") if evidencia else []
    conclusion = {
        "fecha": datetime.now().isoformat(),
        "hora": datetime.now().strftime("%H:%M:%S"),
        "hipotesis_id": hipotesis_id,
        "columna": columna,
        "estado": "provisional_autonoma",
        "confianza": interpretacion.get("confianza_sugerida"),
        "conclusion": interpretacion.get("conclusion_autonoma") or interpretacion.get("hipotesis_refinada") or interpretacion.get("interpretacion"),
        "hipotesis_refinada": interpretacion.get("hipotesis_refinada"),
        "regla_aprendible": interpretacion.get("regla_aprendible"),
        "incertidumbres": interpretacion.get("incertidumbres") or [],
        "siguiente_analisis": interpretacion.get("siguiente_analisis"),
        "evidencia": {
            "total": (evidencia or {}).get("total"),
            "vacios": (evidencia or {}).get("vacios"),
            "distintos": (evidencia or {}).get("distintos"),
            "frecuentes": frecuentes[:5],
            "contexto_movimiento": (evidencia or {}).get("contexto_movimiento", [])[:10],
            "resumen_por_movimiento": (evidencia or {}).get("resumen_por_movimiento", [])[:8],
        },
    }
    persistencia = persistir_conclusion_autonoma(conclusion, interpretacion)
    conclusion["persistencia"] = persistencia
    conclusion["avance_autonomo"] = marcar_hipotesis_autonoma(
        hipotesis_id,
        conclusion.get("confianza")
    )
    conclusion["telegram"] = notificar_telegram_conclusion(conclusion, origen="hipotesis_columna")
    IA_AUTONOMOUS_CONCLUSIONS.insert(0, conclusion)
    del IA_AUTONOMOUS_CONCLUSIONS[40:]
    return conclusion


def construir_prompt_ollama(proxima, evidencia, conocimientos):
    frecuentes = evidencia.get("frecuentes") if evidencia else []
    frecuentes_txt = "\n".join([
        f"- {x.get('valor')}: {x.get('cantidad')} registros"
        for x in (frecuentes or [])[:5]
    ])
    contexto_txt = "\n".join([
        f"- {x.get('tipo_movimiento')} / {x.get('estado_columna')}: {x.get('registros')} registros, {x.get('kilos')} kg"
        for x in ((evidencia or {}).get("contexto_movimiento") or [])[:8]
    ])
    resumen_mov_txt = "\n".join([
        f"- {x.get('tipo_movimiento')}: {x.get('registros')} registros, {x.get('kilos')} kg, {x.get('pct_vacio')}% vacio"
        for x in ((evidencia or {}).get("resumen_por_movimiento") or [])[:6]
    ])
    conocimientos_txt = "\n".join([
        f"- {x.get('tema')}: {shorten_text(x.get('texto'), 140)}"
        for x in (conocimientos or [])[:3]
    ]) or "- Sin conocimiento confirmado relevante."

    return f"""
Sos una IA industrial local de Ecotecnica. Tu tarea es interpretar evidencia SQL y ayudar a construir conocimiento operativo.

Respondé SOLO JSON válido, sin markdown, con estas claves:
{{
  "interpretacion": "texto breve",
  "hipotesis_refinada": "texto breve",
  "conclusion_autonoma": "conclusion provisional propia con confianza",
  "preguntas_mejores": ["pregunta 1", "pregunta 2"],
  "regla_aprendible": "regla operativa tentativa",
  "incertidumbres": ["limite o duda 1", "limite o duda 2"],
  "siguiente_analisis": "proximo cruce o dato a investigar",
  "confianza_sugerida": 0.0
}}

Columna: {proxima.get("columna")}
Hipótesis actual: {proxima.get("hipotesis")}
Pregunta actual: {proxima.get("pregunta")}

Evidencia SQL:
- total registros: {(evidencia or {}).get("total")}
- vacíos: {(evidencia or {}).get("vacios")}
- valores distintos: {(evidencia or {}).get("distintos")}
- valores frecuentes:
{frecuentes_txt or "- Sin valores frecuentes."}

Cruce con TiposDeMovimiento:
{contexto_txt or "- Sin cruce disponible."}

Resumen por tipo de movimiento:
{resumen_mov_txt or "- Sin resumen disponible."}

Conocimiento operativo ya confirmado:
{conocimientos_txt}

No inventes certezas. Si la evidencia no alcanza, indicá qué falta preguntarle a Diego.
""".strip()


def get_evidencia_operativa():
    """Recopila evidencia operativa real de TiposDeMovimiento para análisis de fase 2."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()

        # Producción y recepción de los últimos 7 días por día
        cur.execute("""
            SELECT
                DATE(FechaHora) AS dia,
                TiposDeMovimiento AS tipo,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 1) AS kilos
            FROM TiposDeMovimiento
            WHERE FechaHora >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY dia, tipo
            ORDER BY dia DESC, kilos DESC
            LIMIT 40
        """)
        ultimos_7_dias = cur.fetchall()

        # Top proveedores últimos 7 días por kilos
        cur.execute("""
            SELECT
                Proveedor,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 1) AS kilos,
                COUNT(DISTINCT DATE(FechaHora)) AS dias_activo
            FROM TiposDeMovimiento
            WHERE FechaHora >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
              AND TiposDeMovimiento IN ('Recepcion','Ingreso')
              AND Proveedor IS NOT NULL AND Proveedor <> ''
            GROUP BY Proveedor
            ORDER BY kilos DESC
            LIMIT 10
        """)
        top_proveedores = cur.fetchall()

        # Top máquinas por producción últimos 7 días
        cur.execute("""
            SELECT
                Maquina,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 1) AS kilos
            FROM TiposDeMovimiento
            WHERE FechaHora >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
              AND TiposDeMovimiento IN ('Produccion','Proceso')
              AND Maquina IS NOT NULL AND Maquina <> ''
            GROUP BY Maquina
            ORDER BY kilos DESC
            LIMIT 8
        """)
        top_maquinas = cur.fetchall()

        # Distribución de TiposDeMovimiento histórica
        cur.execute("""
            SELECT
                TiposDeMovimiento AS tipo,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 1) AS kilos,
                COUNT(DISTINCT Proveedor) AS proveedores_distintos
            FROM TiposDeMovimiento
            GROUP BY TiposDeMovimiento
            ORDER BY kilos DESC
        """)
        dist_tipos = cur.fetchall()

        # ERROR_REGISTRO últimas 2 semanas (ritmo)
        cur.execute("""
            SELECT
                DATE(FechaHora) AS dia,
                COUNT(*) AS cantidad,
                ROUND(SUM(kilos), 1) AS kilos
            FROM TiposDeMovimiento
            WHERE Description = 'ERROR_REGISTRO'
              AND FechaHora >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
            GROUP BY dia
            ORDER BY dia DESC
        """)
        errores_recientes = cur.fetchall()

        # Descuentos: magnitud promedio
        cur.execute("""
            SELECT
                TiposDeMovimiento AS tipo,
                ROUND(AVG(Descuento), 2) AS desc_promedio,
                ROUND(AVG(Descuento2), 2) AS desc2_promedio,
                ROUND(MAX(kilos), 1) AS max_kilos,
                ROUND(MIN(kilos), 1) AS min_kilos
            FROM TiposDeMovimiento
            WHERE FechaHora >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY TiposDeMovimiento
            ORDER BY desc_promedio DESC
        """)
        descuentos = cur.fetchall()

        # Horas del día con más actividad
        cur.execute("""
            SELECT
                HOUR(FechaHora) AS hora,
                COUNT(*) AS registros,
                ROUND(SUM(kilos), 1) AS kilos
            FROM TiposDeMovimiento
            WHERE FechaHora >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY hora
            ORDER BY registros DESC
            LIMIT 8
        """)
        horas_pico = cur.fetchall()

        cur.close()
        conn.close()
        return {
            "ultimos_7_dias": ultimos_7_dias,
            "top_proveedores": top_proveedores,
            "top_maquinas": top_maquinas,
            "dist_tipos": dist_tipos,
            "errores_recientes": errores_recientes,
            "descuentos": descuentos,
            "horas_pico": horas_pico,
        }
    except Exception as e:
        print(f"Error en get_evidencia_operativa: {e}", file=sys.stderr)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Focos rotativos — cada ciclo analiza un ángulo distinto
FASE2_FOCOS = [
    {
        "nombre": "maquinas y eficiencia",
        "instruccion": "Analizá específicamente el rendimiento de cada máquina. ¿Cuál procesa más kilos? ¿Hay diferencias entre días? ¿Alguna parece subutilizada?",
    },
    {
        "nombre": "errores de registro",
        "instruccion": "Enfocate en los ERROR_REGISTRO. ¿Cuándo ocurren? ¿Hay patrón por día u hora? ¿Cuántos kilos representan? ¿Es preocupante?",
    },
    {
        "nombre": "distribucion de movimientos",
        "instruccion": "Analizá la proporción entre tipos de movimiento. ¿Hay desequilibrios? ¿Los despachos están en línea con la producción?",
    },
    {
        "nombre": "ritmo temporal",
        "instruccion": "Analizá las horas pico y la variación día a día. ¿Hay días sin actividad? ¿Los fines de semana operan? ¿Picos sospechosos?",
    },
    {
        "nombre": "descuentos y ajustes",
        "instruccion": "Analizá los descuentos (Descuento y Descuento2). ¿Qué movimientos tienen más descuentos? ¿El promedio parece normal?",
    },
    {
        "nombre": "sintesis integradora",
        "instruccion": "Hacé una síntesis de todo. ¿Cuál es el estado general de la operación? ¿Qué es lo más importante que aún no entendés de Ecotecnica?",
    },
]


def construir_prompt_fase2(evidencia_op, conocimientos, conclusiones_existentes, foco_idx=0):
    """Construye prompt para análisis operativo de fase 2 con foco rotativo."""

    def fmt_rows(rows, cols, max_rows=8):
        if not rows:
            return "  (sin datos)"
        lines = []
        for r in (rows or [])[:max_rows]:
            parts = [f"{c}={r.get(c, '-')}" for c in cols if c in (r or {})]
            lines.append("  " + " | ".join(parts))
        return "\n".join(lines)

    foco = FASE2_FOCOS[foco_idx % len(FASE2_FOCOS)]

    dias_txt  = fmt_rows(evidencia_op.get("ultimos_7_dias", []), ["dia", "tipo", "registros", "kilos"])
    maq_txt   = fmt_rows(evidencia_op.get("top_maquinas", []),   ["Maquina", "kilos", "registros"])
    tipos_txt = fmt_rows(evidencia_op.get("dist_tipos", []),      ["tipo", "kilos", "registros"])
    err_txt   = fmt_rows(evidencia_op.get("errores_recientes", []), ["dia", "cantidad", "kilos"])
    horas_txt = fmt_rows(evidencia_op.get("horas_pico", []),      ["hora", "registros", "kilos"])
    desc_txt  = fmt_rows(evidencia_op.get("descuentos", []),      ["tipo", "desc_promedio", "desc2_promedio"])

    conoc_txt = "\n".join([
        f"- {x.get('tema')}: {shorten_text(x.get('texto'), 100)}"
        for x in (conocimientos or [])[:4]
    ]) or "- Sin conocimiento previo."

    previas_auto = "\n".join([
        f"- [{c.get('hora','')}] foco:{c.get('titulo','')} | {shorten_text(c.get('conclusion',''), 90)}"
        for c in ((conclusiones_existentes or {}).get('autonomas') or [])[:5]
    ]) or "- Ninguna aún."

    previas_reglas = "\n".join([
        f"- {x.get('tipo','?')}: {x.get('texto','')}"
        for x in ((conclusiones_existentes or {}).get('reglas') or [])[:4]
    ]) or "- Ninguna aún."

    return f"""Sos una IA industrial local de Ecotecnica, planta de reciclado de plásticos en Argentina.

FOCO DE ESTE CICLO ({foco_idx + 1}): {foco['nombre'].upper()}
{foco['instruccion']}

Respondé SOLO JSON válido, sin markdown:
{{"titulo": "foco:{foco['nombre']} — hallazgo breve", "conclusion_principal": "conclusión concreta sobre este foco", "patrones_detectados": ["patrón 1", "patrón 2"], "anomalias": ["anomalía 1"], "hipotesis_nueva": "hipótesis nueva específica de este foco", "regla_aprendida": "regla operativa estable", "pregunta_para_diego": "pregunta que solo Diego puede responder", "confianza": 0.0, "siguiente_analisis": "próximo ángulo"}}

ÚLTIMOS 7 DÍAS (dia|tipo|registros|kilos):
{dias_txt}

MÁQUINAS (Maquina|kilos|registros):
{maq_txt}

TIPOS MOVIMIENTO (tipo|kilos|registros):
{tipos_txt}

ERRORES REGISTRO (dia|cantidad|kilos):
{err_txt}

HORAS PICO (hora|registros|kilos):
{horas_txt}

DESCUENTOS (tipo|desc_promedio|desc2_promedio):
{desc_txt}

CONOCIMIENTO CONFIRMADO:
{conoc_txt}

YA CONCLUÍ ANTES — no repetir:
{previas_auto}

CONCLUSIONES POR REGLAS:
{previas_reglas}

No repitas análisis previos. Enfocate en: {foco['nombre']}."""


def _valor_vacio_o_null(valor):
    if valor is None:
        return True
    texto = str(valor).strip().lower()
    return texto in ("", "none", "null", "nan", "sin dato", "sin datos")


def _es_columna_conteo(columna):
    c = str(columna or "").lower().strip()
    claves = ["count", "cantidad", "cantidadmovimientos", "movimientos", "registros", "cnt"]
    return any(k in c for k in claves)


def _es_columna_promedio(columna):
    c = str(columna or "").lower().strip()
    return any(k in c for k in ["avg", "prom", "promedio", "media"])


def _es_columna_suma(columna):
    c = str(columna or "").lower().strip()
    return any(k in c for k in ["sum", "suma", "total"])


def _es_columna_metrica(columna):
    c = str(columna or "").lower()
    claves = ["count", "cantidad", "total", "movimientos", "registros", "sum", "avg", "prom", "min", "max"]
    return any(k in c for k in claves)


def _elegir_columna_conteo(cols):
    for c in cols or []:
        if _es_columna_conteo(c):
            return c
    return None


def _elegir_columna_promedio(cols):
    for c in cols or []:
        if _es_columna_promedio(c):
            return c
    return None


def _elegir_columna_suma(cols):
    for c in cols or []:
        if _es_columna_suma(c) and not _es_columna_conteo(c):
            return c
    return None


def _fmt_obs_num(valor, decimales=2):
    n = _numero_observacion(valor)
    if n is None:
        return "-"
    if float(n).is_integer():
        return str(int(n))
    return f"{n:.{decimales}f}".rstrip("0").rstrip(".")


def _numero_observacion(valor):
    if valor is None:
        return None
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        txt = str(valor).replace(",", ".").strip()
        if txt == "":
            return None
        return float(txt)
    except Exception:
        return None


def resultado_sql_sin_datos_utiles(resultado_sql):
    """Devuelve True si el resultado no aporta evidencia usable."""
    if not resultado_sql or not resultado_sql.get("ok"):
        return False
    rows = resultado_sql.get("rows") or []
    cols = resultado_sql.get("columnas") or []
    if not rows:
        return True

    metric_cols = [c for c in cols if _es_columna_metrica(c)]
    if not metric_cols:
        return False

    count_col = _elegir_columna_conteo(metric_cols)
    metricas_revisar = [count_col] if count_col else metric_cols
    valores = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for col in metricas_revisar:
            valores.append(_numero_observacion(row.get(col)))

    valores_validos = [v for v in valores if v is not None]
    if not valores_validos:
        return True
    return all(abs(v) <= 0 for v in valores_validos)


def detectar_observacion_exploratoria(query, resultado_sql):
    if not resultado_sql.get("ok"):
        return {"ok": False, "motivo": "query_error"}
    rows = resultado_sql.get("rows") or []
    cols = resultado_sql.get("columnas") or []
    if not rows or not cols or resultado_sql_sin_datos_utiles(resultado_sql):
        return {"ok": False, "motivo": "sin_datos_utiles"}

    metric_cols = [c for c in cols if _es_columna_metrica(c)]
    dimension_cols = [c for c in cols if c not in metric_cols]
    count_col = _elegir_columna_conteo(metric_cols)
    avg_col = _elegir_columna_promedio(metric_cols)
    sum_col = _elegir_columna_suma(metric_cols)
    metric_col = count_col or (metric_cols[0] if metric_cols else None)
    dimension_col = dimension_cols[0] if dimension_cols else None

    valores_metricos = []
    total_metricas = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        valor_metrica = _numero_observacion(row.get(metric_col)) if metric_col else None
        if valor_metrica is not None:
            valores_metricos.append(valor_metrica)
            total_metricas += max(valor_metrica, 0)

    tiene_agregado = bool(metric_col)
    filas = len(rows)
    top_row = rows[0] if isinstance(rows[0], dict) else {}
    top_valor = top_row.get(dimension_col) if dimension_col else None
    top_metrica = _numero_observacion(top_row.get(metric_col)) if metric_col else None
    top_avg = top_row.get(avg_col) if avg_col else None
    top_sum = top_row.get(sum_col) if sum_col else None
    top_share = (top_metrica / total_metricas) if top_metrica is not None and total_metricas > 0 else None
    segundo = valores_metricos[1] if len(valores_metricos) > 1 else None
    contraste = (top_metrica / segundo) if top_metrica is not None and segundo and segundo > 0 else None

    vacios = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for col in dimension_cols:
            if _valor_vacio_o_null(row.get(col)):
                vacios.append({
                    "columna": col,
                    "valor": row.get(col),
                    "metrica": row.get(metric_col) if metric_col else None,
                    "avg": row.get(avg_col) if avg_col else None,
                    "sum": row.get(sum_col) if sum_col else None,
                })

    senales = []
    fuerte = False
    if tiene_agregado:
        senales.append("resultado_agregado")
    if top_share is not None and top_share >= 0.70:
        senales.append("valor_dominante")
        fuerte = True
    if contraste is not None and contraste >= 3:
        senales.append("contraste_top")
        fuerte = True
    if vacios:
        senales.append("valores_vacios_o_null")
        fuerte = True
    if filas > 0 and (tiene_agregado or vacios or top_share is not None):
        senales.append("filas_con_evidencia")

    if not senales:
        return {"ok": False, "motivo": "sin_senal"}

    query_l = str(query or "").lower()
    columna = dimension_col or "exploracion_autonoma"
    confianza = 0.55
    if tiene_agregado:
        confianza += 0.07
    if vacios:
        confianza += 0.08
    if top_share is not None and top_share >= 0.70:
        confianza += 0.08
    if contraste is not None and contraste >= 3:
        confianza += 0.04
    confianza = min(0.75, round(confianza, 2))

    if "kilosnetos" in query_l and str(dimension_col or "").lower() == "proveedor" and vacios and top_metrica is not None:
        extras = []
        if avg_col:
            extras.append(f"Promedio kilos: {_fmt_obs_num(top_avg)}")
        if sum_col:
            extras.append(f"Suma kilosNetos: {_fmt_obs_num(top_sum)}")
        extras_txt = (" " + ". ".join(extras) + ".") if extras else ""
        conclusion = (
            f"Proveedor vacio/None concentra {_fmt_obs_num(top_metrica, 0)} movimientos con kilosNetos no nulo."
            f"{extras_txt} "
            "Esto sugiere que kilosNetos se usa en contextos donde Proveedor no siempre esta cargado o no es relevante. "
            "Falta cruzar por TiposDeMovimiento para separar ensayo, ajuste o produccion."
        )
    elif vacios and metric_col and dimension_col:
        fila_vacia = vacios[0]
        metrica_txt = _fmt_obs_num(fila_vacia.get("metrica"), 0)
        extras = []
        if avg_col:
            extras.append(f"promedio {avg_col}: {_fmt_obs_num(fila_vacia.get('avg'))}")
        if sum_col:
            extras.append(f"suma {sum_col}: {_fmt_obs_num(fila_vacia.get('sum'))}")
        extras_txt = ("; " + "; ".join(extras)) if extras else ""
        conclusion = f"La exploracion encontro {dimension_col} vacio o NULL con {metrica_txt} {metric_col}{extras_txt}. Conviene cruzarlo por TiposDeMovimiento antes de inferir causa operativa."
    elif top_share is not None and dimension_col and metric_col:
        pct = round(top_share * 100, 1)
        conclusion = f"La distribucion por {dimension_col} muestra un valor dominante: {top_valor} concentra {pct}% de {metric_col}. Es una observacion exploratoria y requiere cruce por TiposDeMovimiento."
    elif tiene_agregado and dimension_col and metric_col:
        conclusion = f"La query produjo una distribucion agregada por {dimension_col}; hay evidencia util para continuar explorando antes de concluir."
    else:
        conclusion = "La query devolvio filas utiles para exploracion, pero todavia no alcanza para una conclusion operativa."

    evidencia = {
        "query": query,
        "columnas": cols,
        "filas": rows[:5],
        "metric_col": metric_col,
        "count_col": count_col,
        "avg_col": avg_col,
        "sum_col": sum_col,
        "dimension_col": dimension_col,
        "top_share": top_share,
        "contraste_top": contraste,
        "senales": senales,
    }
    return {
        "ok": True,
        "columna": columna,
        "conclusion": conclusion,
        "confianza": confianza,
        "evidencia": evidencia,
        "senales": senales,
        "fuerte": fuerte or confianza >= 0.75,
    }

def persistir_observacion_exploratoria(observacion, salida_modelo=None):
    if not observacion or not observacion.get("ok"):
        return {"ok": False, "error": "observacion_invalida"}
    conn_lab = get_ia_connection()
    if not conn_lab:
        return {"ok": False, "error": "sin_conexion_lab"}
    try:
        cur = conn_lab.cursor()
        try:
            ensure_autonomous_tables(cur)
        except Exception:
            pass
        _insert_conclusion_autonoma(cur, {
            "columna": observacion.get("columna") or "exploracion_autonoma",
            "conclusion": observacion.get("conclusion"),
            "confianza": observacion.get("confianza") or 0.6,
            "estado": "observacion_exploratoria",
            "evidencia": observacion.get("evidencia") or {},
            "salida_json": salida_modelo or {},
        })
        conn_lab.commit()
        cur.close()
        conn_lab.close()
        return {"ok": True}
    except Exception as e:
        try:
            conn_lab.rollback()
            conn_lab.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}

def guardar_observacion_exploratoria_si_corresponde(query, resultado_sql, salida_modelo=None):
    observacion = detectar_observacion_exploratoria(query, resultado_sql or {})
    if not observacion.get("ok"):
        if observacion.get("motivo") == "sin_datos_utiles":
            registrar_consulta_sin_datos(query, extraer_filtros_exactos_query(query), "sin_datos_utiles")
            add_reasoning_step(
                "consulta_sin_datos",
                "La query no produjo datos utiles para una observacion exploratoria.",
                "No guardo conocimiento ni observacion."
            )
        return {"ok": False, "skipped": observacion.get("motivo")}

    persistencia = persistir_observacion_exploratoria(observacion, salida_modelo)
    if persistencia.get("ok"):
        add_reasoning_step(
            "observacion_exploratoria",
            "Observacion exploratoria guardada.",
            shorten_text(observacion.get("conclusion"), 220)
        )
        if observacion.get("fuerte") or (observacion.get("confianza") or 0) >= 0.75:
            notificar_telegram_conclusion({
                "fecha": datetime.now().isoformat(),
                "hora": datetime.now().strftime("%H:%M:%S"),
                "columna": observacion.get("columna") or "exploracion_autonoma",
                "estado": "observacion_exploratoria",
                "confianza": observacion.get("confianza"),
                "conclusion": observacion.get("conclusion"),
                "evidencia": observacion.get("evidencia"),
            }, origen="observacion_exploratoria")
    else:
        add_reasoning_step(
            "observacion_exploratoria_error",
            f"No pude persistir observacion exploratoria: {persistencia.get('error')}",
            None
        )
    return persistencia

def persistir_conclusion_fase2(resultado, evidencia_op):
    """Guarda conclusión de fase 2 en ia_conclusiones_autonomas y en conclusiones."""
    if not resultado_fase2_tiene_conocimiento(resultado):
        return {"ok": False, "error": "sin_conclusion_util"}
    conn_lab = get_ia_connection()
    if not conn_lab:
        return {"ok": False, "error": "sin_conexion_lab"}
    try:
        cur = conn_lab.cursor()
        try:
            ensure_autonomous_tables(cur)
        except Exception:
            pass
        conclusion_texto = primer_texto(
            resultado.get("conclusion_principal"),
            resultado.get("conclusion"),
            resultado.get("conclusion_autonoma"),
            resultado.get("interpretacion"),
            resultado.get("resumen"),
        )
        hipotesis_texto = primer_texto(
            resultado.get("hipotesis_nueva"),
            resultado.get("hipotesis_refinada"),
            resultado.get("hipotesis"),
        )
        regla_texto = primer_texto(
            resultado.get("regla_aprendida"),
            resultado.get("regla_aprendible"),
            resultado.get("regla"),
        )
        if hipotesis_texto:
            persistir_hipotesis_autonoma(resultado, evidencia_op, origen="explorador_fase2")
        _insert_conclusion_autonoma(cur, {
            "columna":          "operativo_planta",
            "conclusion":       conclusion_texto,
            "hipotesis_refinada": hipotesis_texto,
            "regla_aprendible": regla_texto,
            "confianza":        resultado.get("confianza"),
            "estado":           "fase2_operativa",
            "evidencia":        evidencia_op or {},
            "salida_json":      resultado,
        })

        # Si la confianza es suficiente, también insertar en conclusiones
        conf = confianza_float(resultado.get("confianza")) or 0
        if conf >= AUTO_IA_MIN_CONFIDENCE and conclusion_texto:
            prioridad = nivel_interes_conclusion({
                "conclusion": conclusion_texto,
                "regla_aprendible": regla_texto,
                "confianza": conf,
                "anomalias": resultado.get("anomalias") or [],
            })
            cur.execute("""
                INSERT INTO conclusiones (tipo, prioridad, texto, vigente)
                VALUES (%s, %s, %s, 1)
            """, (
                "ia_fase2",
                prioridad,
                conclusion_texto,
            ))

        # Si hay hipótesis nueva, insertarla en hipotesis_esquema
        if hipotesis_texto:
            hip_cols = get_table_columns(cur, "hipotesis_esquema")
            pregunta_col = "pregunta_usuario" if "pregunta_usuario" in hip_cols else None
            if pregunta_col:
                cur.execute(f"""
                    INSERT INTO hipotesis_esquema (
                        columna, hipotesis, {pregunta_col}, confianza, estado, vigente
                    ) VALUES (%s, %s, %s, %s, 'hipotesis', 1)
                """, (
                    "operativo_planta",
                    hipotesis_texto,
                    resultado.get("pregunta_para_diego"),
                    confianza_float(resultado.get("confianza")),
                ))
            else:
                cur.execute("""
                    INSERT INTO hipotesis_esquema (
                        columna, hipotesis, confianza, estado, vigente
                    ) VALUES (%s, %s, %s, 'hipotesis', 1)
                """, (
                    "operativo_planta",
                    hipotesis_texto,
                    confianza_float(resultado.get("confianza")),
                ))

        conn_lab.commit()
        cur.close()
        conn_lab.close()
        return {"ok": True}
    except Exception as e:
        try:
            conn_lab.rollback()
            conn_lab.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def guardar_observacion_operativa_si_corresponde():
    """Guarda un pulso operativo deterministico para no depender solo del LLM."""
    if not AUTO_OBSERVADORES_ENABLED:
        return {"ok": False, "skipped": "disabled"}

    conn_lab = get_ia_connection()
    if not conn_lab:
        return {"ok": False, "error": "sin_conexion_lab"}

    try:
        cur_lab = conn_lab.cursor()
        try:
            ensure_autonomous_tables(cur_lab)
        except Exception:
            pass

        cutoff = datetime.now() - timedelta(seconds=AUTO_OBSERVADOR_INTERVAL_SECONDS)
        cur_lab.execute("""
            SELECT COUNT(*) AS total
            FROM ia_conclusiones_autonomas
            WHERE estado = 'observacion_operativa'
              AND creada_en >= %s
        """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),))
        reciente = cur_lab.fetchone() or {}
        if int(reciente.get("total") or 0) > 0:
            cur_lab.close()
            conn_lab.close()
            return {"ok": False, "skipped": "intervalo"}

        conn_op = get_db_connection()
        if not conn_op:
            cur_lab.close()
            conn_lab.close()
            return {"ok": False, "error": "sin_conexion_operativa"}

        cur_op = conn_op.cursor()
        evidencia = {}

        cur_op.execute("""
            SELECT
                COUNT(*) AS movimientos_hoy,
                ROUND(COALESCE(SUM(CASE WHEN TiposDeMovimiento = 'PRODUCCION' THEN kilos ELSE 0 END), 0), 2) AS produccion_hoy_kg,
                ROUND(COALESCE(SUM(CASE WHEN TiposDeMovimiento LIKE 'ING%%' THEN kilos ELSE 0 END), 0), 2) AS ingresos_hoy_kg
            FROM TiposDeMovimiento
            WHERE FechaHora IS NOT NULL
              AND DATE(FechaHora) = CURDATE()
        """)
        hoy = cur_op.fetchone() or {}
        evidencia["hoy"] = hoy

        cur_op.execute("""
            SELECT FechaHora, TiposDeMovimiento, Proveedor, Description, kilos
            FROM TiposDeMovimiento
            WHERE FechaHora IS NOT NULL
              AND TiposDeMovimiento LIKE 'ING%%'
            ORDER BY FechaHora DESC
            LIMIT 1
        """)
        ultimo_ingreso = cur_op.fetchone() or {}
        evidencia["ultimo_ingreso"] = ultimo_ingreso

        # Leer maquina lider del mes actual desde tablas derivadas (evita recalcular rangos manuales)
        maquina_mes = {}
        try:
            periodo_actual = datetime.now().strftime("%Y-%m")
            cur_lab.execute("""
                SELECT Maquina, kilos_total AS kilos_mes
                FROM ia_resumen_mensual_produccion_maquina
                WHERE periodo = %s AND criterio_version = %s
                ORDER BY kilos_total DESC
                LIMIT 1
            """, (periodo_actual, DERIVADAS_VERSION))
            maquina_mes = cur_lab.fetchone() or {}
        except Exception:
            pass
        evidencia["maquina_lider_mes"] = maquina_mes

        cur_op.execute("""
            SELECT COUNT(*) AS fechas_raras
            FROM TiposDeMovimiento
            WHERE FechaHora IS NULL
               OR FechaHora < '2020-01-01'
               OR FechaHora > DATE_ADD(NOW(), INTERVAL 1 DAY)
        """)
        fechas = cur_op.fetchone() or {}
        evidencia["calidad_fechas"] = fechas

        precios = []
        try:
            cur_op.execute("""
                SELECT Description, Proveedor, ROUND(AVG(Precio), 2) AS precio_promedio, COUNT(*) AS registros
                FROM TiposDeMovimiento
                WHERE FechaHora IS NOT NULL
                  AND FechaHora >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                  AND Precio IS NOT NULL
                  AND Precio > 0
                GROUP BY Description, Proveedor
                HAVING registros >= 2
                ORDER BY registros DESC
                LIMIT 3
            """)
            precios = cur_op.fetchall() or []
        except Exception as e:
            precios = [{"error": str(e)}]
        evidencia["precios_recientes"] = precios

        cur_op.close()
        conn_op.close()

        # Top ingresos por proveedor del mes actual desde tablas derivadas
        top_ingresos_proveedor = []
        try:
            periodo_actual = datetime.now().strftime("%Y-%m")
            cur_lab.execute("""
                SELECT Proveedor, registros, kilos_total
                FROM ia_resumen_mensual_ingresos_proveedor
                WHERE periodo = %s AND criterio_version = %s
                ORDER BY kilos_total DESC
                LIMIT 5
            """, (periodo_actual, DERIVADAS_VERSION))
            top_ingresos_proveedor = cur_lab.fetchall() or []
        except Exception:
            pass
        evidencia["top_ingresos_proveedor_mes"] = top_ingresos_proveedor

        partes = [
            "Pulso operativo automatico.",
            f"Hoy: {hoy.get('produccion_hoy_kg') or 0} kg produccion; {hoy.get('ingresos_hoy_kg') or 0} kg ingresos; {hoy.get('movimientos_hoy') or 0} movimientos.",
        ]
        if maquina_mes:
            partes.append(f"Maquina lider del mes: {maquina_mes.get('Maquina') or 'sin maquina'} con {maquina_mes.get('kilos_mes') or 0} kg.")
        if ultimo_ingreso:
            partes.append(
                "Ultimo ingreso: "
                f"{ultimo_ingreso.get('Proveedor') or 'sin proveedor'} | "
                f"{ultimo_ingreso.get('Description') or 'sin descripcion'} | "
                f"{ultimo_ingreso.get('kilos') or 0} kg | "
                f"{ultimo_ingreso.get('FechaHora') or 'sin fecha'}."
            )
        fechas_raras = int((fechas or {}).get("fechas_raras") or 0)
        if fechas_raras:
            partes.append(f"Atencion: hay {fechas_raras} registros con FechaHora nula, futura o anterior a 2020.")
        if precios and not precios[0].get("error"):
            p = precios[0]
            partes.append(
                "Precio reciente mas repetido: "
                f"{p.get('Description') or 'sin descripcion'} / {p.get('Proveedor') or 'sin proveedor'} "
                f"promedio {p.get('precio_promedio')} en {p.get('registros')} registros."
            )

        conclusion = " ".join(partes)
        _insert_conclusion_autonoma(cur_lab, {
            "columna": "pulso_operativo",
            "conclusion": conclusion,
            "confianza": 0.99,
            "estado": "observacion_operativa",
            "evidencia": evidencia,
            "salida_json": {
                "tipo": "observador_deterministico",
                "generado_en": datetime.now().isoformat(),
                "intervalo_segundos": AUTO_OBSERVADOR_INTERVAL_SECONDS,
            },
        })
        conn_lab.commit()
        cur_lab.close()
        conn_lab.close()
        return {"ok": True, "conclusion": conclusion, "evidencia": evidencia}
    except Exception as e:
        try:
            conn_lab.rollback()
            conn_lab.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def inferir_significado_columna(tabla, columna, tipo_dato, evidencia):
    """Inferencia conservadora: diccionario operativo inicial de columnas."""
    c = (columna or "").lower()
    distintos = int((evidencia or {}).get("distintos") or 0)
    pct_vacios = float((evidencia or {}).get("pct_vacios") or 0)

    reglas = [
        (("idtiposdemovimiento",), "identificador unico del movimiento", 0.95),
        (("fechahora",), "fecha y hora del registro o movimiento operativo", 0.92),
        (("fecha",), "fecha del registro o movimiento, probablemente derivada de FechaHora", 0.84),
        (("hora",), "hora del registro o movimiento, probablemente derivada de FechaHora", 0.84),
        (("tiposdemovimiento",), "categoria operativa del movimiento registrado", 0.9),
        (("nrolote", "lote"), "identificador de lote o trazabilidad asociada al movimiento", 0.72),
        (("unidadmedida",), "unidad, patente u otro dato operativo de soporte", 0.65),
        (("kilosnetos",), "kilos resultantes luego de descuentos o ajustes", 0.82),
        (("kilos", "kgsredond", "kgsredondmenos"), "magnitud de peso asociada al movimiento", 0.82),
        (("descuento2",), "valor numerico de descuento o ajuste de kilos", 0.72),
        (("descuento",), "motivo, tipo o etiqueta de descuento/ajuste", 0.68),
        (("proveedor",), "entidad asociada al movimiento; puede ser proveedor, cliente u origen/destino", 0.7),
        (("materiales", "description", "desccorta"), "descripcion del material, producto o clasificacion operativa", 0.72),
        (("operador", "idoperador"), "persona, usuario o puesto que cargo/operó el movimiento", 0.72),
        (("maquina", "idmaquina"), "maquina, puesto o proceso operativo asociado al movimiento", 0.72),
        (("precio",), "valor economico o precio unitario asociado al material/movimiento cuando esta cargado", 0.65),
        (("user_bot",), "usuario/origen que registro el movimiento desde el bot o sistema", 0.78),
        (("id_bolson",), "identificador fisico o etiqueta operativa de bolson", 0.68),
        (("nroensayo", "fechaensayo", "ml", "mlmenos", "segsembud"), "dato de ensayo o laboratorio asociado al material", 0.62),
    ]
    for claves, significado, confianza in reglas:
        if any(k in c for k in claves):
            if pct_vacios > 85:
                confianza = max(0.45, confianza - 0.18)
            return significado, confianza

    if "date" in (tipo_dato or "").lower() or "time" in (tipo_dato or "").lower():
        return "campo temporal pendiente de interpretar", 0.55
    if distintos <= 20 and pct_vacios < 70:
        return "categoria operativa con pocos valores distintos", 0.5
    if distintos > 1000:
        return "identificador o texto altamente variable pendiente de interpretar", 0.45
    return "columna pendiente de interpretacion operativa", 0.35


def guardar_diccionario_columnas_si_corresponde():
    """Construye memoria de bajo nivel: tabla -> columna -> significado probable."""
    if not AUTO_DICCIONARIO_ENABLED:
        return {"ok": False, "skipped": "disabled"}

    conn_lab = get_ia_connection()
    if not conn_lab:
        return {"ok": False, "error": "sin_conexion_lab"}

    try:
        cur_lab = conn_lab.cursor()
        dicc_cols = get_table_columns(cur_lab, "ia_diccionario_columnas")
        if not dicc_cols:
            cur_lab.execute("""
                CREATE TABLE IF NOT EXISTS ia_diccionario_columnas (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    tabla VARCHAR(128) NOT NULL,
                    columna VARCHAR(128) NOT NULL,
                    tipo_dato VARCHAR(128) NULL,
                    significado_inferido TEXT NULL,
                    evidencia_json JSON NULL,
                    confianza DECIMAL(5,3) NULL,
                    estado VARCHAR(64) DEFAULT 'inferido',
                    actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_tabla_columna (tabla, columna)
                ) CHARACTER SET utf8mb4
            """)

        cutoff = datetime.now() - timedelta(seconds=AUTO_DICCIONARIO_INTERVAL_SECONDS)
        cur_lab.execute("""
            SELECT COUNT(*) AS total
            FROM ia_diccionario_columnas
            WHERE tabla = 'TiposDeMovimiento'
              AND actualizado_en >= %s
        """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),))
        reciente = cur_lab.fetchone() or {}
        if int(reciente.get("total") or 0) >= 10:
            cur_lab.close()
            conn_lab.close()
            return {"ok": False, "skipped": "intervalo"}

        conn_op = get_db_connection()
        if not conn_op:
            cur_lab.close()
            conn_lab.close()
            return {"ok": False, "error": "sin_conexion_operativa"}

        cur_op = conn_op.cursor()
        cur_op.execute("""
            SELECT COLUMN_NAME AS columna, DATA_TYPE AS tipo_dato, ORDINAL_POSITION AS posicion
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = 'TiposDeMovimiento'
            ORDER BY ORDINAL_POSITION
        """, (DB_CONFIG.get("database"),))
        columnas = cur_op.fetchall() or []

        asegurar_memoria_explorador(cur_lab)

        guardadas = 0
        for col in columnas:
            nombre = col.get("columna")
            tipo = col.get("tipo_dato")
            safe_col = "`" + str(nombre).replace("`", "``") + "`"
            evidencia = {
                "posicion": col.get("posicion"),
                "tipo_dato": tipo,
            }
            try:
                cur_op.execute(f"""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN {safe_col} IS NULL THEN 1 ELSE 0 END) AS vacios,
                        COUNT(DISTINCT {safe_col}) AS distintos
                    FROM TiposDeMovimiento
                """)
                stats = cur_op.fetchone() or {}
                total = int(stats.get("total") or 0)
                vacios = int(stats.get("vacios") or 0)
                distintos = int(stats.get("distintos") or 0)
                evidencia.update({
                    "total": total,
                    "vacios": vacios,
                    "pct_vacios": round((vacios * 100 / total), 2) if total else 0,
                    "distintos": distintos,
                })
                cur_op.execute(f"""
                    SELECT CAST({safe_col} AS CHAR) AS valor, COUNT(*) AS cantidad
                    FROM TiposDeMovimiento
                    WHERE {safe_col} IS NOT NULL
                    GROUP BY {safe_col}
                    ORDER BY cantidad DESC
                    LIMIT 5
                """)
                evidencia["frecuentes"] = cur_op.fetchall() or []
                for fr in evidencia["frecuentes"]:
                    valor = str(fr.get("valor") or "").strip()
                    if not valor or len(valor) > 255:
                        continue
                    cur_lab.execute("""
                        INSERT INTO ia_valores_observados (tabla, columna, valor, cantidad, ultimo_visto, fuente_query)
                        VALUES ('TiposDeMovimiento', %s, %s, %s, NOW(), 'diccionario_columnas')
                        ON DUPLICATE KEY UPDATE
                            cantidad = GREATEST(COALESCE(cantidad, 0), COALESCE(VALUES(cantidad), 0)),
                            ultimo_visto = NOW()
                    """, (nombre, valor, fr.get("cantidad")))
            except Exception as e:
                evidencia["error"] = str(e)

            significado, confianza = inferir_significado_columna("TiposDeMovimiento", nombre, tipo, evidencia)
            cur_lab.execute("""
                INSERT INTO ia_diccionario_columnas (
                    tabla, columna, tipo_dato, significado_inferido,
                    evidencia_json, confianza, estado
                ) VALUES (%s, %s, %s, %s, %s, %s, 'inferido')
                ON DUPLICATE KEY UPDATE
                    tipo_dato = VALUES(tipo_dato),
                    significado_inferido = VALUES(significado_inferido),
                    evidencia_json = VALUES(evidencia_json),
                    confianza = VALUES(confianza),
                    estado = VALUES(estado),
                    actualizado_en = CURRENT_TIMESTAMP
            """, (
                "TiposDeMovimiento",
                nombre,
                tipo,
                significado,
                json.dumps(evidencia, ensure_ascii=False, default=str),
                confianza,
            ))
            guardadas += 1

        conn_lab.commit()
        cur_op.close()
        conn_op.close()
        cur_lab.close()
        conn_lab.close()
        return {"ok": True, "columnas": guardadas}
    except Exception as e:
        try:
            conn_lab.rollback()
            conn_lab.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def quote_sql_identifier(nombre):
    return "`" + str(nombre).replace("`", "``") + "`"


def data_type_es_numerico(tipo_dato):
    return str(tipo_dato or "").lower() in {
        "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
        "decimal", "numeric", "float", "double", "real",
    }


def expr_presente_sql(columna, tipo_dato=None):
    quoted = quote_sql_identifier(columna)
    if data_type_es_numerico(tipo_dato):
        return f"{quoted} IS NOT NULL"
    return f"{quoted} IS NOT NULL AND TRIM(CAST({quoted} AS CHAR)) <> ''"


def confianza_relacion(total_registros, tipos_con_datos, tipos_total, pct_presencia_global):
    if not total_registros:
        return 0.25
    cobertura_tipos = (tipos_con_datos / tipos_total) if tipos_total else 0
    base = 0.45 + min(total_registros / 10000, 1) * 0.20
    presencia = min(max((pct_presencia_global or 0) / 100, 0), 1) * 0.20
    cobertura = cobertura_tipos * 0.15
    return round(min(0.95, base + presencia + cobertura), 3)


def descripcion_presencia_columna(columna, evidencia):
    por_tipo = evidencia.get("por_tipo") or []
    con_mas_presencia = [x for x in por_tipo if (x.get("pct_presente") or 0) > 0]
    con_mas_presencia.sort(key=lambda x: (x.get("pct_presente") or 0, x.get("registros") or 0), reverse=True)
    top = ", ".join([
        f"{x.get('tipo_movimiento')}: {x.get('pct_presente')}%"
        for x in con_mas_presencia[:4]
    ])
    pct_global = evidencia.get("pct_presente_global")
    if top:
        return f"{columna} esta presente en {pct_global}% de los registros; aparece sobre todo en {top}."
    return f"{columna} no muestra presencia util por TiposDeMovimiento en la evidencia actual."


COLUMNAS_RELACION_BASICAS = {"idtiposdemovimiento", "nrolote", "fechahora", "fecha", "hora", "kilos", "unidadmedida", "kgsredond", "kgsredondmenos", "lote"}
COLUMNAS_RELACION_OPERATIVAS = {"precio": 22, "proveedor": 18, "maquina": 18, "operador": 14, "if": 18, "descuento2": 16, "descuento": 12, "kilosnetos": 12, "materiales": 12, "description": 8, "desccorta": 8}
TIPOS_INGRESO_HINTS = ("ING", "RECEPC", "COMPRA", "BAL PUBLICA", "PESAJE")
TIPOS_PRODUCCION_HINTS = ("PRODUCCION", "PROD")
TIPOS_DESPACHO_HINTS = ("DESP", "VENTA", "CLIENT")


def nombre_columna_limpio(nombre):
    return str(nombre or "").strip().lower()


def nivel_interes_relacion(score):
    s = float(score or 0)
    if s >= 72:
        return "alta"
    if s >= 45:
        return "media"
    return "baja"


def pct_row(row, campo):
    try:
        return float(row.get(campo) or 0)
    except Exception:
        return 0.0


def tipo_contiene(tipo, hints):
    t = str(tipo or "").upper()
    return any(h in t for h in hints)


def top_tipos_por_campo(por_tipo, campo, minimo=1, limite=4):
    rows = [r for r in (por_tipo or []) if pct_row(r, campo) >= minimo]
    rows.sort(key=lambda x: (pct_row(x, campo), int(x.get("registros") or 0)), reverse=True)
    return rows[:limite]


def resumen_tipos(rows, campo):
    return ", ".join([f"{x.get('tipo_movimiento')}: {round(pct_row(x, campo), 2)}%" for x in rows])


def score_relacion_presencia(columna, evidencia):
    por_tipo = evidencia.get("por_tipo") or []
    pct_global = float(evidencia.get("pct_presente_global") or 0)
    vals = [pct_row(x, "pct_presente") for x in por_tipo]
    contraste = (max(vals) - min(vals)) if vals else 0
    tipos_con = int(evidencia.get("tipos_con_datos") or 0)
    tipos_total = int(evidencia.get("tipos_movimiento_total") or 0)
    cobertura = (tipos_con / tipos_total) if tipos_total else 0
    col_l = nombre_columna_limpio(columna)
    score = 10 + min(contraste, 100) * 0.48
    if 5 <= pct_global <= 85:
        score += 16
    if cobertura <= 0.55:
        score += 12
    score += COLUMNAS_RELACION_OPERATIVAS.get(col_l, 0)
    if col_l in COLUMNAS_RELACION_BASICAS:
        score -= 38
    if pct_global >= 95 and contraste < 20:
        score -= 42
    if pct_global <= 1:
        score -= 10
    top = top_tipos_por_campo(por_tipo, "pct_presente", minimo=1, limite=6)
    if col_l == "precio" and any(tipo_contiene(x.get("tipo_movimiento"), TIPOS_INGRESO_HINTS) for x in top): score += 18
    if col_l == "proveedor" and any(tipo_contiene(x.get("tipo_movimiento"), TIPOS_INGRESO_HINTS + TIPOS_DESPACHO_HINTS) for x in top): score += 14
    if col_l == "maquina" and any(tipo_contiene(x.get("tipo_movimiento"), TIPOS_PRODUCCION_HINTS + TIPOS_DESPACHO_HINTS) for x in top): score += 14
    if col_l == "operador" and any(tipo_contiene(x.get("tipo_movimiento"), TIPOS_PRODUCCION_HINTS) for x in top): score += 10
    if col_l == "if" and any(tipo_contiene(x.get("tipo_movimiento"), TIPOS_PRODUCCION_HINTS + TIPOS_DESPACHO_HINTS) for x in top): score += 14
    return round(max(0, min(100, score)), 2)


def descripcion_presencia_interpretable(columna, evidencia):
    por_tipo = evidencia.get("por_tipo") or []
    top = top_tipos_por_campo(por_tipo, "pct_presente", minimo=1, limite=4)
    bajos = [x for x in por_tipo if pct_row(x, "pct_presente") <= 5 and int(x.get("registros") or 0) > 0]
    bajos.sort(key=lambda x: int(x.get("registros") or 0), reverse=True)
    top_txt = resumen_tipos(top, "pct_presente")
    bajos_txt = ", ".join([str(x.get("tipo_movimiento")) for x in bajos[:3]])
    col_l = nombre_columna_limpio(columna)
    if col_l == "precio" and top_txt:
        return f"Precio aparece concentrado en {top_txt}" + (f"; casi no aparece en {bajos_txt}." if bajos_txt else ".")
    if col_l == "proveedor" and top_txt:
        return f"Proveedor aparece sobre todo en {top_txt}; debe leerse como entidad asociada a ingresos/despachos, no solo proveedor de compra."
    if col_l == "maquina" and top_txt:
        return f"Maquina aparece fuerte en {top_txt}; sugiere puesto, linea o proceso operativo asociado al movimiento."
    if col_l == "operador" and top_txt:
        return f"Operador aparece principalmente en {top_txt}; ayuda a entender donde hay carga humana o puesto operativo."
    if col_l == "if" and top_txt:
        return f"IF aparece principalmente en {top_txt}; es una columna estructuralmente especifica y requiere investigacion funcional."
    if col_l in ("descuento2", "descuento") and top_txt:
        return f"{columna} aparece con mayor peso en {top_txt}; conviene cruzarla contra kilos y kilosNetos."
    if top_txt:
        return f"{columna} esta presente en {evidencia.get('pct_presente_global')}% y discrimina contexto: {top_txt}" + (f"; casi ausente en {bajos_txt}." if bajos_txt else ".")
    return descripcion_presencia_columna(columna, evidencia)


def score_relacion_co_presencia(col_a, col_b, evidencia):
    por_tipo = evidencia.get("por_tipo") or []
    pct_global = float(evidencia.get("pct_ambos_presentes_global") or 0)
    vals = [pct_row(x, "pct_ambos_presentes") for x in por_tipo]
    contraste = (max(vals) - min(vals)) if vals else 0
    a_l, b_l = nombre_columna_limpio(col_a), nombre_columna_limpio(col_b)
    score = 8 + min(contraste, 100) * 0.45
    if 5 <= pct_global <= 85: score += 14
    for c in (a_l, b_l):
        score += COLUMNAS_RELACION_OPERATIVAS.get(c, 0) * 0.55
        if c in COLUMNAS_RELACION_BASICAS: score -= 18
    if pct_global >= 95 and contraste < 20: score -= 36
    if frozenset((a_l, b_l)) in {frozenset(("kilosnetos", "descuento2")), frozenset(("precio", "proveedor")), frozenset(("precio", "materiales")), frozenset(("maquina", "operador")), frozenset(("maquina", "kilos"))}: score += 18
    return round(max(0, min(100, score)), 2)


def descripcion_co_presencia_interpretable(col_a, col_b, evidencia):
    top_txt = resumen_tipos(top_tipos_por_campo(evidencia.get("por_tipo") or [], "pct_ambos_presentes", minimo=1, limite=4), "pct_ambos_presentes")
    pct_global = evidencia.get("pct_ambos_presentes_global")
    par = frozenset((nombre_columna_limpio(col_a), nombre_columna_limpio(col_b)))
    if par == frozenset(("precio", "proveedor")) and top_txt: return f"Precio y Proveedor co-aparecen sobre todo en {top_txt}; senala movimientos economicos donde la entidad asociada importa."
    if par == frozenset(("precio", "materiales")) and top_txt: return f"Precio y Materiales co-aparecen en {top_txt}; base para aprender precio por material y tipo de ingreso."
    if par == frozenset(("maquina", "operador")) and top_txt: return f"Maquina y Operador co-aparecen en {top_txt}; relaciona puesto/proceso con carga operativa humana."
    if par == frozenset(("kilosnetos", "descuento2")) and top_txt: return f"kilosNetos y Descuento2 co-aparecen en {top_txt}; conviene revisar si Descuento2 explica diferencias contra kilos."
    return f"{col_a} y {col_b} co-aparecen en {pct_global}%" + (f", concentrado en {top_txt}." if top_txt else ".")


def score_relacion_diferencia_kilos(evidencia):
    diffs = [float(x.get("diferencia_abs_promedio") or 0) for x in (evidencia.get("por_tipo") or [])]
    max_diff = max(diffs) if diffs else 0
    return round(max(0, min(100, 35 + min(max_diff, 100) * 0.45)), 2)


def upsert_conocimiento_estructural(cur_lab, tabla, tema, descripcion, evidencia, confianza, interes_score, nivel_interes):
    cur_lab.execute("""
        CREATE TABLE IF NOT EXISTS ia_conocimiento_estructural (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tabla VARCHAR(128) NOT NULL,
            tema VARCHAR(255) NOT NULL,
            descripcion TEXT NULL,
            evidencia_json JSON NULL,
            confianza DECIMAL(5,3) NULL,
            interes_score DECIMAL(6,2) NULL,
            nivel_interes VARCHAR(32) NULL,
            estado VARCHAR(64) DEFAULT 'relacion_estructural',
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_tema (tabla(64), tema(160))
        ) CHARACTER SET utf8mb4
    """)
    cur_lab.execute("""
        INSERT INTO ia_conocimiento_estructural (tabla, tema, descripcion, evidencia_json, confianza, interes_score, nivel_interes, estado)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'relacion_estructural')
        ON DUPLICATE KEY UPDATE descripcion=VALUES(descripcion), evidencia_json=VALUES(evidencia_json), confianza=VALUES(confianza), interes_score=VALUES(interes_score), nivel_interes=VALUES(nivel_interes), estado=VALUES(estado), actualizado_en=CURRENT_TIMESTAMP
    """, (tabla, tema, descripcion, json.dumps(evidencia, ensure_ascii=False, default=str), confianza, interes_score, nivel_interes))


def guardar_relaciones_columnas_si_corresponde(forzar=False):
    """Aprende relaciones estructurales usando TiposDeMovimiento como eje."""
    if not AUTO_RELACIONES_ENABLED and not forzar:
        return {"ok": False, "skipped": "disabled"}

    conn_lab = get_ia_connection()
    if not conn_lab:
        return {"ok": False, "error": "sin_conexion_lab"}

    try:
        cur_lab = conn_lab.cursor()
        cur_lab.execute("""
            CREATE TABLE IF NOT EXISTS ia_relaciones_columnas (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tabla VARCHAR(128) NOT NULL,
                columna_a VARCHAR(128) NOT NULL,
                columna_b VARCHAR(128) NOT NULL,
                tipo_relacion VARCHAR(128) NOT NULL,
                descripcion TEXT NULL,
                evidencia_json JSON NULL,
                confianza DECIMAL(5,3) NULL,
                interes_score DECIMAL(6,2) NULL,
                nivel_interes VARCHAR(32) NULL,
                categoria VARCHAR(64) NULL,
                estado VARCHAR(64) DEFAULT 'inferido',
                actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_relacion (tabla(64), columna_a(64), columna_b(64), tipo_relacion(64)),
                KEY idx_tabla_tipo (tabla, tipo_relacion),
                KEY idx_actualizado (actualizado_en)
            ) CHARACTER SET utf8mb4
        """)

        rel_cols_actuales = get_table_columns(cur_lab, "ia_relaciones_columnas")
        for col_name, col_def in [
            ("interes_score", "DECIMAL(6,2) NULL"),
            ("nivel_interes", "VARCHAR(32) NULL"),
            ("categoria", "VARCHAR(64) NULL"),
        ]:
            if col_name not in rel_cols_actuales:
                cur_lab.execute(f"ALTER TABLE ia_relaciones_columnas ADD COLUMN {col_name} {col_def}")

        if not forzar:
            cutoff = datetime.now() - timedelta(seconds=AUTO_RELACIONES_INTERVAL_SECONDS)
            cur_lab.execute("""
                SELECT COUNT(*) AS total
                FROM ia_relaciones_columnas
                WHERE tabla = 'TiposDeMovimiento'
                  AND actualizado_en >= %s
            """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),))
            reciente = cur_lab.fetchone() or {}
            if int(reciente.get("total") or 0) >= 10:
                cur_lab.close()
                conn_lab.close()
                return {"ok": False, "skipped": "intervalo"}

        conn_op = get_db_connection()
        if not conn_op:
            cur_lab.close()
            conn_lab.close()
            return {"ok": False, "error": "sin_conexion_operativa"}

        cur_op = conn_op.cursor()
        cur_op.execute("""
            SELECT COLUMN_NAME AS columna, DATA_TYPE AS tipo_dato, ORDINAL_POSITION AS posicion
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = 'TiposDeMovimiento'
            ORDER BY ORDINAL_POSITION
        """, (DB_CONFIG.get("database"),))
        columnas = cur_op.fetchall() or []
        tipos_por_columna = {x.get("columna"): x.get("tipo_dato") for x in columnas}
        nombres_columnas = [x.get("columna") for x in columnas if x.get("columna")]

        cur_op.execute("SELECT COUNT(*) AS total FROM TiposDeMovimiento")
        total_registros = int((cur_op.fetchone() or {}).get("total") or 0)
        cur_op.execute("SELECT COUNT(DISTINCT TiposDeMovimiento) AS total FROM TiposDeMovimiento")
        tipos_total = int((cur_op.fetchone() or {}).get("total") or 0)

        guardadas = 0
        for col in nombres_columnas:
            if col == "TiposDeMovimiento":
                continue
            tipo = tipos_por_columna.get(col)
            qcol = quote_sql_identifier(col)
            presente = expr_presente_sql(col, tipo)
            numerica = data_type_es_numerico(tipo)
            metricas_num = ""
            if numerica:
                metricas_num = f""",
                ROUND(AVG(CASE WHEN {presente} THEN {qcol} ELSE NULL END), 3) AS promedio,
                ROUND(SUM(CASE WHEN {presente} THEN {qcol} ELSE 0 END), 3) AS suma,
                ROUND(MIN(CASE WHEN {presente} THEN {qcol} ELSE NULL END), 3) AS minimo,
                ROUND(MAX(CASE WHEN {presente} THEN {qcol} ELSE NULL END), 3) AS maximo"""
            cur_op.execute(f"""
                SELECT
                    TiposDeMovimiento AS tipo_movimiento,
                    COUNT(*) AS registros,
                    SUM(CASE WHEN {presente} THEN 1 ELSE 0 END) AS presentes,
                    SUM(CASE WHEN {presente} THEN 0 ELSE 1 END) AS nulos_o_vacios,
                    ROUND(AVG(CASE WHEN {presente} THEN 1 ELSE 0 END) * 100, 2) AS pct_presente,
                    ROUND(AVG(CASE WHEN {presente} THEN 0 ELSE 1 END) * 100, 2) AS pct_nulos_o_vacios,
                    COUNT(DISTINCT CASE WHEN {presente} THEN {qcol} ELSE NULL END) AS distintos
                    {metricas_num}
                FROM TiposDeMovimiento
                GROUP BY TiposDeMovimiento
                ORDER BY registros DESC
                LIMIT 80
            """)
            por_tipo = cur_op.fetchall() or []

            cur_op.execute(f"""
                SELECT CAST({qcol} AS CHAR) AS valor, COUNT(*) AS cantidad
                FROM TiposDeMovimiento
                WHERE {presente}
                GROUP BY {qcol}
                ORDER BY cantidad DESC
                LIMIT 8
            """)
            frecuentes = cur_op.fetchall() or []

            presentes_global = sum(int(x.get("presentes") or 0) for x in por_tipo)
            pct_presente_global = round((presentes_global * 100 / total_registros), 2) if total_registros else 0
            tipos_con_datos = len([x for x in por_tipo if int(x.get("presentes") or 0) > 0])
            evidencia = {
                "tabla": "TiposDeMovimiento",
                "eje": "TiposDeMovimiento",
                "columna": col,
                "tipo_dato": tipo,
                "es_numerica": numerica,
                "total_registros": total_registros,
                "tipos_movimiento_total": tipos_total,
                "tipos_con_datos": tipos_con_datos,
                "presentes_global": presentes_global,
                "pct_presente_global": pct_presente_global,
                "frecuentes_globales": frecuentes,
                "por_tipo": por_tipo,
            }
            confianza = confianza_relacion(total_registros, tipos_con_datos, tipos_total, pct_presente_global)
            interes_score = score_relacion_presencia(col, evidencia)
            nivel_interes = nivel_interes_relacion(interes_score)
            categoria = "relacion_interesante" if nivel_interes in ("alta", "media") else "relacion_basica"
            descripcion = descripcion_presencia_interpretable(col, evidencia)
            cur_lab.execute("""
                INSERT INTO ia_relaciones_columnas (
                    tabla, columna_a, columna_b, tipo_relacion,
                    descripcion, evidencia_json, confianza, interes_score, nivel_interes, categoria, estado
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'inferido')
                ON DUPLICATE KEY UPDATE
                    descripcion = VALUES(descripcion),
                    evidencia_json = VALUES(evidencia_json),
                    confianza = VALUES(confianza),
                    interes_score = VALUES(interes_score),
                    nivel_interes = VALUES(nivel_interes),
                    categoria = VALUES(categoria),
                    estado = VALUES(estado),
                    actualizado_en = CURRENT_TIMESTAMP
            """, (
                "TiposDeMovimiento", "TiposDeMovimiento", col, "presencia_por_tipo_movimiento",
                descripcion, json.dumps(evidencia, ensure_ascii=False, default=str), confianza,
                interes_score, nivel_interes, categoria,
            ))
            if interes_score >= 72:
                upsert_conocimiento_estructural(cur_lab, "TiposDeMovimiento", f"TiposDeMovimiento->{col}", descripcion, evidencia, confianza, interes_score, nivel_interes)
            guardadas += 1

        columnas_importantes = [
            c for c in ["Precio", "Maquina", "Proveedor", "Description", "Materiales", "Operador", "kilos", "kilosNetos", "Descuento2", "IF"]
            if c in nombres_columnas
        ]
        pares_guardados = 0
        for i, col_a in enumerate(columnas_importantes):
            for col_b in columnas_importantes[i + 1:]:
                tipo_a = tipos_por_columna.get(col_a)
                tipo_b = tipos_por_columna.get(col_b)
                pres_a = expr_presente_sql(col_a, tipo_a)
                pres_b = expr_presente_sql(col_b, tipo_b)
                cur_op.execute(f"""
                    SELECT
                        TiposDeMovimiento AS tipo_movimiento,
                        COUNT(*) AS registros,
                        SUM(CASE WHEN {pres_a} AND {pres_b} THEN 1 ELSE 0 END) AS ambos_presentes,
                        SUM(CASE WHEN {pres_a} AND NOT ({pres_b}) THEN 1 ELSE 0 END) AS solo_a,
                        SUM(CASE WHEN NOT ({pres_a}) AND {pres_b} THEN 1 ELSE 0 END) AS solo_b,
                        SUM(CASE WHEN NOT ({pres_a}) AND NOT ({pres_b}) THEN 1 ELSE 0 END) AS ninguno,
                        ROUND(AVG(CASE WHEN {pres_a} AND {pres_b} THEN 1 ELSE 0 END) * 100, 2) AS pct_ambos_presentes
                    FROM TiposDeMovimiento
                    GROUP BY TiposDeMovimiento
                    ORDER BY registros DESC
                    LIMIT 80
                """)
                por_tipo = cur_op.fetchall() or []
                ambos_global = sum(int(x.get("ambos_presentes") or 0) for x in por_tipo)
                pct_ambos_global = round((ambos_global * 100 / total_registros), 2) if total_registros else 0
                evidencia = {
                    "tabla": "TiposDeMovimiento",
                    "eje": "TiposDeMovimiento",
                    "columna_a": col_a,
                    "columna_b": col_b,
                    "total_registros": total_registros,
                    "ambos_presentes_global": ambos_global,
                    "pct_ambos_presentes_global": pct_ambos_global,
                    "por_tipo": por_tipo,
                }
                top = sorted(por_tipo, key=lambda x: (x.get("pct_ambos_presentes") or 0, x.get("registros") or 0), reverse=True)[:4]
                top_txt = ", ".join([f"{x.get('tipo_movimiento')}: {x.get('pct_ambos_presentes')}%" for x in top if (x.get("pct_ambos_presentes") or 0) > 0])
                descripcion = descripcion_co_presencia_interpretable(col_a, col_b, evidencia)
                confianza = confianza_relacion(total_registros, len([x for x in por_tipo if int(x.get("ambos_presentes") or 0) > 0]), tipos_total, pct_ambos_global)
                interes_score = score_relacion_co_presencia(col_a, col_b, evidencia)
                nivel_interes = nivel_interes_relacion(interes_score)
                categoria = "relacion_interesante" if nivel_interes in ("alta", "media") else "relacion_basica"
                cur_lab.execute("""
                    INSERT INTO ia_relaciones_columnas (
                        tabla, columna_a, columna_b, tipo_relacion,
                        descripcion, evidencia_json, confianza, interes_score, nivel_interes, categoria, estado
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'inferido')
                    ON DUPLICATE KEY UPDATE
                        descripcion = VALUES(descripcion),
                        evidencia_json = VALUES(evidencia_json),
                        confianza = VALUES(confianza),
                        interes_score = VALUES(interes_score),
                        nivel_interes = VALUES(nivel_interes),
                        categoria = VALUES(categoria),
                        estado = VALUES(estado),
                        actualizado_en = CURRENT_TIMESTAMP
                """, (
                    "TiposDeMovimiento", col_a, col_b, "co_presencia_por_tipo_movimiento",
                    descripcion, json.dumps(evidencia, ensure_ascii=False, default=str), confianza,
                    interes_score, nivel_interes, categoria,
                ))
                if interes_score >= 72:
                    upsert_conocimiento_estructural(cur_lab, "TiposDeMovimiento", f"{col_a}<->{col_b}", descripcion, evidencia, confianza, interes_score, nivel_interes)
                pares_guardados += 1

        if "kilos" in nombres_columnas and "kilosNetos" in nombres_columnas:
            cur_op.execute("""
                SELECT
                    TiposDeMovimiento AS tipo_movimiento,
                    COUNT(*) AS registros,
                    SUM(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN 1 ELSE 0 END) AS comparables,
                    ROUND(AVG(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN kilos - kilosNetos ELSE NULL END), 3) AS diferencia_promedio,
                    ROUND(AVG(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN ABS(kilos - kilosNetos) ELSE NULL END), 3) AS diferencia_abs_promedio,
                    ROUND(MAX(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN ABS(kilos - kilosNetos) ELSE NULL END), 3) AS diferencia_abs_maxima
                FROM TiposDeMovimiento
                GROUP BY TiposDeMovimiento
                ORDER BY registros DESC
                LIMIT 80
            """)
            por_tipo = cur_op.fetchall() or []
            evidencia = {
                "tabla": "TiposDeMovimiento",
                "eje": "TiposDeMovimiento",
                "columna_a": "kilos",
                "columna_b": "kilosNetos",
                "total_registros": total_registros,
                "por_tipo": por_tipo,
            }
            comparables = sum(int(x.get("comparables") or 0) for x in por_tipo)
            pct = round((comparables * 100 / total_registros), 2) if total_registros else 0
            top = sorted(por_tipo, key=lambda x: x.get("diferencia_abs_promedio") or 0, reverse=True)[:4]
            top_txt = ", ".join([f"{x.get('tipo_movimiento')}: {x.get('diferencia_abs_promedio')}" for x in top if x.get("diferencia_abs_promedio") is not None])
            descripcion = f"kilos y kilosNetos son comparables en {pct}% de registros. Diferencia absoluta promedio mas visible: {top_txt or 'sin diferencias medibles'}."
            confianza = confianza_relacion(total_registros, len([x for x in por_tipo if int(x.get("comparables") or 0) > 0]), tipos_total, pct)
            interes_score = score_relacion_diferencia_kilos(evidencia)
            nivel_interes = nivel_interes_relacion(interes_score)
            categoria = "relacion_interesante" if nivel_interes in ("alta", "media") else "relacion_basica"
            cur_lab.execute("""
                INSERT INTO ia_relaciones_columnas (
                    tabla, columna_a, columna_b, tipo_relacion,
                    descripcion, evidencia_json, confianza, interes_score, nivel_interes, categoria, estado
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'inferido')
                ON DUPLICATE KEY UPDATE
                    descripcion = VALUES(descripcion),
                    evidencia_json = VALUES(evidencia_json),
                    confianza = VALUES(confianza),
                    interes_score = VALUES(interes_score),
                    nivel_interes = VALUES(nivel_interes),
                    categoria = VALUES(categoria),
                    estado = VALUES(estado),
                    actualizado_en = CURRENT_TIMESTAMP
            """, (
                "TiposDeMovimiento", "kilos", "kilosNetos", "diferencia_numerica_por_tipo_movimiento",
                descripcion, json.dumps(evidencia, ensure_ascii=False, default=str), confianza,
                interes_score, nivel_interes, categoria,
            ))
            if interes_score >= 72:
                upsert_conocimiento_estructural(cur_lab, "TiposDeMovimiento", "kilos<->kilosNetos", descripcion, evidencia, confianza, interes_score, nivel_interes)
            guardadas += 1

        cur_lab.execute("""
            SELECT descripcion, interes_score
            FROM ia_relaciones_columnas
            WHERE tabla = 'TiposDeMovimiento'
              AND COALESCE(interes_score, 0) >= 72
            ORDER BY interes_score DESC
            LIMIT 3
        """)
        top_interes = cur_lab.fetchall() or []
        if top_interes:
            add_reasoning_step(
                "relaciones_interesantes",
                "Detecte relaciones estructurales con alto interes.",
                " | ".join(shorten_text(x.get("descripcion"), 140) for x in top_interes)
            )

        conn_lab.commit()
        cur_op.close()
        conn_op.close()
        cur_lab.close()
        conn_lab.close()
        return {"ok": True, "relaciones": guardadas + pares_guardados, "columnas": guardadas, "co_presencias": pares_guardados, "interesantes_altas": len(top_interes)}
    except Exception as e:
        try:
            conn_lab.rollback()
            conn_lab.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}




COLUMNAS_CARTOGRAFIA_CLAVE = [
    "TiposDeMovimiento", "FechaHora", "Proveedor", "Materiales", "Description", "Operador", "Maquina",
    "kilos", "kilosNetos", "Descuento", "Descuento2", "Precio", "IF", "Cat10", "Cat11", "Cat12",
]
AUTO_CARTOGRAFIA_INTERVAL_SECONDS = int(os.environ.get("AUTO_CARTOGRAFIA_INTERVAL_SECONDS", "21600"))
AUTO_TEMA_COOLDOWN_HOURS = int(os.environ.get("AUTO_TEMA_COOLDOWN_HOURS", "18"))


def asegurar_tablas_cartografia(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_mapa_tablas (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            base_nombre VARCHAR(128) NOT NULL,
            tabla_nombre VARCHAR(128) NOT NULL,
            columna VARCHAR(128) NOT NULL,
            tipo_dato VARCHAR(128) NULL,
            ordinal_position INT NULL,
            evidencia_json JSON NULL,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_mapa_columna (base_nombre, tabla_nombre, columna)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_perfil_columnas (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tabla VARCHAR(128) NOT NULL,
            columna VARCHAR(128) NOT NULL,
            tipo_dato VARCHAR(128) NULL,
            total_filas BIGINT NULL,
            null_count BIGINT NULL,
            empty_count BIGINT NULL,
            pct_cargado DECIMAL(6,2) NULL,
            distinct_count BIGINT NULL,
            evidencia_json JSON NULL,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_perfil_columna (tabla, columna)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_perfil_tipos_movimiento (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tipo_movimiento VARCHAR(255) NOT NULL,
            registros BIGINT NULL,
            suma_kilos DECIMAL(18,3) NULL,
            evidencia_json JSON NULL,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_tipo_movimiento (tipo_movimiento)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_pendientes_estructurales (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            firma VARCHAR(255) NOT NULL,
            tema VARCHAR(255) NOT NULL,
            tarea TEXT NULL,
            prioridad INT DEFAULT 50,
            estado VARCHAR(64) DEFAULT 'pendiente',
            evidencia_json JSON NULL,
            ultimo_intento TIMESTAMP NULL,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_firma (firma)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_relaciones_columnas (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tabla VARCHAR(128) NOT NULL,
            columna_a VARCHAR(128) NOT NULL,
            columna_b VARCHAR(128) NOT NULL,
            tipo_relacion VARCHAR(128) NOT NULL,
            descripcion TEXT NULL,
            evidencia_json JSON NULL,
            confianza DECIMAL(5,3) NULL,
            interes_score DECIMAL(6,2) NULL,
            nivel_interes VARCHAR(32) NULL,
            categoria VARCHAR(64) NULL,
            estado VARCHAR(64) DEFAULT 'inferido',
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_relacion (tabla(64), columna_a(64), columna_b(64), tipo_relacion(64))
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_exploraciones_temas (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            firma VARCHAR(255) NOT NULL,
            tema VARCHAR(255) NOT NULL,
            resultado VARCHAR(64) NULL,
            evidencia_json JSON NULL,
            creada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_firma_fecha (firma, creada_en)
        ) CHARACTER SET utf8mb4
    """)


def columna_existe_en_lista(columnas, nombre):
    objetivo = str(nombre or "").lower()
    return any(str(c or "").lower() == objetivo for c in columnas or [])


def es_tipo_textual(tipo):
    return str(tipo or "").lower() in {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set"}


def es_tipo_temporal(tipo):
    return any(x in str(tipo or "").lower() for x in ["date", "time", "timestamp", "year"])


def sql_expr_cargado(col, tipo):
    q = quote_sql_identifier(col)
    if data_type_es_numerico(tipo) or es_tipo_temporal(tipo):
        return f"{q} IS NOT NULL"
    return f"{q} IS NOT NULL AND TRIM(CAST({q} AS CHAR)) <> ''"


def top_valores_columna(cur_op, columna, tipo, where_sql=None, limite=5):
    q = quote_sql_identifier(columna)
    filtro = f"WHERE {sql_expr_cargado(columna, tipo)}"
    if where_sql:
        filtro += f" AND ({where_sql})"
    try:
        cur_op.execute(f"""
            SELECT CAST({q} AS CHAR) AS valor, COUNT(*) AS cantidad
            FROM TiposDeMovimiento
            {filtro}
            GROUP BY {q}
            ORDER BY cantidad DESC
            LIMIT {int(limite)}
        """)
        return cur_op.fetchall() or []
    except Exception as e:
        return [{"error": str(e)}]


def ejemplos_columna(cur_op, columna, tipo, limite=5):
    q = quote_sql_identifier(columna)
    try:
        cur_op.execute(f"""
            SELECT DISTINCT CAST({q} AS CHAR) AS valor
            FROM TiposDeMovimiento
            WHERE {sql_expr_cargado(columna, tipo)}
            LIMIT {int(limite)}
        """)
        return cur_op.fetchall() or []
    except Exception as e:
        return [{"error": str(e)}]


def construir_ficha_kilos_vs_kilosnetos(cur_op, nombres_columnas, tipos_por_columna):
    if not (columna_existe_en_lista(nombres_columnas, "kilos") and columna_existe_en_lista(nombres_columnas, "kilosNetos")):
        return {}
    ficha = {}
    try:
        cur_op.execute("""
            SELECT
                COUNT(*) AS total_filas,
                SUM(CASE WHEN kilos IS NOT NULL THEN 1 ELSE 0 END) AS kilos_no_null,
                SUM(CASE WHEN kilosNetos IS NOT NULL THEN 1 ELSE 0 END) AS kilosnetos_no_null,
                SUM(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN 1 ELSE 0 END) AS comparables,
                AVG(CASE WHEN kilos IS NOT NULL THEN kilos ELSE NULL END) AS avg_kilos,
                AVG(CASE WHEN kilosNetos IS NOT NULL THEN kilosNetos ELSE NULL END) AS avg_kilosnetos,
                AVG(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN kilos - kilosNetos ELSE NULL END) AS avg_diferencia,
                AVG(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN ABS(kilos - kilosNetos) ELSE NULL END) AS avg_diferencia_abs
            FROM TiposDeMovimiento
        """)
        stats = cur_op.fetchone() or {}
        total = int(stats.get("total_filas") or 0)
        kn = int(stats.get("kilosnetos_no_null") or 0)
        stats["pct_kilosnetos_cargado"] = round(kn * 100 / total, 2) if total else 0
        ficha["stats"] = stats
    except Exception as e:
        ficha["stats_error"] = str(e)
    try:
        cur_op.execute("""
            SELECT TiposDeMovimiento AS tipo_movimiento,
                   COUNT(*) AS registros,
                   SUM(CASE WHEN kilosNetos IS NOT NULL THEN 1 ELSE 0 END) AS kilosnetos_cargados,
                   ROUND(SUM(CASE WHEN kilosNetos IS NOT NULL THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS pct_kilosnetos_cargado,
                   AVG(CASE WHEN kilos IS NOT NULL AND kilosNetos IS NOT NULL THEN kilos - kilosNetos ELSE NULL END) AS avg_diferencia
            FROM TiposDeMovimiento
            GROUP BY TiposDeMovimiento
            HAVING kilosnetos_cargados > 0
            ORDER BY kilosnetos_cargados DESC, pct_kilosnetos_cargado DESC
            LIMIT 8
        """)
        ficha["top_tipos_kilosnetos"] = cur_op.fetchall() or []
    except Exception as e:
        ficha["top_tipos_error"] = str(e)
    try:
        cur_op.execute("""
            SELECT FechaHora, TiposDeMovimiento, Proveedor, Maquina, Operador, Precio, Descuento, Descuento2, kilos, kilosNetos,
                   (kilos - kilosNetos) AS diferencia
            FROM TiposDeMovimiento
            WHERE kilos IS NOT NULL AND kilosNetos IS NOT NULL
            ORDER BY FechaHora DESC
            LIMIT 5
        """)
        ficha["ejemplos_comparables"] = cur_op.fetchall() or []
    except Exception as e:
        ficha["ejemplos_error"] = str(e)
    co_cols = [c for c in ["Proveedor", "Maquina", "Operador", "Precio", "Descuento", "Descuento2"] if columna_existe_en_lista(nombres_columnas, c)]
    ficha["co_presencia"] = {}
    for col in co_cols:
        try:
            expr = sql_expr_cargado(col, tipos_por_columna.get(col))
            cur_op.execute(f"""
                SELECT
                    SUM(CASE WHEN kilosNetos IS NOT NULL THEN 1 ELSE 0 END) AS kilosnetos_cargados,
                    SUM(CASE WHEN kilosNetos IS NOT NULL AND ({expr}) THEN 1 ELSE 0 END) AS ambos,
                    ROUND(SUM(CASE WHEN kilosNetos IS NOT NULL AND ({expr}) THEN 1 ELSE 0 END) * 100 / NULLIF(SUM(CASE WHEN kilosNetos IS NOT NULL THEN 1 ELSE 0 END), 0), 2) AS pct_con_kilosnetos
                FROM TiposDeMovimiento
            """)
            ficha["co_presencia"][col] = cur_op.fetchone() or {}
        except Exception as e:
            ficha["co_presencia"][col] = {"error": str(e)}
    return ficha

def upsert_pendiente_estructural(cur_lab, firma, tema, tarea, prioridad, evidencia):
    cur_lab.execute("""
        INSERT INTO ia_pendientes_estructurales (firma, tema, tarea, prioridad, estado, evidencia_json)
        VALUES (%s, %s, %s, %s, 'pendiente', %s)
        ON DUPLICATE KEY UPDATE
            tema=VALUES(tema), tarea=VALUES(tarea), prioridad=VALUES(prioridad),
            evidencia_json=VALUES(evidencia_json),
            estado=CASE WHEN estado IN ('resuelto', 'descartado') THEN estado ELSE 'pendiente' END,
            actualizado_en=CURRENT_TIMESTAMP
    """, (firma, tema, tarea, prioridad, json.dumps(evidencia or {}, ensure_ascii=False, default=str)))


def guardar_cartografia_base_si_corresponde(forzar=False):
    conn_lab = get_ia_connection()
    if not conn_lab:
        return {"ok": False, "error": "sin_conexion_lab"}
    try:
        cur_lab = conn_lab.cursor()
        asegurar_tablas_cartografia(cur_lab)
        if not forzar:
            cutoff = datetime.now() - timedelta(seconds=AUTO_CARTOGRAFIA_INTERVAL_SECONDS)
            cur_lab.execute("""
                SELECT COUNT(*) AS total
                FROM ia_perfil_columnas
                WHERE tabla='TiposDeMovimiento' AND actualizado_en >= %s
            """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),))
            reciente = cur_lab.fetchone() or {}
            if int(reciente.get("total") or 0) >= 10:
                cur_lab.close(); conn_lab.close()
                return {"ok": False, "skipped": "intervalo"}

        conn_op = get_db_connection()
        if not conn_op:
            cur_lab.close(); conn_lab.close()
            return {"ok": False, "error": "sin_conexion_operativa"}
        cur_op = conn_op.cursor()
        db_name = DB_CONFIG.get("database")

        cur_op.execute("""
            SELECT TABLE_NAME AS tabla_nombre
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME
        """, (db_name,))
        tablas = cur_op.fetchall() or []
        tablas_guardadas = 0
        for t in tablas:
            tabla = t.get("tabla_nombre")
            cur_op.execute("""
                SELECT COLUMN_NAME AS columna, DATA_TYPE AS tipo_dato, ORDINAL_POSITION AS ordinal_position
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                ORDER BY ORDINAL_POSITION
            """, (db_name, tabla))
            for col in cur_op.fetchall() or []:
                cur_lab.execute("""
                    INSERT INTO ia_mapa_tablas (base_nombre, tabla_nombre, columna, tipo_dato, ordinal_position, evidencia_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE tipo_dato=VALUES(tipo_dato), ordinal_position=VALUES(ordinal_position), evidencia_json=VALUES(evidencia_json), actualizado_en=CURRENT_TIMESTAMP
                """, (db_name, tabla, col.get("columna"), col.get("tipo_dato"), col.get("ordinal_position"), json.dumps({"fuente": "information_schema"}, ensure_ascii=False)))
                tablas_guardadas += 1

        cur_op.execute("""
            SELECT COLUMN_NAME AS columna, DATA_TYPE AS tipo_dato, ORDINAL_POSITION AS posicion
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME='TiposDeMovimiento'
            ORDER BY ORDINAL_POSITION
        """, (db_name,))
        columnas = cur_op.fetchall() or []
        nombres = [c.get("columna") for c in columnas if c.get("columna")]
        tipos = {c.get("columna"): c.get("tipo_dato") for c in columnas}
        cur_op.execute("SELECT COUNT(*) AS total FROM TiposDeMovimiento")
        total_filas = int((cur_op.fetchone() or {}).get("total") or 0)

        perfiles = []
        for col in columnas:
            nombre = col.get("columna")
            tipo = col.get("tipo_dato")
            q = quote_sql_identifier(nombre)
            try:
                cur_op.execute(f"""
                    SELECT
                        SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count,
                        SUM(CASE WHEN {q} IS NOT NULL AND TRIM(CAST({q} AS CHAR)) = '' THEN 1 ELSE 0 END) AS empty_count,
                        COUNT(DISTINCT {q}) AS distinct_count
                    FROM TiposDeMovimiento
                """)
                stats = cur_op.fetchone() or {}
            except Exception as e:
                stats = {"error": str(e)}
            null_count = int(stats.get("null_count") or 0) if not stats.get("error") else 0
            empty_count = int(stats.get("empty_count") or 0) if not stats.get("error") else 0
            cargados = max(0, total_filas - null_count - empty_count)
            pct_cargado = round(cargados * 100 / total_filas, 2) if total_filas else 0
            evidencia = {
                "posicion": col.get("posicion"),
                "total_filas": total_filas,
                "top_valores": top_valores_columna(cur_op, nombre, tipo),
                "ejemplos": ejemplos_columna(cur_op, nombre, tipo),
            }
            if stats.get("error"):
                evidencia["error"] = stats.get("error")
            try:
                if data_type_es_numerico(tipo):
                    cur_op.execute(f"SELECT MIN({q}) AS min, MAX({q}) AS max, AVG({q}) AS avg FROM TiposDeMovimiento WHERE {q} IS NOT NULL")
                    evidencia["numeric_stats"] = cur_op.fetchone() or {}
                elif es_tipo_temporal(tipo):
                    cur_op.execute(f"SELECT MIN({q}) AS min, MAX({q}) AS max FROM TiposDeMovimiento WHERE {q} IS NOT NULL")
                    evidencia["temporal_stats"] = cur_op.fetchone() or {}
            except Exception as e:
                evidencia["stats_error"] = str(e)
            distinct_count = int(stats.get("distinct_count") or 0) if not stats.get("error") else 0
            cur_lab.execute("""
                INSERT INTO ia_perfil_columnas (tabla, columna, tipo_dato, total_filas, null_count, empty_count, pct_cargado, distinct_count, evidencia_json)
                VALUES ('TiposDeMovimiento', %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE tipo_dato=VALUES(tipo_dato), total_filas=VALUES(total_filas), null_count=VALUES(null_count), empty_count=VALUES(empty_count), pct_cargado=VALUES(pct_cargado), distinct_count=VALUES(distinct_count), evidencia_json=VALUES(evidencia_json), actualizado_en=CURRENT_TIMESTAMP
            """, (nombre, tipo, total_filas, null_count, empty_count, pct_cargado, distinct_count, json.dumps(evidencia, ensure_ascii=False, default=str)))
            perfiles.append({"columna": nombre, "tipo_dato": tipo, "pct_cargado": pct_cargado, "distinct_count": distinct_count, "evidencia": evidencia})

        tipo_col = quote_sql_identifier("TiposDeMovimiento")
        kilos_expr = quote_sql_identifier("kilos") if columna_existe_en_lista(nombres, "kilos") else "0"
        cur_op.execute(f"""
            SELECT TiposDeMovimiento AS tipo_movimiento, COUNT(*) AS registros, SUM(COALESCE({kilos_expr},0)) AS suma_kilos
            FROM TiposDeMovimiento
            GROUP BY TiposDeMovimiento
            ORDER BY registros DESC
            LIMIT 40
        """)
        tipos_mov = cur_op.fetchall() or []
        for tm in tipos_mov:
            tipo_mov = tm.get("tipo_movimiento")
            where_tipo = f"{tipo_col} = '{str(tipo_mov).replace(chr(39), chr(39)+chr(39))}'" if tipo_mov is not None else f"{tipo_col} IS NULL"
            evidencia_tipo = {"columnas": {}, "top_valores": {}}
            for col in [c for c in COLUMNAS_CARTOGRAFIA_CLAVE if columna_existe_en_lista(nombres, c)]:
                tipo = tipos.get(col)
                expr = sql_expr_cargado(col, tipo)
                try:
                    cur_op.execute(f"SELECT COUNT(*) AS cargados FROM TiposDeMovimiento WHERE ({where_tipo}) AND ({expr})")
                    cargados = int((cur_op.fetchone() or {}).get("cargados") or 0)
                except Exception:
                    cargados = 0
                registros = int(tm.get("registros") or 0)
                evidencia_tipo["columnas"][col] = {"pct_cargado": round(cargados * 100 / registros, 2) if registros else 0, "cargados": cargados}
                if col not in ("TiposDeMovimiento", "FechaHora"):
                    evidencia_tipo["top_valores"][col] = top_valores_columna(cur_op, col, tipo, where_tipo, 5)
            cur_lab.execute("""
                INSERT INTO ia_perfil_tipos_movimiento (tipo_movimiento, registros, suma_kilos, evidencia_json)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE registros=VALUES(registros), suma_kilos=VALUES(suma_kilos), evidencia_json=VALUES(evidencia_json), actualizado_en=CURRENT_TIMESTAMP
            """, (str(tipo_mov), tm.get("registros"), tm.get("suma_kilos"), json.dumps(evidencia_tipo, ensure_ascii=False, default=str)))

        # Co-presencia deterministica entre columnas clave, sin duplicar relaciones existentes.
        claves_existentes = [c for c in COLUMNAS_CARTOGRAFIA_CLAVE if columna_existe_en_lista(nombres, c)]
        for i, a in enumerate(claves_existentes):
            for b in claves_existentes[i+1:]:
                if a == b:
                    continue
                expr_a = sql_expr_cargado(a, tipos.get(a))
                expr_b = sql_expr_cargado(b, tipos.get(b))
                try:
                    cur_op.execute(f"""
                        SELECT
                            SUM(CASE WHEN {expr_a} THEN 1 ELSE 0 END) AS a_cargada,
                            SUM(CASE WHEN {expr_b} THEN 1 ELSE 0 END) AS b_cargada,
                            SUM(CASE WHEN ({expr_a}) AND ({expr_b}) THEN 1 ELSE 0 END) AS ambos
                        FROM TiposDeMovimiento
                    """)
                    r = cur_op.fetchone() or {}
                    a_c = int(r.get("a_cargada") or 0); b_c = int(r.get("b_cargada") or 0); ambos = int(r.get("ambos") or 0)
                    evidencia = {"a_cargada": a_c, "b_cargada": b_c, "co_ocurrencias": ambos, "a_implica_b_pct": round(ambos*100/a_c,2) if a_c else 0, "b_implica_a_pct": round(ambos*100/b_c,2) if b_c else 0}
                    score = score_relacion_co_presencia(a, b, {"pct_ambos_presentes_global": round(ambos*100/total_filas,2) if total_filas else 0, "por_tipo": []})
                    cur_lab.execute("""
                        INSERT INTO ia_relaciones_columnas (tabla, columna_a, columna_b, tipo_relacion, descripcion, evidencia_json, confianza, interes_score, nivel_interes, categoria, estado)
                        VALUES ('TiposDeMovimiento', %s, %s, 'co_presencia_cartografia', %s, %s, %s, %s, %s, 'estructural', 'inferido')
                        ON DUPLICATE KEY UPDATE descripcion=VALUES(descripcion), evidencia_json=VALUES(evidencia_json), confianza=VALUES(confianza), interes_score=GREATEST(COALESCE(interes_score,0), VALUES(interes_score)), nivel_interes=VALUES(nivel_interes), actualizado_en=CURRENT_TIMESTAMP
                    """, (a, b, descripcion_co_presencia_interpretable(a, b, {"pct_ambos_presentes_global": evidencia["a_implica_b_pct"], "por_tipo": []}), json.dumps(evidencia, ensure_ascii=False, default=str), 0.62, score, nivel_interes_relacion(score)))
                except Exception:
                    pass

        contexto_temporal = construir_contexto_temporal_operativo(persistir=True)

        # Pendientes estructurales concretos para Ollama.
        perfiles_por_col = {p["columna"]: p for p in perfiles}
        for col in ["Precio", "Proveedor", "kilosNetos", "Maquina", "Operador", "Descuento2", "IF"]:
            if col in perfiles_por_col:
                upsert_pendiente_estructural(cur_lab, f"interpretar_columna:{col.lower()}", f"Interpretar columna {col}", f"Interpretar {col} usando perfil por TiposDeMovimiento, ejemplos reales, nulos/no nulos y co-presencia. No ejecutar SQL libre.", 80, {"perfil": perfiles_por_col[col]})
        if "kilos" in perfiles_por_col and "kilosNetos" in perfiles_por_col:
            ficha_kilos = construir_ficha_kilos_vs_kilosnetos(cur_op, nombres, tipos)
            upsert_pendiente_estructural(cur_lab, "comparar:kilos:kilosnetos", "Diferencia kilos vs kilosNetos", "Interpretar diferencia entre kilos y kilosNetos usando perfiles de carga, co-presencia, ejemplos reales y tipos de movimiento. No pedir SQL nuevo.", 88, {"columnas": [perfiles_por_col.get("kilos"), perfiles_por_col.get("kilosNetos")], "ficha_comparativa": ficha_kilos})
        upsert_pendiente_estructural(cur_lab, "exclusivas:produccion", "Columnas casi exclusivas de PRODUCCION", "Detectar columnas que aparecen principalmente en PRODUCCION usando perfil por TiposDeMovimiento, sin asumir causa funcional todavia.", 70, {"tipos_movimiento": tipos_mov[:10]})
        upsert_pendiente_estructural(cur_lab, "exclusivas:ingresos_despachos", "Columnas de ingresos/despachos", "Detectar columnas casi exclusivas de compras, ingresos o despachos usando presencia por TiposDeMovimiento.", 70, {"tipos_movimiento": tipos_mov[:10]})
        upsert_pendiente_estructural(cur_lab, "temporal:resumen_mes_cerrado_anterior", "Resumen mes cerrado anterior", "Interpretar el mes anterior como periodo cerrado y mas confiable que el mes actual abierto.", 76, {"contexto_temporal": contexto_temporal})
        upsert_pendiente_estructural(cur_lab, "temporal:produccion_hoy_parcial", "Produccion de hoy parcial", "Interpretar produccion de hoy como dato parcial porque el dia actual esta abierto.", 68, {"contexto_temporal": contexto_temporal})
        upsert_pendiente_estructural(cur_lab, "temporal:ingresos_hoy_parcial", "Ingresos de hoy parcial", "Interpretar ingresos de hoy como dato parcial porque el dia actual esta abierto.", 66, {"contexto_temporal": contexto_temporal})
        upsert_pendiente_estructural(cur_lab, "temporal:mes_actual_vs_anterior_parcial", "Mes actual vs mes anterior parcial", "Comparar mes actual abierto contra mes anterior cerrado aclarando que el mes actual es parcial.", 74, {"contexto_temporal": contexto_temporal})

        conn_lab.commit()
        cur_op.close(); conn_op.close(); cur_lab.close(); conn_lab.close()
        return {"ok": True, "tablas": len(tablas), "columnas": len(columnas), "tipos_movimiento": len(tipos_mov), "pendientes": 13}
    except Exception as e:
        try:
            conn_lab.rollback(); conn_lab.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def elegir_pendiente_estructural():
    conn = get_ia_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        asegurar_tablas_cartografia(cur)
        cutoff = datetime.now() - timedelta(hours=AUTO_TEMA_COOLDOWN_HOURS)
        cutoff_query_rechazada = datetime.now() - timedelta(minutes=30)
        cur.execute("""
            SELECT p.id, p.firma, p.tema, p.tarea, p.prioridad, p.evidencia_json
            FROM ia_pendientes_estructurales p
            LEFT JOIN (
                SELECT firma, MAX(creada_en) AS ultima
                FROM ia_exploraciones_temas
                GROUP BY firma
            ) e ON e.firma = p.firma
            LEFT JOIN ia_exploraciones_temas er ON er.firma = p.firma AND er.creada_en = e.ultima
            WHERE COALESCE(p.estado, 'pendiente') = 'pendiente'
              AND (
                  e.ultima IS NULL
                  OR (er.resultado = 'query_rechazada' AND e.ultima < %s)
                  OR (COALESCE(er.resultado, '') <> 'query_rechazada' AND e.ultima < %s)
              )
            ORDER BY p.prioridad DESC, p.actualizado_en ASC
            LIMIT 1
        """, (cutoff_query_rechazada.strftime("%Y-%m-%d %H:%M:%S"), cutoff.strftime("%Y-%m-%d %H:%M:%S")))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return None
        try:
            row["evidencia"] = json.loads(row.get("evidencia_json")) if isinstance(row.get("evidencia_json"), str) else row.get("evidencia_json")
        except Exception:
            row["evidencia"] = None
        return row
    except Exception:
        try: conn.close()
        except Exception: pass
        return None


def registrar_exploracion_tema(firma, tema, resultado, evidencia=None):
    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_lab"}
    try:
        cur = conn.cursor()
        asegurar_tablas_cartografia(cur)
        cur.execute("""
            INSERT INTO ia_exploraciones_temas (firma, tema, resultado, evidencia_json)
            VALUES (%s, %s, %s, %s)
        """, (firma, tema, resultado, json.dumps(evidencia or {}, ensure_ascii=False, default=str)))
        cur.execute("""
            UPDATE ia_pendientes_estructurales
            SET ultimo_intento=CURRENT_TIMESTAMP
            WHERE firma=%s
        """, (firma,))
        conn.commit(); cur.close(); conn.close()
        return {"ok": True}
    except Exception as e:
        try: conn.rollback(); conn.close()
        except Exception: pass
        return {"ok": False, "error": str(e)}



MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def fin_de_mes(dt):
    if dt.month == 12:
        siguiente = datetime(dt.year + 1, 1, 1)
    else:
        siguiente = datetime(dt.year, dt.month + 1, 1)
    return siguiente - timedelta(microseconds=1)


def iso_dt(valor):
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.isoformat(sep="T", timespec="seconds")
    return str(valor)


def guardar_contexto_temporal_operativo(contexto):
    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_lab"}
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ia_contexto_temporal (
                id INT PRIMARY KEY,
                contexto_json JSON NULL,
                ahora_local DATETIME NULL,
                fecha_corte_datos DATETIME NULL,
                estado_actualizacion VARCHAR(64) NULL,
                actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4
        """)
        cur.execute("""
            INSERT INTO ia_contexto_temporal (id, contexto_json, ahora_local, fecha_corte_datos, estado_actualizacion)
            VALUES (1, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE contexto_json=VALUES(contexto_json), ahora_local=VALUES(ahora_local), fecha_corte_datos=VALUES(fecha_corte_datos), estado_actualizacion=VALUES(estado_actualizacion), actualizado_en=CURRENT_TIMESTAMP
        """, (
            json.dumps(contexto, ensure_ascii=False, default=str),
            str(contexto.get("ahora_local") or "").replace("T", " ") or None,
            str(contexto.get("fecha_corte_datos") or "").replace("T", " ") or None,
            contexto.get("estado_actualizacion"),
        ))
        conn.commit(); cur.close(); conn.close()
        return {"ok": True}
    except Exception as e:
        try: conn.rollback(); conn.close()
        except Exception: pass
        return {"ok": False, "error": str(e)}


def construir_contexto_temporal_operativo(persistir=True):
    ahora = datetime.now()
    hoy = ahora.date()
    inicio_dia = datetime(hoy.year, hoy.month, hoy.day)
    fin_dia = inicio_dia + timedelta(days=1) - timedelta(microseconds=1)
    inicio_semana = inicio_dia - timedelta(days=inicio_dia.weekday())
    fin_semana = inicio_semana + timedelta(days=7) - timedelta(microseconds=1)
    inicio_mes_actual = datetime(hoy.year, hoy.month, 1)
    fin_mes_actual = fin_de_mes(inicio_mes_actual)
    if hoy.month == 1:
        inicio_mes_anterior = datetime(hoy.year - 1, 12, 1)
    else:
        inicio_mes_anterior = datetime(hoy.year, hoy.month - 1, 1)
    fin_mes_anterior = inicio_mes_actual - timedelta(microseconds=1)

    fecha_corte = None
    corte_error = None
    conn_op = get_db_connection()
    if conn_op:
        try:
            cur_op = conn_op.cursor()
            cur_op.execute("""
                SELECT MAX(FechaHora) AS fecha_corte
                FROM TiposDeMovimiento
                WHERE FechaHora IS NOT NULL
                  AND FechaHora >= '2020-01-01'
                  AND FechaHora <= DATE_ADD(NOW(), INTERVAL 1 DAY)
            """)
            row = cur_op.fetchone() or {}
            fecha_corte = row.get("fecha_corte")
            cur_op.close(); conn_op.close()
        except Exception as e:
            corte_error = str(e)
            try: conn_op.close()
            except Exception: pass
    else:
        corte_error = "sin_conexion_operativa"

    atraso_minutos = None
    estado = "sin_datos_recientes"
    if isinstance(fecha_corte, datetime):
        atraso_minutos = round((ahora - fecha_corte).total_seconds() / 60, 1)
        if atraso_minutos <= 180:
            estado = "al_dia"
        elif atraso_minutos <= 1440:
            estado = "demorado"
        else:
            estado = "sin_datos_recientes"

    contexto = {
        "ahora_local": iso_dt(ahora),
        "fecha_hoy": hoy.isoformat(),
        "anio_actual": hoy.year,
        "mes_actual_numero": hoy.month,
        "mes_actual_nombre": MESES_ES[hoy.month - 1],
        "inicio_dia": iso_dt(inicio_dia),
        "fin_dia": iso_dt(fin_dia),
        "inicio_semana": iso_dt(inicio_semana),
        "fin_semana": iso_dt(fin_semana),
        "inicio_mes_actual": iso_dt(inicio_mes_actual),
        "fin_mes_actual": iso_dt(fin_mes_actual),
        "inicio_mes_anterior": iso_dt(inicio_mes_anterior),
        "fin_mes_anterior": iso_dt(fin_mes_anterior),
        "mes_actual_abierto": True,
        "mes_anterior_cerrado": True,
        "dia_hoy_abierto": True,
        "ayer_cerrado": True,
        "semana_actual_abierta": True,
        "fecha_corte_datos": iso_dt(fecha_corte),
        "datos_actualizados_hasta": iso_dt(fecha_corte),
        "atraso_minutos": atraso_minutos,
        "estado_actualizacion": estado,
    }
    if corte_error:
        contexto["fecha_corte_error"] = corte_error
    if persistir:
        guardar_contexto_temporal_operativo(contexto)
    return contexto


def contexto_temporal_prompt_compacto(contexto):
    if not contexto:
        return "CONTEXTO TEMPORAL: no disponible."
    return (
        "CONTEXTO TEMPORAL:\n"
        f"Hoy es {contexto.get('fecha_hoy')}. Estamos en {contexto.get('mes_actual_nombre')} {contexto.get('anio_actual')}.\n"
        f"Mes actual abierto: {contexto.get('mes_actual_abierto')}. Mes anterior cerrado: {contexto.get('mes_anterior_cerrado')}.\n"
        f"Dia de hoy abierto: {contexto.get('dia_hoy_abierto')}. Ayer cerrado: {contexto.get('ayer_cerrado')}. Semana actual abierta: {contexto.get('semana_actual_abierta')}.\n"
        f"Datos hasta FechaHora={contexto.get('datos_actualizados_hasta')}; atraso_minutos={contexto.get('atraso_minutos')}; estado={contexto.get('estado_actualizacion')}.\n"
        "No trates el mes actual ni hoy como periodos cerradas. Para verdades mensuales cerradas, preferir mes anterior o meses terminados."
    )


VERDAD_PERIODO_MESES_RECIENTES = int(os.environ.get("VERDAD_PERIODO_MESES_RECIENTES", "24"))
VERDAD_PERIODO_MESES_VIEJOS = int(os.environ.get("VERDAD_PERIODO_MESES_VIEJOS", "48"))
VERDAD_PERIODO_MAX_RETROCESO = int(os.environ.get("VERDAD_PERIODO_MAX_RETROCESO_MESES", "40"))


def calcular_confianza_temporal(periodo_inicio):
    """La tabla se fue endureciendo con el tiempo: los registros recientes son
    mas confiables que los viejos (mas de ~2 anios, carga mas laxa). Se usa la
    antiguedad del periodo como proxy de confiabilidad del dato."""
    if not periodo_inicio:
        return 0.5
    if isinstance(periodo_inicio, str):
        try:
            periodo_inicio = datetime.fromisoformat(periodo_inicio[:10])
        except Exception:
            return 0.5
    ahora = datetime.now()
    meses_atras = (ahora.year - periodo_inicio.year) * 12 + (ahora.month - periodo_inicio.month)
    if meses_atras <= VERDAD_PERIODO_MESES_RECIENTES:
        return 0.95
    if meses_atras <= VERDAD_PERIODO_MESES_VIEJOS:
        return 0.75
    return 0.5


def ensure_ia_verdades_periodo(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_verdades_periodo (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            periodo_tipo VARCHAR(16) NOT NULL DEFAULT 'mes',
            periodo_inicio DATE NOT NULL,
            periodo_fin DATE NOT NULL,
            periodo_label VARCHAR(32) NOT NULL,
            metrica VARCHAR(128) NOT NULL,
            valor DECIMAL(18,3) NULL,
            unidad VARCHAR(32) NULL,
            confianza_dato DECIMAL(5,3) NULL,
            confianza_temporal DECIMAL(5,3) NULL,
            fuente_query TEXT NULL,
            origen VARCHAR(64) DEFAULT 'explorador_fase2',
            confirmado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            vigente TINYINT DEFAULT 1,
            UNIQUE KEY uk_periodo_metrica (periodo_tipo, periodo_inicio, metrica(100))
        ) CHARACTER SET utf8mb4
    """)


DERIVADAS_VERSION = "derivadas_v1"
# Cola de curiosidad: límite de profundidad de propagación (protagonistas ->
# materiales -> proveedores dominantes -> ...) y kg histórico mínimo para que
# un proveedor dominante descubierto amerite estudio.
MAX_COLA_PROFUNDIDAD = int(os.environ.get("MAX_COLA_PROFUNDIDAD", "3"))
COLA_KG_MINIMO_DOMINANTE = float(os.environ.get("COLA_KG_MINIMO_DOMINANTE", "500"))
# Cola por cobertura mensual: proveedores que explican hasta este % de los
# ingresos de cada mes cerrado, con tope de proveedores por mes.
COBERTURA_PROVEEDORES_PCT = float(os.environ.get("COBERTURA_PROVEEDORES_PCT", "80"))
COBERTURA_PROVEEDORES_MAX_POR_MES = int(os.environ.get("COBERTURA_PROVEEDORES_MAX_POR_MES", "20"))


def _ensure_tablas_derivadas(cur):
    """Crea las tablas derivadas en crowdbot_lab si no existen."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_periodos_procesados (
            id INT AUTO_INCREMENT PRIMARY KEY,
            periodo VARCHAR(7) NOT NULL COMMENT 'YYYY-MM',
            estado_periodo VARCHAR(16) NOT NULL COMMENT 'abierto o cerrado',
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            filas_tipo_movimiento INT NULL,
            filas_produccion_maquina INT NULL,
            filas_ingresos_proveedor INT NULL,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_periodo_version (periodo, criterio_version)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_resumen_mensual_tipo_movimiento (
            id INT AUTO_INCREMENT PRIMARY KEY,
            periodo VARCHAR(7) NOT NULL,
            estado_periodo VARCHAR(16) NOT NULL,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            TiposDeMovimiento VARCHAR(120) NOT NULL,
            registros INT NOT NULL DEFAULT 0,
            kilos_total DECIMAL(16,2) NOT NULL DEFAULT 0,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_periodo_tipo (periodo, TiposDeMovimiento, criterio_version)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_resumen_mensual_produccion_maquina (
            id INT AUTO_INCREMENT PRIMARY KEY,
            periodo VARCHAR(7) NOT NULL,
            estado_periodo VARCHAR(16) NOT NULL,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            Maquina VARCHAR(120) NOT NULL,
            registros INT NOT NULL DEFAULT 0,
            kilos_total DECIMAL(16,2) NOT NULL DEFAULT 0,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_periodo_maquina (periodo, Maquina, criterio_version)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_resumen_mensual_ingresos_proveedor (
            id INT AUTO_INCREMENT PRIMARY KEY,
            periodo VARCHAR(7) NOT NULL,
            estado_periodo VARCHAR(16) NOT NULL,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            Proveedor VARCHAR(120) NOT NULL,
            registros INT NOT NULL DEFAULT 0,
            kilos_total DECIMAL(16,2) NOT NULL DEFAULT 0,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_periodo_proveedor (periodo, Proveedor, criterio_version)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_resumen_mensual_description_normalizada (
            id INT AUTO_INCREMENT PRIMARY KEY,
            periodo VARCHAR(7) NOT NULL,
            estado_periodo VARCHAR(16) NOT NULL,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            description_normalizada VARCHAR(255) NOT NULL,
            registros_total_periodo INT NOT NULL DEFAULT 0,
            registros_normalizados_periodo INT NOT NULL DEFAULT 0,
            cobertura_pct_periodo DECIMAL(8,2) NOT NULL DEFAULT 0,
            estado_confianza VARCHAR(16) NOT NULL DEFAULT 'debil' COMMENT 'fuerte >=90 | parcial 30-90 | debil <30 (no usar para conclusiones fuertes)',
            registros INT NOT NULL DEFAULT 0,
            kilos_total DECIMAL(16,2) NOT NULL DEFAULT 0,
            pct_kilos_normalizados_periodo DECIMAL(8,2) NOT NULL DEFAULT 0,
            primer_movimiento DATETIME NULL,
            ultimo_movimiento DATETIME NULL,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_periodo_desc_norm (periodo, description_normalizada, criterio_version)
        ) CHARACTER SET utf8mb4
    """)
    # Tablas de normalidad (calculadas sólo sobre meses cerrados)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_normalidad_mensual_tipo_movimiento (
            id INT AUTO_INCREMENT PRIMARY KEY,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            entidad VARCHAR(120) NOT NULL COMMENT 'TiposDeMovimiento',
            meses_con_actividad INT NOT NULL DEFAULT 0,
            kg_promedio_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_min_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_max_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            registros_promedio_mensual DECIMAL(10,2) NOT NULL DEFAULT 0,
            primer_periodo VARCHAR(7) NULL,
            ultimo_periodo VARCHAR(7) NULL,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_norm_tipo (criterio_version, entidad)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_normalidad_mensual_produccion_maquina (
            id INT AUTO_INCREMENT PRIMARY KEY,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            entidad VARCHAR(120) NOT NULL COMMENT 'Maquina',
            meses_con_actividad INT NOT NULL DEFAULT 0,
            kg_promedio_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_min_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_max_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            registros_promedio_mensual DECIMAL(10,2) NOT NULL DEFAULT 0,
            primer_periodo VARCHAR(7) NULL,
            ultimo_periodo VARCHAR(7) NULL,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_norm_maq (criterio_version, entidad)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_normalidad_mensual_ingresos_proveedor (
            id INT AUTO_INCREMENT PRIMARY KEY,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            entidad VARCHAR(120) NOT NULL COMMENT 'Proveedor',
            meses_con_actividad INT NOT NULL DEFAULT 0,
            kg_promedio_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_min_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_max_mensual DECIMAL(16,2) NOT NULL DEFAULT 0,
            registros_promedio_mensual DECIMAL(10,2) NOT NULL DEFAULT 0,
            primer_periodo VARCHAR(7) NULL,
            ultimo_periodo VARCHAR(7) NULL,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_norm_prov (criterio_version, entidad)
        ) CHARACTER SET utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_biografia_protagonistas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            tipo_entidad VARCHAR(32) NOT NULL COMMENT 'proveedor | proveedor_material',
            entidad VARCHAR(200) NOT NULL COMMENT 'proveedor, o material si tipo_entidad=proveedor_material',
            entidad_2 VARCHAR(200) NOT NULL DEFAULT '' COMMENT 'proveedor padre si tipo_entidad=proveedor_material; vacío para proveedor (NULL rompe la UNIQUE)',
            periodo_actual VARCHAR(7) NOT NULL,
            kg_actual DECIMAL(16,2) NOT NULL DEFAULT 0,
            pct_actual_contexto DECIMAL(8,2) NOT NULL DEFAULT 0,
            meses_con_actividad INT NOT NULL DEFAULT 0,
            kg_promedio_meses_activos DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_min_mes_activo DECIMAL(16,2) NOT NULL DEFAULT 0,
            kg_max_mes_activo DECIMAL(16,2) NOT NULL DEFAULT 0,
            pct_promedio_meses_activos DECIMAL(8,2) NOT NULL DEFAULT 0,
            ultimo_mes_con_actividad VARCHAR(7) NULL,
            meses_base_total INT NOT NULL DEFAULT 0,
            confiabilidad VARCHAR(24) NOT NULL DEFAULT 'sin_historia',
            historial_json TEXT NULL,
            lectura_backend TEXT NULL,
            prioridad_score DECIMAL(10,4) NOT NULL DEFAULT 0,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_bio (criterio_version, tipo_entidad, entidad, entidad_2, periodo_actual)
        ) CHARACTER SET utf8mb4
    """)
    # Migración defensiva: entidad_2 NULL en biografías de proveedor rompía la
    # UNIQUE uk_bio (MySQL permite múltiples NULL en claves únicas) y duplicaba
    # filas en cada recálculo. Dedupe dejando la fila de mayor id, luego NULL -> ''.
    try:
        cur.execute("""
            DELETE b1 FROM ia_biografia_protagonistas b1
            JOIN ia_biografia_protagonistas b2
              ON b1.criterio_version = b2.criterio_version
             AND b1.tipo_entidad = b2.tipo_entidad
             AND b1.entidad = b2.entidad
             AND COALESCE(b1.entidad_2, '') = COALESCE(b2.entidad_2, '')
             AND b1.periodo_actual = b2.periodo_actual
             AND b1.id < b2.id
        """)
        cur.execute("UPDATE ia_biografia_protagonistas SET entidad_2 = '' WHERE entidad_2 IS NULL")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE ia_biografia_protagonistas MODIFY entidad_2 VARCHAR(200) NOT NULL DEFAULT '' COMMENT 'proveedor padre si tipo_entidad=proveedor_material; vacío para proveedor'")
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_protagonistas_operativos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            origen_tipo VARCHAR(48) NOT NULL DEFAULT '' COMMENT 'periodo | periodo_tipo_movimiento | proveedor_periodo | material_periodo | maquina_periodo',
            origen_entidad VARCHAR(300) NOT NULL DEFAULT '' COMMENT 'texto legible del contexto padre, ej: 2026-05 ingresos',
            tipo_entidad VARCHAR(32) NOT NULL COMMENT 'proveedor | material | proveedor_material | maquina | tipo_movimiento',
            entidad VARCHAR(200) NOT NULL COMMENT 'nombre de la entidad; en nivel 2 es el material',
            entidad_2 VARCHAR(200) NULL COMMENT 'proveedor padre en nivel 2',
            periodo VARCHAR(7) NOT NULL,
            estado_periodo VARCHAR(16) NOT NULL,
            criterio_version VARCHAR(32) NOT NULL DEFAULT 'derivadas_v1',
            nivel TINYINT NOT NULL DEFAULT 1 COMMENT '1=directo del periodo, 2=drill de un nivel1, 3=drill de nivel2',
            kg_contexto_total DECIMAL(16,2) NOT NULL DEFAULT 0 COMMENT 'kg totales del contexto padre',
            kg_entidad DECIMAL(16,2) NOT NULL DEFAULT 0,
            pct_del_contexto DECIMAL(8,2) NOT NULL DEFAULT 0,
            registros INT NOT NULL DEFAULT 0,
            prioridad_score DECIMAL(10,4) NOT NULL DEFAULT 0,
            motivos_json TEXT NULL,
            siguiente_microtarea VARCHAR(120) NULL,
            fuente VARCHAR(80) NOT NULL DEFAULT 'TiposDeMovimiento',
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_protagonista (origen_tipo, tipo_entidad, entidad, entidad_2, periodo, criterio_version)
        ) CHARACTER SET utf8mb4
    """)
    # Migración compatible: agregar columnas nuevas si la tabla ya existía sin ellas
    for _col, _ddl in [
        ("origen_tipo",       "VARCHAR(48) NOT NULL DEFAULT ''"),
        ("origen_entidad",    "VARCHAR(300) NOT NULL DEFAULT ''"),
        ("nivel",             "TINYINT NOT NULL DEFAULT 1"),
        ("kg_contexto_total", "DECIMAL(16,2) NOT NULL DEFAULT 0"),
        ("kg_entidad",        "DECIMAL(16,2) NOT NULL DEFAULT 0"),
        ("pct_del_contexto",  "DECIMAL(8,2) NOT NULL DEFAULT 0"),
        ("registros",         "INT NOT NULL DEFAULT 0"),
        ("siguiente_microtarea", "VARCHAR(120) NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE ia_protagonistas_operativos ADD COLUMN IF NOT EXISTS {_col} {_ddl}")
        except Exception:
            pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_cola_curiosidad (
            id INT AUTO_INCREMENT PRIMARY KEY,
            tipo_tarea VARCHAR(64) NOT NULL COMMENT 'perfil_proveedor_historico | ranking_vecinos_proveedor_mes | perfil_proveedor_material_historico | perfil_material_global',
            tipo_nodo VARCHAR(32) NOT NULL COMMENT 'proveedor | proveedor_material | material',
            entidad VARCHAR(200) NOT NULL COMMENT 'proveedor, o material si tipo_nodo=proveedor_material/material',
            entidad_2 VARCHAR(200) NULL COMMENT 'proveedor padre si tipo_nodo=proveedor_material',
            material_fuente VARCHAR(32) NOT NULL DEFAULT 'Description' COMMENT 'Description | DescriptionNormalizada (no mezclar entidades entre fuentes)',
            periodo VARCHAR(7) NOT NULL,
            origen VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'protagonista_nivel1 | biografia_proveedor | protagonista_nivel2',
            motivo VARCHAR(300) NULL,
            prioridad_score DECIMAL(10,4) NOT NULL DEFAULT 0,
            profundidad TINYINT NOT NULL DEFAULT 1,
            estado VARCHAR(16) NOT NULL DEFAULT 'pendiente' COMMENT 'pendiente | en_proceso | completada | descartada | error',
            resultado_resumen VARCHAR(300) NULL,
            cooldown_hasta DATETIME NULL,
            creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_cola_estado (estado, prioridad_score),
            KEY idx_cola_dedup (tipo_tarea, entidad, entidad_2, periodo)
        ) CHARACTER SET utf8mb4
    """)
    try:
        cur.execute("ALTER TABLE ia_cola_curiosidad ADD COLUMN IF NOT EXISTS resultado_resumen VARCHAR(300) NULL")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE ia_cola_curiosidad ADD COLUMN IF NOT EXISTS material_fuente VARCHAR(32) NOT NULL DEFAULT 'Description'")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE ia_fichas_operativas ADD COLUMN IF NOT EXISTS material_fuente VARCHAR(32) NOT NULL DEFAULT 'Description'")
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_fichas_operativas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            cola_id INT NULL COMMENT 'id de la tarea en ia_cola_curiosidad que la generó',
            tipo_tarea VARCHAR(64) NOT NULL,
            tipo_nodo VARCHAR(32) NOT NULL COMMENT 'proveedor | proveedor_material | material',
            entidad VARCHAR(200) NOT NULL COMMENT 'proveedor, o material si tipo_nodo=proveedor_material/material',
            entidad_2 VARCHAR(200) NULL COMMENT 'proveedor padre si tipo_nodo=proveedor_material',
            material_fuente VARCHAR(32) NOT NULL DEFAULT 'Description' COMMENT 'Description | DescriptionNormalizada',
            periodo VARCHAR(7) NOT NULL,
            titulo VARCHAR(300) NOT NULL,
            resumen_deterministico TEXT NULL,
            datos_json TEXT NULL,
            senales_json TEXT NULL,
            prioridad_score DECIMAL(10,4) NOT NULL DEFAULT 0,
            creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_ficha_creado (creado_en),
            KEY idx_ficha_cola (cola_id)
        ) CHARACTER SET utf8mb4
    """)
    _ensure_alias_proveedores(cur)


def _ensure_alias_proveedores(cur):
    """Crea ia_alias_proveedores con su seed si no existe (idempotente)."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_alias_proveedores (
            proveedor_raw VARCHAR(200) NOT NULL PRIMARY KEY,
            proveedor_canonico VARCHAR(200) NOT NULL,
            confianza DECIMAL(5,2) NOT NULL DEFAULT 1.0,
            origen VARCHAR(40) NOT NULL DEFAULT 'manual',
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4
    """)
    # Seed inicial de alias conocidos (INSERT IGNORE = idempotente, editable a mano)
    for _raw, _canon in [
        ("Sandro mengar", "Sandro Melgar"),
        ("Sandro", "Sandro Melgar"),
        ("Sandro Melgar", "Sandro Melgar"),
    ]:
        try:
            cur.execute("""
                INSERT IGNORE INTO ia_alias_proveedores (proveedor_raw, proveedor_canonico, origen)
                VALUES (%s, %s, 'seed')
            """, (_raw, _canon))
        except Exception:
            pass


def cargar_alias_proveedores(cur_lab):
    """Devuelve (mapa, inverso) desde ia_alias_proveedores:
    mapa {proveedor_raw_lower: proveedor_canonico} para canonizar;
    inverso {canonico: [variantes crudas + canónico]} para queries IN."""
    mapa, inverso = {}, {}
    try:
        cur_lab.execute("SELECT proveedor_raw, proveedor_canonico FROM ia_alias_proveedores")
        for r in (cur_lab.fetchall() or []):
            raw = str(r["proveedor_raw"] or "").strip()
            canon = str(r["proveedor_canonico"] or "").strip()
            if not raw or not canon:
                continue
            mapa[raw.lower()] = canon
            inverso.setdefault(canon, set()).update([raw, canon])
    except Exception:
        pass
    return mapa, {c: sorted(v) for c, v in inverso.items()}


def canonizar_proveedor(nombre, mapa_alias):
    """Nombre canónico de un proveedor según ia_alias_proveedores."""
    if not nombre:
        return nombre
    limpio = str(nombre).strip()
    return (mapa_alias or {}).get(limpio.lower(), limpio)


def variantes_proveedor(canonico, inverso_alias):
    """Todas las variantes crudas de un proveedor canónico (incluido él mismo)."""
    return (inverso_alias or {}).get(canonico) or [canonico]


def _limpiar_artefactos_alias(cur_lab, mapa_alias):
    """Limpieza idempotente de artefactos derivados con nombres crudos que ya
    tienen alias: biografías y protagonistas del nombre crudo (se regeneran con
    el canónico en el mismo recálculo) y tareas pendientes de cola. Las fichas
    viejas se conservan como evidencia."""
    for raw_l, canon in (mapa_alias or {}).items():
        if raw_l == canon.lower():
            continue  # el canónico no se toca
        try:
            cur_lab.execute(
                "DELETE FROM ia_biografia_protagonistas WHERE entidad = %s OR entidad_2 = %s",
                (raw_l, raw_l))
            cur_lab.execute(
                "DELETE FROM ia_protagonistas_operativos WHERE entidad = %s OR entidad_2 = %s",
                (raw_l, raw_l))
            cur_lab.execute(
                "DELETE FROM ia_cola_curiosidad WHERE estado = 'pendiente' AND (entidad = %s OR entidad_2 = %s)",
                (raw_l, raw_l))
        except Exception:
            pass


def calcular_protagonistas_operativos(cur_op, cur_lab, periodo, estado_periodo):
    """Calcula protagonistas operativos de ingresos para el periodo dado.
    Nivel 1: top proveedores vs total ingresos del periodo.
    Nivel 2: top materiales por proveedor protagonista (mes actual + último cerrado).
    Limpia y recarga para ese periodo+version."""
    yr, mo = int(periodo[:4]), int(periodo[5:7])
    inicio = f"{periodo}-01"
    ultimo_dia = (date(yr, mo % 12 + 1, 1) - timedelta(days=1)) if mo < 12 else date(yr, 12, 31)
    fin_excl = (ultimo_dia + timedelta(days=1)).strftime("%Y-%m-%d")

    # Normalidad de proveedores para score
    norm_prov = {}
    try:
        cur_lab.execute("""
            SELECT entidad, meses_con_actividad
            FROM ia_normalidad_mensual_ingresos_proveedor
            WHERE criterio_version = %s
        """, (DERIVADAS_VERSION,))
        for r in (cur_lab.fetchall() or []):
            norm_prov[r["entidad"]] = int(r["meses_con_actividad"] or 0)
    except Exception:
        pass

    def _score(kg, registros, entidad=None, nivel=1):
        base = float(kg) * 0.7 + float(registros) * 10.0
        if entidad and norm_prov.get(entidad, 0) >= 6:
            base *= 1.15
        if nivel == 2:
            base *= 0.8  # nivel 2 compite solo contra otros nivel 2
        return round(base, 4)

    cur_lab.execute(
        "DELETE FROM ia_protagonistas_operativos WHERE periodo = %s AND criterio_version = %s",
        (periodo, DERIVADAS_VERSION)
    )
    filas = 0

    # --- NIVEL 1: total ingresos periodo ---
    cur_op.execute("""
        SELECT COALESCE(SUM(kilos), 0) AS kg_total, COUNT(*) AS reg_total
        FROM TiposDeMovimiento
        WHERE FechaHora >= %s AND FechaHora < %s
          AND TiposDeMovimiento LIKE 'ING %%'
    """, (inicio, fin_excl))
    tot = cur_op.fetchone() or {}
    kg_total = float(tot.get("kg_total") or 0)
    origen_entidad_n1 = f"{periodo} ingresos"

    cur_op.execute("""
        SELECT Proveedor AS entidad,
               COUNT(*) AS registros,
               COALESCE(SUM(kilos), 0) AS kg
        FROM TiposDeMovimiento
        WHERE FechaHora >= %s AND FechaHora < %s
          AND TiposDeMovimiento LIKE 'ING %%'
          AND Proveedor IS NOT NULL AND TRIM(Proveedor) <> ''
        GROUP BY Proveedor
        ORDER BY kg DESC LIMIT 40
    """, (inicio, fin_excl))
    # Canonizar por alias y re-agrupar antes de rankear
    mapa_alias, inverso_alias = cargar_alias_proveedores(cur_lab)
    _agr_n1 = {}
    for r in (cur_op.fetchall() or []):
        canon = canonizar_proveedor(r["entidad"], mapa_alias)
        acc = _agr_n1.setdefault(canon, {"entidad": canon, "registros": 0, "kg": 0.0})
        acc["registros"] += int(r["registros"] or 0)
        acc["kg"] += float(r["kg"] or 0)
    nivel1_provs = sorted(_agr_n1.values(), key=lambda x: -x["kg"])[:20]

    for r in nivel1_provs:
        kg = float(r["kg"] or 0)
        pct = round(kg / kg_total * 100, 2) if kg_total > 0 else 0
        motivos = json.dumps({
            "explica": f"{pct}% de ingresos del periodo",
            "kg_total_periodo": kg_total,
        }, ensure_ascii=False)
        cur_lab.execute("""
            INSERT INTO ia_protagonistas_operativos
                (origen_tipo, origen_entidad, tipo_entidad, entidad, entidad_2,
                 periodo, estado_periodo, criterio_version, nivel,
                 kg_contexto_total, kg_entidad, pct_del_contexto, registros,
                 prioridad_score, motivos_json, fuente)
            VALUES ('periodo_tipo_movimiento', %s, 'proveedor', %s, NULL,
                    %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, 'TiposDeMovimiento')
            ON DUPLICATE KEY UPDATE
                kg_contexto_total = VALUES(kg_contexto_total),
                kg_entidad = VALUES(kg_entidad),
                pct_del_contexto = VALUES(pct_del_contexto),
                registros = VALUES(registros),
                prioridad_score = VALUES(prioridad_score),
                motivos_json = VALUES(motivos_json),
                actualizado_en = CURRENT_TIMESTAMP
        """, (origen_entidad_n1, r["entidad"],
              periodo, estado_periodo, DERIVADAS_VERSION,
              kg_total, kg, pct, r["registros"],
              _score(kg, r["registros"], r["entidad"], nivel=1), motivos))
        filas += 1

    # --- NIVEL 2: materiales por proveedor protagonista ---
    # Fuente de materiales del periodo: normalizada si su cobertura lo permite.
    # No se mezclan fuentes en la misma corrida: o todas las entidades salen de
    # DescriptionNormalizada, o todas de Description/Materiales cruda.
    fuente_mat = fuente_material_para_periodo(cur_lab, periodo)

    # Tomar top proveedores nivel 1 (los que explican más del 5% o los top 10)
    provs_para_drill = [
        r["entidad"] for r in nivel1_provs[:10]
        if float(r["kg"] or 0) / kg_total * 100 >= 1.0
    ] if kg_total > 0 else []

    for proveedor in provs_para_drill:
        kg_prov = float(next(
            (r["kg"] for r in nivel1_provs if r["entidad"] == proveedor), 0
        ) or 0)
        origen_entidad_n2 = f"{proveedor} | {periodo} ingresos"

        # Incluir todas las variantes crudas del proveedor canónico
        variantes = variantes_proveedor(proveedor, inverso_alias)
        marcadores_var = ", ".join(["%s"] * len(variantes))
        if fuente_mat == "DescriptionNormalizada":
            cur_op.execute(f"""
                SELECT
                  DescriptionNormalizada AS entidad,
                  'DescriptionNormalizada' AS campo_usado,
                  COUNT(*) AS registros,
                  COALESCE(SUM(kilos), 0) AS kg
                FROM TiposDeMovimiento
                WHERE FechaHora >= %s AND FechaHora < %s
                  AND TiposDeMovimiento LIKE 'ING %%'
                  AND Proveedor IN ({marcadores_var})
                  AND DescriptionNormalizada IS NOT NULL AND TRIM(DescriptionNormalizada) <> ''
                GROUP BY entidad
                ORDER BY kg DESC LIMIT 10
            """, tuple([inicio, fin_excl] + variantes))
        else:
            cur_op.execute(f"""
                SELECT
                  CASE
                    WHEN Description IS NOT NULL AND TRIM(Description) <> '' THEN Description
                    ELSE Materiales
                  END AS entidad,
                  MAX(CASE
                    WHEN Description IS NOT NULL AND TRIM(Description) <> '' THEN 'Description'
                    ELSE 'Materiales'
                  END) AS campo_usado,
                  COUNT(*) AS registros,
                  COALESCE(SUM(kilos), 0) AS kg
                FROM TiposDeMovimiento
                WHERE FechaHora >= %s AND FechaHora < %s
                  AND TiposDeMovimiento LIKE 'ING %%'
                  AND Proveedor IN ({marcadores_var})
                  AND (
                    (Description IS NOT NULL AND TRIM(Description) <> '')
                    OR (Materiales IS NOT NULL AND TRIM(Materiales) <> '')
                  )
                GROUP BY entidad
                ORDER BY kg DESC LIMIT 10
            """, tuple([inicio, fin_excl] + variantes))
        mats = cur_op.fetchall() or []

        for rm in mats:
            kg_m = float(rm["kg"] or 0)
            pct_m = round(kg_m / kg_prov * 100, 2) if kg_prov > 0 else 0
            motivos = json.dumps({
                "proveedor_padre": proveedor,
                "campo_fuente": rm["campo_usado"],
                "explica": f"{pct_m}% de kilos del proveedor en el periodo",
            }, ensure_ascii=False)
            cur_lab.execute("""
                INSERT INTO ia_protagonistas_operativos
                    (origen_tipo, origen_entidad, tipo_entidad, entidad, entidad_2,
                     periodo, estado_periodo, criterio_version, nivel,
                     kg_contexto_total, kg_entidad, pct_del_contexto, registros,
                     prioridad_score, motivos_json, fuente)
                VALUES ('proveedor_periodo', %s, 'material', %s, %s,
                        %s, %s, %s, 2, %s, %s, %s, %s, %s, %s, 'TiposDeMovimiento')
                ON DUPLICATE KEY UPDATE
                    kg_contexto_total = VALUES(kg_contexto_total),
                    kg_entidad = VALUES(kg_entidad),
                    pct_del_contexto = VALUES(pct_del_contexto),
                    registros = VALUES(registros),
                    prioridad_score = VALUES(prioridad_score),
                    motivos_json = VALUES(motivos_json),
                    actualizado_en = CURRENT_TIMESTAMP
            """, (origen_entidad_n2, rm["entidad"], proveedor,
                  periodo, estado_periodo, DERIVADAS_VERSION,
                  kg_prov, kg_m, pct_m, rm["registros"],
                  _score(kg_m, rm["registros"], nivel=2), motivos))
            filas += 1

    return filas


def _confiabilidad_bio(n):
    n = int(n or 0)
    if n >= 6:  return "fuerte"
    if n >= 3:  return "media"
    if n == 2:  return "debil"
    if n == 1:  return "antecedente_unico"
    return "sin_historia"


def _lectura_bio(tipo_entidad, confiab, pct_actual, pct_promedio, meses):
    """Genera texto determinístico corto para lectura_backend."""
    if meses == 0:
        return "Sin historial previo en meses cerrados. Dato inicial."
    nuevo = meses <= 1
    if nuevo:
        return "Protagonista nuevo o casi nuevo; no usar como normalidad todavía."
    sup = pct_actual > pct_promedio * 1.15 if pct_promedio else False
    inf = pct_actual < pct_promedio * 0.85 if pct_promedio else False
    if tipo_entidad == "proveedor":
        base = "Proveedor protagonista actual"
        if confiab == "fuerte":
            base += "; historial fuerte"
        elif confiab == "media":
            base += "; historial moderado"
        else:
            base += "; historial débil, tratar como señal exploratoria"
        if sup:
            base += " y participación actual superior a su promedio histórico."
        elif inf:
            base += " pero participación actual inferior a su promedio histórico."
        else:
            base += " con participación dentro de su rango histórico."
        return base
    else:
        base = "Material dominante dentro del proveedor este mes"
        if confiab in ("fuerte", "media"):
            base += "; con historial consistente."
        else:
            base += "; historial débil, tratar como señal exploratoria."
        return base


def calcular_biografia_protagonistas(cur_op, cur_lab, periodo_actual):
    """Genera ficha histórica determinística para cada protagonista nivel1/nivel2.
    Usa ia_resumen_mensual_ingresos_proveedor (meses cerrados) como fuente.
    No llama a Ollama."""
    filas = 0
    _mapa_alias_bio, inverso_alias_bio = cargar_alias_proveedores(cur_lab)
    # Fuente de materiales del periodo (misma decisión que protagonistas nivel 2)
    fuente_mat_bio = fuente_material_para_periodo(cur_lab, periodo_actual)

    # ---------- Nivel 1: proveedores ----------
    # Protagonistas nivel 1 del periodo actual
    cur_lab.execute("""
        SELECT entidad, kg_entidad AS kg_actual, pct_del_contexto AS pct_actual, prioridad_score
        FROM ia_protagonistas_operativos
        WHERE nivel = 1 AND periodo = %s AND criterio_version = %s
        ORDER BY prioridad_score DESC
    """, (periodo_actual, DERIVADAS_VERSION))
    prot_n1 = cur_lab.fetchall() or []

    # Total ingresos por mes cerrado (para calcular pct histórico del proveedor)
    cur_lab.execute("""
        SELECT periodo, SUM(kilos_total) AS kg_total
        FROM ia_resumen_mensual_ingresos_proveedor
        WHERE estado_periodo = 'cerrado' AND criterio_version = %s
        GROUP BY periodo
        ORDER BY periodo
    """, (DERIVADAS_VERSION,))
    totales_mes = {r["periodo"]: float(r["kg_total"] or 0) for r in (cur_lab.fetchall() or [])}
    meses_base_total = len(totales_mes)

    for pn in prot_n1:
        proveedor = pn["entidad"]
        kg_actual = float(pn["kg_actual"] or 0)
        pct_actual = float(pn["pct_actual"] or 0)

        cur_lab.execute("""
            SELECT periodo, kilos_total AS kg, registros
            FROM ia_resumen_mensual_ingresos_proveedor
            WHERE Proveedor = %s AND estado_periodo = 'cerrado' AND criterio_version = %s
            ORDER BY periodo
        """, (proveedor, DERIVADAS_VERSION))
        hist_rows = cur_lab.fetchall() or []

        historial = []
        for h in hist_rows:
            kg_mes = float(h["kg"] or 0)
            tot_mes = totales_mes.get(h["periodo"], 0)
            pct_mes = round(kg_mes / tot_mes * 100, 2) if tot_mes > 0 else 0
            historial.append({
                "periodo": h["periodo"],
                "kg": kg_mes,
                "registros": int(h["registros"] or 0),
                "pct_contexto": pct_mes,
            })

        n_activos = len(historial)
        confiab = _confiabilidad_bio(n_activos)
        kg_vals = [h["kg"] for h in historial]
        pct_vals = [h["pct_contexto"] for h in historial]
        kg_prom = round(sum(kg_vals) / n_activos, 2) if n_activos else 0
        kg_min  = round(min(kg_vals), 2) if n_activos else 0
        kg_max  = round(max(kg_vals), 2) if n_activos else 0
        pct_prom = round(sum(pct_vals) / n_activos, 2) if n_activos else 0
        ultimo_mes = historial[-1]["periodo"] if historial else None
        lectura = _lectura_bio("proveedor", confiab, pct_actual, pct_prom, n_activos)

        cur_lab.execute("""
            INSERT INTO ia_biografia_protagonistas
                (criterio_version, tipo_entidad, entidad, entidad_2, periodo_actual,
                 kg_actual, pct_actual_contexto, meses_con_actividad,
                 kg_promedio_meses_activos, kg_min_mes_activo, kg_max_mes_activo,
                 pct_promedio_meses_activos, ultimo_mes_con_actividad,
                 meses_base_total, confiabilidad, historial_json, lectura_backend, prioridad_score)
            VALUES (%s, 'proveedor', %s, '', %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                kg_actual = VALUES(kg_actual),
                pct_actual_contexto = VALUES(pct_actual_contexto),
                meses_con_actividad = VALUES(meses_con_actividad),
                kg_promedio_meses_activos = VALUES(kg_promedio_meses_activos),
                kg_min_mes_activo = VALUES(kg_min_mes_activo),
                kg_max_mes_activo = VALUES(kg_max_mes_activo),
                pct_promedio_meses_activos = VALUES(pct_promedio_meses_activos),
                ultimo_mes_con_actividad = VALUES(ultimo_mes_con_actividad),
                meses_base_total = VALUES(meses_base_total),
                confiabilidad = VALUES(confiabilidad),
                historial_json = VALUES(historial_json),
                lectura_backend = VALUES(lectura_backend),
                prioridad_score = VALUES(prioridad_score),
                actualizado_en = CURRENT_TIMESTAMP
        """, (
            DERIVADAS_VERSION, proveedor, periodo_actual,
            kg_actual, pct_actual, n_activos,
            kg_prom, kg_min, kg_max,
            pct_prom, ultimo_mes,
            meses_base_total, confiab,
            json.dumps(historial[-12:], ensure_ascii=False),
            lectura,
            float(pn["prioridad_score"] or 0),
        ))
        filas += 1

    # ---------- Nivel 2: materiales por proveedor ----------
    # Regenerar desde cero las biografías de material del periodo: si la fuente
    # cambió (cruda -> normalizada) no deben quedar entidades de la fuente vieja.
    cur_lab.execute("""
        DELETE FROM ia_biografia_protagonistas
        WHERE tipo_entidad = 'proveedor_material' AND periodo_actual = %s AND criterio_version = %s
    """, (periodo_actual, DERIVADAS_VERSION))
    cur_lab.execute("""
        SELECT entidad AS material, entidad_2 AS proveedor,
               kg_entidad AS kg_actual, pct_del_contexto AS pct_actual, prioridad_score
        FROM ia_protagonistas_operativos
        WHERE nivel = 2 AND periodo = %s AND criterio_version = %s
        ORDER BY entidad_2, prioridad_score DESC
    """, (periodo_actual, DERIVADAS_VERSION))
    prot_n2 = cur_lab.fetchall() or []

    for pm in prot_n2:
        proveedor = pm["proveedor"] or ""
        material  = pm["material"] or ""
        kg_actual = float(pm["kg_actual"] or 0)
        pct_actual = float(pm["pct_actual"] or 0)

        # Historial del material dentro del proveedor (meses cerrados desde sh_etp_v2_local)
        # La condición de material respeta la fuente del periodo (sin mezclar)
        try:
            variantes_b = variantes_proveedor(proveedor, inverso_alias_bio)
            marcadores_b = ", ".join(["%s"] * len(variantes_b))
            if fuente_mat_bio == "DescriptionNormalizada":
                cond_material = "DescriptionNormalizada = %s"
                params_material = [material]
            else:
                cond_material = "(Description = %s OR Materiales = %s)"
                params_material = [material, material]
            cur_op.execute(f"""
                SELECT DATE_FORMAT(FechaHora, '%%Y-%%m') AS periodo,
                       COUNT(*) AS registros,
                       COALESCE(SUM(kilos), 0) AS kg,
                       COALESCE(SUM(SUM(kilos)) OVER (PARTITION BY DATE_FORMAT(FechaHora,'%%Y-%%m')), 0) AS kg_prov_mes
                FROM TiposDeMovimiento
                WHERE TiposDeMovimiento LIKE 'ING %%'
                  AND Proveedor IN ({marcadores_b})
                  AND {cond_material}
                  AND DATE_FORMAT(FechaHora, '%%Y-%%m') < %s
                GROUP BY periodo
                ORDER BY periodo
            """, tuple(variantes_b + params_material + [periodo_actual]))
            hist_mat_raw = cur_op.fetchall() or []
        except Exception:
            hist_mat_raw = []

        # Para pct_contexto por mes necesitamos kg del proveedor ese mes
        # Usamos ia_resumen_mensual_ingresos_proveedor como fuente
        cur_lab.execute("""
            SELECT periodo, kilos_total AS kg_prov
            FROM ia_resumen_mensual_ingresos_proveedor
            WHERE Proveedor = %s AND estado_periodo = 'cerrado' AND criterio_version = %s
            ORDER BY periodo
        """, (proveedor, DERIVADAS_VERSION))
        prov_kg_mes = {r["periodo"]: float(r["kg_prov"] or 0) for r in (cur_lab.fetchall() or [])}

        historial = []
        for h in hist_mat_raw:
            kg_mes = float(h["kg"] or 0)
            kg_prov = prov_kg_mes.get(h["periodo"], 0)
            pct_mes = round(kg_mes / kg_prov * 100, 2) if kg_prov > 0 else 0
            historial.append({
                "periodo": h["periodo"],
                "kg": kg_mes,
                "registros": int(h["registros"] or 0),
                "pct_contexto": pct_mes,
            })

        n_activos = len(historial)
        confiab = _confiabilidad_bio(n_activos)
        kg_vals = [h["kg"] for h in historial]
        pct_vals = [h["pct_contexto"] for h in historial]
        kg_prom = round(sum(kg_vals) / n_activos, 2) if n_activos else 0
        kg_min  = round(min(kg_vals), 2) if n_activos else 0
        kg_max  = round(max(kg_vals), 2) if n_activos else 0
        pct_prom = round(sum(pct_vals) / n_activos, 2) if n_activos else 0
        ultimo_mes = historial[-1]["periodo"] if historial else None
        lectura = _lectura_bio("proveedor_material", confiab, pct_actual, pct_prom, n_activos)

        cur_lab.execute("""
            INSERT INTO ia_biografia_protagonistas
                (criterio_version, tipo_entidad, entidad, entidad_2, periodo_actual,
                 kg_actual, pct_actual_contexto, meses_con_actividad,
                 kg_promedio_meses_activos, kg_min_mes_activo, kg_max_mes_activo,
                 pct_promedio_meses_activos, ultimo_mes_con_actividad,
                 meses_base_total, confiabilidad, historial_json, lectura_backend, prioridad_score)
            VALUES (%s, 'proveedor_material', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                kg_actual = VALUES(kg_actual),
                pct_actual_contexto = VALUES(pct_actual_contexto),
                meses_con_actividad = VALUES(meses_con_actividad),
                kg_promedio_meses_activos = VALUES(kg_promedio_meses_activos),
                kg_min_mes_activo = VALUES(kg_min_mes_activo),
                kg_max_mes_activo = VALUES(kg_max_mes_activo),
                pct_promedio_meses_activos = VALUES(pct_promedio_meses_activos),
                ultimo_mes_con_actividad = VALUES(ultimo_mes_con_actividad),
                meses_base_total = VALUES(meses_base_total),
                confiabilidad = VALUES(confiabilidad),
                historial_json = VALUES(historial_json),
                lectura_backend = VALUES(lectura_backend),
                prioridad_score = VALUES(prioridad_score),
                actualizado_en = CURRENT_TIMESTAMP
        """, (
            DERIVADAS_VERSION, material, proveedor, periodo_actual,
            kg_actual, pct_actual, n_activos,
            kg_prom, kg_min, kg_max,
            pct_prom, ultimo_mes,
            meses_base_total, confiab,
            json.dumps(historial[-12:], ensure_ascii=False),
            lectura,
            float(pm["prioridad_score"] or 0),
        ))
        filas += 1

    return filas


def _encolar_curiosidad(cur_lab, tarea):
    """Inserta una tarea en ia_cola_curiosidad si no hay ya una pendiente/en_proceso
    ni una completada reciente (30 días) para el mismo tipo_tarea+entidad+entidad_2+
    periodo+material_fuente (dedupe separado por fuente: una entidad cruda vieja no
    bloquea a la misma entidad normalizada nueva). Devuelve 1 si insertó, 0 si dedupe."""
    fuente = tarea.get("material_fuente") or "Description"
    cur_lab.execute("""
        SELECT id FROM ia_cola_curiosidad
        WHERE tipo_tarea = %s AND entidad = %s
          AND COALESCE(entidad_2, '') = COALESCE(%s, '')
          AND periodo = %s
          AND COALESCE(material_fuente, 'Description') = %s
          AND (estado IN ('pendiente', 'en_proceso')
               OR (estado = 'completada' AND actualizado_en >= NOW() - INTERVAL 30 DAY))
        LIMIT 1
    """, (tarea["tipo_tarea"], tarea["entidad"], tarea.get("entidad_2"), tarea["periodo"], fuente))
    if cur_lab.fetchone():
        return 0
    cur_lab.execute("""
        INSERT INTO ia_cola_curiosidad
            (tipo_tarea, tipo_nodo, entidad, entidad_2, material_fuente, periodo, origen, motivo,
             prioridad_score, profundidad, estado)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendiente')
    """, (
        tarea["tipo_tarea"], tarea["tipo_nodo"], tarea["entidad"], tarea.get("entidad_2"),
        fuente, tarea["periodo"], tarea.get("origen") or "", tarea.get("motivo"),
        float(tarea.get("prioridad_score") or 0),
        int(tarea.get("profundidad") or 1),
    ))
    return 1


def fuente_material_para_periodo(cur_lab, periodo):
    """Decide la fuente de materiales para un periodo: 'DescriptionNormalizada'
    si su cobertura es fuerte/parcial; si es débil o no hay datos, 'Description'."""
    try:
        cur_lab.execute("""
            SELECT estado_confianza
            FROM ia_resumen_mensual_description_normalizada
            WHERE periodo = %s AND criterio_version = %s
            LIMIT 1
        """, (periodo, DERIVADAS_VERSION))
        fila = cur_lab.fetchone()
        if fila and fila.get("estado_confianza") in ("fuerte", "parcial"):
            return "DescriptionNormalizada"
    except Exception:
        pass
    return "Description"


def calcular_cola_curiosidad(cur_lab, periodo_actual):
    """Genera tareas de exploración determinísticas en ia_cola_curiosidad a partir
    de protagonistas y biografías del periodo actual. Solo encola: no ejecuta
    microtareas ni llama a Ollama. Devuelve cantidad de tareas encoladas."""
    encoladas = 0

    # 1) Proveedores protagonistas nivel 1 -> perfil_proveedor_historico
    cur_lab.execute("""
        SELECT entidad, prioridad_score
        FROM ia_protagonistas_operativos
        WHERE nivel = 1 AND periodo = %s AND criterio_version = %s
    """, (periodo_actual, DERIVADAS_VERSION))
    for r in (cur_lab.fetchall() or []):
        encoladas += _encolar_curiosidad(cur_lab, {
            "tipo_tarea": "perfil_proveedor_historico",
            "tipo_nodo": "proveedor",
            "entidad": r["entidad"],
            "entidad_2": None,
            "periodo": periodo_actual,
            "origen": "protagonista_nivel1",
            "motivo": f"Proveedor protagonista nivel 1 en {periodo_actual}",
            "prioridad_score": r["prioridad_score"],
        })

    # 2) Biografías de proveedor con historial -> ranking_vecinos_proveedor_mes
    #    para su último mes cerrado con actividad
    cur_lab.execute("""
        SELECT entidad, ultimo_mes_con_actividad, meses_con_actividad, prioridad_score
        FROM ia_biografia_protagonistas
        WHERE tipo_entidad = 'proveedor' AND periodo_actual = %s AND criterio_version = %s
    """, (periodo_actual, DERIVADAS_VERSION))
    for r in (cur_lab.fetchall() or []):
        if int(r["meses_con_actividad"] or 0) <= 0 or not r["ultimo_mes_con_actividad"]:
            continue
        encoladas += _encolar_curiosidad(cur_lab, {
            "tipo_tarea": "ranking_vecinos_proveedor_mes",
            "tipo_nodo": "proveedor",
            "entidad": r["entidad"],
            "entidad_2": None,
            "periodo": r["ultimo_mes_con_actividad"],
            "origen": "biografia_proveedor",
            "motivo": f"Comparar contra vecinos en su último mes cerrado ({r['ultimo_mes_con_actividad']})",
            "prioridad_score": r["prioridad_score"],
        })

    # 3) proveedor_material protagonistas (nivel 2) -> perfil_proveedor_material_historico
    # Las entidades de nivel 2 se generaron con la fuente del periodo; las tareas
    # heredan esa fuente para no mezclar crudo/normalizado en la misma entidad.
    fuente_mat_cola = fuente_material_para_periodo(cur_lab, periodo_actual)
    cur_lab.execute("""
        SELECT entidad AS material, entidad_2 AS proveedor, prioridad_score
        FROM ia_protagonistas_operativos
        WHERE nivel = 2 AND periodo = %s AND criterio_version = %s
    """, (periodo_actual, DERIVADAS_VERSION))
    nivel2 = cur_lab.fetchall() or []
    for r in nivel2:
        if not r["material"] or not r["proveedor"]:
            continue
        encoladas += _encolar_curiosidad(cur_lab, {
            "tipo_tarea": "perfil_proveedor_material_historico",
            "tipo_nodo": "proveedor_material",
            "entidad": r["material"],
            "entidad_2": r["proveedor"],
            "material_fuente": fuente_mat_cola,
            "periodo": periodo_actual,
            "origen": "protagonista_nivel2",
            "motivo": f"Material protagonista dentro de {r['proveedor']} en {periodo_actual}",
            "prioridad_score": r["prioridad_score"],
        })

    # 4) Materiales protagonistas (distintos) -> perfil_material_global
    #    prioridad heredada: la máxima entre sus apariciones nivel 2
    materiales = {}
    for r in nivel2:
        mat = r["material"]
        score = float(r["prioridad_score"] or 0)
        if mat and (mat not in materiales or score > materiales[mat]):
            materiales[mat] = score
    for mat, score in materiales.items():
        encoladas += _encolar_curiosidad(cur_lab, {
            "tipo_tarea": "perfil_material_global",
            "tipo_nodo": "material",
            "entidad": mat,
            "entidad_2": None,
            "material_fuente": fuente_mat_cola,
            "periodo": periodo_actual,
            "origen": "protagonista_nivel2",
            "motivo": f"Material protagonista en {periodo_actual}; perfilar a nivel global",
            "prioridad_score": score,
        })

    return encoladas


def calcular_cola_proveedores_por_cobertura(cur_lab):
    """Camada de descubrimiento por cobertura mensual: para cada mes cerrado de
    ia_resumen_mensual_ingresos_proveedor, encola perfil_proveedor_historico y
    ranking_vecinos_proveedor_mes para los proveedores que explican hasta
    COBERTURA_PROVEEDORES_PCT% de los kilos del mes (tope
    COBERTURA_PROVEEDORES_MAX_POR_MES por mes). Determinístico, sin Ollama.
    Alcanza proveedores históricos relevantes que no son protagonistas actuales
    ni dominantes de materiales. Devuelve {"encoladas", "dedupe", "periodos"}."""
    resultado = {"encoladas": 0, "dedupe": 0, "periodos": 0}

    cur_lab.execute("""
        SELECT periodo, Proveedor, kilos_total
        FROM ia_resumen_mensual_ingresos_proveedor
        WHERE estado_periodo = 'cerrado' AND criterio_version = %s
        ORDER BY periodo DESC, kilos_total DESC
    """, (DERIVADAS_VERSION,))
    filas = cur_lab.fetchall() or []
    if not filas:
        return resultado

    por_periodo = {}
    for r in filas:
        por_periodo.setdefault(r["periodo"], []).append(r)

    periodos = sorted(por_periodo.keys(), reverse=True)
    p_max = periodos[0]
    y_max, m_max = int(p_max[:4]), int(p_max[5:7])
    periodo_actual = date.today().strftime("%Y-%m")

    for periodo in periodos:
        provs = por_periodo[periodo]
        total_mes = sum(float(x["kilos_total"] or 0) for x in provs)
        if total_mes <= 0:
            continue
        resultado["periodos"] += 1
        y, m = int(periodo[:4]), int(periodo[5:7])
        meses_atras = (y_max - y) * 12 + (m_max - m)
        recencia = 0.9 ** meses_atras

        pct_acumulado = 0.0
        for idx, x in enumerate(provs):
            if idx >= COBERTURA_PROVEEDORES_MAX_POR_MES:
                break
            if pct_acumulado >= COBERTURA_PROVEEDORES_PCT:
                break
            proveedor = x["Proveedor"]
            kg_mes = float(x["kilos_total"] or 0)
            pct_mes = round(kg_mes / total_mes * 100, 2)
            pct_acumulado = round(pct_acumulado + pct_mes, 2)
            if not proveedor or not str(proveedor).strip():
                continue

            # Prioridad: mayor por kg del mes, % del mes y recencia del periodo
            prioridad = round((kg_mes * 0.7 + pct_mes * 50.0) * recencia, 4)
            motivo = (
                f"Proveedor explica {pct_mes}% del periodo {periodo}; "
                f"acumulado hasta este proveedor {pct_acumulado}%"
            )
            base = {
                "tipo_nodo": "proveedor",
                "entidad": proveedor,
                "entidad_2": None,
                "origen": "cobertura_mensual_proveedor",
                "motivo": motivo,
                "prioridad_score": prioridad,
                "profundidad": 1,
            }
            # Perfil histórico: uno por proveedor (periodo = mes en curso, dedupe natural)
            insertada = _encolar_curiosidad(cur_lab, dict(
                base, tipo_tarea="perfil_proveedor_historico", periodo=periodo_actual))
            resultado["encoladas"] += insertada
            resultado["dedupe"] += 1 - insertada
            # Ranking de vecinos en el mes cerrado que lo hizo relevante
            insertada = _encolar_curiosidad(cur_lab, dict(
                base, tipo_tarea="ranking_vecinos_proveedor_mes", periodo=periodo))
            resultado["encoladas"] += insertada
            resultado["dedupe"] += 1 - insertada

    if resultado["encoladas"] or resultado["dedupe"]:
        add_reasoning_step(
            "cola_curiosidad_cobertura_proveedores",
            (f"Cobertura mensual: {resultado['encoladas']} tareas encoladas "
             f"sobre {resultado['periodos']} meses cerrados"),
            (f"dedupe={resultado['dedupe']} | umbral={COBERTURA_PROVEEDORES_PCT}% | "
             f"max/mes={COBERTURA_PROVEEDORES_MAX_POR_MES}")
        )
    return resultado


def _normalizar_proveedor_material(r):
    """Agrega claves explícitas proveedor/material/etiqueta a una fila de
    ia_biografia_protagonistas o ia_protagonistas_operativos.
    Convención real de almacenamiento: en filas de material (tipo_entidad
    'material' o 'proveedor_material'), entidad = material y entidad_2 =
    proveedor padre; en filas de proveedor, entidad = proveedor y entidad_2
    vacío ('' o NULL; se normaliza a None)."""
    if r.get("entidad_2") == "":
        r["entidad_2"] = None
    tipo = str(r.get("tipo_entidad") or "")
    if tipo in ("material", "proveedor_material"):
        r["material"] = r.get("entidad")
        r["proveedor"] = r.get("entidad_2")
        r["etiqueta"] = f"{r.get('entidad_2') or '?'} → {r.get('entidad') or '?'}"
    else:
        r["proveedor"] = r.get("entidad")
        r["material"] = None
        r["etiqueta"] = r.get("entidad") or "?"
    return r


def leer_biografia_protagonistas(periodo_actual=None):
    """Lee biografías del periodo actual para /api/ia/cerebro."""
    resultado = {"proveedores": [], "materiales": [], "actualizado_en": None}
    try:
        conn = get_ia_connection()
        if not conn:
            return resultado
        cur = conn.cursor()
        if not periodo_actual:
            periodo_actual = date.today().strftime("%Y-%m")

        cur.execute("""
            SELECT tipo_entidad, entidad, entidad_2, periodo_actual,
                   kg_actual, pct_actual_contexto, meses_con_actividad,
                   kg_promedio_meses_activos, kg_min_mes_activo, kg_max_mes_activo,
                   pct_promedio_meses_activos, ultimo_mes_con_actividad,
                   meses_base_total, confiabilidad, historial_json, lectura_backend,
                   prioridad_score, actualizado_en
            FROM ia_biografia_protagonistas
            WHERE criterio_version = %s AND periodo_actual = %s
            ORDER BY tipo_entidad, prioridad_score DESC
        """, (DERIVADAS_VERSION, periodo_actual))
        for r in (cur.fetchall() or []):
            try:
                r["historial_json"] = json.loads(r["historial_json"] or "[]")
            except Exception:
                r["historial_json"] = []
            _normalizar_proveedor_material(r)
            if r.get("actualizado_en") and not resultado["actualizado_en"]:
                resultado["actualizado_en"] = str(r["actualizado_en"])
            if r["tipo_entidad"] == "proveedor":
                resultado["proveedores"].append(r)
            else:
                resultado["materiales"].append(r)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error en leer_biografia_protagonistas: {e}", file=sys.stderr)
    return resultado


def leer_protagonistas_operativos():
    """Lee protagonistas agrupados por nivel y origen para /api/ia/cerebro."""
    resultado = {"nivel1": [], "nivel2_por_proveedor": {}, "actualizado_en": None}
    try:
        conn = get_ia_connection()
        if not conn:
            return resultado
        cur = conn.cursor()

        # Nivel 1: todos
        cur.execute("""
            SELECT origen_tipo, origen_entidad, tipo_entidad, entidad, entidad_2,
                   periodo, estado_periodo, kg_contexto_total, kg_entidad,
                   pct_del_contexto, registros, prioridad_score, motivos_json, actualizado_en
            FROM ia_protagonistas_operativos
            WHERE nivel = 1 AND criterio_version = %s
            ORDER BY prioridad_score DESC LIMIT 20
        """, (DERIVADAS_VERSION,))
        for r in (cur.fetchall() or []):
            try:
                r["motivos_json"] = json.loads(r["motivos_json"] or "{}")
            except Exception:
                r["motivos_json"] = {}
            _normalizar_proveedor_material(r)
            resultado["nivel1"].append(r)
            if r.get("actualizado_en") and not resultado["actualizado_en"]:
                resultado["actualizado_en"] = str(r["actualizado_en"])

        # Nivel 2: agrupados por proveedor padre (entidad_2, expuesto como 'proveedor')
        cur.execute("""
            SELECT origen_tipo, origen_entidad, tipo_entidad, entidad, entidad_2,
                   periodo, estado_periodo, kg_contexto_total, kg_entidad,
                   pct_del_contexto, registros, prioridad_score, motivos_json, actualizado_en
            FROM ia_protagonistas_operativos
            WHERE nivel = 2 AND criterio_version = %s
            ORDER BY entidad_2, prioridad_score DESC
        """, (DERIVADAS_VERSION,))
        for r in (cur.fetchall() or []):
            try:
                r["motivos_json"] = json.loads(r["motivos_json"] or "{}")
            except Exception:
                r["motivos_json"] = {}
            _normalizar_proveedor_material(r)
            proveedor = r.get("proveedor") or "?"
            resultado["nivel2_por_proveedor"].setdefault(proveedor, []).append(r)

        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error en leer_protagonistas_operativos: {e}", file=sys.stderr)
    return resultado


def _normalizar_nodo_cola(r):
    """Claves explícitas proveedor/material/etiqueta según tipo_nodo, y fechas a str.
    Para filas de ia_cola_curiosidad e ia_fichas_operativas."""
    if r.get("entidad_2") == "":
        r["entidad_2"] = None
    for k in ("cooldown_hasta", "creado_en", "actualizado_en"):
        if r.get(k) is not None:
            r[k] = str(r[k])
    r["material_normalizado"] = (r.get("material_fuente") == "DescriptionNormalizada")
    if r.get("tipo_nodo") == "proveedor_material":
        r["material"] = r.get("entidad")
        r["proveedor"] = r.get("entidad_2")
        r["etiqueta"] = f"{r.get('entidad_2') or '?'} → {r.get('entidad') or '?'}"
    elif r.get("tipo_nodo") == "material":
        r["material"] = r.get("entidad")
        r["proveedor"] = None
        r["etiqueta"] = r.get("entidad") or "?"
    else:
        r["proveedor"] = r.get("entidad")
        r["material"] = None
        r["etiqueta"] = r.get("entidad") or "?"
    return r


def leer_cola_curiosidad(limite_top=10):
    """Lee estado de ia_cola_curiosidad para /api/ia/cerebro. Solo lectura."""
    resultado = {"pendientes": 0, "top_pendientes": [], "completadas_recientes": [], "resumen_por_origen": {}}
    try:
        conn = get_ia_connection()
        if not conn:
            return resultado
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ia_cola_curiosidad WHERE estado = 'pendiente'")
        fila = cur.fetchone() or {}
        resultado["pendientes"] = int(fila.get("n") or 0)

        cur.execute("""
            SELECT origen, COUNT(*) AS pendientes
            FROM ia_cola_curiosidad
            WHERE estado = 'pendiente'
            GROUP BY origen
            ORDER BY pendientes DESC
        """)
        resultado["resumen_por_origen"] = {
            (r.get("origen") or "sin_origen"): int(r.get("pendientes") or 0)
            for r in (cur.fetchall() or [])
        }

        cur.execute("""
            SELECT id, tipo_tarea, tipo_nodo, entidad, entidad_2, material_fuente,
                   periodo, origen, motivo,
                   prioridad_score, profundidad, estado, cooldown_hasta, creado_en, actualizado_en
            FROM ia_cola_curiosidad
            WHERE estado = 'pendiente'
            ORDER BY prioridad_score DESC, id ASC
            LIMIT %s
        """, (int(limite_top),))
        for r in (cur.fetchall() or []):
            resultado["top_pendientes"].append(_normalizar_nodo_cola(r))

        cur.execute("""
            SELECT id, tipo_tarea, tipo_nodo, entidad, entidad_2, material_fuente,
                   periodo, origen,
                   prioridad_score, estado, resultado_resumen, actualizado_en
            FROM ia_cola_curiosidad
            WHERE estado = 'completada'
            ORDER BY actualizado_en DESC, id DESC
            LIMIT %s
        """, (int(limite_top),))
        for r in (cur.fetchall() or []):
            resultado["completadas_recientes"].append(_normalizar_nodo_cola(r))
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error en leer_cola_curiosidad: {e}", file=sys.stderr)
    return resultado


def leer_fichas_operativas(limite=10):
    """Lee fichas determinísticas recientes para /api/ia/cerebro. Solo lectura."""
    resultado = {"total": 0, "recientes": []}
    try:
        conn = get_ia_connection()
        if not conn:
            return resultado
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ia_fichas_operativas")
        fila = cur.fetchone() or {}
        resultado["total"] = int(fila.get("n") or 0)

        cur.execute("""
            SELECT id, cola_id, tipo_tarea, tipo_nodo, entidad, entidad_2, material_fuente,
                   periodo, titulo, resumen_deterministico, datos_json, senales_json,
                   prioridad_score, creado_en
            FROM ia_fichas_operativas
            ORDER BY creado_en DESC, id DESC
            LIMIT %s
        """, (int(limite),))
        for r in (cur.fetchall() or []):
            for k in ("datos_json", "senales_json"):
                try:
                    r[k] = json.loads(r[k] or "{}")
                except Exception:
                    r[k] = {}
            resultado["recientes"].append(_normalizar_nodo_cola(r))
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error en leer_fichas_operativas: {e}", file=sys.stderr)
    return resultado


def leer_materiales_normalizados_recientes(limite=10):
    """Top materiales por DescriptionNormalizada del mes actual, con cobertura
    visible. Determinístico, solo lectura de la derivada. Con estado_confianza
    'debil' no debe usarse para conclusiones fuertes."""
    resultado = {
        "periodo": date.today().strftime("%Y-%m"),
        "disponible": False,
        "cobertura_pct": 0,
        "estado_confianza": None,
        "apto_para_conclusiones": False,
        "registros_total_periodo": 0,
        "registros_normalizados_periodo": 0,
        "pct_kilos_normalizados": 0,
        "top_materiales": [],
    }
    try:
        conn = get_ia_connection()
        if not conn:
            return resultado
        cur = conn.cursor()
        cur.execute("""
            SELECT description_normalizada, registros, kilos_total,
                   registros_total_periodo, registros_normalizados_periodo,
                   cobertura_pct_periodo, estado_confianza,
                   pct_kilos_normalizados_periodo, primer_movimiento, ultimo_movimiento
            FROM ia_resumen_mensual_description_normalizada
            WHERE periodo = %s AND criterio_version = %s
            ORDER BY kilos_total DESC
            LIMIT %s
        """, (resultado["periodo"], DERIVADAS_VERSION, int(limite)))
        filas = cur.fetchall() or []
        cur.close()
        conn.close()
        if not filas:
            return resultado
        f0 = filas[0]
        resultado.update({
            "disponible": True,
            "cobertura_pct": float(f0["cobertura_pct_periodo"] or 0),
            "estado_confianza": f0["estado_confianza"],
            "apto_para_conclusiones": f0["estado_confianza"] in ("fuerte", "parcial"),
            "registros_total_periodo": int(f0["registros_total_periodo"] or 0),
            "registros_normalizados_periodo": int(f0["registros_normalizados_periodo"] or 0),
            "pct_kilos_normalizados": float(f0["pct_kilos_normalizados_periodo"] or 0),
        })
        for f in filas:
            resultado["top_materiales"].append({
                "material": f["description_normalizada"],
                "registros": int(f["registros"] or 0),
                "kilos_total": float(f["kilos_total"] or 0),
                "primer_movimiento": str(f["primer_movimiento"]) if f["primer_movimiento"] else None,
                "ultimo_movimiento": str(f["ultimo_movimiento"]) if f["ultimo_movimiento"] else None,
            })
    except Exception as e:
        print(f"Error en leer_materiales_normalizados_recientes: {e}", file=sys.stderr)
    return resultado


def leer_mapa_historico_proveedores(max_proveedores=60, max_meses=33):
    """Matriz proveedores x meses para el dashboard, determinística y sin Ollama.
    Filas: proveedores descubiertos por ia_fichas_operativas / ia_cola_curiosidad,
    priorizados por kg total histórico, meses con actividad y prioridad_score.
    Columnas: meses de ia_periodos_procesados (reciente -> antiguo).
    Celdas: kg, pct del mes, ranking (top1/top3/top10), ficha de ranking
    completada y tarea pendiente para ese proveedor/mes."""
    resultado = {
        "meses": [], "proveedores": [], "totales_mes": {},
        "resumen": {"proveedores_mostrados": 0, "meses_mostrados": 0,
                    "fichas_completadas": 0, "pendientes": 0},
    }
    try:
        conn = get_ia_connection()
        if not conn:
            return resultado
        cur = conn.cursor()

        # 1. Meses (reciente -> antiguo)
        cur.execute("""
            SELECT periodo FROM ia_periodos_procesados
            WHERE criterio_version = %s
            ORDER BY periodo DESC
            LIMIT %s
        """, (DERIVADAS_VERSION, int(max_meses)))
        meses = [r["periodo"] for r in (cur.fetchall() or [])]
        if not meses:
            cur.close()
            conn.close()
            return resultado
        resultado["meses"] = meses
        meses_set = set(meses)

        # Canonizar por alias: variantes crudas suman bajo el proveedor canónico
        mapa_alias_m, _inv_m = cargar_alias_proveedores(cur)

        # 2. Resumen mensual de ingresos por proveedor para esos meses
        marcadores = ", ".join(["%s"] * len(meses))
        cur.execute(f"""
            SELECT periodo, Proveedor, kilos_total
            FROM ia_resumen_mensual_ingresos_proveedor
            WHERE criterio_version = %s AND periodo IN ({marcadores})
        """, tuple([DERIVADAS_VERSION] + meses))
        kg_por_prov_mes = {}
        kg_mes_prov = {}
        for r in (cur.fetchall() or []):
            prov = canonizar_proveedor(r["Proveedor"], mapa_alias_m)
            per, kg = r["periodo"], float(r["kilos_total"] or 0)
            if not prov:
                continue
            kg_por_prov_mes[(prov, per)] = kg_por_prov_mes.get((prov, per), 0) + kg
            kg_mes_prov.setdefault(per, {})
            kg_mes_prov[per][prov] = kg_mes_prov[per].get(prov, 0) + kg
        rank_por_prov_mes = {}
        for per, d in kg_mes_prov.items():
            lista = sorted(d.items(), key=lambda x: (-x[1], x[0]))
            resultado["totales_mes"][per] = round(sum(kg for _, kg in lista), 2)
            for pos, (prov, _kg) in enumerate(lista, start=1):
                rank_por_prov_mes[(prov, per)] = pos

        # 3. Proveedores descubiertos por cola y fichas + indicadores
        def _prov_de(fila):
            if fila.get("tipo_nodo") == "proveedor_material":
                return canonizar_proveedor(fila.get("entidad_2"), mapa_alias_m)
            if fila.get("tipo_nodo") == "material":
                return None
            return canonizar_proveedor(fila.get("entidad"), mapa_alias_m)

        descubiertos = {}
        pendientes_prov_mes = set()
        total_pendientes = 0
        cur.execute("""
            SELECT tipo_nodo, entidad, entidad_2, periodo, estado, prioridad_score
            FROM ia_cola_curiosidad
        """)
        for r in (cur.fetchall() or []):
            if r.get("estado") == "pendiente":
                total_pendientes += 1
            prov = _prov_de(r)
            if not prov:
                continue
            score = float(r.get("prioridad_score") or 0)
            if score > descubiertos.get(prov, 0) or prov not in descubiertos:
                descubiertos[prov] = max(descubiertos.get(prov, 0), score)
            if r.get("estado") == "pendiente" and r.get("periodo") in meses_set:
                pendientes_prov_mes.add((prov, r["periodo"]))

        fichas_ranking_prov_mes = set()
        fichas_por_prov = {}
        cur.execute("""
            SELECT tipo_tarea, tipo_nodo, entidad, entidad_2, periodo
            FROM ia_fichas_operativas
        """)
        for r in (cur.fetchall() or []):
            prov = _prov_de(r)
            if not prov:
                continue
            descubiertos.setdefault(prov, 0)
            fichas_por_prov[prov] = fichas_por_prov.get(prov, 0) + 1
            if r.get("tipo_tarea") == "ranking_vecinos_proveedor_mes" and r.get("periodo") in meses_set:
                fichas_ranking_prov_mes.add((prov, r["periodo"]))
        cur.close()
        conn.close()

        # 4. Priorizar filas: kg total histórico, meses activos, prioridad_score
        candidatos = []
        for prov, score in descubiertos.items():
            kg_total = sum(kg_por_prov_mes.get((prov, m), 0) for m in meses)
            meses_activos = sum(1 for m in meses if kg_por_prov_mes.get((prov, m), 0) > 0)
            candidatos.append((prov, kg_total, meses_activos, score))
        candidatos.sort(key=lambda x: (-x[1], -x[2], -x[3], x[0]))
        candidatos = candidatos[:int(max_proveedores)]

        fichas_mostradas = 0
        for prov, kg_total, meses_activos, score in candidatos:
            celdas = {}
            for m in meses:
                kg = kg_por_prov_mes.get((prov, m), 0)
                tiene_ficha = (prov, m) in fichas_ranking_prov_mes
                tiene_pendiente = (prov, m) in pendientes_prov_mes
                if kg <= 0 and not tiene_ficha and not tiene_pendiente:
                    continue  # celda vacía: no viaja en el JSON
                total_mes = resultado["totales_mes"].get(m, 0)
                rank = rank_por_prov_mes.get((prov, m))
                celdas[m] = {
                    "kg": round(kg, 2),
                    "pct": round(kg / total_mes * 100, 2) if total_mes > 0 and kg > 0 else 0,
                    "rank": rank,
                    "top": ("top1" if rank == 1 else "top3" if rank and rank <= 3
                            else "top10" if rank and rank <= 10 else None),
                    "ficha_ranking": tiene_ficha,
                    "pendiente": tiene_pendiente,
                }
            fichas_mostradas += fichas_por_prov.get(prov, 0)
            resultado["proveedores"].append({
                "proveedor": prov,
                "kg_total": round(kg_total, 2),
                "meses_activos": meses_activos,
                "prioridad": round(score, 4),
                "celdas": celdas,
            })

        resultado["resumen"] = {
            "proveedores_mostrados": len(resultado["proveedores"]),
            "meses_mostrados": len(meses),
            "fichas_completadas": fichas_mostradas,
            "pendientes": total_pendientes,
        }
    except Exception as e:
        print(f"Error en leer_mapa_historico_proveedores: {e}", file=sys.stderr)
    return resultado


def _ficha_kg(v):
    try:
        return f"{float(v or 0):,.0f} kg"
    except Exception:
        return f"{v} kg"


def _ficha_perfil_proveedor_historico(cur_lab, tarea):
    """Ficha determinística: historia mensual de un proveedor + ranking actual."""
    proveedor = tarea["entidad"]
    periodo = tarea["periodo"]
    datos = {"proveedor": proveedor, "periodo": periodo}
    senales = {}

    cur_lab.execute("""
        SELECT meses_con_actividad, kg_promedio_meses_activos, kg_min_mes_activo,
               kg_max_mes_activo, pct_promedio_meses_activos, ultimo_mes_con_actividad,
               confiabilidad, historial_json, kg_actual, pct_actual_contexto
        FROM ia_biografia_protagonistas
        WHERE tipo_entidad = 'proveedor' AND entidad = %s
          AND periodo_actual = %s AND criterio_version = %s
        LIMIT 1
    """, (proveedor, periodo, DERIVADAS_VERSION))
    bio = cur_lab.fetchone()
    senales["tiene_biografia"] = bool(bio)

    if bio:
        try:
            historial = json.loads(bio["historial_json"] or "[]")
        except Exception:
            historial = []
        datos.update({
            "meses_con_actividad": int(bio["meses_con_actividad"] or 0),
            "kg_promedio_meses_activos": float(bio["kg_promedio_meses_activos"] or 0),
            "kg_min_mes_activo": float(bio["kg_min_mes_activo"] or 0),
            "kg_max_mes_activo": float(bio["kg_max_mes_activo"] or 0),
            "pct_promedio_meses_activos": float(bio["pct_promedio_meses_activos"] or 0),
            "ultimo_mes_con_actividad": bio["ultimo_mes_con_actividad"],
            "confiabilidad": bio["confiabilidad"],
            "historial_12m": historial[-12:],
            "kg_actual": float(bio["kg_actual"] or 0),
            "pct_actual_contexto": float(bio["pct_actual_contexto"] or 0),
        })
    else:
        # Fallback determinístico desde el resumen mensual de meses cerrados
        cur_lab.execute("""
            SELECT periodo, kilos_total AS kg, registros
            FROM ia_resumen_mensual_ingresos_proveedor
            WHERE Proveedor = %s AND estado_periodo = 'cerrado' AND criterio_version = %s
            ORDER BY periodo
        """, (proveedor, DERIVADAS_VERSION))
        hist = [
            {"periodo": h["periodo"], "kg": float(h["kg"] or 0), "registros": int(h["registros"] or 0)}
            for h in (cur_lab.fetchall() or [])
        ]
        kg_vals = [h["kg"] for h in hist]
        datos.update({
            "meses_con_actividad": len(hist),
            "kg_promedio_meses_activos": round(sum(kg_vals) / len(kg_vals), 2) if kg_vals else 0,
            "kg_min_mes_activo": round(min(kg_vals), 2) if kg_vals else 0,
            "kg_max_mes_activo": round(max(kg_vals), 2) if kg_vals else 0,
            "pct_promedio_meses_activos": None,
            "ultimo_mes_con_actividad": hist[-1]["periodo"] if hist else None,
            "confiabilidad": None,
            "historial_12m": hist[-12:],
        })

    # Ranking actual del proveedor entre protagonistas nivel 1 del periodo
    cur_lab.execute("""
        SELECT entidad, kg_entidad
        FROM ia_protagonistas_operativos
        WHERE nivel = 1 AND periodo = %s AND criterio_version = %s
        ORDER BY kg_entidad DESC, entidad
    """, (periodo, DERIVADAS_VERSION))
    ranking = cur_lab.fetchall() or []
    posicion = next((i + 1 for i, r in enumerate(ranking) if r["entidad"] == proveedor), None)
    datos["ranking_actual"] = (
        {"posicion": posicion, "total_protagonistas": len(ranking)} if posicion else None
    )
    senales["en_ranking_actual"] = posicion is not None
    senales["meses_con_actividad"] = datos["meses_con_actividad"]

    n = datos["meses_con_actividad"]
    resumen = (
        f"{proveedor}: {n} meses cerrados con actividad; "
        f"promedio {_ficha_kg(datos['kg_promedio_meses_activos'])}/mes "
        f"(min {_ficha_kg(datos['kg_min_mes_activo'])}, max {_ficha_kg(datos['kg_max_mes_activo'])})."
    ) if n else f"{proveedor}: sin meses cerrados con actividad registrada."
    if datos.get("pct_promedio_meses_activos"):
        resumen += f" Participación promedio {datos['pct_promedio_meses_activos']}% del contexto."
    if posicion:
        resumen += f" En {periodo} es #{posicion} de {len(ranking)} protagonistas de ingresos."
    else:
        resumen += f" Sin ranking de protagonistas en {periodo}."

    return {
        "titulo": f"Perfil histórico de proveedor: {proveedor}",
        "resumen": resumen,
        "datos": datos,
        "senales": senales,
    }


def _ficha_ranking_vecinos_proveedor_mes(cur_lab, tarea):
    """Ficha determinística: posición del proveedor entre vecinos en un mes cerrado."""
    proveedor = tarea["entidad"]
    periodo = tarea["periodo"]

    cur_lab.execute("""
        SELECT Proveedor, kilos_total AS kg, registros
        FROM ia_resumen_mensual_ingresos_proveedor
        WHERE periodo = %s AND criterio_version = %s
        ORDER BY kilos_total DESC, Proveedor
    """, (periodo, DERIVADAS_VERSION))
    filas = cur_lab.fetchall() or []
    total_kg = sum(float(f["kg"] or 0) for f in filas)

    def _item(f):
        kg = float(f["kg"] or 0)
        return {
            "proveedor": f["Proveedor"],
            "kg": kg,
            "pct": round(kg / total_kg * 100, 2) if total_kg > 0 else 0,
        }

    idx = next((i for i, f in enumerate(filas) if f["Proveedor"] == proveedor), None)
    datos = {
        "proveedor": proveedor,
        "periodo": periodo,
        "total_proveedores": len(filas),
        "ranking_top": [_item(f) for f in filas[:10]],
        "posicion": (idx + 1) if idx is not None else None,
        "kg_mes": _item(filas[idx])["kg"] if idx is not None else 0,
        "pct_mes": _item(filas[idx])["pct"] if idx is not None else 0,
        "vecino_anterior": _item(filas[idx - 1]) if idx is not None and idx > 0 else None,
        "vecino_siguiente": _item(filas[idx + 1]) if idx is not None and idx + 1 < len(filas) else None,
    }
    senales = {
        "en_ranking": idx is not None,
        "es_lider_mes": idx == 0,
        "proveedores_mes": len(filas),
    }

    if idx is None:
        resumen = f"En {periodo}, {proveedor} no registró ingresos (ranking de {len(filas)} proveedores sin él)."
    else:
        resumen = (
            f"En {periodo}, {proveedor} fue #{idx + 1} de {len(filas)} proveedores de ingresos "
            f"con {_ficha_kg(datos['kg_mes'])} ({datos['pct_mes']}% del mes)."
        )
        if datos["vecino_anterior"]:
            resumen += f" Arriba: {datos['vecino_anterior']['proveedor']} ({_ficha_kg(datos['vecino_anterior']['kg'])})."
        else:
            resumen += " Fue el líder del mes."
        if datos["vecino_siguiente"]:
            resumen += f" Abajo: {datos['vecino_siguiente']['proveedor']} ({_ficha_kg(datos['vecino_siguiente']['kg'])})."

    return {
        "titulo": f"Ranking de vecinos {periodo}: {proveedor}",
        "resumen": resumen,
        "datos": datos,
        "senales": senales,
    }


def _ficha_perfil_proveedor_material_historico(cur_op, cur_lab, tarea):
    """Ficha determinística: material dentro de un proveedor, actual + historia."""
    material = tarea["entidad"]
    proveedor = tarea["entidad_2"]
    periodo = tarea["periodo"]
    fuente_material = tarea.get("material_fuente") or "Description"
    es_normalizado = fuente_material == "DescriptionNormalizada"
    datos = {"proveedor": proveedor, "material": material, "periodo": periodo,
             "material_fuente": fuente_material}
    senales = {"material_normalizado": es_normalizado}

    cur_lab.execute("""
        SELECT kg_actual, pct_actual_contexto, meses_con_actividad,
               confiabilidad, historial_json, ultimo_mes_con_actividad
        FROM ia_biografia_protagonistas
        WHERE tipo_entidad = 'proveedor_material' AND entidad = %s AND entidad_2 = %s
          AND periodo_actual = %s AND criterio_version = %s
        LIMIT 1
    """, (material, proveedor, periodo, DERIVADAS_VERSION))
    bio = cur_lab.fetchone()
    senales["tiene_biografia"] = bool(bio)

    if bio:
        try:
            historial = json.loads(bio["historial_json"] or "[]")
        except Exception:
            historial = []
        datos.update({
            "kg_actual": float(bio["kg_actual"] or 0),
            "pct_dentro_proveedor": float(bio["pct_actual_contexto"] or 0),
            "meses_con_actividad": int(bio["meses_con_actividad"] or 0),
            "confiabilidad": bio["confiabilidad"],
            "ultimo_mes_con_actividad": bio["ultimo_mes_con_actividad"],
            "historial_12m": historial[-12:],
        })
    else:
        # Fallback: historia en meses cerrados desde la base operativa
        # (incluye variantes crudas del proveedor canónico por alias; la
        # condición de material respeta la fuente de la tarea, sin mezclar)
        _mapa_f, inverso_f = cargar_alias_proveedores(cur_lab)
        variantes_f = variantes_proveedor(proveedor, inverso_f)
        marcadores_f = ", ".join(["%s"] * len(variantes_f))
        if fuente_material == "DescriptionNormalizada":
            cond_mat_f = "DescriptionNormalizada = %s"
            params_mat_f = [material]
        else:
            cond_mat_f = "(Description = %s OR Materiales = %s)"
            params_mat_f = [material, material]
        cur_op.execute(f"""
            SELECT DATE_FORMAT(FechaHora, '%%Y-%%m') AS periodo,
                   COUNT(*) AS registros,
                   COALESCE(SUM(kilos), 0) AS kg
            FROM TiposDeMovimiento
            WHERE TiposDeMovimiento LIKE 'ING %%'
              AND Proveedor IN ({marcadores_f})
              AND {cond_mat_f}
              AND DATE_FORMAT(FechaHora, '%%Y-%%m') < %s
            GROUP BY periodo
            ORDER BY periodo
        """, tuple(variantes_f + params_mat_f + [periodo]))
        hist = [
            {"periodo": h["periodo"], "kg": float(h["kg"] or 0), "registros": int(h["registros"] or 0)}
            for h in (cur_op.fetchall() or [])
        ]
        cur_lab.execute("""
            SELECT kg_entidad, pct_del_contexto
            FROM ia_protagonistas_operativos
            WHERE nivel = 2 AND entidad = %s AND entidad_2 = %s
              AND periodo = %s AND criterio_version = %s
            LIMIT 1
        """, (material, proveedor, periodo, DERIVADAS_VERSION))
        prot = cur_lab.fetchone() or {}
        datos.update({
            "kg_actual": float(prot.get("kg_entidad") or 0),
            "pct_dentro_proveedor": float(prot.get("pct_del_contexto") or 0),
            "meses_con_actividad": len(hist),
            "confiabilidad": None,
            "ultimo_mes_con_actividad": hist[-1]["periodo"] if hist else None,
            "historial_12m": hist[-12:],
        })

    senales["meses_con_actividad"] = datos["meses_con_actividad"]
    n = datos["meses_con_actividad"]
    etiqueta_mat = "material normalizado" if es_normalizado else "material"
    resumen = (
        f"{material} ({etiqueta_mat}) de {proveedor}: {_ficha_kg(datos['kg_actual'])} en {periodo} "
        f"({datos['pct_dentro_proveedor']}% de los kilos del proveedor); "
    )
    resumen += (
        f"activo {n} meses cerrados previos (último: {datos['ultimo_mes_con_actividad']})."
        if n else "sin meses cerrados previos con este material."
    )

    return {
        "titulo": (f"Perfil histórico proveedor+material normalizado: {proveedor} → {material}"
                   if es_normalizado else
                   f"Perfil histórico proveedor+material: {proveedor} → {material}"),
        "resumen": resumen,
        "datos": datos,
        "senales": senales,
    }


def _ficha_perfil_material_global(cur_op, tarea, mapa_alias=None):
    """Ficha determinística: material a nivel global (todos los proveedores, meses cerrados).
    mapa_alias canoniza los proveedores antes de agrupar/rankear. La condición de
    material respeta material_fuente de la tarea (crudo o normalizado, sin mezclar)."""
    material = tarea["entidad"]
    periodo = tarea["periodo"]
    fuente_material = tarea.get("material_fuente") or "Description"
    es_normalizado = fuente_material == "DescriptionNormalizada"
    if es_normalizado:
        cond_mat_g = "DescriptionNormalizada = %s"
        params_mat_g = [material]
    else:
        cond_mat_g = "(Description = %s OR Materiales = %s)"
        params_mat_g = [material, material]
    inicio_periodo = f"{periodo}-01"  # meses cerrados = anteriores al periodo de la tarea

    cur_op.execute(f"""
        SELECT Proveedor,
               COUNT(*) AS registros,
               COALESCE(SUM(kilos), 0) AS kg
        FROM TiposDeMovimiento
        WHERE TiposDeMovimiento LIKE 'ING %%'
          AND {cond_mat_g}
          AND FechaHora < %s
          AND Proveedor IS NOT NULL AND TRIM(Proveedor) <> ''
        GROUP BY Proveedor
        ORDER BY kg DESC
    """, tuple(params_mat_g + [inicio_periodo]))
    _agr_mat = {}
    for p in (cur_op.fetchall() or []):
        canon = canonizar_proveedor(p["Proveedor"], mapa_alias)
        acc = _agr_mat.setdefault(canon, {"proveedor": canon, "kg": 0.0, "registros": 0})
        acc["kg"] += float(p["kg"] or 0)
        acc["registros"] += int(p["registros"] or 0)
    provs = sorted(_agr_mat.values(), key=lambda x: -x["kg"])
    kg_total = sum(p["kg"] for p in provs)

    cur_op.execute(f"""
        SELECT DATE_FORMAT(FechaHora, '%%Y-%%m') AS periodo,
               COUNT(*) AS registros,
               COALESCE(SUM(kilos), 0) AS kg
        FROM TiposDeMovimiento
        WHERE TiposDeMovimiento LIKE 'ING %%'
          AND {cond_mat_g}
          AND FechaHora < %s
        GROUP BY periodo
        ORDER BY periodo DESC
        LIMIT 12
    """, tuple(params_mat_g + [inicio_periodo]))
    kg_por_mes = [
        {"periodo": h["periodo"], "kg": float(h["kg"] or 0), "registros": int(h["registros"] or 0)}
        for h in reversed(cur_op.fetchall() or [])
    ]

    dominante = provs[0] if provs else None
    pct_dominante = round(dominante["kg"] / kg_total * 100, 2) if dominante and kg_total > 0 else 0
    datos = {
        "material": material,
        "material_fuente": fuente_material,
        "periodo": periodo,
        "proveedores_historicos": provs[:10],
        "proveedores_distintos": len(provs),
        "kg_total_historico": round(kg_total, 2),
        "kg_por_mes_12m": kg_por_mes,
        "proveedor_dominante": ({**dominante, "pct": pct_dominante} if dominante else None),
    }
    senales = {
        "proveedores_distintos": len(provs),
        "monoproveedor": len(provs) == 1,
        "dominante_concentra_mas_50pct": pct_dominante > 50,
        "material_normalizado": es_normalizado,
    }

    etiqueta_mat = "material normalizado" if es_normalizado else "material"
    if not provs:
        resumen = f"{material} ({etiqueta_mat}): sin ingresos registrados en meses cerrados."
    else:
        kg_12m = sum(h["kg"] for h in kg_por_mes)
        resumen = (
            f"{material} ({etiqueta_mat}): traído históricamente por {len(provs)} proveedores; "
            f"dominante {dominante['proveedor']} ({pct_dominante}% del kg histórico). "
            f"Últimos {len(kg_por_mes)} meses cerrados: {_ficha_kg(kg_12m)}."
        )

    return {
        "titulo": (f"Perfil global de material normalizado: {material}"
                   if es_normalizado else f"Perfil global de material: {material}"),
        "resumen": resumen,
        "datos": datos,
        "senales": senales,
    }


def _propagar_dominantes_material(cur_lab, tarea, ficha):
    """Propagación determinística de la red de curiosidad: cuando una ficha
    perfil_material_global descubre proveedores dominantes históricos, encola
    su estudio (perfil histórico, ranking de vecinos y proveedor+material).
    Sin Ollama. Respeta MAX_COLA_PROFUNDIDAD e ignora dominio bajo o volumen
    histórico chico. Devuelve {"encoladas", "dedupe", "proveedores"}."""
    resultado = {"encoladas": 0, "dedupe": 0, "proveedores": []}
    profundidad_nueva = int(tarea.get("profundidad") or 1) + 1
    if profundidad_nueva > MAX_COLA_PROFUNDIDAD:
        return resultado

    material = tarea["entidad"]
    datos = (ficha or {}).get("datos") or {}
    kg_total = float(datos.get("kg_total_historico") or 0)
    if kg_total <= 0:
        return resultado
    kg_por_mes = datos.get("kg_por_mes_12m") or []
    ultimo_mes_cerrado = kg_por_mes[-1]["periodo"] if kg_por_mes else None

    for p in (datos.get("proveedores_historicos") or []):
        proveedor = p.get("proveedor")
        kg_p = float(p.get("kg") or 0)
        registros_p = int(p.get("registros") or 0)
        if not proveedor:
            continue
        # Volumen histórico muy chico: no amerita estudio, sea cual sea el dominio
        if kg_p < COLA_KG_MINIMO_DOMINANTE:
            continue
        pct = round(kg_p / kg_total * 100, 2)
        if pct >= 80:
            factor = 1.0    # prioridad alta
        elif pct >= 50:
            factor = 0.6    # prioridad media
        elif pct >= 30:
            factor = 0.35   # prioridad baja/media (ya pasó el piso de kg)
        else:
            continue        # dominio bajo: ignorar

        # Misma escala que _score de protagonistas, con descuento por profundidad
        prioridad = round(
            (kg_p * 0.7 + registros_p * 10.0) * factor * (0.8 ** (profundidad_nueva - 1)), 4
        )
        motivo = (
            f"Proveedor dominante histórico del material {material} "
            f"con {pct}% del kg histórico"
        )

        tareas_nuevas = [
            {"tipo_tarea": "perfil_proveedor_historico", "periodo": tarea["periodo"],
             "tipo_nodo": "proveedor", "entidad": proveedor, "entidad_2": None},
        ]
        if ultimo_mes_cerrado:
            tareas_nuevas.append(
                {"tipo_tarea": "ranking_vecinos_proveedor_mes", "periodo": ultimo_mes_cerrado,
                 "tipo_nodo": "proveedor", "entidad": proveedor, "entidad_2": None}
            )
        if pct >= 50:
            tareas_nuevas.append(
                {"tipo_tarea": "perfil_proveedor_material_historico", "periodo": tarea["periodo"],
                 "tipo_nodo": "proveedor_material", "entidad": material, "entidad_2": proveedor,
                 "material_fuente": tarea.get("material_fuente") or "Description"}
            )

        nuevas = 0
        for t in tareas_nuevas:
            t.update({
                "origen": "material_global_dominante",
                "motivo": motivo,
                "prioridad_score": prioridad,
                "profundidad": profundidad_nueva,
            })
            insertada = _encolar_curiosidad(cur_lab, t)
            nuevas += insertada
            resultado["dedupe"] += 1 - insertada
        resultado["encoladas"] += nuevas
        if nuevas:
            resultado["proveedores"].append(f"{proveedor} ({pct}%)")

    return resultado


def propagar_dominantes_materiales_existentes(forzar=False):
    """Backfill determinístico: recorre las fichas perfil_material_global ya
    completadas y ejecuta la propagación de proveedores dominantes para cada
    una, con el dedupe habitual. Sin Ollama. Respeta MAX_COLA_PROFUNDIDAD y
    COLA_KG_MINIMO_DOMINANTE.
    Por defecto procesa solo la ficha más reciente de cada material; con
    forzar=True revisa todas las filas (el dedupe evita duplicados igual)."""
    resultado = {
        "ok": False,
        "fichas_revisadas": 0,
        "tareas_encoladas": 0,
        "tareas_dedupe": 0,
        "errores": [],
    }
    conn_lab = get_ia_connection()
    if not conn_lab:
        resultado["errores"].append("Sin conexión a crowdbot_lab")
        return resultado
    conn_op = None
    cur_op = None
    mapa_alias_bf = None
    try:
        cur_lab = conn_lab.cursor()
        cur_lab.execute("""
            SELECT id, cola_id, tipo_nodo, entidad, entidad_2, material_fuente,
                   periodo, prioridad_score, datos_json
            FROM ia_fichas_operativas
            WHERE tipo_tarea = 'perfil_material_global'
            ORDER BY id DESC
        """)
        fichas = cur_lab.fetchall() or []

        materiales_vistos = set()
        proveedores_descubiertos = []
        for f in fichas:
            material = f["entidad"]
            if not forzar and material in materiales_vistos:
                continue  # solo la ficha más reciente de cada material
            materiales_vistos.add(material)
            resultado["fichas_revisadas"] += 1

            try:
                datos = json.loads(f["datos_json"] or "{}")
            except Exception:
                datos = {}

            # Si datos_json no alcanza, recalcular determinísticamente desde SQL
            if not (datos.get("proveedores_historicos") and float(datos.get("kg_total_historico") or 0) > 0):
                try:
                    if conn_op is None:
                        conn_op = get_db_connection()
                        if conn_op:
                            cur_op = conn_op.cursor()
                    if cur_op is not None:
                        if mapa_alias_bf is None:
                            mapa_alias_bf, _ = cargar_alias_proveedores(cur_lab)
                        ficha_recalc = _ficha_perfil_material_global(
                            cur_op, {"entidad": material, "periodo": f["periodo"]},
                            mapa_alias=mapa_alias_bf
                        )
                        datos = ficha_recalc.get("datos") or {}
                except Exception as e_rec:
                    resultado["errores"].append(f"{material}: recalculo fallo: {e_rec}")
                    continue

            # Profundidad real de la tarea que originó la ficha si existe;
            # si no hay info, 2 (material global suele nacer de un protagonista)
            profundidad = 2
            if f.get("cola_id"):
                cur_lab.execute(
                    "SELECT profundidad FROM ia_cola_curiosidad WHERE id = %s",
                    (f["cola_id"],)
                )
                fila_cola = cur_lab.fetchone()
                if fila_cola and fila_cola.get("profundidad"):
                    profundidad = int(fila_cola["profundidad"])

            # Tarea mínima reconstruida desde la ficha
            tarea_min = {
                "tipo_tarea": "perfil_material_global",
                "tipo_nodo": f.get("tipo_nodo") or "material",
                "entidad": material,
                "entidad_2": f.get("entidad_2"),
                "material_fuente": f.get("material_fuente") or "Description",
                "periodo": f["periodo"],
                "prioridad_score": float(f.get("prioridad_score") or 0),
                "profundidad": profundidad,
            }
            prop = _propagar_dominantes_material(cur_lab, tarea_min, {"datos": datos})
            resultado["tareas_encoladas"] += prop["encoladas"]
            resultado["tareas_dedupe"] += prop["dedupe"]
            proveedores_descubiertos.extend(prop["proveedores"])

        conn_lab.commit()
        cur_lab.close()
        resultado["ok"] = True

        add_reasoning_step(
            "cola_curiosidad_backfill_dominantes",
            (f"Backfill dominantes: {resultado['fichas_revisadas']} fichas de material revisadas; "
             f"{resultado['tareas_encoladas']} tareas encoladas, {resultado['tareas_dedupe']} dedupe"),
            ("Descubiertos: " + "; ".join(proveedores_descubiertos[:12])) if proveedores_descubiertos else None
        )
    except Exception as e:
        resultado["errores"].append(str(e))
        print(f"Error en propagar_dominantes_materiales_existentes: {e}", file=sys.stderr)
    finally:
        try:
            conn_lab.close()
        except Exception:
            pass
        try:
            if cur_op is not None:
                cur_op.close()
            if conn_op is not None:
                conn_op.close()
        except Exception:
            pass
    return resultado


def hay_cola_curiosidad_pendiente_disponible(cur_lab=None):
    """True si hay al menos una tarea pendiente y fuera de cooldown en
    ia_cola_curiosidad. Acepta un cursor lab existente o abre conexión propia."""
    q = """
        SELECT 1 FROM ia_cola_curiosidad
        WHERE estado = 'pendiente'
          AND (cooldown_hasta IS NULL OR cooldown_hasta <= NOW())
        LIMIT 1
    """
    try:
        if cur_lab is not None:
            cur_lab.execute(q)
            return cur_lab.fetchone() is not None
        conn = get_ia_connection()
        if not conn:
            return False
        cur = conn.cursor()
        cur.execute(q)
        hay = cur.fetchone() is not None
        cur.close()
        conn.close()
        return hay
    except Exception:
        return False


def ejecutar_tarea_cola_curiosidad():
    """Consumidor determinístico de ia_cola_curiosidad: toma la tarea pendiente de
    mayor prioridad, calcula su ficha según tipo_tarea y la guarda en
    ia_fichas_operativas. No llama a Ollama. Devuelve dict con el resultado,
    o None si no hay tareas pendientes elegibles."""
    conn_lab = get_ia_connection()
    if not conn_lab:
        return None
    tarea = None
    conn_op = None
    try:
        cur_lab = conn_lab.cursor()
        cur_lab.execute("""
            SELECT id, tipo_tarea, tipo_nodo, entidad, entidad_2, material_fuente,
                   periodo, origen, motivo, prioridad_score, profundidad
            FROM ia_cola_curiosidad
            WHERE estado = 'pendiente'
              AND (cooldown_hasta IS NULL OR cooldown_hasta <= NOW())
            ORDER BY prioridad_score DESC, id ASC
            LIMIT 1
        """)
        tarea = cur_lab.fetchone()
        if not tarea:
            cur_lab.close()
            conn_lab.close()
            return None

        cur_lab.execute(
            "UPDATE ia_cola_curiosidad SET estado = 'en_proceso' WHERE id = %s",
            (tarea["id"],)
        )
        conn_lab.commit()

        tipo = tarea["tipo_tarea"]
        if tipo in ("perfil_proveedor_material_historico", "perfil_material_global"):
            conn_op = get_db_connection()
            if not conn_op:
                raise RuntimeError("Sin conexión a la base operativa")
            cur_op = conn_op.cursor()

        if tipo == "perfil_proveedor_historico":
            ficha = _ficha_perfil_proveedor_historico(cur_lab, tarea)
        elif tipo == "ranking_vecinos_proveedor_mes":
            ficha = _ficha_ranking_vecinos_proveedor_mes(cur_lab, tarea)
        elif tipo == "perfil_proveedor_material_historico":
            ficha = _ficha_perfil_proveedor_material_historico(cur_op, cur_lab, tarea)
        elif tipo == "perfil_material_global":
            _mapa_alias_c, _ = cargar_alias_proveedores(cur_lab)
            ficha = _ficha_perfil_material_global(cur_op, tarea, mapa_alias=_mapa_alias_c)
        else:
            raise ValueError(f"tipo_tarea sin constructor de ficha: {tipo}")

        cur_lab.execute("""
            INSERT INTO ia_fichas_operativas
                (cola_id, tipo_tarea, tipo_nodo, entidad, entidad_2, material_fuente, periodo,
                 titulo, resumen_deterministico, datos_json, senales_json, prioridad_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            tarea["id"], tipo, tarea["tipo_nodo"], tarea["entidad"], tarea["entidad_2"],
            tarea.get("material_fuente") or "Description",
            tarea["periodo"], ficha["titulo"], ficha["resumen"],
            json.dumps(ficha.get("datos") or {}, ensure_ascii=False, default=str),
            json.dumps(ficha.get("senales") or {}, ensure_ascii=False, default=str),
            float(tarea["prioridad_score"] or 0),
        ))
        cur_lab.execute("""
            UPDATE ia_cola_curiosidad
            SET estado = 'completada', resultado_resumen = %s
            WHERE id = %s
        """, (shorten_text(ficha["resumen"], 290), tarea["id"]))

        # Propagación determinística: material global -> proveedores dominantes
        propagacion = None
        if tipo == "perfil_material_global":
            try:
                propagacion = _propagar_dominantes_material(cur_lab, tarea, ficha)
            except Exception as e_prop:
                add_reasoning_step(
                    "cola_curiosidad_error",
                    f"Propagación desde material {tarea['entidad']} falló",
                    shorten_text(str(e_prop), 200)
                )
        conn_lab.commit()
        cur_lab.close()
        conn_lab.close()

        add_reasoning_step(
            "cola_curiosidad_ficha",
            ficha["titulo"],
            shorten_text(ficha["resumen"], 300)
        )
        if propagacion and propagacion.get("encoladas"):
            add_reasoning_step(
                "cola_curiosidad_propagada",
                f"Material {tarea['entidad']}: encolados {propagacion['encoladas']} estudios de proveedores dominantes",
                "Descubiertos: " + "; ".join(propagacion.get("proveedores") or [])
            )
        if propagacion and propagacion.get("dedupe"):
            add_reasoning_step(
                "cola_curiosidad_dedupe",
                f"Material {tarea['entidad']}: {propagacion['dedupe']} tareas de dominantes ya existían, no se duplicaron",
                None
            )
        return {
            "ok": True, "cola_id": tarea["id"], "tipo_tarea": tipo,
            "titulo": ficha["titulo"], "propagacion": propagacion,
        }

    except Exception as e:
        # La tarea no se borra: queda en 'error' con el motivo, para revisión.
        try:
            if tarea:
                cur_err = conn_lab.cursor()
                cur_err.execute("""
                    UPDATE ia_cola_curiosidad
                    SET estado = 'error', resultado_resumen = %s
                    WHERE id = %s
                """, (shorten_text(str(e), 290), tarea["id"]))
                conn_lab.commit()
                cur_err.close()
        except Exception:
            pass
        try:
            conn_lab.close()
        except Exception:
            pass
        if tarea:
            add_reasoning_step(
                "cola_curiosidad_error",
                f"Falló ficha de cola id={tarea['id']} ({tarea.get('tipo_tarea')})",
                shorten_text(str(e), 300)
            )
            return {"ok": False, "cola_id": tarea["id"], "error": str(e)}
        print(f"Error en ejecutar_tarea_cola_curiosidad: {e}", file=sys.stderr)
        return None
    finally:
        try:
            if conn_op:
                conn_op.close()
        except Exception:
            pass


def _generar_periodos_desde_db(cur_op):
    """Detecta el primer mes válido en TiposDeMovimiento y devuelve lista de periodos
    desde max(primer_mes_valido, '2023-11-01') hasta el mes actual inclusive.
    El mes actual es 'abierto'; todos los anteriores son 'cerrado'."""
    cur_op.execute("""
        SELECT DATE_FORMAT(MIN(FechaHora), '%%Y-%%m') AS primer_mes
        FROM TiposDeMovimiento
        WHERE FechaHora IS NOT NULL
          AND FechaHora >= '2020-01-01'
          AND FechaHora <= NOW()
    """)
    row = cur_op.fetchone() or {}
    primer_mes_db = row.get("primer_mes") or "2023-11"
    # Cota mínima: no retroceder más de 2023-11
    primer_mes = max(primer_mes_db, "2023-11")

    hoy = date.today()
    mes_actual = hoy.strftime("%Y-%m")

    periodos = []
    y, m = int(primer_mes[:4]), int(primer_mes[5:7])
    while True:
        p = f"{y:04d}-{m:02d}"
        estado = "abierto" if p == mes_actual else "cerrado"
        periodos.append({"periodo": p, "estado": estado})
        if p == mes_actual:
            break
        m += 1
        if m > 12:
            m = 1
            y += 1
    return periodos


def _recalcular_normalidad(cur_lab):
    """Recalcula las tablas de normalidad sobre meses cerrados.
    Trunca y recarga ia_normalidad_mensual_* completo."""
    # tipo_movimiento
    cur_lab.execute("""
        INSERT INTO ia_normalidad_mensual_tipo_movimiento
            (criterio_version, entidad, meses_con_actividad,
             kg_promedio_mensual, kg_min_mensual, kg_max_mensual,
             registros_promedio_mensual, primer_periodo, ultimo_periodo)
        SELECT
            criterio_version,
            TiposDeMovimiento AS entidad,
            COUNT(DISTINCT periodo)   AS meses_con_actividad,
            AVG(kilos_total)          AS kg_promedio_mensual,
            MIN(kilos_total)          AS kg_min_mensual,
            MAX(kilos_total)          AS kg_max_mensual,
            AVG(registros)            AS registros_promedio_mensual,
            MIN(periodo)              AS primer_periodo,
            MAX(periodo)              AS ultimo_periodo
        FROM ia_resumen_mensual_tipo_movimiento
        WHERE estado_periodo = 'cerrado' AND criterio_version = %s
        GROUP BY criterio_version, TiposDeMovimiento
        ON DUPLICATE KEY UPDATE
            meses_con_actividad        = VALUES(meses_con_actividad),
            kg_promedio_mensual        = VALUES(kg_promedio_mensual),
            kg_min_mensual             = VALUES(kg_min_mensual),
            kg_max_mensual             = VALUES(kg_max_mensual),
            registros_promedio_mensual = VALUES(registros_promedio_mensual),
            primer_periodo             = VALUES(primer_periodo),
            ultimo_periodo             = VALUES(ultimo_periodo),
            actualizado_en             = CURRENT_TIMESTAMP
    """, (DERIVADAS_VERSION,))

    # produccion_maquina
    cur_lab.execute("""
        INSERT INTO ia_normalidad_mensual_produccion_maquina
            (criterio_version, entidad, meses_con_actividad,
             kg_promedio_mensual, kg_min_mensual, kg_max_mensual,
             registros_promedio_mensual, primer_periodo, ultimo_periodo)
        SELECT
            criterio_version,
            Maquina AS entidad,
            COUNT(DISTINCT periodo)   AS meses_con_actividad,
            AVG(kilos_total)          AS kg_promedio_mensual,
            MIN(kilos_total)          AS kg_min_mensual,
            MAX(kilos_total)          AS kg_max_mensual,
            AVG(registros)            AS registros_promedio_mensual,
            MIN(periodo)              AS primer_periodo,
            MAX(periodo)              AS ultimo_periodo
        FROM ia_resumen_mensual_produccion_maquina
        WHERE estado_periodo = 'cerrado' AND criterio_version = %s
        GROUP BY criterio_version, Maquina
        ON DUPLICATE KEY UPDATE
            meses_con_actividad        = VALUES(meses_con_actividad),
            kg_promedio_mensual        = VALUES(kg_promedio_mensual),
            kg_min_mensual             = VALUES(kg_min_mensual),
            kg_max_mensual             = VALUES(kg_max_mensual),
            registros_promedio_mensual = VALUES(registros_promedio_mensual),
            primer_periodo             = VALUES(primer_periodo),
            ultimo_periodo             = VALUES(ultimo_periodo),
            actualizado_en             = CURRENT_TIMESTAMP
    """, (DERIVADAS_VERSION,))

    # ingresos_proveedor
    cur_lab.execute("""
        INSERT INTO ia_normalidad_mensual_ingresos_proveedor
            (criterio_version, entidad, meses_con_actividad,
             kg_promedio_mensual, kg_min_mensual, kg_max_mensual,
             registros_promedio_mensual, primer_periodo, ultimo_periodo)
        SELECT
            criterio_version,
            Proveedor AS entidad,
            COUNT(DISTINCT periodo)   AS meses_con_actividad,
            AVG(kilos_total)          AS kg_promedio_mensual,
            MIN(kilos_total)          AS kg_min_mensual,
            MAX(kilos_total)          AS kg_max_mensual,
            AVG(registros)            AS registros_promedio_mensual,
            MIN(periodo)              AS primer_periodo,
            MAX(periodo)              AS ultimo_periodo
        FROM ia_resumen_mensual_ingresos_proveedor
        WHERE estado_periodo = 'cerrado' AND criterio_version = %s
        GROUP BY criterio_version, Proveedor
        ON DUPLICATE KEY UPDATE
            meses_con_actividad        = VALUES(meses_con_actividad),
            kg_promedio_mensual        = VALUES(kg_promedio_mensual),
            kg_min_mensual             = VALUES(kg_min_mensual),
            kg_max_mensual             = VALUES(kg_max_mensual),
            registros_promedio_mensual = VALUES(registros_promedio_mensual),
            primer_periodo             = VALUES(primer_periodo),
            ultimo_periodo             = VALUES(ultimo_periodo),
            actualizado_en             = CURRENT_TIMESTAMP
    """, (DERIVADAS_VERSION,))


def guardar_tablas_derivadas_si_corresponde(forzar=False):
    """Calcula y persiste resúmenes mensuales SQL-puro en crowdbot_lab.
    Procesa desde el primer mes válido en TiposDeMovimiento (mín 2023-11)
    hasta el mes actual. Calcula normalidad sobre meses cerrados.
    No usa Ollama."""
    resultado = {
        "ok": False,
        "criterio_version": DERIVADAS_VERSION,
        "periodos_procesados": [],
        "filas_tipo_movimiento": 0,
        "filas_produccion_maquina": 0,
        "filas_ingresos_proveedor": 0,
        "errores": [],
    }
    try:
        conn_lab = get_ia_connection()
        conn_op = get_db_connection()
        if not conn_lab or not conn_op:
            resultado["errores"].append("Sin conexión a crowdbot_lab o sh_etp_v2_local")
            return resultado

        cur_lab = conn_lab.cursor()
        cur_op = conn_op.cursor()
        _ensure_tablas_derivadas(cur_lab)
        conn_lab.commit()

        # Alias de proveedores: canonizar SIEMPRE al recalcular, y limpiar
        # artefactos derivados que quedaron con nombres crudos (idempotente)
        alias_prov, _inverso_prov = cargar_alias_proveedores(cur_lab)
        _limpiar_artefactos_alias(cur_lab, alias_prov)

        # DescriptionNormalizada: dimensión nueva; puede no existir todavía
        tiene_desc_norm = False
        try:
            cur_op.execute("SHOW COLUMNS FROM TiposDeMovimiento LIKE 'DescriptionNormalizada'")
            tiene_desc_norm = cur_op.fetchone() is not None
        except Exception:
            tiene_desc_norm = False
        resultado["description_normalizada_activa"] = tiene_desc_norm
        resultado["filas_description_normalizada"] = 0

        periodos = _generar_periodos_desde_db(cur_op)

        for p in periodos:
            periodo = p["periodo"]
            estado = p["estado"]
            yr, mo = int(periodo[:4]), int(periodo[5:7])
            inicio = f"{periodo}-01"
            ultimo_dia = (date(yr, mo % 12 + 1, 1) - timedelta(days=1)) if mo < 12 else date(yr, 12, 31)
            fin_excl = (ultimo_dia + timedelta(days=1)).strftime("%Y-%m-%d")

            # --- tipo_movimiento ---
            cur_op.execute("""
                SELECT TiposDeMovimiento,
                       COUNT(*) AS registros,
                       COALESCE(SUM(kilos), 0) AS kilos_total
                FROM TiposDeMovimiento
                WHERE FechaHora >= %s AND FechaHora < %s
                  AND TiposDeMovimiento IS NOT NULL AND TiposDeMovimiento <> ''
                GROUP BY TiposDeMovimiento
            """, (inicio, fin_excl))
            rows_tipo = cur_op.fetchall() or []
            cur_lab.execute(
                "DELETE FROM ia_resumen_mensual_tipo_movimiento WHERE periodo = %s AND criterio_version = %s",
                (periodo, DERIVADAS_VERSION)
            )
            for r in rows_tipo:
                cur_lab.execute("""
                    INSERT INTO ia_resumen_mensual_tipo_movimiento
                        (periodo, estado_periodo, criterio_version, TiposDeMovimiento, registros, kilos_total)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        registros = VALUES(registros),
                        kilos_total = VALUES(kilos_total),
                        estado_periodo = VALUES(estado_periodo),
                        actualizado_en = CURRENT_TIMESTAMP
                """, (periodo, estado, DERIVADAS_VERSION, r["TiposDeMovimiento"], r["registros"], r["kilos_total"]))
            resultado["filas_tipo_movimiento"] += len(rows_tipo)

            # --- produccion_maquina ---
            cur_op.execute("""
                SELECT Maquina,
                       COUNT(*) AS registros,
                       COALESCE(SUM(kilos), 0) AS kilos_total
                FROM TiposDeMovimiento
                WHERE FechaHora >= %s AND FechaHora < %s
                  AND TiposDeMovimiento = 'PRODUCCION'
                  AND Maquina IS NOT NULL AND Maquina <> ''
                GROUP BY Maquina
            """, (inicio, fin_excl))
            rows_maq = cur_op.fetchall() or []
            cur_lab.execute(
                "DELETE FROM ia_resumen_mensual_produccion_maquina WHERE periodo = %s AND criterio_version = %s",
                (periodo, DERIVADAS_VERSION)
            )
            for r in rows_maq:
                cur_lab.execute("""
                    INSERT INTO ia_resumen_mensual_produccion_maquina
                        (periodo, estado_periodo, criterio_version, Maquina, registros, kilos_total)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        registros = VALUES(registros),
                        kilos_total = VALUES(kilos_total),
                        estado_periodo = VALUES(estado_periodo),
                        actualizado_en = CURRENT_TIMESTAMP
                """, (periodo, estado, DERIVADAS_VERSION, r["Maquina"], r["registros"], r["kilos_total"]))
            resultado["filas_produccion_maquina"] += len(rows_maq)

            # --- ingresos_proveedor ---
            cur_op.execute("""
                SELECT Proveedor,
                       COUNT(*) AS registros,
                       COALESCE(SUM(kilos), 0) AS kilos_total
                FROM TiposDeMovimiento
                WHERE FechaHora >= %s AND FechaHora < %s
                  AND TiposDeMovimiento LIKE 'ING %%'
                  AND Proveedor IS NOT NULL AND Proveedor <> ''
                GROUP BY Proveedor
            """, (inicio, fin_excl))
            rows_prov = cur_op.fetchall() or []
            # Canonizar por alias y re-agrupar: "Sandro mengar"/"Sandro" suman
            # bajo "Sandro Melgar" en el resumen mensual
            provs_canon = {}
            for r in rows_prov:
                canon = canonizar_proveedor(r["Proveedor"], alias_prov)
                acc = provs_canon.setdefault(canon, {"registros": 0, "kilos_total": 0.0})
                acc["registros"] += int(r["registros"] or 0)
                acc["kilos_total"] += float(r["kilos_total"] or 0)
            cur_lab.execute(
                "DELETE FROM ia_resumen_mensual_ingresos_proveedor WHERE periodo = %s AND criterio_version = %s",
                (periodo, DERIVADAS_VERSION)
            )
            for prov, acc in provs_canon.items():
                cur_lab.execute("""
                    INSERT INTO ia_resumen_mensual_ingresos_proveedor
                        (periodo, estado_periodo, criterio_version, Proveedor, registros, kilos_total)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        registros = VALUES(registros),
                        kilos_total = VALUES(kilos_total),
                        estado_periodo = VALUES(estado_periodo),
                        actualizado_en = CURRENT_TIMESTAMP
                """, (periodo, estado, DERIVADAS_VERSION, prov, acc["registros"], acc["kilos_total"]))
            resultado["filas_ingresos_proveedor"] += len(provs_canon)

            # --- description_normalizada (dimensión nueva; NO se mezcla con Description vieja) ---
            if tiene_desc_norm:
                try:
                    cur_op.execute("""
                        SELECT COUNT(*) AS registros_total,
                               COALESCE(SUM(kilos), 0) AS kilos_total_periodo,
                               SUM(CASE WHEN DescriptionNormalizada IS NOT NULL AND TRIM(DescriptionNormalizada) <> '' THEN 1 ELSE 0 END) AS registros_norm,
                               COALESCE(SUM(CASE WHEN DescriptionNormalizada IS NOT NULL AND TRIM(DescriptionNormalizada) <> '' THEN kilos ELSE 0 END), 0) AS kilos_norm
                        FROM TiposDeMovimiento
                        WHERE FechaHora >= %s AND FechaHora < %s
                    """, (inicio, fin_excl))
                    tot_dn = cur_op.fetchone() or {}
                    registros_total_p = int(tot_dn.get("registros_total") or 0)
                    registros_norm_p = int(tot_dn.get("registros_norm") or 0)
                    kilos_total_p = float(tot_dn.get("kilos_total_periodo") or 0)
                    kilos_norm_p = float(tot_dn.get("kilos_norm") or 0)
                    cobertura_pct = round(registros_norm_p / registros_total_p * 100, 2) if registros_total_p > 0 else 0
                    pct_kilos_norm = round(kilos_norm_p / kilos_total_p * 100, 2) if kilos_total_p > 0 else 0
                    if cobertura_pct >= 90:
                        estado_conf = "fuerte"
                    elif cobertura_pct >= 30:
                        estado_conf = "parcial"
                    else:
                        estado_conf = "debil"  # no usar para conclusiones fuertes

                    cur_op.execute("""
                        SELECT DescriptionNormalizada AS dn,
                               COUNT(*) AS registros,
                               COALESCE(SUM(kilos), 0) AS kilos_total,
                               MIN(FechaHora) AS primer_movimiento,
                               MAX(FechaHora) AS ultimo_movimiento
                        FROM TiposDeMovimiento
                        WHERE FechaHora >= %s AND FechaHora < %s
                          AND DescriptionNormalizada IS NOT NULL AND TRIM(DescriptionNormalizada) <> ''
                        GROUP BY DescriptionNormalizada
                    """, (inicio, fin_excl))
                    rows_dn = cur_op.fetchall() or []
                    cur_lab.execute(
                        "DELETE FROM ia_resumen_mensual_description_normalizada WHERE periodo = %s AND criterio_version = %s",
                        (periodo, DERIVADAS_VERSION)
                    )
                    for r in rows_dn:
                        cur_lab.execute("""
                            INSERT INTO ia_resumen_mensual_description_normalizada
                                (periodo, estado_periodo, criterio_version, description_normalizada,
                                 registros_total_periodo, registros_normalizados_periodo,
                                 cobertura_pct_periodo, estado_confianza,
                                 registros, kilos_total, pct_kilos_normalizados_periodo,
                                 primer_movimiento, ultimo_movimiento)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                estado_periodo = VALUES(estado_periodo),
                                registros_total_periodo = VALUES(registros_total_periodo),
                                registros_normalizados_periodo = VALUES(registros_normalizados_periodo),
                                cobertura_pct_periodo = VALUES(cobertura_pct_periodo),
                                estado_confianza = VALUES(estado_confianza),
                                registros = VALUES(registros),
                                kilos_total = VALUES(kilos_total),
                                pct_kilos_normalizados_periodo = VALUES(pct_kilos_normalizados_periodo),
                                primer_movimiento = VALUES(primer_movimiento),
                                ultimo_movimiento = VALUES(ultimo_movimiento),
                                actualizado_en = CURRENT_TIMESTAMP
                        """, (
                            periodo, estado, DERIVADAS_VERSION, r["dn"],
                            registros_total_p, registros_norm_p,
                            cobertura_pct, estado_conf,
                            r["registros"], r["kilos_total"], pct_kilos_norm,
                            r["primer_movimiento"], r["ultimo_movimiento"],
                        ))
                    resultado["filas_description_normalizada"] += len(rows_dn)
                except Exception as e_dn:
                    resultado["errores"].append(f"description_normalizada {periodo}: {e_dn}")

            # --- registro en ia_periodos_procesados ---
            cur_lab.execute("""
                INSERT INTO ia_periodos_procesados
                    (periodo, estado_periodo, criterio_version,
                     filas_tipo_movimiento, filas_produccion_maquina, filas_ingresos_proveedor)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    estado_periodo = VALUES(estado_periodo),
                    filas_tipo_movimiento = VALUES(filas_tipo_movimiento),
                    filas_produccion_maquina = VALUES(filas_produccion_maquina),
                    filas_ingresos_proveedor = VALUES(filas_ingresos_proveedor),
                    actualizado_en = CURRENT_TIMESTAMP
            """, (periodo, estado, DERIVADAS_VERSION, len(rows_tipo), len(rows_maq), len(rows_prov)))
            resultado["periodos_procesados"].append({"periodo": periodo, "estado": estado})

        # Normalidad sobre meses cerrados
        _recalcular_normalidad(cur_lab)

        # Protagonistas operativos: calcular para periodo(s) abierto(s)
        filas_prot = 0
        for p in periodos:
            if p["estado"] == "abierto":
                try:
                    filas_prot += calcular_protagonistas_operativos(
                        cur_op, cur_lab, p["periodo"], p["estado"]
                    )
                except Exception as e_prot:
                    resultado["errores"].append(f"protagonistas: {e_prot}")

        # Biografías históricas determinísticas para protagonistas del mes actual
        filas_bio = 0
        periodo_actual = date.today().strftime("%Y-%m")
        try:
            filas_bio = calcular_biografia_protagonistas(cur_op, cur_lab, periodo_actual)
        except Exception as e_bio:
            resultado["errores"].append(f"biografia: {e_bio}")

        # Cola de curiosidad determinística desde protagonistas y biografías
        filas_cola = 0
        try:
            filas_cola = calcular_cola_curiosidad(cur_lab, periodo_actual)
        except Exception as e_cola:
            resultado["errores"].append(f"cola_curiosidad: {e_cola}")

        # Cola por cobertura mensual: proveedores históricos relevantes que no
        # aparecen como protagonistas actuales ni dominantes de materiales
        cobertura = {"encoladas": 0, "dedupe": 0, "periodos": 0}
        try:
            cobertura = calcular_cola_proveedores_por_cobertura(cur_lab)
        except Exception as e_cob:
            resultado["errores"].append(f"cola_cobertura: {e_cob}")

        conn_lab.commit()
        resultado["ok"] = True
        resultado["filas_protagonistas"] = filas_prot
        resultado["filas_biografias"] = filas_bio
        resultado["filas_cola_curiosidad"] = filas_cola
        resultado["filas_cola_cobertura"] = cobertura.get("encoladas", 0)
        resultado["cola_cobertura_dedupe"] = cobertura.get("dedupe", 0)

        add_reasoning_step(
            "tablas_derivadas",
            f"Derivadas actualizadas: {len(periodos)} periodos",
            (f"mensual_tipo={resultado['filas_tipo_movimiento']}, "
             f"produccion_maquina={resultado['filas_produccion_maquina']}, "
             f"ingresos_proveedor={resultado['filas_ingresos_proveedor']} | "
             f"version={DERIVADAS_VERSION}"),
        )

    except Exception as e:
        resultado["errores"].append(str(e))
        print(f"Error en guardar_tablas_derivadas_si_corresponde: {e}", file=sys.stderr)
    finally:
        try:
            cur_lab.close()
            conn_lab.close()
        except Exception:
            pass
        try:
            cur_op.close()
            conn_op.close()
        except Exception:
            pass
    return resultado


def leer_tablas_derivadas_resumen():
    """Lee un resumen liviano de las tablas derivadas para /api/ia/cerebro."""
    resumen = {
        "criterio_version": DERIVADAS_VERSION,
        "periodos": [],
        "conteo": {"tipo_movimiento": 0, "produccion_maquina": 0, "ingresos_proveedor": 0},
        "ultima_actualizacion": None,
        "muestra_tipo_movimiento": [],
        "muestra_produccion_maquina": [],
        "muestra_ingresos_proveedor": [],
    }
    try:
        conn = get_ia_connection()
        if not conn:
            return resumen
        cur = conn.cursor()

        cur.execute("""
            SELECT periodo, estado_periodo, actualizado_en
            FROM ia_periodos_procesados
            WHERE criterio_version = %s
            ORDER BY periodo DESC
            LIMIT 6
        """, (DERIVADAS_VERSION,))
        periodos = cur.fetchall() or []
        resumen["periodos"] = [
            {"periodo": r["periodo"], "estado": r["estado_periodo"],
             "actualizado_en": str(r["actualizado_en"]) if r["actualizado_en"] else None}
            for r in periodos
        ]
        if periodos:
            resumen["ultima_actualizacion"] = str(periodos[0]["actualizado_en"])

        periodo_ref = periodos[0]["periodo"] if periodos else None

        cur.execute("SELECT COUNT(*) AS n FROM ia_resumen_mensual_tipo_movimiento WHERE criterio_version = %s", (DERIVADAS_VERSION,))
        resumen["conteo"]["tipo_movimiento"] = (cur.fetchone() or {}).get("n") or 0

        cur.execute("SELECT COUNT(*) AS n FROM ia_resumen_mensual_produccion_maquina WHERE criterio_version = %s", (DERIVADAS_VERSION,))
        resumen["conteo"]["produccion_maquina"] = (cur.fetchone() or {}).get("n") or 0

        cur.execute("SELECT COUNT(*) AS n FROM ia_resumen_mensual_ingresos_proveedor WHERE criterio_version = %s", (DERIVADAS_VERSION,))
        resumen["conteo"]["ingresos_proveedor"] = (cur.fetchone() or {}).get("n") or 0

        if periodo_ref:
            cur.execute("""
                SELECT TiposDeMovimiento, registros, kilos_total
                FROM ia_resumen_mensual_tipo_movimiento
                WHERE periodo = %s AND criterio_version = %s
                ORDER BY kilos_total DESC LIMIT 5
            """, (periodo_ref, DERIVADAS_VERSION))
            resumen["muestra_tipo_movimiento"] = cur.fetchall() or []

            cur.execute("""
                SELECT Maquina, registros, kilos_total
                FROM ia_resumen_mensual_produccion_maquina
                WHERE periodo = %s AND criterio_version = %s
                ORDER BY kilos_total DESC LIMIT 5
            """, (periodo_ref, DERIVADAS_VERSION))
            resumen["muestra_produccion_maquina"] = cur.fetchall() or []

            cur.execute("""
                SELECT Proveedor, registros, kilos_total
                FROM ia_resumen_mensual_ingresos_proveedor
                WHERE periodo = %s AND criterio_version = %s
                ORDER BY kilos_total DESC LIMIT 5
            """, (periodo_ref, DERIVADAS_VERSION))
            resumen["muestra_ingresos_proveedor"] = cur.fetchall() or []

        cur.close()
        conn.close()
    except Exception as e:
        resumen["error"] = str(e)
    return resumen


def upsert_verdad_periodo(periodo_inicio, periodo_fin, periodo_label, metrica, valor, unidad=None,
                           confianza_dato=0.8, fuente_query=None, origen="explorador_fase2"):
    """Guarda/actualiza una verdad confirmada de un periodo cerrado en la base
    editable (crowdbot_lab), con confianza_temporal segun antiguedad."""
    if not periodo_inicio or not metrica:
        return {"ok": False, "skipped": "datos_incompletos"}
    valor_num = confianza_float(valor)
    if valor_num is None:
        return {"ok": False, "skipped": "valor_no_numerico"}
    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_lab"}
    try:
        cur = conn.cursor()
        ensure_ia_verdades_periodo(cur)
        confianza_temporal = calcular_confianza_temporal(periodo_inicio)
        cur.execute("""
            INSERT INTO ia_verdades_periodo (
                periodo_tipo, periodo_inicio, periodo_fin, periodo_label, metrica,
                valor, unidad, confianza_dato, confianza_temporal, fuente_query, origen
            ) VALUES ('mes', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                valor = VALUES(valor),
                unidad = VALUES(unidad),
                confianza_dato = VALUES(confianza_dato),
                confianza_temporal = VALUES(confianza_temporal),
                fuente_query = VALUES(fuente_query),
                periodo_label = VALUES(periodo_label),
                vigente = 1
        """, (
            periodo_inicio, periodo_fin, periodo_label, metrica,
            valor_num, unidad, confianza_dato, confianza_temporal, fuente_query, origen,
        ))
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True, "confianza_temporal": confianza_temporal}
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def periodos_mes_confirmados():
    """Meses (periodo_inicio) que ya tienen el desglose de tipos de movimiento
    confirmado (nivel 1). Antes bastaba cualquier verdad; ahora se exige el
    sentinel 'tipos_movimiento_visto' para considerar el mes barrido."""
    conn = get_ia_connection()
    if not conn:
        return set()
    try:
        cur = conn.cursor()
        ensure_ia_verdades_periodo(cur)
        cur.execute("""
            SELECT DISTINCT periodo_inicio
            FROM ia_verdades_periodo
            WHERE periodo_tipo = 'mes' AND vigente = 1 AND metrica = 'tipos_movimiento_visto'
        """)
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
        return {str(r["periodo_inicio"]) for r in rows}
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return set()


def tipos_movimiento_conocidos(periodo_inicio):
    """Desglose de TiposDeMovimiento ya confirmado para un mes (nivel 1),
    para mostrarselo a la IA cuando toca profundizar (nivel 2)."""
    conn = get_ia_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        ensure_ia_verdades_periodo(cur)
        cur.execute("""
            SELECT metrica, valor
            FROM ia_verdades_periodo
            WHERE periodo_tipo = 'mes' AND periodo_inicio = %s AND vigente = 1
              AND metrica LIKE 'tipo:%%'
            ORDER BY valor DESC
        """, (periodo_inicio,))
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
        return [{"tipo": r["metrica"].split("tipo:", 1)[-1], "valor": r["valor"]} for r in rows]
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []


def progreso_barrido_periodos(contexto_temporal=None):
    """Texto corto de progreso del barrido de meses, para avisar por Telegram
    'voy por tal mes, me faltan tantos'."""
    contexto_temporal = contexto_temporal or construir_contexto_temporal_operativo(persistir=False)
    try:
        fin_mes_anterior = datetime.fromisoformat(contexto_temporal["fin_mes_anterior"])
    except Exception:
        return ""
    confirmados = periodos_mes_confirmados()
    cursor_mes = datetime(fin_mes_anterior.year, fin_mes_anterior.month, 1)
    total = 0
    pendientes = 0
    for _ in range(VERDAD_PERIODO_MAX_RETROCESO):
        total += 1
        if cursor_mes.date().isoformat() not in confirmados:
            pendientes += 1
        if cursor_mes.month == 1:
            cursor_mes = datetime(cursor_mes.year - 1, 12, 1)
        else:
            cursor_mes = datetime(cursor_mes.year, cursor_mes.month - 1, 1)
    barridos = total - pendientes
    if pendientes == 0:
        return f"Nivel 1 completo: {barridos}/{total} meses barridos. Ahora profundizando en el mes mas reciente."
    return f"Progreso barrido: {barridos}/{total} meses con desglose de tipos confirmado, faltan {pendientes}."


def obtener_periodo_objetivo_explorador(contexto_temporal=None):
    """Proximo mes CERRADO a trabajar, priorizando los mas recientes.
    Nivel 1: mientras haya meses sin desglose de tipos de movimiento
    (sentinel 'tipos_movimiento_visto'), barre hacia atras pidiendo ese desglose.
    Nivel 2: cuando todos los meses del rango ya tienen nivel 1, el objetivo pasa
    a ser el mes cerrado mas reciente, en modo 'profundizar' (entrar en el tipo
    de movimiento mas relevante y la columna que la IA ya aprendio que importa).
    No retrocede mas de VERDAD_PERIODO_MAX_RETROCESO meses: los datos muy viejos
    son menos confiables y no vale la pena perseguirlos en automatico."""
    contexto_temporal = contexto_temporal or construir_contexto_temporal_operativo(persistir=False)
    try:
        fin_mes_anterior = datetime.fromisoformat(contexto_temporal["fin_mes_anterior"])
    except Exception:
        return None
    confirmados = periodos_mes_confirmados()

    cursor_mes = datetime(fin_mes_anterior.year, fin_mes_anterior.month, 1)
    mes_mas_reciente = cursor_mes
    for _ in range(VERDAD_PERIODO_MAX_RETROCESO):
        clave = cursor_mes.date().isoformat()
        if clave not in confirmados:
            inicio = cursor_mes
            fin = fin_de_mes(cursor_mes)
            return {
                "periodo_inicio": inicio.date().isoformat(),
                "periodo_fin": fin.date().isoformat(),
                "periodo_label": f"{MESES_ES[inicio.month - 1]} {inicio.year}",
                "modo": "general",
            }
        if cursor_mes.month == 1:
            cursor_mes = datetime(cursor_mes.year - 1, 12, 1)
        else:
            cursor_mes = datetime(cursor_mes.year, cursor_mes.month - 1, 1)

    # Todos los meses del rango ya tienen nivel 1: profundizar en el mas reciente.
    inicio = mes_mas_reciente
    fin = fin_de_mes(inicio)
    return {
        "periodo_inicio": inicio.date().isoformat(),
        "periodo_fin": fin.date().isoformat(),
        "periodo_label": f"{MESES_ES[inicio.month - 1]} {inicio.year}",
        "modo": "profundizar",
        "tipos_conocidos": tipos_movimiento_conocidos(inicio.date().isoformat()),
    }


def periodo_objetivo_prompt_compacto(periodo_objetivo):
    if not periodo_objetivo:
        return "PERIODO OBJETIVO: ya hay verdades confirmadas para los ultimos meses cerrados; elegi libremente dentro de periodos cerrados."
    if periodo_objetivo.get("modo") == "profundizar":
        tipos = periodo_objetivo.get("tipos_conocidos") or []
        tipos_txt = "; ".join(f"{t['tipo']}={t['valor']}" for t in tipos[:8]) or "(sin desglose guardado todavia, volve a correr el GROUP BY TiposDeMovimiento de este mes)"
        return (
            f"PERIODO OBJETIVO DE ESTE CICLO: {periodo_objetivo['periodo_label']} "
            f"({periodo_objetivo['periodo_inicio']} a {periodo_objetivo['periodo_fin']}). MODO: PROFUNDIZAR.\n"
            f"Ya conoces el desglose de tipos de movimiento de este mes: {tipos_txt}.\n"
            "Elegi UNO de esos tipos (el mas relevante por peso, o el que mas te interese entender) y profundiza DENTRO de el: "
            "que columnas (Operador, Maquina, Proveedor, Materiales, Precio, etc.) explican mejor su comportamiento ese mes. "
            "Usa lo que ya sabes en 'Relaciones interesantes conocidas' para elegir la columna, no arranques de cero. "
            "Si tu query agrega una metrica acotada a ese tipo+columna+mes y la confirmas, incluila en conclude con "
            "periodo_metrica (ej. 'profundidad:PRODUCCION:Operador'), periodo_valor y periodo_label."
        )
    return (
        f"PERIODO OBJETIVO DE ESTE CICLO: {periodo_objetivo['periodo_label']} "
        f"({periodo_objetivo['periodo_inicio']} a {periodo_objetivo['periodo_fin']}), el mes cerrado mas reciente sin desglose de tipos de movimiento. MODO: GENERAL.\n"
        "Primero necesitamos el desglose: corre un GROUP BY TiposDeMovimiento acotado a este mes exacto (COUNT(*) y SUM(kilos)) "
        "para ver que tipos hay y su peso. Si tu query agrega una metrica acotada a ese rango exacto (kilos, movimientos, etc.) y la confirmas, "
        "incluila en el JSON de conclude con periodo_metrica (nombre corto, ej. 'tipo:PRODUCCION'), periodo_valor (numero) y periodo_label "
        "para que quede guardada como verdad de ese periodo. Si no aplica, omiti esos campos."
    )


def capturar_desglose_tipos_movimiento(query, resultado_sql, periodo_objetivo):
    """Si la query es un GROUP BY TiposDeMovimiento acotado al periodo objetivo,
    guarda automaticamente cada tipo encontrado como verdad ('tipo:X') y el
    sentinel 'tipos_movimiento_visto' que marca el mes como barrido en nivel 1.
    No depende de que el modelo recuerde llamar a conclude con periodo_metrica."""
    if not periodo_objetivo or periodo_objetivo.get("modo") != "general":
        return None
    if not resultado_sql or not resultado_sql.get("ok"):
        return None
    q_upper = str(query or "").upper()
    if "GROUP BY" not in q_upper or "TIPOSDEMOVIMIENTO" not in q_upper:
        return None
    if not query_coincide_con_periodo_objetivo(query, periodo_objetivo):
        return None
    rows = resultado_sql.get("rows") or []
    if not rows:
        return None
    tipos_guardados = []
    for fila in rows:
        tipo = None
        numero = None
        for k, v in fila.items():
            if isinstance(v, str) and tipo is None:
                tipo = v
            elif isinstance(v, (int, float, Decimal)) and numero is None:
                numero = v
        if not tipo or numero is None:
            continue
        r = upsert_verdad_periodo(
            periodo_objetivo.get("periodo_inicio"), periodo_objetivo.get("periodo_fin"),
            periodo_objetivo.get("periodo_label"), f"tipo:{tipo}", numero,
            confianza_dato=0.85, fuente_query=query, origen="explorador_fase2_desglose",
        )
        if r.get("ok"):
            tipos_guardados.append(tipo)
    if tipos_guardados:
        upsert_verdad_periodo(
            periodo_objetivo.get("periodo_inicio"), periodo_objetivo.get("periodo_fin"),
            periodo_objetivo.get("periodo_label"), "tipos_movimiento_visto", len(tipos_guardados),
            confianza_dato=0.85, fuente_query=query, origen="explorador_fase2_desglose",
        )
    return tipos_guardados or None


def query_coincide_con_periodo_objetivo(query, periodo_objetivo):
    """True si la query esta explicitamente acotada al mismo mes/anio del periodo objetivo."""
    if not periodo_objetivo:
        return False
    try:
        inicio = datetime.fromisoformat(periodo_objetivo["periodo_inicio"])
    except Exception:
        return False
    q = str(query or "").upper()
    mes_match = re.search(r"MONTH\(\s*FECHA(HORA)?\s*\)\s*=\s*(\d{1,2})", q)
    anio_match = re.search(r"YEAR\(\s*FECHA(HORA)?\s*\)\s*=\s*(\d{4})", q)
    if mes_match and anio_match:
        return int(mes_match.group(2)) == inicio.month and int(anio_match.group(2)) == inicio.year
    return False


def extraer_metricas_numericas_de_fila(resultado_sql):
    """Si el resultado tiene una sola fila, devuelve las columnas numericas (nombre, valor)."""
    if not resultado_sql or not resultado_sql.get("ok"):
        return []
    rows = resultado_sql.get("rows") or []
    if len(rows) != 1:
        return []
    fila = rows[0]
    items = fila.items() if isinstance(fila, dict) else zip(resultado_sql.get("columnas") or [], fila)
    metricas = []
    for col, val in items:
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            metricas.append((col, float(val)))
    return metricas


def intentar_capturar_verdad_periodo_de_resultado(query, resultado_sql, periodo_objetivo, confianza_dato=0.7):
    """Si el explorador ya consiguio un resultado limpio y acotado al periodo objetivo pero
    no llego a un 'conclude' explicito (repitio query, se quedo sin pasos, etc.), guardamos
    igual la verdad de periodo en vez de perder el dato."""
    if not query_coincide_con_periodo_objetivo(query, periodo_objetivo):
        return []
    metricas = extraer_metricas_numericas_de_fila(resultado_sql)
    confirmadas = []
    for metrica, valor in metricas:
        resultado = upsert_verdad_periodo(
            periodo_objetivo.get("periodo_inicio"),
            periodo_objetivo.get("periodo_fin"),
            periodo_objetivo.get("periodo_label"),
            metrica, valor,
            confianza_dato=confianza_dato,
            fuente_query=query,
            origen="explorador_fase2_auto",
        )
        if resultado.get("ok"):
            confirmadas.append((metrica, valor))
    return confirmadas


def compactar_evidencia_estructural(valor, profundidad=0, clave=""):
    clave_l = str(clave or "").lower()
    if "evidencia_json" in clave_l or "salida_json" in clave_l:
        return None
    if "co_presencia" in clave_l or "copresencia" in clave_l:
        return "[omitido: co-presencia resumida fuera del prompt]"
    if profundidad > 3:
        return shorten_text(valor, 120)
    if isinstance(valor, list):
        limite = 5
        if "ejemplo" in clave_l:
            limite = 10 if profundidad == 0 else 3
        elif "top" in clave_l:
            limite = 5
        elif "histor" in clave_l:
            limite = 1
        return [
            x for x in
            (compactar_evidencia_estructural(x, profundidad + 1, clave) for x in valor[:limite])
            if x is not None
        ]
    if isinstance(valor, dict):
        items = list(valor.items())
        compacto = {}
        for k, v in items:
            k_l = str(k).lower()
            if k_l in ("evidencia_json", "salida_json") or "co_presencia" in k_l or "copresencia" in k_l:
                continue
            if "ejemplo" in k_l and isinstance(v, list):
                v = v[:10]
            elif ("top" in k_l or "frecuente" in k_l) and isinstance(v, list):
                v = v[:5]
            elif "histor" in k_l and isinstance(v, list):
                v = v[:1]
            cv = compactar_evidencia_estructural(v, profundidad + 1, k)
            if cv is not None:
                compacto[k] = cv
        return compacto
    if isinstance(valor, str):
        return shorten_text(valor, 140)
    return valor


def mapa_estructural_prompt_compacto(pendiente):
    evidencia = compactar_evidencia_estructural(pendiente.get("evidencia") or {})
    contexto_temporal = construir_contexto_temporal_operativo(persistir=True)
    ev_txt = json.dumps(evidencia, ensure_ascii=False, default=str, separators=(",", ":"))[:520]
    prompt = f"""Rol: IA local Ecotecnica. Lee UNA ficha estructural.
Tarea: {shorten_text(pendiente.get('tarea'), 150)}
Tema: {shorten_text(pendiente.get('tema'), 120)}
Tiempo: {shorten_text(contexto_temporal_prompt_compacto(contexto_temporal), 120)}

FICHA compacta, no inventes fuera de esto:
{ev_txt}

Salida: SOLO JSON una linea: {{"action":"conclude","conclusion":"max 2 frases","confianza":0.55}}.
Permitido conclude/hypothesis. Prohibido query, SQL, markdown, listas largas."""
    return limitar_prompt(prompt, 950)

def asegurar_memoria_explorador(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_valores_observados (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tabla VARCHAR(128) NOT NULL,
            columna VARCHAR(128) NOT NULL,
            valor VARCHAR(255) NOT NULL,
            cantidad BIGINT NULL,
            ultimo_visto DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            fuente_query TEXT NULL,
            UNIQUE KEY uq_valor_observado (tabla, columna, valor)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_consultas_sin_datos (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tabla VARCHAR(128) NOT NULL,
            query_sql TEXT NOT NULL,
            filtros_json JSON NULL,
            motivo TEXT NULL,
            creada_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def extraer_filtros_exactos_query(query):
    q = str(query or "")
    filtros = []
    patron = re.compile(r"(?:`(?P<col_bt>[^`]+)`|\b(?P<col>[A-Za-z_][A-Za-z0-9_]*)\b)\s*=\s*'(?P<val>[^']{1,255})'", re.IGNORECASE)
    for m in patron.finditer(q):
        col = (m.group("col_bt") or m.group("col") or "").strip()
        val = (m.group("val") or "").strip()
        if col and val:
            filtros.append({"columna": col, "valor": val})
    return filtros


def valores_observados_existen(filtros):
    if not filtros:
        return {"ok": True, "faltantes": []}
    conn = get_ia_connection()
    if not conn:
        return {"ok": True, "faltantes": [], "warning": "sin_conexion_memoria"}
    faltantes = []
    try:
        cur = conn.cursor()
        asegurar_memoria_explorador(cur)
        for f in filtros:
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM ia_valores_observados
                WHERE tabla = 'TiposDeMovimiento'
                  AND columna = %s
                  AND valor = %s
            """, (f.get("columna"), f.get("valor")))
            row = cur.fetchone() or {}
            if int(row.get("total") or 0) <= 0:
                faltantes.append(f)
        cur.close()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return {"ok": True, "faltantes": [], "warning": "error_memoria"}
    return {"ok": not faltantes, "faltantes": faltantes}


def inferir_cantidad_fila(row, columnas):
    if not isinstance(row, dict):
        return None
    candidatos = ["cantidad", "count", "total", "movimientos", "registros", "cnt"]
    for c in columnas or []:
        lc = str(c or "").lower()
        if any(x in lc for x in candidatos):
            try:
                return int(float(row.get(c) or 0))
            except Exception:
                return None
    return None


def guardar_valores_observados_desde_resultado(query, resultado_sql):
    if not resultado_sql.get("ok"):
        return {"ok": False, "skipped": "query_error"}
    rows = resultado_sql.get("rows") or []
    cols = resultado_sql.get("columnas") or []
    if not rows or not cols:
        return {"ok": False, "skipped": "sin_filas"}
    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_memoria"}
    guardados = 0
    try:
        cur = conn.cursor()
        asegurar_memoria_explorador(cur)
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            cantidad = inferir_cantidad_fila(row, cols)
            for col in cols:
                val = row.get(col)
                if val is None or isinstance(val, (bytes, bytearray)):
                    continue
                if isinstance(val, (int, float, Decimal)) and str(col).lower() in ("cantidad", "count", "total", "movimientos", "registros"):
                    continue
                valor = str(val).strip()
                if not valor or len(valor) > 255:
                    continue
                cur.execute("""
                    INSERT INTO ia_valores_observados (tabla, columna, valor, cantidad, ultimo_visto, fuente_query)
                    VALUES ('TiposDeMovimiento', %s, %s, %s, NOW(), %s)
                    ON DUPLICATE KEY UPDATE
                        cantidad = GREATEST(COALESCE(cantidad, 0), COALESCE(VALUES(cantidad), 0)),
                        ultimo_visto = NOW(),
                        fuente_query = VALUES(fuente_query)
                """, (str(col), valor, cantidad, shorten_text(query, 1000)))
                guardados += 1
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True, "guardados": guardados}
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def registrar_consulta_sin_datos(query, filtros, motivo="sin_resultados"):
    conn = get_ia_connection()
    if not conn:
        return {"ok": False, "error": "sin_conexion_memoria"}
    try:
        cur = conn.cursor()
        asegurar_memoria_explorador(cur)
        cur.execute("""
            INSERT INTO ia_consultas_sin_datos (tabla, query_sql, filtros_json, motivo)
            VALUES ('TiposDeMovimiento', %s, %s, %s)
        """, (shorten_text(query, 2000), json.dumps(filtros or [], ensure_ascii=False, default=str), motivo))
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True}
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def valores_observados_prompt_compacto(limite=8):
    conn = get_ia_connection()
    if not conn:
        return "- Sin memoria de valores observados."
    try:
        cur = conn.cursor()
        asegurar_memoria_explorador(cur)
        cur.execute("""
            SELECT columna, valor, cantidad
            FROM ia_valores_observados
            WHERE tabla = 'TiposDeMovimiento'
            ORDER BY ultimo_visto DESC
            LIMIT %s
        """, (limite,))
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
        if not rows:
            return "- Sin memoria de valores observados. Primero consulta valores reales con GROUP BY o nulos/no nulos."
        return "\n".join([
            f"- {r.get('columna')}: {shorten_text(r.get('valor'), 60)} ({r.get('cantidad') or '?'})"
            for r in rows
        ])
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return "- Memoria de valores no disponible."

def ejecutar_query_segura(query, db="operativa"):
    """Ejecuta una query SQL de solo lectura. Devuelve {ok, rows, error, columnas}."""
    q = query.strip().rstrip(";").strip()

    # Solo permitir SELECT
    if not q.upper().startswith("SELECT") and not q.upper().startswith("SHOW") and not q.upper().startswith("DESCRIBE"):
        return {"ok": False, "error": "Solo se permiten SELECT, SHOW y DESCRIBE.", "rows": []}

    # Bloquear keywords peligrosos
    bloqueados = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE"]
    q_upper = q.upper()
    for b in bloqueados:
        if b in q_upper:
            return {"ok": False, "error": f"Query bloqueada: contiene {b}.", "rows": []}
    if "FECHAMOVIMIENTO" in q_upper:
        return {"ok": False, "error": "La columna FechaMovimiento no existe. Usar FechaHora o Fecha.", "rows": []}
    if q_upper.startswith("SELECT *"):
        return {"ok": False, "error": "Evitar SELECT *. Elegir columnas explicitas del esquema seguro.", "rows": []}

    conn = get_db_connection() if db == "operativa" else get_ia_connection()
    if not conn:
        return {"ok": False, "error": "Sin conexión a base de datos.", "rows": []}
    try:
        cur = conn.cursor()
        cur.execute(q)
        rows = cur.fetchmany(5)  # muestra acotada para prompts compactos
        columnas = [d[0] for d in cur.description] if cur.description else []
        cur.close()
        conn.close()
        return {"ok": True, "rows": rows, "columnas": columnas, "total": len(rows)}
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e), "rows": []}


def formatear_resultado_query(resultado):
    """Formatea el resultado de una query para incluir en prompt."""
    if not resultado.get("ok"):
        return f"ERROR: {resultado.get('error')}"
    rows = resultado.get("rows") or []
    cols = resultado.get("columnas") or []
    if not rows:
        return "(sin resultados)"
    lines = ["  " + " | ".join(str(c) for c in cols)]
    lines.append("  " + "-" * 60)
    for row in rows[:5]:
        if isinstance(row, dict):
            lines.append("  " + " | ".join(str(row.get(c, "")) for c in cols))
        else:
            lines.append("  " + " | ".join(str(v) for v in row))
    if len(rows) > 5:
        lines.append(f"  ... ({len(rows)} filas, mostrando 5)")
    return "\n".join(lines)


def relaciones_prompt_compacto(relaciones, limite=5):
    items = []
    for r in (relaciones or [])[:limite]:
        items.append(
            f"- {r.get('columna_a')}->{r.get('columna_b')} score={r.get('interes_score')} conf={r.get('confianza')}: {shorten_text(r.get('descripcion'), 120)}"
        )
    return "\n".join(items) or "- Sin relaciones interesantes aun."


def historial_prompt_compacto(historial_queries, limite=2):
    items = []
    for h in (historial_queries or [])[-limite:]:
        items.append(f"- {shorten_text(h.get('query'), 110)} -> {shorten_text(h.get('resumen'), 70)}")
    return "\n".join(items) or "- Sin pasos previos."


def limitar_prompt(prompt, max_chars=FASE2_PROMPT_MAX_CHARS):
    if len(prompt) <= max_chars:
        return prompt
    aviso = "\n\n[Prompt recortado: se conserva inicio, reglas y JSON esperado.]\n\n"
    head_len = max(400, int((max_chars - len(aviso)) * 0.55))
    tail_len = max_chars - len(aviso) - head_len
    return prompt[:head_len].rstrip() + aviso + prompt[-tail_len:].lstrip()


def prompt_explorador_inicial(conocimientos_previos, historial_queries, relaciones_interesantes=None,
                               contexto_temporal=None, periodo_objetivo=None):
    """Prompt inicial fase 2 compacto."""
    rel_txt = relaciones_prompt_compacto(relaciones_interesantes, 5)
    hist_txt = historial_prompt_compacto(historial_queries, 2)
    valores_txt = valores_observados_prompt_compacto(8)
    contexto_temporal = contexto_temporal or construir_contexto_temporal_operativo(persistir=False)
    if periodo_objetivo is None:
        periodo_objetivo = obtener_periodo_objetivo_explorador(contexto_temporal)
    prompt = f"""Rol: IA local de Ecotecnica. Objetivo del ciclo: encontrar UNA senal operacional concreta o una hipotesis verificable, no rankings genericos.

{contexto_temporal_prompt_compacto(contexto_temporal)}

{periodo_objetivo_prompt_compacto(periodo_objetivo)}

Esquema minimo:
{FASE2_SCHEMA_MIN_TXT}

Relaciones interesantes conocidas:
{rel_txt}

Valores reales observados recientemente:
{valores_txt}

Metodo obligatorio: primero ver columnas, ejemplos reales por columna, nulos/no nulos, co-presencia y presencia por TiposDeMovimiento. Recien despues usar filtros exactos.
Regla dura: no uses WHERE columna = 'valor' si ese valor no fue observado antes en esa misma columna. Si falta evidencia, consulta valores reales con GROUP BY columna LIMIT 5.
Regla dura: toda agregacion (GROUP BY, SUM, AVG, COUNT) debe acotarse a un periodo (mes/anio con MONTH()/YEAR() o FechaHora BETWEEN), nunca a toda la historia sin recorte. Preferi fijar verdades de periodos cerrados (mes anterior, meses ya terminados) e ir hacia atras mes a mes para enriquecer lo que aun no se confirmo. El mes actual y el dia de hoy son parciales: si los usas, aclaralo como dato parcial, no como verdad cerrada.

Historial breve:
{hist_txt}

Elegi UNA query SQL concreta. Limite obligatorio: LIMIT 5 si no es agregacion. Buscar faltantes, anomalias, contradicciones, cambios, precio/material/proveedor, descuentos o relacion kilos/kilosNetos.

Acciones permitidas estrictas. Responde SOLO JSON compacto, sin markdown, sin texto extra, una sola linea:
{ACCIONES_JSON_EJEMPLO}
Si no tenes query valida, usa action="conclude" o action="hypothesis"."""
    return limitar_prompt(prompt, FASE2_PROMPT_MAX_CHARS)


def prompt_explorador_siguiente(query_anterior, resultado_txt, historial_queries, conocimientos_previos, pasos_restantes,
                                 relaciones_interesantes=None, contexto_temporal=None, periodo_objetivo=None):
    """Prompt fase 2 compacto para continuar o concluir."""
    rel_txt = relaciones_prompt_compacto(relaciones_interesantes, 5)
    hist_txt = historial_prompt_compacto(historial_queries, 2)
    valores_txt = valores_observados_prompt_compacto(8)
    resultado_corto = shorten_multiline(resultado_txt, 750)
    contexto_temporal = contexto_temporal or construir_contexto_temporal_operativo(persistir=False)
    if periodo_objetivo is None:
        periodo_objetivo = obtener_periodo_objetivo_explorador(contexto_temporal)
    cierre = "Conclui si hay senal o hipotesis. Otra query solo si aporta evidencia nueva." if pasos_restantes <= 1 else "Otra query solo si mejora claramente la evidencia."
    prompt = f"""Rol: IA local de Ecotecnica. Objetivo: aprender una senal operacional concreta o hipotesis verificable.

{contexto_temporal_prompt_compacto(contexto_temporal)}

{periodo_objetivo_prompt_compacto(periodo_objetivo)}

Esquema minimo:
{FASE2_SCHEMA_MIN_TXT}

Relaciones interesantes:
{rel_txt}

Valores reales observados recientemente:
{valores_txt}

Ultima query:
{shorten_text(query_anterior, 220)}

Resultado resumido:
{resultado_corto}

Historial max 2:
{hist_txt}

Metodo obligatorio: columnas -> ejemplos reales -> nulos/no nulos -> co-presencia -> presencia por TiposDeMovimiento -> filtros especificos.
Regla dura: no uses WHERE columna = 'valor' si ese valor no fue observado antes en esa misma columna. Si una consulta dio 0 filas, no concluyas: corrige el eje consultando valores reales.
Regla dura: toda agregacion (GROUP BY, SUM, AVG, COUNT) debe acotarse a un periodo (mes/anio con MONTH()/YEAR() o FechaHora BETWEEN), nunca a toda la historia sin recorte. Preferi fijar verdades de periodos cerrados (mes anterior, meses ya terminados) e ir hacia atras mes a mes para enriquecer lo que aun no se confirmo. El mes actual y el dia de hoy son parciales: si los usas, aclaralo como dato parcial, no como verdad cerrada.

Pasos restantes: {pasos_restantes}. Regla: {cierre}
Evitar rankings genericos. Valen: anomalia, faltante, cambio temporal, contradiccion, precio/material/proveedor, descuentos, nulos criticos, hipotesis operacional.

Acciones permitidas estrictas. Responde SOLO JSON compacto, sin markdown, sin texto extra, una sola linea.
{ACCIONES_JSON_EJEMPLO}
Regla: si action="query", query es obligatorio y debe empezar con SELECT, SHOW o DESCRIBE. Si no tenes query, usa conclude o hypothesis."""
    return limitar_prompt(prompt)


def normalizar_decision_explorador(decision):
    if not isinstance(decision, dict):
        return {"action": "invalid"}

    aliases_campos = {
        "query_sql": "query",
        "sql": "query",
        "consulta_sql": "query",
        "objetivo": "que_espero_encontrar",
        "proposito": "que_espero_encontrar",
        "conclusion_final": "conclusion",
        "hipotesis": "hipotesis_nueva",
    }
    for origen, destino in aliases_campos.items():
        if origen in decision and not decision.get(destino):
            decision[destino] = decision.get(origen)

    action = str(decision.get("action") or "query").strip().lower()
    aliases = {"concluir": "conclude", "conclusion": "conclude", "conclude": "conclude", "hipotesis": "hypothesis", "hypothesis": "hypothesis", "query": "query"}
    action = aliases.get(action, "query")
    query = str(decision.get("query") or "").strip()
    tiene_conclusion = bool(primer_texto(decision.get("conclusion"), decision.get("conclusion_principal"), decision.get("conclusion_autonoma"), decision.get("resumen")))
    tiene_hipotesis = bool(primer_texto(decision.get("hipotesis_nueva"), decision.get("hipotesis_refinada"), decision.get("hipotesis")))
    if action == "query" and not query:
        if tiene_hipotesis:
            action = "hypothesis"
        elif tiene_conclusion:
            action = "conclude"
    decision["action"] = action
    return decision


def query_explorador_valida(query):
    q = str(query or "").strip()
    if not q:
        return False
    q_upper = q.upper()
    return q_upper.startswith("SELECT") or q_upper.startswith("SHOW") or q_upper.startswith("DESCRIBE")


def query_sin_marco_temporal(query):
    """True si es una agregacion (GROUP BY / SUM / AVG / COUNT) sobre toda la
    historia, sin ningun recorte temporal. No exige 'ultimos N dias': alcanza con
    acotar por un periodo cerrado explicito (mes, anio, rango de fechas, MONTH()/YEAR())
    para construir una verdad de ese periodo. Solo bloquea cuando no hay marco alguno."""
    q_upper = str(query or "").upper()
    if "GROUP BY" not in q_upper and not any(f in q_upper for f in ("SUM(", "AVG(", "COUNT(")):
        return False
    tiene_filtro_relativo = bool(re.search(r"\b(FECHAHORA|FECHA)\b\s*(>=|>|<=|<|=|BETWEEN)", q_upper))
    tiene_filtro_funcion = bool(re.search(r"\b(YEAR|MONTH|DATE)\s*\(\s*FECHA(HORA)?\s*\)", q_upper))
    return not (tiene_filtro_relativo or tiene_filtro_funcion)


def query_usa_tabla_valida(query):
    """True si la query referencia unicamente TiposDeMovimiento (o no tiene FROM, ej. SHOW/DESCRIBE).
    El campo TiposDeMovimiento es tanto el nombre de la tabla como una columna dentro de ella
    (con valores como despachos/ventas), por eso no basta con bloquear nombres "raros": se valida
    explicitamente contra la unica tabla real."""
    q_upper = str(query or "").upper()
    if q_upper.startswith("SHOW") or q_upper.startswith("DESCRIBE"):
        return True
    tablas = set(re.findall(r"\bFROM\s+([A-Z_][A-Z0-9_]*)", q_upper))
    tablas |= set(re.findall(r"\bJOIN\s+([A-Z_][A-Z0-9_]*)", q_upper))
    if not tablas:
        return True
    return tablas == {"TIPOSDEMOVIMIENTO"}


def corregir_like_sin_wildcard(query):
    """LIKE 'valor' sin % se comporta igual que =, y el modelo local suele olvidar
    el wildcard al escribir LIKE. Si no tiene ningun % ni _, se asume que quiso
    decir "empieza con" y se le agrega el % al final (correccion sintactica,
    no depende de ninguna lista de categorias de negocio)."""
    def _reemplazar(m):
        prefijo, valor = m.group(1), m.group(2)
        if "%" in valor or "_" in valor:
            return m.group(0)
        return f"{prefijo}'{valor}%'"
    return re.sub(r"(LIKE\s+)'([^'%_]+)'", _reemplazar, query, flags=re.IGNORECASE)


def preparar_query_explorador(query):
    q = str(query or "").strip().strip('`').strip()
    q = q.rstrip(";").strip()
    if not query_explorador_valida(q):
        return None
    q_upper = q.upper()
    bloqueados = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE"]
    if any(b in q_upper for b in bloqueados):
        return None
    q = corregir_like_sin_wildcard(q)
    if q_upper.startswith("SELECT") and " LIMIT " not in f" {q_upper} ":
        q = q + " LIMIT 5"
    return q


def normalizar_query_ciclo(query):
    q = str(query or "").strip().rstrip(";").strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q


def firma_resultado_sql(resultado_sql):
    if not resultado_sql or not resultado_sql.get("ok"):
        return ""
    data = {
        "columnas": resultado_sql.get("columnas") or [],
        "rows": resultado_sql.get("rows") or [],
    }
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)[:4000]
    except Exception:
        return str(data)[:4000]

def extraer_query_sql_desde_texto(text):
    raw = str(text or "")
    if not raw:
        return None

    # Preferir valores de campos tipo query_sql/sql/consulta_sql aunque el JSON este truncado.
    campo = re.search(r'"(?:query_sql|sql|consulta_sql|query)"\s*:\s*"(?P<q>(?:\\.|[^"\\])*)', raw, flags=re.IGNORECASE | re.DOTALL)
    if campo:
        q = campo.group("q")
        q = q.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
        q = q.split('"')[0].strip()
        q = preparar_query_explorador(q)
        if q:
            return q

    m = re.search(r'\b(SELECT|SHOW|DESCRIBE)\b[\s\S]*?(?:;|$)', raw, flags=re.IGNORECASE)
    if not m:
        return None
    q = m.group(0).strip()
    if ";" in q:
        q = q[:q.find(";") + 1]
    else:
        # Si no hay punto y coma, cortar antes de una comilla probable de JSON/texto.
        q = re.split(r'["\n\r]', q, maxsplit=1)[0].strip()
    return preparar_query_explorador(q)

def construir_ficha_compacta_columna(cur_op, cur_lab, columna):
    """Ficha compacta y con foco reciente de una columna de TiposDeMovimiento
    para pendientes estructurales. Máx 10 ejemplos (8 recientes + hasta 2
    históricos solo si aportan valores distintos). Sin co-presencia ni
    evidencia gigante. Recency: 7d alta, 30d media, histórico como contexto."""
    if not re.match(r"^[A-Za-z0-9_]+$", str(columna or "")):
        return None
    col = f"`{columna}`"
    no_vacio = f"{col} IS NOT NULL AND TRIM(CAST({col} AS CHAR)) <> ''"
    ficha = {"columna": columna}

    cur_op.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN {no_vacio} THEN 1 ELSE 0 END) AS no_nulos,
               SUM(CASE WHEN {no_vacio} AND FechaHora >= NOW() - INTERVAL 7 DAY THEN 1 ELSE 0 END) AS nn7,
               SUM(CASE WHEN {no_vacio} AND FechaHora >= NOW() - INTERVAL 30 DAY THEN 1 ELSE 0 END) AS nn30
        FROM TiposDeMovimiento
    """)
    tot = cur_op.fetchone() or {}
    ficha["registros_total"] = int(tot.get("total") or 0)
    ficha["no_nulos_total"] = int(tot.get("no_nulos") or 0)
    ficha["no_nulos_7d"] = int(tot.get("nn7") or 0)
    ficha["no_nulos_30d"] = int(tot.get("nn30") or 0)

    # Distribución liviana por tipo de movimiento (solo registros con valor)
    cur_op.execute(f"""
        SELECT TiposDeMovimiento AS tipo, COUNT(*) AS n
        FROM TiposDeMovimiento
        WHERE {no_vacio}
        GROUP BY TiposDeMovimiento
        ORDER BY n DESC LIMIT 5
    """)
    ficha["top_tipos_movimiento"] = [
        {"tipo": shorten_text(r["tipo"], 30), "n": int(r["n"] or 0)}
        for r in (cur_op.fetchall() or [])
    ]

    # Top valores frecuentes
    cur_op.execute(f"""
        SELECT CAST({col} AS CHAR) AS valor, COUNT(*) AS n
        FROM TiposDeMovimiento
        WHERE {no_vacio}
        GROUP BY valor
        ORDER BY n DESC LIMIT 5
    """)
    ficha["top_valores"] = [
        {"valor": shorten_text(r["valor"], 40), "n": int(r["n"] or 0)}
        for r in (cur_op.fetchall() or [])
    ]

    # Últimos valores no nulos recientes
    cur_op.execute(f"""
        SELECT DATE_FORMAT(FechaHora, '%%Y-%%m-%%d') AS fecha,
               TiposDeMovimiento AS tipo,
               CAST({col} AS CHAR) AS valor
        FROM TiposDeMovimiento
        WHERE {no_vacio}
        ORDER BY FechaHora DESC
        LIMIT 8
    """)
    recientes = [
        {"fecha": r["fecha"], "tipo": shorten_text(r["tipo"], 30), "valor": shorten_text(r["valor"], 40)}
        for r in (cur_op.fetchall() or [])
    ]
    ficha["ejemplos_recientes"] = recientes

    # Ejemplos antiguos solo si muestran valores distintos a los recientes
    valores_recientes = {e["valor"] for e in recientes}
    cur_op.execute(f"""
        SELECT DATE_FORMAT(FechaHora, '%%Y-%%m-%%d') AS fecha,
               TiposDeMovimiento AS tipo,
               CAST({col} AS CHAR) AS valor
        FROM TiposDeMovimiento
        WHERE {no_vacio} AND FechaHora < NOW() - INTERVAL 30 DAY
        ORDER BY FechaHora DESC
        LIMIT 60
    """)
    distintos = []
    for r in (cur_op.fetchall() or []):
        v = shorten_text(r["valor"], 40)
        if v in valores_recientes or any(d["valor"] == v for d in distintos):
            continue
        distintos.append({"fecha": r["fecha"], "tipo": shorten_text(r["tipo"], 30), "valor": v})
        if len(distintos) >= 2:
            break
    ficha["ejemplos_distintos_historicos"] = distintos

    # Lectura previa del diccionario, si existe
    ficha["lectura_backend_previa"] = None
    if cur_lab is not None:
        try:
            cur_lab.execute("""
                SELECT significado_inferido FROM ia_diccionario_columnas
                WHERE columna = %s ORDER BY actualizado_en DESC LIMIT 1
            """, (columna,))
            fila_dic = cur_lab.fetchone()
            if fila_dic and fila_dic.get("significado_inferido"):
                ficha["lectura_backend_previa"] = shorten_text(fila_dic["significado_inferido"], 160)
        except Exception:
            pass

    # Señales de recencia
    nn, nn30 = ficha["no_nulos_total"], ficha["no_nulos_30d"]
    ficha["uso_reciente"] = nn30 > 0
    ficha["solo_historico"] = nn > 0 and nn30 == 0
    # Casi todo lo cargado es de los últimos 30 días: columna nueva o que
    # empezó a cargarse recientemente -> señal interesante
    ficha["aparicion_reciente"] = nn > 0 and (nn - nn30) <= max(3, int(nn * 0.05))
    return ficha


def prompt_pendiente_columna_compacto(columna, ficha):
    """Prompt corto (<900 chars) para interpretar una columna desde su ficha compacta."""
    f = dict(ficha or {})
    lectura_previa = f.pop("lectura_backend_previa", None)
    # Recortar ejemplos y campos largos para que el prompt no crezca
    f.pop("registros_total", None)
    f["ejemplos_recientes"] = (f.get("ejemplos_recientes") or [])[:5]
    f["ejemplos_distintos_historicos"] = (f.get("ejemplos_distintos_historicos") or [])[:3]
    ficha_txt = json.dumps(f, ensure_ascii=False, separators=(",", ":"), default=str)[:430]
    prompt = (
        f"Interpreta la columna {columna} de TiposDeMovimiento (planta reciclado). "
        "Prioriza uso RECIENTE (7d/30d). Si solo hay uso viejo, decilo y baja confianza. "
        "Si empezo a cargarse hace poco, es senal interesante.\n"
        f"FICHA:{ficha_txt}\n"
        + (f"Lectura previa:{shorten_text(lectura_previa, 110)}\n" if lectura_previa else "")
        + 'SOLO JSON: {"action":"conclude","conclusion":"max 2 frases","confianza":0.55-0.75}. Sin SQL ni markdown.'
    )
    return limitar_prompt(prompt, 900)


def _columna_de_pendiente(pendiente):
    """Extrae el nombre de columna de un pendiente estructural, si lo es."""
    ev = pendiente.get("evidencia")
    if isinstance(ev, dict) and ev.get("columna"):
        return str(ev["columna"])
    m = re.search(r"columna\s+([A-Za-z0-9_]+)",
                  f"{pendiente.get('tema') or ''} {pendiente.get('tarea') or ''}", re.I)
    return m.group(1) if m else None


def _texto_similar(a, b, umbral=0.7):
    """True si dos textos comparten la mayoría de sus palabras (sin novedad)."""
    def _toks(t):
        return set(re.findall(r"[a-záéíóúñü0-9]{3,}", str(t or "").lower()))
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= umbral


def ejecutar_pendiente_estructural(cerebro):
    pendiente = elegir_pendiente_estructural()
    if not pendiente:
        return None

    firma = pendiente.get("firma")
    tema = pendiente.get("tema")
    add_reasoning_step(
        "pendiente_estructural",
        f"Trabajo sobre pendiente estructural: {tema}",
        shorten_text(pendiente.get("tarea"), 220)
    )

    # Pendiente de columna: ficha compacta reciente en vez de cartografía completa
    columna = _columna_de_pendiente(pendiente)
    ficha = None
    if columna:
        conn_op_f = None
        conn_lab_f = None
        try:
            conn_op_f = get_db_connection()
            conn_lab_f = get_ia_connection()
            if conn_op_f:
                cur_op_f = conn_op_f.cursor()
                cur_lab_f = conn_lab_f.cursor() if conn_lab_f else None
                ficha = construir_ficha_compacta_columna(cur_op_f, cur_lab_f, columna)
                cur_op_f.close()
                if cur_lab_f is not None:
                    cur_lab_f.close()
        except Exception as e_ficha:
            add_reasoning_step("pendiente_ficha_error", f"No pude armar ficha compacta de {columna}: {e_ficha}", None)
            ficha = None
        finally:
            for _c in (conn_op_f, conn_lab_f):
                try:
                    if _c:
                        _c.close()
                except Exception:
                    pass

    timeout_pendiente = min(PENDIENTE_OLLAMA_TIMEOUT_SECONDS, PENDIENTE_OLLAMA_TIMEOUT_MAX_SECONDS)

    if ficha:
        prompt = prompt_pendiente_columna_compacto(columna, ficha)
        ficha_chars = len(json.dumps(ficha, ensure_ascii=False, default=str))
    else:
        prompt = mapa_estructural_prompt_compacto(pendiente)
        ficha_chars = len(json.dumps(compactar_evidencia_estructural(pendiente.get("evidencia") or {}), ensure_ascii=False, default=str))
    add_reasoning_step(
        "pendiente_prompt_preparado",
        f"Pendiente estructural listo: {tema}",
        f"prompt_chars={len(prompt)}; ficha_chars={ficha_chars}; num_predict={PENDIENTE_OLLAMA_NUM_PREDICT}; timeout={timeout_pendiente}"
        + ("; ficha_compacta_columna" if ficha else "")
    )
    modelo = llamar_ollama(prompt, timeout=timeout_pendiente, num_predict=PENDIENTE_OLLAMA_NUM_PREDICT)
    if not modelo.get("ok"):
        # El cooldown de tema (registrar_exploracion_tema) evita reintentos
        # inmediatos; la cola y el ciclo no se bloquean (la cola corre antes).
        registrar_exploracion_tema(firma, tema, "ollama_error", {"error": modelo.get("error"), "timeout": timeout_pendiente, "cooldown": True})
        add_reasoning_step("pendiente_timeout_cooldown", f"Ollama no respondio pendiente estructural: {modelo.get('error')}", f"timeout={timeout_pendiente}; cooldown activo; sigue intervalo normal")
        cerebro["fase2_avance"] = False
        cerebro["fase2_detener_cadena"] = True
        return cerebro

    parseado = extraer_json_modelo(modelo.get("texto"))
    if not parseado.get("ok"):
        parcial = extraer_decision_parcial_estructural(modelo.get("texto"))
        if parcial:
            registrar_exploracion_tema(firma, tema, "parse_parcial", {"raw": shorten_text(parseado.get("raw"), 500), "decision": parcial})
            add_reasoning_step("pendiente_parse_parcial", "Recupere conclusion desde JSON parcial de pendiente estructural.", shorten_text(parcial.get("conclusion"), 220))
            decision = parcial
        else:
            registrar_exploracion_tema(firma, tema, "parse_error", {"raw": shorten_text(parseado.get("raw"), 500)})
            add_reasoning_step("pendiente_error_parse", "Pendiente estructural sin JSON valido; no guardo conocimiento.", shorten_text(parseado.get("raw"), 220))
            cerebro["fase2_avance"] = False
            cerebro["fase2_detener_cadena"] = True
            return cerebro
    else:
        decision = normalizar_decision_explorador(parseado.get("data") or {})
    if (decision.get("action") or "").lower() == "query":
        registrar_exploracion_tema(firma, tema, "query_rechazada", {"decision": decision})
        add_reasoning_step("pendiente_query_rechazada", "Rechace action=query en pendiente estructural; no ejecuto SQL ni guardo conclusion.", shorten_text(decision.get("query"), 220))
        cerebro["fase2_avance"] = False
        cerebro["fase2_detener_cadena"] = True
        return cerebro
    texto = primer_texto(
        decision.get("conclusion"), decision.get("conclusion_principal"), decision.get("conclusion_autonoma"),
        decision.get("interpretacion"), decision.get("resumen"), decision.get("hipotesis_nueva"), decision.get("hipotesis")
    )
    if not texto:
        registrar_exploracion_tema(firma, tema, "sin_texto_util", {"decision": decision})
        add_reasoning_step("pendiente_sin_conclusion", "El pendiente estructural no produjo texto util.", shorten_text(json.dumps(decision, ensure_ascii=False, default=str), 220))
        cerebro["fase2_avance"] = False
        cerebro["fase2_detener_cadena"] = True
        return cerebro

    conf = confianza_float(decision.get("confianza")) or 0.62
    conf = max(0.55, min(0.75, conf))
    if decision.get("parse_parcial"):
        conf = min(conf, 0.62)
    # Contenido útil casi todo antiguo: bajar confianza
    if ficha and ficha.get("solo_historico"):
        conf = min(conf, 0.60)

    # Guardar solo si aporta algo nuevo con confianza razonable
    if conf < 0.60 and not decision.get("parse_parcial"):
        registrar_exploracion_tema(firma, tema, "confianza_baja", {"conclusion": shorten_text(texto, 300), "confianza": conf})
        add_reasoning_step("pendiente_sin_aporte", f"{tema}: confianza baja ({conf}); no guardo conclusión.", shorten_text(texto, 180))
        cerebro["fase2_avance"] = False
        cerebro["fase2_detener_cadena"] = True
        return cerebro
    lectura_previa = (ficha or {}).get("lectura_backend_previa")
    if lectura_previa and _texto_similar(texto, lectura_previa):
        registrar_exploracion_tema(firma, tema, "sin_novedad", {"conclusion": shorten_text(texto, 300)})
        add_reasoning_step("pendiente_sin_aporte", f"{tema}: la conclusión repite la lectura previa; no la guardo.", shorten_text(texto, 180))
        cerebro["fase2_avance"] = False
        cerebro["fase2_detener_cadena"] = True
        return cerebro

    senales = ["pendiente_estructural", "cartografia_deterministica"]
    if decision.get("parse_parcial"):
        senales.append("parse_parcial")
    if ficha:
        senales.append("ficha_compacta_columna")
        if ficha.get("aparicion_reciente"):
            senales.append("columna_empezo_a_cargarse_recientemente")
            add_reasoning_step(
                "pendiente_senal_interesante",
                f"Columna {columna}: empezó a cargarse recientemente.",
                f"no_nulos_total={ficha.get('no_nulos_total')} no_nulos_30d={ficha.get('no_nulos_30d')}"
            )
        if ficha.get("solo_historico"):
            senales.append("sin_uso_reciente")
    observacion = {
        "ok": True,
        "columna": columna or "mapa_estructural",
        "conclusion": texto,
        "confianza": conf,
        "evidencia": {
            "firma": firma,
            "tema": tema,
            "tarea": pendiente.get("tarea"),
            "evidencia_deterministica": ficha if ficha else pendiente.get("evidencia"),
        },
        "senales": senales,
        "fuerte": False,
    }
    persistencia = persistir_observacion_exploratoria(observacion, decision)
    registrar_exploracion_tema(firma, tema, "observacion_guardada" if persistencia.get("ok") else "persistencia_error", {"persistencia": persistencia, "decision": decision})
    if persistencia.get("ok"):
        add_reasoning_step("observacion_exploratoria", "Observacion estructural guardada desde cartografia.", shorten_text(texto, 220))
    else:
        add_reasoning_step("observacion_exploratoria_error", f"No pude persistir observacion estructural: {persistencia.get('error')}", None)
    cerebro = get_ia_cerebro() or cerebro
    cerebro["fase2_avance"] = bool(persistencia.get("ok"))
    cerebro["fase2_detener_cadena"] = True
    return cerebro

MICROTAREAS_ENABLED = os.environ.get("MICROTAREAS_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
MICROTAREA_COOLDOWN_SEGUNDOS = int(os.environ.get("MICROTAREA_COOLDOWN_SEGUNDOS", "1200"))
MICROTAREA_OLLAMA_TIMEOUT = int(os.environ.get("MICROTAREA_OLLAMA_TIMEOUT", "180"))
MICROTAREA_OLLAMA_NUM_PREDICT = int(os.environ.get("MICROTAREA_OLLAMA_NUM_PREDICT", "120"))
# Cooldown largo tras timeout de Ollama: no reintentar la misma microtarea
# enseguida (clave en modo drain para no bloquear la cola con reintentos de 180s).
MICROTAREA_TIMEOUT_COOLDOWN_SEGUNDOS = max(7200, int(os.environ.get("MICROTAREA_TIMEOUT_COOLDOWN_SEGUNDOS", "7200")))


def _ensure_microtareas_estado(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ia_microtareas_estado (
            id INT AUTO_INCREMENT PRIMARY KEY,
            microtarea VARCHAR(80) NOT NULL,
            ultima_ok DATETIME NULL,
            ultimo_error DATETIME NULL,
            ultima_firma_json JSON NULL,
            ultimo_total_kg DECIMAL(16,2) NULL,
            ultimo_diagnostico VARCHAR(160) NULL,
            UNIQUE KEY uk_microtarea (microtarea)
        ) CHARACTER SET utf8mb4
    """)
    cols = get_table_columns(cur, "ia_microtareas_estado")
    alters = []
    if "ultima_firma_json" not in cols:
        alters.append("ADD COLUMN ultima_firma_json JSON NULL")
    if "ultimo_total_kg" not in cols:
        alters.append("ADD COLUMN ultimo_total_kg DECIMAL(16,2) NULL")
    if "ultimo_diagnostico" not in cols:
        alters.append("ADD COLUMN ultimo_diagnostico VARCHAR(160) NULL")
    for alter in alters:
        try:
            cur.execute(f"ALTER TABLE ia_microtareas_estado {alter}")
        except Exception:
            pass


def _microtarea_en_cooldown(cur, nombre, cooldown_segundos):
    try:
        cur.execute("""
            SELECT ultima_ok, ultimo_error FROM ia_microtareas_estado
            WHERE microtarea = %s
        """, (nombre,))
        row = cur.fetchone()
        if not row:
            return False
        if row.get("ultima_ok"):
            delta = (datetime.now() - row["ultima_ok"]).total_seconds()
            if delta < cooldown_segundos:
                return True
        # Cooldown corto tras timeout/fallo de Ollama: no reintentar enseguida
        if row.get("ultimo_error"):
            delta_err = (datetime.now() - row["ultimo_error"]).total_seconds()
            if delta_err < MICROTAREA_TIMEOUT_COOLDOWN_SEGUNDOS:
                return True
        return False
    except Exception:
        return False


def _microtarea_marcar_timeout(cur, nombre):
    """Registra un fallo/timeout de Ollama para activar el cooldown corto."""
    try:
        cur.execute("""
            INSERT INTO ia_microtareas_estado (microtarea, ultimo_error)
            VALUES (%s, NOW())
            ON DUPLICATE KEY UPDATE ultimo_error = NOW()
        """, (nombre,))
    except Exception:
        pass


def _microtarea_marcar_ok(cur, nombre):
    try:
        cur.execute("""
            INSERT INTO ia_microtareas_estado (microtarea, ultima_ok)
            VALUES (%s, NOW())
            ON DUPLICATE KEY UPDATE ultima_ok = NOW()
        """, (nombre,))
    except Exception:
        pass


def _microtarea_firma_actual(cur, nombre):
    try:
        cur.execute("""
            SELECT ultima_firma_json, ultimo_total_kg, ultimo_diagnostico
            FROM ia_microtareas_estado
            WHERE microtarea = %s
        """, (nombre,))
        row = cur.fetchone() or {}
        firma = row.get("ultima_firma_json")
        if isinstance(firma, str):
            try:
                firma = json.loads(firma)
            except Exception:
                firma = None
        row["ultima_firma"] = firma
        return row
    except Exception:
        return {}


def _kg_bucket(valor, paso=1000):
    try:
        v = float(valor or 0)
    except Exception:
        v = 0.0
    if paso <= 0:
        return round(v, 0)
    return int(round(v / paso) * paso)


def _pct_bucket(valor, paso=5):
    if valor is None:
        return None
    try:
        v = float(valor)
    except Exception:
        return None
    return round(round(v / paso) * paso, 1)


def _diagnostico_microtarea(vs_esperado_pct, confiabilidad):
    conf = str(confiabilidad or "sin_base")
    if vs_esperado_pct is None:
        return f"sin esperado proporcional; normalidad {conf}"
    try:
        vs = float(vs_esperado_pct)
    except Exception:
        return f"sin esperado proporcional; normalidad {conf}"
    if vs >= 25:
        nivel = "por encima del esperado"
    elif vs <= -25:
        nivel = "por debajo del esperado"
    else:
        nivel = "en rango proporcional"
    return f"{nivel}; normalidad {conf}"


def _firma_microtarea_estado(nombre, periodo, total_kg, lider, top_items, esperado_kg, vs_esperado_pct, confiabilidad):
    top = []
    for item in (top_items or [])[:5]:
        nombre_item = item.get("Maquina") or item.get("Proveedor") or "?"
        top.append({
            "nombre": str(nombre_item),
            "kg": _kg_bucket(item.get("kilos_total"), 1000),
        })
    return {
        "microtarea": nombre,
        "periodo": str(periodo),
        "total_kg": _kg_bucket(total_kg, 1000),
        "lider": str(lider or ""),
        "top": top,
        "esperado_kg": _kg_bucket(esperado_kg, 1000),
        "vs_esperado_pct": _pct_bucket(vs_esperado_pct, 5),
        "confiabilidad": str(confiabilidad or ""),
        "diagnostico": _diagnostico_microtarea(vs_esperado_pct, confiabilidad),
    }


def _microtarea_firma_equivalente(firma_a, firma_b):
    if not isinstance(firma_a, dict) or not isinstance(firma_b, dict):
        return False
    claves = ("microtarea", "periodo", "total_kg", "lider", "esperado_kg", "vs_esperado_pct", "confiabilidad", "diagnostico")
    if any(firma_a.get(k) != firma_b.get(k) for k in claves):
        return False
    top_a = [(x.get("nombre"), x.get("kg")) for x in (firma_a.get("top") or [])[:5]]
    top_b = [(x.get("nombre"), x.get("kg")) for x in (firma_b.get("top") or [])[:5]]
    return top_a == top_b


def _microtarea_guardar_firma(cur, nombre, firma, total_kg=None):
    try:
        cur.execute("""
            INSERT INTO ia_microtareas_estado (microtarea, ultima_ok, ultima_firma_json, ultimo_total_kg, ultimo_diagnostico)
            VALUES (%s, NOW(), %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                ultima_ok = NOW(),
                ultima_firma_json = VALUES(ultima_firma_json),
                ultimo_total_kg = VALUES(ultimo_total_kg),
                ultimo_diagnostico = VALUES(ultimo_diagnostico)
        """, (
            nombre,
            json.dumps(firma or {}, ensure_ascii=False, default=str),
            float(total_kg or 0),
            shorten_text((firma or {}).get("diagnostico"), 150),
        ))
    except Exception:
        pass


def _conclusion_deterministica_microtarea(evidencia_doc):
    periodo = evidencia_doc.get("periodo", "?")
    avance = evidencia_doc.get("avance_mes_pct")
    total_kg = float(evidencia_doc.get("total_kg") or 0)
    vs_esp = evidencia_doc.get("actual_vs_esperado_pct")
    confiab = evidencia_doc.get("confiabilidad_normalidad")
    partes = [f"{periodo} abierto"]
    if avance is not None:
        partes[0] += f" ({avance}% del mes)"
    partes.append(f"{int(total_kg):,} kg registrados")
    if vs_esp is not None:
        partes.append(f"{float(vs_esp):+.1f}% vs esperado proporcional")
    partes.append(f"normalidad {confiab or 'sin_base'}")
    return ". ".join(partes) + "."


def extraer_conclusion_microtarea(decision, texto_raw, evidencia_doc=None):
    """Extrae una frase legible de la salida del modelo para microtareas.
    Nunca devuelve JSON completo. Si el modelo no aportó texto legible,
    construye una frase determinística desde evidencia_doc."""
    _CLAVES = ("conclusion", "conclusión", "lectura", "resumen", "texto")

    # 1. Buscar en decision (dict ya parseado)
    if isinstance(decision, dict):
        for k in _CLAVES:
            v = decision.get(k)
            if isinstance(v, str) and len(v.strip()) > 8:
                # verificar que no sea JSON
                t = v.strip()
                if not (t.startswith("{") or t.startswith("[")):
                    return t

    # 2. Si texto_raw parece JSON, parsearlo y reintentar
    if isinstance(texto_raw, str):
        t = texto_raw.strip()
        if t.startswith("{") or t.startswith("["):
            try:
                parsed = json.loads(t)
                if isinstance(parsed, dict):
                    for k in _CLAVES:
                        v = parsed.get(k)
                        if isinstance(v, str) and len(v.strip()) > 8:
                            vc = v.strip()
                            if not (vc.startswith("{") or vc.startswith("[")):
                                return vc
            except Exception:
                pass
        else:
            # texto libre — truncar si es razonable
            if len(t) > 8:
                return t[:400]

    # 3. Fallback determinístico desde evidencia_doc
    if isinstance(evidencia_doc, dict):
        periodo = evidencia_doc.get("periodo", "?")
        avance = evidencia_doc.get("avance_mes_pct")
        total_kg = evidencia_doc.get("total_kg")
        vs_esp = evidencia_doc.get("actual_vs_esperado_pct")
        confiab = evidencia_doc.get("confiabilidad_normalidad")
        meses_b = evidencia_doc.get("meses_base")

        partes = [f"{periodo} abierto"]
        if avance is not None:
            partes[0] += f" ({avance}% del mes)"
        if total_kg is not None:
            partes.append(f"{int(total_kg):,} kg registrados")
        if vs_esp is not None:
            partes.append(f"{vs_esp:+.1f}% vs esperado proporcional")
        if confiab and meses_b:
            partes.append(f"normalidad {confiab} ({meses_b} meses base)")
        elif confiab:
            partes.append(f"normalidad {confiab}")
        return ". ".join(partes) + "."

    # 4. Último recurso
    if isinstance(texto_raw, str) and texto_raw.strip():
        return texto_raw.strip()[:300]
    return "Sin conclusión disponible."


def _confiabilidad_normalidad(meses_con_actividad):
    """Clasifica cuán confiable es un promedio histórico según cuántos meses lo respaldan."""
    n = int(meses_con_actividad or 0)
    if n >= 12:
        return "fuerte"
    if n >= 6:
        return "media"
    if n >= 2:
        return "debil"
    return "antecedente_unico"


def _calcular_avance_temporal_mes(periodo):
    """Devuelve dict con métricas temporales proporcionales para el periodo dado (YYYY-MM).
    Cálculo simple de horas transcurridas / horas totales del mes calendario.
    Siempre usa hora local del servidor."""
    try:
        ahora = datetime.now()
        anio, mes = int(periodo[:4]), int(periodo[5:7])
        inicio = datetime(anio, mes, 1, 0, 0, 0)
        # último día del mes
        ultimo_dia = (datetime(anio, mes + 1, 1) if mes < 12 else datetime(anio + 1, 1, 1)) - timedelta(seconds=1)
        fin = datetime(ultimo_dia.year, ultimo_dia.month, ultimo_dia.day, 23, 59, 59)
        horas_totales = max((fin - inicio).total_seconds() / 3600, 1)
        horas_transcurridas = min(max((ahora - inicio).total_seconds() / 3600, 0), horas_totales)
        avance_pct = round(horas_transcurridas / horas_totales * 100, 1)
        return {
            "ahora_local": ahora.strftime("%Y-%m-%d %H:%M"),
            "inicio_periodo": inicio.strftime("%Y-%m-%d"),
            "fin_periodo": fin.strftime("%Y-%m-%d"),
            "horas_transcurridas_mes": round(horas_transcurridas, 1),
            "horas_totales_mes": round(horas_totales, 1),
            "avance_mes_pct": avance_pct,
        }
    except Exception:
        return {
            "ahora_local": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "inicio_periodo": periodo + "-01",
            "fin_periodo": periodo + "-28",
            "horas_transcurridas_mes": 0,
            "horas_totales_mes": 720,
            "avance_mes_pct": 0,
        }


def ejecutar_microtarea_estado_produccion_mes_actual(cerebro):
    """Microtarea determinística: lee tablas derivadas y pide a Ollama una conclusión
    compacta sobre la producción del mes actual. Si corre OK, devuelve cerebro
    actualizado para cortar el ciclo antes del explorador libre."""
    nombre = "estado_produccion_mes_actual"
    add_reasoning_step("microtarea_inicio", f"Microtarea: {nombre}", None)

    conn_lab = get_ia_connection()
    if not conn_lab:
        add_reasoning_step("microtarea_error", f"{nombre}: sin conexión a crowdbot_lab", None)
        return None
    ollama_intentado = False
    try:
        cur_lab = conn_lab.cursor()
        _ensure_microtareas_estado(cur_lab)
        conn_lab.commit()

        if _microtarea_en_cooldown(cur_lab, nombre, MICROTAREA_COOLDOWN_SEGUNDOS):
            add_reasoning_step("microtarea_cooldown", f"{nombre}: en cooldown, salteo", None)
            cur_lab.close()
            conn_lab.close()
            return None

        # 1. Periodo abierto más reciente
        cur_lab.execute("""
            SELECT periodo, estado_periodo
            FROM ia_periodos_procesados
            WHERE estado_periodo = 'abierto' AND criterio_version = %s
            ORDER BY periodo DESC LIMIT 1
        """, (DERIVADAS_VERSION,))
        periodo_row = cur_lab.fetchone()
        if not periodo_row:
            add_reasoning_step("microtarea_error", f"{nombre}: no hay periodo abierto en tablas derivadas", None)
            cur_lab.close()
            conn_lab.close()
            return None
        periodo = periodo_row["periodo"]
        estado_periodo = periodo_row["estado_periodo"]

        # 2. Top máquinas del periodo
        cur_lab.execute("""
            SELECT Maquina, registros, kilos_total
            FROM ia_resumen_mensual_produccion_maquina
            WHERE periodo = %s AND criterio_version = %s
            ORDER BY kilos_total DESC LIMIT 5
        """, (periodo, DERIVADAS_VERSION))
        top_maquinas = cur_lab.fetchall() or []

        # 3. Total producción del periodo
        cur_lab.execute("""
            SELECT registros, kilos_total
            FROM ia_resumen_mensual_tipo_movimiento
            WHERE periodo = %s AND criterio_version = %s
              AND TiposDeMovimiento = 'PRODUCCION'
        """, (periodo, DERIVADAS_VERSION))
        total_row = cur_lab.fetchone() or {}
        total_kg = float(total_row.get("kilos_total") or 0)
        total_reg = int(total_row.get("registros") or 0)

        # 4. Normal histórica: promedio de meses cerrados + confiabilidad
        cur_lab.execute("""
            SELECT AVG(kilos_total) AS normal_kg, COUNT(DISTINCT periodo) AS meses_base
            FROM ia_resumen_mensual_tipo_movimiento
            WHERE criterio_version = %s AND estado_periodo = 'cerrado'
              AND TiposDeMovimiento = 'PRODUCCION'
        """, (DERIVADAS_VERSION,))
        normal_row = cur_lab.fetchone() or {}
        normal_kg = float(normal_row.get("normal_kg") or 0)
        meses_base = int(normal_row.get("meses_base") or 0)
        confiabilidad_norm = _confiabilidad_normalidad(meses_base)

        # 5. Avance temporal proporcional
        avance = _calcular_avance_temporal_mes(periodo)
        avance_pct = avance["avance_mes_pct"]
        esperado_kg = round(normal_kg * avance_pct / 100, 0) if normal_kg > 0 else 0
        vs_esperado_pct = None
        if esperado_kg > 0:
            vs_esperado_pct = round((total_kg - esperado_kg) / esperado_kg * 100, 1)

        # 6. Armar ficha compacta
        maq_lines = "; ".join(
            f"{r['Maquina']}={int(float(r['kilos_total'] or 0)):,}kg/{r['registros']}reg"
            for r in top_maquinas[:5]
        ) or "sin datos"
        ficha_lines = [
            f"microtarea={nombre}",
            f"periodo={periodo} estado={estado_periodo}",
            f"ahora_local={avance['ahora_local']} avance_mes={avance_pct}%",
            f"total_produccion={int(total_kg):,}kg registros={total_reg}",
        ]
        if normal_kg > 0:
            ficha_lines.append(
                f"normal_mensual_historico={int(normal_kg):,}kg "
                f"(meses_base={meses_base} confiabilidad={confiabilidad_norm})"
            )
            ficha_lines.append(f"esperado_proporcional={int(esperado_kg):,}kg (aprox. por horas transcurridas)")
            if vs_esperado_pct is not None:
                ficha_lines.append(f"actual_vs_esperado={vs_esperado_pct:+.1f}% (aproximacion temporal simple)")
        ficha_lines += [
            f"top_maquinas: {maq_lines}",
            "regla: periodo abierto, comparar solo contra esperado proporcional, no contra total mensual completo",
        ]
        ficha = "\n".join(ficha_lines)

        evidencia_doc = {
            "microtarea": nombre,
            "periodo": periodo,
            "estado_periodo": estado_periodo,
            "ficha": ficha,
            "total_kg": total_kg,
            "total_registros": total_reg,
            "avance_mes_pct": avance_pct,
            "normal_kg": normal_kg,
            "meses_base": meses_base,
            "confiabilidad_normalidad": confiabilidad_norm,
            "esperado_proporcional_kg": esperado_kg,
            "actual_vs_esperado_pct": vs_esperado_pct,
            "top_maquinas": [
                {"Maquina": r["Maquina"], "kilos_total": float(r["kilos_total"] or 0), "registros": r["registros"]}
                for r in top_maquinas[:5]
            ],
        }
        lider_maquina = top_maquinas[0].get("Maquina") if top_maquinas else ""
        firma_estado = _firma_microtarea_estado(
            nombre, periodo, total_kg, lider_maquina, evidencia_doc["top_maquinas"],
            esperado_kg, vs_esperado_pct, confiabilidad_norm
        )
        firma_previa = _microtarea_firma_actual(cur_lab, nombre).get("ultima_firma")
        if _microtarea_firma_equivalente(firma_estado, firma_previa):
            add_reasoning_step(
                "microtarea_sin_novedad",
                "estado_produccion_mes_actual sin cambios materiales; no consulto Ollama.",
                f"periodo={periodo} total_bucket={firma_estado.get('total_kg')} lider={firma_estado.get('lider')} diagnostico={firma_estado.get('diagnostico')}"
            )
            _microtarea_guardar_firma(cur_lab, nombre, firma_estado, total_kg)
            conn_lab.commit()
            cur_lab.close()
            conn_lab.close()
            return None

        # Instrucción según confiabilidad
        if confiabilidad_norm in ("antecedente_unico", "debil"):
            _norm_instruccion = (
                "Si mencionás la normalidad, usá la expresión 'antecedente histórico limitado' "
                f"({meses_base} mes/meses), no 'normal'."
            )
        else:
            _norm_instruccion = "Si hay actual_vs_esperado en la ficha, podés mencionarlo como aproximación temporal."

        _ej_vs = f", {vs_esperado_pct:+.1f}% vs esperado proporcional" if vs_esperado_pct is not None else ""
        prompt = (
            "Sos analista de Ecotecnica, planta de reciclado. Tenes esta ficha operativa:\n\n"
            f"{ficha}\n\n"
            f"RESTRICCIONES ESTRICTAS: no menciones meta ni objetivo. {_norm_instruccion} "
            "Aclará que el periodo está abierto y que es estimación parcial.\n"
            "Respondé SOLO JSON (sin texto extra):\n"
            '{"action":"conclude",'
            f'"conclusion":"ej: Julio abierto ({avance_pct}% del mes): {int(total_kg):,} kg procesados{_ej_vs}. '
            'Máquinas líderes: [nombres].",'
            '"confianza":0.0,"evidencia":"dato concreto de la ficha"}'
        )
        prompt = prompt[:920]  # hard cap

        add_reasoning_step(
            "microtarea_prompt_preparado",
            f"Prompt microtarea preparado: {len(prompt)} chars",
            f"periodo={periodo} total_kg={int(total_kg):,} avance={avance_pct}% esperado={int(esperado_kg):,}kg confiabilidad={confiabilidad_norm}"
        )

        # 5. Llamar Ollama — a partir de aquí siempre devolvemos cerebro (no None)
        # para que el explorador libre NO corra en este ciclo aunque falle Ollama.
        ollama_intentado = True
        modelo = llamar_ollama(prompt, timeout=MICROTAREA_OLLAMA_TIMEOUT, num_predict=MICROTAREA_OLLAMA_NUM_PREDICT)
        if not modelo.get("ok"):
            add_reasoning_step("microtarea_error", f"{nombre}: Ollama no respondió: {modelo.get('error')}", None)
            _microtarea_marcar_timeout(cur_lab, nombre)
            fallback_txt = _conclusion_deterministica_microtarea(evidencia_doc)
            if total_kg > 0:
                _insert_conclusion_autonoma(cur_lab, {
                    "columna": nombre,
                    "conclusion": fallback_txt,
                    "confianza": 0.55,
                    "estado": "observacion_operativa_deterministica",
                    "evidencia": evidencia_doc,
                    "salida_json": {
                        "tipo": "fallback_deterministico",
                        "motivo": "ollama_timeout",
                        "firma": firma_estado,
                    },
                })
                _microtarea_guardar_firma(cur_lab, nombre, firma_estado, total_kg)
            conn_lab.commit()
            add_reasoning_step(
                "microtarea_timeout_cooldown",
                f"{nombre}: cooldown tras timeout de Ollama.",
                f"cooldown_horas={round(MICROTAREA_TIMEOUT_COOLDOWN_SEGUNDOS / 3600, 1)}"
            )
            if total_kg > 0:
                add_reasoning_step(
                    "observacion_guardada",
                    f"{nombre}: fallback determinístico persistido",
                    shorten_text(fallback_txt, 220)
                )
                notificar_telegram_microtarea(
                    nombre, periodo, avance_pct, total_kg,
                    vs_esperado_pct, 0.55, fallback_txt,
                    evidencia=evidencia_doc, cerebro=cerebro
                )
            cur_lab.close()
            conn_lab.close()
            cerebro["fase2_avance"] = False
            return None

        parseado = extraer_json_modelo(modelo.get("texto"))
        decision = parseado.get("data") if parseado.get("ok") else None
        confianza = confianza_float((decision or {}).get("confianza")) or 0.60

        # Guardar sólo el texto de conclusión, nunca el JSON completo como string
        conclusion_txt = extraer_conclusion_microtarea(decision, modelo.get("texto"), evidencia_doc)

        add_reasoning_step(
            "microtarea_resultado",
            f"{nombre}: conclusión obtenida",
            shorten_text(conclusion_txt, 200)
        )

        # Telegram instrumentado
        notificar_telegram_microtarea(
            nombre, periodo, avance_pct, total_kg,
            vs_esperado_pct, confianza, conclusion_txt,
            evidencia=evidencia_doc, cerebro=cerebro
        )

        _insert_conclusion_autonoma(cur_lab, {
            "columna": nombre,
            "conclusion": conclusion_txt,
            "confianza": confianza,
            "estado": "observacion_exploratoria",
            "evidencia": evidencia_doc,
            "salida_json": decision,
        })
        _microtarea_guardar_firma(cur_lab, nombre, firma_estado, total_kg)
        conn_lab.commit()

        add_reasoning_step(
            "observacion_guardada",
            f"{nombre}: observación persistida en ia_conclusiones_autonomas",
            f"periodo={periodo} confianza={confianza} meses_base={meses_base} confiabilidad={confiabilidad_norm}"
        )

        cur_lab.close()
        conn_lab.close()

        cerebro["fase2_avance"] = True
        return cerebro

    except Exception as e:
        add_reasoning_step("microtarea_error", f"{nombre}: excepción: {e}", None)
        try:
            conn_lab.close()
        except Exception:
            pass
        if ollama_intentado:
            cerebro["fase2_avance"] = False
            return cerebro
        return None


def ejecutar_microtarea_estado_ingresos_mes_actual(cerebro):
    """Microtarea: lee ia_resumen_mensual_ingresos_proveedor y genera una ficha para Ollama."""
    nombre = "estado_ingresos_mes_actual"
    add_reasoning_step("microtarea_inicio", f"Microtarea: {nombre}", None)

    conn_lab = get_ia_connection()
    if not conn_lab:
        add_reasoning_step("microtarea_error", f"{nombre}: sin conexión a crowdbot_lab", None)
        return None
    ollama_intentado = False
    try:
        cur_lab = conn_lab.cursor()
        _ensure_microtareas_estado(cur_lab)
        conn_lab.commit()

        if _microtarea_en_cooldown(cur_lab, nombre, MICROTAREA_COOLDOWN_SEGUNDOS):
            add_reasoning_step("microtarea_cooldown", f"{nombre}: en cooldown, salteo", None)
            cur_lab.close()
            conn_lab.close()
            return None

        cur_lab.execute("""
            SELECT periodo, estado_periodo
            FROM ia_periodos_procesados
            WHERE estado_periodo = 'abierto' AND criterio_version = %s
            ORDER BY periodo DESC LIMIT 1
        """, (DERIVADAS_VERSION,))
        periodo_row = cur_lab.fetchone()
        if not periodo_row:
            add_reasoning_step("microtarea_error", f"{nombre}: no hay periodo abierto en tablas derivadas", None)
            cur_lab.close()
            conn_lab.close()
            return None
        periodo = periodo_row["periodo"]
        estado_periodo = periodo_row["estado_periodo"]

        cur_lab.execute("""
            SELECT Proveedor, registros, kilos_total
            FROM ia_resumen_mensual_ingresos_proveedor
            WHERE periodo = %s AND criterio_version = %s
            ORDER BY kilos_total DESC LIMIT 5
        """, (periodo, DERIVADAS_VERSION))
        top_proveedores = cur_lab.fetchall() or []

        cur_lab.execute("""
            SELECT SUM(registros) AS total_reg, SUM(kilos_total) AS total_kg
            FROM ia_resumen_mensual_ingresos_proveedor
            WHERE periodo = %s AND criterio_version = %s
        """, (periodo, DERIVADAS_VERSION))
        totales = cur_lab.fetchone() or {}
        total_kg = float(totales.get("total_kg") or 0)
        total_reg = int(totales.get("total_reg") or 0)

        # Normal histórica de ingresos: SUM por mes cerrado / cantidad de meses + confiabilidad
        cur_lab.execute("""
            SELECT p.periodo, SUM(i.kilos_total) AS kg_mes
            FROM ia_resumen_mensual_ingresos_proveedor i
            JOIN ia_periodos_procesados p
              ON i.periodo = p.periodo AND i.criterio_version = p.criterio_version
            WHERE p.estado_periodo = 'cerrado' AND p.criterio_version = %s
            GROUP BY p.periodo
        """, (DERIVADAS_VERSION,))
        hist_rows = cur_lab.fetchall() or []
        meses_base = len(hist_rows)
        if hist_rows:
            normal_kg = float(sum(float(r.get("kg_mes") or 0) for r in hist_rows) / meses_base)
        else:
            normal_kg = 0.0
        confiabilidad_norm = _confiabilidad_normalidad(meses_base)

        # Avance temporal proporcional
        avance = _calcular_avance_temporal_mes(periodo)
        avance_pct = avance["avance_mes_pct"]
        esperado_kg = round(normal_kg * avance_pct / 100, 0) if normal_kg > 0 else 0
        vs_esperado_pct = None
        if esperado_kg > 0:
            vs_esperado_pct = round((total_kg - esperado_kg) / esperado_kg * 100, 1)

        prov_lines = "; ".join(
            f"{r['Proveedor']}={int(float(r['kilos_total'] or 0)):,}kg/{r['registros']}reg"
            for r in top_proveedores[:5]
        ) or "sin datos"
        ficha_lines = [
            f"microtarea={nombre}",
            f"periodo={periodo} estado={estado_periodo}",
            f"ahora_local={avance['ahora_local']} avance_mes={avance_pct}%",
            f"total_ingresos={int(total_kg):,}kg registros={total_reg}",
        ]
        if normal_kg > 0:
            ficha_lines.append(
                f"normal_mensual_historico={int(normal_kg):,}kg "
                f"(meses_base={meses_base} confiabilidad={confiabilidad_norm})"
            )
            ficha_lines.append(f"esperado_proporcional={int(esperado_kg):,}kg (aprox. por horas transcurridas)")
            if vs_esperado_pct is not None:
                ficha_lines.append(f"actual_vs_esperado={vs_esperado_pct:+.1f}% (aproximacion temporal simple)")
        ficha_lines += [
            f"top_proveedores: {prov_lines}",
            "fuente=ia_resumen_mensual_ingresos_proveedor",
            "regla: periodo abierto, comparar solo contra esperado proporcional, no contra total mensual completo",
        ]
        ficha = "\n".join(ficha_lines)

        evidencia_doc = {
            "microtarea": nombre,
            "periodo": periodo,
            "estado_periodo": estado_periodo,
            "ficha": ficha,
            "total_kg": total_kg,
            "total_registros": total_reg,
            "avance_mes_pct": avance_pct,
            "normal_kg": normal_kg,
            "meses_base": meses_base,
            "confiabilidad_normalidad": confiabilidad_norm,
            "esperado_proporcional_kg": esperado_kg,
            "actual_vs_esperado_pct": vs_esperado_pct,
            "top_proveedores": [
                {"Proveedor": r["Proveedor"], "kilos_total": float(r["kilos_total"] or 0), "registros": r["registros"]}
                for r in top_proveedores[:5]
            ],
            "fuente": "ia_resumen_mensual_ingresos_proveedor",
        }
        lider_proveedor = top_proveedores[0].get("Proveedor") if top_proveedores else ""
        firma_estado = _firma_microtarea_estado(
            nombre, periodo, total_kg, lider_proveedor, evidencia_doc["top_proveedores"],
            esperado_kg, vs_esperado_pct, confiabilidad_norm
        )
        firma_previa = _microtarea_firma_actual(cur_lab, nombre).get("ultima_firma")
        if _microtarea_firma_equivalente(firma_estado, firma_previa):
            add_reasoning_step(
                "microtarea_sin_novedad",
                f"{nombre} sin cambios materiales; no consulto Ollama.",
                f"periodo={periodo} total_bucket={firma_estado.get('total_kg')} lider={firma_estado.get('lider')} diagnostico={firma_estado.get('diagnostico')}"
            )
            _microtarea_guardar_firma(cur_lab, nombre, firma_estado, total_kg)
            conn_lab.commit()
            cur_lab.close()
            conn_lab.close()
            return None

        if confiabilidad_norm in ("antecedente_unico", "debil"):
            _norm_instruccion = (
                "Si mencionás la normalidad, usá la expresión 'antecedente histórico limitado' "
                f"({meses_base} mes/meses), no 'normal'."
            )
        else:
            _norm_instruccion = "Si hay actual_vs_esperado en la ficha, podés mencionarlo como aproximación temporal."

        _ej_vs = f", {vs_esperado_pct:+.1f}% vs esperado proporcional" if vs_esperado_pct is not None else ""
        prompt = (
            "Sos analista de Ecotecnica, planta de reciclado. Tenes esta ficha operativa:\n\n"
            f"{ficha}\n\n"
            f"RESTRICCIONES ESTRICTAS: no menciones meta ni objetivo. {_norm_instruccion} "
            "Aclará que el periodo está abierto y que es estimación parcial.\n"
            "Respondé SOLO JSON (sin texto extra):\n"
            '{"action":"conclude",'
            f'"conclusion":"ej: Julio abierto ({avance_pct}% del mes): {int(total_kg):,} kg de ingresos{_ej_vs}. '
            'Proveedores: [nombres].",'
            '"confianza":0.0,"evidencia":"dato concreto de la ficha"}'
        )
        prompt = prompt[:920]

        add_reasoning_step(
            "microtarea_prompt_preparado",
            f"Prompt microtarea preparado: {len(prompt)} chars",
            f"periodo={periodo} total_kg={int(total_kg):,} avance={avance_pct}% esperado={int(esperado_kg):,}kg confiabilidad={confiabilidad_norm}"
        )

        ollama_intentado = True
        modelo = llamar_ollama(prompt, timeout=MICROTAREA_OLLAMA_TIMEOUT, num_predict=MICROTAREA_OLLAMA_NUM_PREDICT)
        if not modelo.get("ok"):
            add_reasoning_step("microtarea_error", f"{nombre}: Ollama no respondió: {modelo.get('error')}", None)
            _microtarea_marcar_timeout(cur_lab, nombre)
            fallback_txt = _conclusion_deterministica_microtarea(evidencia_doc)
            if total_kg > 0:
                _insert_conclusion_autonoma(cur_lab, {
                    "columna": nombre,
                    "conclusion": fallback_txt,
                    "confianza": 0.55,
                    "estado": "observacion_operativa_deterministica",
                    "evidencia": evidencia_doc,
                    "salida_json": {
                        "tipo": "fallback_deterministico",
                        "motivo": "ollama_timeout",
                        "firma": firma_estado,
                    },
                })
                _microtarea_guardar_firma(cur_lab, nombre, firma_estado, total_kg)
            conn_lab.commit()
            add_reasoning_step(
                "microtarea_timeout_cooldown",
                f"{nombre}: cooldown tras timeout de Ollama.",
                f"cooldown_horas={round(MICROTAREA_TIMEOUT_COOLDOWN_SEGUNDOS / 3600, 1)}"
            )
            if total_kg > 0:
                add_reasoning_step(
                    "observacion_guardada",
                    f"{nombre}: fallback determinístico persistido",
                    shorten_text(fallback_txt, 220)
                )
                notificar_telegram_microtarea(
                    nombre, periodo, avance_pct, total_kg,
                    vs_esperado_pct, 0.55, fallback_txt,
                    evidencia=evidencia_doc, cerebro=cerebro
                )
            cur_lab.close()
            conn_lab.close()
            cerebro["fase2_avance"] = False
            return None

        parseado = extraer_json_modelo(modelo.get("texto"))
        decision = parseado.get("data") if parseado.get("ok") else None
        confianza = confianza_float((decision or {}).get("confianza")) or 0.70

        # Guardar sólo el texto de conclusión, nunca el JSON completo como string
        conclusion_txt = extraer_conclusion_microtarea(decision, modelo.get("texto"), evidencia_doc)

        add_reasoning_step("microtarea_resultado", f"{nombre}: conclusión obtenida", shorten_text(conclusion_txt, 200))

        # Telegram instrumentado
        notificar_telegram_microtarea(
            nombre, periodo, avance_pct, total_kg,
            vs_esperado_pct, confianza, conclusion_txt,
            evidencia=evidencia_doc, cerebro=cerebro
        )

        _insert_conclusion_autonoma(cur_lab, {
            "columna": nombre,
            "conclusion": conclusion_txt,
            "confianza": confianza,
            "estado": "observacion_exploratoria",
            "evidencia": evidencia_doc,
            "salida_json": decision,
        })
        _microtarea_guardar_firma(cur_lab, nombre, firma_estado, total_kg)
        conn_lab.commit()

        add_reasoning_step(
            "observacion_guardada",
            f"{nombre}: observación persistida en ia_conclusiones_autonomas",
            f"periodo={periodo} confianza={confianza} meses_base={meses_base} confiabilidad={confiabilidad_norm}"
        )

        cur_lab.close()
        conn_lab.close()
        cerebro["fase2_avance"] = True
        return cerebro

    except Exception as e:
        add_reasoning_step("microtarea_error", f"{nombre}: excepción: {e}", None)
        try:
            conn_lab.close()
        except Exception:
            pass
        if ollama_intentado:
            cerebro["fase2_avance"] = False
            return cerebro
        return None


def ejecutar_fase2_ia(cerebro, modo_drain=False):
    """Fase 2: análisis operativo. En todos los modos el orden es
    cola de curiosidad -> microtareas Ollama -> pendientes estructurales ->
    explorador viejo (opt-in). modo_drain se conserva por compatibilidad."""
    if MICROTAREAS_ENABLED:
        # El ciclo prioriza microtareas: no anunciar exploración libre que no va a ocurrir.
        add_reasoning_step(
            "explorador_inicio",
            "Iniciando ciclo IA operativo.",
            "Orden: cola de curiosidad -> microtareas -> pendientes estructurales -> explorador. Solo lectura."
        )
    else:
        add_reasoning_step(
            "explorador_inicio",
            "Iniciando exploración autónoma de la base de datos.",
            f"Solo lectura. Max {FASE2_MAX_PASOS} pasos por sesion. Esquema operativo precargado."
        )

    cartografia = guardar_cartografia_base_si_corresponde()
    if cartografia.get("ok"):
        add_reasoning_step(
            "cartografia_base",
            "Actualice cartografia deterministica de base antes de consultar al modelo.",
            f"Tablas: {cartografia.get('tablas')} | columnas TiposDeMovimiento: {cartografia.get('columnas')} | tipos: {cartografia.get('tipos_movimiento')}"
        )
    elif cartografia.get("error"):
        add_reasoning_step("cartografia_base_error", "No pude actualizar cartografia deterministica.", cartografia.get("error"))

    # Orden del ciclo (todos los modos): 1. cola de curiosidad (determinística,
    # sin Ollama) -> 2. microtareas Ollama -> 3. pendientes estructurales (solo
    # sin cola disponible) -> 4. explorador viejo (opt-in).
    # La cola nunca espera a Ollama: un timeout de microtarea no puede bloquearla.

    # 1. Cola de curiosidad disponible: consumir SIEMPRE antes de llamar a Ollama
    resultado_cola = ejecutar_tarea_cola_curiosidad()
    if resultado_cola is not None:
        add_reasoning_step(
            "cola_curiosidad_priorizada",
            "Cola de curiosidad procesada antes de microtareas/Ollama.",
            f"tarea={resultado_cola.get('tipo_tarea')} cola_id={resultado_cola.get('cola_id')}"
        )
        cerebro["fase2_avance"] = bool(resultado_cola.get("ok"))
        return cerebro

    # 2. Microtareas operativas (Ollama); sus cooldowns —incluido el de
    #    timeout— evitan reintentos repetidos
    if MICROTAREAS_ENABLED:
        cerebro_microtarea = ejecutar_microtarea_estado_produccion_mes_actual(cerebro)
        if cerebro_microtarea is not None:
            return cerebro_microtarea
        # Produccion en cooldown: intentar ingresos antes de seguir.
        cerebro_microtarea = ejecutar_microtarea_estado_ingresos_mes_actual(cerebro)
        if cerebro_microtarea is not None:
            return cerebro_microtarea
        add_reasoning_step(
            "microtarea_sin_pendientes",
            "Microtareas habilitadas pero ninguna disponible (cooldown o sin datos). Sigo con pendientes estructurales.",
            None
        )

    # 3. Pendientes estructurales: solo cuando la cola está vacía o toda en cooldown.
    if hay_cola_curiosidad_pendiente_disponible():
        # El consumidor no pudo tomar la tarea (p.ej. error transitorio de conexión)
        # pero la cola sigue disponible: no gastar Ollama en pendientes estructurales.
        add_reasoning_step(
            "cola_curiosidad_priorizada",
            "Cola de curiosidad disponible; salteo pendiente estructural en este ciclo.",
            None
        )
        cerebro["fase2_avance"] = False
        return cerebro

    cerebro_pendiente = ejecutar_pendiente_estructural(cerebro)
    if cerebro_pendiente is not None:
        return cerebro_pendiente

    # 4. Explorador viejo: solo si está habilitado explícitamente. Sin trabajo
    # determinístico útil, mejor quedarse quieto que gastar Ollama en prompts
    # largos que terminan en timeout o explorador_sin_conclusion.
    if not EXPLORADOR_LIBRE_ENABLED:
        add_reasoning_step(
            "ciclo_sin_trabajo_util",
            "Sin microtareas, cola ni pendientes estructurales; ciclo en reposo.",
            "Explorador libre deshabilitado (EXPLORADOR_LIBRE_ENABLED=0)."
        )
        cerebro["fase2_avance"] = False
        return cerebro

    conocimientos_completos = []
    relaciones_interesantes = (cerebro.get("relaciones_interesantes") or [])[:5]

    contexto_temporal_ciclo = construir_contexto_temporal_operativo(persistir=False)
    periodo_objetivo_ciclo = obtener_periodo_objetivo_explorador(contexto_temporal_ciclo)

    MAX_PASOS = FASE2_MAX_PASOS
    historial_queries = []
    resultado_final = None
    ultima_decision_modelo = None
    ultima_query_sql = None
    ultimo_resultado_sql = None
    observacion_exploratoria_cerrada = False

    for paso in range(MAX_PASOS):
        pasos_restantes = MAX_PASOS - paso

        if paso == 0:
            prompt = prompt_explorador_inicial(
                conocimientos_completos, historial_queries, relaciones_interesantes,
                contexto_temporal=contexto_temporal_ciclo, periodo_objetivo=periodo_objetivo_ciclo
            )
        else:
            ultimo = historial_queries[-1]
            prompt = prompt_explorador_siguiente(
                ultimo["query"],
                ultimo["resultado_txt"],
                historial_queries,
                conocimientos_completos,
                pasos_restantes,
                relaciones_interesantes,
                contexto_temporal=contexto_temporal_ciclo,
                periodo_objetivo=periodo_objetivo_ciclo
            )

        add_reasoning_step(
            "explorador_consulta_modelo",
            f"Paso {paso + 1}/{MAX_PASOS}: consultando a {OLLAMA_MODEL}.",
            f"Prompt con {len(prompt)} chars."
        )

        modelo = llamar_ollama(prompt, timeout=FASE2_OLLAMA_TIMEOUT_SECONDS, num_predict=FASE2_OLLAMA_NUM_PREDICT)
        if not modelo.get("ok"):
            add_reasoning_step("explorador_error_ollama", f"Ollama no respondió: {modelo.get('error')}", None)
            break

        parseado = extraer_json_modelo(modelo.get("texto"))
        decision = parseado.get("data") if parseado.get("ok") else None
        if not decision:
            query_recuperada = extraer_query_sql_desde_texto(parseado.get("raw"))
            if query_recuperada:
                add_reasoning_step(
                    "explorador_recupero_query_parse",
                    "Recupere query desde salida JSON invalida.",
                    shorten_text(query_recuperada, 220)
                )
                decision = {
                    "action": "query",
                    "query": query_recuperada,
                    "razonamiento": "Query recuperada desde respuesta no parseable del modelo.",
                    "que_espero_encontrar": "Continuar exploracion sin guardar conocimiento hasta tener conclusion valida.",
                }
            else:
                detalle_parse = f"{parseado.get('error')} | raw: {shorten_text(parseado.get('raw'), 220)}"
                add_reasoning_step(
                    "explorador_error_parse",
                    "No devolvio JSON valido; no guardo conocimiento.",
                    detalle_parse
                )
                if texto_modelo_util_no_json(parseado.get("raw")):
                    add_reasoning_step(
                        "explorador_sin_conclusion",
                        "El modelo devolvio texto util pero no JSON; ciclo descartado para evitar basura.",
                        shorten_text(parseado.get("raw"), 260)
                    )
                break
        decision = normalizar_decision_explorador(decision)
        ultima_decision_modelo = decision
        action = decision.get("action", "query")
        razonamiento = decision.get("razonamiento", "")

        if action in ("conclude", "hypothesis"):
            if action == "hypothesis" and not decision.get("conclusion"):
                decision["conclusion"] = decision.get("hipotesis_nueva") or "Hipotesis generada por el explorador."
            add_reasoning_step(
                "explorador_concluye",
                f"El modelo decidio {action}: {shorten_text(decision.get('conclusion') or decision.get('hipotesis_nueva') or '', 120)}",
                razonamiento or decision.get("evidencia")
            )
            resultado_final = decision
            break

        # Ejecutar query
        query = preparar_query_explorador(decision.get("query")) or ""
        decision["query"] = query
        if not query_explorador_valida(query):
            detalle = razonamiento or shorten_text(json.dumps(decision, ensure_ascii=False, default=str), 220)
            add_reasoning_step("explorador_sin_query", "Action query sin query SQL valida; ciclo cortado sin guardar conocimiento.", detalle)
            break

        if not query_usa_tabla_valida(query):
            add_reasoning_step(
                "explorador_tabla_invalida",
                "La query referencia una tabla que no existe; la unica tabla disponible es TiposDeMovimiento. Ventas/despachos son un valor de su columna TiposDeMovimiento, no una tabla aparte.",
                shorten_text(query, 220)
            )
            break

        query_norm = normalizar_query_ciclo(query)
        if any(h.get("query_norm") == query_norm for h in historial_queries):
            add_reasoning_step(
                "explorador_query_repetida",
                "El modelo repitio una query del mismo ciclo; cierro observacion exploratoria si hay evidencia.",
                shorten_text(query, 220)
            )
            verdades_capturadas = intentar_capturar_verdad_periodo_de_resultado(
                ultima_query_sql, ultimo_resultado_sql, periodo_objetivo_ciclo
            )
            if verdades_capturadas:
                add_reasoning_step(
                    "explorador_periodo_confirmado",
                    f"El modelo no concluyo, pero ya tenia el dato limpio de {periodo_objetivo_ciclo.get('periodo_label')}; lo guarde igual.",
                    " | ".join(f"{m}={v}" for m, v in verdades_capturadas)
                )
                cerebro["fase2_avance"] = True
                break
            obs_persistida = guardar_observacion_exploratoria_si_corresponde(
                ultima_query_sql,
                ultimo_resultado_sql,
                decision or ultima_decision_modelo
            )
            observacion_exploratoria_cerrada = bool(obs_persistida.get("ok"))
            if observacion_exploratoria_cerrada:
                cerebro["fase2_avance"] = True
            break
        if query_sin_marco_temporal(query):
            add_reasoning_step(
                "explorador_sin_marco_temporal",
                "Agregacion sin ningun periodo acotado; un ranking de toda la historia no es una verdad utilizable.",
                "Corregir: acotar a un periodo (mes cerrado, anio, rango con MONTH()/YEAR() o FechaHora BETWEEN) para fijar una verdad de ese periodo, o usar el mes anterior si se busca el ultimo dato cerrado."
            )
            break

        filtros_exactos = extraer_filtros_exactos_query(query)
        memoria_filtros = valores_observados_existen(filtros_exactos)
        if not memoria_filtros.get("ok"):
            faltantes = memoria_filtros.get("faltantes") or []
            add_reasoning_step(
                "explorador_filtro_no_observado",
                "No ejecuto filtro exacto con valor no observado.",
                json.dumps(faltantes, ensure_ascii=False, default=str)
            )
            add_reasoning_step(
                "consulta_sin_datos",
                "Antes de filtrar hay que consultar valores reales de esa columna.",
                "Corregir eje: usar GROUP BY de la columna, nulos/no nulos o presencia por TiposDeMovimiento."
            )
            break
        add_reasoning_step(
            "explorador_query",
            f"Ejecutando: {shorten_text(query, 100)}",
            decision.get("que_espero_encontrar", "")
        )

        resultado_sql = ejecutar_query_segura(query)
        ultima_query_sql = query
        ultimo_resultado_sql = resultado_sql
        resultado_txt = formatear_resultado_query(resultado_sql)

        resumen = f"ok={resultado_sql.get('ok')} filas={resultado_sql.get('total',0)}"
        if not resultado_sql.get("ok"):
            resumen = f"ERROR: {resultado_sql.get('error','')}"

        add_reasoning_step(
            "explorador_resultado",
            f"Resultado: {resumen}",
            shorten_text(resultado_txt, 200)
        )

        if resultado_sql.get("ok"):
            guardar_valores_observados_desde_resultado(query, resultado_sql)
            tipos_capturados = capturar_desglose_tipos_movimiento(query, resultado_sql, periodo_objetivo_ciclo)
            if tipos_capturados:
                add_reasoning_step(
                    "explorador_periodo_confirmado",
                    f"Desglose de tipos de movimiento confirmado para {periodo_objetivo_ciclo.get('periodo_label')}.",
                    f"Tipos: {', '.join(tipos_capturados[:8])}. {progreso_barrido_periodos(contexto_temporal_ciclo)}"
                )
                cerebro["fase2_avance"] = True
        resultado_signature = firma_resultado_sql(resultado_sql)
        resultado_repetido = bool(historial_queries and historial_queries[-1].get("resultado_signature") == resultado_signature)
        historial_queries.append({
            "query": query,
            "query_norm": query_norm,
            "resultado_txt": resultado_txt,
            "resultado_signature": resultado_signature,
            "resumen": resumen,
        })

        if resultado_repetido and resultado_sql.get("ok") and (resultado_sql.get("rows") or []):
            add_reasoning_step(
                "explorador_resultado_repetido",
                "Dos pasos consecutivos devolvieron el mismo resultado; cierro observacion exploratoria.",
                shorten_text(resultado_txt, 220)
            )
            verdades_capturadas = intentar_capturar_verdad_periodo_de_resultado(
                query, resultado_sql, periodo_objetivo_ciclo
            )
            if verdades_capturadas:
                add_reasoning_step(
                    "explorador_periodo_confirmado",
                    f"El modelo no concluyo, pero ya tenia el dato limpio de {periodo_objetivo_ciclo.get('periodo_label')}; lo guarde igual.",
                    " | ".join(f"{m}={v}" for m, v in verdades_capturadas)
                )
                cerebro["fase2_avance"] = True
                return cerebro
            obs_persistida = guardar_observacion_exploratoria_si_corresponde(
                query,
                resultado_sql,
                decision or ultima_decision_modelo
            )
            observacion_exploratoria_cerrada = bool(obs_persistida.get("ok"))
            if observacion_exploratoria_cerrada:
                cerebro["fase2_avance"] = True
            break

        if resultado_sql.get("ok") and (not (resultado_sql.get("rows") or []) or resultado_sql_sin_datos_utiles(resultado_sql)):
            registrar_consulta_sin_datos(query, filtros_exactos, "sin_resultados")
            add_reasoning_step(
                "consulta_sin_datos",
                "La query no devolvio filas; aprendi que ese cruce no existe.",
                "No genero conclusion operativa. Proximo paso sugerido: consultar valores reales y corregir el eje."
            )
            cerebro["fase2_avance"] = True
            break
        # Si el último paso y no concluyó, forzar conclusión
        if paso == MAX_PASOS - 1 and not resultado_final:
            add_reasoning_step(
                "explorador_forzar_conclusion",
                "Alcanzó el límite de pasos. Forzando conclusión con lo explorado.",
                None
            )
            prompt_final = prompt_explorador_siguiente(
                query, resultado_txt, historial_queries, conocimientos_completos, 0, relaciones_interesantes
            )
            m2 = llamar_ollama(prompt_final, timeout=FASE2_OLLAMA_TIMEOUT_SECONDS, num_predict=FASE2_OLLAMA_NUM_PREDICT)
            if m2.get("ok"):
                parse_final = extraer_json_modelo(m2.get("texto"))
                if parse_final.get("ok"):
                    resultado_final = parse_final.get("data")
                else:
                    add_reasoning_step(
                        "explorador_error_parse",
                        "Conclusion forzada sin JSON valido; no guardo conocimiento.",
                        f"{parse_final.get('error')} | raw: {shorten_text(parse_final.get('raw'), 220)}"
                    )

    # Persistir lo que encontró
    if resultado_final:
        conf = resultado_final.get("confianza") or 0.6
        conclusion = primer_texto(
            resultado_final.get("conclusion"),
            resultado_final.get("conclusion_principal"),
            resultado_final.get("conclusion_autonoma"),
            resultado_final.get("interpretacion"),
            resultado_final.get("resumen"),
        ) or ""
        patrones = resultado_final.get("patrones") or resultado_final.get("patrones_detectados") or []
        anomalias = resultado_final.get("anomalias") or []
        hipotesis = primer_texto(
            resultado_final.get("hipotesis_nueva"),
            resultado_final.get("hipotesis_refinada"),
            resultado_final.get("hipotesis"),
        ) or ""
        regla = primer_texto(
            resultado_final.get("regla_aprendida"),
            resultado_final.get("regla_aprendible"),
            resultado_final.get("regla"),
        ) or ""
        pregunta = resultado_final.get("pregunta_para_diego") or ""

        add_reasoning_step(
            "explorador_conclusion_final",
            f"Conclusión: {shorten_text(conclusion, 150)}",
            f"Confianza: {conf} | Queries ejecutadas: {len(historial_queries)}"
        )

        periodo_metrica = resultado_final.get("periodo_metrica")
        periodo_valor = resultado_final.get("periodo_valor")
        if periodo_metrica and periodo_valor is not None and periodo_objetivo_ciclo:
            verdad_persistida = upsert_verdad_periodo(
                periodo_objetivo_ciclo.get("periodo_inicio"),
                periodo_objetivo_ciclo.get("periodo_fin"),
                resultado_final.get("periodo_label") or periodo_objetivo_ciclo.get("periodo_label"),
                periodo_metrica,
                periodo_valor,
                confianza_dato=conf,
                fuente_query=(historial_queries[-1] if historial_queries else {}).get("query"),
                origen="explorador_fase2",
            )
            if verdad_persistida.get("ok"):
                add_reasoning_step(
                    "explorador_periodo_confirmado",
                    f"Verdad de periodo guardada: {periodo_objetivo_ciclo.get('periodo_label')} | {periodo_metrica} = {periodo_valor}",
                    f"Confianza temporal: {verdad_persistida.get('confianza_temporal')}. {progreso_barrido_periodos(contexto_temporal_ciclo)}"
                )
            else:
                add_reasoning_step(
                    "explorador_periodo_error",
                    "No pude guardar la verdad de periodo en la tabla editable.",
                    verdad_persistida.get("error") or verdad_persistida.get("skipped")
                )
        elif periodo_objetivo_ciclo and historial_queries:
            ultima = historial_queries[-1]
            sin_filas = bool(re.search(r"\bfilas=0\b", str(ultima.get("resumen") or "")))
            if sin_filas and query_coincide_con_periodo_objetivo(ultima.get("query"), periodo_objetivo_ciclo):
                verdad_vacia = upsert_verdad_periodo(
                    periodo_objetivo_ciclo.get("periodo_inicio"),
                    periodo_objetivo_ciclo.get("periodo_fin"),
                    periodo_objetivo_ciclo.get("periodo_label"),
                    "movimientos",
                    0,
                    confianza_dato=conf,
                    fuente_query=ultima.get("query"),
                    origen="explorador_fase2_sin_datos",
                )
                if verdad_vacia.get("ok"):
                    add_reasoning_step(
                        "explorador_periodo_confirmado",
                        f"Periodo sin datos confirmado: {periodo_objetivo_ciclo.get('periodo_label')} tuvo 0 movimientos. No vuelvo a perseguirlo.",
                        f"Query: {shorten_text(ultima.get('query'), 150)}. {progreso_barrido_periodos(contexto_temporal_ciclo)}"
                    )

        if patrones:
            add_reasoning_step("explorador_patrones", f"{len(patrones)} patrones detectados.", " | ".join(str(p) for p in patrones[:3]))
        if anomalias:
            add_reasoning_step("explorador_anomalias", f"{len(anomalias)} anomalías.", " | ".join(str(a) for a in anomalias[:3]))
        if hipotesis:
            evidencia_hipotesis = {
                "historial_queries": len(historial_queries),
                "ultima_query": (historial_queries[-1] if historial_queries else {}).get("query"),
                "ultimo_resumen": (historial_queries[-1] if historial_queries else {}).get("resumen"),
                "ultimo_resultado": shorten_multiline((historial_queries[-1] if historial_queries else {}).get("resultado_txt"), 900),
            }
            hip_persistida = persistir_hipotesis_autonoma(resultado_final, evidencia_hipotesis, origen="explorador_fase2")
            if hip_persistida.get("ok"):
                detalle_hip = f"Hipotesis nueva guardada #{hip_persistida.get('id')}"
                if pregunta:
                    detalle_hip += f" | Pregunta: {pregunta}"
            else:
                detalle_hip = f"No pude persistir hipotesis: {hip_persistida.get('error') or hip_persistida.get('skipped')}"
                if pregunta:
                    detalle_hip += f" | Pregunta: {pregunta}"
            add_reasoning_step("explorador_hipotesis", f"Hipotesis nueva: {shorten_text(hipotesis, 100)}", detalle_hip)

        if not resultado_fase2_tiene_conocimiento(resultado_final):
            add_reasoning_step(
                "explorador_sin_conclusion",
                "El modelo pidio seguir consultando; intento guardar observacion exploratoria si hay evidencia util.",
                shorten_text(resultado_final.get("razonamiento"), 180)
            )
            obs_persistida = guardar_observacion_exploratoria_si_corresponde(
                ultima_query_sql,
                ultimo_resultado_sql,
                resultado_final or ultima_decision_modelo
            )
            cerebro = get_ia_cerebro() or cerebro
            cerebro["fase2_avance"] = bool(obs_persistida.get("ok"))
            return cerebro

        ultima_evidencia = historial_queries[-1] if historial_queries else {}
        evidencia_final = {
            "historial_queries": len(historial_queries),
            "ultima_query": ultima_evidencia.get("query"),
            "ultimo_resumen": ultima_evidencia.get("resumen"),
            "ultimo_resultado": shorten_multiline(ultima_evidencia.get("resultado_txt"), 1200),
        }

        # Guardar en BD
        persistencia = persistir_conclusion_fase2(resultado_final, evidencia_final)
        if persistencia.get("ok"):
            add_reasoning_step("explorador_persistida", "Conclusión guardada en BD.", None)
        else:
            add_reasoning_step("explorador_persistencia_error", f"No pude persistir: {persistencia.get('error')}", None)

        # Memoria en proceso
        conclusion_memoria = {
            "fecha": datetime.now().isoformat(),
            "hora": datetime.now().strftime("%H:%M:%S"),
            "columna": "exploracion_autonoma",
            "estado": "explorador_autonomo",
            "confianza": conf,
            "conclusion": conclusion,
            "titulo": f"Exploración autónoma — {len(historial_queries)} queries",
            "patrones_detectados": patrones,
            "anomalias": anomalias,
            "hipotesis_refinada": hipotesis,
            "incertidumbres": anomalias,
            "siguiente_analisis": resultado_final.get("siguiente_analisis") or "continuar exploración",
            "regla_aprendible": regla,
            "evidencia": evidencia_final,
        }
        conclusion_memoria["telegram"] = notificar_telegram_conclusion(
            conclusion_memoria,
            origen="exploracion_autonoma"
        )
        IA_AUTONOMOUS_CONCLUSIONS.insert(0, conclusion_memoria)
        del IA_AUTONOMOUS_CONCLUSIONS[40:]
        cerebro["fase2_avance"] = bool(persistencia.get("ok"))
    else:
        add_reasoning_step("explorador_sin_conclusion", "No llego a una conclusion en esta sesion; intento guardar observacion exploratoria si hay evidencia util.", None)
        if not observacion_exploratoria_cerrada:
            obs_persistida = guardar_observacion_exploratoria_si_corresponde(
                ultima_query_sql,
                ultimo_resultado_sql,
                ultima_decision_modelo
            )
            if obs_persistida.get("ok"):
                cerebro["fase2_avance"] = True

    fase2_avance = bool(cerebro.get("fase2_avance"))
    cerebro = get_ia_cerebro() or cerebro
    cerebro["fase2_avance"] = fase2_avance
    return cerebro



def estado_autonomia_ia():
    return {
        "activa": AUTO_IA_ENABLED,
        "intervalo_segundos": AUTO_IA_INTERVAL_SECONDS,
        "delay_cadena_segundos": AUTO_IA_CHAIN_DELAY_SECONDS,
        "max_ciclos_cadena": AUTO_IA_MAX_CHAIN_CYCLES,
        "modelo": OLLAMA_MODEL,
        "ultima_corrida": AUTO_IA_LAST_RUN,
        "en_ejecucion": AUTO_IA_IN_PROGRESS,
        "inicio_corrida_actual": AUTO_IA_CURRENT_START,
        "ultimo_fin": AUTO_IA_LAST_FINISH,
        "ultimo_resultado": AUTO_IA_LAST_RESULT,
        "ultimo_error": AUTO_IA_LAST_ERROR,
        "telegram": {
            "configurado": telegram_configurado(),
            "chat_id_configurado": bool(TELEGRAM_CHAT_ID),
            "aviso_inicio": TELEGRAM_STARTUP_NOTIFY,
            "razonamiento_en_vivo": TELEGRAM_NOTIFY_REASONING,
            "modo_razonamiento": TELEGRAM_REASONING_MODE,
            "baja_prioridad": TELEGRAM_NOTIFY_LOW_INTEREST,
        },
    }

def marcar_inicio_ciclo_ia(modo="manual"):
    global AUTO_IA_LAST_RUN, AUTO_IA_LAST_ERROR, AUTO_IA_IN_PROGRESS
    global AUTO_IA_CURRENT_START, AUTO_IA_LAST_RESULT
    ahora = datetime.now().isoformat()
    AUTO_IA_LAST_RUN = ahora
    AUTO_IA_CURRENT_START = ahora
    AUTO_IA_IN_PROGRESS = True
    AUTO_IA_LAST_ERROR = None
    AUTO_IA_LAST_RESULT = {
        "modo": modo,
        "estado": "en_ejecucion",
        "inicio": ahora,
    }
    return ahora


def marcar_fin_ciclo_ia(modo="manual", resultado=None, error=None):
    global AUTO_IA_LAST_ERROR, AUTO_IA_IN_PROGRESS, AUTO_IA_CURRENT_START
    global AUTO_IA_LAST_FINISH, AUTO_IA_LAST_RESULT
    ahora = datetime.now().isoformat()
    AUTO_IA_LAST_FINISH = ahora
    AUTO_IA_IN_PROGRESS = False
    AUTO_IA_CURRENT_START = None
    if error:
        AUTO_IA_LAST_ERROR = str(error)
        AUTO_IA_LAST_RESULT = {
            "modo": modo,
            "estado": "error",
            "fin": ahora,
            "error": str(error),
        }
    else:
        ciclo = (resultado or {}).get("ciclo") or {}
        AUTO_IA_LAST_RESULT = {
            "modo": modo,
            "estado": "ok" if resultado else "sin_resultado",
            "fin": ahora,
            "fase": ciclo.get("fase"),
            "avanzo_autonomamente": ciclo.get("avanzo_autonomamente"),
            "sin_pendientes": ciclo.get("sin_pendientes"),
        }


def ejecutar_ciclo_ia(modo="manual"):
    marcar_inicio_ciclo_ia(modo)
    try:
        resultado = _ejecutar_ciclo_ia_impl(modo=modo)
        marcar_fin_ciclo_ia(modo, resultado=resultado)
        if isinstance(resultado, dict):
            resultado["autonomia"] = estado_autonomia_ia()
        return resultado
    except Exception as e:
        marcar_fin_ciclo_ia(modo, error=e)
        raise

def _ejecutar_ciclo_ia_impl(modo="manual"):
    cerebro = get_ia_cerebro()
    if not cerebro:
        return None

    metricas = cerebro.get("metricas") or {}
    proxima = cerebro.get("proxima_accion")

    # Resumen principal: estado real actual de la cartografía.
    # Las columnas base de fase 1 quedan solo como métrica secundaria.
    _prot = cerebro.get("protagonistas_operativos") or {}
    _bio = cerebro.get("biografias_protagonistas") or {}
    n_protagonistas = len(_prot.get("nivel1") or [])
    n_biografias = len(_bio.get("proveedores") or []) + len(_bio.get("materiales") or [])
    detalle_obs = (
        f"Cartografía activa: {metricas.get('columnas_perfiladas', 0)}/{metricas.get('columnas_esquema_total', 0)} columnas perfiladas; "
        f"{metricas.get('tipos_movimiento_perfilados', 0)} tipos; "
        f"{metricas.get('relaciones_estructurales', 0)} relaciones"
    )
    if n_protagonistas or n_biografias:
        detalle_obs += f"; {n_protagonistas} protagonistas; {n_biografias} biografías"
    detalle_obs += (
        f". Secundario: {metricas.get('columnas_comprendidas', 0)}/{metricas.get('columnas_exploradas', 0)} columnas base; "
        f"{metricas.get('hipotesis_activas', 0)} hipótesis activas."
    )
    add_reasoning_step(
        "observacion",
        "Revisé el estado del cerebro IA.",
        detalle_obs
    )

    diccionario = guardar_diccionario_columnas_si_corresponde()
    if diccionario.get("ok"):
        add_reasoning_step(
            "diccionario_columnas",
            "Actualice el diccionario operativo de columnas.",
            f"Columnas procesadas: {diccionario.get('columnas')}"
        )
    elif diccionario.get("error"):
        add_reasoning_step(
            "diccionario_columnas_error",
            "No pude actualizar diccionario de columnas.",
            diccionario.get("error")
        )

    cartografia = guardar_cartografia_base_si_corresponde()
    if cartografia.get("ok"):
        add_reasoning_step(
            "cartografia_base",
            "Actualice cartografia deterministica de tablas, columnas y tipos de movimiento.",
            f"Tablas: {cartografia.get('tablas')} | columnas: {cartografia.get('columnas')} | tipos: {cartografia.get('tipos_movimiento')}"
        )
    elif cartografia.get("error"):
        add_reasoning_step("cartografia_base_error", "No pude actualizar cartografia deterministica.", cartografia.get("error"))

    relaciones = guardar_relaciones_columnas_si_corresponde()
    if relaciones.get("ok"):
        add_reasoning_step(
            "relaciones_columnas",
            "Actualice relaciones estructurales entre columnas.",
            f"Relaciones: {relaciones.get('relaciones')} | eje principal: TiposDeMovimiento"
        )
    elif relaciones.get("error"):
        add_reasoning_step(
            "relaciones_columnas_error",
            "No pude actualizar relaciones estructurales.",
            relaciones.get("error")
        )

    observacion_op = guardar_observacion_operativa_si_corresponde()
    if observacion_op.get("ok"):
        add_reasoning_step(
            "observador_operativo",
            "Guarde pulso operativo deterministico en BD.",
            shorten_text(observacion_op.get("conclusion"), 220)
        )
    elif observacion_op.get("error"):
        add_reasoning_step(
            "observador_operativo_error",
            "No pude guardar pulso operativo.",
            observacion_op.get("error")
        )

    if not proxima:
        # Fase 2: análisis operativo cuando el esquema ya está comprendido
        cerebro = ejecutar_fase2_ia(cerebro, modo_drain=(modo == "autonomo_drain"))
        fase2_avance = bool(cerebro.get("fase2_avance"))
        detener_cadena = bool(cerebro.get("fase2_detener_cadena"))
        cerebro["ciclo"] = {
            "modo": modo,
            "avanzo_autonomamente": fase2_avance,
            "fase": "2_operativa",
            "detener_cadena": detener_cadena,
        }
        cerebro["razonamiento_reciente"] = IA_REASONING_LOG[:30]
        return cerebro

    columna = proxima.get("columna")
    add_reasoning_step(
        "seleccion",
        f"Elegí {columna} como siguiente verificación.",
        proxima.get("pregunta")
    )

    evidencia = get_column_evidence(columna)
    avanzo_autonomamente = False
    if evidencia and not evidencia.get("error"):
        frecuentes = evidencia.get("frecuentes") or []
        ejemplos = ", ".join([f"{x.get('valor')} ({x.get('cantidad')})" for x in frecuentes[:5]])
        add_reasoning_step(
            "evidencia",
            f"Exploré valores reales de la columna {columna}.",
            f"Total: {evidencia.get('total')}; vacíos: {evidencia.get('vacios')}; distintos: {evidencia.get('distintos')}; frecuentes: {ejemplos or 'sin ejemplos'}."
        )
        contexto = evidencia.get("resumen_por_movimiento") or []
        if contexto:
            contexto_txt = "; ".join([
                f"{x.get('tipo_movimiento')}: {x.get('pct_vacio')}% vacio"
                for x in contexto[:5]
            ])
            add_reasoning_step(
                "cruce_contextual",
                f"Crucé {columna} con TiposDeMovimiento.",
                contexto_txt
            )
        add_reasoning_step(
            "modelo_local",
            f"Le pedi a Ollama ({OLLAMA_MODEL}) que interprete la evidencia.",
            "El modelo trabaja solo sobre evidencia SQL y conocimiento confirmado."
        )
        prompt = construir_prompt_ollama(
            proxima,
            evidencia,
            cerebro.get("conocimiento_confirmado") or []
        )
        modelo = llamar_ollama(prompt)
        if modelo.get("ok"):
            interpretacion = normalizar_interpretacion_ia(parse_model_json(modelo.get("texto")))
            if interpretacion:
                conclusion = guardar_conclusion_autonoma(
                    columna,
                    interpretacion,
                    evidencia,
                    proxima.get("id")
                )
                avanzo_autonomamente = bool(conclusion.get("avance_autonomo"))
                add_reasoning_step(
                    "interpretacion_ia",
                    interpretacion.get("interpretacion") or "El modelo genero una interpretacion.",
                    interpretacion.get("hipotesis_refinada")
                )
                if conclusion.get("conclusion"):
                    add_reasoning_step(
                        "conclusion_autonoma",
                        "La IA saco una conclusion provisional propia.",
                        conclusion.get("conclusion")
                    )
                if conclusion.get("persistencia", {}).get("ok"):
                    add_reasoning_step(
                        "persistencia_lab",
                        "Guarde la conclusion autonoma en crowdbot_lab.",
                        "Tabla: ia_conclusiones_autonomas"
                    )
                else:
                    add_reasoning_step(
                        "persistencia_lab_error",
                        "No pude persistir la conclusion autonoma en crowdbot_lab.",
                        (conclusion.get("persistencia") or {}).get("error")
                    )
                if avanzo_autonomamente:
                    add_reasoning_step(
                        "avance_autonomo",
                        "La IA marco esta hipotesis como autonoma_provisional y seguira con la siguiente.",
                        f"Confianza: {conclusion.get('confianza')}; umbral: {AUTO_IA_MIN_CONFIDENCE}"
                    )
                preguntas = interpretacion.get("preguntas_mejores") or []
                if preguntas:
                    add_reasoning_step(
                        "preguntas_mejores",
                        "El modelo propuso preguntas mas especificas.",
                        " | ".join([str(x) for x in preguntas[:3]])
                    )
                if interpretacion.get("regla_aprendible"):
                    add_reasoning_step(
                        "regla_aprendible",
                        "El modelo propuso una regla operativa tentativa.",
                        interpretacion.get("regla_aprendible")
                    )
                incertidumbres = interpretacion.get("incertidumbres") or []
                if incertidumbres:
                    add_reasoning_step(
                        "incertidumbres",
                        "La IA marco limites de su conclusion.",
                        " | ".join([str(x) for x in incertidumbres[:3]])
                    )
                if interpretacion.get("siguiente_analisis"):
                    add_reasoning_step(
                        "siguiente_analisis",
                        "La IA propuso como profundizar.",
                        interpretacion.get("siguiente_analisis")
                    )
            else:
                add_reasoning_step(
                    "interpretacion_ia",
                    "Ollama respondio, pero no devolvio JSON estructurado.",
                    shorten_text(modelo.get("texto"), 500)
                )
        else:
            add_reasoning_step(
                "modelo_local_error",
                f"No pude consultar Ollama ({OLLAMA_MODEL}).",
                modelo.get("error")
            )
    elif evidencia:
        add_reasoning_step(
            "bloqueo",
            f"No pude juntar evidencia automática para {columna}.",
            evidencia.get("error")
        )

    if avanzo_autonomamente:
        cerebro = get_ia_cerebro() or cerebro
        cerebro["ciclo"] = {
            "modo": modo,
            "avanzo_autonomamente": True,
            "columna": columna,
        }
        cerebro["razonamiento_reciente"] = IA_REASONING_LOG[:30]
        return cerebro

    add_reasoning_step(
        "pregunta_usuario",
        "No puedo cerrar esta hipótesis solo con frecuencia de valores.",
        proxima.get("pregunta")
    )

    cerebro = get_ia_cerebro() or cerebro
    cerebro["ciclo"] = {
        "modo": modo,
        "avanzo_autonomamente": False,
        "columna": columna,
    }
    cerebro["razonamiento_reciente"] = IA_REASONING_LOG[:30]
    return cerebro


def ciclo_autonomo_worker():
    global AUTO_IA_LAST_RUN, AUTO_IA_LAST_ERROR
    time.sleep(10)
    drain_cadenas = 0
    drain_inicio = None
    modo_cadena = "autonomo"
    while AUTO_IA_ENABLED:
        if AUTO_IA_LOCK.acquire(blocking=False):
            try:
                chain_count = 0
                while AUTO_IA_ENABLED and chain_count < AUTO_IA_MAX_CHAIN_CYCLES:
                    chain_count += 1
                    AUTO_IA_LAST_RUN = datetime.now().isoformat()
                    AUTO_IA_LAST_ERROR = None
                    add_reasoning_step(
                        "autonomia",
                        "Inicio ciclo autonomo de exploracion.",
                        f"Cadena: {chain_count}/{AUTO_IA_MAX_CHAIN_CYCLES}; modo: {modo_cadena}; modelo: {OLLAMA_MODEL}"
                    )
                    resultado = ejecutar_ciclo_ia(modo=modo_cadena)
                    ciclo = (resultado or {}).get("ciclo") or {}
                    if ciclo.get("sin_pendientes"):
                        break
                    if ciclo.get("detener_cadena"):
                        add_reasoning_step(
                            "pendiente_cadena_cortada",
                            "Corto la cadena tras una pendiente estructural.",
                            "La siguiente pendiente esperara el intervalo normal."
                        )
                        break
                    if not ciclo.get("avanzo_autonomamente"):
                        break
                    add_reasoning_step(
                        "autonomia",
                        "La IA avanzo; lanzo el siguiente ciclo en breve.",
                        f"Espera: {AUTO_IA_CHAIN_DELAY_SECONDS}s"
                    )
                    time.sleep(AUTO_IA_CHAIN_DELAY_SECONDS)
            except Exception as e:
                AUTO_IA_LAST_ERROR = str(e)
                print(f"Error en ciclo autonomo IA: {e}", file=sys.stderr)
                add_reasoning_step(
                    "autonomia_error",
                    "Fallo el ciclo autonomo de exploracion.",
                    str(e)
                )
            finally:
                AUTO_IA_LOCK.release()

        # Modo drain: si queda cola de curiosidad pendiente, seguir masticando
        # fichas en tandas cortas en vez de esperar el intervalo largo.
        if (AUTO_IA_ENABLED and not AUTO_IA_IN_PROGRESS
                and hay_cola_curiosidad_pendiente_disponible()):
            if drain_inicio is None:
                drain_inicio = time.time()
            drain_cadenas += 1
            minutos_drain = (time.time() - drain_inicio) / 60.0
            if (drain_cadenas > AUTO_COLA_DRAIN_MAX_CADENAS
                    or minutos_drain >= AUTO_COLA_DRAIN_MAX_MINUTES):
                add_reasoning_step(
                    "cola_curiosidad_drain_pausada",
                    "Drain de cola pausado por límite de seguridad; vuelvo al intervalo normal.",
                    (f"cadenas={drain_cadenas - 1}/{AUTO_COLA_DRAIN_MAX_CADENAS} "
                     f"minutos={minutos_drain:.1f}/{AUTO_COLA_DRAIN_MAX_MINUTES}")
                )
                drain_cadenas = 0
                drain_inicio = None
                modo_cadena = "autonomo"
                time.sleep(AUTO_IA_INTERVAL_SECONDS)
                continue
            add_reasoning_step(
                "cola_curiosidad_drain_continua",
                "Queda cola de curiosidad pendiente; lanzo otra cadena en breve.",
                (f"cadena_drain={drain_cadenas}/{AUTO_COLA_DRAIN_MAX_CADENAS} "
                 f"espera={AUTO_COLA_DRAIN_SLEEP_SECONDS}s")
            )
            # La próxima cadena arranca directo por la cola (saltea microtareas
            # salvo que la cola se vacíe en el medio)
            modo_cadena = "autonomo_drain"
            time.sleep(AUTO_COLA_DRAIN_SLEEP_SECONDS)
            continue

        # Cola vacía (o autonomía ocupada): si veníamos drenando, cerrar el drain
        if drain_cadenas:
            add_reasoning_step(
                "cola_curiosidad_drain_completa",
                "Cola de curiosidad drenada; vuelvo al intervalo normal.",
                f"cadenas_drain={drain_cadenas}"
            )
        drain_cadenas = 0
        drain_inicio = None
        modo_cadena = "autonomo"
        time.sleep(AUTO_IA_INTERVAL_SECONDS)


def iniciar_autonomia_ia():
    if not AUTO_IA_ENABLED:
        return
    t = threading.Thread(target=ciclo_autonomo_worker, daemon=True)
    t.start()


def parse_json_obj(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def inferir_columnas_desde_observacion(obs):
    """Devuelve [(columna, es_exacta)]. es_exacta=True solo cuando obs['columna']
    es justamente esa columna (campo estructurado); el resto son apariciones del
    nombre en el texto libre (conclusion/hipotesis/evidencia), que son una senal
    debil y no deben pisar la conclusion principal de una columna que no es la suya."""
    columna_propia = obs.get("columna") if obs.get("columna") != "mapa_estructural" else None
    texto = " ".join(str(x or "") for x in [
        obs.get("conclusion"), obs.get("hipotesis_refinada"), obs.get("regla_aprendible")
    ])
    evidencia = obs.get("evidencia") or {}
    texto += " " + " ".join(str(x or "") for x in [
        evidencia.get("tema") if isinstance(evidencia, dict) else "",
        evidencia.get("tarea") if isinstance(evidencia, dict) else "",
    ])
    resultado = []
    if columna_propia:
        resultado.append((columna_propia, True))
    for col in ["Precio", "Proveedor", "Materiales", "Description", "Operador", "Maquina", "kilos", "kilosNetos", "Descuento", "Descuento2", "IF", "Cat10", "Cat11", "Cat12"]:
        if col == columna_propia:
            continue
        if re.search(rf"\b{re.escape(col)}\b", texto, flags=re.IGNORECASE):
            resultado.append((col, False))
    vistos = set()
    salida = []
    for col, exacta in resultado:
        if col in vistos:
            continue
        vistos.add(col)
        salida.append((col, exacta))
    return salida


def dedupe_por_texto(items, campos=("conclusion", "tema", "texto"), limite=20):
    salida = []
    vistos = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        texto = primer_texto(*[item.get(c) for c in campos]) or str(item.get("id") or item.get("firma") or "")
        clave = " ".join(str(texto).lower().split())[:260]
        if not clave or clave in vistos:
            continue
        vistos.add(clave)
        salida.append(item)
        if len(salida) >= limite:
            break
    return salida


def sumar_tiempo(dt, **kwargs):
    if not dt:
        return None
    try:
        if isinstance(dt, str):
            base = datetime.fromisoformat(dt.replace("Z", ""))
        else:
            base = dt
        return (base + timedelta(**kwargs)).isoformat(sep=" ")
    except Exception:
        return None


def construir_modelo_estructural_inferido(metricas, contexto_temporal, diccionario_columnas, relaciones_interesantes,
                                          observaciones_exploratorias, mapa_estructural, perfiles_columnas,
                                          perfiles_tipos, pendientes_estructurales):
    metricas = metricas or {}
    perfiles_columnas = perfiles_columnas or []
    perfiles_tipos = perfiles_tipos or []
    relaciones_interesantes = relaciones_interesantes or []
    observaciones_limpias = dedupe_por_texto(observaciones_exploratorias or [], limite=30)

    dicc_por_col = {d.get("columna"): d for d in (diccionario_columnas or []) if isinstance(d, dict) and d.get("columna")}
    obs_por_col = {}
    for obs in observaciones_limpias:
        for col, es_exacta in inferir_columnas_desde_observacion(obs):
            obs_por_col.setdefault(col, []).append((obs, es_exacta))
    for col in list(obs_por_col.keys()):
        # las observaciones que son exactamente sobre esta columna van primero,
        # asi nunca se muestra como "conclusion" de una columna un texto que
        # solo la menciona de paso (ej. Proveedor mencionando "kilos").
        obs_por_col[col] = [o for o, exacta in sorted(obs_por_col[col], key=lambda t: t[1], reverse=True)]

    rel_por_col = {}
    for rel in relaciones_interesantes:
        for col in [rel.get("columna_a"), rel.get("columna_b")]:
            if col:
                rel_por_col.setdefault(col, []).append(rel)

    tipos_por_col = {}
    for tm in perfiles_tipos:
        ev = tm.get("evidencia") or parse_json_obj(tm.get("evidencia_json"), {}) or {}
        columnas = ev.get("columnas") if isinstance(ev, dict) else {}
        if not isinstance(columnas, dict):
            continue
        for col, info in columnas.items():
            if not isinstance(info, dict):
                continue
            pct = info.get("pct_cargado") or 0
            try:
                pct_num = float(pct)
            except Exception:
                pct_num = 0
            if pct_num > 0:
                tipos_por_col.setdefault(col, []).append({
                    "tipo_movimiento": tm.get("tipo_movimiento"),
                    "registros": tm.get("registros"),
                    "pct_cargado": pct_num,
                    "cargados": info.get("cargados"),
                })
    for col in list(tipos_por_col.keys()):
        tipos_por_col[col] = sorted(tipos_por_col[col], key=lambda x: (x.get("pct_cargado") or 0, x.get("registros") or 0), reverse=True)[:5]

    def prioridad_col(p):
        col = p.get("columna")
        obs = 1 if obs_por_col.get(col) else 0
        rel_score = max([float(r.get("interes_score") or 0) for r in rel_por_col.get(col, [])] or [0])
        dicc = 1 if (dicc_por_col.get(col) or {}).get("significado_inferido") else 0
        return (obs, rel_score, dicc, float(p.get("pct_cargado") or 0))

    inferencias_por_columna = []
    for perfil in sorted(perfiles_columnas, key=prioridad_col, reverse=True):
        col = perfil.get("columna")
        ev = perfil.get("evidencia") or parse_json_obj(perfil.get("evidencia_json"), {}) or {}
        dicc = dicc_por_col.get(col) or {}
        obs_col = obs_por_col.get(col, [])
        rel_col = sorted(rel_por_col.get(col, []), key=lambda r: float(r.get("interes_score") or 0), reverse=True)[:5]
        conclusion = primer_texto(
            obs_col[0].get("conclusion") if obs_col else None,
            dicc.get("significado_inferido"),
            rel_col[0].get("descripcion") if rel_col else None,
        )
        if not conclusion and not obs_col and not rel_col and not dicc.get("significado_inferido"):
            continue
        inferencias_por_columna.append({
            "columna": col,
            "tipo_dato": perfil.get("tipo_dato") or dicc.get("tipo_dato"),
            "significado_inferido": dicc.get("significado_inferido") or conclusion,
            "confianza": next((v for v in [dicc.get("confianza"), obs_col[0].get("confianza") if obs_col else None, rel_col[0].get("confianza") if rel_col else None] if v is not None and v != ""), None),
            "estado": (obs_col[0].get("estado") if obs_col else None) or dicc.get("estado") or "inferido",
            "evidencia": {
                "porcentaje_cargado": perfil.get("pct_cargado"),
                "distinct_count": perfil.get("distinct_count"),
                "valores_frecuentes": (ev.get("top_valores") if isinstance(ev, dict) else []) or [],
                "ejemplos": (ev.get("ejemplos") if isinstance(ev, dict) else []) or [],
                "aparece_mas_en_tipos": tipos_por_col.get(col, []),
                "co_presencias": [{
                    "columna_a": r.get("columna_a"),
                    "columna_b": r.get("columna_b"),
                    "descripcion": r.get("descripcion"),
                    "interes_score": r.get("interes_score"),
                    "confianza": r.get("confianza"),
                } for r in rel_col],
                "observaciones": [{
                    "id": o.get("id"),
                    "conclusion": o.get("conclusion"),
                    "confianza": o.get("confianza"),
                    "creada_en": o.get("creada_en") or o.get("fecha"),
                    "evidencia": o.get("evidencia"),
                } for o in obs_col[:3]],
            },
            "conclusion": conclusion,
            "actualizado_en": perfil.get("actualizado_en") or dicc.get("actualizado_en") or (obs_col[0].get("creada_en") if obs_col else None),
        })

    inferencias_por_tipo = []
    for tm in perfiles_tipos[:30]:
        ev = tm.get("evidencia") or parse_json_obj(tm.get("evidencia_json"), {}) or {}
        cols = ev.get("columnas") if isinstance(ev, dict) else {}
        relevantes = []
        if isinstance(cols, dict):
            for col, info in cols.items():
                if isinstance(info, dict):
                    pct = info.get("pct_cargado") or 0
                    try:
                        pct_num = float(pct)
                    except Exception:
                        pct_num = 0
                    if pct_num >= 40:
                        relevantes.append({"columna": col, "pct_cargado": pct_num, "cargados": info.get("cargados")})
            relevantes = sorted(relevantes, key=lambda x: x.get("pct_cargado") or 0, reverse=True)[:8]
        lectura = f"{tm.get('tipo_movimiento')} tiene {tm.get('registros')} registros"
        if relevantes:
            lectura += "; columnas mas cargadas: " + ", ".join(f"{x.get('columna')} {x.get('pct_cargado')}%" for x in relevantes[:4])
        inferencias_por_tipo.append({
            "tipo_movimiento": tm.get("tipo_movimiento"),
            "registros": tm.get("registros"),
            "kilos": tm.get("suma_kilos"),
            "columnas_relevantes": relevantes,
            "lectura": lectura + ".",
            "evidencia": ev,
            "actualizado_en": tm.get("actualizado_en"),
        })

    pendientes = []
    for p in pendientes_estructurales or []:
        ultimo_resultado = p.get("ultimo_resultado")
        ultimo_intento = p.get("ultimo_intento")
        cooldown = sumar_tiempo(ultimo_intento, minutes=30) if ultimo_resultado == "query_rechazada" else sumar_tiempo(ultimo_intento, hours=AUTO_TEMA_COOLDOWN_HOURS)
        pendientes.append({
            "firma": p.get("firma"),
            "tema": p.get("tema"),
            "tarea": p.get("tarea"),
            "estado": p.get("estado") or "pendiente",
            "prioridad": p.get("prioridad"),
            "ultimo_intento": ultimo_intento,
            "ultimo_resultado": ultimo_resultado,
            "cooldown_hasta": cooldown,
            "actualizado_en": p.get("actualizado_en"),
        })

    return {
        "generado_en": datetime.now().isoformat(),
        "contexto_temporal": contexto_temporal,
        "resumen": {
            "columnas_perfiladas": metricas.get("columnas_perfiladas") or len(perfiles_columnas),
            "tipos_movimiento_perfilados": metricas.get("tipos_movimiento_perfilados") or len(perfiles_tipos),
            "observaciones_exploratorias": metricas.get("observaciones_exploratorias") or len(observaciones_limpias),
            "pendientes_abiertos": metricas.get("pendientes_estructurales") or len(pendientes),
            "relaciones_interesantes": metricas.get("relaciones_interesantes") or len(relaciones_interesantes),
        },
        "inferencias_por_columna": inferencias_por_columna,
        "inferencias_por_tipo_movimiento": inferencias_por_tipo,
        "observaciones_estructurales_recientes": [{
            "id": o.get("id"),
            "tema": ((o.get("evidencia") or {}).get("tema") if isinstance(o.get("evidencia"), dict) else None) or o.get("columna"),
            "conclusion": o.get("conclusion"),
            "confianza": o.get("confianza"),
            "estado": o.get("estado"),
            "evidencia_json": o.get("evidencia"),
            "creada_en": o.get("creada_en") or o.get("fecha"),
        } for o in observaciones_limpias[:20]],
        "pendientes_estructurales": pendientes,
        "limitaciones": [
            "El mes actual esta abierto; no tratarlo como periodo cerrado.",
            "Hoy y la semana actual son parciales mientras no cierren.",
            "Algunas inferencias son observaciones exploratorias y requieren validacion humana.",
        ],
    }

def get_ia_cerebro():
    conn = get_ia_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()

        hip_cols = get_table_columns(cur, "hipotesis_esquema")
        dudas_cols = get_table_columns(cur, "dudas_modelo")
        conoc_cols = get_table_columns(cur, "conocimiento_operativo")
        concl_cols = get_table_columns(cur, "conclusiones")
        ia_auto_cols = get_table_columns(cur, "ia_conclusiones_autonomas")
        dicc_cols = get_table_columns(cur, "ia_diccionario_columnas")
        try:
            ensure_ia_hipotesis_autonomas(cur)
        except Exception:
            pass
        hip_auto_cols = get_table_columns(cur, "ia_hipotesis_autonomas")
        perfil_cols = get_table_columns(cur, "ia_perfil_columnas")
        perfil_tipo_cols = get_table_columns(cur, "ia_perfil_tipos_movimiento")
        pendientes_estruct_cols = get_table_columns(cur, "ia_pendientes_estructurales")
        mapa_tablas_cols = get_table_columns(cur, "ia_mapa_tablas")
        rel_cols = get_table_columns(cur, "ia_relaciones_columnas")
        contexto_temporal = construir_contexto_temporal_operativo(persistir=True)
        if rel_cols:
            for col_name, col_def in [
                ("interes_score", "DECIMAL(6,2) NULL"),
                ("nivel_interes", "VARCHAR(32) NULL"),
                ("categoria", "VARCHAR(64) NULL"),
            ]:
                if col_name not in rel_cols:
                    cur.execute(f"ALTER TABLE ia_relaciones_columnas ADD COLUMN {col_name} {col_def}")
                    rel_cols.add(col_name)

        metricas = {
            "hipotesis_activas": 0,
            "hipotesis_confirmadas": 0,
            "hipotesis_descartadas": 0,
            "hipotesis_autonomas": 0,
            "hipotesis_abiertas": 0,
            "dudas_abiertas": 0,
            "conocimiento_operativo_aprendido": 0,
            "columnas_exploradas": 0,
            "columnas_comprendidas": 0,
            "progreso_columnas_pct": 0,
            "columnas_esquema_total": 0,
            "columnas_diccionario": 0,
            "columnas_con_relaciones": 0,
            "relaciones_estructurales": 0,
            "relaciones_columnas": 0,
            "relaciones_alta_confianza": 0,
            "relaciones_interesantes": 0,
            "mapa_tablas": 0,
            "columnas_perfiladas": 0,
            "tipos_movimiento_perfilados": 0,
            "pendientes_estructurales": 0,
        }

        if hip_cols:
            metricas["hipotesis_activas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM hipotesis_esquema
                WHERE vigente = 1
                  AND LOWER(COALESCE(estado, '')) NOT IN ('confirmada', 'confirmado', 'validada', 'validado', 'descartada', 'descartado', 'rechazada', 'rechazado')
            """)
            metricas["hipotesis_confirmadas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM hipotesis_esquema
                WHERE LOWER(COALESCE(estado, '')) IN ('confirmada', 'confirmado', 'validada', 'validado')
            """)
            metricas["hipotesis_descartadas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM hipotesis_esquema
                WHERE LOWER(COALESCE(estado, '')) IN ('descartada', 'descartado', 'rechazada', 'rechazado')
            """)
            metricas["hipotesis_autonomas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM hipotesis_esquema
                WHERE LOWER(COALESCE(estado, '')) IN ('autonoma_provisional')
            """)
            if "columna" in hip_cols:
                metricas["columnas_exploradas"] = scalar_count(cur, """
                    SELECT COUNT(DISTINCT columna) AS total
                    FROM hipotesis_esquema
                    WHERE columna IS NOT NULL AND columna <> ''
                """)
                metricas["columnas_comprendidas"] = scalar_count(cur, """
                    SELECT COUNT(DISTINCT columna) AS total
                    FROM hipotesis_esquema
                    WHERE columna IS NOT NULL
                      AND columna <> ''
                      AND LOWER(COALESCE(estado, '')) IN ('confirmada', 'confirmado', 'validada', 'validado', 'autonoma_provisional')
                """)
                exploradas = metricas["columnas_exploradas"] or 0
                comprendidas = metricas["columnas_comprendidas"] or 0
                if exploradas:
                    metricas["progreso_columnas_pct"] = round((comprendidas / exploradas) * 100, 1)

        hipotesis_autonomas = []
        hipotesis_abiertas = []
        if hip_auto_cols:
            metricas["hipotesis_autonomas"] += scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM ia_hipotesis_autonomas
            """)
            metricas["hipotesis_abiertas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM ia_hipotesis_autonomas
                WHERE estado IN ('abierta', 'investigando', 'requiere_contexto')
            """)
            metricas["hipotesis_activas"] += metricas["hipotesis_abiertas"]

        if dudas_cols:
            metricas["dudas_abiertas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM dudas_modelo
                WHERE estado = 'abierta'
            """)

        if conoc_cols:
            activo_filter = "WHERE activo = 1" if "activo" in conoc_cols else ""
            metricas["conocimiento_operativo_aprendido"] = scalar_count(cur, f"""
                SELECT COUNT(*) AS total
                FROM conocimiento_operativo
                {activo_filter}
            """)

        hipotesis_activas = []
        proxima_accion = None
        if hip_cols:
            pregunta_col = "pregunta_usuario" if "pregunta_usuario" in hip_cols else "NULL AS pregunta_usuario"
            cur.execute(f"""
                SELECT id, columna, hipotesis, {pregunta_col}, confianza, estado, creada_en
                FROM hipotesis_esquema
                WHERE vigente = 1
                  AND LOWER(COALESCE(estado, '')) NOT IN ('confirmada', 'confirmado', 'validada', 'validado', 'descartada', 'descartado', 'rechazada', 'rechazado')
                ORDER BY id DESC
                LIMIT 20
            """)
            hipotesis_activas = cur.fetchall()
            if hipotesis_activas:
                pendientes = sorted(hipotesis_activas, key=lambda x: x.get("id") or 0)
                proxima = pendientes[0]
                proxima_accion = {
                    "tipo": "verificar_hipotesis",
                    "id": proxima.get("id"),
                    "columna": proxima.get("columna"),
                    "pregunta": proxima.get("pregunta_usuario") or f"¿Se confirma esta hipótesis sobre {proxima.get('columna') or 'esta columna'}?",
                    "hipotesis": proxima.get("hipotesis"),
                    "confianza": proxima.get("confianza"),
                    "estado": proxima.get("estado"),
                    "fecha": proxima.get("creada_en"),
                }

        if hip_auto_cols:
            cur.execute("""
                SELECT id, origen, tabla_nombre, columna, hipotesis, pregunta,
                       evidencia_json, estado, confianza, modelo, creada_en, actualizada_en
                FROM ia_hipotesis_autonomas
                ORDER BY actualizada_en DESC, id DESC
                LIMIT 50
            """)
            for row in cur.fetchall():
                evidencia = row.get("evidencia_json")
                if evidencia:
                    try:
                        row["evidencia"] = json.loads(evidencia) if isinstance(evidencia, str) else evidencia
                    except Exception:
                        row["evidencia"] = None
                else:
                    row["evidencia"] = None
                row["evidencia_resumen"] = shorten_text(
                    ((row.get("evidencia") or {}).get("ultimo_resumen") or (row.get("evidencia") or {}).get("ultima_query") or ""),
                    160
                )
                hipotesis_autonomas.append(row)
                if (row.get("estado") or "") in ("abierta", "investigando", "requiere_contexto"):
                    hipotesis_abiertas.append(row)

        verificaciones_recientes = []
        if hip_cols:
            cur.execute("""
                SELECT id, columna, hipotesis, confianza, estado, creada_en
                FROM hipotesis_esquema
                WHERE LOWER(COALESCE(estado, '')) IN ('confirmada', 'confirmado', 'validada', 'validado', 'descartada', 'descartado', 'rechazada', 'rechazado')
                ORDER BY id DESC
                LIMIT 20
            """)
            verificaciones_recientes = cur.fetchall()

        conocimiento_confirmado = []
        if conoc_cols:
            activo_filter = "WHERE activo = 1" if "activo" in conoc_cols else ""
            fecha_col = "creada_en" if "creada_en" in conoc_cols else "actualizado_en" if "actualizado_en" in conoc_cols else "NULL"
            fuente_col = "fuente" if "fuente" in conoc_cols else "NULL"
            cur.execute(f"""
                SELECT tema, texto, {fecha_col} AS fecha, {fuente_col} AS fuente
                FROM conocimiento_operativo
                {activo_filter}
                ORDER BY fecha DESC
                LIMIT 20
            """)
            for row in cur.fetchall():
                row["texto_resumido"] = shorten_text(row.get("texto"))
                conocimiento_confirmado.append(row)

        actividad_reciente = []

        if hip_cols:
            cur.execute("""
                SELECT columna, hipotesis, confianza, estado, creada_en
                FROM hipotesis_esquema
                ORDER BY creada_en DESC
                LIMIT 20
            """)
            for row in cur.fetchall():
                columna = row.get("columna") or "sin columna"
                actividad_reciente.append({
                    "fecha": row.get("creada_en"),
                    "hora": time_label(row.get("creada_en")),
                    "tipo": "hipotesis",
                    "texto": f"Generó hipótesis sobre {columna}",
                    "detalle": shorten_text(row.get("hipotesis"), 120),
                })

            for row in verificaciones_recientes:
                estado = (row.get("estado") or "").lower()
                columna = row.get("columna") or "sin columna"
                accion = "confirmó" if estado in ("confirmada", "confirmado", "validada", "validado") else "descartó"
                actividad_reciente.append({
                    "fecha": row.get("creada_en"),
                    "hora": time_label(row.get("creada_en")),
                    "tipo": "verificacion_hipotesis",
                    "texto": f"Diego {accion} hipótesis sobre {columna}",
                    "detalle": shorten_text(row.get("hipotesis"), 120),
                })

        for row in hipotesis_abiertas[:20]:
            actividad_reciente.append({
                "fecha": row.get("actualizada_en") or row.get("creada_en"),
                "hora": time_label(row.get("actualizada_en") or row.get("creada_en")),
                "tipo": "hipotesis_autonoma",
                "texto": f"Hipotesis autonoma {row.get('estado') or 'abierta'}",
                "detalle": shorten_text(row.get("hipotesis"), 140),
            })

        if dudas_cols:
            cur.execute("""
                SELECT pregunta, motivo, creada_en
                FROM dudas_modelo
                ORDER BY creada_en DESC
                LIMIT 20
            """)
            for row in cur.fetchall():
                actividad_reciente.append({
                    "fecha": row.get("creada_en"),
                    "hora": time_label(row.get("creada_en")),
                    "tipo": "duda_generada",
                    "texto": "Generó una duda abierta",
                    "detalle": shorten_text(row.get("pregunta") or row.get("motivo"), 120),
                })

            if {"respuesta_usuario", "respondida_en"}.issubset(dudas_cols):
                cur.execute("""
                    SELECT pregunta, respuesta_usuario, respondida_en
                    FROM dudas_modelo
                    WHERE respuesta_usuario IS NOT NULL
                      AND respuesta_usuario <> ''
                      AND respondida_en IS NOT NULL
                    ORDER BY respondida_en DESC
                    LIMIT 20
                """)
                for row in cur.fetchall():
                    actividad_reciente.append({
                        "fecha": row.get("respondida_en"),
                        "hora": time_label(row.get("respondida_en")),
                        "tipo": "duda_respondida",
                        "texto": "Diego respondió una duda del modelo",
                        "detalle": shorten_text(row.get("respuesta_usuario"), 120),
                    })

        if conoc_cols:
            fecha_col = "creada_en" if "creada_en" in conoc_cols else "actualizado_en" if "actualizado_en" in conoc_cols else "NULL"
            cur.execute(f"""
                SELECT tema, texto, {fecha_col} AS fecha
                FROM conocimiento_operativo
                ORDER BY fecha DESC
                LIMIT 20
            """)
            for row in cur.fetchall():
                tema = row.get("tema") or "conocimiento operativo"
                actividad_reciente.append({
                    "fecha": row.get("fecha"),
                    "hora": time_label(row.get("fecha")),
                    "tipo": "conocimiento_aprendido",
                    "texto": f"Se confirmó conocimiento operativo {tema}",
                    "detalle": shorten_text(row.get("texto"), 120),
                })

        if concl_cols:
            cur.execute("""
                SELECT tipo, prioridad, texto, creada_en
                FROM conclusiones
                WHERE vigente = 1
                ORDER BY creada_en DESC
                LIMIT 20
            """)
            for row in cur.fetchall():
                actividad_reciente.append({
                    "fecha": row.get("creada_en"),
                    "hora": time_label(row.get("creada_en")),
                    "tipo": "conclusion_confirmada",
                    "texto": "Conclusión confirmada",
                    "detalle": shorten_text(row.get("texto"), 120),
                })

        actividad_reciente.sort(
            key=lambda x: str(x.get("fecha") or ""),
            reverse=True
        )

        # Inferencias vigentes (para sección "¿Qué cree?" del cerebro IA)
        inferencias_cerebro = []
        inf_cols = get_table_columns(cur, "inferencias")
        if inf_cols:
            cur.execute("""
                SELECT id, tipo, texto, confianza, creada_en
                FROM inferencias
                WHERE vigente = 1
                ORDER BY id DESC
                LIMIT 20
            """)
            inferencias_cerebro = cur.fetchall()

        diccionario_columnas = []
        if dicc_cols:
            metricas["columnas_esquema_total"] = scalar_count(cur, """
                SELECT COUNT(DISTINCT columna) AS total
                FROM ia_diccionario_columnas
                WHERE tabla = 'TiposDeMovimiento'
                  AND columna IS NOT NULL AND columna <> ''
            """)
            metricas["columnas_diccionario"] = scalar_count(cur, """
                SELECT COUNT(DISTINCT columna) AS total
                FROM ia_diccionario_columnas
                WHERE tabla = 'TiposDeMovimiento'
                  AND columna IS NOT NULL AND columna <> ''
                  AND TRIM(COALESCE(significado_inferido, '')) <> ''
            """)
            cur.execute("""
                SELECT
                    id, tabla, columna, tipo_dato, significado_inferido,
                    confianza, estado, actualizado_en
                FROM ia_diccionario_columnas
                ORDER BY tabla, id
                LIMIT 80
            """)
            diccionario_columnas = cur.fetchall()

        conn_op_cols = None
        try:
            conn_op_cols = get_db_connection()
            if conn_op_cols:
                cur_op_cols = conn_op_cols.cursor()
                cur_op_cols.execute("SHOW COLUMNS FROM TiposDeMovimiento")
                columnas_schema = cur_op_cols.fetchall() or []
                total_schema = len(columnas_schema)
                cur_op_cols.close()
                conn_op_cols.close()
                metricas["columnas_esquema_total"] = max(metricas.get("columnas_esquema_total") or 0, total_schema)
        except Exception:
            try:
                if conn_op_cols:
                    conn_op_cols.close()
            except Exception:
                pass
        relaciones_columnas = []
        relaciones_interesantes = []
        if rel_cols:
            metricas["relaciones_columnas"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM ia_relaciones_columnas
                WHERE tabla = 'TiposDeMovimiento'
            """)
            metricas["relaciones_estructurales"] = metricas["relaciones_columnas"]
            metricas["columnas_con_relaciones"] = scalar_count(cur, """
                SELECT COUNT(DISTINCT columna) AS total
                FROM (
                    SELECT columna_a AS columna FROM ia_relaciones_columnas WHERE tabla = 'TiposDeMovimiento' AND columna_a IS NOT NULL AND columna_a <> ''
                    UNION
                    SELECT columna_b AS columna FROM ia_relaciones_columnas WHERE tabla = 'TiposDeMovimiento' AND columna_b IS NOT NULL AND columna_b <> ''
                ) cols_rel
            """)
            metricas["relaciones_alta_confianza"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM ia_relaciones_columnas
                WHERE tabla = 'TiposDeMovimiento'
                  AND confianza >= 0.75
            """)
            metricas["relaciones_interesantes"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM ia_relaciones_columnas
                WHERE tabla = 'TiposDeMovimiento'
                  AND COALESCE(interes_score, 0) >= 45
            """)
            cur.execute("""
                SELECT
                    id, tabla, columna_a, columna_b, tipo_relacion,
                    descripcion, evidencia_json, confianza, interes_score, nivel_interes, categoria, estado, actualizado_en
                FROM ia_relaciones_columnas
                WHERE tabla = 'TiposDeMovimiento'
                ORDER BY COALESCE(interes_score, 0) DESC, confianza DESC, actualizado_en DESC
                LIMIT 120
            """)
            for row in cur.fetchall():
                evidencia = row.get("evidencia_json")
                if evidencia:
                    try:
                        row["evidencia"] = json.loads(evidencia) if isinstance(evidencia, str) else evidencia
                    except Exception:
                        row["evidencia"] = None
                else:
                    row["evidencia"] = None
                relaciones_columnas.append(row)

            cur.execute("""
                SELECT
                    id, tabla, columna_a, columna_b, tipo_relacion,
                    descripcion, evidencia_json, confianza, interes_score, nivel_interes, categoria, estado, actualizado_en
                FROM ia_relaciones_columnas
                WHERE tabla = 'TiposDeMovimiento'
                  AND COALESCE(interes_score, 0) >= 45
                ORDER BY COALESCE(interes_score, 0) DESC, confianza DESC, actualizado_en DESC
                LIMIT 20
            """)
            for row in cur.fetchall():
                evidencia = row.get("evidencia_json")
                if evidencia:
                    try:
                        row["evidencia"] = json.loads(evidencia) if isinstance(evidencia, str) else evidencia
                    except Exception:
                        row["evidencia"] = None
                else:
                    row["evidencia"] = None
                relaciones_interesantes.append(row)

        mapa_estructural = {
            "columnas_mayor_presencia": [],
            "columnas_raras": [],
            "tipos_movimiento": [],
            "co_presencias_fuertes": [],
            "pendientes": [],
        }
        perfiles_columnas_modelo = []
        perfiles_tipos_modelo = []
        pendientes_estructurales_modelo = []
        if mapa_tablas_cols:
            metricas["mapa_tablas"] = scalar_count(cur, """
                SELECT COUNT(DISTINCT tabla_nombre) AS total
                FROM ia_mapa_tablas
            """)
        if perfil_cols:
            metricas["columnas_perfiladas"] = scalar_count(cur, """
                SELECT COUNT(DISTINCT columna) AS total
                FROM ia_perfil_columnas
                WHERE tabla='TiposDeMovimiento'
            """)
            cur.execute("""
                SELECT columna, tipo_dato, pct_cargado, distinct_count, evidencia_json, actualizado_en
                FROM ia_perfil_columnas
                WHERE tabla='TiposDeMovimiento'
                ORDER BY pct_cargado DESC, distinct_count DESC
                LIMIT 80
            """)
            for row in cur.fetchall() or []:
                row["evidencia"] = parse_json_obj(row.get("evidencia_json"), None)
                perfiles_columnas_modelo.append(row)
            mapa_estructural["columnas_mayor_presencia"] = perfiles_columnas_modelo[:8]
            cur.execute("""
                SELECT columna, tipo_dato, pct_cargado, distinct_count, evidencia_json, actualizado_en
                FROM ia_perfil_columnas
                WHERE tabla='TiposDeMovimiento'
                  AND COALESCE(pct_cargado,0) > 0
                ORDER BY pct_cargado ASC, distinct_count DESC
                LIMIT 8
            """)
            mapa_estructural["columnas_raras"] = cur.fetchall() or []
        if perfil_tipo_cols:
            metricas["tipos_movimiento_perfilados"] = scalar_count(cur, """
                SELECT COUNT(*) AS total FROM ia_perfil_tipos_movimiento
            """)
            cur.execute("""
                SELECT tipo_movimiento, registros, suma_kilos, evidencia_json, actualizado_en
                FROM ia_perfil_tipos_movimiento
                ORDER BY registros DESC
                LIMIT 10
            """)
            for row in cur.fetchall() or []:
                row["evidencia"] = parse_json_obj(row.get("evidencia_json"), None)
                perfiles_tipos_modelo.append(row)
                mapa_estructural["tipos_movimiento"].append(row)
        if rel_cols:
            cur.execute("""
                SELECT columna_a, columna_b, descripcion, evidencia_json, confianza, interes_score, nivel_interes
                FROM ia_relaciones_columnas
                WHERE tabla='TiposDeMovimiento'
                  AND tipo_relacion='co_presencia_cartografia'
                ORDER BY COALESCE(interes_score,0) DESC, confianza DESC
                LIMIT 8
            """)
            for row in cur.fetchall() or []:
                ev = row.get("evidencia_json")
                try:
                    row["evidencia"] = json.loads(ev) if isinstance(ev, str) else ev
                except Exception:
                    row["evidencia"] = None
                mapa_estructural["co_presencias_fuertes"].append(row)
        if pendientes_estruct_cols:
            metricas["pendientes_estructurales"] = scalar_count(cur, """
                SELECT COUNT(*) AS total
                FROM ia_pendientes_estructurales
                WHERE COALESCE(estado,'pendiente')='pendiente'
            """)
            cur.execute("""
                SELECT p.firma, p.tema, p.tarea, p.prioridad, p.estado, p.ultimo_intento, p.actualizado_en,
                       er.resultado AS ultimo_resultado
                FROM ia_pendientes_estructurales p
                LEFT JOIN (
                    SELECT firma, MAX(creada_en) AS ultima
                    FROM ia_exploraciones_temas
                    GROUP BY firma
                ) e ON e.firma = p.firma
                LEFT JOIN ia_exploraciones_temas er ON er.firma = p.firma AND er.creada_en = e.ultima
                WHERE COALESCE(p.estado,'pendiente')='pendiente'
                ORDER BY p.prioridad DESC, p.actualizado_en ASC
                LIMIT 30
            """)
            pendientes_estructurales_modelo = cur.fetchall() or []
            mapa_estructural["pendientes"] = pendientes_estructurales_modelo[:8]
        conclusiones_autonomas_db = []
        observaciones_exploratorias_db = []
        if ia_auto_cols:
            id_col = "id" if "id" in ia_auto_cols else "NULL AS id"
            columna_col = "columna" if "columna" in ia_auto_cols else "NULL AS columna"
            estado_col = "estado" if "estado" in ia_auto_cols else "NULL AS estado"
            confianza_col = "confianza" if "confianza" in ia_auto_cols else "NULL AS confianza"
            conclusion_col = "conclusion" if "conclusion" in ia_auto_cols else "NULL AS conclusion"
            hipotesis_col = "hipotesis_refinada" if "hipotesis_refinada" in ia_auto_cols else "NULL AS hipotesis_refinada"
            regla_col = "regla_aprendible" if "regla_aprendible" in ia_auto_cols else "NULL AS regla_aprendible"
            salida_col = "salida_json" if "salida_json" in ia_auto_cols else "NULL AS salida_json"
            evidencia_auto_col = "evidencia_json" if "evidencia_json" in ia_auto_cols else "NULL AS evidencia_json"
            fecha_col = "creada_en" if "creada_en" in ia_auto_cols else "NULL AS creada_en"
            cur.execute(f"""
                SELECT
                    {id_col},
                    {columna_col},
                    {estado_col},
                    {confianza_col},
                    {conclusion_col},
                    {hipotesis_col},
                    {regla_col},
                    {salida_col},
                    {evidencia_auto_col},
                    {fecha_col}
                FROM ia_conclusiones_autonomas
                ORDER BY id DESC
                LIMIT 60
            """)
            for row in cur.fetchall():
                salida = row.get("salida_json")
                salida_data = None
                if salida:
                    try:
                        salida_data = json.loads(salida) if isinstance(salida, str) else salida
                    except Exception:
                        salida_data = None
                if isinstance(salida_data, dict):
                    row["conclusion"] = primer_texto(
                        row.get("conclusion"),
                        salida_data.get("conclusion_principal"),
                        salida_data.get("conclusion"),
                        salida_data.get("conclusion_autonoma"),
                        salida_data.get("interpretacion"),
                        salida_data.get("resumen"),
                    )
                    row["hipotesis_refinada"] = primer_texto(
                        row.get("hipotesis_refinada"),
                        salida_data.get("hipotesis_nueva"),
                        salida_data.get("hipotesis_refinada"),
                        salida_data.get("hipotesis"),
                    )
                    row["regla_aprendible"] = primer_texto(
                        row.get("regla_aprendible"),
                        salida_data.get("regla_aprendida"),
                        salida_data.get("regla_aprendible"),
                        salida_data.get("regla"),
                    )
                evidencia_auto = row.get("evidencia_json")
                if evidencia_auto:
                    try:
                        row["evidencia"] = json.loads(evidencia_auto) if isinstance(evidencia_auto, str) else evidencia_auto
                    except Exception:
                        row["evidencia"] = None
                row["fecha"] = row.get("creada_en")
                row["hora"] = time_label(row.get("creada_en"))
                if (row.get("estado") or "").lower() == "observacion_exploratoria":
                    if row.get("conclusion"):
                        observaciones_exploratorias_db.append(row)
                    continue
                if row.get("conclusion") or row.get("hipotesis_refinada") or row.get("regla_aprendible"):
                    conclusiones_autonomas_db.append(row)

        cur.close()
        conn.close()

        observaciones_exploratorias = dedupe_por_texto(observaciones_exploratorias_db, limite=30)[:20]
        modelo_estructural_inferido = construir_modelo_estructural_inferido(
            metricas, contexto_temporal, diccionario_columnas, relaciones_interesantes,
            observaciones_exploratorias, mapa_estructural, perfiles_columnas_modelo,
            perfiles_tipos_modelo, pendientes_estructurales_modelo
        )

        conclusiones_autonomas = []
        vistos = set()
        for item in conclusiones_autonomas_db + list(IA_AUTONOMOUS_CONCLUSIONS[:20]):
            if not isinstance(item, dict):
                continue
            texto_clave = (item.get("conclusion") or item.get("hipotesis_refinada") or item.get("regla_aprendible") or "").strip().lower()
            clave = texto_clave[:260] if texto_clave else item.get("id")
            if not clave or clave in vistos:
                continue
            vistos.add(clave)
            item["nivel_interes"] = nivel_interes_conclusion(item)
            conclusiones_autonomas.append(item)
        conclusiones_autonomas = conclusiones_autonomas[:20]

        hallazgos_interesantes = []
        firmas_hallazgos = set()
        for item in conclusiones_autonomas:
            if item.get("nivel_interes") not in ("alta", "media"):
                continue
            firma = firma_tematica_hallazgo(item)
            if firma in firmas_hallazgos:
                continue
            firmas_hallazgos.add(firma)
            item["firma_tematica"] = firma
            hallazgos_interesantes.append(item)
            if len(hallazgos_interesantes) >= 8:
                break
        metricas["conclusiones_autonomas"] = len(conclusiones_autonomas)
        metricas["observaciones_exploratorias"] = len(observaciones_exploratorias)
        metricas["hallazgos_interesantes"] = len(hallazgos_interesantes)
        metricas["hallazgos_alta_prioridad"] = len([
            x for x in conclusiones_autonomas
            if x.get("nivel_interes") == "alta"
        ])
        metricas["aprendizajes_totales"] = (
            (metricas.get("conocimiento_operativo_aprendido") or 0)
            + metricas["conclusiones_autonomas"]
            + metricas.get("observaciones_exploratorias", 0)
        )

        return {
            "metricas": metricas,
            "actividad_reciente": actividad_reciente[:40],
            "proxima_accion": proxima_accion,
            "hipotesis_activas": hipotesis_activas,
            "hipotesis_autonomas": hipotesis_autonomas,
            "hipotesis_abiertas": hipotesis_abiertas,
            "verificaciones_recientes": verificaciones_recientes,
            "conocimiento_confirmado": conocimiento_confirmado,
            "inferencias": inferencias_cerebro,
            "diccionario_columnas": diccionario_columnas,
            "relaciones_columnas": relaciones_columnas,
            "relaciones_interesantes": relaciones_interesantes,
            "mapa_estructural": mapa_estructural,
            "contexto_temporal": contexto_temporal,
            "modelo_estructural_inferido": modelo_estructural_inferido,
            "conclusiones_autonomas": conclusiones_autonomas,
            "observaciones_exploratorias": observaciones_exploratorias,
            "hallazgos_interesantes": hallazgos_interesantes,
            "razonamiento_reciente": IA_REASONING_LOG[:30],
            "autonomia": estado_autonomia_ia(),
            "tablas_derivadas": leer_tablas_derivadas_resumen(),
            "protagonistas_operativos": leer_protagonistas_operativos(),
            "biografias_protagonistas": leer_biografia_protagonistas(),
            "cola_curiosidad": leer_cola_curiosidad(),
            "fichas_operativas": leer_fichas_operativas(),
            "mapa_historico_proveedores": leer_mapa_historico_proveedores(),
            "materiales_normalizados_recientes": leer_materiales_normalizados_recientes(),
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        print(f"Error en get_ia_cerebro: {e}", file=sys.stderr)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.route('/api/ia/responder_duda', methods=['POST'])
def api_responder_duda():
    data = request.get_json(force=True)
    duda_id = data.get("id")
    respuesta = (data.get("respuesta") or "").strip()

    if not duda_id or not respuesta:
        return jsonify({"error": "Falta id o respuesta"}), 400

    conn = get_ia_connection()
    if not conn:
        return jsonify({"error": "Error conectando a crowdbot_lab"}), 500

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT pregunta
            FROM dudas_modelo
            WHERE id = %s
            LIMIT 1
        """, (duda_id,))
        duda = cur.fetchone()

        if not duda:
            return jsonify({"error": "Duda no encontrada"}), 404

        tema = "aprendido_desde_duda"
        pregunta = duda.get("pregunta") if isinstance(duda, dict) else str(duda)

        cur.execute("""
            INSERT INTO conocimiento_operativo (tema, texto, fuente)
            VALUES (%s, %s, 'dashboard_ia')
        """, (
            tema,
            f"Pregunta del modelo: {pregunta}\nRespuesta de Diego: {respuesta}"
        ))

        cur.execute("""
            UPDATE dudas_modelo
            SET estado='respondida',
                respuesta_usuario=%s,
                respondida_en=CURRENT_TIMESTAMP
            WHERE id=%s
        """, (respuesta, duda_id))

        conn.commit()
        return jsonify({"ok": True})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/ia/feedback_hipotesis', methods=['POST'])
def api_feedback_hipotesis():
    data = request.get_json(force=True)
    hipotesis_id = data.get("id")
    accion = (data.get("accion") or "").strip().lower()
    comentario = (data.get("comentario") or "").strip()

    if not hipotesis_id or accion not in ("confirmar", "descartar"):
        return jsonify({"error": "Falta id o accion valida"}), 400

    conn = get_ia_connection()
    if not conn:
        return jsonify({"error": "Error conectando a crowdbot_lab"}), 500

    try:
        cur = conn.cursor()
        hip_cols = get_table_columns(cur, "hipotesis_esquema")
        conoc_cols = get_table_columns(cur, "conocimiento_operativo")

        cur.execute("""
            SELECT id, columna, hipotesis, confianza, estado
            FROM hipotesis_esquema
            WHERE id = %s
            LIMIT 1
        """, (hipotesis_id,))
        hip = cur.fetchone()

        if not hip:
            return jsonify({"error": "Hipotesis no encontrada"}), 404

        nuevo_estado = "confirmada" if accion == "confirmar" else "descartada"
        updates = ["estado = %s"]
        params = [nuevo_estado]

        if "vigente" in hip_cols:
            updates.append("vigente = 0")
        if "actualizada_en" in hip_cols:
            updates.append("actualizada_en = CURRENT_TIMESTAMP")
        elif "actualizado_en" in hip_cols:
            updates.append("actualizado_en = CURRENT_TIMESTAMP")

        params.append(hipotesis_id)
        cur.execute(f"""
            UPDATE hipotesis_esquema
            SET {", ".join(updates)}
            WHERE id = %s
        """, params)

        if accion == "confirmar" and conoc_cols:
            columna = hip.get("columna") or "sin_columna"
            texto = f"Hipotesis confirmada sobre {columna}: {hip.get('hipotesis') or ''}"
            if comentario:
                texto += f"\nVerificacion de Diego: {comentario}"

            cols = ["tema", "texto"]
            values = ["%s", "%s"]
            insert_params = [f"hipotesis_confirmada_{columna}", texto]

            if "fuente" in conoc_cols:
                cols.append("fuente")
                values.append("%s")
                insert_params.append("dashboard_ia_feedback")
            if "activo" in conoc_cols:
                cols.append("activo")
                values.append("1")

            cur.execute(f"""
                INSERT INTO conocimiento_operativo ({", ".join(cols)})
                VALUES ({", ".join(values)})
            """, insert_params)

        conn.commit()
        return jsonify({"ok": True, "estado": nuevo_estado})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/ia/estado')
def api_ia_estado():
    estado = get_ia_estado()
    if not estado:
        return jsonify({'error': 'Error consultando crowdbot_lab'}), 500
    return jsonify(estado)



@app.route('/api/ia/relaciones/recalcular', methods=['POST'])
def api_ia_relaciones_recalcular():
    resultado = guardar_relaciones_columnas_si_corresponde(forzar=True)
    if not resultado.get("ok"):
        return jsonify(resultado), 500
    cerebro = get_ia_cerebro() or {}
    cerebro["recalculo_relaciones"] = resultado
    return jsonify(cerebro)

@app.route('/api/ia/cartografia/recalcular', methods=['POST'])
def api_ia_cartografia_recalcular():
    resultado = guardar_cartografia_base_si_corresponde(forzar=True)
    if not resultado.get("ok"):
        return jsonify(resultado), 500
    cerebro = get_ia_cerebro() or {}
    cerebro["recalculo_cartografia"] = resultado
    return jsonify(cerebro)

@app.route('/api/ia/cerebro')
def api_ia_cerebro():
    cerebro = get_ia_cerebro()
    if not cerebro:
        return jsonify({'error': 'Error consultando cerebro IA'}), 500
    return jsonify(cerebro)


@app.route('/api/ia/derivadas/recalcular', methods=['POST'])
def api_ia_derivadas_recalcular():
    resultado = guardar_tablas_derivadas_si_corresponde(forzar=True)
    # Backfill de dominantes sobre fichas de material ya completadas
    # (dedupe lo hace idempotente y barato)
    try:
        resultado["backfill_dominantes"] = propagar_dominantes_materiales_existentes()
    except Exception as e_bf:
        resultado["backfill_dominantes"] = {"ok": False, "errores": [str(e_bf)]}
    status = 200 if resultado.get("ok") else 500
    return jsonify(resultado), status


@app.route('/api/ia/cola/propagar-dominantes', methods=['POST'])
def api_ia_cola_propagar_dominantes():
    """Backfill manual: propaga proveedores dominantes desde fichas
    perfil_material_global ya completadas hacia ia_cola_curiosidad."""
    forzar = False
    try:
        data = request.get_json(silent=True) or {}
        forzar = bool(data.get("forzar"))
    except Exception:
        pass
    resultado = propagar_dominantes_materiales_existentes(forzar=forzar)
    status = 200 if resultado.get("ok") else 500
    return jsonify(resultado), status


@app.route('/api/ia/telegram_test', methods=['POST'])
def api_ia_telegram_test():
    resultado = enviar_telegram(
        "Ecotecnica IA Local\nPrueba de Telegram OK.\nSi ves esto, el cerebro IA puede avisarte cuando aprenda algo nuevo."
    )
    status = 200 if resultado.get("ok") else 400
    return jsonify({
        "ok": bool(resultado.get("ok")),
        "telegram_configurado": telegram_configurado(),
        "resultado": resultado,
    }), status


@app.route('/api/ia/ciclo', methods=['POST'])
def api_ia_ciclo():
    if not AUTO_IA_LOCK.acquire(blocking=False):
        return jsonify({'error': 'Hay un ciclo IA en ejecucion'}), 409
    try:
        cerebro = ejecutar_ciclo_ia(modo="manual")
        if not cerebro:
            return jsonify({'error': 'Error ejecutando ciclo IA'}), 500
        return jsonify(cerebro)
    finally:
        AUTO_IA_LOCK.release()


@app.route('/api/ia/export')
def api_ia_export():
    estado = get_ia_estado()
    cerebro = get_ia_cerebro()

    if not estado:
        return jsonify({'error': 'Error consultando crowdbot_lab'}), 500
    if not cerebro:
        return jsonify({'error': 'Error consultando cerebro IA'}), 500

    contexto = estado.get("contexto_operacional") or {}
    generado_en = datetime.now().isoformat()
    payload = {
        "estado_planta": estado.get("estado"),
        "resumen_mensual": contexto.get("resumen_mes"),
        "inferencias": estado.get("inferencias", []),
        "conclusiones": estado.get("conclusiones", []),
        "conclusiones_modelo": estado.get("conclusiones_modelo", []),
        "dudas_modelo": estado.get("dudas_modelo", []),
        "preguntas_esquema": estado.get("preguntas_esquema", []),
        "proxima_accion": cerebro.get("proxima_accion"),
        "hipotesis_activas": cerebro.get("hipotesis_activas", []),
        "hipotesis_autonomas": cerebro.get("hipotesis_autonomas", []),
        "hipotesis_abiertas": cerebro.get("hipotesis_abiertas", []),
        "verificaciones_recientes": cerebro.get("verificaciones_recientes", []),
        "conocimiento_confirmado": cerebro.get("conocimiento_confirmado", []),
        "diccionario_columnas": cerebro.get("diccionario_columnas", []),
        "relaciones_columnas": cerebro.get("relaciones_columnas", []),
        "relaciones_interesantes": cerebro.get("relaciones_interesantes", []),
        "mapa_estructural": cerebro.get("mapa_estructural", {}),
        "contexto_temporal": cerebro.get("contexto_temporal", {}),
        "modelo_estructural_inferido": cerebro.get("modelo_estructural_inferido", {}),
        "observaciones_exploratorias": cerebro.get("observaciones_exploratorias", []),
        "conclusiones_autonomas": cerebro.get("conclusiones_autonomas", []),
        "hallazgos_interesantes": cerebro.get("hallazgos_interesantes", []),
        "actividad_reciente": cerebro.get("actividad_reciente", []),
        "razonamiento_reciente": cerebro.get("razonamiento_reciente", []),
        "autonomia": cerebro.get("autonomia", {}),
        "metricas_cerebro": cerebro.get("metricas", {}),
        "timestamp_generacion": generado_en,
    }

    response = jsonify(payload)
    filename = f"cerebro_ia_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route('/ia')
def ia_dashboard():
    html = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Ecotecnica · Cerebro IA</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:0; }

    /* -- Top bar --------------------------------- */
    .topbar {
      display:flex; align-items:center; justify-content:space-between;
      background:#161b22; border-bottom:1px solid #30363d;
      padding:10px 20px; position:sticky; top:0; z-index:100;
    }
    .topbar h1 { margin:0; font-size:18px; color:#58a6ff; }
    .topbar .right { display:flex; gap:8px; align-items:center; }
    .status-dot { width:9px; height:9px; border-radius:50%; background:#3fb950; display:inline-block; margin-right:4px; }
    .muted { color:#8b949e; font-size:13px; }
    button { padding:7px 12px; border-radius:6px; border:1px solid #30363d; cursor:pointer;
             background:#21262d; color:#c9d1d9; font-size:13px; }
    button:hover { background:#30363d; }
    button.primary { background:#238636; border-color:#2ea043; color:#fff; }
    button.primary:hover { background:#2ea043; }

    /* -- Main layout ---------------------------- */
    .main { padding:20px; max-width:1400px; margin:0 auto; }

    /* -- Section titles ------------------------- */
    .section-title {
      font-size:11px; font-weight:700; letter-spacing:.08em;
      text-transform:uppercase; color:#8b949e;
      border-bottom:1px solid #21262d; margin:24px 0 12px; padding-bottom:6px;
    }

    /* -- Grid layouts --------------------------- */
    .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:16px; }
    .grid3 { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }

    /* -- Cards ---------------------------------- */
    .card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:16px; }
    .card h2 { margin:0 0 12px; font-size:14px; color:#e6edf3; }

    /* -- Metricas bar --------------------------- */
    .metrics-bar {
      display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
      gap:10px; margin-bottom:20px;
    }
    .metric-box {
      background:#161b22; border:1px solid #30363d; border-radius:8px;
      padding:10px 12px; text-align:center;
    }
    .metric-box .val { font-size:26px; font-weight:700; line-height:1.1; }
    .metric-box .lbl { font-size:11px; color:#8b949e; margin-top:4px; }
    .val.green { color:#3fb950; }
    .val.yellow { color:#d29922; }
    .val.blue { color:#58a6ff; }
    .val.red { color:#f85149; }
    .val.gray { color:#8b949e; }

    /* -- Progress bar --------------------------- */
    .progress-wrap { background:#21262d; border-radius:4px; height:8px; margin:8px 0; overflow:hidden; }
    .progress-fill { height:100%; background:#238636; border-radius:4px; transition:width .4s; }

    /* -- Timeline ------------------------------- */
    .timeline { list-style:none; padding:0; margin:0; }
    .timeline li {
      display:flex; gap:10px; padding:8px 0;
      border-bottom:1px solid #21262d; align-items:flex-start;
    }
    .timeline li:last-child { border-bottom:0; }
    .tl-icon { width:22px; height:22px; border-radius:50%; flex-shrink:0;
               display:flex; align-items:center; justify-content:center; font-size:11px; margin-top:1px; }
    .tl-icon.hipotesis  { background:#1f3a5a; color:#58a6ff; }
    .tl-icon.conocimiento { background:#1a3a2a; color:#3fb950; }
    .tl-icon.duda       { background:#3a2a10; color:#d29922; }
    .tl-icon.verificacion { background:#2a1a3a; color:#bc8cff; }
    .tl-icon.conclusion { background:#1a2a3a; color:#79c0ff; }
    .tl-icon.razonamiento { background:#21262d; color:#8b949e; }
    .tl-body { flex:1; min-width:0; }
    .tl-hora { font-size:11px; color:#3fb950; font-family:monospace; font-weight:700; }
    .tl-texto { font-size:13px; color:#e6edf3; }
    .tl-detalle { font-size:12px; color:#8b949e; margin-top:2px; white-space:pre-wrap; word-break:break-word; }

    /* -- Hipotesis cards ------------------------ */
    .hip-card { border:1px solid #30363d; border-radius:8px; padding:12px; margin-bottom:10px; background:#0d1117; }
    .hip-card .col-badge {
      display:inline-block; background:#1f3a5a; color:#79c0ff;
      font-size:11px; padding:2px 8px; border-radius:12px; margin-bottom:6px; font-weight:600;
    }
    .hip-card .hip-texto { font-size:13px; color:#c9d1d9; margin:4px 0; }
    .hip-card .hip-meta { font-size:12px; color:#8b949e; }
    .confianza-bar { background:#21262d; border-radius:3px; height:5px; margin-top:4px; overflow:hidden; }
    .confianza-fill { height:100%; border-radius:3px; }

    /* -- Conocimiento cards ---------------------- */
    .conoc-card { border-left:3px solid #238636; padding:8px 12px; margin-bottom:10px; background:#0d1117; border-radius:0 6px 6px 0; }
    .conoc-card .tema { font-size:11px; font-weight:700; color:#3fb950; margin-bottom:4px; }
    .conoc-card .texto { font-size:13px; color:#c9d1d9; }
    .conoc-card .meta { font-size:11px; color:#8b949e; margin-top:4px; }

    /* -- Inferencias ---------------------------- */
    .inf-card { border-left:3px solid #58a6ff; padding:8px 12px; margin-bottom:8px; background:#0d1117; border-radius:0 6px 6px 0; }
    .inf-card .tipo { font-size:11px; font-weight:700; color:#79c0ff; }
    .inf-card .texto { font-size:13px; color:#c9d1d9; }
    .inf-card .confianza { font-size:11px; color:#8b949e; }
    .rel-card { border-left:3px solid #d29922; padding:8px 12px; margin-bottom:8px; background:#0d1117; border-radius:0 6px 6px 0; }
    .rel-card .tipo { font-size:11px; font-weight:700; color:#d29922; }
    .rel-card .texto { font-size:13px; color:#c9d1d9; }
    .rel-card .meta { font-size:11px; color:#8b949e; margin-top:4px; }
    .rel-chip { display:inline-block; border:1px solid #30363d; border-radius:10px; padding:1px 7px; margin:4px 4px 0 0; font-size:11px; color:#8b949e; }
    .quick-read { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
    .quick-read li { background:#0d1117; border:1px solid #30363d; border-left:3px solid #3fb950; border-radius:8px; padding:9px 11px; font-size:13px; color:#dbe7f3; line-height:1.35; }
    .useful-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; }
    .signal-card { background:#0d1117; border:1px solid #30363d; border-left:3px solid #58a6ff; border-radius:8px; padding:11px 12px; }
    .signal-card.relacion { border-left-color:#f2cc60; }
    .signal-card.operativo { border-left-color:#3fb950; }
    .signal-card.hipotesis { border-left-color:#bc8cff; }
    .signal-card.limitacion { border-left-color:#f85149; }
    .signal-kind { font-size:11px; font-weight:700; color:#8b949e; text-transform:uppercase; margin-bottom:5px; }
    .signal-text { font-size:13px; color:#e6edf3; line-height:1.35; display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
    .signal-meta { font-size:11px; color:#8b949e; margin-top:7px; }
    .mental-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:16px; }
    .mental-card { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; }
    .mental-card .num { font-size:24px; font-weight:700; color:#e6edf3; line-height:1; }
    .mental-card .lbl { font-size:12px; color:#8b949e; margin-top:6px; }
    .mental-card .hint { font-size:11px; color:#6e7681; margin-top:4px; }
    .key-rel-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; }
    .rel-card.key { border-left-color:#f2cc60; padding:12px; }
    .rel-title { font-size:13px; font-weight:700; color:#e6edf3; margin-bottom:4px; }
    .rel-meta-line { font-size:11px; color:#8b949e; margin-bottom:6px; }
    .rel-section-label { font-size:11px; font-weight:700; color:#79c0ff; margin:9px 0 2px; }
    .rel-section-text { font-size:12px; color:#c9d1d9; line-height:1.35; display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
    .rel-section-text.evidence { color:#8b949e; }
    .rel-card[title] { cursor:help; }
    .summary-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; margin-top:16px; }
    .compact-list { list-style:none; margin:0; padding:0; }
    .compact-list li { padding:8px 0; border-bottom:1px solid #21262d; font-size:13px; color:#c9d1d9; }
    .compact-list li:last-child { border-bottom:0; }
    .compact-meta { display:block; font-size:11px; color:#8b949e; margin-top:2px; }
    .insight-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; margin-bottom:16px; }
    .insight-card { background:#0d1117; border:1px solid #30363d; border-left:4px solid #58a6ff; border-radius:8px; padding:12px; }
    .insight-card.alta { border-left-color:#f85149; }
    .insight-card.media { border-left-color:#d29922; }
    .insight-card.baja { border-left-color:#8b949e; }
    .insight-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:8px; }
    .insight-title { font-size:12px; font-weight:700; color:#e6edf3; }
    .pill { border:1px solid #30363d; border-radius:12px; padding:2px 8px; font-size:11px; color:#8b949e; white-space:nowrap; }
    .pill.alta { color:#f85149; border-color:#6e2932; background:#2a1a1a; }
    .pill.media { color:#d29922; border-color:#6b4f16; background:#2b2414; }
    .pill.baja { color:#8b949e; }
    .insight-text { font-size:13px; color:#c9d1d9; line-height:1.35; }
    .insight-meta { font-size:11px; color:#8b949e; margin-top:8px; }

    /* -- Verificaciones ------------------------- */
    .ver-card { border-left:3px solid #bc8cff; padding:8px 12px; margin-bottom:8px; background:#0d1117; border-radius:0 6px 6px 0; }
    .ver-card .estado { font-size:11px; font-weight:700; }
    .estado-conf { color:#3fb950; }
    .estado-desc { color:#f85149; }
    .ver-card .texto { font-size:13px; color:#c9d1d9; }
    .ver-card .meta { font-size:11px; color:#8b949e; }

    /* -- Feedback -------------------------------- */
    textarea { background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:8px; width:100%; resize:vertical; margin-top:6px; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:6px; }
    .btn-confirm { background:#238636; border-color:#2ea043; color:#fff; }
    .btn-confirm:hover { background:#2ea043; }
    .btn-discard { background:#6e2932; border-color:#9d2930; color:#fff; }
    .btn-discard:hover { background:#9d2930; }

    /* -- Tabs ------------------------------------ */
    .tabs { display:flex; gap:4px; margin-bottom:16px; border-bottom:1px solid #30363d; }
    .tab { padding:8px 14px; font-size:13px; cursor:pointer; border-radius:6px 6px 0 0;
           border:1px solid transparent; border-bottom:0; color:#8b949e; }
    .tab.active { background:#161b22; border-color:#30363d; color:#e6edf3; border-bottom-color:#161b22; margin-bottom:-1px; }

    /* -- Estado planta (compacto, al fondo) ---- */
    .planta-row { display:flex; gap:12px; flex-wrap:wrap; }
    .planta-stat { flex:1; min-width:120px; background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:10px; text-align:center; }
    .planta-stat .val { font-size:20px; font-weight:700; color:#58a6ff; }
    .planta-stat .lbl { font-size:11px; color:#8b949e; margin-top:2px; }

    details summary { cursor:pointer; color:#58a6ff; font-size:13px; margin-top:8px; }

    /* -- Autonomia badge ------------------------ */
    .auto-badge { display:inline-flex; align-items:center; gap:5px; background:#162b1a; border:1px solid #238636; border-radius:12px; padding:3px 10px; font-size:12px; color:#3fb950; }
    .auto-badge.off { background:#2a1a1a; border-color:#6e2932; color:#f85149; }

    .empty { color:#8b949e; font-size:13px; font-style:italic; }

    /* -- Badges de antigüedad y tipo ------------ */
    .badge { display:inline-block; border-radius:10px; padding:1px 7px; font-size:10px; font-weight:700;
             letter-spacing:.03em; vertical-align:middle; margin-left:5px; }
    .badge.reciente  { background:#162b1a; color:#3fb950; border:1px solid #238636; }
    .badge.hoy       { background:#1a2a3a; color:#79c0ff; border:1px solid #1f6feb; }
    .badge.historico { background:#1c1c1c; color:#6e7681; border:1px solid #30363d; }
    .badge.microtarea{ background:#2a1a3a; color:#bc8cff; border:1px solid #6e40c9; }
    .badge.pulso     { background:#1a3a2a; color:#3fb950; border:1px solid #238636; }
    .badge.estructural{ background:#3a2a10; color:#d29922; border:1px solid #9e6a03; }

    /* -- Banda de última actividad -------------- */
    .actividad-banda {
      display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px;
      background:#0d1117; border:1px solid #30363d; border-radius:10px;
      padding:12px 16px; margin-bottom:20px;
    }
    .act-item { display:flex; flex-direction:column; gap:3px; }
    .act-label { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:#8b949e; }
    .act-valor { font-size:13px; color:#e6edf3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .act-hora  { font-size:11px; color:#3fb950; font-family:monospace; }

    /* -- Timeline agrupado ---------------------- */
    .tl-group-label {
      font-size:10px; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
      color:#8b949e; padding:6px 0 4px; border-bottom:1px solid #21262d; margin-bottom:4px;
    }
    .timeline li.noise {
      opacity:.45; font-size:12px;
    }
    .timeline li.noise .tl-texto { font-size:12px; color:#8b949e; }
    .tl-noise-toggle { font-size:11px; color:#58a6ff; cursor:pointer; padding:4px 0; display:block; }
  </style>
</head>
<body>

<div class="topbar">
  <h1>?? Cerebro IA · Ecotecnica</h1>
  <div class="right">
    <span class="status-dot" id="statusDot"></span>
    <span class="muted" id="actualizado">Cargando...</span>
    <span id="autoBadge"></span>
    <button onclick="cargar()">Refrescar</button>
    <button class="primary" onclick="ejecutarCicloIA()">Ejecutar ciclo IA</button>
    <button onclick="descargarSnapshot()">? Snapshot</button>
  </div>
</div>

<div class="main">

  <!-- -- BANDA ÚLTIMA ACTIVIDAD -- -->
  <div class="actividad-banda" id="bandaActividad">
    <div class="act-item"><div class="act-label">Última observación</div><div class="act-valor" id="baObsValor">-</div><div class="act-hora" id="baObsHora">-</div></div>
    <div class="act-item"><div class="act-label">Última microtarea</div><div class="act-valor" id="baMicroValor">-</div><div class="act-hora" id="baMicroHora">-</div></div>
    <div class="act-item"><div class="act-label">Último pulso operativo</div><div class="act-valor" id="baPulsoValor">-</div><div class="act-hora" id="baPulsoHora">-</div></div>
    <div class="act-item"><div class="act-label">Derivadas recalculadas</div><div class="act-valor" id="baDerivadasValor">-</div><div class="act-hora" id="baDerivadasHora">-</div></div>
  </div>

  <!-- -- MÉTRICAS GENERALES -- -->
  <div class="section-title">Conocimiento acumulado</div>
  <div class="metrics-bar">
    <div class="metric-box"><div class="val green" id="mHallazgos">-</div><div class="lbl">Hallazgos IA</div></div>
    <div class="metric-box"><div class="val yellow" id="mAutConc">-</div><div class="lbl">Conclusiones autonomas</div></div>
    <div class="metric-box"><div class="val red" id="mHallAlta">-</div><div class="lbl">Alta prioridad</div></div>
    <div class="metric-box"><div class="val blue" id="mAprTotal">-</div><div class="lbl">Aprendizajes totales</div></div>
    <div class="metric-box"><div class="val green" id="mHipConf">-</div><div class="lbl">Hipótesis confirmadas</div></div>
    <div class="metric-box"><div class="val blue" id="mHipAct">-</div><div class="lbl">Hipótesis activas</div></div>
    <div class="metric-box"><div class="val yellow" id="mHipAuto">-</div><div class="lbl">Autónomas provisionales</div></div>
    <div class="metric-box"><div class="val red" id="mHipDesc">-</div><div class="lbl">Descartadas</div></div>
    <div class="metric-box"><div class="val yellow" id="mDudas">-</div><div class="lbl">Dudas abiertas</div></div>
    <div class="metric-box"><div class="val green" id="mConoc">-</div><div class="lbl">Conocimiento operativo</div></div>
    <div class="metric-box"><div class="val blue" id="mBaseCols">-</div><div class="lbl">Columnas base comprendidas</div></div>
    <div class="metric-box"><div class="val blue" id="mSchemaCols">-</div><div class="lbl">Columnas del esquema</div></div>
    <div class="metric-box"><div class="val green" id="mDictCols">-</div><div class="lbl">Columnas interpretadas</div></div>
    <div class="metric-box"><div class="val yellow" id="mLinkedCols">-</div><div class="lbl">Columnas vinculadas</div></div>
    <div class="metric-box"><div class="val blue" id="mRelaciones">-</div><div class="lbl">Relaciones estructurales</div></div>
    <div class="metric-box"><div class="val yellow" id="mRelInteres">-</div><div class="lbl">Relaciones interesantes</div></div>
  </div>
  <div style="margin-bottom:4px;font-size:12px;color:#8b949e;">Fase 1 · columnas base: <span id="mProgreso">-</span></div>
  <div class="progress-wrap"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>

  <!-- -- TABS PRINCIPAL -- -->
  <div class="section-title" style="margin-top:28px;">Modelo mental</div>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('tab-resumen', this)">Resumen</div>
    <div class="tab" onclick="switchTab('tab-relaciones', this)">Relaciones</div>
    <div class="tab" onclick="switchTab('tab-actividad', this)">Actividad reciente</div>
    <div class="tab" onclick="switchTab('tab-hipotesis', this)">Hipotesis activas</div>
    <div class="tab" onclick="switchTab('tab-conocimiento', this)">Conocimiento confirmado</div>
    <div class="tab" onclick="switchTab('tab-inferencias', this)">Inferencias vigentes</div>
    <div class="tab" onclick="switchTab('tab-verificaciones', this)">Verificaciones</div>
  </div>

  <!-- TAB: Resumen -->
  <div id="tab-resumen" class="tab-panel">
    <div class="card">
      <h2>Lectura rapida</h2>
      <ul class="quick-read" id="lecturaRapida"></ul>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>Contexto temporal operativo</h2>
      <ul class="compact-list" id="contextoTemporalOperativo"></ul>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>Mapa mental actual</h2>
      <div class="mental-grid" id="mapaMentalActual"></div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>Mapa estructural</h2>
      <div class="summary-grid">
        <div><h2>Columnas mas cargadas</h2><ul class="compact-list" id="mapaColumnasCargadas"></ul></div>
        <div><h2>Columnas raras</h2><ul class="compact-list" id="mapaColumnasRaras"></ul></div>
        <div><h2>Tipos perfilados</h2><ul class="compact-list" id="mapaTiposMovimiento"></ul></div>
        <div><h2>Co-presencias fuertes</h2><ul class="compact-list" id="mapaCoPresencias"></ul></div>
        <div><h2>Pendientes estructurales</h2><ul class="compact-list" id="mapaPendientes"></ul></div>
      </div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>Señales utiles</h2>
      <div class="useful-grid" id="senalesUtiles"></div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>Relaciones clave detectadas</h2>
      <div class="key-rel-grid" id="relacionesClave"></div>
    </div>

    <div class="card" id="limitacionesCard" style="margin-top:12px;display:none;">
      <h2>Limitaciones detectadas</h2>
      <div class="useful-grid" id="limitacionesDetectadas"></div>
    </div>
    <div class="summary-grid">
      <div class="card">
        <h2>Hallazgos IA</h2>
        <ul class="compact-list" id="resumenHallazgos"></ul>
      </div>
      <div class="card">
        <h2>Pulso operativo</h2>
        <ul class="compact-list" id="resumenPulso"></ul>
      </div>
      <div class="card">
        <h2>Aprendizajes recientes</h2>
        <ul class="compact-list" id="resumenAprendizajes"></ul>
      </div>
      <div class="card">
        <h2>Observaciones exploratorias</h2>
        <ul class="compact-list" id="resumenObservaciones"></ul>
      </div>
    </div>
  </div>

  <!-- TAB: Actividad reciente -->
  <div id="tab-actividad" class="tab-panel" style="display:none">
    <div class="card">
      <h2>Actividad reciente con valor para el modelo mental</h2>
      <ul class="timeline" id="actividadReciente"></ul>
      <details>
        <summary id="actividadMoreLink" style="display:none">Ver mas eventos</summary>
        <ul class="timeline" id="actividadRestante"></ul>
      </details>
    </div>
  </div>

  <!-- TAB: Hipótesis activas -->
  <div id="tab-hipotesis" class="tab-panel" style="display:none">
    <div class="card">
      <h2>¿Qué está intentando entender?</h2>
      <p class="muted">Hipótesis pendientes de verificación — esperan respuesta de Diego para avanzar.</p>

      <div id="proximaAccionCerebro" style="margin-bottom:16px;border:1px solid #30363d;border-radius:8px;padding:14px;background:#0d1117">
        <div style="font-size:11px;font-weight:700;color:#d29922;margin-bottom:8px;">? SIGUIENTE VERIFICACIÓN</div>
        <div id="proximaAccionInner">-</div>
      </div>

      <div id="hipotesisActivas"></div>

      <div style="margin-top:16px;border-top:1px solid #30363d;padding-top:14px;">
        <h2>Hipotesis autonomas</h2>
        <p class="muted">Hipotesis generadas por el explorador y persistidas como piezas vivas del modelo mental.</p>
        <div id="hipotesisAutonomas"></div>
      </div>
    </div>
  </div>

  <!-- TAB: Conocimiento confirmado -->
  <div id="tab-conocimiento" class="tab-panel" style="display:none">
    <div class="card">
      <h2>¿Qué sabe? — Conocimiento operativo aprendido</h2>
      <p class="muted">Reglas, contextos y comportamientos de Ecotecnica que el modelo internalizó.</p>
      <div id="conocimientoConfirmado"></div>
    </div>
  </div>

  <!-- TAB: Inferencias -->
  <div id="tab-inferencias" class="tab-panel" style="display:none">
    <div class="card">
      <h2>¿Qué cree? — Inferencias vigentes</h2>
      <p class="muted">Observaciones activas derivadas de datos recientes. Alta confianza pero no validadas por Diego.</p>
      <div id="inferenciasVigentes"></div>
    </div>
  </div>

  <!-- TAB: Relaciones estructurales -->
  <div id="tab-relaciones" class="tab-panel" style="display:none">
    <div class="card">
      <h2>Relaciones</h2>
      <p class="muted">Primero aparecen las relaciones interesantes; las basicas quedan por debajo cuando no hay senales fuertes.</p>
      <div id="relacionesColumnas"></div>
    </div>
  </div>

  <!-- TAB: Verificaciones recientes -->
  <div id="tab-verificaciones" class="tab-panel" style="display:none">
    <div class="card">
      <h2>Historial de verificaciones</h2>
      <p class="muted">Hipótesis que ya fueron confirmadas o descartadas.</p>
      <div id="verificacionesRecientes"></div>
    </div>
  </div>

  <!-- -- PROTAGONISTAS OPERATIVOS -- -->
  <div class="section-title" style="margin-top:28px;">Protagonistas operativos (ingresos)</div>
  <div id="protagonistasOperativos" style="margin-bottom:16px;"></div>

  <!-- -- BIOGRAFIA PROTAGONISTAS -- -->
  <div class="section-title" style="margin-top:20px;">Biografía de protagonistas</div>
  <div id="biografiaProtagonistas" style="margin-bottom:16px;"></div>

  <!-- -- COLA DE CURIOSIDAD (contador) -- -->
  <div id="colaCuriosidad" style="margin-bottom:16px;font-size:12px;color:#8b949e;"></div>

  <!-- -- FICHAS OPERATIVAS RECIENTES -- -->
  <div class="section-title" style="margin-top:20px;">Fichas operativas recientes</div>
  <div id="fichasOperativas" style="margin-bottom:16px;"></div>

  <!-- -- MAPA HISTORICO PROVEEDORES -- -->
  <div class="section-title" style="margin-top:20px;">Mapa histórico proveedores</div>
  <div id="mapaHistoricoProveedores" style="margin-bottom:16px;"></div>

  <!-- -- MATERIALES NORMALIZADOS (mes actual) -- -->
  <div class="section-title" style="margin-top:20px;">Materiales normalizados (mes actual)</div>
  <div id="materialesNormalizados" style="margin-bottom:16px;"></div>

  <!-- -- ULTIMAS OBSERVACIONES UTILES (microtareas) -- -->
  <div class="section-title" style="margin-top:28px;">Últimas observaciones útiles</div>
  <div id="ultimasObservacionesUtiles" class="insight-grid"></div>

  <!-- -- RAZONAMIENTO + CONCLUSIONES -- -->
  <div class="section-title" style="margin-top:28px;">Señales interesantes</div>
  <div id="hallazgosInteresantes" class="insight-grid"></div>

  <div class="section-title" style="margin-top:28px;">Observaciones exploratorias</div>
  <div id="observacionesExploratorias" class="insight-grid"></div>

  <div class="section-title" style="margin-top:28px;">Proceso interno</div>
  <div class="grid2">
    <div class="card">
      <h2>Razonamiento en vivo</h2>
      <ul class="timeline" id="razonamientoReciente"></ul>
    </div>
    <div class="card">
      <h2>Conclusiones autónomas</h2>
      <div id="conclusionesAutonomas"></div>
    </div>
  </div>

  <!-- -- SIGUIENTE VERIFICACIÓN FLOTANTE -- -->
  <div class="section-title" style="margin-top:28px;">Siguiente acción del modelo</div>
  <div class="card" id="proximaAccionCard">
    <div id="proximaAccionMain">No hay verificaciones pendientes.</div>
  </div>

  <!-- -- TABLAS DERIVADAS -- -->
  <div class="section-title" style="margin-top:28px;">Resumen mensual verificado</div>
  <div class="grid3" id="tablasDerivadas">
    <div class="card">
      <h2>Producción por máquina <span class="muted" id="tdPeriodoMaq" style="font-size:11px;font-weight:400;"></span></h2>
      <ul id="tdMaquinas" style="list-style:none;margin:0;padding:0;font-size:13px;"></ul>
    </div>
    <div class="card">
      <h2>Tipos de movimiento <span class="muted" id="tdPeriodoTipo" style="font-size:11px;font-weight:400;"></span></h2>
      <ul id="tdTipos" style="list-style:none;margin:0;padding:0;font-size:13px;"></ul>
    </div>
    <div class="card">
      <h2>Ingresos por proveedor <span class="muted" id="tdPeriodoProv" style="font-size:11px;font-weight:400;"></span></h2>
      <ul id="tdProveedores" style="list-style:none;margin:0;padding:0;font-size:13px;"></ul>
    </div>
  </div>

  <!-- -- ESTADO PLANTA (compacto) -- -->
  <div class="section-title" style="margin-top:28px;">Estado operativo de planta</div>
  <div class="planta-row" id="estadoPlanta">
    <div class="planta-stat"><div class="val" id="prod">-</div><div class="lbl">Producción hoy</div></div>
    <div class="planta-stat"><div class="val" id="ing">-</div><div class="lbl">Ingresos hoy</div></div>
    <div class="planta-stat"><div class="val" id="prodMes">-</div><div class="lbl">Producción mes</div></div>
    <div class="planta-stat"><div class="val" id="ingMes">-</div><div class="lbl">Ingresos mes</div></div>
  </div>

  <details style="margin-top:12px;">
    <summary>Detalles planta: top producción, ingresos, conclusiones, inferencias operativas, dudas</summary>
    <div class="grid2" style="margin-top:14px;">
      <div class="card"><h2>Top producción</h2><ul id="topProd"></ul></div>
      <div class="card"><h2>Top ingresos</h2><ul id="topIng"></ul></div>
      <div class="card"><h2>Conclusiones IA modelo</h2><ul id="conclusionesModelo"></ul></div>
      <div class="card"><h2>Conclusiones por reglas</h2><ul id="conclusiones"></ul></div>
      <div class="card"><h2>Inferencias operativas</h2><ul id="inferenciasOper"></ul></div>
      <div class="card"><h2>Dudas del modelo</h2><div id="dudasModelo"></div></div>
      <div class="card"><h2>Preguntas sobre la base</h2><div id="preguntasEsquema"></div></div>
      <div class="card"><h2>Eventos detectados</h2><ul id="eventos"></ul></div>
      <div class="card"><h2>MQTT</h2><div>Total recibidos: <span id="mqtt">-</span></div></div>
    </div>
  </details>

</div><!-- /main -->

<script>
const borradoresTexto = {};
let _cerebro = null;

/* -- Tabs ---------------------------------- */
function switchTab(id, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).style.display = '';
  el.classList.add('active');
}

/* -- Helpers ------------------------------- */
function kg(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString("es-AR", {maximumFractionDigits:1}) + " kg";
}
function fmtNums(text) {
  return String(text ?? "").replace(/(^|[^\\w])([-+]?\\d{4,}(?:\\.\\d+)?)(?![\\w])/g, (match, pre, raw) => {
    const limpio = raw.replace(/^[-+]/, "");
    if (!limpio.includes(".") && limpio.length <= 4) return match;
    const n = Number(raw);
    if (!Number.isFinite(n)) return match;
    return pre + n.toLocaleString("es-AR", { maximumFractionDigits: Math.abs(n) >= 1000 ? 0 : 1 });
  });
}
function li(text) {
  const x = document.createElement("li"); x.textContent = text; return x;
}
function fechaCorta(v) {
  if (!v) return "-";
  return String(v).replace("T", " ").slice(0, 16);
}
function setMetric(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value ?? 0;
}
function hayTextoEnEdicion() {
  const el = document.activeElement;
  return el && el.tagName === "TEXTAREA";
}
function prepararTextarea(ta, key) {
  ta.value = borradoresTexto[key] || "";
  ta.addEventListener("input", () => { borradoresTexto[key] = ta.value; });
}
function descargarSnapshot() { window.location.href = "/api/ia/export"; }
function nivelInteresLabel(n) {
  if (n === "alta") return "alta";
  if (n === "media") return "media";
  return "baja";
}
function insightCard(x) {
  const nivel = nivelInteresLabel(x.nivel_interes);
  const box = document.createElement("div");
  box.className = "insight-card " + nivel;
  const head = document.createElement("div");
  head.className = "insight-head";
  const title = document.createElement("div");
  title.className = "insight-title";
  title.textContent = `${x.columna || "operacion"} · ${x.estado || "hallazgo"}`;
  head.appendChild(title);
  const pill = document.createElement("span");
  pill.className = "pill " + nivel;
  pill.textContent = `${nivel} · conf ${x.confianza ?? "-"}`;
  head.appendChild(pill);
  box.appendChild(head);
  if (x.firma_tematica) {
    const firma = document.createElement("div");
    firma.className = "insight-meta";
    firma.textContent = "Tema: " + x.firma_tematica.replaceAll("_", " ");
    box.appendChild(firma);
  }
  const txt = document.createElement("div");
  txt.className = "insight-text";
  txt.textContent = fmtNums(x.conclusion || x.hipotesis_refinada || x.regla_aprendible || "-");
  box.appendChild(txt);
  if (x.regla_aprendible) {
    const regla = document.createElement("div");
    regla.className = "insight-meta";
    regla.textContent = "Regla: " + x.regla_aprendible;
    box.appendChild(regla);
  }
  const meta = document.createElement("div");
  meta.className = "insight-meta";
  meta.textContent = `${x.hora || fechaCorta(x.fecha) || "-"} · ${x.id ? "id " + x.id : "memoria"}`;
  box.appendChild(meta);
  return box;
}

/* -- Timeline icon por tipo ---------------- */
const TL_ICONS = {
  hipotesis:          { cls:"hipotesis",    icon:"??" },
  conocimiento_aprendido: { cls:"conocimiento", icon:"?" },
  duda_generada:      { cls:"duda",         icon:"?" },
  duda_respondida:    { cls:"duda",         icon:"??" },
  verificacion_hipotesis: { cls:"verificacion", icon:"??" },
  conclusion_confirmada:  { cls:"conclusion",   icon:"??" },
  razonamiento:       { cls:"razonamiento", icon:"??" },
};

function tlItem(x) {
  const li = document.createElement("li");
  const ti = TL_ICONS[x.tipo] || { cls:"razonamiento", icon:"·" };
  const icon = document.createElement("div");
  icon.className = "tl-icon " + ti.cls;
  icon.textContent = ti.icon;
  li.appendChild(icon);
  const body = document.createElement("div");
  body.className = "tl-body";
  const hora = document.createElement("div");
  hora.className = "tl-hora";
  hora.textContent = x.hora || "--:--";
  body.appendChild(hora);
  const texto = document.createElement("div");
  texto.className = "tl-texto";
  texto.textContent = x.texto || "-";
  body.appendChild(texto);
  if (x.detalle) {
    const det = document.createElement("div");
    det.className = "tl-detalle";
    det.textContent = x.detalle;
    body.appendChild(det);
  }
  li.appendChild(body);
  return li;
}

/* -- Confianza color ----------------------- */
function confianzaColor(v) {
  const n = parseFloat(v) || 0;
  if (n >= 0.75) return "#3fb950";
  if (n >= 0.5)  return "#d29922";
  return "#f85149";
}

/* -- API calls ----------------------------- */
async function ejecutarCicloIA() {
  const rr = await fetch("/api/ia/ciclo", {method:"POST"});
  if (!rr.ok) { alert("Error ejecutando ciclo IA"); return; }
  renderCerebro(await rr.json());
}

async function enviarFeedbackHipotesis(id, accion, comentarioId) {
  const comentario = document.getElementById(comentarioId)?.value?.trim() || "";
  const rr = await fetch("/api/ia/feedback_hipotesis", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id, accion, comentario})
  });
  if (!rr.ok) { alert("Error guardando feedback de hipotesis"); return; }
  delete borradoresTexto[comentarioId];
  cargar();
}

/* -- Render hipotesis card ----------------- */
function hipCard(x, showFeedback) {
  const box = document.createElement("div");
  box.className = "hip-card";

  const badge = document.createElement("div");
  badge.className = "col-badge";
  badge.textContent = x.columna || "sin columna";
  box.appendChild(badge);

  const txt = document.createElement("div");
  txt.className = "hip-texto";
  txt.textContent = '\\"' + (x.hipotesis || "-") + '\\"';
  box.appendChild(txt);

  const meta = document.createElement("div");
  meta.className = "hip-meta";
  const conf = parseFloat(x.confianza) || 0;
  meta.textContent = `confianza: ${x.confianza ?? "-"} · estado: ${x.estado || "-"} · ${fechaCorta(x.creada_en)}`;
  box.appendChild(meta);

  const cbar = document.createElement("div");
  cbar.className = "confianza-bar";
  const cfill = document.createElement("div");
  cfill.className = "confianza-fill";
  cfill.style.width = (conf * 100) + "%";
  cfill.style.background = confianzaColor(conf);
  cbar.appendChild(cfill);
  box.appendChild(cbar);

  if (showFeedback) {
    const comentarioId = "feedbackHip_" + x.id;
    const ta = document.createElement("textarea");
    ta.rows = 2; ta.placeholder = "Verificación o comentario para enseñar al sistema...";
    prepararTextarea(ta, comentarioId);
    ta.id = comentarioId;
    box.appendChild(ta);
    const acts = document.createElement("div");
    acts.className = "actions";
    const conf_ = document.createElement("button");
    conf_.className = "btn-confirm"; conf_.textContent = "Confirmar";
    conf_.onclick = () => enviarFeedbackHipotesis(x.id, "confirmar", comentarioId);
    acts.appendChild(conf_);
    const desc = document.createElement("button");
    desc.className = "btn-discard"; desc.textContent = "Descartar";
    desc.onclick = () => enviarFeedbackHipotesis(x.id, "descartar", comentarioId);
    acts.appendChild(desc);
    box.appendChild(acts);
  }
  return box;
}

/* -- Render Cerebro ------------------------ */

function textoCorto(v, max=180) {
  const t = String(v || "").replace(/\\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1).trim() + "..." : t;
}
function primeraFrase(v, max=170) {
  const t = String(v || "").replace(/\\s+/g, " ").trim();
  if (!t) return "Sin descripcion clara todavia.";
  const m = t.match(/^(.+?[.!?])(?:\\s|$)/);
  return textoCorto((m && m[1]) || t, max);
}
function asArray(v) {
  return Array.isArray(v) ? v : [];
}
function safeSection(nombre, fn) {
  try { return fn(); }
  catch (err) { console.error("Error renderResumenModelo", err); return null; }
}
function metricasDe(c) {
  return (c && (c.metricas || c.metricas_cerebro)) || {};
}
function relacionTitulo(x) {
  return `${x.columna_a || "-"} -> ${x.columna_b || "-"}`;
}
function evidenciaTextoRelacion(x) {
  const piezas = [];
  const ev = x.evidencia || x.evidencia_json || null;
  const agregar = (v) => {
    const t = String(v || "").replace(/\\s+/g, " ").trim();
    if (t && !piezas.includes(t)) piezas.push(t);
  };
  if (ev && typeof ev === "object") {
    ["tipos_principales", "top_tipos", "por_tipo", "principales", "ejemplos"].forEach(k => {
      const v = ev[k];
      if (Array.isArray(v)) {
        v.slice(0, 4).forEach(it => {
          if (typeof it === "string") agregar(it);
          else if (it && typeof it === "object") {
            const nombre = it.tipo || it.Tipo || it.TiposDeMovimiento || it.valor || it.nombre || it.label;
            const pct = it.pct_presencia ?? it.porcentaje ?? it.pct ?? it.presencia_pct ?? it.presencia;
            agregar(nombre ? `${nombre}${pct !== undefined ? " " + fmtNums(pct) + "%" : ""}` : JSON.stringify(it));
          }
        });
      } else if (v && typeof v === "object") {
        Object.entries(v).slice(0, 4).forEach(([k2, val]) => agregar(`${k2} ${fmtNums(val)}`));
      }
    });
  }
  const desc = String(x.descripcion || "");
  const pctMatches = desc.match(/[^.;:]*\\b\\d+(?:[.,]\\d+)?\\s*%[^.;]*/g) || [];
  pctMatches.slice(0, 3).forEach(agregar);
  if (!piezas.length && desc) agregar(primeraFrase(desc, 130));
  return textoCorto(piezas.join(" · "), 220);
}
function datoUtilRelacion(x) {
  const titulo = relacionTitulo(x).toLowerCase();
  const desc = String(x.descripcion || "").toLowerCase();
  if (titulo.includes("precio")) return "Precio aparece principalmente asociado a movimientos economicos o de ingreso.";
  if (titulo.includes("proveedor")) return "Proveedor cambia de significado segun el tipo de movimiento.";
  if (titulo.includes("maquina") && titulo.includes("tiposdemovimiento")) return "Maquina aparece ligada a contextos operativos especificos.";
  if (titulo.includes("operador")) return "Operador aparece como señal de ejecucion operativa, especialmente en produccion.";
  if (titulo.includes("if")) return "IF aparece concentrado en algunos movimientos y pide investigacion de contexto.";
  if (titulo.includes("kilosnetos") || titulo.includes("descuento2")) return "La relacion entre kilos netos y descuentos puede señalar ajustes o datos incompletos.";
  if (desc.includes("concentr")) return primeraFrase(x.descripcion, 170);
  return primeraFrase(x.descripcion, 170);
}
function lecturaOperativaRelacion(x) {
  const titulo = relacionTitulo(x).toLowerCase();
  if (titulo.includes("precio")) return "Variable economica: conviene leerla como compra/ingreso, no como dato universal de produccion.";
  if (titulo.includes("proveedor")) return "No interpretarlo siempre como proveedor de compra; puede representar cliente, origen o destino.";
  if (titulo.includes("maquina")) return "Ayuda a separar puesto, linea o proceso cuando el movimiento es operativo.";
  if (titulo.includes("operador")) return "Sirve para entender responsabilidad o intervencion humana en movimientos productivos.";
  if (titulo.includes("if")) return "Mantener como señal a investigar antes de convertirla en regla del negocio.";
  if (titulo.includes("kilosnetos") || titulo.includes("descuento")) return "Revisar consistencia entre peso bruto, descuentos y neto antes de sacar conclusiones.";
  return "Relacion util para orientar preguntas del explorador y evitar interpretaciones genericas.";
}
function addRelSection(box, label, text, extraClass="") {
  const l = document.createElement("div");
  l.className = "rel-section-label";
  l.textContent = label;
  box.appendChild(l);
  const v = document.createElement("div");
  v.className = "rel-section-text" + (extraClass ? " " + extraClass : "");
  v.textContent = text || "-";
  box.appendChild(v);
}
function relacionCard(x, key=false) {
  x = x || {};
  const box = document.createElement("div");
  box.className = "rel-card" + (key ? " key" : "");
  box.title = [x.descripcion, evidenciaTextoRelacion(x), lecturaOperativaRelacion(x)].filter(Boolean).join("\\n");
  const title = document.createElement("div");
  title.className = "rel-title";
  title.textContent = relacionTitulo(x);
  box.appendChild(title);
  const meta = document.createElement("div");
  meta.className = "rel-meta-line";
  const nivel = x.nivel_interes || "-";
  meta.textContent = `Interes: ${nivel} · score ${x.interes_score ?? "-"} · confianza ${x.confianza ?? "-"} · ${x.categoria || "-"}`;
  box.appendChild(meta);
  if (key) {
    addRelSection(box, "Dato util", datoUtilRelacion(x));
    addRelSection(box, "Evidencia", evidenciaTextoRelacion(x), "evidence");
    addRelSection(box, "Lectura operativa", lecturaOperativaRelacion(x));
  } else {
    const txt = document.createElement("div");
    txt.className = "texto rel-section-text";
    txt.textContent = fmtNums(textoCorto(x.descripcion || "-", 260));
    box.appendChild(txt);
  }
  return box;
}
function textoUtil(text) {
  const t = String(text || "").replace(/\\s+/g, " ").trim();
  if (t.length < 18) return false;
  const ruido = /(se ha revisado|no se encontraron patrones|no se encontro|sin patrones|ranking|top\\s+\\d|mas frecuente|más frecuente|principales proveedores|tipo mas frecuente|tipo más frecuente)/i;
  return !ruido.test(t);
}
function signalCard(kind, text, meta, cls="") {
  const box = document.createElement("div");
  box.className = "signal-card " + cls;
  const k = document.createElement("div");
  k.className = "signal-kind";
  k.textContent = kind;
  box.appendChild(k);
  const t = document.createElement("div");
  t.className = "signal-text";
  t.textContent = textoCorto(fmtNums(text || "-"), 260);
  t.title = text || "";
  box.appendChild(t);
  if (meta) {
    const m = document.createElement("div");
    m.className = "signal-meta";
    m.textContent = meta;
    box.appendChild(m);
  }
  return box;
}
function renderCards(containerId, cards, emptyText) {
  const cont = document.getElementById(containerId);
  if (!cont) return;
  const lista = asArray(cards);
  cont.innerHTML = "";
  if (!lista.length) {
    const p = document.createElement("p"); p.className = "empty"; p.textContent = emptyText;
    cont.appendChild(p); return;
  }
  lista.forEach(x => cont.appendChild(x instanceof Node ? x : signalCard("Señal", x, "")));
}
function compactLi(text, meta) {
  if (text instanceof Node) return text;
  const item = document.createElement("li");
  item.textContent = textoCorto(fmtNums(text ?? "-"), 150);
  if (meta) {
    const m = document.createElement("span");
    m.className = "compact-meta";
    m.textContent = String(meta);
    item.appendChild(m);
  }
  return item;
}
function fillCompactList(id, items, emptyText) {
  const ul = document.getElementById(id);
  if (!ul) return;
  const lista = asArray(items);
  ul.innerHTML = "";
  if (!lista.length) {
    ul.appendChild(compactLi(emptyText || "Sin datos todavia", ""));
    return;
  }
  lista.forEach(x => ul.appendChild(x instanceof Node ? x : compactLi(x, "")));
}
function actividadPeso(x) {
  const tipo = String(x.tipo || "");
  const pesos = {
    relaciones_interesantes: 100,
    observacion_exploratoria: 97,
    explorador_persistida: 96,
    conocimiento_aprendido: 94,
    conclusion_confirmada: 92,
    hipotesis_autonoma: 90,
    explorador_hipotesis: 88,
    observador_operativo: 84,
    relacion_columna: 78,
    hipotesis: 70,
    duda_generada: 62,
    verificacion_hipotesis: 58,
    explorador_limitacion_datos: 56,
    explorador_sin_conclusion: 45,
    explorador_error_parse: 42,
    explorador_sin_query: 40,
  };
  if (["explorador_consulta_modelo", "explorador_inicio", "observacion", "autonomia", "telegram_inicio"].includes(tipo)) return -10;
  return pesos[tipo] ?? 24;
}
function actividadFiltrada(items) {
  return (items || [])
    .filter(x => actividadPeso(x) >= 30)
    .sort((a,b) => (actividadPeso(b) - actividadPeso(a)) || String(b.fecha || "").localeCompare(String(a.fecha || "")));
}
function renderContextoTemporal(c) {
  const t = (c && c.contexto_temporal) || {};
  const items = [
    compactLi(`Hoy: ${t.fecha_hoy || "-"}`, `ahora ${t.ahora_local || "-"}`),
    compactLi(`Mes actual: ${t.mes_actual_nombre || "-"} ${t.anio_actual || ""}`, t.mes_actual_abierto ? "abierto/parcial" : "cerrado"),
    compactLi("Mes anterior", t.mes_anterior_cerrado ? "cerrado/confiable" : "abierto"),
    compactLi(`Datos hasta: ${t.datos_actualizados_hasta || "sin corte"}`, `estado ${t.estado_actualizacion || "-"}`),
    compactLi("Atraso estimado", `${t.atraso_minutos ?? "-"} min`),
  ];
  fillCompactList("contextoTemporalOperativo", items, "Sin contexto temporal disponible");
}
function renderMapaEstructural(c) {
  const mapa = (c && c.mapa_estructural) || {};
  const colPct = (x) => `${x.columna || "-"}: ${x.pct_cargado ?? "-"}%`;
  fillCompactList("mapaColumnasCargadas", asArray(mapa.columnas_mayor_presencia).slice(0, 6).map(x => compactLi(colPct(x), `${x.tipo_dato || ""} · distintos ${x.distinct_count ?? "-"}`)), "Sin columnas perfiladas");
  fillCompactList("mapaColumnasRaras", asArray(mapa.columnas_raras).slice(0, 6).map(x => compactLi(colPct(x), `${x.tipo_dato || ""} · distintos ${x.distinct_count ?? "-"}`)), "Sin rarezas perfiladas");
  fillCompactList("mapaTiposMovimiento", asArray(mapa.tipos_movimiento).slice(0, 6).map(x => compactLi(x.tipo_movimiento || "-", `${x.registros ?? "-"} mov · ${kg(x.suma_kilos)}`)), "Sin tipos perfilados");
  fillCompactList("mapaCoPresencias", asArray(mapa.co_presencias_fuertes).slice(0, 6).map(x => compactLi(`${x.columna_a || "-"} -> ${x.columna_b || "-"}`, `score ${x.interes_score ?? "-"} · conf ${x.confianza ?? "-"}`)), "Sin co-presencias calculadas");
  fillCompactList("mapaPendientes", asArray(mapa.pendientes).slice(0, 6).map(x => compactLi(x.tema || x.firma || "-", `prioridad ${x.prioridad ?? "-"}`)), "Sin pendientes estructurales");
}
function renderMapaMental(c) {
  const m = metricasDe(c);
  const cards = [
    [`${m.columnas_comprendidas ?? 0}/${m.columnas_exploradas ?? 0}`, "Columnas base comprendidas", "Fase 1"],
    [m.columnas_esquema_total ?? 0, "Columnas del esquema", "TiposDeMovimiento"],
    [`${m.columnas_diccionario ?? 0}/${m.columnas_esquema_total ?? 0}`, "Columnas interpretadas", "Diccionario IA"],
    [m.columnas_con_relaciones ?? 0, "Columnas vinculadas", "Aparecen en relaciones"],
    [m.relaciones_estructurales ?? m.relaciones_columnas ?? 0, "Relaciones estructurales", "Base completa de cruces"],
    [m.relaciones_interesantes ?? 0, "Relaciones interesantes", "Priorizadas por contraste"],
    [m.hallazgos_interesantes ?? 0, "Señales operativas", `${m.hallazgos_alta_prioridad ?? 0} alta prioridad`],
    [m.observaciones_exploratorias ?? 0, "Observaciones exploratorias", "Piezas chicas guardadas"],
    [m.conocimiento_operativo_aprendido ?? 0, "Conocimiento confirmado", "Validado o persistido"],
  ];
  const cont = document.getElementById("mapaMentalActual");
  if (!cont) return;
  cont.innerHTML = "";
  cards.forEach(([num, label, hint]) => {
    const card = document.createElement("div"); card.className = "mental-card";
    const n = document.createElement("div"); n.className = "num"; n.textContent = num ?? 0; card.appendChild(n);
    const l = document.createElement("div"); l.className = "lbl"; l.textContent = label; card.appendChild(l);
    const h = document.createElement("div"); h.className = "hint"; h.textContent = hint; card.appendChild(h);
    cont.appendChild(card);
  });
}
function renderLecturaRapida(c) {
  const rels = asArray(c && c.relaciones_interesantes);
  const bullets = [];
  const tieneRel = (pat) => rels.some(r => relacionTitulo(r).toLowerCase().includes(pat));
  if (tieneRel("precio")) bullets.push("Precio se concentra en movimientos de ingreso o compra; no conviene leerlo como variable de produccion.");
  if (tieneRel("proveedor")) bullets.push("Proveedor no siempre significa proveedor de compra: cambia segun el tipo de movimiento.");
  if (tieneRel("maquina") || tieneRel("operador")) bullets.push("Maquina y Operador funcionan como señales de proceso operativo, especialmente en produccion.");
  if (tieneRel("kilosnetos") || tieneRel("descuento2")) bullets.push("kilosNetos y descuentos requieren lectura cuidadosa antes de usarlos como peso final confiable.");
  const pulso = asArray(c && c.actividad_reciente).find(x => x.tipo === "observador_operativo" && textoUtil(x.detalle || x.texto));
  if (pulso) bullets.push(textoCorto(pulso.detalle || pulso.texto, 150));
  asArray(c && c.hallazgos_interesantes).forEach(x => {
    const t = x.conclusion || x.hipotesis_refinada || x.regla_aprendible;
    if (bullets.length < 5 && textoUtil(t)) bullets.push(textoCorto(t, 150));
  });
  const unicos = [...new Set(bullets)].slice(0, 5);
  const ul = document.getElementById("lecturaRapida");
  if (!ul) return;
  ul.innerHTML = "";
  if (!unicos.length) ul.appendChild(compactLi("Todavia no hay señales claras para lectura rapida.", ""));
  unicos.forEach(t => { const li = document.createElement("li"); li.textContent = fmtNums(t); ul.appendChild(li); });
}
function construirSenalesUtiles(c) {
  const cards = [];
  asArray(c && c.hallazgos_interesantes).slice(0, 4).forEach(x => {
    const t = x.conclusion || x.hipotesis_refinada || x.regla_aprendible;
    if (textoUtil(t)) cards.push(signalCard("Hallazgo IA", t, `${x.nivel_interes || "-"} · conf ${x.confianza ?? "-"}`, "operativo"));
  });
  asArray(c && c.relaciones_interesantes).slice().sort((a,b) => Number(b.interes_score || 0) - Number(a.interes_score || 0)).slice(0, 3).forEach(x => {
    cards.push(signalCard("Relacion", `${relacionTitulo(x)}: ${datoUtilRelacion(x)}`, `score ${x.interes_score ?? "-"} · conf ${x.confianza ?? "-"}`, "relacion"));
  });
  actividadFiltrada(asArray(c && c.actividad_reciente)).filter(x => x.tipo === "observador_operativo").slice(0, 2).forEach(x => {
    const t = x.detalle || x.texto;
    if (textoUtil(t)) cards.push(signalCard("Pulso operativo", t, x.hora || "", "operativo"));
  });
  asArray(c && c.conclusiones_autonomas).slice(0, 6).forEach(x => {
    const t = x.conclusion || x.regla_aprendible || x.hipotesis_refinada;
    const estado = String(x.estado || "").toLowerCase();
    const util = ["observacion_operativa", "relacion_estructural", "confirmada_autonoma", "persistida", "aprendida"].some(s => estado.includes(s));
    if ((util || x.nivel_interes === "alta" || x.nivel_interes === "media") && textoUtil(t)) {
      cards.push(signalCard("Aprendizaje", t, `${x.estado || "aprendizaje"} · conf ${x.confianza ?? "-"}`, "hipotesis"));
    }
  });
  return cards.slice(0, 8);
}
function renderLimitaciones(c) {
  const tipos = ["explorador_limitacion_datos", "explorador_sin_conclusion", "explorador_error_parse", "explorador_sin_query"];
  const items = asArray(c && c.razonamiento_reciente).filter(x => tipos.includes(String(x.tipo || ""))).slice(0, 6);
  const card = document.getElementById("limitacionesCard");
  const cont = document.getElementById("limitacionesDetectadas");
  if (!card || !cont) return;
  cont.innerHTML = "";
  if (!items.length) { card.style.display = "none"; return; }
  card.style.display = "";
  items.forEach(x => cont.appendChild(signalCard("Limitacion", x.detalle || x.texto || x.tipo, `${x.hora || ""} · ${x.tipo || ""}`, "limitacion")));
}
function renderResumenModelo(c) {
  c = c || {};
  safeSection("lectura", () => renderLecturaRapida(c));
  safeSection("contextoTemporal", () => renderContextoTemporal(c));
  safeSection("mapa", () => { renderMapaMental(c); renderMapaEstructural(c); });
  safeSection("senales", () => renderCards("senalesUtiles", construirSenalesUtiles(c), "Todavia no hay señales utiles filtradas."));
  safeSection("limitaciones", () => renderLimitaciones(c));
  safeSection("relaciones", () => {
    const rels = asArray(c.relaciones_interesantes).slice().sort((a,b) => Number(b.interes_score || 0) - Number(a.interes_score || 0)).slice(0, 8);
    const relCont = document.getElementById("relacionesClave");
    if (!relCont) return;
    relCont.innerHTML = "";
    if (!rels.length) {
      const p = document.createElement("p"); p.className = "empty"; p.textContent = "Todavia no hay relaciones interesantes."; relCont.appendChild(p);
    } else {
      rels.forEach(x => relCont.appendChild(relacionCard(x, true)));
    }
  });
  safeSection("hallazgos", () => {
    const hallazgos = asArray(c.hallazgos_interesantes)
      .filter(x => textoUtil(x.conclusion || x.hipotesis_refinada || x.regla_aprendible))
      .slice(0, 5)
      .map(x => compactLi(x.conclusion || x.hipotesis_refinada || x.regla_aprendible, `${x.nivel_interes || "-"} · conf ${x.confianza ?? "-"}`));
    fillCompactList("resumenHallazgos", hallazgos, "Sin hallazgos IA visibles");
  });
  safeSection("pulso", () => {
    const actividad = actividadFiltrada(asArray(c.actividad_reciente));
    const pulso = actividad.filter(x => ["observador_operativo", "relaciones_interesantes", "explorador_persistida", "explorador_hipotesis"].includes(x.tipo)).slice(0, 5);
    fillCompactList("resumenPulso", pulso.map(x => compactLi(x.detalle || x.texto, `${x.hora || ""} · ${x.tipo || ""}`)), "Sin pulso operativo reciente");
  });
  safeSection("aprendizajes", () => {
    const aprend = [];
    asArray(c.hipotesis_autonomas).slice(0, 2).forEach(x => { if (textoUtil(x.hipotesis)) aprend.push(compactLi(x.hipotesis, `hipotesis ${x.estado || "abierta"} · conf ${x.confianza ?? "-"}`)); });
    asArray(c.conocimiento_confirmado).slice(0, 3).forEach(x => aprend.push(compactLi(x.texto_resumido || x.texto, `conocimiento · ${fechaCorta(x.fecha)}`)));
    asArray(c.conclusiones_autonomas).filter(x => textoUtil(x.conclusion || x.regla_aprendible)).slice(0, 3).forEach(x => aprend.push(compactLi(x.conclusion || x.regla_aprendible, `${x.estado || "aprendizaje"} · conf ${x.confianza ?? "-"}`)));
    fillCompactList("resumenAprendizajes", aprend.slice(0, 5), "Sin aprendizajes recientes");
  });
  safeSection("observaciones", () => {
    const obs = asArray(c.observaciones_exploratorias)
      .filter(x => textoUtil(x.conclusion))
      .slice(0, 4)
      .map(x => compactLi(x.conclusion, `${x.columna || "exploracion"} · conf ${x.confianza ?? "-"}`));
    fillCompactList("resumenObservaciones", obs, "Sin observaciones exploratorias recientes");
  });
}
function renderTablasDerivadas(td) {
  td = td || {};
  const periodos = td.periodos || [];
  const ref = periodos.length ? periodos[0] : null;
  const label = ref ? `${ref.periodo} (${ref.estado})` : "";

  function fillDerivadasList(ulId, rows, keyName) {
    const ul = document.getElementById(ulId);
    if (!ul) return;
    ul.innerHTML = "";
    if (!rows || !rows.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "Sin datos aún";
      ul.appendChild(li);
      return;
    }
    rows.forEach(r => {
      const li = document.createElement("li");
      li.style.cssText = "display:flex;justify-content:space-between;align-items:baseline;padding:3px 0;border-bottom:1px solid #21262d";
      const name = document.createElement("span");
      name.textContent = r[keyName] || "-";
      name.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%";
      const vals = document.createElement("span");
      vals.style.cssText = "color:#8b949e;font-size:12px;white-space:nowrap;margin-left:6px";
      const kTotal = Number(r.kilos_total || 0);
      vals.textContent = kTotal >= 1000
        ? kTotal.toLocaleString("es-AR", {maximumFractionDigits:0}) + " kg"
        : kTotal.toLocaleString("es-AR", {maximumFractionDigits:1}) + " kg";
      li.appendChild(name);
      li.appendChild(vals);
      ul.appendChild(li);
    });
  }

  ["tdPeriodoMaq","tdPeriodoTipo","tdPeriodoProv"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = label;
  });
  fillDerivadasList("tdMaquinas", td.muestra_produccion_maquina, "Maquina");
  fillDerivadasList("tdTipos", td.muestra_tipo_movimiento, "TiposDeMovimiento");
  fillDerivadasList("tdProveedores", td.muestra_ingresos_proveedor, "Proveedor");
}

/* ── Helpers de tiempo y badges ─────────────────────────── */
function horaRelativa(fechaStr) {
  if (!fechaStr) return null;
  try {
    const d = new Date(String(fechaStr).replace(" ", "T"));
    if (isNaN(d)) return null;
    const diff = Math.round((Date.now() - d.getTime()) / 1000);
    if (diff < 60)   return "hace " + diff + "s";
    if (diff < 3600) return "hace " + Math.round(diff/60) + " min";
    if (diff < 86400)return "hace " + Math.round(diff/3600) + " h";
    return "hace " + Math.round(diff/86400) + " d";
  } catch(e) { return null; }
}
function edadDias(fechaStr) {
  if (!fechaStr) return null;
  try {
    const d = new Date(String(fechaStr).replace(" ", "T"));
    if (isNaN(d)) return null;
    return Math.floor((Date.now() - d.getTime()) / 86400000);
  } catch(e) { return null; }
}
function badgeEdad(fechaStr) {
  const dias = edadDias(fechaStr);
  if (dias === null) return "";
  if (dias === 0 && edadDias(fechaStr) !== null) {
    const diff = Math.round((Date.now() - new Date(String(fechaStr).replace(" ","T")).getTime()) / 1000);
    if (diff < 1800) return '<span class="badge reciente">reciente</span>';
    return '<span class="badge hoy">hoy</span>';
  }
  if (dias <= 1) return '<span class="badge hoy">hoy</span>';
  if (dias > 7)  return '<span class="badge historico">histórico</span>';
  return "";
}
function tipoBadge(tipo) {
  const t = String(tipo || "");
  if (t.startsWith("microtarea")) return '<span class="badge microtarea">microtarea</span>';
  if (["observador_operativo","observacion_operativa","tablas_derivadas"].includes(t)) return '<span class="badge pulso">pulso</span>';
  if (["pendiente_estructural","cartografia_base","relaciones_interesantes"].includes(t)) return '<span class="badge estructural">estructural</span>';
  return "";
}

/* -- Tipos de ruido visual: colapsados/apagados ----------- */
const TIPOS_RUIDO = new Set([
  "telegram_inicio","explorador_inicio","autonomia_inicio","microtarea_cooldown",
  "microtarea_sin_pendientes","explorador_consulta_modelo","cartografia_base_nochange"
]);
const TIPOS_UTILES = new Set([
  "observacion_guardada","microtarea_inicio","microtarea_prompt_preparado","microtarea_resultado",
  "microtarea_error","tablas_derivadas","observador_operativo","cartografia_base",
  "pendiente_estructural","observacion_exploratoria","explorador_hipotesis","relaciones_interesantes"
]);

/* -- Razonamiento en vivo agrupado ----------------------- */
function renderRazonamientoAgrupado(items) {
  const razUl = document.getElementById("razonamientoReciente");
  if (!razUl) return;
  razUl.innerHTML = "";
  const ahora = Date.now();
  const grupos = { reciente: [], hoy: [], anteriores: [] };
  asArray(items).forEach(x => {
    try {
      const d = new Date(String(x.hora || x.ts || "").replace(" ","T"));
      const diff = isNaN(d) ? Infinity : (ahora - d.getTime()) / 1000;
      if (diff < 1800)       grupos.reciente.push(x);
      else if (diff < 86400) grupos.hoy.push(x);
      else                   grupos.anteriores.push(x);
    } catch(e) { grupos.anteriores.push(x); }
  });

  function addGrupo(label, lista) {
    if (!lista.length) return;
    const lbl = document.createElement("div");
    lbl.className = "tl-group-label";
    lbl.textContent = label;
    razUl.appendChild(lbl);

    const utiles = lista.filter(x => !TIPOS_RUIDO.has(String(x.tipo||"")));
    const ruido  = lista.filter(x =>  TIPOS_RUIDO.has(String(x.tipo||"")));

    utiles.forEach(x => {
      const li = tlItem({...x, tipo: TIPOS_UTILES.has(String(x.tipo||"")) ? "conocimiento" : "razonamiento",
        texto: `${x.tipo || "paso"}: ${x.texto || "-"}`});
      const badges = tipoBadge(x.tipo);
      if (badges) {
        const span = document.createElement("span");
        span.innerHTML = badges;
        li.querySelector(".tl-texto")?.appendChild(span.firstChild);
      }
      razUl.appendChild(li);
    });

    if (ruido.length) {
      const toggle = document.createElement("span");
      toggle.className = "tl-noise-toggle";
      toggle.textContent = `▸ ${ruido.length} evento${ruido.length>1?"s":""} técnico${ruido.length>1?"s":""} (click para ver)`;
      let visible = false;
      const noiseItems = ruido.map(x => {
        const li = tlItem({...x, tipo:"razonamiento",
          texto: `${x.tipo||"paso"}: ${x.texto||"-"}`});
        li.classList.add("noise");
        li.style.display = "none";
        razUl.appendChild(li);
        return li;
      });
      toggle.addEventListener("click", () => {
        visible = !visible;
        noiseItems.forEach(el => el.style.display = visible ? "" : "none");
        toggle.textContent = visible
          ? `▾ ${ruido.length} evento${ruido.length>1?"s":""} técnico${ruido.length>1?"s":""} (ocultar)`
          : `▸ ${ruido.length} evento${ruido.length>1?"s":""} técnico${ruido.length>1?"s":""} (click para ver)`;
      });
      razUl.insertBefore(toggle, noiseItems[0]);
    }
  }

  addGrupo("Ahora · últimos 30 min", grupos.reciente);
  addGrupo("Hoy", grupos.hoy);
  addGrupo("Anteriores", grupos.anteriores);
  if (!items.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Sin actividad registrada aún.";
    razUl.appendChild(p);
  }
}

/* -- Protagonistas operativos ----------------------------- */
function renderProtagonistas(po) {
  const cont = document.getElementById("protagonistasOperativos");
  if (!cont) return;
  cont.innerHTML = "";
  if (!po) return;
  const nivel1 = asArray(po.nivel1);
  const nivel2map = po.nivel2_por_proveedor || {};
  if (!nivel1.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Sin protagonistas aún. Ejecutar recalcular derivadas.";
    cont.appendChild(p);
    return;
  }

  function fmtKg(v) { return v != null ? Number(v).toLocaleString("es-AR") + " kg" : "-"; }
  function fmtPct(v) { return v != null ? Number(v).toFixed(1) + "%" : "-"; }

  const card = document.createElement("div"); card.className = "card";
  card.style.cssText = "padding:14px;";
  cont.appendChild(card);

  const hdr = document.createElement("h2");
  hdr.textContent = nivel1.length ? (nivel1[0].origen_entidad || "Ingresos del período") : "Ingresos";
  card.appendChild(hdr);

  const tree = document.createElement("div");
  tree.style.cssText = "margin-top:8px;font-size:12px;";
  card.appendChild(tree);

  nivel1.forEach((prov, idx) => {
    const nombre = prov.proveedor || prov.entidad || "?";
    const children = asArray(nivel2map[nombre]);

    const rowProv = document.createElement("div");
    rowProv.style.cssText =
      "display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:4px;" +
      (idx % 2 === 0 ? "background:rgba(255,255,255,.04);" : "");

    const icon = document.createElement("span");
    icon.textContent = children.length ? "▶" : "·";
    icon.style.cssText = "color:#58a6ff;font-size:10px;min-width:10px;cursor:" + (children.length ? "pointer" : "default");

    const nameEl = document.createElement("span");
    nameEl.textContent = nombre;
    nameEl.style.cssText = "flex:1;color:#e6edf3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    nameEl.title = nombre;

    const pctEl = document.createElement("span");
    pctEl.textContent = fmtPct(prov.pct_del_contexto);
    pctEl.style.cssText = "color:#58a6ff;min-width:48px;text-align:right;font-weight:600;";

    const kgEl = document.createElement("span");
    kgEl.textContent = fmtKg(prov.kg_entidad);
    kgEl.style.cssText = "color:#8b949e;min-width:90px;text-align:right;";

    rowProv.appendChild(icon);
    rowProv.appendChild(nameEl);
    rowProv.appendChild(pctEl);
    rowProv.appendChild(kgEl);
    tree.appendChild(rowProv);

    if (children.length) {
      const childWrap = document.createElement("div");
      childWrap.style.cssText = "display:none;margin-left:18px;border-left:1px solid rgba(88,166,255,.2);padding-left:8px;";
      children.forEach((mat, mi) => {
        const rowMat = document.createElement("div");
        rowMat.style.cssText =
          "display:flex;gap:8px;padding:3px 6px;border-radius:3px;" +
          (mi % 2 === 0 ? "background:rgba(255,255,255,.03);" : "");
        const matName = document.createElement("span");
        matName.textContent = mat.material || mat.entidad || "?";
        matName.style.cssText = "flex:1;color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        matName.title = mat.material || mat.entidad || "";
        const matPct = document.createElement("span");
        matPct.textContent = fmtPct(mat.pct_del_contexto);
        matPct.style.cssText = "color:#3fb950;min-width:48px;text-align:right;";
        const matKg = document.createElement("span");
        matKg.textContent = fmtKg(mat.kg_entidad);
        matKg.style.cssText = "color:#8b949e;min-width:90px;text-align:right;";
        rowMat.appendChild(matName);
        rowMat.appendChild(matPct);
        rowMat.appendChild(matKg);
        childWrap.appendChild(rowMat);
      });
      tree.appendChild(childWrap);

      icon.addEventListener("click", () => {
        const open = childWrap.style.display !== "none";
        childWrap.style.display = open ? "none" : "block";
        icon.textContent = open ? "▶" : "▼";
      });
    }
  });

  if (po.actualizado_en) {
    const ts = document.createElement("div");
    ts.style.cssText = "margin-top:8px;font-size:10px;color:#484f58;text-align:right;";
    ts.textContent = "Actualizado: " + po.actualizado_en;
    card.appendChild(ts);
  }
}

/* -- Biografía de protagonistas ----------------------------- */
function renderBiografiaProtagonistas(bio) {
  const cont = document.getElementById("biografiaProtagonistas");
  if (!cont) return;
  cont.innerHTML = "";
  if (!bio) return;
  const provs = asArray(bio.proveedores);
  const mats  = asArray(bio.materiales);
  if (!provs.length && !mats.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Sin biografías disponibles. Ejecutar recalcular derivadas.";
    cont.appendChild(p);
    return;
  }

  function fmtKg(v) { return v != null ? Number(v).toLocaleString("es-AR") + " kg" : "-"; }
  function fmtPct(v) { return v != null ? Number(v).toFixed(1) + "%" : "-"; }

  const CONF_COLOR = {fuerte:"#3fb950", media:"#d29922", debil:"#f85149", antecedente_unico:"#8b949e", sin_historia:"#484f58"};

  function ficha(r, esProveedor) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:10px 12px;margin-bottom:8px;background:rgba(255,255,255,.03);";

    const titulo = document.createElement("div");
    titulo.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:6px;";
    const nombre = document.createElement("span");
    const provNombre = r.proveedor || (esProveedor ? r.entidad : r.entidad_2);
    const matNombre = r.material || (esProveedor ? null : r.entidad);
    nombre.textContent = r.etiqueta || (esProveedor ? provNombre : (provNombre + " → " + matNombre));
    nombre.style.cssText = "font-weight:600;color:#e6edf3;flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    nombre.title = nombre.textContent;
    const badge = document.createElement("span");
    badge.textContent = r.confiabilidad || "?";
    badge.style.cssText = `font-size:10px;padding:1px 6px;border-radius:3px;background:rgba(0,0,0,.3);color:${CONF_COLOR[r.confiabilidad]||"#8b949e"};white-space:nowrap;`;
    titulo.appendChild(nombre);
    titulo.appendChild(badge);
    wrap.appendChild(titulo);

    const grid = document.createElement("div");
    grid.style.cssText = "display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 8px;font-size:11px;color:#8b949e;";
    const cells = [
      ["Actual", fmtKg(r.kg_actual)],
      ["% ctx actual", fmtPct(r.pct_actual_contexto)],
      ["Meses activos", r.meses_con_actividad || 0],
      ["Prom. hist.", fmtKg(r.kg_promedio_meses_activos)],
      ["% ctx prom.", fmtPct(r.pct_promedio_meses_activos)],
      ["Último mes", r.ultimo_mes_con_actividad || "-"],
    ];
    cells.forEach(([lbl, val]) => {
      const c = document.createElement("div");
      c.innerHTML = `<span style="color:#484f58;">${lbl}</span><br><span style="color:#c9d1d9;">${val}</span>`;
      grid.appendChild(c);
    });
    wrap.appendChild(grid);

    if (r.lectura_backend) {
      const lec = document.createElement("div");
      lec.style.cssText = "margin-top:6px;font-size:11px;color:#8b949e;font-style:italic;border-top:1px solid rgba(255,255,255,.06);padding-top:5px;";
      lec.textContent = r.lectura_backend;
      wrap.appendChild(lec);
    }
    return wrap;
  }

  const grid2 = document.createElement("div");
  grid2.style.cssText = "display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;";
  cont.appendChild(grid2);

  if (provs.length) {
    const col = document.createElement("div");
    const h = document.createElement("div");
    h.style.cssText = "font-size:11px;font-weight:600;color:#58a6ff;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;";
    h.textContent = "Proveedores";
    col.appendChild(h);
    provs.forEach(r => col.appendChild(ficha(r, true)));
    grid2.appendChild(col);
  }

  if (mats.length) {
    const col = document.createElement("div");
    const h = document.createElement("div");
    h.style.cssText = "font-size:11px;font-weight:600;color:#3fb950;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;";
    h.textContent = "Materiales";
    col.appendChild(h);
    mats.forEach(r => col.appendChild(ficha(r, false)));
    grid2.appendChild(col);
  }

  if (bio.actualizado_en) {
    const ts = document.createElement("div");
    ts.style.cssText = "margin-top:6px;font-size:10px;color:#484f58;text-align:right;";
    ts.textContent = "Actualizado: " + bio.actualizado_en;
    cont.appendChild(ts);
  }
}

/* -- Materiales normalizados del mes (DescriptionNormalizada) -- */
function renderMaterialesNormalizados(mn) {
  const cont = document.getElementById("materialesNormalizados");
  if (!cont) return;
  cont.innerHTML = "";
  if (!mn || !mn.disponible || !asArray(mn.top_materiales).length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Sin datos de DescriptionNormalizada todavía. Ejecutar recalcular derivadas.";
    cont.appendChild(p);
    return;
  }

  const CONF_COLOR = {fuerte: "#3fb950", parcial: "#d29922", debil: "#f85149"};
  const conf = mn.estado_confianza || "debil";
  const head = document.createElement("div");
  head.style.cssText = "font-size:11px;color:#8b949e;margin-bottom:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;";
  const badge = document.createElement("span");
  badge.textContent = "cobertura " + (mn.cobertura_pct != null ? Number(mn.cobertura_pct).toFixed(1) : "?") + "% · " + conf;
  badge.style.cssText = "font-size:10px;padding:1px 8px;border-radius:3px;background:rgba(0,0,0,.3);color:" + (CONF_COLOR[conf] || "#8b949e") + ";";
  head.appendChild(badge);
  const detalle = document.createElement("span");
  detalle.textContent = mn.registros_normalizados_periodo + "/" + mn.registros_total_periodo +
    " registros normalizados · " + Number(mn.pct_kilos_normalizados || 0).toFixed(1) + "% de los kilos";
  head.appendChild(detalle);
  cont.appendChild(head);

  if (conf === "debil") {
    const warn = document.createElement("div");
    warn.style.cssText = "font-size:10px;color:#f85149;margin-bottom:6px;";
    warn.textContent = "Cobertura baja (<30%): no usar para conclusiones fuertes.";
    cont.appendChild(warn);
  }

  const tabla = document.createElement("div");
  tabla.style.cssText = "font-size:12px;";
  asArray(mn.top_materiales).forEach((m, idx) => {
    const row = document.createElement("div");
    row.style.cssText =
      "display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:4px;" +
      (idx % 2 === 0 ? "background:rgba(255,255,255,.04);" : "");
    const nombre = document.createElement("span");
    nombre.textContent = m.material || "?";
    nombre.style.cssText = "flex:1;color:#e6edf3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    nombre.title = (m.material || "") + (m.ultimo_movimiento ? " · último: " + m.ultimo_movimiento : "");
    const kg = document.createElement("span");
    kg.textContent = Number(m.kilos_total || 0).toLocaleString("es-AR") + " kg";
    kg.style.cssText = "color:#58a6ff;min-width:90px;text-align:right;font-weight:600;";
    const regs = document.createElement("span");
    regs.textContent = (m.registros || 0) + " reg";
    regs.style.cssText = "color:#8b949e;min-width:56px;text-align:right;";
    row.appendChild(nombre);
    row.appendChild(kg);
    row.appendChild(regs);
    tabla.appendChild(row);
  });
  cont.appendChild(tabla);
}

/* -- Cola de curiosidad (contador simple) ------------------- */
function renderColaCuriosidad(cc) {
  const el = document.getElementById("colaCuriosidad");
  if (!el) return;
  const n = (cc && cc.pendientes) || 0;
  el.textContent = "Cola de curiosidad: " + n + " pendientes.";
}

/* -- Fichas operativas recientes ---------------------------- */
function renderFichasOperativas(fo) {
  const cont = document.getElementById("fichasOperativas");
  if (!cont) return;
  cont.innerHTML = "";
  const items = fo ? asArray(fo.recientes) : [];
  if (!items.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Sin fichas operativas todavía.";
    cont.appendChild(p);
    return;
  }
  items.forEach(f => {
    const wrap = document.createElement("div");
    wrap.style.cssText = "border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:10px 12px;margin-bottom:8px;background:rgba(255,255,255,.03);";

    const top = document.createElement("div");
    top.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:4px;";
    const tit = document.createElement("span");
    tit.textContent = f.titulo || "?";
    tit.style.cssText = "font-weight:600;color:#e6edf3;flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    tit.title = f.titulo || "";
    const tag = document.createElement("span");
    tag.textContent = f.tipo_tarea || "";
    tag.style.cssText = "font-size:10px;padding:1px 6px;border-radius:3px;background:rgba(0,0,0,.3);color:#58a6ff;white-space:nowrap;";
    top.appendChild(tit);
    top.appendChild(tag);
    wrap.appendChild(top);

    const meta = document.createElement("div");
    meta.style.cssText = "font-size:10px;color:#484f58;margin-bottom:4px;";
    const partes = [f.etiqueta || f.entidad || ""];
    if (f.material_normalizado) partes.push("material normalizado");
    if (f.periodo) partes.push(f.periodo);
    if (f.creado_en) partes.push(f.creado_en);
    meta.textContent = partes.join(" · ");
    wrap.appendChild(meta);

    const res = document.createElement("div");
    res.style.cssText = "font-size:11px;color:#8b949e;";
    res.textContent = f.resumen_deterministico || "";
    wrap.appendChild(res);

    cont.appendChild(wrap);
  });
  if (fo.total != null) {
    const ts = document.createElement("div");
    ts.style.cssText = "margin-top:4px;font-size:10px;color:#484f58;text-align:right;";
    ts.textContent = "Total fichas: " + fo.total;
    cont.appendChild(ts);
  }
}

/* -- Mapa histórico proveedores (matriz proveedores x meses) -- */
function renderMapaHistoricoProveedores(mh) {
  const cont = document.getElementById("mapaHistoricoProveedores");
  if (!cont) return;
  cont.innerHTML = "";
  const meses = mh ? asArray(mh.meses) : [];
  const provs = mh ? asArray(mh.proveedores) : [];
  if (!meses.length || !provs.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Sin datos para el mapa histórico. Ejecutar recalcular derivadas.";
    cont.appendChild(p);
    return;
  }

  const res = mh.resumen || {};
  const head = document.createElement("div");
  head.style.cssText = "font-size:11px;color:#8b949e;margin-bottom:6px;";
  head.textContent =
    res.proveedores_mostrados + " proveedores · " + res.meses_mostrados + " meses · " +
    res.fichas_completadas + " fichas completadas · " + res.pendientes + " pendientes en cola";
  cont.appendChild(head);

  const leyenda = document.createElement("div");
  leyenda.style.cssText = "font-size:10px;color:#484f58;margin-bottom:6px;";
  leyenda.textContent = "👑 top 1 · ★ top 3 · · top 10 · ✓ ficha ranking · ○ pendiente en cola";
  cont.appendChild(leyenda);

  const scroll = document.createElement("div");
  scroll.style.cssText = "overflow-x:auto;border:1px solid rgba(255,255,255,.08);border-radius:6px;";
  cont.appendChild(scroll);

  const tabla = document.createElement("table");
  tabla.style.cssText = "border-collapse:collapse;font-size:10px;min-width:100%;";
  scroll.appendChild(tabla);

  function fmtKgCorto(v) {
    if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k";
    return String(Math.round(v));
  }

  const thead = document.createElement("tr");
  const thProv = document.createElement("th");
  thProv.textContent = "Proveedor";
  thProv.style.cssText = "position:sticky;left:0;background:#161b22;color:#8b949e;text-align:left;padding:4px 8px;white-space:nowrap;z-index:1;";
  thead.appendChild(thProv);
  meses.forEach(m => {
    const th = document.createElement("th");
    th.textContent = m.slice(2);  // "26-06"
    th.style.cssText = "color:#484f58;padding:4px 4px;white-space:nowrap;font-weight:400;";
    thead.appendChild(th);
  });
  tabla.appendChild(thead);

  provs.forEach((p, idx) => {
    const tr = document.createElement("tr");
    if (idx % 2 === 0) tr.style.background = "rgba(255,255,255,.02)";
    const tdNombre = document.createElement("td");
    tdNombre.style.cssText = "position:sticky;left:0;background:#161b22;padding:3px 8px;white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis;color:#c9d1d9;z-index:1;";
    tdNombre.textContent = p.proveedor;
    tdNombre.title = p.proveedor + " · " + Number(p.kg_total).toLocaleString("es-AR") +
      " kg históricos · " + p.meses_activos + " meses activos";
    tr.appendChild(tdNombre);

    const celdas = p.celdas || {};
    meses.forEach(m => {
      const td = document.createElement("td");
      td.style.cssText = "padding:3px 4px;text-align:center;white-space:nowrap;min-width:44px;";
      const c = celdas[m];
      if (c) {
        // Intensidad por % del mes
        const pct = Number(c.pct) || 0;
        const alpha = pct >= 50 ? 0.55 : pct >= 25 ? 0.4 : pct >= 10 ? 0.25 : pct > 0 ? 0.12 : 0;
        if (alpha) td.style.background = "rgba(63,185,80," + alpha + ")";
        let txt = c.kg > 0 ? fmtKgCorto(c.kg) : "";
        if (c.top === "top1") txt = "👑" + txt;
        else if (c.top === "top3") txt = "★" + txt;
        else if (c.top === "top10") txt = "·" + txt;
        if (c.ficha_ranking) txt += "✓";
        if (c.pendiente) txt += "○";
        td.textContent = txt;
        td.style.color = "#e6edf3";
        td.title = p.proveedor + " " + m + ": " + Number(c.kg).toLocaleString("es-AR") + " kg" +
          (c.pct ? " (" + c.pct + "% del mes)" : "") +
          (c.rank ? " · #" + c.rank + " del mes" : "") +
          (c.ficha_ranking ? " · ficha ranking completada" : "") +
          (c.pendiente ? " · tarea pendiente en cola" : "");
      }
      tr.appendChild(td);
    });
    tabla.appendChild(tr);
  });
}

/* -- Sonido discreto cuando aparece observación útil nueva -- */
let _ultimoIdObsUtil = null;
function _sonarObsUtil() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = "sine"; osc.frequency.value = 880;
    gain.gain.setValueAtTime(0, ctx.currentTime);
    gain.gain.linearRampToValueAtTime(0.08, ctx.currentTime + 0.05);
    gain.gain.linearRampToValueAtTime(0, ctx.currentTime + 0.35);
    osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.4);
  } catch(e) { /* browser bloqueó audio, silencioso */ }
}

/* -- Últimas observaciones útiles (microtareas primero, pulsos compactados) -- */
function renderUltimasObservacionesUtiles(items) {
  const cont = document.getElementById("ultimasObservacionesUtiles");
  if (!cont) return;
  cont.innerHTML = "";
  const arr = asArray(items);
  // Microtareas recientes primero (cualquier estado_* columna)
  const micros = arr.filter(x => String(x.columna||"").startsWith("estado_"));
  // Pulso operativo: compactado — mostrar solo el más reciente
  const pulsos = arr.filter(x => String(x.estado||"").includes("operativa") && !String(x.columna||"").startsWith("estado_"));
  const ultPulso = pulsos.length ? [pulsos[0]] : [];
  const mostrar = [...micros.slice(0, 4), ...ultPulso];

  // Sonido si hay micro nueva con id distinto al último visto
  if (micros.length > 0) {
    const primId = micros[0].id ?? micros[0].creada_en ?? null;
    if (primId !== null && primId !== _ultimoIdObsUtil) {
      if (_ultimoIdObsUtil !== null) _sonarObsUtil(); // no sonar en primer render
      _ultimoIdObsUtil = primId;
    }
  }

  if (!mostrar.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Aún no hay observaciones de microtareas.";
    cont.appendChild(p);
    return;
  }
  mostrar.forEach(x => {
    const box = document.createElement("div");
    box.className = "inf-card";
    const esMicro = String(x.columna||"").startsWith("estado_");
    if (esMicro) box.style.borderLeft = "3px solid #58a6ff";
    const dias = edadDias(x.creada_en || x.fecha);
    if (dias !== null && dias > 1) box.style.opacity = dias > 7 ? "0.45" : "0.7";
    const meta = document.createElement("div"); meta.className = "tipo";
    meta.innerHTML =
      (x.columna || "sin columna") + " · conf " + (x.confianza ?? "-") +
      tipoBadge(x.estado || x.columna) +
      badgeEdad(x.creada_en || x.fecha);
    box.appendChild(meta);
    const txt = document.createElement("div"); txt.className = "texto";
    txt.textContent = x.conclusion || "-";
    box.appendChild(txt);
    // Datos numéricos de evidencia si existen
    const ev = (typeof x.evidencia === "object" && x.evidencia) ? x.evidencia : {};
    const numParts = [];
    if (ev.total_kg != null)               numParts.push(`total: ${Number(ev.total_kg).toLocaleString("es-AR")} kg`);
    if (ev.esperado_proporcional_kg != null) numParts.push(`esperado: ${Number(ev.esperado_proporcional_kg).toLocaleString("es-AR")} kg`);
    if (ev.actual_vs_esperado_pct != null) {
      const pct = Number(ev.actual_vs_esperado_pct);
      const color = pct >= 0 ? "#3fb950" : "#f85149";
      numParts.push(`<span style="color:${color};font-weight:600">${pct >= 0 ? "+" : ""}${pct.toFixed(1)}% vs esp.</span>`);
    }
    if (ev.confiabilidad_normalidad)       numParts.push(`norm: ${ev.confiabilidad_normalidad}${ev.meses_base ? " ("+ev.meses_base+"m)" : ""}`);
    if (numParts.length) {
      const numEl = document.createElement("div");
      numEl.style.cssText = "font-size:11px;color:#8b949e;margin-top:4px;";
      numEl.innerHTML = numParts.join(" · ");
      box.appendChild(numEl);
    }
    const horaEl = document.createElement("div");
    horaEl.style.cssText = "font-size:11px;color:#8b949e;margin-top:2px;";
    horaEl.textContent = horaRelativa(x.creada_en || x.fecha) || "";
    box.appendChild(horaEl);
    if (!esMicro && pulsos.length > 1) {
      const comp = document.createElement("div");
      comp.style.cssText = "font-size:11px;color:#8b949e;margin-top:2px;";
      comp.textContent = `(+${pulsos.length - 1} pulsos anteriores omitidos)`;
      box.appendChild(comp);
    }
    cont.appendChild(box);
  });
}

/* -- Conclusiones autónomas priorizadas ------------------ */
function renderConclusionesAutonomas(items) {
  const autCont = document.getElementById("conclusionesAutonomas");
  if (!autCont) return;
  autCont.innerHTML = "";
  if (!items.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "No hay conclusiones autónomas aún.";
    autCont.appendChild(p);
    return;
  }
  const ahora = Date.now();
  const prioridad = (x) => {
    try {
      const d = new Date(String(x.creada_en||x.fecha||"").replace(" ","T"));
      const diff = isNaN(d) ? Infinity : (ahora - d.getTime()) / 1000;
      const esMicro = String(x.columna||"").startsWith("estado_produccion") || String(x.estado||"").includes("microtarea");
      const esPulso = String(x.estado||"").includes("operativa");
      if (diff < 86400 && esMicro) return 0;
      if (diff < 86400 && esPulso) return 1;
      if (diff < 86400)            return 2;
      return 3;
    } catch(e) { return 3; }
  };
  const sorted = [...items].sort((a,b) => prioridad(a) - prioridad(b));
  sorted.slice(0, 6).forEach(x => {
    const box = document.createElement("div");
    box.className = "inf-card";
    const dias = edadDias(x.creada_en || x.fecha);
    if (dias !== null && dias > 1) box.style.opacity = dias > 7 ? "0.45" : "0.7";
    const meta = document.createElement("div"); meta.className = "tipo";
    meta.innerHTML =
      (x.columna || "sin columna") + " · " + (x.estado || "provisional") + " · conf " + (x.confianza ?? "-") +
      tipoBadge(x.estado || x.columna) +
      badgeEdad(x.creada_en || x.fecha);
    box.appendChild(meta);
    const txt = document.createElement("div"); txt.className = "texto";
    txt.textContent = x.conclusion || "-";
    box.appendChild(txt);
    const horaEl = document.createElement("div");
    horaEl.style.cssText = "font-size:11px;color:#8b949e;margin-top:4px;";
    horaEl.textContent = horaRelativa(x.creada_en || x.fecha) || "";
    box.appendChild(horaEl);
    autCont.appendChild(box);
  });
}

/* -- Banda de última actividad útil ---------------------- */
function renderBandaActividad(c) {
  function fill(idVal, idHora, valor, hora) {
    const elV = document.getElementById(idVal);
    const elH = document.getElementById(idHora);
    if (elV) elV.textContent = valor || "-";
    if (elH) elH.textContent = hora  || "-";
  }
  const razon = asArray(c.razonamiento_reciente);

  const ultObs = razon.find(x => x.tipo === "observacion_guardada" || x.tipo === "observador_operativo");
  fill("baObsValor", "baObsHora", ultObs ? (ultObs.texto||"ok") : "sin datos", horaRelativa(ultObs?.hora) || "");

  const ultMicro = razon.find(x => x.tipo === "microtarea_resultado" || x.tipo === "microtarea_inicio");
  fill("baMicroValor", "baMicroHora", ultMicro ? (ultMicro.texto||"ok") : "sin datos", horaRelativa(ultMicro?.hora) || "");

  const ultPulso = razon.find(x => x.tipo === "observador_operativo");
  fill("baPulsoValor", "baPulsoHora", ultPulso ? (ultPulso.texto||"ok") : "sin datos", horaRelativa(ultPulso?.hora) || "");

  const td = c.tablas_derivadas || {};
  const ultDeriv = (td.ultima_actualizacion) || ((td.periodos||[])[0]||{}).actualizado_en;
  fill("baDerivadasValor","baDerivadasHora",
    ultDeriv ? (td.criterio_version||"derivadas") : "sin datos",
    horaRelativa(ultDeriv) || "");
}

function renderCerebro(c) {
  c = c || {};
  _cerebro = c;
  const m = metricasDe(c);
  setMetric("mHallazgos", m.hallazgos_interesantes);
  setMetric("mAutConc", m.conclusiones_autonomas);
  setMetric("mHallAlta", m.hallazgos_alta_prioridad);
  setMetric("mAprTotal", m.aprendizajes_totales);
  setMetric("mHipAct", m.hipotesis_activas);
  setMetric("mHipConf", m.hipotesis_confirmadas);
  setMetric("mHipAuto", m.hipotesis_autonomas);
  setMetric("mHipDesc", m.hipotesis_descartadas);
  setMetric("mDudas", m.dudas_abiertas);
  setMetric("mConoc", m.conocimiento_operativo_aprendido);
  const baseCompr = m.columnas_comprendidas ?? 0;
  const baseExpl = m.columnas_exploradas ?? 0;
  const schemaCols = m.columnas_esquema_total ?? 0;
  const dictCols = m.columnas_diccionario ?? 0;
  setMetric("mBaseCols", `${baseCompr}/${baseExpl}`);
  setMetric("mSchemaCols", schemaCols);
  setMetric("mDictCols", schemaCols ? `${dictCols}/${schemaCols}` : dictCols);
  setMetric("mLinkedCols", m.columnas_con_relaciones ?? 0);
  setMetric("mRelaciones", m.relaciones_estructurales ?? m.relaciones_columnas);
  setMetric("mRelInteres", m.relaciones_interesantes);
  const progresoEl = document.getElementById("mProgreso");
  if (progresoEl) progresoEl.textContent = `Columnas base comprendidas: ${baseCompr}/${baseExpl} (${m.progreso_columnas_pct ?? 0}%)`;
  const progressFill = document.getElementById("progressFill");
  if (progressFill) progressFill.style.width = (m.progreso_columnas_pct ?? 0) + "%";

  // Autonomia badge
  const aut = c.autonomia || {};
  const badge = document.getElementById("autoBadge");
  if (badge) {
    badge.innerHTML = "";
    const b = document.createElement("span");
    b.className = "auto-badge" + (aut.activa ? "" : " off");
    b.textContent = aut.activa ? `? Auto · ${aut.modelo || "-"} · cada ${aut.intervalo_segundos || "-"}s` : "Auto: OFF";
    badge.appendChild(b);
  }
  try { renderResumenModelo(c); } catch (err) { console.error("Error renderResumenModelo", err); }
  try { renderTablasDerivadas(c.tablas_derivadas); } catch (err) { console.error("Error renderTablasDerivadas", err); }

  /* -- Actividad reciente ------------------- */
  const actList = actividadFiltrada(asArray(c.actividad_reciente));
  const VISIBLE = 12;
  const alUl = document.getElementById("actividadReciente");
  if (alUl) {
    alUl.innerHTML = "";
    actList.slice(0, VISIBLE).forEach(x => alUl.appendChild(tlItem(x)));
  }
  const alRest = document.getElementById("actividadRestante");
  if (alRest) {
    alRest.innerHTML = "";
    actList.slice(VISIBLE).forEach(x => alRest.appendChild(tlItem(x)));
  }
  const moreLink = document.getElementById("actividadMoreLink");
  if (moreLink) {
    if (actList.length > VISIBLE) {
      moreLink.style.display = "";
      moreLink.textContent = `Ver ${actList.length - VISIBLE} eventos más`;
    } else {
      moreLink.style.display = "none";
    }
  }

  /* -- Hipótesis activas -------------------- */
  const proxInner = document.getElementById("proximaAccionInner");
  proxInner.innerHTML = "";
  const proximaAccionMain = document.getElementById("proximaAccionMain");
  proximaAccionMain.innerHTML = "";

  function renderProxima(container) {
    if (c.proxima_accion) {
      const x = c.proxima_accion;
      const cid = "prox_" + x.id;
      const badge2 = document.createElement("div");
      badge2.className = "col-badge"; badge2.style.marginBottom = "8px";
      badge2.textContent = x.columna || "sin columna";
      container.appendChild(badge2);
      const preg = document.createElement("p");
      preg.textContent = x.pregunta || "-";
      preg.style.fontWeight = "600";
      container.appendChild(preg);
      const hip = document.createElement("div");
      hip.className = "muted";
      hip.textContent = "Hipótesis: " + (x.hipotesis || "-");
      container.appendChild(hip);
      const ta = document.createElement("textarea");
      ta.rows = 3; ta.placeholder = "Respuesta o verificación para avanzar...";
      ta.id = cid;
      prepararTextarea(ta, cid);
      container.appendChild(ta);
      const acts = document.createElement("div");
      acts.className = "actions";
      const conf_ = document.createElement("button");
      conf_.className = "btn-confirm"; conf_.textContent = "Confirmar y seguir";
      conf_.onclick = () => enviarFeedbackHipotesis(x.id, "confirmar", cid);
      acts.appendChild(conf_);
      const desc = document.createElement("button");
      desc.className = "btn-discard"; desc.textContent = "Descartar";
      desc.onclick = () => enviarFeedbackHipotesis(x.id, "descartar", cid);
      acts.appendChild(desc);
      container.appendChild(acts);
    } else {
      const p = document.createElement("p");
      p.className = "empty"; p.textContent = "No hay verificaciones pendientes.";
      container.appendChild(p);
    }
  }
  renderProxima(proxInner);
  renderProxima(proximaAccionMain);

  const hipActContainer = document.getElementById("hipotesisActivas");
  hipActContainer.innerHTML = "";
  const hipItems = c.hipotesis_activas || [];
  if (!hipItems.length) {
    const p = document.createElement("p");
    p.className = "empty"; p.textContent = "No hay hipótesis activas pendientes.";
    hipActContainer.appendChild(p);
  } else {
    const info = document.createElement("div");
    info.className = "muted"; info.style.marginBottom = "10px";
    info.textContent = `${hipItems.length} hipótesis en cola. La primera se responde arriba.`;
    hipActContainer.appendChild(info);
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "Ver cola completa";
    det.appendChild(sum);
    hipItems.forEach(x => det.appendChild(hipCard(x, true)));
    hipActContainer.appendChild(det);
  }


  const hipAutoContainer = document.getElementById("hipotesisAutonomas");
  if (hipAutoContainer) {
    hipAutoContainer.innerHTML = "";
    const hipAutoItems = c.hipotesis_autonomas || [];
    if (!hipAutoItems.length) {
      const p = document.createElement("p");
      p.className = "empty"; p.textContent = "Todavia no hay hipotesis autonomas persistidas.";
      hipAutoContainer.appendChild(p);
    } else {
      hipAutoItems.slice(0, 12).forEach(x => {
        const box = document.createElement("div");
        box.className = "hip-card";
        const badge = document.createElement("div");
        badge.className = "col-badge";
        badge.textContent = `${x.origen || "explorador"} · ${x.estado || "abierta"}`;
        box.appendChild(badge);
        const txt = document.createElement("div");
        txt.className = "hip-texto";
        txt.textContent = x.hipotesis || "-";
        box.appendChild(txt);
        if (x.pregunta) {
          const pregunta = document.createElement("div");
          pregunta.className = "tl-detalle";
          pregunta.textContent = "Pregunta: " + x.pregunta;
          box.appendChild(pregunta);
        }
        const meta = document.createElement("div");
        meta.className = "hip-meta";
        meta.textContent = `#${x.id || "-"} · confianza ${x.confianza ?? "-"} · ${fechaCorta(x.creada_en)} · ${x.evidencia_resumen || "sin evidencia resumida"}`;
        box.appendChild(meta);
        hipAutoContainer.appendChild(box);
      });
    }
  }

  /* -- Conocimiento confirmado --------------- */
  const conocContainer = document.getElementById("conocimientoConfirmado");
  conocContainer.innerHTML = "";
  const conocItems = c.conocimiento_confirmado || [];
  if (!conocItems.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "Aún no hay conocimiento operativo registrado.";
    conocContainer.appendChild(p);
  }
  conocItems.forEach(x => {
    const box = document.createElement("div");
    box.className = "conoc-card";
    const tema = document.createElement("div"); tema.className = "tema";
    tema.textContent = (x.tema || "conocimiento operativo").toUpperCase();
    box.appendChild(tema);
    const txt = document.createElement("div"); txt.className = "texto";
    txt.textContent = x.texto_resumido || x.texto || "-";
    box.appendChild(txt);
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = `${fechaCorta(x.fecha)} · fuente: ${x.fuente || "-"}`;
    box.appendChild(meta);
    conocContainer.appendChild(box);
  });

  /* -- Inferencias vigentes ------------------- */
  const infContainer = document.getElementById("inferenciasVigentes");
  infContainer.innerHTML = "";
  const infItems = c.inferencias || [];
  if (!infItems.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "No hay inferencias vigentes actualmente.";
    infContainer.appendChild(p);
  }
  infItems.forEach(x => {
    const box = document.createElement("div");
    box.className = "inf-card";
    const tipo = document.createElement("div"); tipo.className = "tipo";
    tipo.textContent = (x.tipo || "inferencia").toUpperCase();
    box.appendChild(tipo);
    const txt = document.createElement("div"); txt.className = "texto";
    txt.textContent = x.texto || "-";
    box.appendChild(txt);
    const conf2 = document.createElement("div"); conf2.className = "confianza";
    const n = parseFloat(x.confianza) || 0;
    conf2.textContent = `confianza: ${x.confianza ?? "-"} · ${fechaCorta(x.creada_en)}`;
    conf2.style.color = confianzaColor(n);
    box.appendChild(conf2);
    infContainer.appendChild(box);
  });

  /* -- Verificaciones recientes -------------- */

  /* Relaciones estructurales */
  const relContainer = document.getElementById("relacionesColumnas");
  if (relContainer) {
    relContainer.innerHTML = "";
    const relItems = (c.relaciones_interesantes && c.relaciones_interesantes.length) ? c.relaciones_interesantes : (c.relaciones_columnas || []);
    if (!relItems.length) {
      const p = document.createElement("p"); p.className = "empty";
      p.textContent = "Todavia no hay relaciones interesantes calculadas.";
      relContainer.appendChild(p);
    }
    relItems.slice(0, 35).forEach(x => {
      relContainer.appendChild(relacionCard(x, false));
    });
  }

  const verContainer = document.getElementById("verificacionesRecientes");
  verContainer.innerHTML = "";
  const verItems = c.verificaciones_recientes || [];
  if (!verItems.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "No hay verificaciones registradas todavía.";
    verContainer.appendChild(p);
  }
  verItems.forEach(x => {
    const box = document.createElement("div");
    box.className = "ver-card";
    const st = (x.estado || "").toLowerCase();
    const esConf = ["confirmada","confirmado","validada","validado"].includes(st);
    const stDiv = document.createElement("div");
    stDiv.className = "estado " + (esConf ? "estado-conf" : "estado-desc");
    stDiv.textContent = (esConf ? "? " : "? ") + (x.columna || "sin columna") + " · " + (x.estado || "-");
    box.appendChild(stDiv);
    const txt = document.createElement("div"); txt.className = "texto";
    txt.textContent = x.hipotesis || "-";
    box.appendChild(txt);
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = `confianza original: ${x.confianza ?? "-"} · ${fechaCorta(x.creada_en)}`;
    box.appendChild(meta);
    verContainer.appendChild(box);
  });

  /* -- Razonamiento -------------------------- */
  const hallCont = document.getElementById("hallazgosInteresantes");
  hallCont.innerHTML = "";
  const hallItems = c.hallazgos_interesantes || [];
  if (!hallItems.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "Todavia no hay senales interesantes nuevas.";
    hallCont.appendChild(p);
  } else {
    hallItems.slice(0, 6).forEach(x => hallCont.appendChild(insightCard(x)));
  }

  const obsCont = document.getElementById("observacionesExploratorias");
  if (obsCont) {
    obsCont.innerHTML = "";
    const obsItems = c.observaciones_exploratorias || [];
    if (!obsItems.length) {
      const p = document.createElement("p"); p.className = "empty";
      p.textContent = "Todavia no hay observaciones exploratorias.";
      obsCont.appendChild(p);
    } else {
      obsItems.slice(0, 6).forEach(x => obsCont.appendChild(insightCard({...x, nivel_interes: x.nivel_interes || "baja"})));
    }
  }

  renderProtagonistas(c.protagonistas_operativos || null);
  renderBiografiaProtagonistas(c.biografias_protagonistas || null);
  renderColaCuriosidad(c.cola_curiosidad || null);
  renderFichasOperativas(c.fichas_operativas || null);
  renderMapaHistoricoProveedores(c.mapa_historico_proveedores || null);
  renderMaterialesNormalizados(c.materiales_normalizados_recientes || null);
  renderUltimasObservacionesUtiles(c.conclusiones_autonomas || []);
  renderRazonamientoAgrupado(c.razonamiento_reciente || []);
  renderConclusionesAutonomas(c.conclusiones_autonomas || []);
  renderBandaActividad(c);
}

/* -- Render planta (estado operativo) ----- */
function renderEstado(d) {
  const e = d.estado || {};
  document.getElementById("prod").textContent = kg(e.produccion_hoy_kg);
  document.getElementById("ing").textContent = kg(e.ingresos_hoy_kg);
  const ctx = d.contexto_operacional || {};
  const mes = ctx.resumen_mes || {};
  document.getElementById("prodMes").textContent = kg(mes.produccion_mes_kg);
  document.getElementById("ingMes").textContent = kg(mes.ingresos_mes_kg);

  const u = e.ultimo_ingreso;
  // top produccion
  const topProd = document.getElementById("topProd");
  topProd.innerHTML = "";
  (e.top_produccion || []).slice(0, 5).forEach(x =>
    topProd.appendChild(li(`${x.grupo}: ${kg(x.kilos)} · ${x.ultimo_registro}`)));
  const topIng = document.getElementById("topIng");
  topIng.innerHTML = "";
  (e.top_ingresos || []).slice(0, 5).forEach(x =>
    topIng.appendChild(li(`${x.grupo}: ${kg(x.kilos)} · ${x.ultimo_registro}`)));
  const concM = document.getElementById("conclusionesModelo");
  concM.innerHTML = "";
  (d.conclusiones_modelo || []).slice(0, 5).forEach(x =>
    concM.appendChild(li(`${String(x.prioridad||"").toUpperCase()} · ${x.texto}`)));
  const conc = document.getElementById("conclusiones");
  conc.innerHTML = "";
  (d.conclusiones || []).slice(0, 5).forEach(x =>
    conc.appendChild(li(`${x.prioridad.toUpperCase()} · ${x.texto}`)));
  const inf = document.getElementById("inferenciasOper");
  inf.innerHTML = "";
  (d.inferencias || []).filter(x => x.vigente === 1 || x.vigente === true).slice(0, 5).forEach(x =>
    inf.appendChild(li(`${x.tipo}: ${x.texto}`)));
  document.getElementById("mqtt").textContent = d.mqtt_total ?? "-";

  // Dudas del modelo (operativo)
  const dudas = document.getElementById("dudasModelo");
  if (!hayTextoEnEdicion()) {
    dudas.innerHTML = "";
    (d.dudas_modelo || []).forEach(x => {
      const box = document.createElement("div");
      box.style.cssText = "border-top:1px solid #30363d;padding-top:10px;margin-top:10px;";
      const borrId = "duda_" + x.id;
      const p = document.createElement("p"); p.textContent = x.pregunta; box.appendChild(p);
      const m = document.createElement("div"); m.className = "muted"; m.textContent = x.motivo || ""; box.appendChild(m);
      const ta = document.createElement("textarea");
      ta.rows = 3; ta.placeholder = "Responder y enseñar al sistema...";
      prepararTextarea(ta, borrId);
      box.appendChild(ta);
      const b = document.createElement("button"); b.textContent = "Guardar respuesta";
      b.onclick = async () => {
        const resp = ta.value.trim();
        if (!resp) return alert("Escribí una respuesta");
        const rr = await fetch("/api/ia/responder_duda", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({id: x.id, respuesta: resp})
        });
        if (!rr.ok) { alert("Error guardando respuesta"); } else { delete borradoresTexto[borrId]; cargar(); }
      };
      box.appendChild(b);
      dudas.appendChild(box);
    });
  }

  // Preguntas esquema
  const pe = document.getElementById("preguntasEsquema");
  pe.innerHTML = "";
  (d.preguntas_esquema || []).forEach(x => {
    const box = document.createElement("div");
    box.style.cssText = "border-top:1px solid #30363d;padding-top:10px;margin-top:10px;";
    const col = document.createElement("div"); col.className = "muted"; col.textContent = "Columna: " + (x.columna || "-"); box.appendChild(col);
    const p = document.createElement("p"); p.textContent = x.pregunta_usuario || "-"; box.appendChild(p);
    const h = document.createElement("div"); h.className = "muted"; h.textContent = x.hipotesis || ""; box.appendChild(h);
    pe.appendChild(box);
  });

  // Eventos
  const ev = document.getElementById("eventos");
  ev.innerHTML = "";
  (d.eventos || []).forEach(x =>
    ev.appendChild(li(`${x.resuelto ? "OK" : "ABIERTA"} · ${x.severidad} · ${x.descripcion}`)));
}

/* -- Carga principal ----------------------- */
async function cargar() {
  const editando = hayTextoEnEdicion();
  try {
    const r = await fetch("/api/ia/estado");
    const d = await r.json();
    document.getElementById("actualizado").textContent =
      "Actualizado: " + (d.estado_actualizado_en || d.timestamp || "");
    document.getElementById("statusDot").style.background = "#3fb950";
    renderEstado(d);

    if (!editando) {
      const rc = await fetch("/api/ia/cerebro");
      if (rc.ok) renderCerebro(await rc.json());
    }
  } catch(e) {
    document.getElementById("statusDot").style.background = "#f85149";
  }
}

cargar();
setInterval(cargar, 10000);
</script>
</body>
</html>
"""
    return Response(html, mimetype='text/html')



@app.route('/')
def index():
    """Sirve la página HTML."""
    return send_file('index.html', mimetype='text/html')

@app.route('/api/stats')
def api_stats():
    """API JSON con estadísticas."""
    stats = get_stats()
    if not stats:
        return jsonify({'error': 'Error consultando base de datos'}), 500
    
    # Agregar estado del servicio
    stats['service_status'] = get_service_status()
    stats['update_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    return jsonify(stats)

@app.route('/api/export')
def api_export():
    """Exporta datos en JSON."""
    stats = get_stats()
    if not stats:
        return jsonify({'error': 'Error consultando base de datos'}), 500
    
    return jsonify(stats)

if __name__ == '__main__':
    print(f"Iniciando servidor en http://0.0.0.0:8080", file=sys.stderr)
    iniciar_autonomia_ia()
    notificar_telegram_inicio_async()
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)







