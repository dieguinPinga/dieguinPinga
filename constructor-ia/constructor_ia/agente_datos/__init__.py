"""Agente de datos (AGENTE_DATOS.md): explorador autónomo de bases MariaDB con memoria persistente.

El coordinador conduce, el modelo interpreta en microtareas, el sistema verifica y la memoria guarda solo lo
verificado, con su procedencia. Reutiliza sin modificarlos el intermediario de solo lectura del Explorador
(broker, sqlguard, mysqlproto), su mapa (instantáneas de esquema) y su estimación de contexto.
"""
VERSION = "agente de datos 0.8-fase2c"
