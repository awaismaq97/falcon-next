"""
settings.py — FastAPI-layer configuration (transport concerns only).

This is intentionally separate from ``falcon.config`` (the domain config that
loads config.yaml and the OpenRouter/Mongo secrets). Here we only configure how
the HTTP server behaves: CORS, docs exposure, and the server bind. Everything is
overridable via environment variables so DigitalOcean App Platform can inject
values without code changes.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Comma-separated list of allowed CORS origins. "*" allows any origin, which
    # is safe here because the app carries no auth cookies/credentials. In
    # production set this to the frontend URL, e.g.
    #   CORS_ORIGINS=https://falcon-xxxx.ondigitalocean.app
    cors_origins: str = "*"

    # Expose /docs and /redoc. Handy in dev; can be disabled in production.
    enable_docs: bool = True

    # Prefix every API route lives under. The frontend calls "<backend>/api/...".
    api_prefix: str = "/api"

    # App metadata
    app_title: str = "Falcon API"
    app_version: str = "2.0.0"

    # ElevenLabs text-to-speech. Optional: if this is empty the /voice endpoints
    # report themselves disabled and the UI hides the audio controls. Set it in
    # backend/.env locally, or as an App-Level SECRET in production. The key never
    # reaches the browser — every TTS call is proxied through the backend.
    elevenlabs_api_key: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
