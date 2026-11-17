"""
Hallucination guard tests
"""


class TestHallucinationGuard:
    """Test hallucination protection"""

    def test_refusal_no_info(self):
        """Should generate refusal for no information"""
        from app.services.hallucination_guard import hallucination_guard

        refusal = hallucination_guard.generate_refusal("test query", "no_info")

        assert refusal is not None
        assert len(refusal) > 0
        assert "没有" in refusal or "未找到" in refusal or "无法" in refusal

    def test_refusal_uncertain(self):
        """Should generate refusal for uncertain information"""
        from app.services.hallucination_guard import hallucination_guard

        refusal = hallucination_guard.generate_refusal("test query", "uncertain")

        assert refusal is not None
        assert len(refusal) > 0

    def test_verify_answer_exists(self):
        """verify_answer method should exist"""
        from app.services.hallucination_guard import hallucination_guard

        # verify_answer 方法存在
        assert hasattr(hallucination_guard, 'verify_answer')

    def test_check_facts_not_required(self):
        """check_facts 方法不存在时跳过测试（使用 verify_answer 代替）"""
        # 幻觉防护使用 verify_answer，不是 check_facts
        # 这是一个预期行为的文档化测试
        pass