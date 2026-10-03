"""Which fields are read from an uploaded document, and the only ones kept.

Apart from the extractor so that the schemas can apply the same lists to what a
client sends, without importing the model client and the PDF tooling.
"""

#: What is read from a bill, and all that is kept of what the model returns: the
#: fields the wizard fills in (name, tax code, POD, supply address) and the
#: municipality the registry area falls back to. Nothing is read only to be shown.
BILL_FIELDS = ("nome", "cognome", "codice_fiscale", "pod", "indirizzo", "comune")

#: What is read from an identity document: the three fields it is compared with
#: the bill and the form on, and its expiry, because an expired document backs no
#: verification. Birth data, sex and the document number are not read.
ID_CARD_FIELDS = ("nome", "cognome", "codice_fiscale", "scadenza")


def keep_fields(data: dict | None, fields: tuple[str, ...]) -> dict:
    """Only `fields`, whatever the model or a client sent besides."""
    data = data or {}
    return {key: data.get(key) for key in fields}


__all__ = ["BILL_FIELDS", "ID_CARD_FIELDS", "keep_fields"]
