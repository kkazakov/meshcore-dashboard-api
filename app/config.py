"""
Application configuration loaded from environment variables / .env file.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MeshCore device connection
    connection_type: str = "ble"
    ble_address: str = ""
    ble_pin: str = ""
    serial_port: str = "/dev/ttyUSB0"
    serial_baudrate: int = 115200
    tcp_host: str = "192.168.1.100"
    tcp_port: int = 4000
    debug: bool = False
    # Path hash mode: 0 = 1-byte hashes, 1 = 2-byte hashes (fw protocol >= v10)
    path_hash_mode: int = 1

    # ClickHouse
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "meshcore"
    clickhouse_user: str = "admin"
    clickhouse_password: str = ""

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Repeater telemetry poller
    repeater_poll_interval: int = 600

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
