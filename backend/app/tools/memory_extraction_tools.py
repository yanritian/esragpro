"""
Agent Memory System - Memory Extraction Tools
记忆提取相关的 MCP 工具
"""
from uuid import UUID
from pydantic import BaseModel, Field
import logging

from app.services.memory_extraction import get_memory_extractor
from app.services.llm import get_llm_service
from app.db.session import get_async_session

logger = logging.getLogger(__name__)


class SaveToLongTermInput(BaseModel):
    """保存到长期记忆输入"""
    session_id: str = Field(..., description="会话 ID")


class SaveToLongTermOutput(BaseModel):
    """保存到长期记忆输出"""
    session_id: str
    extracted_facts: int
    message: str


async def save_to_long_term_tool(
    input: SaveToLongTermInput,
    principal,
) -> SaveToLongTermOutput:
    """
    手动将当前会话保存到长期记忆

    Args:
        input: 保存输入参数
        principal: 当前用户主体上下文

    Returns:
        提取的事实数量
    """
    session_id = UUID(input.session_id)

    async for db in get_async_session():
        # 获取 LLM 服务
        llm_service = get_llm_service()

        # 获取记忆提取器
        extractor = get_memory_extractor(llm_service)

        # 提取并保存
        count = await extractor.extract_from_conversation(session_id, db)

        return SaveToLongTermOutput(
            session_id=input.session_id,
            extracted_facts=count,
            message=f"Successfully extracted {count} facts to long-term memory",
        )

    raise RuntimeError("No database session available")


class EnableAutoExtractionInput(BaseModel):
    """启用自动提取输入"""
    session_id: str = Field(..., description="会话 ID")
    enabled: bool = Field(default=True, description="是否启用")


class EnableAutoExtractionOutput(BaseModel):
    """启用自动提取输出"""
    session_id: str
    auto_extraction_enabled: bool


async def enable_auto_extraction_tool(
    input: EnableAutoExtractionInput,
    principal,
) -> EnableAutoExtractionOutput:
    """
    启用/禁用会话的自动记忆提取

    Args:
        input: 启用输入参数
        principal: 当前用户主体上下文

    Returns:
        配置状态
    """
    session_id = UUID(input.session_id)

    # TODO: 在 Session 模型中添加 auto_extraction_enabled 字段
    # 这里暂时返回成功
    return EnableAutoExtractionOutput(
        session_id=str(session_id),
        auto_extraction_enabled=input.enabled,
    )


# === MCP Tool Definitions ===

MEMORY_EXTRACTION_TOOLS = [
    {
        "name": "memory.save_to_long_term",
        "description": "手动将当前会话提取并保存到长期记忆",
        "input_schema": SaveToLongTermInput.model_json_schema(),
        "handler": save_to_long_term_tool,
    },
    {
        "name": "memory.enable_auto_extraction",
        "description": "启用/禁用会话的自动记忆提取",
        "input_schema": EnableAutoExtractionInput.model_json_schema(),
        "handler": enable_auto_extraction_tool,
    },
]
