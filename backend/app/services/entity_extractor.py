"""
实体识别服务
用于从文本中提取实体（人物、组织、法律、案例等）
"""
import re
from typing import List, Dict
from dataclasses import dataclass
import logging


logger = logging.getLogger(__name__)


@dataclass
class Entity:
    """实体数据结构"""
    text: str           # 实体文本
    type: str           # 实体类型
    start: int          # 起始位置
    end: int            # 结束位置
    confidence: float = 1.0  # 置信度


class EntityExtractor:
    """实体识别服务

    支持两种模式:
    1. 规则+词典模式: 快速，无需外部依赖
    2. HanLP模式: 更准确，需要安装 hanlp

    法律领域实体类型:
    - PERSON: 人物
    - ORGANIZATION: 组织/公司
    - LAW: 法律法规
    - CASE: 案例
    - DOCUMENT: 文档类型
    - CONCEPT: 法律概念
    - DATE: 日期
    - MONEY: 金额
    """

    # 法律领域实体类型
    ENTITY_TYPES = {
        'PERSON': '人物',
        'ORGANIZATION': '组织',
        'LAW': '法律',
        'CASE': '案例',
        'DOCUMENT': '文档',
        'CONCEPT': '概念',
        'DATE': '日期',
        'MONEY': '金额',
        'LOCATION': '地点',
    }

    # 法律关键词词典
    LAW_KEYWORDS = {
        'LAW': [
            '民法典', '合同法', '公司法', '劳动法', '刑法',
            '民事诉讼法', '行政诉讼法', '宪法', '知识产权法',
            '商标法', '专利法', '著作权法', '消费者权益保护法',
            '劳动合同法', '社会保险法', '婚姻法', '继承法',
        ],
        'CONCEPT': [
            '诉讼时效', '违约金', '管辖', '举证', '强制执行',
            '合同违约', '侵权责任', '精神损害赔偿', '连带责任',
            '不可抗力', '善意取得', '不当得利', '无因管理',
            '合同解除', '撤销权', '代位权', '抗辩权',
        ],
        'DOCUMENT': [
            '合同', '协议', '判决书', '裁定书', '调解书',
            '起诉状', '答辩状', '上诉状', '申请书', '委托书',
            '借条', '欠条', '收据', '发票', '营业执照',
        ],
    }

    # 正则表达式模式
    PATTERNS = {
        'MONEY': [
            r'(\d+(?:\.\d{1,2})?)\s*[万元]',  # 金额
            r'[人民币￥]\s*(\d+(?:\.\d{1,2})?)',  # 人民币
        ],
        'DATE': [
            r'(\d{4})年(\d{1,2})月(\d{1,2})日?',  # 日期
            r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',  # 日期格式
        ],
        'PERSON': [
            r'([^\s，。！？]{2,4})(?:说|表示|认为|指出|称)',  # 某人说
            r'(原告|被告|上诉人|被上诉人|申请人|被申请人)([^\s，。！？]{2,4})',  # 诉讼角色
        ],
        'ORGANIZATION': [
            r'([^\s，。！？]{2,20})(?:公司|集团|企业|银行|法院|检察院|公安局|政府)',
            r'([^，。！？]{2,10})(?:律师事务所)',
        ],
        'CASE': [
            r'（(\d{4})）[^）]+号',  # 案号格式
            r'([^\s，。！？]+案)',
        ],
        'LAW': [
            r'《([^》]+)》',  # 法律名称
            r'根据([^\s，。！？]{2,15}法)',
        ],
    }

    def __init__(self, use_hanlp: bool = False):
        """初始化实体识别器

        Args:
            use_hanlp: 是否使用 HanLP 模型（更准确但需要额外安装）
        """
        self.use_hanlp = use_hanlp
        self.hanlp_ner = None

        if use_hanlp:
            try:
                import hanlp
                self.hanlp_ner = hanlp.load(hanlp.pretrained.ner.MSRA_NER_BERT_BASE_ZH)
                logger.info("HanLP NER 模型加载成功")
            except ImportError:
                logger.warning("HanLP 未安装，将使用规则模式")
                self.use_hanlp = False
            except Exception as e:
                logger.warning(f"HanLP 加载失败: {e}，将使用规则模式")
                self.use_hanlp = False

    def extract(self, text: str) -> List[Entity]:
        """从文本中提取实体

        Args:
            text: 输入文本

        Returns:
            实体列表
        """
        entities = []

        # 1. 使用正则表达式提取
        entities.extend(self._regex_extract(text))

        # 2. 使用词典匹配
        entities.extend(self._dict_extract(text))

        # 3. 使用 HanLP 提取（如果启用）
        if self.use_hanlp and self.hanlp_ner:
            entities.extend(self._hanlp_extract(text))

        # 4. 去重和合并
        entities = self._deduplicate(entities)

        # 5. 按位置排序
        entities.sort(key=lambda e: e.start)

        return entities

    def _regex_extract(self, text: str) -> List[Entity]:
        """使用正则表达式提取实体"""
        entities = []

        for entity_type, patterns in self.PATTERNS.items():
            for pattern in patterns:
                matches = re.finditer(pattern, text)
                for match in matches:
                    entity_text = match.group(0)
                    # 清理提取的文本
                    entity_text = entity_text.strip('《》「」【】')

                    entities.append(Entity(
                        text=entity_text,
                        type=entity_type,
                        start=match.start(),
                        end=match.end(),
                        confidence=0.85  # 规则匹配置信度
                    ))

        return entities

    def _dict_extract(self, text: str) -> List[Entity]:
        """使用词典匹配实体"""
        entities = []

        for entity_type, keywords in self.LAW_KEYWORDS.items():
            for keyword in keywords:
                start = 0
                while True:
                    pos = text.find(keyword, start)
                    if pos == -1:
                        break

                    entities.append(Entity(
                        text=keyword,
                        type=entity_type,
                        start=pos,
                        end=pos + len(keyword),
                        confidence=0.95  # 词典匹配置信度高
                    ))
                    start = pos + len(keyword)

        return entities

    def _hanlp_extract(self, text: str) -> List[Entity]:
        """使用 HanLP 提取实体"""
        entities = []

        if self.hanlp_ner is None:
            # HanLP 未加载（未安装或加载失败），无实体可提取
            return entities

        try:
            # HanLP NER 输出格式: [(实体文本, 实体类型, 开始, 结束), ...]
            results = self.hanlp_ner(text)

            # HanLP 类型映射
            type_mapping = {
                'NR': 'PERSON',
                'PER': 'PERSON',
                'NT': 'ORGANIZATION',
                'ORG': 'ORGANIZATION',
                'NS': 'LOCATION',
                'LOC': 'LOCATION',
                'TIME': 'DATE',
                'DURATION': 'DATE',
                'MONEY': 'MONEY',
            }

            for entity_text, entity_type, start, end in results:
                mapped_type = type_mapping.get(entity_type, (entity_type or "").upper())
                entities.append(Entity(
                    text=entity_text,
                    type=mapped_type,
                    start=start,
                    end=end,
                    confidence=0.9  # HanLP 置信度
                ))

        except Exception as e:
            logger.warning(f"HanLP 提取失败: {e}")

        return entities

    def _deduplicate(self, entities: List[Entity]) -> List[Entity]:
        """去重实体

        规则：
        1. 相同位置相同类型的实体只保留一个
        2. 重叠的实体保留置信度高的
        """
        if not entities:
            return entities

        # 按位置排序
        sorted_entities = sorted(entities, key=lambda e: (e.start, -e.confidence))

        result = []
        for entity in sorted_entities:
            # 检查是否与已有实体重叠
            is_duplicate = False
            for existing in result:
                # 检查重叠
                if entity.start < existing.end and entity.end > existing.start:
                    # 重叠，检查是否是同一实体
                    if entity.text == existing.text:
                        is_duplicate = True
                        break
                    # 不同实体但重叠，保留置信度高的
                    if entity.confidence > existing.confidence:
                        result.remove(existing)
                    else:
                        is_duplicate = True
                    break

            if not is_duplicate:
                result.append(entity)

        return result

    def get_entity_types(self) -> Dict[str, str]:
        """获取支持的实体类型"""
        return self.ENTITY_TYPES.copy()


# 全局实例（默认使用规则模式）
entity_extractor = EntityExtractor(use_hanlp=False)