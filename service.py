"""PingDiagnose

  PingDiagnose.exe                          chạy bởi Windows Service Manager
  PingDiagnose.exe run                      chạy trong cửa sổ console
  PingDiagnose.exe config --port 8443 [--https on|off] [--name ten.mien] [--proxy http://proxy:port]
  PingDiagnose.exe cert                     tạo/cấp lại chứng chỉ HTTPS, in đường dẫn file CA
  PingDiagnose.exe reset-admin              đặt lại tài khoản admin/admin
"""
import argparse
import os
import signal
import sys
import threading

from pingdiagnose.config import APP_NAME, VERSION, load_config, save_config

SERVICE_NAME = APP_NAME
SERVICE_DISPLAY = "PingDiagnose - Giám sát kết nối IP"
SERVICE_DESC = "Định kỳ ping các địa chỉ IP, cảnh báo mất kết nối và thống kê qua giao diện web."


def run_console():
    from pingdiagnose.server import AppServer, setup_logging

    setup_logging(console=True)
    srv = AppServer()
    url = srv.start()
    print(f"\n{APP_NAME} {VERSION} đang chạy: {url}\nNhấn Ctrl+C để dừng.\n")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(1):
            pass
    finally:
        srv.stop()


def cmd_config(argv):
    p = argparse.ArgumentParser(prog="PingDiagnose config")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--https", choices=["on", "off"])
    p.add_argument("--name", action="append", help="tên miền/IP thêm vào chứng chỉ")
    p.add_argument("--proxy", help="proxy để máy chủ gửi push, '' để bỏ")
    a = p.parse_args(argv)
    cfg = load_config()
    if a.port:
        cfg["port"] = a.port
    if a.host:
        cfg["host"] = a.host
    if a.https:
        cfg["https"] = a.https == "on"
    if a.name:
        cfg["extra_names"] = sorted(set(cfg.get("extra_names") or []) | set(a.name))
    if a.proxy is not None:
        cfg["push_proxy"] = a.proxy
    save_config(cfg)
    print(cfg)


def cmd_cert():
    from pingdiagnose import certs
    from pingdiagnose.config import data_dir

    certs.ensure_server_cert(load_config().get("extra_names") or [])
    print(os.path.join(data_dir(), certs.CA_CRT))


def cmd_reset_admin():
    from pingdiagnose import db

    db.reset_admin()
    print('Đã đặt lại tài khoản "admin" với mật khẩu "admin" (phải đổi khi đăng nhập).')


if os.name == "nt":
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class PingDiagnoseService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.server = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            from pingdiagnose.server import AppServer, setup_logging

            setup_logging()
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                  servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
            try:
                self.server = AppServer()
                self.server.start()
            except Exception as e:
                import logging
                logging.getLogger("pingdiagnose").exception("Không khởi động được")
                servicemanager.LogErrorMsg(f"{APP_NAME} không khởi động được: {e}")
                return
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            self.server.stop()
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                  servicemanager.PYS_SERVICE_STOPPED, (self._svc_name_, ""))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = sys.argv[1:]
    cmd = args[0].lower() if args else ""
    if cmd in ("run", "console", "debug-run"):
        return run_console()
    if cmd == "config":
        return cmd_config(args[1:])
    if cmd == "cert":
        return cmd_cert()
    if cmd == "reset-admin":
        return cmd_reset_admin()
    if cmd in ("-h", "--help", "help", "version", "--version"):
        print(f"{APP_NAME} {VERSION}")
        print(__doc__)
        return
    if os.name != "nt":
        if cmd:
            print(__doc__)
            return
        return run_console()

    if not args:
        try:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(PingDiagnoseService)
            servicemanager.StartServiceCtrlDispatcher()
        except Exception as e:
            if getattr(e, "winerror", None) == 1063:
                print("Không được chạy bởi Service Manager -> chạy ở chế độ console.")
                return run_console()
            raise
    else:
        win32serviceutil.HandleCommandLine(PingDiagnoseService)


if __name__ == "__main__":
    main()
