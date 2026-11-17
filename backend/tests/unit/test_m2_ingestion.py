"""
M2 Tests: Observation Ingestion, Consent, DLP, Idempotent Outbox
"""

import pytest
from uuid import uuid4
from datetime import timedelta

from app.memory_core.contracts import MemoryAccessContext, MemoryObservation
from app.memory_core.ingestion import (
    ObservationIngestionService,
    ConsentError,
    DLPViolationError,
)
from app.memory_core.dlp import DLPScanner, DLPViolationType
from app.memory_core.outbox import (
    OutboxWriter,
    OutboxEvent,
    OutboxStatus,
    InMemoryOutboxStorage,
)


class TestDLPScanner:
    """DLP scanner tests"""

    def test_scan_clean_content(self):
        """Test scanning clean content"""
        scanner = DLPScanner()
        content = "User prefers Python programming language"
        # Should not raise
        scanner.scan(content)

    def test_scan_detects_password(self):
        """Test detecting password pattern"""
        scanner = DLPScanner()
        content = "password = my_secret_password_123"

        with pytest.raises(DLPViolationError) as exc_info:
            scanner.scan(content)

        assert exc_info.value.violation_type == DLPViolationType.PASSWORD

    def test_scan_detects_private_key(self):
        """Test detecting private key"""
        scanner = DLPScanner()
        content = """
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA...
-----END RSA PRIVATE KEY-----
"""
        with pytest.raises(DLPViolationError) as exc_info:
            scanner.scan(content)

        assert exc_info.value.violation_type == DLPViolationType.PRIVATE_KEY

    def test_scan_detects_api_key(self):
        """Test detecting API key"""
        scanner = DLPScanner()
        content = "api_key = sk_test_1234567890abcdef1234567890abcdef"

        with pytest.raises(DLPViolationError) as exc_info:
            scanner.scan(content)

        assert exc_info.value.violation_type == DLPViolationType.API_TOKEN

    def test_scan_detects_jwt(self):
        """Test detecting JWT token"""
        scanner = DLPScanner()
        content = "token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"

        with pytest.raises(DLPViolationError) as exc_info:
            scanner.scan(content)

        assert exc_info.value.violation_type == DLPViolationType.JWT

    def test_scan_detects_aws_key(self):
        """Test detecting AWS access key"""
        scanner = DLPScanner()
        content = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"

        with pytest.raises(DLPViolationError) as exc_info:
            scanner.scan(content)

        assert exc_info.value.violation_type == DLPViolationType.AWS_KEY

    def test_scan_detects_connection_string(self):
        """Test detecting connection string"""
        scanner = DLPScanner()
        content = "mongodb://user:password123@host.example.com:27017/dbname"

        with pytest.raises(DLPViolationError) as exc_info:
            scanner.scan(content)

        assert exc_info.value.violation_type == DLPViolationType.CONNECTION_STRING

    def test_scan_and_report(self):
        """Test non-raising scan"""
        scanner = DLPScanner()
        content = "password = secret123"

        violations = scanner.scan_and_report(content)
        assert len(violations) > 0
        assert DLPViolationType.PASSWORD in violations


class TestObservationIngestionService:
    """Observation ingestion service tests"""

    @pytest.fixture
    def dlp_scanner(self):
        return DLPScanner()

    @pytest.fixture
    def outbox_writer(self):
        storage = InMemoryOutboxStorage()
        return OutboxWriter(storage)

    @pytest.fixture
    def ingestion_service(self, dlp_scanner, outbox_writer):
        return ObservationIngestionService(dlp_scanner, outbox_writer)

    @pytest.fixture
    def access_context_with_consent(self):
        return MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            consent_given=True,
        )

    @pytest.fixture
    def access_context_without_consent(self):
        return MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            consent_given=False,
        )

    @pytest.mark.asyncio
    async def test_ingest_with_consent(
        self,
        ingestion_service,
        access_context_with_consent,
    ):
        """Test ingesting observation with consent"""
        observation = await ingestion_service.ingest(
            tenant_id=uuid4(),
            principal_id=uuid4(),
            session_id=uuid4(),
            request_id="req-123",
            content="User prefers Python",
            source="conversation",
            access_context=access_context_with_consent,
        )

        assert observation.id is not None
        assert observation.content == "User prefers Python"
        assert observation.source == "conversation"

    @pytest.mark.asyncio
    async def test_ingest_without_consent_raises(
        self,
        ingestion_service,
        access_context_without_consent,
    ):
        """Test ingesting without consent raises error"""
        with pytest.raises(ConsentError) as exc_info:
            await ingestion_service.ingest(
                tenant_id=uuid4(),
                principal_id=uuid4(),
                session_id=uuid4(),
                request_id="req-123",
                content="User prefers Python",
                source="conversation",
                access_context=access_context_without_consent,
            )

        assert "Consent not given" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ingest_with_dlp_violation_raises(
        self,
        ingestion_service,
        access_context_with_consent,
    ):
        """Test ingesting content with DLP violation raises error"""
        with pytest.raises(DLPViolationError):
            await ingestion_service.ingest(
                tenant_id=uuid4(),
                principal_id=uuid4(),
                session_id=uuid4(),
                request_id="req-123",
                content="password = my_secret_password",
                source="conversation",
                access_context=access_context_with_consent,
            )


class TestOutboxWriter:
    """Outbox writer tests"""

    @pytest.fixture
    def storage(self):
        return InMemoryOutboxStorage()

    @pytest.fixture
    def writer(self, storage):
        return OutboxWriter(storage)

    @pytest.fixture
    def sample_observation(self):
        return MemoryObservation(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            session_id=uuid4(),
            request_id="req-123",
            content="Test observation",
            source="conversation",
        )

    @pytest.mark.asyncio
    async def test_write_observation(self, writer, sample_observation):
        """Test writing observation to outbox"""
        event = await writer.write(sample_observation)

        assert event.id is not None
        assert event.event_type == "observation.created"
        assert event.status == OutboxStatus.PENDING
        assert event.attempt_count == 0

    @pytest.mark.asyncio
    async def test_idempotency_key_prevents_duplicate(
        self,
        writer,
        sample_observation,
    ):
        """Test idempotency key prevents duplicate writes"""
        event1 = await writer.write(sample_observation)
        event2 = await writer.write(sample_observation)

        assert event1.id == event2.id

    @pytest.mark.asyncio
    async def test_mark_processing(self, writer, sample_observation):
        """Test marking event as processing"""
        event = await writer.write(sample_observation)
        updated = await writer.mark_processing(event.id)

        assert updated.status == OutboxStatus.PROCESSING
        assert updated.attempt_count == 1

    @pytest.mark.asyncio
    async def test_mark_completed(self, writer, sample_observation):
        """Test marking event as completed"""
        event = await writer.write(sample_observation)
        await writer.mark_processing(event.id)
        updated = await writer.mark_completed(event.id)

        assert updated.status == OutboxStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_mark_failed_with_retry(self, writer, sample_observation):
        """Test marking event as failed with retry"""
        event = await writer.write(sample_observation)
        updated = await writer.mark_failed(
            event.id,
            error="Temporary failure",
            retry_delay=timedelta(seconds=30),
        )

        assert updated.status == OutboxStatus.PENDING
        assert updated.attempt_count == 0  # Still retryable
        assert updated.last_error == "Temporary failure"
        assert updated.next_retry_at is not None

    @pytest.mark.asyncio
    async def test_mark_failed_to_dlq(self, writer, sample_observation):
        """Test marking event as failed to DLQ"""
        event = await writer.write(sample_observation)
        event.max_attempts = 1  # Set low to trigger DLQ

        # First attempt
        await writer.mark_processing(event.id)
        event = await writer.storage.get(event.id)
        event.mark_failed("Permanent failure", retry_delay=None)
        await writer.storage.update(event)

        # Second attempt (should go to DLQ)
        await writer.mark_processing(event.id)
        updated = await writer.mark_failed(event.id, "Still failing")

        assert updated.status == OutboxStatus.DLQ

    @pytest.mark.asyncio
    async def test_update_checkpoint(self, writer, sample_observation):
        """Test updating checkpoint"""
        event = await writer.write(sample_observation)
        checkpoint = {"step": 1, "data": "partial_result"}

        updated = await writer.update_checkpoint(event.id, checkpoint)

        assert updated.checkpoint == checkpoint

    @pytest.mark.asyncio
    async def test_get_pending(self, writer, sample_observation):
        """Test getting pending events"""
        await writer.write(sample_observation)
        pending = await writer.get_pending()

        assert len(pending) > 0
        assert all(e.status == OutboxStatus.PENDING for e in pending)

    @pytest.mark.asyncio
    async def test_get_dlq(self, writer, sample_observation):
        """Test getting DLQ events"""
        event = await writer.write(sample_observation)
        event.max_attempts = 1

        # Trigger DLQ
        await writer.mark_processing(event.id)
        event = await writer.storage.get(event.id)
        event.mark_failed("Permanent failure", retry_delay=None)
        await writer.storage.update(event)

        dlq = await writer.get_dlq()
        assert len(dlq) > 0
        assert all(e.status == OutboxStatus.DLQ for e in dlq)


class TestOutboxEvent:
    """Outbox event tests"""

    def test_is_retryable(self):
        """Test retryable check"""
        event = OutboxEvent(
            id=uuid4(),
            event_type="test",
            payload={},
            idempotency_key="test-key",
            attempt_count=1,
            max_attempts=3,
        )

        assert event.is_retryable() is True

    def test_is_not_retryable(self):
        """Test non-retryable check"""
        event = OutboxEvent(
            id=uuid4(),
            event_type="test",
            payload={},
            idempotency_key="test-key",
            attempt_count=3,
            max_attempts=3,
        )

        assert event.is_retryable() is False

    def test_to_dict(self):
        """Test to_dict conversion"""
        event = OutboxEvent(
            id=uuid4(),
            event_type="test",
            payload={"key": "value"},
            idempotency_key="test-key",
        )

        d = event.to_dict()
        assert "id" in d
        assert "event_type" in d
        assert "payload" in d
        assert "status" in d
