"""Jinja filters and globals shared by every template."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from flask import Flask, request, url_for


def _number(value: Any, decimals: int = 0) -> str:
    """Format a number with thousands separators; blank for None."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _when(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime(fmt)
    return "" if value is None else str(value)


def _from_json(value: Any) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def url_with(**changes: Any) -> str:
    """The current URL with some query parameters changed.

    A value of None removes the parameter. Used by sort headers, pagination
    and "download these results" links so they keep every other filter.
    """
    pairs = [
        (key, value)
        for key, value in request.args.items(multi=True)
        if key not in changes and value != ""
    ]
    for key, value in changes.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, v) for v in value)
        else:
            pairs.append((key, value))
    query = urlencode(pairs)
    return request.path + (f"?{query}" if query else "")


def query_string(**changes: Any) -> str:
    """Just the query string of :func:`url_with`, without the path."""
    url = url_with(**changes)
    return url.split("?", 1)[1] if "?" in url else ""


def url_for_query(endpoint: str, **changes: Any) -> str:
    """Another endpoint carrying the current query string (with changes)."""
    query = query_string(**changes)
    return url_for(endpoint) + (f"?{query}" if query else "")


def sort_url(key: str) -> str:
    """Link for a sortable header: toggles direction if already sorted by key."""
    current = request.args.get("sort", "reporting_year")
    order = request.args.get("order", "desc")
    new_order = ("asc" if order == "desc" else "desc") if current == key else "desc"
    return url_with(sort=key, order=new_order, page=None)


def register_template_helpers(app: Flask) -> None:
    app.add_template_filter(_number, "num")
    app.add_template_filter(_when, "when")
    app.add_template_filter(_from_json, "fromjson")
    app.add_template_global(url_with)
    app.add_template_global(url_for_query)
    app.add_template_global(sort_url)
