"""Único canal de red del sandbox: puente hacia Ollama con lista blanca.

  anfitrión: python3 -I relay.py host --socket S --destino http://IP:PUERTO --modelo M --log L
      Escucha en el socket Unix S. Por cada pedido HTTP: lo valida (método, ruta, modelo, tamaño) y lo
      reenvía al destino fijo con «Connection: close». Rechaza con 403 cualquier otra cosa
      (/api/pull, /api/delete, /api/create, otro modelo, cuerpo chunked...).
  sandbox:   relay.interno(S) — hilo que escucha 127.0.0.1:11435 dentro del espacio de red vacío y copia
      bytes al socket S. No interpreta nada: la validación está del lado del anfitrión.

Qué permite y qué no (no es aislamiento absoluto de información):
  - SÍ deja salir contenido: cualquier proceso del sandbox, también un comando que escriba el agente, puede
    mandar a Ollama, dentro de /api/chat, cualquier texto (por ejemplo el contenido de /work) y leer la
    respuesta. Ese texto llega a Pulenta y puede quedar en sus registros. También puede consultar /api/tags,
    /api/ps y /api/show, y pedir inferencias al modelo permitido con las opciones que quiera (num_ctx,
    num_predict), o sea consumir GPU y tiempo de Pulenta.
  - NO permite conectarse a otra IP, puerto ni servicio (LAN, tailnet, internet, MariaDB, Node-RED, la
    instancia 11434), ni usar otras rutas de Ollama (/api/pull, /api/delete, /api/create, /api/generate...),
    ni otros modelos.
  - Lo que hay en el sandbox es poco y no sensible: la copia de tarea/ y lo que genere el agente. No hay
    secretos, /home, pruebas ocultas ni archivos del proyecto.

Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from urllib.parse import urlsplit

PERMITIDAS = {("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"),
              ("POST", "/api/show"), ("POST", "/api/chat")}
CON_MODELO = {"/api/show", "/api/chat"}
MAX_CUERPO = 4 * 1024 * 1024
OMITIR = {"host", "connection", "keep-alive", "proxy-connection", "content-length"}


def _responder(cli: socket.socket, codigo: int, texto: str) -> None:
    cuerpo = json.dumps({"error": texto}).encode()
    cli.sendall(f"HTTP/1.1 {codigo} Rechazado\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(cuerpo)}\r\nConnection: close\r\n\r\n".encode() + cuerpo)


class Puente:
    def __init__(self, destino: str, modelo: str, log: str):
        u = urlsplit(destino)
        if u.scheme != "http" or not u.hostname or not u.port:
            raise SystemExit(f"destino inválido (se espera http://IP:PUERTO): {destino}")
        self.host, self.port, self.modelo = u.hostname, u.port, modelo
        self._log = open(log, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def registrar(self, **datos) -> None:
        with self._lock:
            self._log.write(json.dumps({"t": round(time.time(), 3), **datos}, ensure_ascii=False) + "\n")
            self._log.flush()

    def atender(self, cli: socket.socket) -> None:
        with cli:
            f = cli.makefile("rb")
            linea = f.readline(8192)
            if not linea:
                return
            try:
                metodo, ruta_q, _ = linea.decode("latin-1").split(" ", 2)
            except ValueError:
                return _responder(cli, 400, "línea de pedido inválida")
            ruta = ruta_q.split("?", 1)[0]
            cabeceras, largo, chunked = [], 0, False
            while True:
                h = f.readline(8192)
                if h in (b"\r\n", b"\n", b""):
                    break
                nombre = h.split(b":", 1)[0].strip().lower().decode("latin-1")
                if nombre == "content-length":
                    largo = int(h.split(b":", 1)[1])
                elif nombre == "transfer-encoding":
                    chunked = True
                if nombre not in OMITIR:
                    cabeceras.append(h)
            motivo = None
            if (metodo, ruta) not in PERMITIDAS:
                motivo = f"ruta no permitida: {metodo} {ruta}"
            elif chunked or largo > MAX_CUERPO:
                motivo = "cuerpo chunked o demasiado grande"
            cuerpo = f.read(largo) if largo and not motivo else b""
            opciones = {}
            if not motivo and ruta in CON_MODELO:
                try:
                    datos = json.loads(cuerpo or b"{}")
                except ValueError:
                    datos = {}
                pedido = datos.get("model") or datos.get("name")
                if pedido != self.modelo:
                    motivo = f"modelo no permitido: {pedido!r}"
                opciones = datos.get("options") or {}
            if motivo:
                self.registrar(metodo=metodo, ruta=ruta, permitido=False, motivo=motivo)
                return _responder(cli, 403, motivo)
            inicio, recibidos = time.time(), 0
            with socket.create_connection((self.host, self.port), timeout=900) as up:
                up.sendall(linea + f"Host: {self.host}:{self.port}\r\nConnection: close\r\n"
                           f"Content-Length: {len(cuerpo)}\r\n".encode() + b"".join(cabeceras) + b"\r\n" + cuerpo)
                while bloque := up.recv(65536):
                    recibidos += len(bloque)
                    cli.sendall(bloque)
            self.registrar(metodo=metodo, ruta=ruta, permitido=True, bytes_enviados=len(cuerpo),
                           bytes_recibidos=recibidos, segundos=round(time.time() - inicio, 2),
                           num_ctx=opciones.get("num_ctx"), num_predict=opciones.get("num_predict"),
                           stop=opciones.get("stop"), seed=opciones.get("seed"),
                           temperature=opciones.get("temperature"))


def host(args) -> None:
    puente = Puente(args.destino, args.modelo, args.log)
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    os.chmod(args.socket, 0o600)
    srv.listen(8)
    puente.registrar(evento="inicio", destino=args.destino, modelo=args.modelo)
    while True:
        cli, _ = srv.accept()
        threading.Thread(target=_seguro, args=(puente, cli), daemon=True).start()


def _seguro(puente: Puente, cli: socket.socket) -> None:
    try:
        puente.atender(cli)
    except Exception as e:  # el puente no debe caerse por un pedido
        puente.registrar(evento="error", detalle=repr(e))


def _copiar(origen: socket.socket, destino: socket.socket) -> None:
    try:
        while bloque := origen.recv(65536):
            destino.sendall(bloque)
    except OSError:
        pass
    finally:
        for s in (origen, destino):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def interno(ruta_socket: str, puerto: int = 11435) -> None:
    """Dentro del sandbox: 127.0.0.1:puerto → socket Unix. Corre en un hilo daemon."""
    srv = socket.create_server(("127.0.0.1", puerto))

    def bucle():
        while True:
            cli, _ = srv.accept()
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.connect(ruta_socket)
            threading.Thread(target=_copiar, args=(cli, up), daemon=True).start()
            threading.Thread(target=_copiar, args=(up, cli), daemon=True).start()

    threading.Thread(target=bucle, daemon=True).start()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="modo", required=True)
    h = sub.add_parser("host")
    h.add_argument("--socket", required=True)
    h.add_argument("--destino", required=True)
    h.add_argument("--modelo", required=True)
    h.add_argument("--log", required=True)
    a = p.parse_args()
    host(a)
