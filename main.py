#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键批量抓取所有分类的 ETF 基金“历史规模数据”，更新数据库，打印变动文本报告并生成走势对比图。"""

import subprocess
import sys
from fundscale_fetch import FUND_CATEGORIES


def main():
    print("=== 开始批量抓取 ETF 基金历史规模数据 ===")

    # 循环遍历所有分类和代码
    for category, codes in FUND_CATEGORIES.items():
        print(f"\n[分类] ---------------- {category} ----------------")
        for code in codes:
            print(f"正在抓取基金代码: {code} 的历史数据 ...")

            cmd = [sys.executable, "fundscale_fetch.py", "--code", code, "--history"]
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print(f"提示: 基金 {code} 抓取可能遇到网络波动或暂无数据，继续处理下一个。")

    print("\n=== 所有基金历史数据更新完毕 ===")

    # 【新增】运行文本统计分析脚本，打印各周期规模变化
    print("正在生成基金规模变动文字分析报告...")
    subprocess.run([sys.executable, "fund_stats.py"])

    print("\n正在启动趋势图绘制程序（将按全量及最近半年分别画图）...")
    # 运行画图脚本（默认生成全量图与最近半年图）
    subprocess.run([sys.executable, "show_trend.py", "--save"])


if __name__ == "__main__":
    main()