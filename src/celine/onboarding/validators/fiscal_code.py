import re

CF_PATTERN = re.compile(
    r"^(?:[A-Z][AEIOUX][AEIOUX]|[B-DF-HJ-NP-TV-Z]{2}[A-Z]){2}"
    r"(?:[\dLMNP-V]{2}(?:[A-EHLMPR-T](?:[04LQ][1-9MNP-V]|[15MR][\dLMNP-V]|[26NS][0-8LMNP-U])"
    r"|[DHPS][37PT][0L]|[ACELMRT][37PT][01LM]|[AC-EHLMPR-T][26NS][9V])"
    r"|(?:[02468LNQSU][048LQU]|[13579MPRTV][26NS])B[26NS][9V])"
    r"(?:[A-MZ][1-9MNP-V][\dLMNP-V]{2}|[A-M][0L](?:[1-9MNP-V][\dLMNP-V]|[0L][1-9MNP-V]))[A-Z]$"
)


def validate_fiscal_code(cf: str) -> bool:
    cf = cf.upper().strip()
    return bool(CF_PATTERN.match(cf))
