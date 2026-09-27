"""Load enterprise HTTP signing material without embedding or persisting plaintext keys."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sys


class KeyMaterialError(RuntimeError):
    pass


_ENV_NAME = "ROUTING_CONSOLE_ENTERPRISE_SIGNING_KEY"


def _decode_environment_key(value: str) -> bytes:
    text = str(value or "").strip()
    try:
        if text.startswith("base64:"):
            result = base64.b64decode(text[7:], validate=True)
        elif text.startswith("hex:"):
            result = bytes.fromhex(text[4:])
        else:
            result = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise KeyMaterialError("enterprise_signing_key_encoding_invalid") from exc
    if len(result) < 32:
        raise KeyMaterialError("enterprise_signing_key_too_short")
    return result


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(value)
    return (_DATA_BLOB(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
            buffer)


def _dpapi_protect(value: bytes) -> bytes:
    if sys.platform != "win32":
        raise KeyMaterialError("windows_dpapi_unavailable")
    source, keepalive = _blob(value)
    output = _DATA_BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    # CRYPTPROTECT_UI_FORBIDDEN: local, non-interactive per-user protection.
    if not crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None,
                                    0x1, ctypes.byref(output)):
        del keepalive
        raise KeyMaterialError("windows_dpapi_protect_failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _dpapi_unprotect(value: bytes) -> bytes:
    if sys.platform != "win32":
        raise KeyMaterialError("windows_dpapi_unavailable")
    source, keepalive = _blob(value)
    output = _DATA_BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                      0x1, ctypes.byref(output)):
        del keepalive
        raise KeyMaterialError("windows_dpapi_unprotect_failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def load_http_master_key(*, deployment_mode: str, protected_path: str | Path,
                         environ: dict[str, str] | os._Environ[str] | None = None) -> bytes:
    """Return injected material, or a DPAPI-protected per-user development key.

    LAN/production never generate fallback credentials. Their key must be
    injected by the deployment secret boundary; the external Secret Manager
    integration remains pending configuration.
    """
    environment = os.environ if environ is None else environ
    injected = environment.get(_ENV_NAME)
    if injected:
        return _decode_environment_key(injected)
    if deployment_mode != "development":
        raise KeyMaterialError("external_key_material_pending_external_configuration")
    path = Path(protected_path)
    if sys.platform != "win32":
        # Non-Windows development/CI must inject an ephemeral key. This avoids
        # pretending filesystem permissions are equivalent to Windows DPAPI.
        raise KeyMaterialError("development_signing_key_injection_required")
    if path.exists():
        try:
            key = _dpapi_unprotect(path.read_bytes())
        except OSError as exc:
            raise KeyMaterialError("development_key_material_unreadable") from exc
        if len(key) < 32:
            raise KeyMaterialError("development_key_material_invalid")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(48)
    protected = _dpapi_protect(key)
    try:
        with path.open("xb") as handle:
            handle.write(protected)
    except FileExistsError:
        return _dpapi_unprotect(path.read_bytes())
    return key


def derive_key(master: bytes, purpose: str) -> bytes:
    if len(master) < 32 or not purpose:
        raise KeyMaterialError("enterprise_key_derivation_input_invalid")
    return hmac.new(master, ("routing-console:" + purpose).encode("utf-8"),
                    hashlib.sha256).digest()
