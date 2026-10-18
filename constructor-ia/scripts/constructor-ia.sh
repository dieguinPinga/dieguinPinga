#!/bin/sh
# Lanzador de Constructor IA: entorno virtual propio, modo aislado de Python (-I).
# El paquete se encuentra mediante venv/.../site-packages/constructor_ia.pth.
CIA_BASE_DIR="${CIA_BASE_DIR:-$HOME/constructor-ia}"
export CIA_BASE_DIR
exec "$CIA_BASE_DIR/venv/bin/python3" -I -m constructor_ia "$@"
