from hashlib import sha256


BOT_CLIENT_ORDER_PREFIX = "tb-"


def client_order_id_for_intent(
    intent_id: str,
) -> str:
    intent_id = intent_id.strip()
    if not intent_id:
        raise ValueError("intent_id is required.")
    digest = sha256(
        intent_id.encode("utf-8")
    ).hexdigest()[:24]
    return BOT_CLIENT_ORDER_PREFIX + digest


def is_bot_client_order_id(
    client_order_id: str | None,
) -> bool:
    return bool(
        client_order_id
        and client_order_id.startswith(
            BOT_CLIENT_ORDER_PREFIX
        )
    )
