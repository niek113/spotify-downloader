import json
import os
from pydantic_settings import BaseSettings

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/settings.json")


class Settings(BaseSettings):
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    slskd_host: str = "http://localhost:5030"
    slskd_api_key: str = ""
    slskd_download_dir: str = ""

    download_dir: str = "./downloads"

    search_timeout_ms: int = 30000
    min_bitrate: int = 192

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_saved_config() -> dict:
    """Load saved config from JSON file."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_config(data: dict) -> None:
    """Save config to JSON file."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_settings() -> Settings:
    """Get settings, merging env vars with saved config."""
    saved = load_saved_config()
    # Env vars take priority over saved config
    settings = Settings()

    # If env vars are empty, use saved config values
    if not settings.spotify_client_id and saved.get("spotify_client_id"):
        settings.spotify_client_id = saved["spotify_client_id"]
    if not settings.spotify_client_secret and saved.get("spotify_client_secret"):
        settings.spotify_client_secret = saved["spotify_client_secret"]
    if not settings.slskd_api_key and saved.get("slskd_api_key"):
        settings.slskd_api_key = saved["slskd_api_key"]
    if settings.slskd_host == "http://localhost:5030" and saved.get("slskd_host"):
        settings.slskd_host = saved["slskd_host"]

    return settings


def is_configured(settings: Settings) -> bool:
    """Check if all required settings are filled."""
    return bool(
        settings.spotify_client_id
        and settings.spotify_client_secret
        and settings.slskd_api_key
    )
