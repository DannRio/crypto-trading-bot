from dataclasses import dataclass, field
import os
import re


@dataclass(frozen=True)
class ExchangeCredentials:
    api_key: str = field(repr=False)
    secret: str = field(repr=False)
    password: str | None = field(default=None, repr=False)
    uid: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if not self.api_key.strip():
            raise ValueError("api_key is required.")
        if not self.secret.strip():
            raise ValueError("secret is required.")


class EnvironmentCredentialProvider:
    """Loads exchange credentials from process environment only."""

    def __init__(self, prefix: str = "TRADING"):
        prefix = prefix.strip().upper()
        if not prefix:
            raise ValueError("Credential environment prefix is required.")
        self.prefix = prefix

    @staticmethod
    def _exchange_token(exchange_id: str) -> str:
        token = re.sub(
            r"[^A-Z0-9]+",
            "_",
            exchange_id.strip().upper(),
        ).strip("_")
        if not token:
            raise ValueError("exchange_id is required.")
        return token

    def variable_names(self, exchange_id: str) -> dict[str, str]:
        token = self._exchange_token(exchange_id)
        base = f"{self.prefix}_{token}"
        return {
            "api_key": f"{base}_API_KEY",
            "secret": f"{base}_SECRET",
            "password": f"{base}_PASSWORD",
            "uid": f"{base}_UID",
        }

    def load(self, exchange_id: str) -> ExchangeCredentials:
        names = self.variable_names(exchange_id)
        api_key = os.getenv(names["api_key"], "").strip()
        secret = os.getenv(names["secret"], "").strip()

        missing = [
            names["api_key"]
            for value in [api_key]
            if not value
        ] + [
            names["secret"]
            for value in [secret]
            if not value
        ]
        if missing:
            raise ValueError(
                "Missing exchange credential environment variables: "
                + ", ".join(missing)
            )

        password = os.getenv(names["password"])
        uid = os.getenv(names["uid"])

        return ExchangeCredentials(
            api_key=api_key,
            secret=secret,
            password=(
                password.strip()
                if password and password.strip()
                else None
            ),
            uid=(
                uid.strip()
                if uid and uid.strip()
                else None
            ),
        )
