from pathlib import Path

from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    database_url: str
    openai_api_key: str = ""
    extraction_base_url: str = "https://api.openai.com/v1"
    extraction_model: str = "gpt-5.4"

    data_dir: str = str(REPO_ROOT / "data")
    template_dir: str = str(REPO_ROOT / "templates" / "example")
    max_upload_size_mb: int = 10

    encryption_key: str = ""
    require_encryption: bool = True
    dpa_signed: bool = False

    admin_token: str = ""
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    security_headers: bool = True

    download_token_ttl: int = 86400  # 24 hours

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True
    smtp_notify: str = ""

    model_config = {"env_file": str(REPO_ROOT / ".env"), "env_file_encoding": "utf-8"}

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p


settings = Settings()
