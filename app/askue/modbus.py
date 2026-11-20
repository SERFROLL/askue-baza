"""Modbus RTU поверх TCP (прозрачный режим шлюза USR-DR302). Только чтение (функция 04)."""
import socket
import struct
import time


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class ModbusError(Exception):
    pass


class Link:
    """Одно TCP-соединение со шлюзом. Переподключается само при следующем запросе после ошибки."""

    def __init__(self, host, port, timeout=3.0, source_ip=None):
        self.host, self.port, self.timeout, self.source_ip = host, port, timeout, source_ip
        self.sock = None

    def connect(self):
        self.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        if self.source_ip:
            s.bind((self.source_ip, 0))
        s.connect((self.host, self.port))
        self.sock = s

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _drain(self):
        self.sock.settimeout(0.05)
        try:
            while self.sock.recv(512):
                pass
        except (socket.timeout, BlockingIOError, OSError):
            pass

    def read_input(self, unit, addr, count):
        """Прочитать count входных регистров (функция 04). Возвращает список u16."""
        if self.sock is None:
            self.connect()
        try:
            return self._read(unit, 4, addr, count)
        except (OSError, ModbusError):
            self.close()
            raise

    def _read(self, unit, func, addr, count):
        req = struct.pack(">BBHH", unit, func, addr, count)
        req += struct.pack("<H", crc16(req))
        self._drain()
        self.sock.settimeout(self.timeout)
        self.sock.sendall(req)
        buf = b""
        need = 5
        deadline = time.monotonic() + self.timeout
        while len(buf) < need:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ModbusError("нет ответа за %.1f с" % self.timeout)
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(512)
            except socket.timeout:
                raise ModbusError("нет ответа за %.1f с" % self.timeout)
            if not chunk:
                raise ModbusError("шлюз закрыл соединение")
            buf += chunk
            if len(buf) >= 3:
                if buf[0] != unit:
                    raise ModbusError("ответил другой адрес: %d" % buf[0])
                if buf[1] == (func | 0x80):
                    need = 5
                elif buf[1] == func:
                    need = 3 + buf[2] + 2
        frame = buf[:need]
        if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
            raise ModbusError("ошибка контрольной суммы")
        if frame[1] == (func | 0x80):
            raise ModbusError("код ошибки Modbus %d" % frame[2])
        data = frame[3:-2]
        return list(struct.unpack(">%dH" % (len(data) // 2), data))


def s16(w):
    return w - 65536 if w > 32767 else w


def s32(hi, lo):
    return struct.unpack(">i", struct.pack(">HH", hi, lo))[0]


def u64(words):
    return struct.unpack(">Q", struct.pack(">4H", *words))[0]
