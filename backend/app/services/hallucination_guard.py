"""
幻觉防护模块
包含：强制引用、事实核查、拒绝回答机制
"""
import re
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class AnswerStatus(Enum):
    """回答状态"""
    RELIABLE = "reliable"           # 可靠：有来源支撑
    PARTIAL = "partial"             # 部分可靠：部分有来源
    UNCERTAIN = "uncertain"         # 不确定：来源不足
    OUT_OF_SCOPE = "out_of_scope"   # 超出范围：文档中无相关信息
    HALLUCINATED = "hallucinated"   # 幻觉：与文档矛盾


@dataclass
class VerifiedAnswer:
    """验证后的回答"""
    answer: str                     # 最终回答
    status: AnswerStatus            # 回答状态
    confidence: float               # 置信度 0-1
    citations: List[Dict[str, Any]]           # 引用列表
    warnings: List[str]             # 警告信息
    original_answer: str            # LLM 原始回答
    removed_claims: List[str]       # 被移除的无法验证的说法


class HallucinationGuard:
    """幻觉防护器"""

    # 拒绝回答的模板
    REFUSAL_TEMPLATES = {
        "no_info": "抱歉，根据现有知识库文档，我没有找到与「{query}」相关的信息。",
        "insufficient": "抱歉，关于「{query}」的信息不足，无法给出可靠回答。",
        "uncertain": "关于「{query}」，现有文档信息不够明确，建议查阅原始资料确认。",
    }

    # 需要移除的模糊词汇（容易产生幻觉）
    VAGUE_WORDS = [
        "可能", "也许", "大概", "或许", "应该", "估计",
        "我想", "我认为", "我觉得", "据我所知",
        "一般来说", "通常", "往往"
    ]

    # 确定性词汇（用于判断回答置信度）
    CERTAIN_WORDS = [
        "根据文档", "文档明确说明", "规定", "制度规定",
        "明确指出", "具体说明"
    ]

    def __init__(self, strict_mode: bool = True):
        """
        初始化

        Args:
            strict_mode: 严格模式 - 移除所有无法验证的说法
        """
        self.strict_mode = strict_mode

    def build_safe_prompt(self, query: str, context: str) -> str:
        """
        构建安全的提示词

        强调：必须基于文档回答，不能编造
        """
        prompt = f"""你是一个严格基于知识库回答问题的助手。

【核心规则】
1. 只使用下方「参考资料」中的信息回答
2. 每个观点必须标注来源，格式：[来源：文档标题]
3. 如果参考资料中没有相关信息，直接说"根据现有文档，我无法回答这个问题"
4. 禁止使用"可能"、"也许"、"大概"等模糊词汇
5. 禁止添加文档中未提及的任何信息
6. 禁止推测或推断

【参考资料】
{context}

【问题】
{query}

【回答要求】
- 先判断是否有相关信息，没有则直接拒绝
- 有信息则逐一列出，每条标注来源
- 不要添加任何个人观点或推测

请回答："""
        return prompt

    def verify_answer(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
        context: str
    ) -> VerifiedAnswer:
        """
        验证回答是否有幻觉

        Args:
            answer: LLM 生成的回答
            sources: 来源文档列表
            context: 原始上下文

        Returns:
            验证后的回答对象
        """
        citations = []
        warnings = []
        removed_claims = []

        # 1. 检查是否是拒绝回答
        if self._is_refusal(answer):
            return VerifiedAnswer(
                answer=answer,
                status=AnswerStatus.OUT_OF_SCOPE,
                confidence=1.0,
                citations=[],
                warnings=["文档中无相关信息，已拒绝回答"],
                original_answer=answer,
                removed_claims=[]
            )

        # 2. 提取回答中的关键声明
        claims = self._extract_claims(answer)

        # 3. 验证每个声明
        verified_claims = []
        for claim in claims:
            is_verified, source = self._verify_claim(claim, context, sources)
            if is_verified:
                verified_claims.append(claim)
                if source:
                    citations.append(source)
            else:
                removed_claims.append(claim)
                if self.strict_mode:
                    warnings.append(f"移除未验证的说法：{claim[:50]}...")

        # 4. 构建安全回答
        if self.strict_mode and removed_claims:
            safe_answer = self._build_safe_answer(answer, removed_claims)
        else:
            safe_answer = answer

        # 5. 计算置信度
        confidence = self._calculate_confidence(
            answer, context, len(verified_claims), len(claims)
        )

        # 6. 判断状态
        status = self._determine_status(
            confidence, len(removed_claims), len(sources)
        )

        # 7. 添加警告
        if self._has_vague_words(answer):
            warnings.append("回答包含模糊词汇，可能不够准确")

        if not self._has_citations(answer):
            warnings.append("回答未标注来源，可信度降低")

        return VerifiedAnswer(
            answer=safe_answer,
            status=status,
            confidence=confidence,
            citations=citations,
            warnings=warnings,
            original_answer=answer,
            removed_claims=removed_claims
        )

    def _is_refusal(self, answer: str) -> bool:
        """检查是否是拒绝回答"""
        refusal_patterns = [
            "无法回答", "没有找到", "没有相关", "无法提供",
            "不包含", "未提及", "不存在", "没有信息"
        ]
        return any(p in answer for p in refusal_patterns)

    def _extract_claims(self, answer: str) -> List[str]:
        """提取回答中的关键声明（句子级别）"""
        # 按句号分割
        sentences = re.split(r'[。！？\n]', answer)
        # 过滤空句和引用句
        claims = []
        for s in sentences:
            s = s.strip()
            if len(s) > 5 and not s.startswith('['):
                claims.append(s)
        return claims

    def _verify_claim(
        self,
        claim: str,
        context: str,
        sources: List[Dict[str, Any]]
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        验证单个声明是否有来源支撑

        简单实现：检查关键词是否在上下文中出现
        """
        # 提取声明中的关键词
        keywords = self._extract_keywords(claim)

        if not keywords:
            return False, None

        # 检查关键词是否在上下文中
        matched_count = 0
        for kw in keywords:
            if kw in context:
                matched_count += 1

        # 如果超过 50% 的关键词匹配，认为有支撑
        match_ratio = matched_count / len(keywords) if keywords else 0

        if match_ratio >= 0.5:
            # 找到最相关的来源
            for source in sources:
                source_text = source.get("content_snippet", "")
                if any(kw in source_text for kw in keywords):
                    return True, {
                        "claim": claim[:50] + "...",
                        "source_title": source.get("title", "未知"),
                        "source_id": source.get("doc_id", "")
                    }

        return match_ratio >= 0.3, None

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词（简单实现）"""
        # 移除标点
        text = re.sub(r'[，。！？、；：""''（）【】]', ' ', text)
        # 分词（简单按空格）
        words = text.split()
        # 过滤停用词
        stop_words = {"的", "是", "在", "有", "和", "了", "与", "等", "为", "对"}
        keywords = [w for w in words if len(w) >= 2 and w not in stop_words]
        return keywords[:10]  # 最多 10 个关键词

    def _build_safe_answer(
        self,
        original: str,
        removed_claims: List[str]
    ) -> str:
        """构建安全回答（移除未验证内容）"""
        safe_answer = original
        for claim in removed_claims:
            # 移除未验证的句子
            safe_answer = safe_answer.replace(claim, "")

        # 清理多余空行和标点
        safe_answer = re.sub(r'\n{3,}', '\n\n', safe_answer)
        safe_answer = re.sub(r'[。]{2,}', '。', safe_answer)

        if not safe_answer.strip():
            return "根据现有文档，无法提供可靠回答。"

        return safe_answer.strip()

    def _calculate_confidence(
        self,
        answer: str,
        context: str,
        verified_count: int,
        total_count: int
    ) -> float:
        """计算置信度"""
        # 基础置信度基于验证比例
        if total_count == 0:
            return 0.0

        base_confidence = verified_count / total_count

        # 根据确定性词汇调整
        certain_count = sum(1 for w in self.CERTAIN_WORDS if w in answer)
        confidence_boost = min(certain_count * 0.1, 0.2)

        # 根据模糊词汇调整
        vague_count = sum(1 for w in self.VAGUE_WORDS if w in answer)
        confidence_penalty = min(vague_count * 0.1, 0.3)

        final_confidence = base_confidence + confidence_boost - confidence_penalty
        return max(0.0, min(1.0, final_confidence))

    def _determine_status(
        self,
        confidence: float,
        removed_count: int,
        source_count: int
    ) -> AnswerStatus:
        """判断回答状态"""
        if source_count == 0:
            return AnswerStatus.OUT_OF_SCOPE

        if confidence >= 0.8:
            return AnswerStatus.RELIABLE
        elif confidence >= 0.5:
            return AnswerStatus.PARTIAL
        elif confidence >= 0.3:
            return AnswerStatus.UNCERTAIN
        else:
            return AnswerStatus.HALLUCINATED

    def _has_vague_words(self, answer: str) -> bool:
        """检查是否有模糊词汇"""
        return any(w in answer for w in self.VAGUE_WORDS)

    def _has_citations(self, answer: str) -> bool:
        """检查是否有引用标注"""
        citation_pattern = r'\[来源[：:][^\]]+\]'
        return bool(re.search(citation_pattern, answer))

    def generate_refusal(self, query: str, reason: str = "no_info") -> str:
        """生成拒绝回答"""
        template = self.REFUSAL_TEMPLATES.get(reason, self.REFUSAL_TEMPLATES["no_info"])
        return template.format(query=query)


# 全局实例
hallucination_guard = HallucinationGuard(strict_mode=True)