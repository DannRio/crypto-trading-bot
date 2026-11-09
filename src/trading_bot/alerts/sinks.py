from dataclasses import asdict
import json
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen

from trading_bot.alerts.models import AlertEvent


class AlertSink(Protocol):
    def send(self, event: AlertEvent) -> None:
        ...


def _json_payload(event: AlertEvent) -> dict:
    payload = asdict(event)
    payload["kind"] = event.kind.value
    payload["created_at"] = event.created_at.isoformat()
    return payload


class ConsoleAlertSink:
    def send(self, event: AlertEvent) -> None:
        print(
            f"[ALERT:{event.kind.value}] "
            f"{event.symbol} {event.timeframe} "
            f"{event.message}"
        )


class JsonlAlertSink:
    def __init__(
        self,
        path: str | Path = "data/alerts/events.jsonl",
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, event: AlertEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _json_payload(event),
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )


class WebhookAlertSink:
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 10.0,
    ):
        if not url:
            raise ValueError("Webhook URL is required.")
        self.url = url
        self.timeout_seconds = timeout_seconds

    def send(self, event: AlertEvent) -> None:
        body = json.dumps(_json_payload(event)).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(
            request,
            timeout=self.timeout_seconds,
        ) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise RuntimeError(
                    f"Webhook returned HTTP {status}."
                )
