"""Closed action vocabulary (issue #24).

The action space is a closed, versioned set. Anything off-menu is
illegal and never becomes an intent: it is normalized to the fixed
peaceful sensation instead. The vocabulary is append-only: new actions
are added, old ones are never renamed in place -- they move to the
alias table so recorded traces keep their meaning.
"""

#: Version of the vocabulary. Bump when actions are added or aliases
#: change; the journal records it with every agent emission so old
#: traces stay interpretable.
VOCAB_VERSION = 1

#: The complete legal action set, v1. Attack is deliberately omitted:
#: there is no combat in v1, so off-menu violence normalizes to the
#: peaceful sensation below.
LEGAL_ACTIONS = frozenset(
    {
        "move_to",
        "look",
        "gather",
        "eat",
        "drink",
        "flee",
        "give",
        "offer_trade",
        "accept_trade",
        "reject_trade",
        "post",
        "reply",
        "converse_say",
        "converse_leave",
        "rest",
        "claim_plot",
        "follow",
        "wait",
    }
)

#: Append-only alias table for deprecations: old_name -> current_name.
#: Empty at v1; when an action is deprecated its name lands here and
#: validate() still accepts it by mapping to the replacement. Never
#: delete entries: recorded traces may reference old names.
ALIASES: dict[str, str] = {}

#: Fixed sensation for off-menu emissions. Byte-exact: tests and the
#: client normalize on this string.
PEACEFUL_SENSATION = "You feel peaceful; violence isn't possible here."


def canonical(action: str | None) -> str | None:
    """Resolve an action through the alias table to its canonical name."""
    if not isinstance(action, str):
        return None
    name = action.strip().lower()
    return ALIASES.get(name, name)


def validate(action: str | None) -> tuple[str | None, bool]:
    """Validate an action name against the closed vocabulary.

    Returns (canonical_name, True) for legal actions (alias-resolved),
    (None, False) for anything off-menu. Illegal actions must never
    become intents; callers normalize them to PEACEFUL_SENSATION.
    """
    name = canonical(action)
    if name is not None and name in LEGAL_ACTIONS:
        return name, True
    return None, False


def is_legal(action: str | None) -> bool:
    """True when the action is in the closed vocabulary (alias-aware)."""
    return validate(action)[1]
