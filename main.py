#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键批量更新所有分类 ETF 的规模数据，打印变动报告并生成走势对比图。"""

import subprocess
import sys

from fundscale_fetch import FUND_CATEGORIES


def main() -> int:
    print("=== 开始批量更新 ETF 基金规模数据 ===")
    print("上交所基金按数据库最新日期增量更新；深交所基金抓取最新可用数据。")

    failures: list[str] = []
    for category, codes in FUND_CATEGORIES.items():
        print(f"\n[分类] ---------------- {category} ----------------")
        for code in codes:
            print(f"正在更新基金代码: {code} ...")
            # 不传 --history：已有数据只补最新日期，避免重复抓取全部历史。
            cmd = [sys.executable, "fundscale_fetch.py", "--code", code]
            result = subprocess.run(cmd)
            if result.returncode != 0:
                failures.append(code)
                print(
                    f"提示: 基金 {code} 更新失败，可能是网络波动、停牌或暂无数据，"
                    "继续处理下一个。"
                )

    print("\n=== 所有基金数据更新完毕 ===")
    if failures:
        print("以下基金更新失败，请稍后重试：" + "、".join(failures))

    print("正在生成基金规模变动文字分析报告...")
    subprocess.run([sys.executable, "fund_stats.py"], check=False)

    print("\n正在生成趋势图（全量图和最近半年图，保存为图片）...")
    subprocess.run(
        [sys.executable, "show_trend.py", "--save", "--no-show"],
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
