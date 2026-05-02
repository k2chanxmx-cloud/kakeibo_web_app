import os
from datetime import datetime, date

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
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "kakeibo-secret-key")

DATABASE_URL = os.getenv("DATABASE_URL")
APP_PASSWORD = os.getenv("APP_PASSWORD", "1234")

CLOSING_DAY = 25

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
    "シーシャ": 20000,
    "交通費": 10000,
    "携帯代": 9000,
    "服": 5000,
    "その他": 20000,
}


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


def this_accounting_month():
    return get_accounting_month(datetime.now().date())


def to_date(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def get_accounting_month(d):
    d = to_date(d)
    y, m = d.year, d.month

    if d.day >= CLOSING_DAY:
        y, m = add_month(y, m, 1)

    return f"{y:04d}-{m:02d}"


def get_period_range(ym):
    y = int(ym[:4])
    m = int(ym[5:7])

    py, pm = add_month(y, m, -1)

    start = date(py, pm, CLOSING_DAY)
    end = date(y, m, CLOSING_DAY)

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
    if "シーシャ" in title:
        return "シーシャ"
    if "飲み" in title or "ご飯" in title or "ごはん" in title or "デート" in title or "遊び" in title:
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
        CREATE TABLE IF NOT EXISTS incomes (
            id SERIAL PRIMARY KEY,
            year_month TEXT UNIQUE,
            amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_income(ym):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT amount
        FROM incomes
        WHERE year_month = %s
    """, (ym,))

    row = cur.fetchone()

    cur.close()
    conn.close()

    if row:
        return row[0]

    return 0


def login_required():
    return session.get("logged_in") is True


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


@app.route("/")
def home():
    ym = this_accounting_month()
    start, end = get_period_range(ym)
    income = get_income(ym)

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


@app.route("/income", methods=["GET", "POST"])
def income():
    ym = this_accounting_month()
    start, end = get_period_range(ym)

    if request.method == "POST":
        amount = request.form.get("amount")

        if not amount:
            flash("収入を入力してください")
            return redirect(url_for("income"))

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO incomes (year_month, amount)
            VALUES (%s, %s)
            ON CONFLICT (year_month)
            DO UPDATE SET amount = EXCLUDED.amount
        """, (ym, int(amount)))

        conn.commit()
        cur.close()
        conn.close()

        flash("収入を保存しました")
        return redirect(url_for("home"))

    current_income = get_income(ym)

    return render_template(
        "income.html",
        active="income",
        ym=ym,
        start=start,
        end=end,
        income=current_income,
    )


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
        cur.execute("""
            INSERT INTO expense_candidates (
                source_event_id,
                event_date,
                title,
                owner,
                category,
                amount,
                status,
                memo
            )
            VALUES (%s, %s, %s, %s, %s, NULL, 'pending', %s)
            ON CONFLICT (source_event_id) DO NOTHING
        """, (
            event["id"],
            event["start_date"],
            event["title"],
            event["owner"],
            guess_category(event["title"]),
            event.get("memo") or "",
        ))

        if cur.rowcount > 0:
            count += 1

    conn.commit()
    cur.close()
    conn.close()

    flash(f"{count}件取り込みました")
    return redirect(url_for("candidates"))


@app.route("/candidates/<int:candidate_id>/confirm", methods=["POST"])
def confirm_candidate(candidate_id):
    amount = request.form.get("amount")

    if not amount:
        flash("金額を入力してください")
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
        c["category"],
        int(amount),
        f"カレンダー連携：{c['title']}",
        c["owner"],
        c["source_event_id"],
    ))

    cur.execute("""
        UPDATE expense_candidates
        SET amount = %s,
            status = 'confirmed'
        WHERE id = %s
    """, (
        int(amount),
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

    flash("予定候補を削除しました")
    return redirect(url_for("candidates"))


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