import json
import os
import secrets
import sys

APP_NAME = "PingDiagnose"
VERSION = "1.3.2"
FROZEN = getattr(sys, "frozen", False)

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8443,
    "https": True,
    "cert_file": "",
    "key_file": "",
    "extra_names": [],
}


def resource_dir():
    base = getattr(sys, "_MEIPASS", None)
    return os.path.join(base, "pingdiagnose") if base else os.path.dirname(os.path.abspath(__file__))


def data_dir():
    path = os.environ.get("PINGDIAGNOSE_DATA")
    if not path:
        if FROZEN and os.name == "nt":
            path = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), APP_NAME)
        else:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(data_dir(), "config.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def secret_key():
    path = os.path.join(data_dir(), "secret.key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key
