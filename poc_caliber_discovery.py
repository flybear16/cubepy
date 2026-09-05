#!/usr/bin/env python3
"""口径发现 POC：P1 聚合提取 + P3 指纹聚类（见 docs/2026-08-30-存量指标口径发现方案.md）

样例语料模拟 DataWorks/MaxCompute ODPS 数仓三层（dwd→dws→ads），
故意埋了三类问题用于验证发现能力：
  A. 同名不同指纹（口径冲突）: order_cnt 两处定义，一处 COUNT、一处 COUNT DISTINCT
  B. 同指纹不同名（重复建设）: pay_amt_1d 与 payment_amount_1d 计算逻辑完全一致
  C. 孤儿口径: arpu_1d 只在代码里，无登记
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT = "hive"  # 实测: sqlglot 30.x 无 odps 方言，hive 方言可完整解析 ODPS SQL

# ---------------- P0 语料（模拟 DataStudio 拉下来的任务 SQL） ----------------

CORPUS = {
    "task_001_dwd_trade_pay": """
    INSERT OVERWRITE TABLE dwd_trade_pay_info PARTITION (ds='${bizdate}')
    SELECT order_id, buyer_id, pay_amount, pay_channel, pay_status
    FROM ods_trade_orders
    WHERE ds='${bizdate}' AND pay_status = 4
    """,
    "task_002_dws_pay_1d": """
    INSERT OVERWRITE TABLE dws_trade_pay_1d PARTITION (ds='${bizdate}')
    SELECT buyer_id,
           SUM(pay_amount) AS pay_amt_1d,
           COUNT(order_id) AS order_cnt_1d
    FROM dwd_trade_pay_info
    WHERE ds='${bizdate}'
    GROUP BY buyer_id
    """,
    "task_003_dws_pay_1d_pc": """
    INSERT OVERWRITE TABLE dws_trade_pay_pc_1d PARTITION (ds='${bizdate}')
    SELECT buyer_id,
           SUM(pay_amount) AS payment_amount_1d,
           COUNT(DISTINCT order_id) AS order_cnt_1d,
           CAST(SUM(pay_amount) / COUNT(DISTINCT buyer_id) AS DECIMAL(20,4)) AS arpu_1d
    FROM dwd_trade_pay_info
    WHERE ds='${bizdate}' AND pay_channel = 'pc'
    GROUP BY buyer_id
    """,
    "task_004_ads_dashboard": """
    INSERT OVERWRITE TABLE ads_trade_overview_1d PARTITION (ds='${bizdate}')
    SELECT 'all' AS scope,
           SUM(pay_amt_1d) AS total_pay_amt_1d,
           SUM(order_cnt_1d) AS total_order_cnt_1d
    FROM dws_trade_pay_1d
    WHERE ds='${bizdate}'
    """,
}

# 已登记指标（模拟 DataWorks 数据指标模块的存量登记，故意不含 arpu_1d → 孤儿）
REGISTERED = {"pay_amt_1d", "order_cnt_1d", "total_pay_amt_1d"}

# ---------------- P2 词根词典（OneData 命名拆解） ----------------

ATOMIC_TOKENS = {"amt": "金额", "amount": "金额", "cnt": "数量", "uv": "用户数", "arpu": "客单价"}
PERIOD_TOKENS = {"1d": "自然日", "7d": "近7天", "30d": "近30天", "wtd": "周累计", "mtd": "月累计"}
MODIFIER_TOKENS = {"pc": "PC端", "app": "APP端", "pty": "分终端"}

# ---------------- P1 聚合提取 ----------------


@dataclass
class CaliberCandidate:
    task: str
    sink: str                 # 目标表.字段
    agg: str                  # 聚合表达式
    dims: list[str]           # GROUP BY
    filters: list[str]        # WHERE 业务条件（剔除分区条件）
    name: str = ""            # 输出字段名
    parsed_name: dict = field(default_factory=dict)  # P2 命名拆解结果

    @property
    def fingerprint(self) -> str:
        """P3 口径指纹(派生层): 归一化(全聚合链+维度集+过滤集)"""
        return self._fp(with_filters=True)

    @property
    def atomic_fingerprint(self) -> str:
        """P3 原子层指纹: 忽略过滤集(修饰词) → 同原子不同修饰在此归并"""
        return self._fp(with_filters=False)

    def _fp(self, with_filters: bool) -> str:
        norm_agg = re.sub(r"\s+", "", self.agg.lower())
        parts = [norm_agg, ".".join(sorted(d.lower() for d in self.dims))]
        if with_filters:
            parts.append(".".join(sorted(f.lower() for f in self.filters)))
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def is_partition_cond(node: exp.Expression) -> bool:
    """剔除 ds='${bizdate}' 类分区条件，保留业务限定"""
    return isinstance(node.this, exp.Column) and node.this.name in ("ds", "pt", "dt")


def extract(corpora: dict[str, str]) -> list[CaliberCandidate]:
    out = []
    for task, sql in corpora.items():
        sql = re.sub(r"\$\{[^}]+\}", "20260905", sql)  # 调度参数占位替换（保留原有引号）
        try:
            tree = sqlglot.parse_one(sql, read=DIALECT)
        except sqlglot.errors.ParseError as e:
            print(f"[WARN] {task} 解析失败: {e}")
            continue
        sink_table = ""
        if isinstance(tree, exp.Insert):
            t = tree.find(exp.Table)
            sink_table = t.name if t else ""
        for sel in tree.find_all(exp.Select):
            dims = [d.alias_or_name for d in sel.args.get("group") or []]
            filters = []
            where = sel.args.get("where")
            if where is not None:
                for w in where.find_all(exp.EQ):
                    if not is_partition_cond(w):
                        filters.append(w.sql(dialect=DIALECT))
            for p in sel.selects:
                aggs = [a for a in p.find_all(exp.AggFunc)]
                if not aggs:
                    continue
                name = p.alias_or_name
                out.append(CaliberCandidate(
                    task=task, sink=f"{sink_table}.{name}",
                    agg=",".join(a.sql(dialect=DIALECT) for a in aggs),  # 全聚合链，复合指标不丢信息
                    dims=dims, filters=filters, name=name,
                ))
    return out


# ---------------- P2 命名拆解 ----------------

def parse_name(name: str) -> dict:
    tokens = re.split(r"[_]+", name.lower())
    return {
        "atomic": next((v for t, v in ATOMIC_TOKENS.items() if t in tokens), ""),
        "period": next((v for t, v in PERIOD_TOKENS.items() if t in tokens), ""),
        "modifier": [v for t, v in MODIFIER_TOKENS.items() if t in tokens],
    }


def corroborate(c: CaliberCandidate) -> float:
    """命名与 SQL 互相印证 → 置信度"""
    score = 0.5
    a = c.parsed_name["atomic"]
    if a == "金额" and ("sum(" in c.agg.lower()):
        score += 0.3
    if a == "数量" and ("count(" in c.agg.lower()):
        score += 0.3
    if c.parsed_name["period"]:
        score += 0.1
    return min(score, 1.0)


# ---------------- P3 指纹聚类 + 三类发现 ----------------

def analyze(cands: list[CaliberCandidate]):
    by_name, by_fp = {}, {}
    for c in cands:
        by_name.setdefault(c.name, []).append(c)
        by_fp.setdefault(c.fingerprint, []).append(c)

    conflicts, duplicates, orphans = [], [], []
    for name, group in by_name.items():
        fps = {c.fingerprint for c in group}
        if len(fps) > 1:
            conflicts.append((name, group))
    for fp, group in by_fp.items():
        names = {c.name for c in group}
        if len(names) > 1:
            duplicates.append((names, group))
    # 原子层归并: 同原子指纹不同名（修饰词差异属正常派生，但值得提示同源）
    by_atomic_fp = {}
    for c in cands:
        by_atomic_fp.setdefault(c.atomic_fingerprint, []).append(c)
    atomic_dupes = []
    for fp, group in by_atomic_fp.items():
        names = {c.name for c in group}
        if len(names) > 1 and fp not in {g[1][0].fingerprint for g in duplicates}:
            atomic_dupes.append((names, group))
    for c in cands:
        if c.name not in REGISTERED:
            orphans.append(c)
    return conflicts, duplicates, atomic_dupes, orphans


def main():
    cands = extract(CORPUS)
    for c in cands:
        c.parsed_name = parse_name(c.name)
    print(f"== P1 提取: {len(cands)} 个候选口径 ==\n")
    for c in cands:
        conf = corroborate(c)
        print(f"  {c.sink:42s} {c.agg:28s} dims={c.dims} filters={c.filters or '[]'} "
              f"原子={c.parsed_name['atomic']}-{c.parsed_name['period']} 置信={conf:.1f}")

    conflicts, duplicates, atomic_dupes, orphans = analyze(cands)
    print(f"\n== P3 发现 ==\n")
    print(f"❗ 口径冲突（同名不同指纹）: {len(conflicts)} 组")
    for name, group in conflicts:
        print(f"\n  指标 [{name}] 存在 {len(group)} 个口径:")
        for c in group:
            print(f"    - {c.task}: {c.agg}  filters={c.filters or '[]'}  fp={c.fingerprint}")
    print(f"\n🔁 重复建设（同指纹不同名）: {len(duplicates)} 组")
    for names, group in duplicates:
        print(f"  {sorted(names)} → 建议归并, 共 {len(group)} 处定义")
    print(f"\n🧬 同原子口径不同名（修饰词派生，提示同源）: {len(atomic_dupes)} 组")
    for names, group in atomic_dupes:
        print(f"  {sorted(names)} → 同原子口径 {group[0].agg}")
    print(f"\n🧙 孤儿口径（未登记）: {len(orphans)} 个")
    for c in orphans:
        print(f"  {c.sink}  ({c.agg})")


if __name__ == "__main__":
    main()
