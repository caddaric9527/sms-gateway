"""运行时配置：全部来自环境变量，容器部署时由 compose 注入。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # 串口
    modem_port: str = field(default_factory=lambda: _env("MODEM_PORT", "auto"))
    # 多设备：显式指定端口列表，逗号分隔，如 "COM7,COM10"；留空则自动发现全部模块
    modem_ports: str = field(default_factory=lambda: _env("MODEM_PORTS", ""))
    modem_baud: int = field(default_factory=lambda: _env_int("MODEM_BAUD", 115200))
    modem_mock: bool = field(default_factory=lambda: _env_bool("MODEM_MOCK", False))
    # mock 模式下模拟的设备台数，用于多设备联调
    modem_mock_count: int = field(default_factory=lambda: max(1, _env_int("MODEM_MOCK_COUNT", 1)))
    # 通讯协议：auto=先探 LuatOS 桥接再回退 AT；at=标准 AT 指令；luatos=模块侧脚本桥接
    modem_protocol: str = field(default_factory=lambda: _env("MODEM_PROTOCOL", "auto").lower())
    # 短信存储位置：SM=SIM 卡，ME=模块
    sms_storage: str = field(default_factory=lambda: _env("SMS_STORAGE", "SM"))
    # SIM 卡 PIN 码：留空=不处理（大多数国内卡默认没开 PIN）。
    # 只在 SIM 明确未就绪时才尝试，且每次启动最多试 2 次——错 3 次卡会被锁，只能用 PUK 解。
    sim_pin: str = field(default_factory=lambda: _env("SIM_PIN", ""))
    # 同步后是否从模块/SIM 删除已入库短信
    keep_on_modem: bool = field(default_factory=lambda: _env_bool("KEEP_ON_MODEM", False))
    poll_interval: int = field(default_factory=lambda: _env_int("POLL_INTERVAL", 20))
    status_interval: int = field(default_factory=lambda: _env_int("STATUS_INTERVAL", 45))
    # 数据
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "/data/sms.db"))
    log_retention_days: int = field(default_factory=lambda: _env_int("LOG_RETENTION_DAYS", 7))
    # 认证：留空则不启用登录（仅限可信内网）
    auth_password: str = field(default_factory=lambda: _env("AUTH_PASSWORD", ""))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    # 外观
    site_title: str = field(default_factory=lambda: _env("SITE_TITLE", "短信网关"))
    # 设备标识，转发时带上
    device_name: str = field(default_factory=lambda: _env("DEVICE_NAME", "air780epm"))


settings = Settings()


def ensure_data_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
