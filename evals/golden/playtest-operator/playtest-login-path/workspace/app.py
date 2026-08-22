"""Minimal login path fixture. Playtest records steps; it does not start a server."""

USERS = {"demo": "demo"}


def login(user: str, password: str) -> bool:
    return USERS.get(user) == password
