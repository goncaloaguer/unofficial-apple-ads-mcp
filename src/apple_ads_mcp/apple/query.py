"""Request-body builders for the Platform API's four query families (PLAN.md §3.2).

(a) entity ``/query``            — QueryFilter{field, operator, value, ignoreCase}
                                   + QueryPagination{offset, pageSize, fetchTotalCount}
(b) reports & insights           — Filter{field, operator, value}
                                   + RequestPagination{offset, pageSize}
(c) recommendations/suggestions  — FilterCondition{field, operator, value: [..], ignoreCase}
                                   + pagination{offset, pageSize<=1000}
(d) change history               — AuditFilter + Pagination{offset, pageSize}
                                   + options{needTotals: "true"|"false", timeZone, metadata}

Filter vocabularies are curated per tool; these builders only shape validated
inputs. Nothing here touches the network.
"""
from __future__ import annotations

from typing import Any

# Enumerations mirror the SDK (QueryFilterOperator / report Filter validator).
ENTITY_OPERATORS = frozenset(
    {"EQUALS", "NOT_EQUALS", "IN", "NOT_IN", "LIKE", "NOT_LIKE", "STARTS_WITH",
     "ENDS_WITH", "GREATER_THAN", "GREATER_THAN_OR_EQUAL_TO", "LESS_THAN",
     "LESS_THAN_OR_EQUAL_TO", "BETWEEN", "IS_NULL", "IS_NOT_NULL",
     "CONTAINS_ANY", "CONTAINS_ALL", "NOT_CONTAINS_ANY", "NOT_CONTAINS_ALL"}
)
REPORT_OPERATORS = frozenset(
    {"EQUALS", "NOT_EQUALS", "IN", "LIKE", "STARTS_WITH", "ENDS_WITH", "CONTAINS",
     "CONTAINS_ANY", "CONTAINS_ALL", "GREATER_THAN", "GREATER_THAN_OR_EQUAL_TO",
     "LESS_THAN", "LESS_THAN_OR_EQUAL_TO", "BETWEEN"}
)


def _filters(filters: list[dict[str, Any]] | None, allowed_ops: frozenset[str]) -> list[dict]:
    out = []
    for f in filters or []:
        op = str(f["operator"]).upper()
        if op not in allowed_ops:
            raise ValueError(f"unsupported filter operator {op}")
        item: dict[str, Any] = {"field": f["field"], "operator": op}
        if op not in ("IS_NULL", "IS_NOT_NULL"):
            item["value"] = f["value"]
        if f.get("ignoreCase") is not None and allowed_ops is ENTITY_OPERATORS:
            item["ignoreCase"] = bool(f["ignoreCase"])
        out.append(item)
    return out


def entity_query(
    *,
    filters: list[dict[str, Any]] | None = None,
    sorting: list[dict[str, str]] | None = None,
    offset: int = 0,
    page_size: int = 100,
    fetch_total_count: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pagination": {"offset": offset, "pageSize": page_size, "fetchTotalCount": fetch_total_count}
    }
    if filters:
        body["filters"] = _filters(filters, ENTITY_OPERATORS)
    if sorting:
        body["sorting"] = [{"field": s["field"], "order": s.get("order", "ASC").upper()} for s in sorting]
    return body


def report_query(
    *,
    start: str,
    end: str,
    time_zone: str = "ORTZ",
    granularity: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    sorting: list[dict[str, str]] | None = None,
    group_by: list[str] | None = None,
    fields: list[str] | None = None,
    include_rows: list[str] | None = None,
    offset: int = 0,
    page_size: int = 100,
) -> dict[str, Any]:
    time_range: dict[str, Any] = {"start": start, "end": end, "timeZone": time_zone}
    if granularity:
        time_range["granularity"] = granularity
    body: dict[str, Any] = {
        "timeRange": time_range,
        "pagination": {"offset": offset, "pageSize": page_size},
    }
    if filters:
        body["filters"] = _filters(filters, REPORT_OPERATORS)
    if sorting:
        body["sorting"] = [{"field": s["field"], "order": s.get("order", "ASC").upper()} for s in sorting]
    if group_by:
        body["groupBy"] = list(group_by)
    if fields:
        body["fields"] = list(fields)
    if include_rows:
        body["options"] = {"includeRows": list(include_rows)}
    return body


def recommendation_query(
    *,
    filters: list[dict[str, Any]] | None = None,
    sorting: list[dict[str, str]] | None = None,
    offset: int = 0,
    page_size: int = 20,
) -> dict[str, Any]:
    body: dict[str, Any] = {"pagination": {"offset": offset, "pageSize": min(page_size, 1000)}}
    conditions = []
    for f in filters or []:
        value = f["value"]
        conditions.append(
            {
                "field": f["field"],
                "operator": str(f["operator"]).upper(),
                "value": value if isinstance(value, list) else [value],
            }
        )
    if conditions:
        body["filters"] = conditions
    if sorting:
        body["sorting"] = [{"field": s["field"], "order": s.get("order", "ASC").upper()} for s in sorting]
    return body


def audit_query(
    *,
    event_time_from: str,
    event_time_to: str,
    filters: list[dict[str, Any]] | None = None,
    time_zone: str = "UTC",
    metadata: str = "none",
    need_totals: bool = True,
    offset: int = 0,
    page_size: int = 100,
) -> dict[str, Any]:
    body_filters: list[dict[str, Any]] = [
        {"field": "eventTime", "operator": "BETWEEN", "value": [event_time_from, event_time_to]}
    ]
    for f in filters or []:
        body_filters.append({"field": f["field"], "operator": str(f["operator"]).upper(), "value": f["value"]})
    return {
        "filters": body_filters,
        "sorting": [{"field": "eventTime", "order": "DESC"}],
        "pagination": {"offset": offset, "pageSize": page_size},
        "options": {
            "needTotals": "true" if need_totals else "false",
            "timeZone": time_zone,
            "metadata": metadata,
        },
    }


def set_offset(body: dict[str, Any], offset: int, *, first_page: bool) -> dict[str, Any]:
    """Return a copy of ``body`` for the next page (full body re-sent, offset changed)."""
    page = dict(body)
    pagination = dict(page.get("pagination") or {})
    pagination["offset"] = offset
    if "fetchTotalCount" in pagination and not first_page:
        pagination["fetchTotalCount"] = False
    page["pagination"] = pagination
    if "options" in page and isinstance(page["options"], dict) and "needTotals" in page["options"] and not first_page:
        options = dict(page["options"])
        options["needTotals"] = "false"
        page["options"] = options
    return page
