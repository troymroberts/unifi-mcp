"""Settings: environment variables plus an optional consoles.yaml of aliases."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

CONFIG_DIR = Path.home() / ".config" / "unifi-mcp"
DEFAULT_CONSOLES_FILE = CONFIG_DIR / "consoles.yaml"
DEFAULT_SECRETS_FILE = CONFIG_DIR / "secrets.env"
TRUTHY = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ConsoleAlias:
    """A named console.

    Either ``local_url`` (talk to the console directly on the LAN with a local
    Network API key) or ``host`` (a Site Manager host id or hostname, reached
    through the api.ui.com Connector with the Site Manager key).
    """

    name: str
    local_url: str | None = None
    api_key_env: str | None = None
    verify_ssl: bool | str = True  # bool, or a CA bundle path
    host: str | None = None
    # MeshCentral device-group names (or other client labels) whose machines sit behind
    # this console; find_client searches these consoles first.
    groups: tuple[str, ...] = ()

    @property
    def is_local(self) -> bool:
        return self.local_url is not None


@dataclass(frozen=True)
class Settings:
    sm_api_key: str | None
    allow_writes: bool = False
    aliases: dict[str, ConsoleAlias] = field(default_factory=dict)
    default_console: str | None = None
    cloud_base_url: str = "https://api.ui.com"
    timeout: float = 30.0
    rate_limit_per_minute: int = 100


def _parse_alias(name: str, raw: Mapping) -> ConsoleAlias:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"console '{name}': expected a mapping")
    local_url = raw.get("local_url")
    host = raw.get("host")
    if bool(local_url) == bool(host):
        raise ConfigError(f"console '{name}': set exactly one of local_url or host")
    verify = raw.get("verify_ssl", True)
    if raw.get("ca_file"):
        verify = str(Path(raw["ca_file"]).expanduser())
    if local_url and not raw.get("api_key_env"):
        raise ConfigError(f"console '{name}': local consoles need api_key_env")
    groups = raw.get("groups") or ()
    if isinstance(groups, str):
        groups = (groups,)
    if not isinstance(groups, (list, tuple)) or not all(isinstance(g, str) for g in groups):
        raise ConfigError(f"console '{name}': groups must be a list of names")
    return ConsoleAlias(
        name=name,
        local_url=str(local_url).rstrip("/") if local_url else None,
        api_key_env=raw.get("api_key_env"),
        verify_ssl=verify,
        host=str(host) if host else None,
        groups=tuple(g.strip() for g in groups if g.strip()),
    )


def load_consoles_file(path: Path) -> tuple[dict[str, ConsoleAlias], str | None]:
    if not path.exists():
        return {}, None
    data = yaml.safe_load(path.read_text()) or {}
    consoles = data.get("consoles") or {}
    aliases = {name.lower(): _parse_alias(name.lower(), raw) for name, raw in consoles.items()}
    default = data.get("default")
    if default and default.lower() not in aliases:
        # A default may also be a hostname/host id resolved later via Site Manager.
        return aliases, str(default)
    return aliases, default.lower() if default else None


def load_secrets_file(path: Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Load KEY=VALUE lines into ``env`` (default os.environ) without overriding what is set.

    Keeps API keys out of MCP client configs such as ~/.claude.json. Returns warnings.
    """
    env = os.environ if env is None else env
    path = Path(path or env.get("UNIFI_SECRETS_FILE") or DEFAULT_SECRETS_FILE).expanduser()
    if not path.exists():
        return []
    warnings = []
    if path.stat().st_mode & 0o077:
        warnings.append(f"{path} is readable by other users; run: chmod 600 {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        env.setdefault(key.strip(), value.strip().strip("'\""))
    return warnings


def _get(env: Mapping[str, str], key: str) -> str | None:
    """Env value, treating blanks and unsubstituted plugin placeholders (``${...}``) as unset."""
    value = (env.get(key) or "").strip()
    return value if value and not value.startswith("${") else None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    path = Path(_get(env, "UNIFI_CONSOLES_FILE") or DEFAULT_CONSOLES_FILE).expanduser()
    aliases, default = load_consoles_file(path)
    # A single local console straight from env (how the Claude Code plugin configures "home").
    local_url = _get(env, "UNIFI_LOCAL_URL")
    if local_url:
        name = (_get(env, "UNIFI_LOCAL_NAME") or "home").lower()
        aliases.setdefault(name, ConsoleAlias(
            name=name,
            local_url=local_url.rstrip("/"),
            api_key_env="UNIFI_LOCAL_API_KEY",
            verify_ssl=(_get(env, "UNIFI_LOCAL_VERIFY_SSL") or "false").lower() in TRUTHY,
        ))
    return Settings(
        sm_api_key=_get(env, "UNIFI_SM_API_KEY"),
        allow_writes=(_get(env, "UNIFI_ALLOW_WRITES") or "").lower() in TRUTHY,
        aliases=aliases,
        default_console=_get(env, "UNIFI_DEFAULT_CONSOLE") or default,
        cloud_base_url=(_get(env, "UNIFI_CLOUD_BASE_URL") or "https://api.ui.com").rstrip("/"),
        timeout=float(_get(env, "UNIFI_TIMEOUT") or "30"),
    )
