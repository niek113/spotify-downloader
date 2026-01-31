from pydantic_settings import BaseSettings


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
