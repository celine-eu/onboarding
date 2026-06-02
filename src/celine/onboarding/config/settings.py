from pathlib import Path

from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+asyncpg://postgres:securepassword123@172.17.0.1:15432/rec_onboarding"
    )
    openai_api_key: str = ""
    extraction_base_url: str = "https://api.openai.com/v1"
    extraction_model: str = "gpt-5.4"

    data_dir: str = str(REPO_ROOT / "data")
    template_dir: str = str(REPO_ROOT / "templates" / "example")
    max_upload_size_mb: int = 10

    model_config = {"env_file": str(REPO_ROOT / ".env"), "env_file_encoding": "utf-8"}


settings = Settings()
