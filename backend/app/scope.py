"""封账合计口径（close scope）的唯一事实来源。

整个系统只允许两种口径，且全局互斥：

* ``cash_only``     仅现金：系统合计只累加支付方式为现金的流水；
* ``all_methods``   全支付方式：系统合计累加该营业日全部有效流水。

本模块只放纯规则（常量、支付方式判定、入账判定），
数据库读写在各 service 中完成，避免仓储层与配置层互相依赖。
"""
from __future__ import annotations

from typing import Any

#: 现金支付方式在流水表中的字面值。
CASH_PAY_METHOD = "现金"

SCOPE_CASH_ONLY = "cash_only"
SCOPE_ALL_METHODS = "all_methods"

#: 新库 / 新配置的默认口径。封账是与实盘现金对账，默认按仅现金更稳妥。
DEFAULT_SCOPE = SCOPE_CASH_ONLY

SCOPES: tuple[str, ...] = (SCOPE_CASH_ONLY, SCOPE_ALL_METHODS)

SCOPE_LABELS: dict[str, str] = {
    SCOPE_CASH_ONLY: "仅现金",
    SCOPE_ALL_METHODS: "全支付方式",
}

SCOPE_DESCRIPTIONS: dict[str, str] = {
    SCOPE_CASH_ONLY: (
        "系统合计只累加支付方式为现金的有效流水；"
        "非现金流水仍保留可查，但不进入封账对照与现金长短款。"
    ),
    SCOPE_ALL_METHODS: (
        "系统合计累加该营业日全部有效流水；"
        "实盘现金与系统合计之差仍记长短款，批次同时按支付方式写出分项合计。"
    ),
}

#: 设置表中保存口径的键。
SETTING_KEY = "close_scope"

#: 人工输入时允许的别名，统一归一到 canonical slug。
_ALIASES: dict[str, str] = {
    SCOPE_CASH_ONLY: SCOPE_CASH_ONLY,
    "cash": SCOPE_CASH_ONLY,
    "cash-only": SCOPE_CASH_ONLY,
    "仅现金": SCOPE_CASH_ONLY,
    SCOPE_ALL_METHODS: SCOPE_ALL_METHODS,
    "all": SCOPE_ALL_METHODS,
    "all-methods": SCOPE_ALL_METHODS,
    "all_payments": SCOPE_ALL_METHODS,
    "全支付方式": SCOPE_ALL_METHODS,
    "全部支付方式": SCOPE_ALL_METHODS,
}


def is_valid(scope: str | None) -> bool:
    return scope in SCOPE_LABELS


def normalize(scope: str | None) -> str:
    """把库中/请求中的口径归一；无法识别时回落到默认口径。"""
    if scope is None:
        return DEFAULT_SCOPE
    return _ALIASES.get(str(scope).strip().lower(), DEFAULT_SCOPE)


def parse(scope: str | None) -> str:
    """严格解析外部输入；非法值抛 ``ValueError``（由 API 转成 400）。"""
    key = str(scope or "").strip().lower()
    if key not in _ALIASES:
        raise ValueError(f"未知封账口径：{scope!r}；只允许 {', '.join(SCOPES)}")
    return _ALIASES[key]


def label(scope: str) -> str:
    return SCOPE_LABELS.get(normalize(scope), SCOPE_LABELS[DEFAULT_SCOPE])


def is_cash(pay_method: str | None) -> bool:
    return pay_method == CASH_PAY_METHOD


def includes_method(scope: str, pay_method: str | None) -> bool:
    """该支付方式是否计入给定口径下的系统合计。"""
    if normalize(scope) == SCOPE_ALL_METHODS:
        return True
    return is_cash(pay_method)


def options() -> list[dict[str, Any]]:
    return [
        {
            "value": scope,
            "label": SCOPE_LABELS[scope],
            "description": SCOPE_DESCRIPTIONS[scope],
        }
        for scope in SCOPES
    ]
