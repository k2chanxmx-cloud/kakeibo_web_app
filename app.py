import os
import json
import base64
import hashlib
import re
from datetime import datetime, date
from email.utils import parsedate_to_datetime

import jpholiday
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_from_directory,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "kakeibo-secret-key")

DATABASE_URL = os.getenv("DATABASE_URL")
APP_PASSWORD = os.getenv("APP_PASSWORD", "1234")

PAYDAY = 25
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CATEGORIES = [
    "旦那に渡す",
    "携帯代",
    "コンカフェ",
    "同伴",
    "交際費",
    "Uber",
    "タクシー",
    "シーシャ",
    "服",
    "食費",
    "交通費",
    "美容",
    "その他",
]

BUDGETS = {
    "コンカフェ": 50000,
    "同伴": 10000,
    "交際費": 30000,
    "Uber": 15000,
    "タクシー": 15000,
    "シーシャ": 20000,
    "交通費": 10000,
    "携帯代": 9000,
    "服": 5000,
    "その他": 20000,
}

PLAY_CATEGORIES = ["コンカフェ", "同伴", "交際費", "シーシャ"]


def yen(value):
    if value is None:
        value = 0
    return f"¥{int(value):,}"


app.jinja_env.filters["yen"] = yen


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL が設定されていません")
    return psycopg2.connect(DATABASE_URL)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def add_month(y, m, delta):
    m2 = m + delta
    y2 = y + (m2 - 1) // 12
    m2 = (m2 - 1) % 12 + 1
    return y2, m2


def to_date(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def is_business_day(d):
    if d.weekday() >= 5:
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def get_actual_payday(year, month):
    d = date(year, month, PAYDAY)

    while not is_business_day(d):
        d = date(d.year, d.month, d.day - 1)

    return d


def get_accounting_month(d):
    d = to_date(d)
    actual_payday = get_actual_payday(d.year, d.month)

    if d >= actual_payday:
        y, m = add_month(d.year, d.month, 1)
        return f"{y:04d}-{m:02d}"

    return f"{d.year:04d}-{d.month:02d}"


def this_accounting_month():
    return get_accounting_month(datetime.now().date())


def get_period_range(ym):
    y = int(ym[:4])
    m = int(ym[5:7])

    py, pm = add_month(y, m, -1)

    start = get_actual_payday(py, pm)
    end = get_actual_payday(y, m)

    return start, end


def get_last_accounting_months(n=6):
    now_ym = this_accounting_month()
    y = int(now_ym[:4])
    m = int(now_ym[5:7])

    result = []

    for i in range(n - 1, -1, -1):
        yy, mm = add_month(y, m, -i)
        result.append(f"{yy:04d}-{mm:02d}")

    return result


def guess_category(title):
    title = title or ""

    if "コンカフェ" in title:
        return "コンカフェ"
    if "同伴" in title:
        return "同伴"
    if "Uber" in title or "ウーバー" in title:
        return "Uber"
    if "タクシー" in title:
        return "タクシー"
    if "シーシャ" in title:
        return "シーシャ"
    if (
        "飲み" in title
        or "ご飯" in title
        or "ごはん" in title
        or "デート" in title
        or "遊び" in title
    ):
        return "交際費"

    return "交際費"


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            expense_date DATE NOT NULL,
            category TEXT NOT NULL,
            amount INTEGER NOT NULL,
            memo TEXT,
            owner TEXT,
            source_event_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS expense_candidates (
            id SERIAL PRIMARY KEY,
            source_event_id INTEGER UNIQUE NOT NULL,
            event_date DATE NOT NULL,
            title TEXT NOT NULL,
            owner TEXT NOT NULL,
            category TEXT,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            memo TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        ALTER TABLE expense_candidates
        ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'calendar';
    """)

    cur.execute("""
        ALTER TABLE expense_candidates
        ADD COLUMN IF NOT EXISTS source_key TEXT;
    """)

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_expense_candidates_source_key
        ON expense_candidates(source_key);
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS income_records (
            id SERIAL PRIMARY KEY,
            income_date DATE NOT NULL,
            amount INTEGER NOT NULL,
            memo TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS savings_records (
            id SERIAL PRIMARY KEY,
            saving_date DATE NOT NULL,
            amount INTEGER NOT NULL,
            type TEXT NOT NULL,
            memo TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_income_total(ym):
    start, end = get_period_range(ym)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM income_records
        WHERE income_date >= %s
          AND income_date < %s
    """, (start, end))

    total = cur.fetchone()[0] or 0

    cur.close()
    conn.close()

    return total


def login_required():
    return session.get("logged_in") is True


# ==============================
# Gmail / Uber Eats
# ==============================
def get_gmail_credentials():
    token_json = os.getenv("GMAIL_TOKEN_JSON")

    if token_json:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info, GMAIL_SCOPES)
    else:
        creds = Credentials.from_authorized_user_file("token.json", GMAIL_SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return creds


def get_gmail_service():
    creds = get_gmail_credentials()
    return build("gmail", "v1", credentials=creds)


def decode_gmail_part(data):
    if not data:
        return ""

    missing_padding = len(data) % 4
    if missing_padding:
        data += "=" * (4 - missing_padding)

    return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
        "utf-8",
        errors="ignore"
    )


def extract_body_from_payload(payload):
    body_data = payload.get("body", {}).get("data")

    if body_data:
        return decode_gmail_part(body_data)

    texts = []

    for part in payload.get("parts", []):
        mime = part.get("mimeType", "")

        if mime in ["text/plain", "text/html"]:
            data = part.get("body", {}).get("data")
            if data:
                texts.append(decode_gmail_part(data))

        if part.get("parts"):
            texts.append(extract_body_from_payload(part))

    return "\n".join(texts)


def clean_mail_text(text):
    text = text or ""

    if "<html" in text.lower() or "<body" in text.lower():
        soup = BeautifulSoup(text, "html.parser")
        return soup.get_text("\n")

    return text


def extract_uber_amount(text):
    text = clean_mail_text(text)
    text = text.replace("\xa0", " ")

    patterns = [
        r"(?:合計|総計|ご請求額|お支払い|Total)[^\d¥￥]{0,40}[¥￥]\s*([\d,]+)",
        r"[¥￥]\s*([\d,]+)",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        if matches:
            amounts = [int(m.replace(",", "")) for m in matches]
            return max(amounts)

    return None


def get_header(headers, name):
    return next(
        (h["value"] for h in headers if h["name"].lower() == name.lower()),
        ""
    )


def gmail_message_to_date(headers):
    date_text = get_header(headers, "Date")

    if not date_text:
        return today()

    try:
        dt = parsedate_to_datetime(date_text)
        return dt.date().strftime("%Y-%m-%d")
    except Exception:
        return today()


def gmail_message_key(msg_id):
    digest = hashlib.sha1(msg_id.encode("utf-8")).hexdigest()
    return -1 * (int(digest[:8], 16) % 1000000000)


def import_uber_messages_to_candidates():
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me",
        q='from:(uber.com OR ubereats.com) newer_than:180d',
        maxResults=20
    ).execute()

    messages = results.get("messages", [])

    conn = get_conn()
    cur = conn.cursor()

    imported_count = 0

    for msg in messages:
        msg_id = msg["id"]

        detail = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="full"
        ).execute()

        payload = detail.get("payload", {})
        headers = payload.get("headers", [])

        subject = get_header(headers, "Subject") or "Uber Eats"
        body = extract_body_from_payload(payload)

        amount = extract_uber_amount(body)
        event_date = gmail_message_to_date(headers)

        if not amount:
            continue

        source_key = f"gmail_uber_{msg_id}"
        source_event_id = gmail_message_key(msg_id)

        cur.execute("""
            INSERT INTO expense_candidates (
                source_event_id,
                event_date,
                title,
                owner,
                category,
                amount,
                status,
                memo,
                source_type,
                source_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, 'uber', %s)
            ON CONFLICT DO NOTHING
        """, (
            source_event_id,
            event_date,
            subject,
            "まき",
            "Uber",
            amount,
            "Gmailから取り込み",
            source_key,
        ))

        if cur.rowcount > 0:
            imported_count += 1

    conn.commit()
    cur.close()
    conn.close()

    return imported_count


# ==============================
# 共通
# ==============================
@app.before_request
def before_request():
    if request.endpoint in ["login", "static", "manifest", "service_worker"]:
        return

    if not login_required():
        return redirect(url_for("login"))

    init_db()


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory("static", "service-worker.js")


# ==============================
# ログイン
# ==============================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password")

        if password == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("home"))

        flash("パスワードが違います")

    return """
    <!doctype html>
    <html lang="ja">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>家計簿ログイン</title>
      <link rel="manifest" href="/manifest.json">
      <link rel="apple-touch-icon" href="/static/icons/icon-192.png">
      <link rel="stylesheet" href="/static/style.css">
    </head>
    <body>
      <div class="app-shell">
        <div class="big-card">
          <h1>家計簿ログイン</h1>
          <form method="post" class="form">
            <label>パスワード</label>
            <input type="password" name="password" class="input" required>
            <button class="primary-btn">ログイン</button>
          </form>
        </div>
      </div>
    </body>
    </html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==============================
# ホーム
# ==============================
@app.route("/")
def home():
    ym = this_accounting_month()
    start, end = get_period_range(ym)
    income = get_income_total(ym)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT category, COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE expense_date >= %s
          AND expense_date < %s
        GROUP BY category
        ORDER BY total DESC
    """, (start, end))
    category_rows = cur.fetchall()

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE expense_date >= %s
          AND expense_date < %s
    """, (start, end))
    total = cur.fetchone()["total"] or 0

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE expense_date >= %s
          AND expense_date < %s
          AND category IN ('コンカフェ', '同伴', '交際費', 'シーシャ')
    """, (start, end))
    play_total = cur.fetchone()["total"] or 0

    cur.close()
    conn.close()

    remain = income - total

    overuse = []
    data_map = {r["category"]: r["total"] for r in category_rows}

    for category, budget in BUDGETS.items():
        spent = data_map.get(category, 0)
        rate = spent / budget if budget else 0

        if rate >= 0.7:
            overuse.append({
                "category": category,
                "spent": spent,
                "budget": budget,
                "rate": min(rate * 100, 100),
                "danger": rate >= 1,
            })

    chart_labels = [r["category"] for r in category_rows]
    chart_values = [int(r["total"]) for r in category_rows]

    return render_template(
        "home.html",
        active="home",
        ym=ym,
        start=start,
        end=end,
        income=income,
        total=total,
        remain=remain,
        play_total=play_total,
        category_rows=category_rows,
        chart_labels=chart_labels,
        chart_values=chart_values,
        overuse=overuse,
    )


# ==============================
# 収入
# ==============================
@app.route("/income", methods=["GET", "POST"])
def income():
    ym = this_accounting_month()
    start, end = get_period_range(ym)

    if request.method == "POST":
        income_date = request.form.get("income_date")
        amount = request.form.get("amount")
        memo = request.form.get("memo")

        if not income_date or not amount:
            flash("日付と金額を入力してください")
            return redirect(url_for("income"))

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO income_records (
                income_date,
                amount,
                memo
            )
            VALUES (%s, %s, %s)
        """, (
            income_date,
            int(amount),
            memo,
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("収入を追加しました")
        return redirect(url_for("income"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT *
        FROM income_records
        WHERE income_date >= %s
          AND income_date < %s
        ORDER BY income_date DESC, id DESC
    """, (start, end))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    income_total = get_income_total(ym)

    return render_template(
        "income.html",
        active="income",
        ym=ym,
        start=start,
        end=end,
        today=today(),
        rows=rows,
        income_total=income_total,
    )


@app.route("/income/<int:income_id>/delete", methods=["POST"])
def delete_income(income_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM income_records
        WHERE id = %s
    """, (income_id,))

    conn.commit()
    cur.close()
    conn.close()

    flash("収入を削除しました")
    return redirect(url_for("income"))


# ==============================
# 支出入力
# ==============================
@app.route("/input", methods=["GET", "POST"])
def input_expense():
    if request.method == "POST":
        expense_date = request.form.get("expense_date")
        owner = request.form.get("owner")
        category = request.form.get("category")
        amount = request.form.get("amount")
        memo = request.form.get("memo")

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO expenses (
                expense_date,
                category,
                amount,
                memo,
                owner,
                source_event_id
            )
            VALUES (%s, %s, %s, %s, %s, NULL)
        """, (
            expense_date,
            category,
            int(amount),
            memo,
            owner,
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("支出を追加しました")
        return redirect(url_for("home"))

    return render_template(
        "input.html",
        active="input",
        today=today(),
        categories=CATEGORIES,
    )


# ==============================
# 貯金
# ==============================
@app.route("/savings", methods=["GET", "POST"])
def savings():
    ym = this_accounting_month()
    start, end = get_period_range(ym)

    if request.method == "POST":
        saving_date = request.form.get("saving_date")
        amount = request.form.get("amount")
        saving_type = request.form.get("type")
        memo = request.form.get("memo")

        if not saving_date or not amount or not saving_type:
            flash("日付・金額・種別を入力してください")
            return redirect(url_for("savings"))

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO savings_records (
                saving_date,
                amount,
                type,
                memo
            )
            VALUES (%s, %s, %s, %s)
        """, (
            saving_date,
            int(amount),
            saving_type,
            memo,
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("貯金を登録しました")
        return redirect(url_for("savings"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN type = 'deposit' THEN amount
                    WHEN type = 'withdraw' THEN -amount
                    ELSE 0
                END
            ), 0) AS total
        FROM savings_records
    """)
    total_savings = cur.fetchone()["total"] or 0

    cur.execute("""
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN type = 'deposit' THEN amount
                    WHEN type = 'withdraw' THEN -amount
                    ELSE 0
                END
            ), 0) AS total
        FROM savings_records
        WHERE saving_date >= %s
          AND saving_date < %s
    """, (start, end))
    monthly_savings = cur.fetchone()["total"] or 0

    cur.execute("""
        SELECT *
        FROM savings_records
        ORDER BY saving_date DESC, id DESC
        LIMIT 100
    """)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "savings.html",
        active="savings",
        today=today(),
        ym=ym,
        start=start,
        end=end,
        total_savings=total_savings,
        monthly_savings=monthly_savings,
        rows=rows,
    )


@app.route("/savings/<int:saving_id>/delete", methods=["POST"])
def delete_saving(saving_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM savings_records
        WHERE id = %s
    """, (saving_id,))

    conn.commit()
    cur.close()
    conn.close()

    flash("貯金履歴を削除しました")
    return redirect(url_for("savings"))


# ==============================
# データ取り込み
# ==============================
@app.route("/candidates")
def candidates():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT *
        FROM expense_candidates
        WHERE status = 'pending'
        ORDER BY event_date ASC, id ASC
    """)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "candidates.html",
        active="candidates",
        rows=rows,
        categories=CATEGORIES,
    )


@app.route("/candidates/import", methods=["POST"])
def import_candidates():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT id, start_date, title, owner, memo
        FROM events
        WHERE owner IN ('まき', '二人')
        ORDER BY start_date ASC
    """)
    events = cur.fetchall()

    count = 0

    for event in events:
        source_key = f"calendar_{event['id']}"

        cur.execute("""
            INSERT INTO expense_candidates (
                source_event_id,
                event_date,
                title,
                owner,
                category,
                amount,
                status,
                memo,
                source_type,
                source_key
            )
            VALUES (%s, %s, %s, %s, %s, NULL, 'pending', %s, 'calendar', %s)
            ON CONFLICT DO NOTHING
        """, (
            event["id"],
            event["start_date"],
            event["title"],
            event["owner"],
            guess_category(event["title"]),
            event.get("memo") or "",
            source_key,
        ))

        if cur.rowcount > 0:
            count += 1

    conn.commit()
    cur.close()
    conn.close()

    flash(f"カレンダー予定を{count}件取り込みました")
    return redirect(url_for("candidates"))


@app.route("/candidates/import_uber", methods=["POST"])
def import_uber_candidates():
    try:
        count = import_uber_messages_to_candidates()
        flash(f"Uberメールを{count}件取り込みました")
    except Exception as e:
        flash(f"Uber取り込みエラー：{e}")

    return redirect(url_for("candidates"))


@app.route("/candidates/<int:candidate_id>/confirm", methods=["POST"])
def confirm_candidate(candidate_id):
    amount = request.form.get("amount")
    category = request.form.get("category")

    if not amount:
        flash("金額を入力してください")
        return redirect(url_for("candidates"))

    if not category:
        flash("カテゴリを選択してください")
        return redirect(url_for("candidates"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT *
        FROM expense_candidates
        WHERE id = %s
    """, (candidate_id,))
    c = cur.fetchone()

    if not c:
        flash("対象が見つかりません")
        cur.close()
        conn.close()
        return redirect(url_for("candidates"))

    cur.execute("""
        INSERT INTO expenses (
            expense_date,
            category,
            amount,
            memo,
            owner,
            source_event_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        c["event_date"],
        category,
        int(amount),
        f"取り込み：{c['title']}",
        c["owner"],
        c["source_event_id"],
    ))

    cur.execute("""
        UPDATE expense_candidates
        SET amount = %s,
            category = %s,
            status = 'confirmed'
        WHERE id = %s
    """, (
        int(amount),
        category,
        candidate_id,
    ))

    conn.commit()
    cur.close()
    conn.close()

    flash("家計簿に反映しました")
    return redirect(url_for("home"))


@app.route("/candidates/<int:candidate_id>/delete", methods=["POST"])
def delete_candidate(candidate_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM expense_candidates
        WHERE id = %s
    """, (candidate_id,))

    conn.commit()
    cur.close()
    conn.close()

    flash("取り込み候補を削除しました")
    return redirect(url_for("candidates"))


# ==============================
# 比較
# ==============================
@app.route("/compare")
def compare():
    selected_category = request.args.get("category", "コンカフェ")
    months = get_last_accounting_months(6)

    conn = get_conn()
    cur = conn.cursor()

    category_values = []
    play_values = []

    for ym in months:
        start, end = get_period_range(ym)

        cur.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE expense_date >= %s
              AND expense_date < %s
              AND category = %s
        """, (start, end, selected_category))
        category_values.append(cur.fetchone()[0] or 0)

        cur.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE expense_date >= %s
              AND expense_date < %s
              AND category IN ('コンカフェ', '同伴', '交際費', 'シーシャ')
        """, (start, end))
        play_values.append(cur.fetchone()[0] or 0)

    cur.close()
    conn.close()

    return render_template(
        "compare.html",
        active="compare",
        categories=CATEGORIES,
        selected_category=selected_category,
        labels=[m[5:] + "月" for m in months],
        category_values=[int(v) for v in category_values],
        play_values=[int(v) for v in play_values],
    )


# ==============================
# 履歴
# ==============================
@app.route("/history")
def history():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT *
        FROM expenses
        ORDER BY expense_date DESC, id DESC
        LIMIT 200
    """)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "history.html",
        active="history",
        rows=rows,
    )


@app.route("/history/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM expenses
        WHERE id = %s
    """, (expense_id,))

    deleted_count = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    if deleted_count > 0:
        flash("削除しました")
    else:
        flash("削除対象が見つかりませんでした")

    return redirect(url_for("history"))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)