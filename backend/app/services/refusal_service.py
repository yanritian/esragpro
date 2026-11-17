"""
Refusal Service - 拒答服务

WP5 核心功能：
- 无信息时拒答
- 证据冲突时拒答
- 缺失槽位时请求澄清
- 置信度过低时拒答
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RefusalReason(str, Enum):
    """拒答原因"""
    NO_EVIDENCE = "no_evidence"
    LOW_CONFIDENCE = "low_confidence"
    CONFLICT_EVIDENCE = "conflict_evidence"
    OUT_OF_SCOPE = "out_of_scope"
    MISSING_SLOTS = "missing_slots"


@dataclass
class RefusalResult:
    """拒答结果"""
    should_refuse: bool
    reason: Optional[RefusalReason]
    message: str
    needs_clarification: bool
    missing_slots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "should_refuse": self.should_refuse,
            "reason": self.reason.value if self.reason else None,
            "message": self.message,
            "needs_clarification": self.needs_clarification,
            "missing_slots": self.missing_slots,
        }


class RefusalService:
    """拒答服务"""

    # 置信度阈值
    LOW_CONFIDENCE_THRESHOLD = 0.3
    NO_EVIDENCE_THRESHOLD = 0.05

    def evaluate(
        self,
        query: str,
        evidence_strength: float,
        has_conflict: bool,
        missing_slots: list[str],
    ) -> RefusalResult:
        """
        评估是否应该拒答

        Args:
            query: 用户查询
            evidence_strength: 证据强度 (0-1)
            has_conflict: 是否有证据冲突
            missing_slots: 缺失的槽位列表

        Returns:
            RefusalResult: 拒答评估结果
        """
        # 无证据时拒答
        if evidence_strength <= self.NO_EVIDENCE_THRESHOLD:
            return RefusalResult(
                should_refuse=True,
                reason=RefusalReason.NO_EVIDENCE,
                message=self.generate_refusal_message(query, RefusalReason.NO_EVIDENCE),
                needs_clarification=False,
                missing_slots=[],
            )

        # 证据冲突时拒答
        if has_conflict:
            return RefusalResult(
                should_refuse=True,
                reason=RefusalReason.CONFLICT_EVIDENCE,
                message=self.generate_refusal_message(query, RefusalReason.CONFLICT_EVIDENCE),
                needs_clarification=False,
                missing_slots=[],
            )

        # 置信度过低时拒答
        if evidence_strength < self.LOW_CONFIDENCE_THRESHOLD:
            return RefusalResult(
                should_refuse=True,
                reason=RefusalReason.LOW_CONFIDENCE,
                message=self.generate_refusal_message(query, RefusalReason.LOW_CONFIDENCE),
                needs_clarification=False,
                missing_slots=[],
            )

        # 缺失槽位时请求澄清
        if missing_slots:
            return RefusalResult(
                should_refuse=False,
                reason=RefusalReason.MISSING_SLOTS,
                message=self.generate_clarification_request(query, missing_slots),
                needs_clarification=True,
                missing_slots=missing_slots,
            )

        # 证据可靠，不拒答
        return RefusalResult(
            should_refuse=False,
            reason=None,
            message="",
            needs_clarification=False,
            missing_slots=[],
        )

    def generate_refusal_message(
        self,
        query: str,
        reason: RefusalReason,
    ) -> str:
        """
        生成拒答消息

        Args:
            query: 用户查询
            reason: 拒答原因

        Returns:
            str: 拒答消息
        """
        templates = {
            RefusalReason.NO_EVIDENCE: (
                f'抱歉，我没有找到关于"{query}"的相关信息。'
                f'请提供更多背景信息或在相关文档中查找。'
            ),
            RefusalReason.LOW_CONFIDENCE: (
                f'抱歉，我对"{query}"的回答置信度过低。'
                f'现有信息不足以给出可靠答案，建议查阅原始文档。'
            ),
            RefusalReason.CONFLICT_EVIDENCE: (
                f'抱歉，关于"{query}"的信息存在冲突或矛盾。'
                f'不同文档给出了不同的说法，建议核实原始来源。'
            ),
            RefusalReason.OUT_OF_SCOPE: (
                f'抱歉，"{query}"超出了我的知识范围。'
                f'请尝试其他相关问题。'
            ),
            RefusalReason.MISSING_SLOTS: (
                f'抱歉，回答"{query}"需要更多信息。'
            ),
        }

        return templates.get(reason, f'抱歉，无法回答"{query}"。')

    def generate_clarification_request(
        self,
        query: str,
        missing_slots: list[str],
    ) -> str:
        """
        生成澄清请求

        Args:
            query: 用户查询
            missing_slots: 缺失的槽位列表

        Returns:
            str: 澄清请求消息
        """
        slots_text = "、".join(missing_slots)

        return (
            f'为了更好地回答"{query}"，'
            f'请提供以下信息：{slots_text}。'
        )