"""Encryption at rest: a key list, loud failures, rotation, and a separate OTP key."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from celine.onboarding.config.settings import settings
from celine.onboarding.services import crypto, key_rotation, otp

OLD = Fernet.generate_key().decode()
NEW = Fernet.generate_key().decode()


@pytest.fixture()
def keys(monkeypatch):
    """Set ENCRYPTION_KEY and drop the cached keys, before and after."""

    def _set(value: str):
        monkeypatch.setattr(settings, "encryption_key", value)
        crypto.reset()

    yield _set
    crypto.reset()


class TestKeyList:
    def test_the_first_key_encrypts_and_every_key_decrypts(self, keys):
        """
        @verifies REQ-0032
        """
        keys(OLD)
        under_old = crypto.encrypt_str("Mario")
        keys(f"{NEW},{OLD}")
        assert crypto.decrypt_str(under_old) == "Mario"
        under_new = crypto.encrypt_str("Mario")
        Fernet(NEW.encode()).decrypt(under_new.encode())

    def test_a_malformed_key_is_named_by_position_only(self):
        with pytest.raises(ValueError) as exc:
            crypto.parse_keys(f"{NEW},not-a-key")
        assert "#2" in str(exc.value)
        assert "not-a-key" not in str(exc.value)


class TestFailuresAreLoud:
    def test_a_token_no_key_opens_raises(self, keys, caplog):
        """Never the ciphertext handed back as if it were the value.

        @verifies REQ-0032
        """
        keys(OLD)
        token = crypto.encrypt_str("RSSMRA85T10A562S")
        keys(NEW)
        with caplog.at_level(logging.ERROR, logger=crypto.__name__):
            with pytest.raises(crypto.DecryptionError):
                crypto.decrypt_str(token)
        assert caplog.records
        assert "RSSMRA85T10A562S" not in caplog.text

    def test_a_document_no_key_opens_raises(self, keys):
        """
        @verifies REQ-0032
        """
        keys(OLD)
        token = crypto.encrypt(b"%PDF-1.4")
        keys(NEW)
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(token)

    def test_a_token_without_any_key_raises(self, keys):
        """
        @verifies REQ-0032
        """
        keys(OLD)
        token = crypto.encrypt_str("Mario")
        keys("")
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_str(token)

    def test_a_value_stored_unencrypted_is_read_with_a_warning(self, keys, caplog):
        """Written while no key was set: read as it is, and named in the log.

        @verifies REQ-0032
        """
        keys(NEW)
        with caplog.at_level(logging.WARNING, logger=crypto.__name__):
            assert crypto.decrypt_str("Mario") == "Mario"
            assert crypto.decrypt(b"%PDF-1.4 plain") == b"%PDF-1.4 plain"
        assert "rotate-encryption-key" in caplog.text

    def test_without_a_key_plaintext_round_trips(self, keys):
        keys("")
        assert crypto.encrypt_str("Mario") == "Mario"
        assert crypto.decrypt_str("Mario") == "Mario"


class TestRotation:
    def test_rotate_moves_a_value_to_the_first_key(self, keys):
        """
        @verifies REQ-0032
        """
        keys(OLD)
        token = crypto.encrypt(b"Mario")
        keys(f"{NEW},{OLD}")
        rotated = crypto.rotate(token)
        assert Fernet(NEW.encode()).decrypt(rotated) == b"Mario"
        assert crypto.rotate(rotated) is None
        assert Fernet(NEW.encode()).decrypt(crypto.rotate(b"plain")) == b"plain"

    async def test_columns_are_rewritten_and_an_unreadable_one_is_reported(self, keys, monkeypatch):
        """
        @verifies REQ-0032
        """
        from celine.onboarding.models.submission import Submission

        keys(OLD)
        old = crypto.encrypt_str("Mario")
        keys(Fernet.generate_key().decode())
        foreign = crypto.encrypt_str("Luigi")
        keys(f"{NEW},{OLD}")

        table = Submission.__table__
        monkeypatch.setattr(
            key_rotation, "encrypted_columns", lambda: [(table, table.c.first_name)]
        )
        ids = [uuid.uuid4() for _ in range(3)]
        selected = MagicMock()
        selected.all.return_value = [(ids[0], old), (ids[1], "Anna"), (ids[2], foreign)]
        statements = []

        async def _execute(stmt):
            statements.append(stmt)
            return selected

        db = SimpleNamespace(execute=_execute, commit=AsyncMock())
        report = key_rotation.RotationReport()
        await key_rotation.rotate_columns(db, report)

        updates = [s.compile().params for s in statements[1:]]
        assert len(updates) == 2
        for params in updates:
            value = next(v for k, v in params.items() if not k.startswith("id"))
            assert Fernet(NEW.encode()).decrypt(value.encode()) in (b"Mario", b"Anna")
        assert report.rewritten == {"submissions.first_name": 2}
        assert len(report.undecryptable) == 1 and str(ids[2]) in report.undecryptable[0]
        assert not report.ok

    async def test_documents_are_replaced_under_the_first_key(self, keys, monkeypatch, tmp_path):
        """
        @verifies REQ-0032
        """
        keys(OLD)
        (tmp_path / "a.pdf").write_bytes(crypto.encrypt(b"%PDF-a"))
        (tmp_path / "b.pdf").write_bytes(b"%PDF-b")
        keys(f"{NEW},{OLD}")
        monkeypatch.setattr(settings, "data_dir", str(tmp_path))
        result = MagicMock()
        result.all.return_value = [
            (uuid.uuid4(), "a.pdf"),
            (uuid.uuid4(), "b.pdf"),
            (uuid.uuid4(), "gone.pdf"),
        ]
        db = SimpleNamespace(execute=AsyncMock(return_value=result))

        report = key_rotation.RotationReport()
        await key_rotation.rotate_documents(db, report)

        new = Fernet(NEW.encode())
        assert new.decrypt((tmp_path / "a.pdf").read_bytes()) == b"%PDF-a"
        assert new.decrypt((tmp_path / "b.pdf").read_bytes()) == b"%PDF-b"
        assert report.rewritten == {"documents": 2}
        assert report.missing_files == 1


class TestOtpKey:
    def test_the_hashes_are_keyed_by_their_own_secret(self, monkeypatch):
        """Changing ENCRYPTION_KEY leaves them alone; OTP_HMAC_KEY changes them.

        @verifies REQ-0033
        """
        monkeypatch.setattr(settings, "otp_hmac_key", "otp-secret-1")
        monkeypatch.setattr(settings, "encryption_key", OLD)
        first = otp.hash_phone("+390000000000")
        monkeypatch.setattr(settings, "encryption_key", NEW)
        assert otp.hash_phone("+390000000000") == first
        monkeypatch.setattr(settings, "otp_hmac_key", "otp-secret-2")
        assert otp.hash_phone("+390000000000") != first
