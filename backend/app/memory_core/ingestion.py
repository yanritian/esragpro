"""
Observation Ingestion Service (M2)

Handles immutable observation events with consent and DLP validation.
"""

import logging
from typing import Optional, Dict, Any
from uuid import UUID, uuid4

from app.memory_core.contracts import MemoryObservation, MemoryAccessContext

logger = logging.getLogger(__name__)


class ConsentError(Exception):
    """Raised when consent is not given"""
    pass


class DLPViolationError(Exception):
    """Raised when DLP scan detects violation"""
    def __init__(self, violation_type: str, message: str):
        self.violation_type = violation_type
        self.message = message
        super().__init__(f"[{violation_type}] {message}")


class ObservationIngestionService:
    """
    Service for ingesting memory observations.

    Pipeline:
    1. Validate consent
    2. Scan for DLP/Secret violations
    3. Create immutable observation
    4. Write to outbox
    """

    def __init__(self, dlp_scanner, outbox_writer):
        self.dlp_scanner = dlp_scanner
        self.outbox_writer = outbox_writer

    async def ingest(
        self,
        tenant_id: UUID,
        principal_id: UUID,
        session_id: UUID,
        request_id: str,
        content: str,
        source: str,
        access_context: MemoryAccessContext,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryObservation:
        """
        Ingest a new observation.

        Args:
            tenant_id: Tenant ID
            principal_id: Principal ID
            session_id: Session ID
            request_id: Request ID
            content: Observation content
            source: Source type (conversation, tool_call, etc.)
            access_context: Memory access context (for consent check)
            metadata: Optional metadata

        Returns:
            Created MemoryObservation

        Raises:
            ConsentError: If consent not given
            DLPViolationError: If DLP scan fails
        """
        # Step 1: Validate consent
        if not access_context.consent_given:
            raise ConsentError("Consent not given for memory ingestion")

        # Step 2: Check legal hold
        if access_context.legal_hold:
            logger.warning("Ingestion attempted while on legal hold")
            # Still allow ingestion for audit trail, but mark appropriately

        # Step 3: DLP/Secret scanning BEFORE persistence
        try:
            self.dlp_scanner.scan(content)
        except DLPViolationError as e:
            logger.error(
                f"DLP violation detected: {e.violation_type} - {e.message}"
            )
            raise

        # Step 4: Create immutable observation
        observation = MemoryObservation(
            id=uuid4(),
            tenant_id=tenant_id,
            principal_id=principal_id,
            session_id=session_id,
            request_id=request_id,
            content=content,
            source=source,
            metadata=metadata or {},
        )

        # Step 5: Write to outbox
        await self.outbox_writer.write(observation)

        logger.info(
            f"Ingested observation {observation.id} for principal {principal_id}"
        )

        return observation
