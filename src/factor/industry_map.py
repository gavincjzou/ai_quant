"""阶段 9 V1 新增：Industry 因子映射

给不同行业的标的叠加 σ 级别 bias，反映 Gavin "AI 热点偏好"。

数据源优先级（Westock 返回中文 industry，FMP/硬编码 fallback 是英文）：
1. 优先用 Westock 的中文 industry 映射
2. 其次用英文 industry 映射（FMP 或硬编码）
3. 最后用 Symbol 硬编码 fallback（处理 SMCI/PLTR 这种 Westock 数据缺失的）

映射决策：
- 半导体核心：+1.0σ（NVDA/AMD/TSM/AVGO/MU/MRVL/QCOM/INTC）
- AI 基建硬件：+0.8σ（SMCI = Computer Hardware 但做 AI 服务器）
- 软件/互联网：+0.7σ（MSFT/GOOGL/META/ORCL/PLTR）
- 科技硬件消费：+0.3σ（AAPL Consumer Electronics）
- 娱乐/电商/其他：0σ（NFLX/AMZN/TSLA 等，虽然是科技但不属于 AI 核心赛道）
- 非科技：0σ（金融/医药/能源/消费/工业）
"""
from __future__ import annotations

from typing import Dict, Optional


# 中文 industry（来自 Westock）→ bias
INDUSTRY_BIAS_ZH: Dict[str, float] = {
    # 半导体核心
    "半导体": 1.0,
    # AI 基建硬件
    "电子零部件": 0.8,  # 有些数据源会这么归类
    # 软件/互联网
    "封装式软件": 0.7,           # ORCL / MSFT 在 Westock 下可能被归到这
    "互联网软件服务": 0.7,       # GOOGL/NFLX
    "软件-基础设施": 0.7,
    "软件开发": 0.7,
    # 数据分析/企业软件
    "专业软件": 0.7,
    # 消费电子 Apple 级
    "电信设备": 0.3,            # AAPL 在 Westock 归类
    # 零售/娱乐/消费 → 0
    "互联网零售": 0.0,          # AMZN
    "娱乐": 0.0,
    "餐饮": 0.0,
    "机动车": 0.0,              # TSLA
    # 其他非科技
    "大型银行": 0.0,
    "投资银行/经纪人": 0.0,
    "大型药物生产商": 0.0,
    "石油综合": 0.0,
    "建筑机械": 0.0,
    "信托/基金": 0.0,            # ETF（但 ETF 走特殊通道，这里不生效）
}

# 英文 industry（FMP profile 返回或硬编码）→ bias
# 实测校准：2026-04-25 通过 FMP /stable/profile 确认 35 只标的拼写
INDUSTRY_BIAS_EN: Dict[str, float] = {
    "Semiconductors": 1.0,
    "Computer Hardware": 0.8,          # SMCI = AI 服务器
    "Software - Infrastructure": 0.7,  # MSFT/ORCL/PLTR
    "Software - Application": 0.7,
    "Internet Content & Information": 0.7,  # GOOGL/META
    "Information Technology Services": 0.7,
    "Consumer Electronics": 0.3,       # AAPL
    # 明确 0σ
    "Specialty Retail": 0.0,           # AMZN
    "Entertainment": 0.0,              # NFLX
    "Auto Manufacturers": 0.0,         # TSLA
    "Banks - Diversified": 0.0,
    "Capital Markets": 0.0,
    "Drug Manufacturers - General": 0.0,
    "Oil & Gas Integrated": 0.0,
    "Restaurants": 0.0,
    "Discount Stores": 0.0,
    "Beverages - Non-Alcoholic": 0.0,
    "Credit Services": 0.0,            # V/MA
    "Farm & Heavy Construction Machinery": 0.0,  # CAT
    "Drug Manufacturers - Specialty & Generic": 0.0,
}

# Symbol 硬编码 fallback（当 Westock + FMP 都缺数据时）
# 覆盖所有 watchlist + V1 新增（2026-04-25）
SYMBOL_BIAS_FALLBACK: Dict[str, float] = {
    # 半导体核心（+1.0σ）
    "NVDA.US": 1.0, "AMD.US": 1.0, "TSM.US": 1.0, "AVGO.US": 1.0,
    "MU.US": 1.0, "MRVL.US": 1.0, "QCOM.US": 1.0, "INTC.US": 1.0,
    # AI 基建硬件（+0.8σ）
    "SMCI.US": 0.8,
    # 软件/互联网（+0.7σ）
    "MSFT.US": 0.7, "GOOGL.US": 0.7, "META.US": 0.7, "ORCL.US": 0.7,
    "PLTR.US": 0.7,
    # 消费电子（+0.3σ）
    "AAPL.US": 0.3,
    # 娱乐/电商（0σ）
    "NFLX.US": 0.0, "AMZN.US": 0.0, "TSLA.US": 0.0,
    # 非科技（0σ）
    "JPM.US": 0.0, "BAC.US": 0.0, "GS.US": 0.0, "V.US": 0.0,
    "JNJ.US": 0.0, "LLY.US": 0.0,
    "WMT.US": 0.0, "COST.US": 0.0, "KO.US": 0.0, "MCD.US": 0.0,
    "XOM.US": 0.0, "CAT.US": 0.0,
    "BABA.US": 0.0, "PDD.US": 0.0,
    # ETF（虽然因子流程会特殊处理，这里也给 0 防意外）
    "SPY.US": 0.0, "QQQ.US": 0.0, "IWM.US": 0.0,
}


def get_industry_bias(
    symbol: str,
    industry_zh: Optional[str] = None,
    industry_en: Optional[str] = None,
) -> float:
    """
    拿到某只标的的 Industry σ bias。

    查找顺序：
    1. industry_zh（Westock 返回）→ 中文映射表
    2. industry_en（FMP/硬编码）→ 英文映射表
    3. 按 symbol → SYMBOL_BIAS_FALLBACK 硬编码
    4. 都没有 → 默认 0.0（中性）

    Args:
        symbol: LongPort symbol（如 "NVDA.US"）
        industry_zh: Westock 的 industry 字段（中文）
        industry_en: FMP 或其他英文数据源的 industry 字段

    Returns:
        σ bias 值（-1.0 ~ +1.0 之间）
    """
    # 1. 中文
    if industry_zh and industry_zh.strip() and industry_zh != "-":
        if industry_zh in INDUSTRY_BIAS_ZH:
            return INDUSTRY_BIAS_ZH[industry_zh]
        # 模糊匹配（某些 Westock 返回可能有变体）
        for key, val in INDUSTRY_BIAS_ZH.items():
            if key in industry_zh or industry_zh in key:
                return val

    # 2. 英文
    if industry_en and industry_en.strip() and industry_en != "-":
        if industry_en in INDUSTRY_BIAS_EN:
            return INDUSTRY_BIAS_EN[industry_en]
        for key, val in INDUSTRY_BIAS_EN.items():
            if key.lower() in industry_en.lower() or industry_en.lower() in key.lower():
                return val

    # 3. Symbol 硬编码 fallback
    if symbol in SYMBOL_BIAS_FALLBACK:
        return SYMBOL_BIAS_FALLBACK[symbol]

    # 4. 都没命中
    return 0.0
