"""
DLP/Secret Scanner (M2)

Scans content for sensitive information BEFORE persistence.
Default deny policy for tokens, passwords, private keys, cookies,
connection strings, and high-sensitivity PII.
"""

import re
import logging
from typing import List, Tuple, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class DLPViolationType(str, Enum):
    """Types of DLP violations"""
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"
    API_TOKEN = "api_token"
    CREDIT_CARD = "credit_card"
    SSN = "ssn"
    CONNECTION_STRING = "connection_string"
    COOKIE = "cookie"
    JWT = "jwt"
    AWS_KEY = "aws_key"
    GCP_KEY = "gcp_key"


class DLPScanner:
    """
    DLP/Secret scanner with default deny policy.

    Scans content for patterns that should NOT be persisted.
    Raises DLPViolationError if violation detected.
    """

    def __init__(self, custom_patterns: Optional[List[Tuple[str, str]]] = None):
        """
        Initialize scanner with pattern library.

        Args:
            custom_patterns: List of (pattern_name, regex_pattern) tuples
        """
        self.patterns = self._build_pattern_library()
        if custom_patterns:
            self.patterns.extend(custom_patterns)

    def _build_pattern_library(self) -> List[Tuple[str, str]]:
        """Build pattern library for common secrets"""
        return [
            # Password patterns
            (DLPViolationType.PASSWORD, r'(?i)password\s*[=:]\s*\S+'),
            (DLPViolationType.PASSWORD, r'(?i)passwd\s*[=:]\s*\S+'),
            (DLPViolationType.PASSWORD, r'(?i)pwd\s*[=:]\s*\S+'),

            # Private keys
            (DLPViolationType.PRIVATE_KEY, r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'),
            (DLPViolationType.PRIVATE_KEY, r'-----BEGIN\s+EC\s+PRIVATE\s+KEY-----'),

            # API tokens
            (DLPViolationType.API_TOKEN, r'(?i)api[_-]?key\s*[=:]\s*\S{20,}'),
            (DLPViolationType.API_TOKEN, r'(?i)api[_-]?token\s*[=:]\s*\S{20,}'),
            (DLPViolationType.API_TOKEN, r'(?i)access[_-]?token\s*[=:]\s*\S{20,}'),

            # Credit cards (simplified)
            (DLPViolationType.CREDIT_CARD, r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b'),

            # SSN (US Social Security Number)
            (DLPViolationType.SSN, r'\b\d{3}-\d{2}-\d{4}\b'),

            # Connection strings
            (DLPViolationType.CONNECTION_STRING, r'(?i)(mongodb|postgres|mysql|redis):\/\/[^@\s]+@[^@\s]+'),
            (DLPViolationType.CONNECTION_STRING, r'(?i)Server=[^;]+;.*Password=[^;]+'),

            # Cookies
            (DLPViolationType.COOKIE, r'(?i)cookie\s*[=:]\s*[a-zA-Z0-9+/=]{20,}'),

            # JWT tokens
            (DLPViolationType.JWT, r'eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+'),

            # AWS keys
            (DLPViolationType.AWS_KEY, r'AKIA[0-9A-Z]{16}'),
            (DLPViolationType.AWS_KEY, r'(?i)aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*\S+'),

            # GCP keys
            (DLPViolationType.GCP_KEY, r'(?i)gcp[_-]?service[_-]?account[_-]?key\s*[=:]\s*\S+'),
        ]

    def scan(self, content: str) -> None:
        """
        Scan content for DLP violations.

        Args:
            content: Content to scan

        Raises:
            DLPViolationError: If violation detected
        """
        for violation_type, pattern in self.patterns:
            if re.search(pattern, content):
                from app.memory_core.ingestion import DLPViolationError
                raise DLPViolationError(
                    violation_type=violation_type,
                    message=f"Detected {violation_type} pattern in content"
                )

        logger.debug("DLP scan passed - no violations detected")

    def scan_and_report(self, content: str) -> List[str]:
        """
        Scan content and return list of violations (non-raising).

        Args:
            content: Content to scan

        Returns:
            List of violation types found
        """
        violations = []
        for violation_type, pattern in self.patterns:
            if re.search(pattern, content):
                violations.append(violation_type)

        return violations
