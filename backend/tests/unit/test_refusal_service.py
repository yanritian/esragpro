"""
Refusal Service Tests - 拒答服务测试

验证：
- 无信息时拒答
- 证据冲突时拒答
- 缺失槽位时请求澄清
- 置信度过低时拒答
"""

from app.services.refusal_service import (
    RefusalService,
    RefusalReason,
    RefusalResult,
)


class TestRefusalService:
    """拒答服务测试"""

    def test_refuse_no_evidence(self):
        """无证据时拒答"""
        service = RefusalService()

        result = service.evaluate(
            query="测试问题",
            evidence_strength=0.0,
            has_conflict=False,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.NO_EVIDENCE

    def test_refuse_low_confidence(self):
        """置信度过低时拒答"""
        service = RefusalService()

        result = service.evaluate(
            query="测试问题",
            evidence_strength=0.2,
            has_conflict=False,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.LOW_CONFIDENCE

    def test_refuse_conflict_evidence(self):
        """证据冲突时拒答"""
        service = RefusalService()

        result = service.evaluate(
            query="测试问题",
            evidence_strength=0.8,
            has_conflict=True,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.CONFLICT_EVIDENCE

    def test_no_refuse_reliable(self):
        """证据可靠时不拒答"""
        service = RefusalService()

        result = service.evaluate(
            query="测试问题",
            evidence_strength=0.9,
            has_conflict=False,
            missing_slots=[],
        )

        assert result.should_refuse is False

    def test_request_clarification_missing_slots(self):
        """缺失槽位时请求澄清"""
        service = RefusalService()

        result = service.evaluate(
            query="测试问题",
            evidence_strength=0.7,
            has_conflict=False,
            missing_slots=["时间", "地点"],
        )

        assert result.needs_clarification is True
        assert len(result.missing_slots) == 2

    def test_generate_refusal_message_no_evidence(self):
        """生成拒答消息 - 无证据"""
        service = RefusalService()

        message = service.generate_refusal_message(
            query="张三在哪里工作？",
            reason=RefusalReason.NO_EVIDENCE,
        )

        assert "没有找到" in message or "无法回答" in message

    def test_generate_refusal_message_conflict(self):
        """生成拒答消息 - 冲突"""
        service = RefusalService()

        message = service.generate_refusal_message(
            query="张三在哪里工作？",
            reason=RefusalReason.CONFLICT_EVIDENCE,
        )

        assert "冲突" in message or "矛盾" in message or "不同说法" in message

    def test_generate_clarification_request(self):
        """生成澄清请求"""
        service = RefusalService()

        message = service.generate_clarification_request(
            query="发生什么事了？",
            missing_slots=["时间", "地点"],
        )

        assert "时间" in message
        assert "地点" in message


class TestRefusalResult:
    """拒答结果测试"""

    def test_create_refusal_result(self):
        """创建拒答结果"""
        result = RefusalResult(
            should_refuse=True,
            reason=RefusalReason.NO_EVIDENCE,
            message="无法回答",
            needs_clarification=False,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.NO_EVIDENCE

    def test_refusal_result_to_dict(self):
        """拒答结果转字典"""
        result = RefusalResult(
            should_refuse=True,
            reason=RefusalReason.LOW_CONFIDENCE,
            message="置信度过低",
            needs_clarification=False,
            missing_slots=[],
        )

        d = result.to_dict()

        assert d["should_refuse"] is True
        assert d["reason"] == "low_confidence"


class TestRefusalReason:
    """拒答原因测试"""

    def test_all_refusal_reasons(self):
        """所有拒答原因"""
        reasons = [
            RefusalReason.NO_EVIDENCE,
            RefusalReason.LOW_CONFIDENCE,
            RefusalReason.CONFLICT_EVIDENCE,
            RefusalReason.OUT_OF_SCOPE,
            RefusalReason.MISSING_SLOTS,
        ]

        for reason in reasons:
            assert isinstance(reason.value, str)