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
    # Politeness: seconds between requests to one host (plus random jitter), and between
    # keyless search queries. Throttled hosts/engines are left alone until their cooldown ends.
    crawl_interval_seconds: float = 1.5
    crawl_jitter_seconds: float = 1.0
    keyless_search_interval_seconds: float = 8.0
    search_cooldown_minutes: int = 30
    named_contact_search: bool = True
    max_documents_per_organization: int = 3


settings = Settings()
