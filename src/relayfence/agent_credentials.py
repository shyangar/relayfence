"""Encrypted local API-key storage for installed agent runtimes.

Only ciphertext and a short display hint are written to SQLite.  The Fernet key
lives beside the local project index in a private file so copying the ledger does
not disclose the configured provider keys.  Plaintext is decrypted only when a
local runner starts a task.
"""

import os
import re
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]*$")
_MAX_SECRET = 4096


def mask_secret(value):
    """Return a stable hint that never contains the complete secret."""
    value = str(value)
    if len(value) >= 12:
        return "••••••••" + value[-4:]
    if len(value) >= 4:
        return "••••" + value[-2:]
    return "••••"


class AgentCredentials:
    """Encrypt provider keys at rest and decrypt them for local task execution."""

    def __init__(self, store, directory, key_name="agent-credentials.key"):
        self.store = store
        self.directory = Path(directory)
        self.key_path = self.directory / key_name
        self._key_lock = threading.Lock()

    @staticmethod
    def _identifier(value, label):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"Invalid {label}")
        return value

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise ValueError("Invalid credential name")
        return value

    def _fernet(self):
        with self._key_lock:
            try:
                key = self.key_path.read_bytes().strip()
            except FileNotFoundError:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                key = Fernet.generate_key()
                try:
                    fd = os.open(
                        self.key_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    # Another process may have created the file but not finished
                    # writing it. Retry briefly instead of caching an empty key.
                    for _ in range(20):
                        try:
                            key = self.key_path.read_bytes().strip()
                        except FileNotFoundError:
                            key = b""
                        if key:
                            break
                        time.sleep(0.01)
                    else:
                        raise ValueError(
                            "Stored credential key is being initialized; retry"
                        ) from None
                else:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(key + b"\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.chmod(self.key_path, 0o600)
                    except OSError:
                        pass
            try:
                return Fernet(key)
            except (ValueError, TypeError) as exc:
                raise ValueError("Stored credential key is invalid; reconfigure keys") from exc

    def save(self, software, provider, name, value):
        software = self._identifier(software, "software")
        provider = self._identifier(provider, "provider")
        name = self._name(name)
        if name.startswith("RELAYFENCE_"):
            raise ValueError("Reserved credential name")
        if not isinstance(value, str):
            raise ValueError("请输入密钥")
        value = value.strip()
        if (
            not value
            or len(value) > _MAX_SECRET
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("密钥格式无效")
        ciphertext = self._fernet().encrypt(value.encode("utf-8")).decode("ascii")
        hint = mask_secret(value)
        now = self.store.clock()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO agent_credentials(software,provider,name,ciphertext,hint,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(software,provider,name) DO UPDATE SET "
                "ciphertext=excluded.ciphertext,hint=excluded.hint,updated_at=excluded.updated_at",
                (software, provider, name, ciphertext, hint, now, now),
            )
        return hint

    def delete(self, software, provider, name):
        software = self._identifier(software, "software")
        provider = self._identifier(provider, "provider")
        name = self._name(name)
        with self.store.connection() as db:
            cursor = db.execute(
                "DELETE FROM agent_credentials WHERE software=? AND provider=? AND name=?",
                (software, provider, name),
            )
        return cursor.rowcount > 0

    def hint(self, software, provider, name):
        if not software or not provider or not name:
            return None
        with self.store.connection() as db:
            row = db.execute(
                "SELECT hint FROM agent_credentials WHERE software=? AND provider=? AND name=?",
                (software, provider, name),
            ).fetchone()
        return row["hint"] if row else None

    def environment(self, software, provider, names=None):
        """Decrypt keys for one provider immediately before a local execution.

        ``names`` restricts injection to the env names the current catalogue
        advertises for this provider, so rows saved under an old or dropped env
        name are not silently injected.
        """
        if not software or not provider:
            return {}
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT name,ciphertext FROM agent_credentials WHERE software=? AND provider=?",
                (software, provider),
            ).fetchall()
        if not rows:
            return {}
        if names is not None:
            rows = [row for row in rows if row["name"] in set(names)]
            if not rows:
                return {}
        fernet = self._fernet()
        result = {}
        for row in rows:
            if row["name"].startswith("RELAYFENCE_"):
                continue
            try:
                result[row["name"]] = fernet.decrypt(row["ciphertext"].encode("ascii")).decode(
                    "utf-8"
                )
            except (InvalidToken, UnicodeError, ValueError) as exc:
                raise ValueError("已保存的密钥无法解密，请重新配置该供应商的密钥") from exc
        return result

    def annotate(self, catalogue):
        """Merge stored-key availability into a freshly discovered catalogue."""
        for software in catalogue:
            software_id = software.get("id")
            for provider in software.get("providers", []):
                auth = provider.get("auth")
                if not isinstance(auth, dict):
                    continue
                env_name = auth.get("credential_env")
                hint = self.hint(software_id, provider.get("id"), env_name) if env_name else None
                auth["stored"] = bool(hint)
                auth["masked"] = hint
                if hint:
                    try:
                        # Prove the row is still decryptable. A copied ledger
                        # without its key file must not look authenticated.
                        self.environment(software_id, provider.get("id"), {env_name})
                    except ValueError:
                        auth["authenticated"] = False
                        auth["source"] = None
                        auth["message"] = "已保存的密钥无法解密，请重新配置该供应商的密钥"
                    else:
                        auth["authenticated"] = True
                        auth["source"] = "database"
                        auth["message"] = "已使用 RelayFence 加密保存的密钥"
                elif auth.get("source") == "database":
                    auth["authenticated"] = False
                    auth["source"] = None
                    if auth.get("kind") == "api_key" and env_name:
                        auth["message"] = f"缺少 {env_name}，请配置密钥"
                    else:
                        auth["message"] = "需要重新登录或配置密钥"
                elif not auth.get("authenticated") and auth.get("kind") == "api_key" and env_name:
                    auth["message"] = f"缺少 {env_name}，请配置密钥"
                provider["authenticated"] = bool(auth.get("authenticated"))
        return catalogue
