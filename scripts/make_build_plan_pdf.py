"""Builds docs/ops-brain-build-plan.pdf: the Ops Brain build plan, 7 spaced-out A4 pages.

  python scripts/make_build_plan_pdf.py

Pages: 1 summary and v1 | 2 architecture | 3 data model (Notion) | 4-5 data model (Postgres)
       6 buy vs build and failure handling | 7 running cost and next steps
"""
import math
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

OUT = Path(__file__).resolve().parents[1] / "docs" / "ops-brain-build-plan.pdf"
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
pdfmetrics.registerFont(TTFont("Body", str(FONT_DIR / "DejaVuSansCondensed.ttf")))
pdfmetrics.registerFont(TTFont("Body-Bold", str(FONT_DIR / "DejaVuSansCondensed-Bold.ttf")))
pdfmetrics.registerFont(TTFont("Body-Italic", str(FONT_DIR / "DejaVuSansCondensed-Oblique.ttf")))
pdfmetrics.registerFont(TTFont("Mono", str(FONT_DIR / "DejaVuSansMono.ttf")))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-Bold")

W, H = A4
M = 42                      # side margin
CW = W - 2 * M              # content width
TOP = H - 112               # first content line below the page header
TOTAL_PAGES = 7

INK = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#667085")
ACCENT = colors.HexColor("#1d4ed8")
LINE = colors.HexColor("#d5dbe5")
PAPER = colors.HexColor("#f7f9fc")
CODE, CODE_S = colors.HexColor("#e3ebf7"), colors.HexColor("#6c8bbf")       # plain tested code
LLM, LLM_S = colors.HexColor("#fde9c2"), colors.HexColor("#d49a2a")         # language-model work
EXT, EXT_S = colors.HexColor("#e2f2e7"), colors.HexColor("#5fa271")         # external services
AUDIT, AUDIT_S = colors.HexColor("#efe3fb"), colors.HexColor("#8b5cf6")     # audit and cost log
INPUT_C = colors.HexColor("#2f6fd6")                                        # a person fills it in
OUTPUT_C = colors.HexColor("#2e9b5b")                                       # Ops Brain writes it back
OLD_C = colors.HexColor("#a3acb9")                                          # existing, not touched
NOTE_BG = colors.HexColor("#fff8e8")

S = {
    "body": ParagraphStyle("body", fontName="Body", fontSize=9.6, leading=14.2, textColor=INK),
    "small": ParagraphStyle("small", fontName="Body", fontSize=8.6, leading=12.4, textColor=INK),
    "bullet": ParagraphStyle("bullet", fontName="Body", fontSize=9.4, leading=13.6, textColor=INK,
                             leftIndent=11, bulletIndent=1),
    "cell": ParagraphStyle("cell", fontName="Body", fontSize=8.6, leading=12, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Body-Bold", fontSize=8.6, leading=12, textColor=INK),
}


# ----- drawing helpers ----------------------------------------------------------------------
def para(c, text, x, ytop, w, style="body") -> float:
    p = Paragraph(text, S[style])
    _, h = p.wrap(w, 10_000)
    p.drawOn(c, x, ytop - h)
    return h


def bullets(c, items, x, ytop, w, gap=5) -> float:
    y = ytop
    for t in items:
        p = Paragraph(t, S["bullet"], bulletText="•")
        _, h = p.wrap(w, 10_000)
        p.drawOn(c, x, y - h)
        y -= h + gap
    return ytop - y


def heading(c, text, x, y, size=13, color=ACCENT) -> float:
    c.setFillColor(color)
    c.setFont("Body-Bold", size)
    c.drawString(x, y - size, text)
    return size + 8


def page_chrome(c, n, title, subtitle):
    c.setFillColor(INK)
    c.rect(0, H - 40, W, 40, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Body-Bold", 9)
    c.drawString(M, H - 25, "OPS BRAIN  ·  BUILD PLAN")
    c.setFont("Body", 8.6)
    c.drawRightString(W - M, H - 25, "PRD 1  ·  19 Nov 2026")
    c.setFillColor(INK)
    c.setFont("Body-Bold", 22)
    c.drawString(M, H - 76, title)
    c.setFillColor(MUTED)
    c.setFont("Body", 10)
    c.drawString(M, H - 92, subtitle)
    c.setFont("Body", 8.2)
    c.drawCentredString(W / 2, 30, f"{n} / {TOTAL_PAGES}")
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(M, 44, W - M, 44)


def box(c, x, ybot, w, h, lines, fill, stroke, fs=8.4, bold_first=True, r=5):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.9)
    c.roundRect(x, ybot, w, h, r, fill=1, stroke=1)
    c.setFillColor(INK)
    lead = fs * 1.28
    y = ybot + h / 2 + (len(lines) - 1) * lead / 2 - fs * 0.33
    for i, ln in enumerate(lines):
        c.setFont("Body-Bold" if (i == 0 and bold_first) else "Body", fs)
        c.drawCentredString(x + w / 2, y - i * lead, ln)


def arrow(c, x1, y1, x2, y2, both=False, color=MUTED, width=1.1):
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    c.line(x1, y1, x2, y2)
    ang = math.atan2(y2 - y1, x2 - x1)

    def head(x, y, a):
        s = 4.4
        p = c.beginPath()
        p.moveTo(x, y)
        p.lineTo(x - s * math.cos(a - 0.45), y - s * math.sin(a - 0.45))
        p.lineTo(x - s * math.cos(a + 0.45), y - s * math.sin(a + 0.45))
        p.close()
        c.drawPath(p, fill=1, stroke=0)
    head(x2, y2, ang)
    if both:
        head(x1, y1, ang + math.pi)


def elbow(c, pts, color=ACCENT, width=1.5, head=True):
    """Polyline connector; arrowhead on the final segment."""
    c.setStrokeColor(color)
    c.setLineWidth(width)
    for (xa, ya), (xb, yb) in zip(pts[:-2], pts[1:-1]):
        c.line(xa, ya, xb, yb)
    (xa, ya), (xb, yb) = pts[-2], pts[-1]
    if head:
        arrow(c, xa, ya, xb, yb, color=color, width=width)
    else:
        c.line(xa, ya, xb, yb)


def label(c, x, y, text, fs=7.8, color=MUTED):
    c.setFillColor(color)
    c.setFont("Body-Italic", fs)
    c.drawString(x, y, text)


def card(c, x, ytop, w, h, fill=PAPER, stroke=LINE):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.8)
    c.roundRect(x, ytop - h, w, h, 6, fill=1, stroke=1)


def legend(c, x, y, items, gap=118):
    for i, (f, s_, t) in enumerate(items):
        c.setFillColor(f)
        c.setStrokeColor(s_)
        c.setLineWidth(0.8)
        c.rect(x + i * gap, y, 10, 7, fill=1, stroke=1)
        c.setFillColor(MUTED)
        c.setFont("Body", 8)
        c.drawString(x + i * gap + 14, y + 0.8, t)


def table(c, rows, col_w, x, ytop, extra_styles=None) -> float:
    data = [[Paragraph(str(v), S["cellb"] if ri == 0 else S["cell"]) for v in r] for ri, r in enumerate(rows)]
    t = Table(data, colWidths=col_w)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
          ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
          ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6ebf3"))]
    st += [("BACKGROUND", (0, i), (-1, i), PAPER) for i in range(2, len(rows), 2)]
    t.setStyle(TableStyle(st + (extra_styles or [])))
    _, h = t.wrap(sum(col_w), 10_000)
    t.drawOn(c, x, ytop - h)
    return h


# ----- page 1: summary ---------------------------------------------------------------------
def page1(c):
    page_chrome(c, 1, "What ships first, and why",
                "Turn meeting decisions into owned, dated, linked work, and catch slips before they break a goal.")
    y = TOP - 4
    y -= heading(c, "My v1: one loop, end to end", M, y)
    y -= para(c, "A meeting note becomes <i>proposed</i> tasks with an owner, a date, a goal and dependencies. A person approves them in "
                 "Slack and they land in Notion. When any task's date later slips, <b>every downstream owner gets a Slack alert</b> "
                 "that names the cause and the goal at risk. Nothing is ever changed automatically.", M, y, CW) + 16

    steps = [("Meeting note", "English, Hindi or\nGujarati, in Notion", CODE, CODE_S),
             ("AI proposes", "Reads the note. Flags\nanything unsure", LLM, LLM_S),
             ("Person approves", "In Slack. Nothing is\ncreated before this", EXT, EXT_S),
             ("Tasks in Notion", "Owner, date, goal,\nwhat they depend on", EXT, EXT_S),
             ("A date slips", "Everyone downstream\nis told, with the cause", CODE, CODE_S)]
    bw, bh, gap = 88, 50, 18.75
    fy = y - bh
    for i, (t, sub, f, st_) in enumerate(steps):
        x = M + i * (bw + gap)
        box(c, x, fy, bw, bh, [t], f, st_, fs=8.8)
        for j, ln in enumerate(sub.split("\n")):
            c.setFillColor(MUTED)
            c.setFont("Body", 7.9)
            c.drawCentredString(x + bw / 2, fy - 14 - j * 10.5, ln)
        if i < 4:
            arrow(c, x + bw + 2, fy + bh / 2, x + bw + gap - 2, fy + bh / 2, color=INK, width=1.3)
    y = fy - 52
    legend(c, M, y, [(CODE, CODE_S, "plain tested code"), (LLM, LLM_S, "language model (reading only)"),
                     (EXT, EXT_S, "people and tools")], gap=160)
    y -= 36

    y -= heading(c, "Why this is first", M, y)
    y -= bullets(c, [
        "<b>It is the trial's Done test.</b> A slip on a chain of 4+ tasks must reach every affected person within 15 minutes. "
        "Measured on the demo chain: about 16 seconds, including the Notion round trips.",
        "<b>It is also the take-home demo:</b> one meeting note turned into structured tasks with owners, dates, goal links and dependencies.",
        "<b>It builds the spine everything else reuses:</b> the Notion sync, the dependency graph, the approval gate and Slack delivery.",
    ], M, y, CW, gap=8) + 16

    y -= heading(c, "Roadmap", M, y)
    cols = [("v1  ·  built", ["Notion sync (poll every 5 min)", "Meeting note → proposals", "Approval in Slack",
                              "Dependency graph, slip alerts", "Alerts that survive a crash", "Priorities with a “why”",
                              "Follow-up ladder, goal health"]),
            ("Next", ["Always-on hosting", "Notion webhooks (instant sync)", "Page someone if an alert gives up",
                      "Labelled notes to measure the 80% target", "Holiday calendar"]),
            ("Later", ["Recurring tasks handed to AI agents", "Learning from approval edits",
                       "More languages and time zones", "History-based estimates"])]
    cw3 = (CW - 24) / 3
    ch = 152
    for i, (t, items) in enumerate(cols):
        x = M + i * (cw3 + 12)
        card(c, x, y, cw3, ch)
        c.setFillColor(ACCENT if i == 0 else INK)
        c.setFont("Body-Bold", 9.8)
        c.drawString(x + 10, y - 19, t)
        bullets(c, items, x + 10, y - 30, cw3 - 18, gap=4)
    y -= ch + 22

    card(c, M, y, CW, 78, fill=NOTE_BG, stroke=LLM_S)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9.4)
    c.drawString(M + 12, y - 19, "Honest status")
    para(c, "Everything marked <b>built</b> is tested (111 automated tests). The Slack buttons and the meeting-approval step have been tested "
            "with fakes and a real Slack message path, but not yet clicked by a person against real Notion. Nothing is hosted yet: "
            "it runs only while started.", M + 12, y - 26, CW - 24, "small")


# ----- page 2: architecture ----------------------------------------------------------------
def page2(c):
    page_chrome(c, 2, "Architecture", "Notion is the record, Slack is how people act, one small service does the thinking.")
    x0 = M
    bw = (CW - 70) / 2
    y = TOP - 6
    box(c, x0, y - 54, bw, 54, ["Notion", "system of record + main interface", "Tasks Tracker · Projects (goals) · Meeting Notes"],
        EXT, EXT_S, fs=8.6)
    box(c, x0 + bw + 70, y - 54, bw, 54, ["Slack", "alerts and one-tap actions", "DMs · alerts channel · buttons · /ops"],
        EXT, EXT_S, fs=8.6)
    core_top = y - 54 - 44
    ax1, ax2 = x0 + bw / 2, x0 + bw + 70 + bw / 2
    arrow(c, ax1, y - 54, ax1, core_top, both=True)
    arrow(c, ax2, y - 54, ax2, core_top, both=True)
    label(c, ax1 + 9, y - 54 - 19, "reads tasks, writes back")
    label(c, ax1 + 9, y - 54 - 30, "priority · risk · finish date")
    label(c, ax2 + 9, y - 54 - 19, "alerts and cards out,")
    label(c, ax2 + 9, y - 54 - 30, "button clicks in")

    core_h = 212
    cy = core_top - core_h
    c.setFillColor(colors.white)
    c.setStrokeColor(INK)
    c.setLineWidth(1.2)
    c.roundRect(x0, cy, CW, core_h, 6, fill=1, stroke=1)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9)
    c.drawString(x0 + 10, core_top - 18, "CORE SERVICE")
    c.setFont("Body", 8.4)
    c.setFillColor(MUTED)
    c.drawString(x0 + 92, core_top - 18, "one container  ·  Python, FastAPI, job queue and worker  ·  runs on any always-on host")
    mw, mh, gap = (CW - 20 - 3 * 12) / 4, 44, 12
    rows = [
        [("Notion sync", "poll every 5 min", False), ("Dependency graph", "slack · risk · goal health", False),
         ("Priority engine", "P0–P3 bands + “why”", False), ("Slip alerts", "one message per person", False)],
        [("Task extractor", "note → proposals", True), ("Approval gate", "person confirms", False),
         ("Follow-up scheduler", "working hours · ladder", False), ("Slack handlers", "Done · On track · Slipping", False)],
    ]
    ry = core_top - 32 - mh
    for r in rows:
        for i, (t, sub, is_llm) in enumerate(r):
            mx = x0 + 10 + i * (mw + gap)
            box(c, mx, ry, mw, mh, [t, sub], LLM if is_llm else CODE, LLM_S if is_llm else CODE_S, fs=8.2)
            if i < 3:
                arrow(c, mx + mw + 1, ry + mh / 2, mx + mw + gap - 1, ry + mh / 2, color=INK)
        ry -= mh + 12
    iw = (CW - 20 - 2 * 12) / 3
    for i, (t, sub, is_llm) in enumerate([("Web API", "health · webhook · admin", False),
                                          ("Job queue + worker", "queue · schedules · resend sweeper", False),
                                          ("Model gateway", "LiteLLM: checks, retries, fail-over", True)]):
        box(c, x0 + 10 + i * (iw + 12), ry - 2, iw, 36, [t, sub], LLM if is_llm else CODE, LLM_S if is_llm else CODE_S, fs=8.2)

    by = cy - 54 - 46
    box(c, x0, by, bw, 54, ["Neon Postgres", "cache of Notion + audit log + job queue", "managed, not self-run"], EXT, EXT_S, fs=8.6)
    box(c, x0 + bw + 70, by, bw, 54, ["Model API provider (today: Groq)", "open-weight models: Qwen, gpt-oss",
                                      "called over the internet; we run none"], LLM, LLM_S, fs=8.6)
    arrow(c, ax1, cy, ax1, by + 54, both=True)
    arrow(c, ax2, cy, ax2, by + 54, both=True)
    label(c, ax1 + 9, cy - 21, "state, de-duplication keys,")
    label(c, ax1 + 9, cy - 32, "audit trail")
    label(c, ax2 + 9, cy - 21, "API calls via the gateway:")
    label(c, ax2 + 9, cy - 32, "JSON checked, retried")

    y = by - 24
    legend(c, M, y, [(CODE, CODE_S, "plain tested code"), (LLM, LLM_S, "language-model work"), (EXT, EXT_S, "external service")], gap=140)
    y -= 28

    card(c, M, y, CW, 78, fill=NOTE_BG, stroke=LLM_S)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9.2)
    c.drawString(M + 12, y - 18, "About the models")
    para(c, "Every model call goes through one gateway (LiteLLM) to a <b>third-party model API</b>; today that is Groq's free tier. The models are "
            "open-weight (Qwen, gpt-oss) but <b>we host none of them</b>: no GPU, no local model. Changing provider or model is a config edit. "
            "Free tiers may log prompts, so real founder content waits for a paid zero-retention provider.",
         M + 12, y - 25, CW - 24, "small")
    y -= 78 + 20
    para(c, "<b>How one slip travels:</b> a person edits a due date in Notion → the 5-minute poll sees it → the dependency graph recomputes "
            "every downstream task → one grouped Slack message per affected person (saved together with the slip, so a crash cannot lose it) → "
            "risk, priority and goal health are written back to Notion.",
         M, y, CW, "small")


# ----- page 3: Notion data model -----------------------------------------------------------
def field_card(c, x, ytop, w, title, sub, fields, row_h=19):
    """Database card. fields: (kind, name, detail). kind: in | out | old. Returns (height, {name: y_centre})."""
    head_h = 36
    h = head_h + row_h * len(fields) + 6
    c.setFillColor(colors.white)
    c.setStrokeColor(EXT_S)
    c.setLineWidth(1.1)
    c.roundRect(x, ytop - h, w, h, 6, fill=1, stroke=1)
    c.setFillColor(EXT)
    c.roundRect(x, ytop - head_h, w, head_h, 6, fill=1, stroke=0)
    c.rect(x, ytop - head_h, w, 9, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 10.4)
    c.drawString(x + 10, ytop - 16, title)
    c.setFillColor(MUTED)
    c.setFont("Body", 7.8)
    c.drawString(x + 10, ytop - 28, sub)
    anchors = {}
    for i, (kind, name, detail) in enumerate(fields):
        yy = ytop - head_h - i * row_h
        if i % 2 == 1:
            c.setFillColor(PAPER)
            c.rect(x + 1, yy - row_h, w - 2, row_h, fill=1, stroke=0)
        c.setFillColor({"in": INPUT_C, "out": OUTPUT_C, "old": OLD_C}[kind])
        c.circle(x + 12, yy - row_h / 2, 3.2, fill=1, stroke=0)
        c.setFillColor(INK if kind != "old" else MUTED)
        c.setFont("Body-Bold" if kind != "old" else "Body", 8.6)
        c.drawString(x + 22, yy - row_h / 2 - 3, name)
        c.setFillColor(MUTED)
        c.setFont("Body", 7.9)
        c.drawRightString(x + w - 8, yy - row_h / 2 - 2.8, detail)
        anchors[name] = yy - row_h / 2
    return h, anchors


def page3(c):
    page_chrome(c, 3, "Data model: Notion", "What people see and edit. Notion is the system of record.")
    y = TOP - 4
    legend(c, M, y - 8, [(INPUT_C, INPUT_C, "a person fills it in"), (OUTPUT_C, OUTPUT_C, "Ops Brain writes it back"),
                         (OLD_C, OLD_C, "already there, left alone")], gap=160)
    y -= 40
    tw, gw = 290, 180
    gx = M + CW - gw
    th, ta = field_card(c, M, y, tw, "Tasks Tracker", "one row per task", [
        ("in", "Task name", "title"), ("in", "Assignee", "Notion user"), ("in", "Owner (name)", "text, when no Notion user"),
        ("in", "Due date", "date"), ("in", "Status", "Not started · In progress · Blocked · Done"),
        ("in", "Estimate (days)", "number"), ("in", "Goal", "→ Projects"), ("in", "Depends on", "→ Tasks Tracker  ↻"),
        ("out", "Ops priority", "P0 · P1 · P2 · P3"), ("out", "Rank reason", "plain-language why"),
        ("out", "Risk", "On track · At risk · Off track"), ("out", "Projected finish", "date"),
        ("old", "Priority", "High · Medium · Low"), ("old", "Effort level", "select"), ("old", "Task type", "multi-select"),
        ("old", "Description", "also notes how an approved task began"),
    ], row_h=20)
    pj_h, pa = field_card(c, gx, y, gw, "Projects  (= goals)", "one row per company goal", [
        ("in", "Name", "title"), ("in", "Weight", "1 to 5"), ("in", "Target date", "date"),
        ("out", "Health", "On / At risk / Off track"), ("out", "Health reason", "text"),
        ("old", "Status", "select"), ("old", "Date Created", "auto"),
    ], row_h=20)
    field_card(c, gx, y - pj_h - 40, gw, "Meeting Notes", "the source for new tasks", [
        ("old", "Meeting name", "title"), ("old", "Date", "date"), ("old", "Attendees", "Notion users"),
        ("old", "Page text", "read-only, by the extractor"),
    ], row_h=20)
    # Goal relation: elbow from Tasks.Goal to Projects.Name
    mid = M + tw + (gx - M - tw) / 2
    elbow(c, [(M + tw, ta["Goal"]), (mid, ta["Goal"]), (mid, pa["Name"]), (gx, pa["Name"])])

    ny = y - th - 28
    card(c, M, ny, CW, 160, fill=PAPER)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9.6)
    c.drawString(M + 12, ny - 19, "How to read this")
    bullets(c, [
        "<b>Depends on</b> points back at the same database (a chain: spec → backend → QA → launch). Notion adds the reverse column, <b>Blocks</b>, itself.",
        "<b>A task has one owner:</b> the Assignee if set, otherwise the text in Owner (name). That lets people who are not Notion users still own work.",
        "<b>The green fields are the only ones Ops Brain writes on its own.</b> It never edits dates, owners, status or your own Priority column. "
        "Those change only when a person clicks Done, or a lead approves a new date.",
        "<b>Projects become goals</b> once they have a Weight and a Target date. A project with no tasks shows “no tasks linked yet”, not “on track”.",
    ], M + 12, ny - 28, CW - 24, gap=6)


# ----- page 4: Postgres data model ---------------------------------------------------------
def table_card(c, x, ytop, w, name, cols, fill=CODE, stroke=CODE_S, note=None):
    lh = 11.4
    h = 22 + lh * len(cols) + (14 if note else 0) + 6
    c.setFillColor(colors.white)
    c.setStrokeColor(stroke)
    c.setLineWidth(1)
    c.roundRect(x, ytop - h, w, h, 5, fill=1, stroke=1)
    c.setFillColor(fill)
    c.roundRect(x, ytop - 19, w, 19, 5, fill=1, stroke=0)
    c.rect(x, ytop - 19, w, 7, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9)
    c.drawString(x + 8, ytop - 13.5, name)
    for i, (col, kind) in enumerate(cols):
        yy = ytop - 19 - 4 - i * lh - 8
        c.setFont("Mono", 7.1)
        c.setFillColor(ACCENT if kind == "fk" else INK)
        c.drawString(x + 8, yy, col)
        tag = {"pk": ("PK", colors.HexColor("#b45309")), "fk": ("FK", ACCENT), "uq": ("UNIQUE", colors.HexColor("#7c3aed"))}.get(kind)
        if tag:
            c.setFillColor(tag[1])
            c.setFont("Body-Bold", 6.8)
            c.drawRightString(x + w - 8, yy, tag[0])
    if note:
        c.setFillColor(MUTED)
        c.setFont("Body-Italic", 7.3)
        c.drawString(x + 8, ytop - h + 7, note)
    return h


def row_y(ytop, i, lh=11.4):
    """y of the visual centre of column row i inside a table_card that starts at ytop."""
    return ytop - 19 - 4 - i * lh - 8 + 2.6


def page4(c):
    page_chrome(c, 4, "Data model: Postgres (1 of 2)", "A cache of Notion plus a log. If it were lost, only history would be lost.")
    cw3 = 146
    gap = (CW - 3 * cw3) / 2
    xs = [M + i * (cw3 + gap) for i in range(3)]

    def zone(text, ytop):
        c.setFillColor(MUTED)
        c.setFont("Body-Bold", 8.4)
        c.drawString(M, ytop - 9, text.upper())

    y = TOP - 2
    zone("Cache of Notion (rebuilt by the sync)", y)
    y1 = y - 22
    hg = table_card(c, xs[0], y1, cw3, "goals", [("id", "pk"), ("name", ""), ("weight  1..5", ""), ("target_date", "")])
    ht = table_card(c, xs[1], y1, cw3, "tasks", [("id", "pk"), ("title", ""), ("owner", ""), ("due", ""), ("estimate_days", ""),
                                                  ("status", ""), ("goal_id", "fk"), ("last_update", "")])
    he = table_card(c, xs[2], y1, cw3, "edges", [("task_id", "fk"), ("depends_on_id", "fk")], note="PK = both columns")
    gm = xs[1] - gap / 2
    elbow(c, [(xs[1], row_y(y1, 6)), (gm, row_y(y1, 6)), (gm, row_y(y1, 0)), (xs[0] + cw3, row_y(y1, 0))])
    em = xs[2] - gap / 2
    elbow(c, [(xs[2], row_y(y1, 1)), (em, row_y(y1, 1)), (em, row_y(y1, 0))], head=False)
    arrow(c, xs[2], row_y(y1, 0), xs[1] + cw3, row_y(y1, 0), color=ACCENT, width=1.5)

    y = y1 - max(hg, ht, he) - 64
    zone("People, rankings and risk", y)
    y2 = y - 22
    h1 = table_card(c, xs[0], y2, cw3, "people", [("id", "pk"), ("name", "uq"), ("aliases  text[]", ""), ("email", ""), ("slack_user_id", ""),
                                                   ("is_lead", ""), ("backup_id → people", "fk")])
    h2 = table_card(c, xs[1], y2, cw3, "priority_snapshots", [("day", "pk"), ("task_id", "pk"), ("score", ""), ("band  P0..P3", ""),
                                                              ("fingerprint", ""), ("why", "")])
    h3 = table_card(c, xs[2], y2, cw3, "risk_events", [("id", "pk"), ("task_id", ""), ("cause_task_id", ""), ("old_risk, new_risk", ""),
                                                        ("delay_days, slack_days", ""), ("notified_at", "")])
    y = y2 - max(h1, h2, h3) - 58
    card(c, M, y, CW, 118, fill=PAPER)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9.4)
    c.drawString(M + 12, y - 19, "Reading the diagram")
    bullets(c, [
        "<b>Blue arrows are real foreign keys:</b> tasks → goals, and both edges columns → tasks.",
        "<b>Other task ids are plain references</b> (for example risk_events.task_id), so history survives when a Notion page is archived.",
        "<b>Notion stays the truth.</b> goals, tasks and edges are rebuilt from it on every sync.",
    ], M + 12, y - 28, CW - 24, gap=5)


def page5(c):
    page_chrome(c, 5, "Data model: Postgres (2 of 2)", "Approvals, follow-ups, alert delivery, duplicate protection and the audit trail.")
    cw3 = 146
    gap = (CW - 3 * cw3) / 2
    xs = [M + i * (cw3 + gap) for i in range(3)]

    def zone(text, ytop):
        c.setFillColor(MUTED)
        c.setFont("Body-Bold", 8.4)
        c.drawString(M, ytop - 9, text.upper())

    y = TOP - 2
    zone("Approvals, follow-ups and alert delivery", y)
    y1 = y - 22
    h1 = table_card(c, xs[0], y1, cw3, "proposals", [("id", "pk"), ("meeting_id, local_id", "uq"), ("payload  json", ""), ("status", ""),
                                                      ("decided_by, decided_at", ""), ("card_sent_at", "")],
                    note="also holds date-change requests")
    h2 = table_card(c, xs[1], y1, cw3, "followups", [("id", "pk"), ("task_id", ""), ("kind, step", ""), ("dedupe_key", "uq"),
                                                      ("due_at, sent_at", ""), ("recipient_role", ""), ("outcome", "")])
    h3 = table_card(c, xs[2], y1, cw3, "alert_outbox", [("id", "pk"), ("owner, severity, kind", ""), ("text", ""), ("task_ids, items", ""),
                                                         ("attempts, claimed_at", ""), ("sent_at", ""), ("last_error", "")],
                    fill=NOTE_BG, stroke=LLM_S, note="saved WITH the slip; resent if unsent")
    y = y1 - max(h1, h2, h3) - 38
    zone("Duplicate protection and write-back bookkeeping", y)
    y2 = y - 22
    h1 = table_card(c, xs[0], y2, cw3, "inbox", [("event_id", "pk"), ("source", ""), ("received_at", ""), ("processed_at", "")],
                    note="a repeat webhook is a no-op")
    h2 = table_card(c, xs[1], y2, cw3, "notion_writes", [("page_id", "pk"), ("payload_hash", ""), ("written_at", "")],
                    note="stops our own edits looking human")
    h3 = 0
    y = y2 - max(h1, h2, h3) - 38
    zone("Audit trail and cost log (never edited)", y)
    y3 = y - 22
    h1 = table_card(c, xs[0], y3, cw3, "events", [("id", "pk"), ("ts", ""), ("kind", ""), ("entity", ""), ("payload  json", ""), ("actor", "")],
                    fill=AUDIT, stroke=AUDIT_S, note="append-only (database trigger)")
    h2 = table_card(c, xs[1], y3, cw3, "runs", [("run_id", "pk"), ("job_type", ""), ("model, tier, attempt", ""), ("tokens_in, tokens_out", ""),
                                                 ("cost_usd, latency_ms", ""), ("config_version", ""), ("ok, error_code", "")],
                    fill=AUDIT, stroke=AUDIT_S, note="one row per model call")
    y = y3 - max(h1, h2) - 34
    card(c, M, y, CW, 150, fill=NOTE_BG, stroke=LLM_S)
    c.setFillColor(INK)
    c.setFont("Body-Bold", 9.4)
    c.drawString(M + 12, y - 19, "Why alert_outbox exists")
    para(c, "An alert used to be sent <i>after</i> the slip was saved. A crash in between meant the slip was recorded but the alert was lost, "
            "and a retry saw “nothing changed”. Now the alert is written <b>in the same atomic write</b> as the slip. Delivery marks it sent, "
            "and a sweeper runs every minute to send anything still unsent, once (each alert is claimed before sending, so two senders "
            "cannot both send it). After 10 failed tries an alert stops retrying and shows in the admin view.",
         M + 12, y - 28, CW - 24, "small")
    para(c, "The job queue's own tables are created by the Procrastinate library. Duplicate protection comes from <font face='Mono' size='7.6'>followups.dedupe_key</font>, "
            "<font face='Mono' size='7.6'>inbox.event_id</font> and <font face='Mono' size='7.6'>proposals(meeting_id, local_id)</font>.",
         M + 12, y - 100, CW - 24, "small")


# ----- page 6: buy vs build + failures -----------------------------------------------------
def page6(c):
    page_chrome(c, 6, "Buy vs build, and failure handling", "Buy what already works. Build only what Notion cannot do.")
    y = TOP - 4
    y -= heading(c, "What I buy, and what I build", M, y)
    BUY, BLD = "<font color='#2e7d4f'><b>Buy</b></font>", "<font color='#1d4ed8'><b>Build</b></font>"
    rows = [
        ["Need", "Decision", "Why"],
        ["Task system and interface", f"{BUY}: Notion", "The team already works there. No front-end to build."],
        ["Alerts, approvals, commands", f"{BUY}: Slack", "Buttons, modals and DMs come ready-made."],
        ["Database", f"{BUY}: Neon Postgres", "Managed, free to start. Holds only a cache and a log."],
        ["Reading mixed-language notes", f"{BUY}: model API (today Groq)", "Open-weight models through one gateway. Pay per token; none self-hosted."],
        ["Job queue", "Use a library: Procrastinate", "Runs on the same Postgres. No Redis to run."],
        ["Dependency graph, slack, risk", BLD, "Notion cannot compute how one late task delays a chain."],
        ["Priority engine", BLD, "Needs stable rankings that explain themselves. Notion sorts cannot."],
        ["Approval rules, follow-ups, sync", BLD, "“AI proposes, a person confirms” has to be enforced in code."],
    ]
    h = table(c, rows, [150, 150, CW - 300], M, y)
    y -= h + 22

    y -= heading(c, "What happens when things go wrong", M, y)
    rows = [
        ["When this fails", "What happens"],
        ["A Notion event is missed", "The 5-minute poll re-reads everything, so a webhook is a speed-up, not a dependency."],
        ["The same event or click arrives twice", "Unique keys make it a no-op; one alert per cause per hour; a lock stops a poll and a click syncing at once."],
        ["The model API is down or returns junk", "Output is schema-checked, repaired once, then the next provider, then a bigger model. Jobs retry with back-off."],
        ["Bad data in Notion", "Dependency cycles are rejected with a plain alert. Odd values become warnings. Unknown owners or goals are flagged, never guessed."],
        ["Slack cannot DM someone", "The message goes to the alerts channel instead."],
        ["A Notion write fails", "A failed approval is reopened so nothing is half-done; all multi-row database writes are atomic."],
        ["A secret is not set", "The endpoint refuses requests instead of running open."],
        ["The service crashes after saving a slip, before sending its alert", "Nothing is lost: the alert was saved in the same atomic write as the slip. A sweeper, every minute, sends anything unsent, once."],
        ["<b>Remaining gap:</b> Slack stays down for a long time", "After 10 failed tries an alert stops retrying and shows in the admin view. Nobody is paged yet."],
    ]
    table(c, rows, [190, CW - 190], M, y, extra_styles=[("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.HexColor("#fff3dc"))])


# ----- page 7: cost + next -----------------------------------------------------------------
def page7(c):
    page_chrome(c, 7, "Estimated monthly running cost", "US dollars. Estimates, not measurements. Notion and Slack seats are excluded.")
    y = TOP - 4
    rows = [
        ["Item", "MVP", "Production", "How I got it"],
        ["Always-on hosting", "$0", "$0 – 6", "Oracle's free VM, or a small VPS"],
        ["Postgres (Neon)", "$0", "$5 – 20", "Free plan to start; the paid range is unverified"],
        ["Model API tokens", "$0", "under $1", "About 20 meetings × 6k tokens a month, on open-weight models"],
        ["Monitoring and CI", "$0", "$0", "Free tiers (Sentry, Healthchecks, GitHub). Not wired in yet"],
        ["<b>Total</b>", "<b>$0</b>", "<b>about $10 – 30</b>", "<b>Excluding Notion and Slack seats</b>"],
    ]
    h = table(c, rows, [130, 55, 100, CW - 285], M, y, extra_styles=[("BACKGROUND", (0, 5), (-1, 5), colors.HexColor("#e6ebf3"))])
    y -= h + 26

    y -= heading(c, "Assumptions behind the numbers", M, y)
    y -= bullets(c, [
        "5 to 10 users, a few hundred open tasks, around 20 meetings a month.",
        "The model is only used to read meeting notes; everything else is plain code, so token use stays tiny.",
        "<b>Free model tiers may log prompts.</b> Real founder content should wait for a paid zero-retention provider. Swapping is a config change.",
        "A host in AWS us-east-2, next to the database, cuts a query from about 215 ms to a few ms. That is a speed gain, not a cost.",
    ], M, y, CW, gap=7) + 20

    y -= heading(c, "Measured so far", M, y)
    y -= bullets(c, [
        "A slip on the 4-task demo chain alerted all four owners in about <b>16 seconds</b>, including Notion round trips.",
        "Database operations cost about <b>0.4 – 0.7 s</b> each after batching (a slip was 12.4 s before).",
        "Reading one mixed English, Hindi and Gujarati note took about <b>11 seconds</b> and produced correct owners, dates and dependencies.",
        "Accuracy is <b>not</b> measured yet: the 80% approved-without-edits target needs a labelled set of real notes.",
    ], M, y, CW, gap=7) + 20

    y -= heading(c, "What I would revisit as it grows", M, y)
    bullets(c, [
        "Turn on Notion webhooks once there is a public address, so a change is seen in seconds, not up to 5 minutes.",
        "Page someone when an alert has given up after 10 tries (today it only shows in the admin view).",
        "Hold less urgent slips until working hours (today only follow-ups respect working hours).",
        "Give AI agents their own approval gate before they are allowed to own recurring tasks.",
    ], M, y, CW, gap=7)


def build():
    OUT.parent.mkdir(exist_ok=True)
    c = canvas.Canvas(str(OUT), pagesize=A4)
    c.setTitle("Ops Brain: build plan")
    c.setAuthor("Ops Brain build")
    c.setSubject("PRD 1 build plan: v1, architecture, data model, buy vs build, failure handling, running cost")
    for fn in (page1, page2, page3, page4, page5, page6, page7):
        fn(c)
        c.showPage()
    c.save()


if __name__ == "__main__":
    build()
    print(f"wrote {OUT} ({TOTAL_PAGES} pages)")
