"""Managed entry point for the independent Host.

The bootstrap credential is generated once, stored in the existing encrypted
Windows-user vault and injected into the Host process only through memory.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn

from backend.agent_skill_host_service import AgentSkillHostService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import TenantScope


ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("ROUTING_CONSOLE_STATE_DIR") or
             Path.home() / "AppData/Local/IntelligentChannelScheduler")
DATABASE = Path(os.environ.get("ROUTING_CONSOLE_DATABASE_PATH") or
                STATE / "data/routing_quality_console.sqlite3")
HOST_DATABASE = Path(os.environ.get("EXTERNAL_AGENT_HOST_DB") or
                     STATE / "data/external-agent-host.sqlite3")
HOST_URL = "http://127.0.0.1:8010"


def load_or_create_token() -> str:
    vault = PersistentCredentialVault()
    scope = CredentialScope("external-agent-host-runtime", "local", "local", "host-bootstrap-8010")
    loaded = vault.load(scope, required=False)
    if loaded:
        return loaded[0]
    token = secrets.token_urlsafe(48)
    vault.save(scope, token)
    return token


def provision_main_control_plane(token: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urlopen(HOST_URL + "/health", timeout=1):
                break
        except Exception:
            time.sleep(.25)
    else:
        return
    service = AgentSkillHostService(
        DATABASE, scope=TenantScope.local_development(), allowed_hosts={"127.0.0.1"},
        allow_loopback_http=True, loopback_ports={8010}, request_timeout_seconds=5)
    matches = [item for item in service.list_hosts()["items"]
               if item.get("base_url") == HOST_URL and item.get("host_type") == "independent_http_runtime"]
    if matches:
        host = matches[0]
    else:
        host = service.create_host(host_name="本地独立 Agent Host", host_type="independent_http_runtime",
            base_url=HOST_URL, environment="local_acceptance", protocol="formal-agent-skill",
            protocol_version="1.0", auth_type="bearer", operator_id="runtime-bootstrap")
    try:
        service.set_credentials(host["host_id"], token, auth_type="bearer", operator_id="runtime-bootstrap")
        service.test_connection(host["host_id"], operator_id="runtime-bootstrap")
        service.enable_host(host["host_id"], operator_id="runtime-bootstrap")
    except Exception:
        # The main /ready endpoint and Host audit expose any provisioning fault;
        # never print or log the credential in this process.
        return


def main() -> None:
    token = load_or_create_token()
    os.environ["EXTERNAL_AGENT_HOST_TOKEN"] = token
    os.environ["EXTERNAL_AGENT_HOST_DB"] = str(HOST_DATABASE)
    from external_agent_host.app import app
    threading.Thread(target=provision_main_control_plane, args=(token,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8010, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
