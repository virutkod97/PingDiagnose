import ipaddress
import os
import re
import socket
import subprocess
import time

HOST_RE = re.compile(r"^[A-Za-z0-9:][A-Za-z0-9.\-:%]{0,252}$")


def valid_target(target):
    target = (target or "").strip()
    if not HOST_RE.match(target):
        return False
    try:
        ipaddress.ip_address(target.split("%")[0])
        return True
    except ValueError:
        pass
    labels = target.rstrip(".").split(".")
    return all(re.match(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$", l) for l in labels)


class PingResult:
    __slots__ = ("sent", "received", "rtts", "error")

    def __init__(self, sent=0, received=0, rtts=None, error=None):
        self.sent = sent
        self.received = received
        self.rtts = rtts or []
        self.error = error

    @property
    def rtt_avg(self):
        return round(sum(self.rtts) / len(self.rtts), 2) if self.rtts else None

    def to_dict(self):
        return {
            "sent": self.sent,
            "received": self.received,
            "rtt_avg": self.rtt_avg,
            "rtts": self.rtts,
            "error": self.error,
        }


_icmp = None
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes

        class IP_OPTION_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("Ttl", ctypes.c_ubyte),
                ("Tos", ctypes.c_ubyte),
                ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte),
                ("OptionsData", ctypes.c_void_p),
            ]

        class ICMP_ECHO_REPLY(ctypes.Structure):
            _fields_ = [
                ("Address", ctypes.c_ulong),
                ("Status", ctypes.c_ulong),
                ("RoundTripTime", ctypes.c_ulong),
                ("DataSize", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort),
                ("Data", ctypes.c_void_p),
                ("Options", IP_OPTION_INFORMATION),
            ]

        _iphlp = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
        _iphlp.IcmpCreateFile.restype = wintypes.HANDLE
        _iphlp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        _iphlp.IcmpSendEcho.argtypes = [
            wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, wintypes.WORD,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ]
        _iphlp.IcmpSendEcho.restype = wintypes.DWORD
        _icmp = (_iphlp, ICMP_ECHO_REPLY)
    except Exception:
        _icmp = None


def _ping_win_icmp(ip, count, timeout_ms):
    import ctypes

    iphlp, reply_cls = _icmp
    addr = int.from_bytes(socket.inet_aton(ip), "little")
    payload = b"PingDiagnose-abcdefghijklmnopqr"
    req = ctypes.create_string_buffer(payload, len(payload))
    reply_size = ctypes.sizeof(reply_cls) + len(payload) + 8 + 64
    reply = ctypes.create_string_buffer(reply_size)
    handle = iphlp.IcmpCreateFile()
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("IcmpCreateFile failed")
    res = PingResult(sent=count)
    try:
        for i in range(count):
            if i:
                time.sleep(0.2)
            n = iphlp.IcmpSendEcho(handle, addr, req, len(payload), None, reply, reply_size, timeout_ms)
            if n:
                r = reply_cls.from_buffer_copy(reply.raw[: ctypes.sizeof(reply_cls)])
                if r.Status == 0:
                    res.received += 1
                    res.rtts.append(float(r.RoundTripTime))
    finally:
        iphlp.IcmpCloseHandle(handle)
    return res


_RTT_RE = re.compile(r"[=<]\s*([\d.,]+)\s*ms", re.I)
_V6_OK_RE = re.compile(r"[=<][\d.,]+\s?ms", re.I)


def _ping_cmd(target, count, timeout_ms, ipv6=False):
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms)]
        if ipv6:
            cmd.append("-6")
        flags = 0x08000000
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, round(timeout_ms / 1000)))]
        if ipv6:
            cmd.append("-6")
        flags = 0
    cmd.append(target)
    res = PingResult(sent=count)
    for i in range(count):
        if i:
            time.sleep(0.2)
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace",
                timeout=timeout_ms / 1000 + 5, creationflags=flags,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            res.error = str(e)
            continue
        out = p.stdout
        ok_line = None
        for line in out.splitlines():
            low = line.lower()
            if "ttl=" in low or (ipv6 and _V6_OK_RE.search(low)):
                ok_line = line
                break
        if ok_line is not None:
            res.received += 1
            m = _RTT_RE.search(ok_line)
            if m:
                try:
                    res.rtts.append(float(m.group(1).replace(",", ".")))
                except ValueError:
                    pass
    return res


def ping(target, count=2, timeout_ms=1000):
    if not valid_target(target):
        return PingResult(sent=count, error="Địa chỉ không hợp lệ")
    ip = target
    is_v6 = False
    try:
        is_v6 = ipaddress.ip_address(target.split("%")[0]).version == 6
    except ValueError:
        try:
            infos = socket.getaddrinfo(target, None)
            v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
            if v4:
                ip = v4[0]
            else:
                ip = infos[0][4][0]
                is_v6 = True
        except socket.gaierror:
            return PingResult(sent=count, error="Không phân giải được tên miền")
    try:
        if _icmp is not None and not is_v6:
            return _ping_win_icmp(ip, count, timeout_ms)
    except Exception:
        pass
    return _ping_cmd(ip, count, timeout_ms, ipv6=is_v6)
