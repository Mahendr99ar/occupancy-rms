"""Weekly strategy deck (PowerPoint), five slides, generated from the same
numbers as the dashboard and the Excel report:

  1. Headline: last 28 days and the next 30 on the books
  2. Next 30 days: rooms on the books against current and competitor rates
  3. Where the hotel sits against its competitive set
  4. Decisions waiting for approval
  5. Data health and what to do about it
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.util import Inches, Pt

from ..intel import kpis
from ..market.hotels import CLIENT

INK = RGBColor(0x1F, 0x3A, 0x4D)
ACCENT = RGBColor(0xC2, 0x7C, 0x2C)
MUTED = RGBColor(0x6B, 0x7B, 0x86)


def _title(slide, text: str, sub: str = "") -> None:
    box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12.3), Inches(0.9)).text_frame
    box.text = text
    p = box.paragraphs[0]
    p.font.size, p.font.bold, p.font.color.rgb = Pt(28), True, INK
    if sub:
        q = box.add_paragraph()
        q.text = sub
        q.font.size, q.font.color.rgb = Pt(14), MUTED


def _kpi(slide, x: float, label: str, value: str, note: str) -> None:
    tf = slide.shapes.add_textbox(Inches(x), Inches(1.8), Inches(3.0), Inches(1.6)).text_frame
    tf.text = value
    tf.paragraphs[0].font.size, tf.paragraphs[0].font.bold = Pt(36), True
    tf.paragraphs[0].font.color.rgb = INK
    for text, size, color in ((label, 14, ACCENT), (note, 11, MUTED)):
        p = tf.add_paragraph()
        p.text, p.font.size, p.font.color.rgb = text, Pt(size), color


def _table(slide, rows: list[list[str]], top: float, widths: list[float]) -> None:
    shape = slide.shapes.add_table(
        len(rows), len(rows[0]), Inches(0.5), Inches(top), Inches(sum(widths)), Inches(0.3 * len(rows))
    )
    for i, w in enumerate(widths):
        shape.table.columns[i].width = Inches(w)
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            cell = shape.table.cell(r, c)
            cell.text = str(v)
            cell.text_frame.paragraphs[0].font.size = Pt(10 if r else 12)
            cell.text_frame.word_wrap = True


def build(conn, as_of: date, out_dir: str | None = None) -> Path:
    ov = kpis.overview(conn, as_of)
    cal = kpis.calendar(conn, as_of)
    checks = kpis.latest_checks(conn)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    s = prs.slides.add_slide(blank)
    _title(s, f"{CLIENT.name}: week of {as_of:%d %b %Y}", "Revenue summary from Occupancy")
    last, nxt = ov["last_28_days"], ov["next_30_days_on_books"]
    _kpi(
        s,
        0.5,
        "Occupancy, last 28 days",
        f"{last['occupancy']:.0%}",
        f"Next 30 days on the books: {nxt['occupancy']:.0%}",
    )
    _kpi(s, 3.7, "ADR, last 28 days", f"₹{last['adr']:,}", f"On the books ahead: ₹{nxt['adr']:,}")
    _kpi(s, 6.9, "RevPAR, last 28 days", f"₹{last['revpar']:,}", "Room revenue per available room")
    pace = ov["pace_vs_4_weeks_ago"]
    _kpi(
        s,
        10.1,
        "Pace vs 4 weeks ago",
        f"{pace:+.0%}" if pace is not None else "n/a",
        f"Pickup last 7 days: {ov['pickup_7_days']} room nights",
    )

    s = prs.slides.add_slide(blank)
    _title(s, "Next 30 days", "Rooms on the books (bars) against our rate and the competitor median (lines)")
    labels = [f"{d['date'][8:]} {d['weekday']}" for d in cal]
    cd = CategoryChartData()
    cd.categories = labels
    cd.add_series("Rooms on the books", [d["on_books"] for d in cal])
    chart = s.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1.4), Inches(12.3), Inches(2.6), cd
    ).chart
    chart.has_legend = False
    cd2 = CategoryChartData()
    cd2.categories = labels
    cd2.add_series("Our rate", [d["current_rate"] for d in cal])
    cd2.add_series("Competitor median", [d["comp_median"] or 0 for d in cal])
    lines = s.shapes.add_chart(
        XL_CHART_TYPE.LINE_MARKERS, Inches(0.5), Inches(4.1), Inches(12.3), Inches(3.0), cd2
    ).chart
    lines.has_legend, lines.legend.position, lines.legend.include_in_layout = True, XL_LEGEND_POSITION.TOP, False

    s = prs.slides.add_slide(blank)
    _title(s, "Position against the competitive set", "Our rate divided by the competitor median, per stay date")
    cd3 = CategoryChartData()
    cd3.categories = labels
    cd3.add_series(
        "Rate index", [round(d["current_rate"] / d["comp_median"], 2) if d["comp_median"] else 0 for d in cal]
    )
    s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1.4), Inches(12.3), Inches(5.5), cd3)

    s = prs.slides.add_slide(blank)
    pending = [d for d in cal if d["status"] == "pending"][:10]
    _title(
        s,
        f"{len([d for d in cal if d['status'] == 'pending'])} rate changes waiting for approval",
        "Largest moves first is how to review them; each row says why",
    )
    rows = [["Date", "Now", "Recommended", "Competitors", "Why"]]
    for d in sorted(pending, key=lambda d: -abs(d["recommended_rate"] - d["current_rate"])):
        rows.append(
            [
                f"{d['date']} {d['weekday']}",
                f"₹{d['current_rate']:,}",
                f"₹{d['recommended_rate']:,}",
                f"₹{d['comp_median']:,}" if d["comp_median"] else "-",
                d["explanation"] or "",
            ]
        )
    _table(s, rows, 1.5, [1.6, 1.0, 1.3, 1.3, 7.1])

    s = prs.slides.add_slide(blank)
    bad = [c for c in checks if c["status"] != "pass"]
    _title(s, "Data health", f"{len(checks) - len(bad)} of {len(checks)} checks passed on the latest run")
    rows = [["Feed", "Check", "Status", "Detail"]] + [
        [c["feed"], c["check_name"], c["status"], (c["detail"] or "")[:90]] for c in (bad or checks)[:12]
    ]
    _table(s, rows, 1.5, [2.0, 2.2, 1.0, 7.1])

    out = Path(out_dir or tempfile.gettempdir()) / f"occupancy-deck-{as_of:%Y-%m-%d}.pptx"
    prs.save(out)
    return out
