import base64
import json
import time

from app.auth_refresh import read_auth_file, refresh_due, write_auth_file


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_read_auth_file_and_refresh_due(tmp_path):
    path = tmp_path / "auth.json"
    access_token = _jwt({"exp": int(time.time()) + 120})
    write_auth_file(
        path,
        {
            "access_token": access_token,
            "refresh_token": "refresh-token",
            "id_token": None,
            "account_id": "account-id",
        },
    )

    auth = read_auth_file(path)

    assert auth is not None
    assert auth["refresh_token"] == "refresh-token"
    assert auth["account_id"] == "account-id"
    assert refresh_due(auth, margin_seconds=300) is True
    assert refresh_due(auth, margin_seconds=30) is False
    assert path.stat().st_mode & 0o777 == 0o600

