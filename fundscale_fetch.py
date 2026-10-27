#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取基金规模，并保存到统一的 SQLite 数据库 (data.db)。

数据来源分两类：

* 上交所（SSE）基金：页面 https://etf.sse.com.cn/fundlist/scalelist/index.shtml?code=...
  页面脚本调用 query.sse.com.cn/commonQuery.do，历史数据单位为亿元。
* 深交所（SZSE）基金（159 开头，例如 159915、159952）：
  页面 https://www.szse.cn/market/fund/volume/etf/index.html 的“ETF规模”表格，
  原始字段 current_size 的单位是万份。脚本同时读取深交所官网的基金净值，
  用“万份 × 单位净值 ÷ 10000”换算成亿元，保证与上交所数据同单位。

更新策略：
* 上交所默认增量：只请求数据库中最新日期之后的数据；数据库为空时才拉取全部历史。
* 深交所默认只抓取最新可用一天的数据；若数据库已有该日期则跳过。
* --history 仅对上交所有意义，用于清空/重建数据库时回补完整历史。
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SSE_PAGE_URL_TEMPLATE = (
    "https://etf.sse.com.cn/fundlist/scalelist/index.shtml?code={code}"
)
SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SQL_ID = "COMMON_JJZWZ_JJLB_JJXQ_JJGM_CKLSGM_L"

SZSE_PAGE_URL = "https://www.szse.cn/market/fund/volume/etf/index.html"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_SCALE_CATALOG = "scsj_fund_jjgm"
SZSE_NAV_CATALOG = "1785_child"

DEFAULT_FUND_CODE = "510050"
DEFAULT_DB_NAME = "data.db"

# 定义基金分类及代码列表。159 开头为深交所基金，其余按上交所处理。
FUND_CATEGORIES = {
    "上证50ETF": ["510050", "510100", "510190", "510850", "563090"],
    "沪深300ETF": ["510300", "510330", "510360", "510310"],
    "科创50ETF": ["588000", "588080"],
    "中证500": ["510500", "512500", "512510"],
    "中证1000": ["512100", "560010"],
    "创业板指": ["159915", "159952"],
}

PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30.0
# 深交所页面单次最多查询约六个月；30 天足够覆盖春节等长假，且远小于限制。
SZSE_LOOKBACK_DAYS = 30
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
    """避免 Windows 管道输出使用 GBK 导致中文乱码。"""

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass


def six_digit_code(value: str) -> str:
    """argparse 类型：校验 6 位数字基金代码。"""

    code = str(value).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise argparse.ArgumentTypeError("基金代码必须是 6 位数字，例如 510050")
    return code


def is_szse_code(code: str) -> bool:
    """159 开头的是深交所基金；本项目其余配置代码走上交所。"""

    return code.startswith("159")


def default_db_path(code: str) -> Path:
    return Path(DEFAULT_DB_NAME)


def build_page_url(code: str) -> str:
    if is_szse_code(code):
        return SZSE_PAGE_URL
    return SSE_PAGE_URL_TEMPLATE.format(code=code)


def build_session() -> requests.Session:
    """创建带重试和浏览器请求头的会话。"""

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


def validate_sse_page(session: requests.Session, code: str, timeout: float) -> None:
    """访问上交所页面并确认基金规模表格存在。"""

    page_url = build_page_url(code)
    response = session.get(page_url, timeout=(10, timeout))
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "html.parser")
    if soup.select_one("#scalelist") is None:
        raise RuntimeError(
            f"上交所页面中未找到基金规模表格（#scalelist），"
            f"网站结构可能已变化：{page_url}"
        )


def validate_szse_page(session: requests.Session, timeout: float) -> None:
    """访问深交所 ETF 规模页面并确认报表容器存在。"""

    response = session.get(SZSE_PAGE_URL, timeout=(10, timeout))
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "html.parser")
    container = soup.select_one(
        '.report-container[data-s-catalog-id="scsj_fund_jjgm"]'
    )
    if container is None:
        raise RuntimeError(
            "深交所页面中未找到 ETF 规模报表容器，网站结构可能已变化："
            f"{SZSE_PAGE_URL}"
        )


def validate_page(session: requests.Session, code: str, timeout: float) -> None:
    if is_szse_code(code):
        validate_szse_page(session, timeout)
    else:
        validate_sse_page(session, code, timeout)


def _normalize_date(raw: object) -> str:
    """把接口日期统一为 YYYY-MM-DD。"""

    text = str(raw or "").strip()
    if not text:
        raise ValueError("日期为空")
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    return date.fromisoformat(text[:10]).isoformat()


def _parse_number(raw: object) -> float:
    """把接口数值转换为 float，支持千分位；无效值抛出 ValueError。"""

    text = str(raw or "").strip().replace(",", "")
    if text in {"", "-", "--", "None", "null", "NULL"}:
        raise ValueError("数值为空")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("数值不是有限数值")
    if value < 0:
        raise ValueError("数值不能为负数")
    return value


def _parse_sse_payload(
    payload: dict, requested_code: str
) -> tuple[list[ScaleRecord], dict]:
    """解析上交所 commonQuery.do 返回的一页数据。"""

    if not isinstance(payload, dict):
        raise RuntimeError("上交所接口返回的 JSON 结构异常。")

    if payload.get("actionErrors"):
        raise RuntimeError(f"上交所接口返回错误：{payload['actionErrors']}")

    page_help = payload.get("pageHelp") or {}
    if not isinstance(page_help, dict):
        raise RuntimeError("上交所接口缺少有效的 pageHelp 分页信息。")

    raw_rows = page_help.get("data") or []
    if not isinstance(raw_rows, list):
        raise RuntimeError("上交所接口中的 pageHelp.data 不是列表。")

    records: list[ScaleRecord] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue

        row_code = str(row.get("FUND_CODE") or "").strip()
        if row_code and row_code != requested_code:
            continue

        try:
            trade_date = _normalize_date(row.get("TRADE_DATE"))
            value = _parse_number(row.get("SCALE"))
        except (TypeError, ValueError):
            continue

        fund_name = (
            str(row.get("FUND_EXPANSION_ABBR") or "").strip()
            or f"基金 {requested_code}"
        )
        records.append(ScaleRecord(requested_code, trade_date, value, fund_name))

    return records, page_help


def fetch_sse_records(
    session: requests.Session,
    code: str,
    timeout: float,
    start_date: date | None = None,
) -> list[ScaleRecord]:
    """读取上交所基金规模。

    start_date 为 None 时拉取全部历史；否则只请求 start_date 至今天的区间。
    """

    page_url = build_page_url(code)
    validate_sse_page(session, code, timeout)

    today = date.today()
    if start_date is not None and start_date > today:
        return []

    records_by_date: dict[str, ScaleRecord] = {}
    page_no = 1

    while True:
        params = {
            "isPagination": "true",
            "sqlId": SSE_SQL_ID,
            "pageHelp.cacheSize": 1,
            "pageHelp.pageSize": PAGE_SIZE,
            "pageHelp.pageNo": page_no,
            "pageHelp.beginPage": page_no,
            "pageHelp.endPage": page_no,
            "FUND_CODE": code,
        }
        if start_date is not None:
            params["START_DATE"] = start_date.strftime("%Y%m%d")
            params["END_DATE"] = today.strftime("%Y%m%d")

        response = session.get(
            SSE_QUERY_URL,
            params=params,
            headers={"Referer": page_url},
            timeout=(10, timeout),
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("上交所接口返回的不是有效 JSON") from exc

        page_records, page_help = _parse_sse_payload(payload, code)
        for record in page_records:
            records_by_date.setdefault(record.trade_date, record)

        try:
            page_count = max(1, int(page_help.get("pageCount") or 1))
        except (TypeError, ValueError):
            page_count = 1

        if page_no >= page_count:
            break
        page_no += 1
        time.sleep(0.2)

    if not records_by_date and start_date is None:
        raise RuntimeError(
            f"未获取到上交所基金 {code} 的规模数据，请检查代码或稍后重试。"
        )

    return sorted(records_by_date.values(), key=lambda item: item.trade_date)


def _request_szse_report(
    session: requests.Session,
    params: dict,
    timeout: float,
) -> dict:
    """请求深交所报表接口并返回第一个报表块。"""

    response = session.get(
        SZSE_REPORT_URL,
        params=params,
        headers={"Referer": SZSE_PAGE_URL},
        timeout=(10, timeout),
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("深交所接口返回的不是有效 JSON") from exc

    if isinstance(payload, list):
        if not payload:
            raise RuntimeError("深交所接口返回了空报表。")
        block = payload[0]
    else:
        block = payload

    if not isinstance(block, dict):
        raise RuntimeError("深交所接口返回的 JSON 结构异常。")

    error = block.get("error")
    if error:
        raise RuntimeError(f"深交所接口返回错误：{error}")

    return block


def fetch_szse_size_rows(
    session: requests.Session,
    code: str,
    start: date,
    end: date,
    timeout: float,
) -> list[tuple[str, float, str]]:
    """读取深交所 ETF 规模表，返回 (日期, 万份, 基金简称)。"""

    validate_szse_page(session, timeout)
    rows_by_date: dict[str, tuple[str, float, str]] = {}
    page_no = 1

    while True:
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": SZSE_SCALE_CATALOG,
            "TABKEY": "tab1",
            "jjlb": "ETF",
            "txtDm": code,
            "txtStart": start.isoformat(),
            "txtEnd": end.isoformat(),
            "random": f"{time.time():.6f}",
            "tab1PAGENO": str(page_no),
        }
        block = _request_szse_report(session, params, timeout)
        metadata = block.get("metadata") or {}
        raw_rows = block.get("data") or []
        if not isinstance(raw_rows, list):
            raise RuntimeError("深交所接口中的 data 不是列表。")

        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            row_code = str(row.get("fund_code") or "").strip()
            if row_code and row_code != code:
                continue

            try:
                trade_date = _normalize_date(row.get("size_date"))
                size_wan = _parse_number(row.get("current_size"))
            except (TypeError, ValueError):
                continue

            fund_name = (
                str(row.get("security_short_name") or "").strip()
                or f"基金 {code}"
            )
            rows_by_date.setdefault(
                trade_date, (trade_date, size_wan, fund_name)
            )

        try:
            page_count = max(1, int(metadata.get("pagecount") or 1))
        except (TypeError, ValueError):
            page_count = 1

        if page_no >= page_count:
            break
        page_no += 1
        time.sleep(0.1)

    return [rows_by_date[key] for key in sorted(rows_by_date)]


def fetch_szse_navs(
    session: requests.Session,
    code: str,
    timeout: float,
) -> dict[str, float]:
    """读取深交所官网的基金单位净值，返回 {日期: 净值}。"""

    params = {
        "SHOWTYPE": "JSON",
        "CATALOGID": SZSE_NAV_CATALOG,
        "TABKEY": "tab1",
        "txtDm": code,
        "random": f"{time.time():.6f}",
        "tab1PAGENO": "1",
    }
    block = _request_szse_report(session, params, timeout)
    raw_rows = block.get("data") or []
    if not isinstance(raw_rows, list):
        raise RuntimeError("深交所基金净值接口中的 data 不是列表。")

    navs: dict[str, float] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        try:
            nav_date = _normalize_date(row.get("nav_date"))
            nav = _parse_number(row.get("nav_per_share"))
            if nav <= 0:
                continue
        except (TypeError, ValueError):
            continue
        navs.setdefault(nav_date, nav)
    return navs


def fetch_szse_records(
    session: requests.Session,
    code: str,
    timeout: float,
    start_date: date | None = None,
) -> list[ScaleRecord]:
    """读取深交所基金最新规模并换算成亿元。

    深交所当前规模单位为万份，必须配合同一天的单位净值换算：
        亿元 = 万份 × 元/份 ÷ 10000

    start_date 为 None 时查询最近 30 天并只返回最新一条；否则返回区间内
    所有能匹配到净值的记录。
    """

    today = date.today()
    if start_date is not None and start_date > today:
        return []

    latest_only = start_date is None
    query_start = today - timedelta(days=SZSE_LOOKBACK_DAYS)
    if start_date is not None:
        # 正常增量更新最多只差几天；若间隔过久则从最近 30 天重新衔接。
        query_start = max(start_date, today - timedelta(days=SZSE_LOOKBACK_DAYS))

    size_rows = fetch_szse_size_rows(
        session=session,
        code=code,
        start=query_start,
        end=today,
        timeout=timeout,
    )
    if not size_rows:
        return []

    navs = fetch_szse_navs(session, code, timeout)
    records: list[ScaleRecord] = []

    for trade_date, size_wan, fund_name in size_rows:
        nav = navs.get(trade_date)
        if nav is None:
            continue

        value = size_wan * nav / 10000.0
        if not math.isfinite(value) or value < 0:
            continue
        records.append(ScaleRecord(code, trade_date, value, fund_name))

    if not records:
        return []

    records.sort(key=lambda item: item.trade_date)
    if latest_only:
        return [records[-1]]
    return records


def open_database(db_path: Path) -> sqlite3.Connection:
    """打开/创建数据库并确保 records 表存在。"""

    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA busy_timeout = 5000")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS records
        (
            fund_code TEXT NOT NULL,
            date      TEXT NOT NULL,
            value     REAL NOT NULL,
            fund_name TEXT,
            PRIMARY KEY (fund_code, date)
        )
        """
    )
    connection.commit()
    return connection


def get_latest_date(connection: sqlite3.Connection, fund_code: str) -> str | None:
    """返回某只基金在数据库中的最新日期。"""

    row = connection.execute(
        "SELECT MAX(date) FROM records WHERE fund_code = ?",
        (fund_code,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def save_records(
    connection: sqlite3.Connection, records: Iterable[ScaleRecord]
) -> tuple[int, int]:
    """写入记录；同一基金同一天已有记录时跳过。返回（新增数，跳过数）。"""

    records = list(records)
    inserted = 0
    try:
        for record in records:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO records
                    (fund_code, date, value, fund_name)
                VALUES (?, ?, ?, ?)
                """,
                (
                    record.fund_code,
                    record.trade_date,
                    record.value,
                    record.fund_name,
                ),
            )
            inserted += max(cursor.rowcount, 0)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return inserted, len(records) - inserted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取上交所/深交所基金规模并保存到 SQLite。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--code",
        type=six_digit_code,
        default=DEFAULT_FUND_CODE,
        help="基金代码：159 开头自动走深交所，其余走上交所",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"数据库路径，默认 {DEFAULT_DB_NAME}",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="上交所基金回补全部历史；深交所仅按最新数据抓取",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="网络读取超时（秒）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_when_piped()
    args = build_parser().parse_args(argv)
    db_path = args.db if args.db is not None else default_db_path(args.code)
    page_url = build_page_url(args.code)
    source_name = "深交所" if is_szse_code(args.code) else "上交所"

    print(f"正在读取页面（{source_name}）：{page_url}")
    session = build_session()
    connection: sqlite3.Connection | None = None

    try:
        connection = open_database(db_path)
        latest_saved = get_latest_date(connection, args.code)
        today = date.today()
        initial = latest_saved is None

        if is_szse_code(args.code):
            if args.history:
                print("提示：深交所接口只提供近期净值，--history 仍按最新可用数据抓取。")

            start_date: date | None = None
            if latest_saved is not None:
                next_day = date.fromisoformat(latest_saved) + timedelta(days=1)
                if next_day > today:
                    print(
                        f"[{args.code}] 数据库已是最新（{latest_saved}），无需抓取。"
                    )
                    return 0
                start_date = next_day

            records = fetch_szse_records(
                session=session,
                code=args.code,
                timeout=args.timeout,
                start_date=start_date,
            )
        else:
            start_date = None
            if not args.history and latest_saved is not None:
                next_day = date.fromisoformat(latest_saved) + timedelta(days=1)
                if next_day > today:
                    print(
                        f"[{args.code}] 数据库已是最新（{latest_saved}），无需抓取。"
                    )
                    return 0
                start_date = next_day
                print(
                    f"[{args.code}] 数据库最新日期 {latest_saved}，"
                    f"只请求 {next_day.isoformat()} 以来的数据。"
                )
            elif args.history:
                print(f"[{args.code}] 正在回补全部历史数据。")

            records = fetch_sse_records(
                session=session,
                code=args.code,
                timeout=args.timeout,
                start_date=start_date,
            )

        if not records:
            if initial or (args.history and not is_szse_code(args.code)):
                raise RuntimeError(
                    f"未获取到基金 {args.code} 的数据，请检查代码、日期或稍后重试。"
                )
            print(
                f"[{args.code}] 没有新数据，数据库最新日期仍为 "
                f"{latest_saved or '无'}。"
            )
            return 0

        latest = records[-1]
        inserted, skipped = save_records(connection, records)

        print(
            f"[{args.code}] {latest.fund_name} | {source_name} | "
            f"最新日期 {latest.trade_date} | {latest.value:.4f} 亿元"
        )
        print(f"[{args.code}] 写入完成：新增 {inserted} 条，跳过 {skipped} 条。")
        if skipped:
            print(f"[{args.code}] 已有日期的记录已跳过，没有重复写入。")
        return 0

    except requests.RequestException as exc:
        print(f"[{args.code}] 网络请求失败：{exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"[{args.code}] 抓取失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    finally:
        if connection is not None:
            connection.close()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
