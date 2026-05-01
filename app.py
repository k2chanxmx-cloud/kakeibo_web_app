import os
from datetime import datetime

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

CATEGORIES = [
    "旦那に渡す",
    "携帯代",
    "コンカフェ",
    "同伴",
    "交際費",
    "服",
    "食費",
    "交通費",
    "美容",
    "その他",
]

AUTO_FIXED_EXPENSES = {
    "旦那に渡す": 80000,
    "携帯代": 9000,
}

BUDGETS = {
    "コンカフェ": 50000,
    "同伴": 10000,
    "交際費": 30000,
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


def this_month():
    return datetime.now().strftime("%Y-%m")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def guess_category(title):
    title = title or ""

    if "コンカフェ" in title:
        return "コンカフェ"
    if "同伴" in title:
        return "同伴"
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


def auto_insert_fixed_expenses():
    ym = this_month()
    first_day = f"{ym}-01"

    conn = get_conn()
    cur = conn.cursor()

    for category, amount in AUTO_FIXED_EXPENSES.items():
        memo = f"自動反映:{ym}:{category}"

        cur.execute("""
            SELECT id FROM expenses
            WHERE memo = %s
            LIMIT 1
        """, (memo,))

        if not cur.fetchone():
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
                first_day,
                category,
                amount,
                memo,
                "まき",
            ))

    conn.commit()
    cur.close()
    conn.close()


def login_required():
    return session.get("logged_in") is True


@app.before_request
def before_request():
    if request.endpoint in ["login", "static", "manifest", "service_worker"]:
        return

    if not login_required():
        return redirect(url_for("login"))

    init_db()
    auto_insert_fixed_expenses()


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
    ym = this_month()
    income = get_income(ym)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT category, COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE TO_CHAR(expense_date, 'YYYY-MM') = %s
        GROUP BY category
        ORDER BY total DESC
    """, (ym,))
    category_rows = cur.fetchall()

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE TO_CHAR(expense_date, 'YYYY-MM') = %s
    """, (ym,))
    total = cur.fetchone()["total"] or 0

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE TO_CHAR(expense_date, 'YYYY-MM') = %s
          AND category IN ('コンカフェ', '同伴', '交際費')
    """, (ym,))
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
    ym = this_month()

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

    return redirect(url_for("candidates"))


@app.route("/compare")
def compare():
    selected_category = request.args.get("category", "コンカフェ")
    mode = request.args.get("mode", "category")

    months = []
    now = datetime.now()

    for i in range(5, -1, -1):
        y = now.year
        m = now.month - i

        while m <= 0:
            m += 12
            y -= 1

        months.append(f"{y:04d}-{m:02d}")

    conn = get_conn()
    cur = conn.cursor()

    values = []

    for ym in months:
        if mode == "play":
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM expenses
                WHERE TO_CHAR(expense_date, 'YYYY-MM') = %s
                  AND category IN ('コンカフェ', '同伴', '交際費')
            """, (ym,))
        else:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM expenses
                WHERE TO_CHAR(expense_date, 'YYYY-MM') = %s
                  AND category = %s
            """, (ym, selected_category))

        values.append(cur.fetchone()[0] or 0)

    cur.close()
    conn.close()

    return render_template(
        "compare.html",
        active="compare",
        categories=CATEGORIES,
        selected_category=selected_category,
        mode=mode,
        labels=[m[5:] + "月" for m in months],
        values=[int(v) for v in values],
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

    conn.commit()
    cur.close()
    conn.close()

    flash("削除しました")
    return redirect(url_for("history"))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)