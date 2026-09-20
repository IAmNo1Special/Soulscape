"""Shared REST actor resolution.

Souls act as themselves. Tamers act as themselves, or as a soul they
hold custody of (custody is verified again at intent enqueue and
adjudication). Operators name any actor. This is the `_rest_actor`
pattern proven in routers/social.py, shared so the marketplace and
social routers resolve identities one way.
"""

from __future__ import annotations

from fastapi import HTTPException

from .. import database
from ..security import UserIdentity


def rest_actor(
    identity: UserIdentity,
    actor_type: str | None = None,
    actor_id: str | None = None,
    *,
    operator_must_name: bool = False,
) -> tuple[str, str, str | None]:
    """Return (actor_type, actor_id, custodian_id) for a REST call.

    The caller owns the request body; this resolves who the call acts
    as. For a tamer naming ``actor_type="soul"`` without an id the
    tamer themself is returned -- routers that need a soul there
    reject the missing id themselves.
    """
    if actor_type not in (None, database.ACTOR_SOUL, database.ACTOR_TAMER):
        raise HTTPException(status_code=400, detail="Bad actor_type")
    if identity.is_operator:
        if operator_must_name and not actor_id:
            raise HTTPException(
                status_code=400, detail="Operator must specify actor_id"
            )
        return (
            actor_type or database.ACTOR_SOUL,
            actor_id or identity.id,
            None,
        )
    if identity.is_tamer:
        if actor_type == database.ACTOR_SOUL and actor_id:
            with database.get_db() as conn:
                row = conn.execute(
                    "SELECT custodian_id, owner_id FROM souls "
                    "WHERE soul_id = ?",
                    (actor_id,),
                ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="Soul not found"
                )
            custodian = row["custodian_id"] or row["owner_id"]
            if custodian != identity.custodian_id:
                raise HTTPException(
                    status_code=403, detail="No custody of this soul"
                )
            return database.ACTOR_SOUL, actor_id, identity.custodian_id
        return database.ACTOR_TAMER, identity.id, identity.custodian_id
    return (
        database.ACTOR_SOUL,
        identity.id,
        identity.custodian_id or identity.owner_id,
    )
