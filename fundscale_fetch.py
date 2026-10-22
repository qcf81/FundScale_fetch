#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取基金规模，并保存到统一的 SQLite 数据库 (data.db)，同时记录官方基金名称与分类。"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PAGE_URL_TEMPLATE = "https://etf.sse.com.cn/fundlist/scalelist/index.shtml?code={code}"
QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SQL_ID = "COMMON_JJZWZ_JJLB_JJXQ_JJGM_CKLSGM_L"

DEFAULT_FUND_CODE = "510050"
DEFAULT_DB_NAME = "data.db"

# 定义基金分类及代码列表
FUND_CATEGORIES = {
    "上证50ETF": ["510050", "510100", "510190", "510850", "563090"],
    "沪深300ETF": ["510300", "510330", "510360","510310"],
    "科创50ETF": ["588000", "588080"],
    "中证500": ["510500", "512500", "512510"],
    "中证1000": ["512100", "560010"],
    "创业板指": ["159915","159952"]
}

PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ScaleRecord:
    fund_code: str
    trade_date: str
    value: float
    fund_name: str


def force_utf8_when_piped() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass


def six_digit_code(value: str) -> str:
    code = str(value).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise argparse.ArgumentTypeError("基金代码必须是 6 位数字，例如 510050")
    return code


def default_db_path(code: str) -> Path:
    return Path(DEFAULT_DB_NAME)


def build_page_url(code: str) -> str:
    return PAGE_URL_TEMPLATE.format(code=code)


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def validate_page(session: requests.Session, code: str, timeout: float) -> str:
    page_url = build_page_url(code)
    response = session.get(page_url, timeout=(10, timeout))
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "html.parser")
    if soup.select_one("#scalelist") is None:
        raise RuntimeError(
            f"页面中未找到基金规模表格（#scalelist），网站结构可能已变化：{page_url}"
        )
    title = soup.title.get_text(" ", strip=True) if soup.title else "基金规模"
    return title


def _normalize_date(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("日期为空")
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    return date.fromisoformat(text[:10]).isoformat()


def _parse_scale(raw: object) -> float:
    text = str(raw or "").strip().replace(",", "")
    if text in {"", "-", "--", "None", "null", "NULL"}:
        raise ValueError("基金规模为空")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("基金规模不是有限数值")
    if value < 0:
        raise ValueError("基金规模不能为负数")
    return value


def _parse_payload(
        payload: dict, requested_code: str
) -> tuple[list[ScaleRecord], dict]:
    if not isinstance(payload, dict):
        raise RuntimeError("数据接口返回的 JSON 结构异常。")

    if payload.get("actionErrors"):
        raise RuntimeError(f"接口返回错误：{payload['actionErrors']}")

    page_help = payload.get("pageHelp") or {}
    if not isinstance(page_help, dict):
        raise RuntimeError("数据接口缺少有效的 pageHelp 分页信息。")

    raw_rows = page_help.get("data") or []
    if not isinstance(raw_rows, list):
        raise RuntimeError("数据接口中的 pageHelp.data 不是列表。")
    records: list[ScaleRecord] = []

    for row in raw_rows:
        if not isinstance(row, dict):
            continue

        row_code = str(row.get("FUND_CODE") or "").strip()
        if row_code and row_code != requested_code:
            continue

        try:
            trade_date = _normalize_date(row.get("TRADE_DATE"))
            value = _parse_scale(row.get("SCALE"))
        except (TypeError, ValueError):
            continue

        fund_name = str(row.get("FUND_EXPANSION_ABBR") or "").strip() or f"基金 {requested_code}"
        records.append(ScaleRecord(requested_code, trade_date, value, fund_name))

    return records, page_help


def fetch_scale_records(
        session: requests.Session,
        code: str,
        timeout: float,
        fetch_all: bool,
) -> list[ScaleRecord]:
    page_url = build_page_url(code)
    validate_page(session, code, timeout)

    records_by_date: dict[str, ScaleRecord] = {}
    page_no = 1
    page_size = PAGE_SIZE if fetch_all else 30

    while True:
        params = {
            "isPagination": "true",
            "sqlId": SQL_ID,
            "pageHelp.cacheSize": 1,
            "pageHelp.pageSize": page_size,
            "pageHelp.pageNo": page_no,
            "pageHelp.beginPage": page_no,
            "pageHelp.endPage": page_no,
            "FUND_CODE": code,
        }
        response = session.get(
            QUERY_URL,
            params=params,
            headers={"Referer": page_url},
            timeout=(10, timeout),
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("数据接口返回的不是有效 JSON") from exc

        page_records, page_help = _parse_payload(payload, code)
        for record in page_records:
            records_by_date.setdefault(record.trade_date, record)

        if not fetch_all:
            break

        try:
            page_count = max(1, int(page_help.get("pageCount") or 1))
        except (TypeError, ValueError):
            page_count = 1

        if page_no >= page_count:
            break

        page_no += 1
        time.sleep(0.2)

    if not records_by_date:
        raise RuntimeError(f"未获取到基金 {code} 的规模数据，请检查代码或稍后重试。")

    return sorted(records_by_date.values(), key=lambda item: item.trade_date)


def open_database(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA busy_timeout = 5000")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS records
        (
            fund_code
            TEXT
            NOT
            NULL,
            date
            TEXT
            NOT
            NULL,
            value
            REAL
            NOT
            NULL,
            fund_name
            TEXT,
            PRIMARY
            KEY
        (
            fund_code,
            date
        )
            )
        """
    )
    connection.commit()
    return connection


def save_records(
        connection: sqlite3.Connection, records: Iterable[ScaleRecord]
) -> tuple[int, int]:
    records = list(records)
    inserted = 0
    try:
        for record in records:
            cursor = connection.execute(
                """
                INSERT INTO records (fund_code, date, value, fund_name)
                VALUES (?, ?, ?, ?) ON CONFLICT(fund_code, date) 
                DO
                UPDATE SET fund_name=excluded.fund_name, value =excluded.value
                """,
                (record.fund_code, record.trade_date, record.value, record.fund_name),
            )
            inserted += max(cursor.rowcount, 0)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return inserted, len(records) - inserted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取基金规模并保存到 SQLite。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--code", type=six_digit_code, default=DEFAULT_FUND_CODE, help="基金代码")
    parser.add_argument("--db", type=Path, default=None, help="数据库路径")
    parser.add_argument("--history", action="store_true", help="导入全部历史数据")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="超时时间")
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_when_piped()
    args = build_parser().parse_args(argv)
    db_path = args.db if args.db is not None else default_db_path(args.code)
    page_url = build_page_url(args.code)

    print(f"正在读取页面：{page_url}")
    session = build_session()
    connection: sqlite3.Connection | None = None

    try:
        all_records = fetch_scale_records(
            session=session,
            code=args.code,
            timeout=args.timeout,
            fetch_all=args.history,
        )
        latest = all_records[-1]
        records_to_save = all_records if args.history else [latest]

        connection = open_database(db_path)
        inserted, skipped = save_records(connection, records_to_save)
        print(f"[{args.code}] 写入完成：新增 {inserted} 条，更新/跳过 {skipped} 条。")
        return 0
    except Exception as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())