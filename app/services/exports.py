"""CSV and Excel download responses.

CSV is streamed row by row, so a download of the whole database never has to
fit in memory. Excel workbooks cannot be streamed and are built in memory,
which is fine at this project's scale (tens of thousands of rows).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Iterable, Iterator

from flask import Response, stream_with_context
from openpyxl import Workbook

FORMATS = {
    "csv": ("CSV", "text/csv; charset=utf-8"),
    "xlsx": (
        "Excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
}


def _cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _filename(stem: str, extension: str) -> str:
    return f"{stem}-{datetime.now():%Y%m%d-%H%M}.{extension}"


def _csv_stream(header: list[str], rows: Iterable[Iterable[Any]]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # The BOM lets Excel open the file as UTF-8 (facility names are not all ASCII).
    buffer.write("﻿")
    writer.writerow(header)
    for count, row in enumerate(rows, start=1):
        writer.writerow(["" if v is None else _cell(v) for v in row])
        if count % 500 == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


def tabular_response(
    header: list[str],
    rows: Iterable[Iterable[Any]],
    filename_stem: str,
    fmt: str = "csv",
) -> Response:
    """A download response for a header and rows, as CSV (default) or Excel."""
    if fmt == "xlsx":
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(title=filename_stem[:31] or "data")
        sheet.append(header)
        for row in rows:
            sheet.append([_cell(v) for v in row])
        output = io.BytesIO()
        workbook.save(output)
        return Response(
            output.getvalue(),
            mimetype=FORMATS["xlsx"][1],
            headers={
                "Content-Disposition": f'attachment; filename="{_filename(filename_stem, "xlsx")}"'
            },
        )

    return Response(
        stream_with_context(_csv_stream(header, rows)),
        mimetype=FORMATS["csv"][1],
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(filename_stem, "csv")}"'
        },
    )


def requested_format(value: str | None) -> str:
    value = (value or "csv").lower()
    return value if value in FORMATS else "csv"
