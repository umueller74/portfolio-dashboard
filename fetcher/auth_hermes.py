import sys

HERMES_DIR = "/home/hermes/.hermes/hermes-agent"
SERVER = "scalable"
REDIRECT_PORT = 8765  # same as mcp_servers.scalable.oauth.redirect_port in Hermes' config


def bootstrap() -> None:
    """Make Hermes' dependencies (`mcp`, `tools.mcp_oauth`) importable. Call before importing `mcp`."""
    if HERMES_DIR not in sys.path:
        sys.path.insert(0, HERMES_DIR)
    import hermes_bootstrap  # noqa: F401  (adds Hermes' venv with `mcp` to sys.path)


def build_auth(url: str):
    """OAuth provider backed by Hermes' token store. Raises if no cached tokens exist."""
    bootstrap()
    from tools.mcp_oauth import build_oauth_auth

    return build_oauth_auth(SERVER, url, {"redirect_port": REDIRECT_PORT})
