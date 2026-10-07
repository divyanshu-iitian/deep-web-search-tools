from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    search_provider: str = "auto"
    brave_search_api_key: str = ""
    searxng_url: str = ""
    github_token: str = ""
    app_api_key: str = ""
    data_dir: str = "./data"
    result_cache_hours: int = 24
    max_results_per_query: int = 8
    organization_keyless_search: bool = True


settings = Settings()
