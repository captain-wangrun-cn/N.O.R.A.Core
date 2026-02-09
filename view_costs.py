'''
Author: WR(captain-wangrun-cn)
Date: 2026-02-09 15:34:34
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-02-09 15:34:40
FilePath: \N.O.R.A.Core\view_costs.py
'''
'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 15:34:34
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
#!/usr/bin/env python3
"""
成本统计查看工具 - 查看 LLM 调用的 token 使用和成本
"""
import argparse
import sys
from core.cost_tracker import get_cost_tracker

def main():
    parser = argparse.ArgumentParser(
        description="查看 LLM 调用成本统计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python view_costs.py              # 查看总体统计
  python view_costs.py --today      # 查看今日统计
  python view_costs.py --week       # 查看本周统计
  python view_costs.py --month      # 查看本月统计
        """
    )
    
    parser.add_argument('--today', action='store_true', help='查看今日统计')
    parser.add_argument('--week', action='store_true', help='查看本周统计')
    parser.add_argument('--month', action='store_true', help='查看本月统计')
    parser.add_argument('--provider', type=str, help='筛选指定提供商 (gemini/openai)')
    parser.add_argument('--alias', type=str, help='筛选指定模型别名 (smart/fast/coder/summary)')
    
    args = parser.parse_args()
    
    # 确定时间段
    if args.today:
        period = "today"
    elif args.week:
        period = "week"
    elif args.month:
        period = "month"
    else:
        period = "total"
    
    # 获取 tracker
    tracker = get_cost_tracker()
    
    # 打印统计
    if args.provider or args.alias:
        stats = tracker.get_stats(period=period, provider=args.provider, model_alias=args.alias)
        
        print(f"\n{'='*60}")
        print(f"  LLM Usage Statistics - {period.upper()}")
        if args.provider:
            print(f"  Provider: {args.provider}")
        if args.alias:
            print(f"  Model Alias: {args.alias}")
        print(f"{'='*60}")
        print(f"Total Calls:        {stats['total_calls']}")
        print(f"Total Input Tokens: {stats['total_input_tokens']:,}")
        print(f"Total Output Tokens:{stats['total_output_tokens']:,}")
        print(f"Total Cost:         ${stats['total_cost']:.4f} USD")
        print(f"{'='*60}\n")
    else:
        tracker.print_stats(period=period)

if __name__ == "__main__":
    main()
