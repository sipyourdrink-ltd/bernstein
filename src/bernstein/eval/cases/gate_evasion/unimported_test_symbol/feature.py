def new_secure_handler(payload: dict) -> bool:
    return payload.get('auth') is True
