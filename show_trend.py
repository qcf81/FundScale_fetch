#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 SQLite 读取基金规模记录，并按类别（上证50ETF / 沪深300ETF）分别绘制独立的折线图。"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

from fundscale_fetch import (
    DEFAULT_DB_NAME,
    FUND_CATEGORIES,
    force_utf8_when_piped,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按基金大类分别绘制规模走势对比图。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"数据库路径，默认 {DEFAULT_DB_NAME}",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="把图表分别保存为图片（如 sse50_trend.png, csi300_trend.png）",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="只生成/保存图片，不弹出图表窗口",
    )
    return parser


def load_all_records(db_path: Path) -> tuple[dict[str, dict[str, list[tuple[date, float]]]], dict[str, str]]:
    db_path = db_path.expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path.resolve()}\n请先运行 main.py 抓取数据。")

    connection = sqlite3.connect(str(db_path))
    try:
        raw_rows = connection.execute("SELECT fund_code, date, value, fund_name FROM records ORDER BY fund_code, date").fetchall()
    finally:
        connection.close()

    # 构建代码到分类的映射
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
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            continue

        # 归类，若不在预定义列表中则归为 "其他ETF"
        category = code_to_category.get(f_code, "其他ETF")
        categorized_records[category][f_code].append((day, value))

    # 排序内部日期
    result = {}
    for cat, fund_dict in categorized_records.items():
        result[cat] = {f_code: sorted(items, key=lambda x: x[0]) for f_code, items in fund_dict.items()}

    return result, fund_names_map


def configure_chinese_font() -> None:
    from matplotlib import font_manager, rcParams
    candidates = (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Arial Unicode MS",
        "WenQuanYi Micro Hei",
    )
    available = {font.name for font in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            break
    rcParams["axes.unicode_minus"] = False


def draw_category_chart(
    category_name: str,
    fund_records: dict[str, list[tuple[date, float]]],
    fund_names_map: dict[str, str],
    save_images: bool,
    show: bool,
) -> None:
    import matplotlib

    if not show and not save_images:
        return
    if not show:
        matplotlib.use("Agg")

    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    configure_chinese_font()

    fig, ax = plt.subplots(figsize=(11.5, 6.2), dpi=120)
    colors = ["#b33939", "#218c74", "#227093", "#cc8e35", "#474787", "#706fd3", "#e17055", "#00cec9"]

    all_days = []
    for idx, (f_code, records) in enumerate(fund_records.items()):
        color = colors[idx % len(colors)]
        fund_name = fund_names_map.get(f_code, f"基金 {f_code}")
        days = [item[0] for item in records]
        values = [item[1] for item in records]
        all_days.extend(days)

        ax.plot(
            days,
            values,
            color=color,
            linewidth=1.8,
            marker="o" if len(days) <= 180 else None,
            markersize=4.0,
            markerfacecolor="white",
            markeredgewidth=1.0,
            label=fund_name,
            zorder=3,
        )

        if records:
            latest_day, latest_value = records[-1]
            ax.scatter([latest_day], [latest_value], color=color, s=48, zorder=4, edgecolor="white", linewidth=1.2)

    if all_days:
        min_day, max_day = min(all_days), max(all_days)
        if min_day == max_day:
            ax.set_xlim(min_day - timedelta(days=3), min_day + timedelta(days=3))
        else:
            ax.margins(x=0.025)
    ax.margins(y=0.14)

    title = f"{category_name} 规模走势对比"
    ax.set_title(title, fontsize=16, fontweight="bold", pad=16)
    ax.set_xlabel("日期", fontsize=11)
    ax.set_ylabel("基金规模（亿元）", fontsize=11)
    ax.grid(True, which="major", axis="both", linestyle="--", alpha=0.30)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))

    locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    ax.legend(loc="upper left", frameon=True, fontsize=10)
    fig.text(0.99, 0.012, "数据来源：上海证券交易所基金网站", ha="right", va="bottom", fontsize=8, color="#777777")
    fig.tight_layout(rect=(0, 0.03, 1, 1))

    if save_images:
        filename = f"{category_name}_trend.png"
        save_path = Path(filename)
        fig.savefig(save_path, dpi=160, bbox_inches="tight", facecolor="white")
        print(f"[{category_name}] 图表已保存为：{save_path.resolve()}")

    if show:
        print(f"正在显示 [{category_name}] 的图表窗口...")
        plt.show()
    else:
        plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    force_utf8_when_piped()
    args = build_parser().parse_args(argv)
    db_path = args.db if args.db is not None else Path(DEFAULT_DB_NAME)

    try:
        categorized_records, fund_names_map = load_all_records(db_path)
    except Exception as exc:
        print(f"读取数据库失败：{exc}", file=sys.stderr)
        return 1

    if not categorized_records:
        print(f"数据库中没有记录，请先运行 main.py。", file=sys.stderr)
        return 1

    # 针对每一个分类（上证50ETF、沪深300ETF等）分别画图
    for category_name, fund_records in categorized_records.items():
        if not fund_records:
            continue
        print(f"\n正在生成分类: {category_name} 的走势图...")
        draw_category_chart(
            category_name=category_name,
            fund_records=fund_records,
            fund_names_map=fund_names_map,
            save_images=args.save,
            show=not args.no_show,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())