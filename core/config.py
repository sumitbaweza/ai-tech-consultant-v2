"""Settings for every service, read from environment variables.

In Docker, Compose passes the variables in. Run on this computer, the repo's .env file is read
too. A real environment variable always wins over the .env file.

Nothing here is a model key: each business stores its own keys, encrypted, in Settings.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

REPO_ROOT = Path(__file__).resolve().parents[1]

# The three database login roles (db/init/01-roles.sh)
DbRole = Literal["owner", "web", "brain"]
_ROLE_USERS: dict[DbRole, str] = {
    "owner": "consultant_owner",
    "web": "web_user",
    "brain": "brain_user",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # Postgres. 127.0.0.1, not localhost: on Windows, localhost tries IPv6 first, where
    # nothing listens, and each refused attempt costs about 2 seconds.
    db_host: str = "127.0.0.1"
    db_port: int = 5442
    db_name: str = "consultant"
    owner_db_password: SecretStr = SecretStr("owner")
    web_db_password: SecretStr = SecretStr("web")
    brain_db_password: SecretStr = SecretStr("brain")

    # 32 random bytes, base64: encrypts every stored key and token (core/crypto.py)
    secret_key: SecretStr = SecretStr("")

    # Where the web app finds the Knowledge Brain's API, and where people open the app
    brain_api_url: str = "http://127.0.0.1:8081"
    public_url: str = "http://localhost:8080"

    # Platform-level services. Each may be empty; the feature that needs it says so.
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    voyage_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_repo: str = ""
    ollama_url: str = "http://localhost:11434"
    # The fixture website for the 10 URL benchmark cases (the fixture-site service)
    fixture_site_url: str = "http://fixture-site:8083"
    # The business API key the MCP server uses (Settings > Members and API keys)
    brain_mcp_api_key: SecretStr = SecretStr("")
    # Demo logins made by the seed (development only)
    demo_password: SecretStr = SecretStr("demo1234")

    # Screenshots, uploaded documents, lesson files and Self-Improve reports
    storage_dir: Path = REPO_ROOT / "storage"
    # The git repo the Self-Improve Loop works in (a bind mount in Docker)
    self_improve_repo: Path = REPO_ROOT

    # Crawler politeness: who we say we are, and the most requests per second to one host
    crawler_user_agent: str = "AITechConsultantBot/2.0 (polite help-center reader)"
    crawler_requests_per_second: float = 1.0

    # Development only: answer every model call with a canned fake reply, for clicking
    # through the screens without any key. The screens show a banner while it's on.
    llm_fake: bool = False

    def database_url(self, role: DbRole) -> URL:
        # URL.create escapes special characters in the password; an f-string would not
        password = {
            "owner": self.owner_db_password,
            "web": self.web_db_password,
            "brain": self.brain_db_password,
        }[role]
        return URL.create(
            "postgresql+psycopg",
            username=_ROLE_USERS[role],
            password=password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    def conninfo(self, role: DbRole) -> str:
        """The same connection as a libpq string, for Procrastinate's psycopg connector."""
        return (
            self.database_url(role)
            .render_as_string(hide_password=False)
            .replace("postgresql+psycopg", "postgresql")
        )


@lru_cache
def get_settings() -> Settings:
    """One Settings object per process. Tests call get_settings.cache_clear() to change it."""
    return Settings()
