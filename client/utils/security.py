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


def redact_secret(text: str, secret: str) -> str:
    """Redacts a sensitive secret from a string to prevent leakage in logs."""
    if not secret or not text:
        return text
    return text.replace(secret, "[REDACTED]")


def sanitize_content(content: str, is_untrusted: bool = True) -> str:
    """Sanitizes message content to prevent indirect prompt injection.

    Args:
        content: The text to sanitize.
        is_untrusted: If True, wraps the content in tags to mark it as untrusted.
    """
    if not content:
        return ""

    # Remove markdown block delimiters and other framing characters
    clean = (
        content.replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
    )
    # Escape tags to prevent spoofing of markers
    clean = clean.replace("<", "&lt;").replace(">", "&gt;")

    # Limit length to prevent context flooding
    clean = clean[:1000].strip()

    if is_untrusted:
        # Wrap in clear markers for the LLM
        return f"\n<UNTRUSTED_CONTENT>\n{clean}\n</UNTRUSTED_CONTENT>\n"

    return clean
