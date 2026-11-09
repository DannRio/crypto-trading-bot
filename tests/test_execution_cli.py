import json

import pytest

from trading_bot.execution.cli import main
from trading_bot.execution import OrderStore


def test_execution_cli_status_is_disarmed_by_default(
    tmp_path,
    capsys,
):
    db = tmp_path / "live.db"

    assert main(
        [
            "--order-db",
            str(db),
            "status",
        ]
    ) == 0

    payload = json.loads(
        capsys.readouterr().out
    )
    assert payload["environment_enabled"] is False
    assert payload["control"]["armed"] is False
    assert payload["control"]["halted"] is False
    assert payload["context"] is None


def test_execution_cli_arm_requires_environment_gate(
    monkeypatch,
    tmp_path,
):
    db = tmp_path / "live.db"
    monkeypatch.delenv(
        "TRADING_LIVE_EXECUTION",
        raising=False,
    )

    with pytest.raises(
        RuntimeError,
        match="Cannot arm",
    ):
        main(
            [
                "--order-db",
                str(db),
                "arm",
                "--actor",
                "Danny",
                "--confirm",
                "ARM_LIVE",
                "--exchange",
                "kucoin",
                "--market-type",
                "spot",
            ]
        )

    store = OrderStore(db)
    assert store.load_execution_context() == {
        "exchange_id": "kucoin",
        "market_type": "spot",
        "created_at": store.load_execution_context()[
            "created_at"
        ],
    }
    assert store.load_execution_control().armed is False
    store.close()


def test_execution_cli_arm_and_emergency_halt_persist(
    monkeypatch,
    tmp_path,
):
    db = tmp_path / "live.db"
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )

    assert main(
        [
            "--order-db",
            str(db),
            "arm",
            "--actor",
            "Danny",
            "--confirm",
            "ARM_LIVE",
            "--exchange",
            "kucoin",
            "--market-type",
            "spot",
        ]
    ) == 0

    assert main(
        [
            "--order-db",
            str(db),
            "trip",
            "--actor",
            "Danny",
            "--reason",
            "manual test halt",
            "--no-cancel",
        ]
    ) == 0

    store = OrderStore(db)
    state = store.load_execution_control()
    assert state.armed is False
    assert state.halted is True
    assert state.halt_reason == "manual test halt"
    store.close()


def test_execution_cli_live_submit_requires_exact_confirmation(
    tmp_path,
):
    db = tmp_path / "live.db"

    with pytest.raises(
        ValueError,
        match="SUBMIT_LIVE",
    ):
        main(
            [
                "--order-db",
                str(db),
                "live-submit-intent",
                "--intent-id",
                "intent-1",
                "--exchange",
                "kucoin",
                "--market-type",
                "spot",
                "--confirm",
                "NO",
            ]
        )
