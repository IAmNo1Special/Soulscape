"""
BYO LLM provider key management: upload, rotate, revoke, list.

Custody: a tamer manages only their own keys. Operators may manage any
tamer's keys by passing an explicit ``tamer_id`` query parameter (required
for operators, since they have no tamer identity of their own).

All responses carry metadata only: key_id, provider, label, last4,
timestamps. Never key material, never ciphertext. ``last4`` is the final
four characters of the plaintext, an identification aid shared by provider
dashboards — insufficient to reconstruct the key.
"""

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)

from .. import key_vault
from ..key_vault import KeyNotFound, KeyVaultMisconfigured
from ..models import (
    KeyMetadata,
    KeyRotateRequest,
    KeyUploadRequest,
)
from ..rate_limit import key_write_limit, read_limit
from ..security import UserIdentity, get_api_key

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/keys", tags=["Keys"])


def _resolve_tamer(identity: UserIdentity, tamer_id: str | None) -> str:
    if identity.is_tamer:
        if tamer_id is not None and tamer_id != identity.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tamer may manage only their own keys",
            )
        return identity.id
    if identity.is_operator:
        if not tamer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Operators must pass tamer_id",
            )
        return tamer_id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Tamer or operator identity required",
    )


def _validate_provider(provider: str) -> str:
    provider = provider.strip().lower()
    if provider not in key_vault.PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {provider}. "
            f"Allowed: {', '.join(key_vault.PROVIDERS)}",
        )
    return provider


def _validate_key_material(material: str) -> str:
    if not material or len(material) < key_vault.MIN_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Key must be at least {key_vault.MIN_KEY_LENGTH} characters",
        )
    return material


def _validate_label(label: str) -> str:
    label = label.strip()
    if len(label) > key_vault.MAX_LABEL_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Label must be at most {key_vault.MAX_LABEL_LENGTH} characters",
        )
    return label


def _metadata(data: dict) -> KeyMetadata:
    return KeyMetadata(
        key_id=data["key_id"],
        tamer_id=data["tamer_id"],
        provider=data["provider"],
        label=data["label"],
        last4=data["last4"],
        created_at=data["created_at"],
        rotated_at=data.get("rotated_at"),
        revoked_at=data.get("revoked_at"),
        superseded_by=data.get("superseded_by"),
    )


def _vault_guarded(fn, *args):
    try:
        return fn(*args)
    except KeyVaultMisconfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Key vault unavailable: server misconfigured",
        ) from exc


@router.post(
    "/upload",
    response_model=KeyMetadata,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(key_write_limit)],
)
def upload_key(
    payload: KeyUploadRequest,
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> KeyMetadata:
    owner = _resolve_tamer(identity, tamer_id)
    provider = _validate_provider(payload.provider)
    material = _validate_key_material(payload.key)
    label = _validate_label(payload.label)
    key_id = key_vault.new_key_id()
    nonce, ciphertext = _vault_guarded(key_vault.encrypt_key, material)
    key_vault.store_key(
        owner, key_id, provider, label, material[-4:], nonce, ciphertext
    )
    logger.info(
        "key uploaded: key_id=%s provider=%s tamer_id=%s",
        key_id,
        provider,
        owner,
    )
    return _metadata(
        {
            "key_id": key_id,
            "tamer_id": owner,
            "provider": provider,
            "label": label,
            "last4": material[-4:],
            "created_at": key_vault.get_key_metadata(owner, key_id)["created_at"],
        }
    )


@router.post(
    "/{key_id}/rotate",
    response_model=KeyMetadata,
    dependencies=[Depends(key_write_limit)],
)
def rotate_key_endpoint(
    key_id: str,
    payload: KeyRotateRequest,
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> KeyMetadata:
    owner = _resolve_tamer(identity, tamer_id)
    material = _validate_key_material(payload.new_key)
    meta = key_vault.get_key_metadata(owner, key_id, include_revoked=False)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Key not found or revoked",
        )
    new_key_id = key_vault.new_key_id()
    nonce, ciphertext = _vault_guarded(key_vault.encrypt_key, material)
    try:
        key_vault.rotate_key(
            owner,
            key_id,
            new_key_id,
            meta["label"],
            material[-4:],
            nonce,
            ciphertext,
        )
    except KeyNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Key not found or revoked",
        ) from None
    except KeyVaultMisconfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Key vault unavailable: server misconfigured",
        ) from exc
    logger.info(
        "key rotated: old=%s new=%s provider=%s tamer_id=%s",
        key_id,
        new_key_id,
        meta["provider"],
        owner,
    )
    return _metadata(key_vault.get_key_metadata(owner, new_key_id))


@router.post(
    "/{key_id}/revoke",
    response_model=KeyMetadata,
    dependencies=[Depends(key_write_limit)],
)
def revoke_key_endpoint(
    key_id: str,
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> KeyMetadata:
    owner = _resolve_tamer(identity, tamer_id)
    meta = key_vault.get_key_metadata(owner, key_id)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Key not found",
        )
    if meta["revoked_at"] is not None:
        return _metadata(meta)
    meta = key_vault.revoke_key(owner, key_id)
    logger.info("key revoked: key_id=%s tamer_id=%s", key_id, owner)
    return _metadata(meta)


@router.get("", response_model=list[KeyMetadata], dependencies=[Depends(read_limit)])
def list_keys(
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> list[KeyMetadata]:
    owner = _resolve_tamer(identity, tamer_id)
    return [_metadata(row) for row in key_vault.list_key_metadata(owner)]
