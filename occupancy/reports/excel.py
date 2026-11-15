"""Weekly client report as an Excel workbook: the format hotel owners and
general managers actually open.

Sheets: Summary (KPIs and checks), Calendar (next 30 days with rates,
recommendations and competitor range, plus a chart), Requests (corporate
rate requests read from the inbox).
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..intel import kpis
from ..market.hotels import CLIENT

HEAD = PatternFill("solid", fgColor="1F3A4D")
WHITE = Font(color="FFFFFF", bold=True)


def _header(ws, row: int, values: list[str]) -> None:
    for i, v in enumerate(values, 1):
        c = ws.cell(row=row, column=i, value=v)
        c.fill, c.font = HEAD, WHITE
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build(conn, as_of: date, out_dir: str | None = None) -> Path:
    ov = kpis.overview(conn, as_of)
    cal = kpis.calendar(conn, as_of)
    checks = kpis.latest_checks(conn)
    reqs = kpis.requests_list(conn, 100)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"{CLIENT.name}: revenue report"
    ws["A1"].font = Font(size=14, bold=True)
    ws["A2"] = f"Business date {as_of:%d %b %Y}. Figures in INR."
    _header(ws, 4, ["Measure", "Last 28 days", "Next 30 days (on the books)"])
    last, nxt = ov["last_28_days"], ov["next_30_days_on_books"]
    for r, (label, key, fmt) in enumerate(
        (
            ("Occupancy", "occupancy", "0.0%"),
            ("ADR", "adr", "#,##0"),
            ("RevPAR", "revpar", "#,##0"),
            ("Rooms sold", "rooms_sold", "#,##0"),
            ("Room revenue", "revenue", "#,##0"),
        ),
        start=5,
    ):
        ws.cell(r, 1, label)
        for col, src in ((2, last), (3, nxt)):
            c = ws.cell(r, col, src[key])
            c.number_format = fmt
    ws["A11"], ws["B11"] = "Pickup, last 7 days (room nights)", ov["pickup_7_days"]
    ws["A12"], ws["B12"] = "Pace vs 4 weeks ago", ov["pace_vs_4_weeks_ago"]
    ws["B12"].number_format = "+0.0%;-0.0%"
    _header(ws, 14, ["Feed", "Check", "Status", "Value", "Detail"])
    colors = {"pass": "D9F2E3", "warn": "FFF1C7", "fail": "F8D3D0"}
    for r, c in enumerate(checks, start=15):
        for col, key in enumerate(("feed", "check_name", "status", "value", "detail"), 1):
            ws.cell(r, col, c[key])
        ws.cell(r, 3).fill = PatternFill("solid", fgColor=colors.get(c["status"], "FFFFFF"))
    _widths(ws, [34, 18, 26, 12, 70])

    cs = wb.create_sheet("Calendar")
    cols = [
        "Date",
        "Day",
        "Event",
        "On the books",
        "Occupancy OTB",
        "Forecast rooms",
        "Current rate",
        "Recommended",
        "Competitor median",
        "Competitor low",
        "Competitor high",
        "Status",
        "Why",
    ]
    _header(cs, 1, cols)
    for r, d in enumerate(cal, start=2):
        vals = [
            date.fromisoformat(d["date"]),
            d["weekday"],
            d["event"] or "",
            d["on_books"],
            d["occupancy_on_books"],
            d["forecast_rooms"],
            d["current_rate"],
            d["recommended_rate"],
            d["comp_median"],
            d["comp_low"],
            d["comp_high"],
            d["status"] or "",
            d["explanation"] or "",
        ]
        for col, v in enumerate(vals, 1):
            cs.cell(r, col, v)
        cs.cell(r, 1).number_format = "dd-mmm"
        cs.cell(r, 5).number_format = "0%"
        for col in (7, 8, 9, 10, 11):
            cs.cell(r, col).number_format = "#,##0"
    last_row = len(cal) + 1
    cs.conditional_formatting.add(
        f"E2:E{last_row}",
        ColorScaleRule(
            start_type="num", start_value=0, start_color="F8F1E5", end_type="num", end_value=1, end_color="2E7D6B"
        ),
    )
    _widths(cs, [10, 6, 24, 12, 12, 12, 12, 12, 14, 12, 12, 11, 90])
    cs.freeze_panes = "B2"

    line = LineChart()
    line.title, line.y_axis.title, line.height, line.width = "Rate vs competitors", "INR", 9, 22
    line.add_data(Reference(cs, min_col=7, max_col=9, min_row=1, max_row=last_row), titles_from_data=True)
    line.set_categories(Reference(cs, min_col=1, min_row=2, max_row=last_row))
    bar = BarChart()
    bar.add_data(Reference(cs, min_col=4, min_row=1, max_row=last_row), titles_from_data=True)
    bar.y_axis.axId, bar.y_axis.title = 200, "Rooms on the books"
    line.y_axis.crosses = "min"
    line += bar
    cs.add_chart(line, f"A{last_row + 3}")

    rq = wb.create_sheet("Requests")
    _header(
        rq,
        1,
        [
            "Received",
            "Company",
            "Room",
            "Check-in",
            "Check-out",
            "Rate",
            "Meal",
            "Rooms",
            "Read by",
            "Valid",
            "Original text",
        ],
    )
    for r, q in enumerate(reqs, start=2):
        f = q.get("fields") or {}
        vals = [
            q["received"],
            f.get("company"),
            f.get("room_type"),
            f.get("check_in"),
            f.get("check_out"),
            f.get("rate"),
            f.get("meal_plan"),
            f.get("rooms"),
            q["extractor"],
            "yes" if q["valid"] else "no",
            q["text"],
        ]
        for col, v in enumerate(vals, 1):
            rq.cell(r, col, v)
    _widths(rq, [11, 18, 11, 11, 11, 9, 7, 7, 9, 7, 100])

    out = Path(out_dir or tempfile.gettempdir()) / f"occupancy-report-{as_of:%Y-%m-%d}.xlsx"
    wb.save(out)
    return out
