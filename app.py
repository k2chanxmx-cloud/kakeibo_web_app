from flask import Flask, render_template, request, redirect, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


# =========================================
# DB接続
# =========================================

def get_conn():
    return psycopg2.connect(DATABASE_URL)


# =========================================
# DB初期化
# =========================================

def init_db():

    with get_conn() as conn:

        with conn.cursor() as cur:

            # =========================
            # tickets
            # =========================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tickets (

                    id SERIAL PRIMARY KEY,

                    company TEXT NOT NULL,

                    balance INTEGER NOT NULL DEFAULT 0,

                    expire_date DATE,

                    category TEXT NOT NULL DEFAULT 'その他',

                    memo TEXT,

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                ALTER TABLE tickets
                ADD COLUMN IF NOT EXISTS memo TEXT;
            """)

            # =========================
            # logs
            # =========================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS logs (

                    id SERIAL PRIMARY KEY,

                    message TEXT,

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        conn.commit()


@app.before_request
def before_request():
    init_db()


# =========================================
# Home
# =========================================

@app.route("/")
def home():

    with get_conn() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    id,
                    company,
                    balance,
                    expire_date,
                    category,
                    memo,
                    created_at
                FROM tickets
                ORDER BY
                    expire_date IS NULL,
                    expire_date ASC,
                    id DESC;
            """)

            tickets = cur.fetchall()

            cur.execute("""
                SELECT
                    id,
                    message,
                    created_at
                FROM logs
                ORDER BY id DESC
                LIMIT 20;
            """)

            logs = cur.fetchall()

    total_balance = sum(
        ticket["balance"]
        for ticket in tickets
    )

    ticket_count = len(tickets)

    return render_template(
        "index.html",
        tickets=tickets,
        total_balance=total_balance,
        ticket_count=ticket_count,
        logs=logs
    )


# =========================================
# Add Ticket
# =========================================

@app.route("/add", methods=["POST"])
def add_ticket():

    company = request.form.get(
        "company",
        ""
    ).strip()

    balance = request.form.get(
        "balance",
        "0"
    ).strip()

    expire_date = request.form.get(
        "expire_date",
        ""
    ).strip()

    category = request.form.get(
        "category",
        "その他"
    ).strip()

    memo = request.form.get(
        "memo",
        ""
    ).strip()

    if not company:
        return redirect(url_for("home"))

    try:
        balance = int(balance)

    except ValueError:
        balance = 0

    if expire_date == "":
        expire_date = None

    with get_conn() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO tickets
                    (
                        company,
                        balance,
                        expire_date,
                        category,
                        memo
                    )
                VALUES
                    (%s, %s, %s, %s, %s);
            """, (
                company,
                balance,
                expire_date,
                category,
                memo
            ))

            cur.execute("""
                INSERT INTO logs
                    (message)
                VALUES
                    (%s);
            """, (
                f"{company} を登録しました",
            ))

        conn.commit()

    return redirect(url_for("home"))


# =========================================
# Delete
# =========================================

@app.route("/delete/<int:ticket_id>")
def delete_ticket(ticket_id):

    with get_conn() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT company
                FROM tickets
                WHERE id = %s;
            """, (ticket_id,))

            row = cur.fetchone()

            company_name = (
                row[0]
                if row
                else "優待"
            )

            cur.execute("""
                DELETE FROM tickets
                WHERE id = %s;
            """, (ticket_id,))

            cur.execute("""
                INSERT INTO logs
                    (message)
                VALUES
                    (%s);
            """, (
                f"{company_name} を削除しました",
            ))

        conn.commit()

    return redirect(url_for("home"))


# =========================================
# Main
# =========================================

if __name__ == "__main__":
    app.run(debug=True)