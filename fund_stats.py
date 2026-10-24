#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计并输出各基金在不同周期（最近1天、1周、1月、半年）的规模变化情况，并显示具体的对照日期。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from collections import defaultdict
from pathlib import Path

from fundscale_fetch import DEFAULT_DB_NAME, FUND_CATEGORIES, force_utf8_when_piped


def get_latest_value_on_or_before(records: list[tuple[date, float]], target_date: date) -> tuple[date, float] | None:
    """在记录列表中寻找小于等于 target_date 的最近一天的数据（用于处理非交易日对齐）。"""
    valid = [(d, v) for d, v in records if d <= target_date]
    if not valid:
        return None
    valid.sort(key=lambda x: x[0])
    return valid[-1]


def main() -> int:
    force_utf8_when_piped()
    db_path = Path(DEFAULT_DB_NAME)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path.resolve()}\n请先运行 main.py 抓取数据。", file=sys.stderr)
        return 1

    connection = sqlite3.connect(str(db_path))
    try:
        raw_rows = connection.execute(
            "SELECT fund_code, date, value, fund_name FROM records ORDER BY fund_code, date"
        ).fetchall()
    finally:
        connection.close()

    if not raw_rows:
        print("数据库中暂无记录。", file=sys.stderr)
        return 1

    # 建立代码到分类的映射
    code_to_category = {}
    for cat_name, codes in FUND_CATEGORIES.items():
        for c in codes:
            code_to_category[c] = cat_name

    categorized_records: dict[str, dict[str, list[tuple[date, float]]]] = defaultdict(lambda: defaultdict(list))
    fund_names_map: dict[str, str] = {}

    for f_code, raw_date, raw_value, f_name in raw_rows:
        f_code = str(f_code)
        if f_name and f_name.strip():
            fund_names_map[f_code] = f_name.strip()
        else:
            fund_names_map[f_code] = f"基金 {f_code}"

        try:
            day = date.fromisoformat(str(raw_date)[:10])
            value = float(raw_value)
        except (TypeError, ValueError):
            continue

        category = code_to_category.get(f_code, "其他ETF")
        categorized_records[category][f_code].append((day, value))

    # 寻找全局最新日期作为基准点
    all_dates = [date.fromisoformat(str(r[1])[:10]) for r in raw_rows]
    if not all_dates:
        print("无效的日期数据。", file=sys.stderr)
        return 1
    global_latest_date = max(all_dates)

    # 定义周期及目标回溯天数
    periods = [
        ("最近1天", 1),
        ("最近1周", 7),
        ("最近1月", 30),
        ("最近半年", 182),
    ]

    # 获取全图中各周期实际对齐的推算目标日期（以全局最新日期往前推）
    period_target_dates = {}
    for p_name, days_delta in periods:
        period_target_dates[p_name] = global_latest_date - timedelta(days=days_delta)

    print(
        f"\n==========================================================================================================")
    print(f"                                   ETF 基金规模变动统计报告")
    print(f"  最新数据基准日期: {global_latest_date}")
    print(f"  各周期对比基准日: " + " | ".join([f"{p_name}(~{period_target_dates[p_name]})" for p_name, _ in periods]))
    print(
        f"==========================================================================================================\n")

    for category_name, fund_dict in categorized_records.items():
        print(f"【分类：{category_name}】")
        print("-" * 110)
        print(
            f"{'代码':<6} | {'基金名称':<12} | {'最新规模':<9} | {'最近1天变动':<16} | {'最近1周变动':<16} | {'最近1月变动':<16} | {'最近半年变动':<16}")
        print("-" * 110)

        for f_code, records in fund_dict.items():
            records.sort(key=lambda x: x[0])
            fund_name = fund_names_map.get(f_code, f"基金 {f_code}")

            # 最新数据
            latest_rec = records[-1]
            latest_val = latest_rec[1]
            latest_date_str = latest_rec[0].strftime("%Y-%m-%d")

            period_strs = []
            for p_name, days_delta in periods:
                target_d = global_latest_date - timedelta(days=days_delta)
                past_rec = get_latest_value_on_or_before(records, target_d)

                if past_rec:
                    past_d, past_val = past_rec
                    diff_val = latest_val - past_val
                    diff_pct = (diff_val / past_val) * 100 if abs(past_val) > 1e-6 else 0.0
                    sign = "+" if diff_val > 0 else ""
                    # 包含变动金额、百分比以及对比的具体日期
                    p_str = f"{sign}{diff_val:.2f}亿({sign}{diff_pct:.1f}%) [{past_d.strftime('%m-%d')}]"
                else:
                    p_str = "无历史数据"
                period_strs.append(p_str)

            print(
                f"{f_code:<6} | {fund_name:<12} | {latest_val:<9.2f}亿 | {period_strs[0]:<16} | {period_strs[1]:<16} | {period_strs[2]:<16} | {period_strs[3]:<16}")

        print("-" * 110 + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())