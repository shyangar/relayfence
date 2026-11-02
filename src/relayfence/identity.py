"""Transport-authenticated identities. An actor label alone is not a credential."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    admin: bool = False
    token_hash: str = ""


# In-process callers already have direct access to the database. HTTP must authenticate.
ADMIN = Principal(admin=True)
