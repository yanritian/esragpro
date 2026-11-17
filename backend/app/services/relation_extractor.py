"""
关系抽取服务
用于从文本中抽取实体之间的关系
"""
import re
import json
import logging
from typing import List, Dict, Any
from dataclasses import dataclass
from enum import Enum


logger = logging.getLogger(__name__)


class RelationType(str, Enum):
    """关系类型枚举"""
    # 人物-组织关系
    WORKS_FOR = "WORKS_FOR"          # 任职于
    FOUNDED = "FOUNDED"              # 创办
    INVESTED_IN = "INVESTED_IN"      # 投资
    OWNS = "OWNS"                    # 拥有

    # 法律关系
    CITES = "CITES"                  # 引用法律
    DEFINED_BY = "DEFINED_BY"        # 由...定义
    APPLIES_TO = "APPLIES_TO"        # 适用于

    # 案例关系
    SIMILAR_TO = "SIMILAR_TO"        # 相似案例
    INVOLVES = "INVOLVES"            # 涉及
    PRECEDES = "PRECEDES"            # 前置

    # 概念关系
    RELATED_TO = "RELATED_TO"        # 相关
    SUBTYPE_OF = "SUBTYPE_OF"        # 子类型
    CAUSES = "CAUSES"                # 导致

    # 文档关系
    ABOUT = "ABOUT"                  # 关于
    REFERENCES = "REFERENCES"        # 参考
    SUPERSEDES = "SUPERSEDES"        # 取代


@dataclass
class Relation:
    """关系数据结构"""
    subject: str           # 主体实体
    subject_type: str      # 主体类型
    relation: str          # 关系类型
    object: str            # 客体实体
    object_type: str       # 客体类型
    confidence: float = 1.0  # 置信度
    source_text: str = ""    # 来源文本片段


# 预定义关系模式（法律领域）
RELATION_PATTERNS = [
    # 人物-组织关系
    {
        'pattern': r'([^\s，。！？]{2,4})(?:创办|创立|成立)了?([^\s，。！？]{2,20})(?:公司|企业|集团)',
        'relation': RelationType.FOUNDED,
        'subject_type': 'PERSON',
        'object_type': 'ORGANIZATION',
    },
    {
        'pattern': r'([^\s，。！？]{2,4})(?:在|于)([^\s，。！？]{2,20})(?:公司|企业|集团|单位)工作',
        'relation': RelationType.WORKS_FOR,
        'subject_type': 'PERSON',
        'object_type': 'ORGANIZATION',
    },
    {
        'pattern': r'([^\s，。！？]{2,4})(?:是|担任)([^\s，。！？]{2,20})(?:的)?(?:法人|股东|董事|经理)',
        'relation': RelationType.WORKS_FOR,
        'subject_type': 'PERSON',
        'object_type': 'ORGANIZATION',
    },

    # 法律引用关系
    {
        'pattern': r'根据《([^》]+)》',
        'relation': RelationType.CITES,
        'subject_type': 'DOCUMENT',
        'object_type': 'LAW',
    },
    {
        'pattern': r'依照([^\s，。！？]{2,15}法)第',
        'relation': RelationType.CITES,
        'subject_type': 'DOCUMENT',
        'object_type': 'LAW',
    },
    {
        'pattern': r'([^，。！？]{2,15}法)第(\d+)条规定',
        'relation': RelationType.DEFINED_BY,
        'subject_type': 'CONCEPT',
        'object_type': 'LAW',
    },

    # 概念关系
    {
        'pattern': r'([^\s，。！？]{2,10})包括([^\s，。！？]{2,10})',
        'relation': RelationType.RELATED_TO,
        'subject_type': 'CONCEPT',
        'object_type': 'CONCEPT',
    },
    {
        'pattern': r'([^\s，。！？]{2,10})导致([^\s，。！？]{2,10})',
        'relation': RelationType.CAUSES,
        'subject_type': 'CONCEPT',
        'object_type': 'CONCEPT',
    },
    {
        'pattern': r'([^\s，。！？]{2,10})诉讼时效为([^\s，。！？]{2,10})',
        'relation': RelationType.DEFINED_BY,
        'subject_type': 'CONCEPT',
        'object_type': 'DURATION',
    },

    # 案例关系
    {
        'pattern': r'([^\s，。！？]{2,20})案涉及([^\s，。！？]{2,10})',
        'relation': RelationType.INVOLVES,
        'subject_type': 'CASE',
        'object_type': 'CONCEPT',
    },
    {
        'pattern': r'([^\s，。！？]{2,20})案与([^\s，。！？]{2,20})案相似',
        'relation': RelationType.SIMILAR_TO,
        'subject_type': 'CASE',
        'object_type': 'CASE',
    },
]


class RelationExtractor:
    """关系抽取服务

    支持两种模式:
    1. 规则模式: 快速，基于预定义模式匹配
    2. LLM模式: 更准确，使用大语言模型进行关系抽取
    """

    def __init__(self, llm_client=None):
        """初始化关系抽取器

        Args:
            llm_client: LLM 客户端（可选，用于 LLM 模式）
        """
        self.llm_client = llm_client
        self.patterns = RELATION_PATTERNS

    async def extract(
        self,
        text: str,
        entities: List[Dict[str, Any]]
    ) -> List[Relation]:
        """从文本中抽取实体间关系

        Args:
            text: 输入文本
            entities: 已识别的实体列表

        Returns:
            关系列表
        """
        relations = []

        # 1. 基于规则的关系抽取
        relations.extend(self._rule_based_extract(text))

        # 2. 基于实体共现的关系（弱关系）
        relations.extend(self._cooccurrence_extract(text, entities))

        # 3. 使用 LLM 进行关系抽取（如果启用）
        if self.llm_client:
            llm_relations = await self._llm_extract(text, entities)
            relations.extend(llm_relations)

        # 4. 去重
        relations = self._deduplicate(relations)

        return relations

    def _rule_based_extract(self, text: str) -> List[Relation]:
        """基于规则的关系抽取"""
        relations = []

        for pattern_config in self.patterns:
            pattern = pattern_config['pattern']
            relation_type = pattern_config['relation']

            matches = re.finditer(pattern, text)
            for match in matches:
                groups = match.groups()
                if len(groups) >= 2:
                    subject = groups[0].strip()
                    obj = groups[1].strip()

                    # 避免提取到空实体
                    if subject and obj:
                        relations.append(Relation(
                            subject=subject,
                            subject_type=pattern_config.get('subject_type', 'ENTITY'),
                            relation=relation_type.value if isinstance(relation_type, RelationType) else relation_type,
                            object=obj,
                            object_type=pattern_config.get('object_type', 'ENTITY'),
                            confidence=0.8,
                            source_text=match.group(0)
                        ))

        return relations

    def _cooccurrence_extract(
        self,
        text: str,
        entities: List[Dict[str, Any]]
    ) -> List[Relation]:
        """基于实体共现的关系抽取

        如果两个实体在同一句子中出现，建立弱关系
        """
        relations = []

        if len(entities) < 2:
            return relations

        # 按句子分割
        sentences = re.split(r'[。！？\n]', text)

        for sentence in sentences:
            if not sentence.strip():
                continue

            # 找出句子中的实体
            sentence_entities = []
            for entity in entities:
                if entity['text'] in sentence:
                    sentence_entities.append(entity)

            # 建立共现关系
            for i, e1 in enumerate(sentence_entities):
                for e2 in sentence_entities[i+1:]:
                    # 只对不同类型的实体建立关系
                    if e1['type'] != e2['type']:
                        relations.append(Relation(
                            subject=e1['text'],
                            subject_type=e1['type'],
                            relation=RelationType.RELATED_TO.value,
                            object=e2['text'],
                            object_type=e2['type'],
                            confidence=0.5,  # 共现关系置信度较低
                            source_text=sentence[:100]
                        ))

        return relations

    async def _llm_extract(
        self,
        text: str,
        entities: List[Dict[str, Any]]
    ) -> List[Relation]:
        """使用 LLM 进行关系抽取"""
        if not self.llm_client:
            return []

        relations = []

        try:
            # 构建提示词
            entity_list = [f"{e['text']}({e['type']})" for e in entities]
            prompt = f"""请从以下文本中抽取实体之间的关系。

文本：
{text[:1000]}

已识别的实体：
{', '.join(entity_list[:10])}

请以 JSON 格式输出关系列表，格式如下：
[
    {{"subject": "实体1", "relation": "关系类型", "object": "实体2", "confidence": 0.9}}
]

关系类型包括：
- WORKS_FOR: 任职于
- FOUNDED: 创办
- CITES: 引用法律
- DEFINED_BY: 由...定义
- RELATED_TO: 相关
- CAUSES: 导致
- INVOLVES: 涉及

只输出 JSON 数组，不要有其他内容。"""

            # 调用 LLM
            response = await self.llm_client.generate(prompt)

            # 解析结果
            # 尝试提取 JSON
            json_match = re.search(r'\[[\s\S]*\]', response)
            if json_match:
                json_str = json_match.group(0)
                result = json.loads(json_str)

                for item in result:
                    if all(k in item for k in ['subject', 'relation', 'object']):
                        relations.append(Relation(
                            subject=item['subject'],
                            subject_type='ENTITY',
                            relation=item['relation'],
                            object=item['object'],
                            object_type='ENTITY',
                            confidence=item.get('confidence', 0.7),
                            source_text=text[:200]
                        ))

        except json.JSONDecodeError as e:
            logger.warning(f"LLM 关系抽取结果解析失败: {e}")
        except Exception as e:
            logger.warning(f"LLM 关系抽取失败: {e}")

        return relations

    def _deduplicate(self, relations: List[Relation]) -> List[Relation]:
        """去重关系"""
        if not relations:
            return relations

        seen = set()
        unique = []

        for r in relations:
            key = (r.subject, r.relation, r.object)
            if key not in seen:
                seen.add(key)
                unique.append(r)

        return unique

    def get_relation_types(self) -> Dict[str, str]:
        """获取支持的关系类型"""
        return {
            RelationType.WORKS_FOR.value: "任职于",
            RelationType.FOUNDED.value: "创办",
            RelationType.INVESTED_IN.value: "投资",
            RelationType.CITES.value: "引用法律",
            RelationType.DEFINED_BY.value: "由...定义",
            RelationType.SIMILAR_TO.value: "相似",
            RelationType.RELATED_TO.value: "相关",
            RelationType.CAUSES.value: "导致",
            RelationType.INVOLVES.value: "涉及",
            RelationType.ABOUT.value: "关于",
        }


# 全局实例（默认不使用 LLM）
relation_extractor = RelationExtractor(llm_client=None)