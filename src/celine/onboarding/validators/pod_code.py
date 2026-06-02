import re

POD_PATTERN = re.compile(r"^IT\d{3}E\d{8,9}$", re.IGNORECASE)


def validate_pod_code(pod: str) -> bool:
    """Validate an Italian POD code format (IT + 3 digits + E + 8-9 digits)."""
    return bool(POD_PATTERN.match(pod.strip()))
