import json

from sqlalchemy import Text, TypeDecorator


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from celine.onboarding.services.crypto import encrypt_str
        return encrypt_str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from celine.onboarding.services.crypto import decrypt_str
        return decrypt_str(value)


class EncryptedJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from celine.onboarding.services.crypto import encrypt_str
        return encrypt_str(json.dumps(value, default=str))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from celine.onboarding.services.crypto import decrypt_str
        raw = decrypt_str(value)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
