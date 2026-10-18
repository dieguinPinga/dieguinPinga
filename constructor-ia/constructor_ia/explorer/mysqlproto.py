"""Cliente mínimo del protocolo MySQL/MariaDB (solo biblioteca estándar).

Diseñado para el intermediario del Explorador, no como cliente general:
* Nunca anuncia CLIENT_LOCAL_FILES (el servidor no puede pedir archivos locales)
  ni CLIENT_MULTI_STATEMENTS (una sentencia por petición, lo rechaza el servidor).
* Autenticación mysql_native_password (incluido el cambio de plugin). Otros
  plugins se rechazan con un error claro.
* Protocolo de texto (COM_QUERY). Lectura con topes de filas, bytes y tiempo:
  al superarlos se cierra el socket (el servidor aborta el envío).
"""
from __future__ import annotations

import hashlib
import socket
import struct
import time
from dataclasses import dataclass, field

CLIENT_LONG_PASSWORD = 0x1
CLIENT_LONG_FLAG = 0x4
CLIENT_CONNECT_WITH_DB = 0x8
CLIENT_PROTOCOL_41 = 0x200
CLIENT_TRANSACTIONS = 0x2000
CLIENT_SECURE_CONNECTION = 0x8000
CLIENT_PLUGIN_AUTH = 0x80000
CLIENT_LOCAL_FILES = 0x80
CLIENT_MULTI_STATEMENTS = 0x10000
CLIENT_FLAGS = (CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG | CLIENT_CONNECT_WITH_DB | CLIENT_PROTOCOL_41
                | CLIENT_TRANSACTIONS | CLIENT_SECURE_CONNECTION | CLIENT_PLUGIN_AUTH)
assert not CLIENT_FLAGS & (CLIENT_LOCAL_FILES | CLIENT_MULTI_STATEMENTS)

COM_QUERY = 0x03
COM_QUIT = 0x01
UTF8MB4_GENERAL_CI = 45
BINARY_CHARSET = 63
INT_TYPES = {1, 2, 3, 8, 9, 13, 16}
FLOAT_TYPES = {4, 5}


class MySQLError(Exception):
    def __init__(self, code: int, message: str, sqlstate: str = ""):
        super().__init__(f"({code}) {message}")
        self.code, self.message, self.sqlstate = code, message, sqlstate


class LimitExceeded(Exception):
    """Se cortó la lectura por un tope del intermediario (filas, bytes o tiempo)."""


@dataclass
class Column:
    name: str
    table: str
    type: int
    charset: int


@dataclass
class Result:
    columns: list[Column] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    truncated_reason: str = ""
    bytes_read: int = 0
    affected: int = 0


def native_password(password: bytes, scramble: bytes) -> bytes:
    if not password:
        return b""
    h1 = hashlib.sha1(password).digest()
    h2 = hashlib.sha1(h1).digest()
    h3 = hashlib.sha1(scramble + h2).digest()
    return bytes(a ^ b for a, b in zip(h1, h3))


def _lenenc_int(buf: bytes, pos: int) -> tuple[int | None, int]:
    first = buf[pos]
    if first < 0xFB:
        return first, pos + 1
    if first == 0xFB:
        return None, pos + 1
    if first == 0xFC:
        return struct.unpack_from("<H", buf, pos + 1)[0], pos + 3
    if first == 0xFD:
        return int.from_bytes(buf[pos + 1:pos + 4], "little"), pos + 4
    return struct.unpack_from("<Q", buf, pos + 1)[0], pos + 9


def _lenenc_str(buf: bytes, pos: int) -> tuple[bytes | None, int]:
    n, pos = _lenenc_int(buf, pos)
    if n is None:
        return None, pos
    return buf[pos:pos + n], pos + n


class Connection:
    def __init__(self, *, user: str, password: str, database: str, host: str = "127.0.0.1", port: int = 3306,
                 unix_socket: str | None = None, connect_timeout: float = 5.0):
        self.seq = 0
        self.deadline: float | None = None
        if unix_socket:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            target = unix_socket
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target = (host, port)
        self.sock.settimeout(connect_timeout)
        try:
            self.sock.connect(target)
            self._handshake(user.encode(), password.encode(), database.encode())
        except Exception:
            self.close()
            raise

    # Transporte -------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        chunks, got = [], 0
        while got < n:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("tiempo")
                self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(min(65536, n - got))
            except TimeoutError:
                raise LimitExceeded("tiempo") from None
            if not chunk:
                raise ConnectionError("El servidor cerró la conexión")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _read_packet(self) -> bytes:
        header = self._recv_exact(4)
        length = int.from_bytes(header[:3], "little")
        self.seq = (header[3] + 1) & 0xFF
        if length == 0xFFFFFF:
            raise LimitExceeded("paquete demasiado grande")
        return self._recv_exact(length)

    def _write_packet(self, payload: bytes) -> None:
        self.sock.sendall(len(payload).to_bytes(3, "little") + bytes([self.seq]) + payload)
        self.seq = (self.seq + 1) & 0xFF

    @staticmethod
    def _raise_err(pkt: bytes) -> None:
        code = struct.unpack_from("<H", pkt, 1)[0]
        if pkt[3:4] == b"#":
            state, msg = pkt[4:9].decode(), pkt[9:].decode("utf-8", "replace")
        else:
            state, msg = "", pkt[3:].decode("utf-8", "replace")
        raise MySQLError(code, msg, state)

    # Conexión ---------------------------------------------------------
    def _handshake(self, user: bytes, password: bytes, database: bytes) -> None:
        pkt = self._read_packet()
        if pkt[0] == 0xFF:
            self._raise_err(pkt)
        if pkt[0] != 10:
            raise ConnectionError("Protocolo de servidor no soportado")
        pos = pkt.index(b"\0", 1)
        self.server_version = pkt[1:pos].decode("utf-8", "replace")
        pos += 1
        self.thread_id = struct.unpack_from("<I", pkt, pos)[0]
        pos += 4
        scramble = pkt[pos:pos + 8]
        pos += 9
        pos += 2 + 1 + 2 + 2  # capacidades bajas, charset, estado, capacidades altas
        auth_len = pkt[pos]
        pos += 1 + 10
        part2_len = max(13, auth_len - 8)
        scramble += pkt[pos:pos + part2_len].rstrip(b"\0")[:12]
        pos += part2_len
        plugin = pkt[pos:pkt.index(b"\0", pos)].decode() if pos < len(pkt) and b"\0" in pkt[pos:] else ""
        if plugin and plugin != "mysql_native_password":
            # Pedimos native igual: el servidor enviará un cambio de plugin si corresponde.
            plugin = "mysql_native_password"
        auth = native_password(password, scramble)
        payload = (struct.pack("<IIB", CLIENT_FLAGS, 16 * 1024 * 1024, UTF8MB4_GENERAL_CI) + b"\0" * 23
                   + user + b"\0" + bytes([len(auth)]) + auth + database + b"\0" + b"mysql_native_password\0")
        self._write_packet(payload)
        pkt = self._read_packet()
        if pkt[0] == 0xFE:  # cambio de plugin
            end = pkt.index(b"\0", 1)
            name = pkt[1:end].decode()
            if name != "mysql_native_password":
                raise ConnectionError(f"Plugin de autenticación no soportado: {name} "
                                      "(cree el usuario con mysql_native_password)")
            self._write_packet(native_password(password, pkt[end + 1:].rstrip(b"\0")[:20]))
            pkt = self._read_packet()
        if pkt[0] == 0xFF:
            self._raise_err(pkt)
        if pkt[0] != 0x00:
            raise ConnectionError("Respuesta de autenticación inesperada")

    # Consultas --------------------------------------------------------
    def query(self, sql: str, max_rows: int = 1000, max_bytes: int = 1_000_000, timeout: float = 30.0) -> Result:
        """Ejecuta UNA sentencia. Al superar un tope corta la lectura y deja la conexión inutilizable."""
        self.seq = 0
        self.deadline = time.monotonic() + timeout
        res = Result()
        try:
            self._write_packet(bytes([COM_QUERY]) + sql.encode("utf-8"))
            pkt = self._read_packet()
            res.bytes_read += len(pkt)
            if pkt[0] == 0xFF:
                self._raise_err(pkt)
            if pkt[0] == 0x00:
                res.affected = _lenenc_int(pkt, 1)[0] or 0
                return res
            if pkt[0] == 0xFB:
                # Petición LOCAL INFILE: nunca se atiende (y la capacidad no se anunció).
                raise ConnectionError("El servidor pidió leer un archivo local: rechazado")
            ncols, _ = _lenenc_int(pkt, 0)
            for _ in range(ncols):
                p = self._read_packet()
                res.bytes_read += len(p)
                pos = 0
                vals = []
                for _ in range(6):
                    v, pos = _lenenc_str(p, pos)
                    vals.append(v or b"")
                pos += 1  # longitud de campos fijos (0x0c)
                charset, _length, ctype = struct.unpack_from("<HIB", p, pos)
                res.columns.append(Column(vals[4].decode("utf-8", "replace"), vals[2].decode("utf-8", "replace"),
                                          ctype, charset))
            p = self._read_packet()  # EOF tras definiciones
            if p[0] != 0xFE:
                raise ConnectionError("Protocolo inesperado tras columnas")
            while True:
                p = self._read_packet()
                res.bytes_read += len(p) + 4
                if p[0] == 0xFE and len(p) < 9:
                    break
                if p[0] == 0xFF:
                    self._raise_err(p)
                if res.bytes_read > max_bytes:
                    res.truncated, res.truncated_reason = True, "bytes"
                    raise LimitExceeded("bytes")
                if len(res.rows) >= max_rows:
                    res.truncated, res.truncated_reason = True, "filas"
                    raise LimitExceeded("filas")
                row, pos = [], 0
                for col in res.columns:
                    v, pos = _lenenc_str(p, pos)
                    row.append(_convert(v, col))
                res.rows.append(row)
            return res
        except LimitExceeded as e:
            res.truncated = True
            res.truncated_reason = res.truncated_reason or str(e)
            self.close()
            if res.truncated_reason == "tiempo":
                raise
            return res
        finally:
            self.deadline = None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def abort(self) -> None:
        """Corta la conexión desde otro hilo: shutdown despierta un recv bloqueado (close solo no lo hace)."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.close()


def _convert(v: bytes | None, col: Column):
    if v is None:
        return None
    if col.charset == BINARY_CHARSET and col.type in (249, 250, 251, 252, 253, 254, 15):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binario {len(v)} bytes>"
    if col.type in INT_TYPES:
        try:
            return int(v)
        except ValueError:
            pass
    if col.type in FLOAT_TYPES:
        try:
            return float(v)
        except ValueError:
            pass
    return v.decode("utf-8", "replace")
