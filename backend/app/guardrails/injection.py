"""
Prompt Injection Protection - 提示词注入防护

WP5 P0 #3: 保护 LLM 提示词免受注入攻击
"""
import re
from dataclasses import dataclass
from enum import Enum


class InjectionRisk(str, Enum):
    """注入风险级别"""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class InjectionCheckResult:
    """注入检查结果"""
    risk_level: InjectionRisk
    detected_patterns: list[str]
    sanitized_content: str
    was_modified: bool


# 危险模式列表
DANGEROUS_PATTERNS = [
    # 系统指令覆盖
    r"ignore\s+.*instructions?",
    r"ignore\s+(previous|all|above)",
    r"disregard\s+(all|any|previous)",
    r"forget\s+(all|everything|previous)",

    # 角色切换
    r"you\s+are\s+now",
    r"act\s+as\s+(if|a|an)",
    r"pretend\s+(to\s+be|you\s+are)",
    r"simulate\s+(being|a|an)",

    # 指令注入
    r"new\s+instructions?",
    r"override\s+(previous|default|all)",
    r"system\s*:",

    # 输出操纵
    r"output\s+only",
    r"print\s+exactly",
    r"respond\s+with",

    # 越狱尝试
    r"developer\s+mode",
    r"debug\s+mode",
    r"admin\s+mode",

    # 分隔符绕过
    r"===+",
    r"---+",
    r"\*\*\*+",
]

# 高风险模式（直接拒绝）
CRITICAL_PATTERNS = [
    r"<\|.*?\|>",  # 特殊标记
    r"\[SYSTEM\]",
    r"\[INST\]",
    r"<<.*?>>",  # 嵌套指令
]


class PromptInjectionGuard:
    """提示词注入防护"""

    def __init__(self, strict_mode: bool = True):
        self.strict_mode = strict_mode
        self._compile_patterns()

    def _compile_patterns(self):
        """预编译正则表达式"""
        self.dangerous_regex = [
            re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS
        ]
        self.critical_regex = [
            re.compile(p, re.IGNORECASE) for p in CRITICAL_PATTERNS
        ]

    def check(self, content: str) -> InjectionCheckResult:
        """
        检查内容是否存在注入风险

        Args:
            content: 要检查的内容

        Returns:
            InjectionCheckResult: 检查结果
        """
        detected_patterns = []

        # 检查严重模式
        for regex in self.critical_regex:
            if regex.search(content):
                detected_patterns.append(f"CRITICAL: {regex.pattern}")
                return InjectionCheckResult(
                    risk_level=InjectionRisk.CRITICAL,
                    detected_patterns=detected_patterns,
                    sanitized_content="[CONTENT BLOCKED - CRITICAL INJECTION DETECTED]",
                    was_modified=True,
                )

        # 检查危险模式
        for regex in self.dangerous_regex:
            matches = regex.findall(content)
            if matches:
                detected_patterns.append(regex.pattern)

        # 确定风险级别
        if not detected_patterns:
            return InjectionCheckResult(
                risk_level=InjectionRisk.NONE,
                detected_patterns=[],
                sanitized_content=content,
                was_modified=False,
            )

        # 根据检测到的模式数量确定风险级别
        if len(detected_patterns) >= 3:
            risk_level = InjectionRisk.HIGH
        elif len(detected_patterns) >= 2:
            risk_level = InjectionRisk.MEDIUM
        else:
            risk_level = InjectionRisk.LOW

        # 如果是严格模式，对中高风险进行清理
        sanitized = content
        was_modified = False

        if self.strict_mode and risk_level in [InjectionRisk.HIGH, InjectionRisk.MEDIUM]:
            sanitized = self._sanitize(content)
            was_modified = True

        return InjectionCheckResult(
            risk_level=risk_level,
            detected_patterns=detected_patterns,
            sanitized_content=sanitized,
            was_modified=was_modified,
        )

    def _sanitize(self, content: str) -> str:
        """
        清理内容中的潜在注入模式

        Args:
            content: 原始内容

        Returns:
            清理后的内容
        """
        sanitized = content

        # 移除危险模式
        for regex in self.dangerous_regex:
            sanitized = regex.sub("[REDACTED]", sanitized)

        # 移除控制字符
        sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', sanitized)

        return sanitized

    def is_safe(self, content: str) -> bool:
        """
        快速判断内容是否安全

        Args:
            content: 要检查的内容

        Returns:
            True 如果内容安全
        """
        result = self.check(content)
        return result.risk_level in [InjectionRisk.NONE, InjectionRisk.LOW]


# 全局防护实例
injection_guard = PromptInjectionGuard()


def check_injection(content: str) -> InjectionCheckResult:
    """便捷函数：检查注入风险"""
    return injection_guard.check(content)


def is_content_safe(content: str) -> bool:
    """便捷函数：判断内容是否安全"""
    return injection_guard.is_safe(content)