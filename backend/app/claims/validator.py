"""
Claim Validation - 声明验证服务

WP4 核心功能：
- 验证候选声明
- 坐标越界检查
- Predicate 白名单验证
- 类型约束检查
"""
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claims import (
    ClaimCandidate,
    CandidateEvidence,
    ClaimCandidateState,
    Entity,
)

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """验证错误"""
    pass


class ClaimValidationService:
    """声明验证服务"""

    def __init__(self, session: AsyncSession, ontology: Optional[dict[str, Any]] = None):
        self.session = session
        self.ontology = ontology or self._default_ontology()

    def _default_ontology(self) -> dict[str, Any]:
        """默认本体"""
        return {
            "predicates": {
                "WORKS_FOR": {
                    "subject_types": ["PERSON"],
                    "object_types": ["ORGANIZATION"],
                    "description": "Person works for organization",
                },
                "LOCATED_IN": {
                    "subject_types": ["PERSON", "ORGANIZATION"],
                    "object_types": ["LOCATION"],
                    "description": "Entity located in location",
                },
                "KNOWS": {
                    "subject_types": ["PERSON"],
                    "object_types": ["PERSON"],
                    "description": "Person knows person",
                },
                "AGE": {
                    "subject_types": ["PERSON"],
                    "object_kind": "value",
                    "datatype": "integer",
                    "description": "Person's age",
                },
            },
            "entity_types": ["PERSON", "ORGANIZATION", "LOCATION", "EVENT", "CONCEPT"],
        }

    async def validate_candidate(
        self,
        candidate: ClaimCandidate,
        evidence: list[CandidateEvidence],
        subject_entity: Entity,
        object_entity: Optional[Entity] = None,
    ) -> tuple[bool, list[str]]:
        """
        验证候选声明

        Args:
            candidate: 候选声明
            evidence: 证据列表
            subject_entity: 主体实体
            object_entity: 客体实体（如果 object_kind == entity）

        Returns:
            (is_valid, errors) - 是否有效和错误列表
        """
        errors = []

        # 1. 验证 predicate 在本体中
        if not self._validate_predicate(candidate.predicate):
            errors.append(f"Predicate '{candidate.predicate}' not in ontology")

        # 2. 验证主体类型约束
        if not self._validate_subject_type(candidate.predicate, subject_entity.entity_type):
            errors.append(
                f"Subject type '{subject_entity.entity_type}' not valid for predicate '{candidate.predicate}'"
            )

        # 3. 验证客体类型约束
        if candidate.object_kind == "entity" and object_entity:
            if not self._validate_object_type(candidate.predicate, object_entity.entity_type):
                errors.append(
                    f"Object type '{object_entity.entity_type}' not valid for predicate '{candidate.predicate}'"
                )

        # 4. 验证证据
        if not evidence:
            errors.append("No evidence provided")

        for ev in evidence:
            if not self._validate_evidence_bounds(ev):
                errors.append(f"Evidence bounds invalid: start={ev.start_char}, end={ev.end_char}")

        # 5. 验证时态约束
        if candidate.valid_from and candidate.valid_to:
            if candidate.valid_from > candidate.valid_to:
                errors.append("valid_from must be before valid_to")

        # 6. 验证置信度范围
        if not (0.0 <= candidate.confidence <= 1.0):
            errors.append(f"Confidence must be between 0 and 1, got {candidate.confidence}")

        is_valid = len(errors) == 0
        return is_valid, errors

    def _validate_predicate(self, predicate: str) -> bool:
        """验证 predicate 在本体中"""
        return predicate in self.ontology.get("predicates", {})

    def _validate_subject_type(self, predicate: str, entity_type: str) -> bool:
        """验证主体类型"""
        pred_def = self.ontology.get("predicates", {}).get(predicate, {})
        allowed_types = pred_def.get("subject_types", [])
        return entity_type in allowed_types if allowed_types else True

    def _validate_object_type(self, predicate: str, entity_type: str) -> bool:
        """验证客体类型"""
        pred_def = self.ontology.get("predicates", {}).get(predicate, {})
        allowed_types = pred_def.get("object_types", [])
        return entity_type in allowed_types if allowed_types else True

    def _validate_evidence_bounds(self, evidence: CandidateEvidence) -> bool:
        """验证证据边界"""
        if evidence.start_char < 0:
            return False
        if evidence.end_char <= evidence.start_char:
            return False
        return True

    async def transition_state(
        self,
        candidate: ClaimCandidate,
        new_state: str,
    ) -> bool:
        """
        状态转换

        Args:
            candidate: 候选声明
            new_state: 新状态

        Returns:
            是否成功
        """
        valid_transitions = {
            ClaimCandidateState.EXTRACTED: [
                ClaimCandidateState.VALIDATED,
                ClaimCandidateState.REVIEW_REQUIRED,
            ],
            ClaimCandidateState.VALIDATED: [
                ClaimCandidateState.APPROVED,
                ClaimCandidateState.REJECTED,
                ClaimCandidateState.REVIEW_REQUIRED,
            ],
            ClaimCandidateState.REVIEW_REQUIRED: [
                ClaimCandidateState.APPROVED,
                ClaimCandidateState.REJECTED,
                ClaimCandidateState.VALIDATED,
            ],
        }

        current_state = candidate.state
        if new_state not in valid_transitions.get(current_state, []):
            logger.warning(f"Invalid state transition: {current_state} -> {new_state}")
            return False

        candidate.state = new_state
        candidate.updated_at = datetime.utcnow()
        candidate.revision += 1

        await self.session.flush()
        return True

    async def validate_and_transition(
        self,
        candidate: ClaimCandidate,
        evidence: list[CandidateEvidence],
        subject_entity: Entity,
        object_entity: Optional[Entity] = None,
    ) -> tuple[bool, list[str]]:
        """
        验证并转换状态

        Args:
            candidate: 候选声明
            evidence: 证据列表
            subject_entity: 主体实体
            object_entity: 客体实体

        Returns:
            (is_valid, errors) - 是否有效和错误列表
        """
        is_valid, errors = await self.validate_candidate(
            candidate=candidate,
            evidence=evidence,
            subject_entity=subject_entity,
            object_entity=object_entity,
        )

        if is_valid:
            # 验证通过，转为 validated
            await self.transition_state(candidate, ClaimCandidateState.VALIDATED)
        else:
            # 验证失败，可能需要人工审核
            if self._requires_review(errors):
                await self.transition_state(candidate, ClaimCandidateState.REVIEW_REQUIRED)
            else:
                await self.transition_state(candidate, ClaimCandidateState.REJECTED)

        return is_valid, errors

    def _requires_review(self, errors: list[str]) -> bool:
        """判断是否需要人工审核"""
        # 某些错误需要人工审核，某些可以直接拒绝
        review_required_errors = [
            "Subject type",
            "Object type",
        ]
        for error in errors:
            for pattern in review_required_errors:
                if pattern in error:
                    return True
        return False