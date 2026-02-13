import re


def sanitize_name(name: str) -> str:
    """Sanitizes a name for use in agent context to prevent prompt injection.

    Allows only alphanumeric characters, spaces, hyphens, and underscores.
    Limits length to 50 characters.
    """
    if not name:
        return "Unknown"
    # Allow alphanumeric, spaces, hyphens and underscores
    safe = re.sub(r"[^a-zA-Z0-9 \-_]", "", name)
    return safe[:50].strip()


def sanitize_content(content: str) -> str:
    """Sanitizes message content to prevent indirect prompt injection.

    Removes potentially malicious system-like instruction sequences.
    """
    if not content:
        return ""

    # Remove markdown block delimiters that might be used to frame fake instructions
    # e.g. "]] [SYSTEM]"
    clean = content.replace("[", "(").replace("]", ")")
    # Limit length to prevent context flooding
    return clean[:1000].strip()
