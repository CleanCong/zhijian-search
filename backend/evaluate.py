#!/usr/bin/env python3
"""知鉴 Search · 评测命令行入口（任务 A6）。

filters.py 里已有 evaluate() 核心逻辑，这里补一个能独立跑的命令行入口。

用法：
    cd backend
    python evaluate.py --labels ../data/labels.csv
    python evaluate.py --labels ../data/labels.csv --sample ../data/sample-renovation.json

两种数据来源：
  1. --sample 指定一份检索结果 JSON（含 answers，带 content_text 等字段）：
     用当前 rules.json 重新跑一遍 filters.judge_all，再在标注集上评测。
     这样评测的是「规则 + 当前 rules.json」的真实效果，结果可复现。
  2. 不指定 --sample 时，默认读同目录样本 ../data/sample-renovation.json。

labels.csv 列（UTF-8，可带 BOM）：
    id, label[, title, author]
    label ∈ {可信, 软文, 过时, 跑题}；只有「可信 / 软文」参与召回/误杀评测。

输出：召回率 / 误杀率 / 混淆矩阵 / 漏判案例 / 误杀案例。
数字真跑真算，可直接写进计划书；换 rules.json 后重跑即可看到变化。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import filters  # noqa: E402


def _read_labels(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        print(f"[错误] 找不到标注文件：{path}", file=sys.stderr)
        sys.exit(2)
    labels: dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip() for c in (reader.fieldnames or [])}
        if not {"id", "label"}.issubset(cols):
            print(f"[错误] CSV 需含列 id,label，实际 {reader.fieldnames}", file=sys.stderr)
            sys.exit(2)
        for row in reader:
            rid = (row.get("id") or "").strip()
            lab = (row.get("label") or "").strip()
            if rid:
                labels[rid] = lab
    return labels


def _load_items(sample_path: str) -> list[dict]:
    if not os.path.exists(sample_path):
        print(f"[错误] 找不到样本文件：{sample_path}", file=sys.stderr)
        sys.exit(2)
    data = json.load(open(sample_path, encoding="utf-8"))
    items = data.get("answers") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print("[错误] 样本里没有 answers 列表", file=sys.stderr)
        sys.exit(2)
    return items


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="知鉴 Search 评测命令")
    ap.add_argument("--labels", required=True, help="标注 CSV 路径")
    ap.add_argument("--sample", default=os.path.join(here, "..", "data", "sample-renovation.json"),
                    help="检索结果样本 JSON（默认用全屋定制样本）")
    ap.add_argument("--rules", default=None, help="指定 rules.json（默认用 backend/rules.json）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出指标")
    args = ap.parse_args()

    labels = _read_labels(args.labels)
    items = _load_items(args.sample)

    # 用当前 rules.json 重新判定（评测的是真实规则效果，不吃样本里已存的旧判定）
    cfg = filters.load_rules(args.rules) if args.rules else filters.load_rules()
    scene = None
    data = json.load(open(args.sample, encoding="utf-8"))
    if isinstance(data, dict):
        scene = data.get("intent")
    filters.judge_all(items, cfg, scene=scene)

    metrics = filters.evaluate(items, labels)
    conf = metrics["confusion"]

    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return

    print("=" * 56)
    print("过滤器评测 · 软文识别（A6）")
    print(f"样本：{os.path.basename(args.sample)}　规则：rules.json v{cfg.get('version','?')}")
    print("=" * 56)
    evaluated = conf["tp"] + conf["fp"] + conf["fn"] + conf["tn"]
    print(f"参与评测（可信/软文）: {evaluated} 条（标注集共 {len(labels)} 条）")
    print(f"命中软文 TP: {conf['tp']}   漏判 FN: {conf['fn']}")
    print(f"误杀   FP: {conf['fp']}   正确放行 TN: {conf['tn']}")
    print("-" * 56)
    print(f"召回率 recall            : {metrics['recall']*100:.1f}%   （真软文抓到的比例）")
    print(f"误杀率 false_positive_rate: {metrics['false_positive_rate']*100:.1f}%   （正常内容被误判的比例）")
    print("-" * 56)

    if metrics["missed_ads"]:
        print(f"漏判案例（真软文未识别）× {len(metrics['missed_ads'])}：")
        for m in metrics["missed_ads"]:
            print(f"  · [{m['id']}] {m['title'][:40]}")
    else:
        print("漏判案例：无")

    if metrics["false_alarms"]:
        print(f"误杀案例（正常被判软文）× {len(metrics['false_alarms'])}：")
        for m in metrics["false_alarms"]:
            print(f"  · [{m['id']}] {m['title'][:40]}")
    else:
        print("误杀案例：无")


if __name__ == "__main__":
    main()
