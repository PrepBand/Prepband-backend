from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    # Server-only Postgres connection string (Supabase project settings ->
    # Database -> Connection string). Not set in .env.example since it's a
    # real secret; /me returns a clear 503 instead of crashing until it's set.
    database_url: str | None = None

    # Public project config - not secrets, same values the frontend's own
    # .env already points at (project ref ztiuocjlthzvuonvwjzm). Defaulted
    # here so /health and JWT verification work out of the box in local dev
    # without requiring a .env file, matching the project's existing pattern.
    supabase_url: str = "https://ztiuocjlthzvuonvwjzm.supabase.co"
    supabase_jwt_audience: str = "authenticated"
    supabase_jwt_issuer: str = "https://ztiuocjlthzvuonvwjzm.supabase.co/auth/v1"
    supabase_jwks_url: str = (
        "https://ztiuocjlthzvuonvwjzm.supabase.co/auth/v1/.well-known/jwks.json"
    )

    # Private Cloudflare R2 bucket for the resource library (covers and
    # files). Server-only secrets: the API signs short-lived URLs with them,
    # so the bucket never needs to be public. Until they're set, resource
    # uploads return a clear 503 and file links can't be opened.
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_resources_bucket: str | None = None

    cors_allowed_origins: str = "http://localhost:5180"
    frontend_url: str = "http://localhost:5180"
    log_level: str = "INFO"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
