"""核心数据契约。

所有层之间传递的就是这几个模型，前端不消费 LLM 原始输出。

设计原则：
1. 字段名一旦确定就冻结，改字段名等于全链路返工
2. 枚举值用 Literal，非法值在构造时就报错，不留到运行时
3. 模型只装数据，不做业务判断（判断在 services 层）
"""

from models.evidence import Evidence, SourceType, ContentType
from models.claim import Claim, ClaimType, EvidenceStatus, ModelConfidence
from models.knowledge import KnowledgePackage, Analogy, GlossaryItem, RelatedQuestion
from models.script import Script, Scene, Visual, VisualType

__all__ = [
    "Evidence", "SourceType", "ContentType",
    "Claim", "ClaimType", "EvidenceStatus", "ModelConfidence",
    "KnowledgePackage", "Analogy", "GlossaryItem", "RelatedQuestion",
    "Script", "Scene", "Visual", "VisualType",
]
