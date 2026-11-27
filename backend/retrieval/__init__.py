"""检索层：把多来源的原始结果统一成 Evidence。"""

from retrieval.result_normalizer import NormalizeReport, normalize_batch, to_json_payload

__all__ = ["normalize_batch", "NormalizeReport", "to_json_payload"]
