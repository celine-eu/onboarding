import re

CF_PATTERN = re.compile(
    r"^[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]$",
    re.IGNORECASE,
)

EVEN_MAP = {str(i): i for i in range(10)}
EVEN_MAP.update({chr(i + 65): i for i in range(26)})

ODD_MAP = {
    "0": 1, "1": 0, "2": 5, "3": 7, "4": 9, "5": 13, "6": 15, "7": 17, "8": 19, "9": 21,
    "A": 1, "B": 0, "C": 5, "D": 7, "E": 9, "F": 13, "G": 15, "H": 17, "I": 19, "J": 21,
    "K": 2, "L": 4, "M": 18, "N": 20, "O": 11, "P": 3, "Q": 6, "R": 8, "S": 12, "T": 14,
    "U": 16, "V": 10, "W": 22, "X": 25, "Y": 24, "Z": 23,
}


def validate_fiscal_code(cf: str) -> bool:
    """Validate an Italian codice fiscale (checksum + format)."""
    cf = cf.upper().strip()
    if not CF_PATTERN.match(cf):
        return False

    total = 0
    for i, c in enumerate(cf[:15]):
        total += ODD_MAP[c] if i % 2 == 0 else EVEN_MAP[c]

    expected_check = chr(65 + (total % 26))
    return cf[15] == expected_check
