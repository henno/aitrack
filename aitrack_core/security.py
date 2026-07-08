"""Security and authentication helpers for the aitrack server.

Pure helpers live here so password/session/path handling can be reviewed without
scrolling through the HTTP and SQLite implementation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import urllib.parse

PASSWORD_HASH_ITERATIONS = 260_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
def _hash_password(password: str, *, salt: bytes | None = None, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
    if not password:
        raise ValueError("parool puudub")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"
def _verify_password(password: str, stored_hash: str | None) -> bool:
    if not password or not stored_hash:
        return False
    try:
        algo, iterations_s, salt_s, digest_s = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = _unb64(salt_s)
        expected = _unb64(digest_s)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:  # noqa: BLE001 - vigane hash tähendab vale parooli
        return False
def _session_hash(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()
def _public_user(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    return {"id": int(row["id"]), "name": row["name"], "role": row["role"]}
def _normalise_request_ip(value: str | None) -> str:
    raw = (value or "").split(",", 1)[0].strip()
    if not raw or len(raw) > 80 or any(c in raw for c in "\r\n\t "):
        return "unknown"
    if re.fullmatch(r"[0-9a-fA-F:.]+", raw):
        return raw
    return "unknown"
def _is_suspicious_request_path(path: str) -> bool:
    lowered = urllib.parse.unquote(path or "").lower()
    probes = (
        "..", "\\", "/.env", "/.git", "/wp-", "/wp/", "wp-login", "xmlrpc.php",
        "phpmyadmin", ".php", "/etc/passwd", "/cgi-bin/", "/vendor/phpunit", "/.aws",
        "/.ssh", "/server-status", "/actuator", "/debug", "/boaform/",
    )
    return any(p in lowered for p in probes)
