"""知鉴 Search 后端入口。

跑法：
    cd backend
    uvicorn main:app --reload --port 8000

接口：
    GET  /              健康检查，前端启动时用它判断后端在不在
    POST /api/search    主接口

约定（给前端）：
    · 只返回 JSON，不返回 HTML
    · 任何失败都返回 200 + mode=degraded，不返回 5xx
      （官方提交检查第 7 条：接口失败、额度耗尽、空数据都要有真实提示）
"""

from __future__ import annotations

import traceback
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import cache
import pipeline

app = FastAPI(title="知鉴 Search API", version="0.1.0")

# 本地开发与静态页面（file:// 或另一个端口）都会跨域，先全放开。
# 上线后如无必要，可以收紧到具体域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchOptions(BaseModel):
    filter_ad: bool = True
    include_baseline: bool = True
    time_window_days: int = 0


class SearchRequest(BaseModel):
    query: str = Field(default="", description="用户原话")
    options: SearchOptions = Field(default_factory=SearchOptions)


@app.get("/")
def health() -> dict[str, Any]:
    """健康检查。前端用它决定显示「真实检索」还是「演示数据」。"""
    return {
        "ok": True,
        "service": "zhijian-search",
        "mode": "real",
        "cached_queries": len(list(cache.CACHE_DIR.glob("*.json"))) if cache.CACHE_DIR.exists() else 0,
    }


@app.post("/api/search")
def search(body: SearchRequest) -> dict[str, Any]:
    try:
        return pipeline.run(body.query, body.options.model_dump())
    except Exception as exc:  # noqa: BLE001
        # 兜底：绝不让前端看到 500。有缓存就返回缓存，没有就返回空态。
        traceback.print_exc()
        fallback = cache.latest_any()
        if fallback:
            fallback["mode"] = "degraded"
            fallback["degraded_reason"] = f"这次没跑通（{type(exc).__name__}），先给你看上一次的结果"
            return fallback
        return {
            "query": body.query,
            "intent": "general",
            "intent_label": "综合探索",
            "mode": "degraded",
            "degraded_reason": f"服务出了点问题（{type(exc).__name__}），暂时没有可用结果",
            "answers": [],
            "cards": [],
            "matrices": [],
            "trace": [],
            "errors": [str(exc)],
            "stats": {"recalled": 0, "unique": 0, "after_freshness": 0, "hidden": 0, "kept": 0},
        }


@app.get("/api/cache/clear")
def clear_cache() -> dict[str, Any]:
    """调试用：清空缓存，强制重新检索。"""
    removed = 0
    if cache.CACHE_DIR.exists():
        for path in cache.CACHE_DIR.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return {"ok": True, "removed": removed}
