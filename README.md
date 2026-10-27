# ETF 基金规模抓取与可视化分析

这是一个基于 Python 的 ETF 基金规模数据工具：从上海证券交易所和深圳证券交易所抓取 ETF 规模，保存到本地 SQLite（`data.db`），并按基金分类绘制规模走势图、输出周期变动统计。

## 数据来源

| 市场 | 基金示例 | 页面/接口 | 数据处理 |
| --- | --- | --- | --- |
| 上交所 | 510050、510300、588000 | [上交所基金规模页](https://etf.sse.com.cn/fundlist/scalelist/index.shtml?code=510050) | 历史规模本身就是亿元，直接入库 |
| 深交所 | 159915、159952 | [深交所 ETF 规模页](https://www.szse.cn/market/fund/volume/etf/index.html) | 规模表单位为万份，结合官网单位净值换算成亿元 |

深交所换算公式：

```text
基金规模（亿元） = 当前规模（万份） × 单位基金净值（元/份） ÷ 10000
```

例如 159915 的当前规模为 `3,470,745.49 万份`，同日单位净值为 `2.6556 元`，换算后约为 `921.6912 亿元`。这样深市和沪市数据可以直接放在同一张图里比较。

## 增量更新策略

### 上交所基金

- 如果数据库中没有该基金：首次拉取完整历史；
- 如果数据库中已有该基金：读取该基金最新日期，只请求“最新日期 + 1 天”到今天的数据；
- 使用接口的 `START_DATE` / `END_DATE` 参数，不再重复下载已有历史；
- 同一基金同一天已有记录时使用 `INSERT OR IGNORE` 跳过。

`--history` 仅用于上交所基金的数据重建，会主动回补完整历史。日常运行 `main.py` 不会使用它。

### 深交所基金（159 开头）

- 脚本自动识别 `159` 开头的代码并切换深交所数据源；
- 默认查询最近 30 天的规模表，选取“规模日期和净值日期能够匹配”的最新一天；
- 当日数据尚未更新时，自动使用最近一个可用交易日；
- 深交所页面没有提供可直接入库的“亿元”字段，因此规模统一按上面的公式换算为亿元；
- 官网基金净值接口只返回最近 10 个交易日，所以本项目对深市基金只维护最新可用数据，不拉取深市完整历史。

## 环境准备

要求 Python 3.10+。项目已有 `.venv` 虚拟环境，在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果使用 PyCharm，将解释器设为：

```text
E:\Projects\WebScraperApp\.venv\Scripts\python.exe
```

## 快速使用

### 一键更新所有基金

```powershell
.\.venv\Scripts\python.exe main.py
```

执行流程：

1. 按分类遍历 `FUND_CATEGORIES` 中的所有代码；
2. 上交所基金增量补数据，深交所基金抓取最新可用数据；
3. 运行 `fund_stats.py` 输出最近 1 天、1 周、1 月、半年的规模变化；
4. 运行 `show_trend.py --save --no-show` 生成全量图和最近半年图（不弹窗）。

### 单独抓取一只基金

```powershell
# 上交所：按数据库最新日期增量抓取
.\.venv\Scripts\python.exe fundscale_fetch.py --code 510050

# 深交所：输入代码，抓取最新可用数据并换算为亿元
.\.venv\Scripts\python.exe fundscale_fetch.py --code 159915
.\.venv\Scripts\python.exe fundscale_fetch.py --code 159952

# 上交所：明确回补完整历史（仅重建数据库时使用）
.\.venv\Scripts\python.exe fundscale_fetch.py --code 510050 --history
```

### 查看图表和统计

```powershell
# 弹出所有分类的图表
.\.venv\Scripts\python.exe show_trend.py

# 保存全量图和最近半年图，不弹窗
.\.venv\Scripts\python.exe show_trend.py --save --no-show

# 输出基金规模变动统计
.\.venv\Scripts\python.exe fund_stats.py
```

图片文件名形如：

```text
上证50ETF_trend_history.png
上证50ETF_trend_recent.png
创业板指_trend_history.png
创业板指_trend_recent.png
```

## 数据库结构

```sql
CREATE TABLE records (
    fund_code TEXT NOT NULL,  -- 基金代码
    date      TEXT NOT NULL,  -- 交易日，YYYY-MM-DD
    value     REAL NOT NULL,  -- 基金规模，统一为亿元
    fund_name TEXT,           -- 官方基金简称
    PRIMARY KEY (fund_code, date)
);
```

## 增加基金

编辑 `fundscale_fetch.py` 中的 `FUND_CATEGORIES`：

```python
FUND_CATEGORIES = {
    "上证50ETF": ["510050", "510100", "510190", "510850", "563090"],
    "沪深300ETF": ["510300", "510330", "510360", "510310"],
    "科创50ETF": ["588000", "588080"],
    "中证500": ["510500", "512500", "512510"],
    "中证1000": ["512100", "560010"],
    "创业板指": ["159915", "159952"],
}
```

新增 `159` 开头代码会自动使用深交所抓取逻辑，其余代码使用上交所逻辑。

## 注意事项

- **数据日期**：页面在交易日晚上更新 T 日数据，T+1 日早间会校正。盘中或收盘后较早运行时，可能只能拿到上一交易日的数据，脚本会使用最新可用日期。
- **深市历史限制**：深交所基金净值接口只提供最近 10 个交易日，因此深市基金按日增量积累，不能像上交所那样一次性回补多年历史。
- **网络**：程序内置重试；某只基金失败不会中断 `main.py`，最后会列出失败代码。
- **数据来源**：上海证券交易所基金网站、深圳证券交易所 ETF 规模及基金净值页面。

## 项目文件

```text
.
├── main.py               # 批量增量更新 + 统计 + 绘图
├── fundscale_fetch.py    # 上交所/深交所抓取、单位换算、SQLite 写入
├── fund_stats.py         # 各周期规模变化文字报告
├── show_trend.py         # 按分类生成全量/最近半年走势图
├── requirements.txt      # 第三方依赖
├── data.db               # SQLite 数据库
└── README.md
```
