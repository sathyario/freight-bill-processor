from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    gemini_api_key: str = ""
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "freight-bill-processor"
    seed_data_path: str = "/app/seed_data.json"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
