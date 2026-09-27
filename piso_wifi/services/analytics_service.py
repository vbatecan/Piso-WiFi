"""Business intelligence and sales analytics service for Piso-WiFi."""

from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
import io
import logging
import sqlite3
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


class AnalyticsService:
    """Service for calculating gross revenue, usage trends, and exporting transaction reports."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "config/piso_wifi.db"

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_summary(self, period: str = "day") -> Dict[str, Any]:
        """Calculate gross revenue, transactions, and average spend for a given timeframe.

        Args:
            period: 'day' (last 24h), 'week' (last 7d), 'month' (last 30d), or 'all'.
        """
        now = datetime.now()
        where_clause = ""
        params: List[Any] = []

        if period == "day":
            since = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            where_clause = "WHERE t.created_at >= ?"
            params.append(since)
        elif period == "week":
            since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            where_clause = "WHERE t.created_at >= ?"
            params.append(since)
        elif period == "month":
            since = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            where_clause = "WHERE t.created_at >= ?"
            params.append(since)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                query = f"""
                    SELECT 
                        COALESCE(SUM(t.amount), 0.0) as total_revenue,
                        COALESCE(SUM(t.minutes), 0) as total_minutes,
                        COUNT(t.id) as total_transactions,
                        COUNT(DISTINCT t.user_id) as unique_users
                    FROM transactions t
                    {where_clause}
                """
                cursor.execute(query, params)
                row = cursor.fetchone()

                total_rev = float(row["total_revenue"]) if row else 0.0
                total_min = int(row["total_minutes"]) if row else 0
                total_tx = int(row["total_transactions"]) if row else 0
                unique_users = int(row["unique_users"]) if row else 0
                avg_ticket = (total_rev / total_tx) if total_tx > 0 else 0.0

                return {
                    "period": period,
                    "total_revenue": total_rev,
                    "total_minutes": total_min,
                    "total_transactions": total_tx,
                    "unique_users": unique_users,
                    "average_ticket": round(avg_ticket, 2),
                }
        except Exception as e:
            logger.error(f"Error computing sales summary: {e}")
            return {
                "period": period,
                "total_revenue": 0.0,
                "total_minutes": 0,
                "total_transactions": 0,
                "unique_users": 0,
                "average_ticket": 0.0,
            }

    def get_denomination_breakdown(self) -> Dict[str, Dict[str, Any]]:
        """Calculate counts and peso sums grouped by coin denomination (₱1, ₱5, ₱10, ₱20, Other)."""
        breakdown = {
            "p1": {"denomination": 1, "count": 0, "total_pesos": 0.0},
            "p5": {"denomination": 5, "count": 0, "total_pesos": 0.0},
            "p10": {"denomination": 10, "count": 0, "total_pesos": 0.0},
            "p20": {"denomination": 20, "count": 0, "total_pesos": 0.0},
            "other": {"denomination": 0, "count": 0, "total_pesos": 0.0},
        }

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT amount, COUNT(id) as cnt FROM transactions GROUP BY amount")
                rows = cursor.fetchall()
                for row in rows:
                    amt = float(row["amount"]) if row["amount"] is not None else 0.0
                    cnt = int(row["cnt"])
                    tot = amt * cnt
                    if amt == 1:
                        breakdown["p1"]["count"] += cnt
                        breakdown["p1"]["total_pesos"] += tot
                    elif amt == 5:
                        breakdown["p5"]["count"] += cnt
                        breakdown["p5"]["total_pesos"] += tot
                    elif amt == 10:
                        breakdown["p10"]["count"] += cnt
                        breakdown["p10"]["total_pesos"] += tot
                    elif amt == 20:
                        breakdown["p20"]["count"] += cnt
                        breakdown["p20"]["total_pesos"] += tot
                    else:
                        breakdown["other"]["count"] += cnt
                        breakdown["other"]["total_pesos"] += tot
        except Exception as e:
            logger.error(f"Error computing denomination breakdown: {e}")

        return breakdown

    def get_hourly_heatmap(self) -> Dict[int, float]:
        """Compute total gross sales aggregated by hour of the day (0-23)."""
        hourly = {h: 0.0 for h in range(24)}
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # SQLite strftime('%H', created_at) returns '00'..'23'
                cursor.execute("""
                    SELECT strftime('%H', created_at) as hr, SUM(amount) as total
                    FROM transactions
                    WHERE created_at IS NOT NULL
                    GROUP BY hr
                """)
                rows = cursor.fetchall()
                for row in rows:
                    if row["hr"] is not None:
                        try:
                            h = int(row["hr"])
                            hourly[h] = float(row["total"] or 0.0)
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            logger.error(f"Error computing hourly sales heatmap: {e}")

        return hourly

    def get_recent_transactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent transactions with user MAC addresses."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        t.id,
                        COALESCE(u.mac_address, 'UNKNOWN') as mac_address,
                        t.amount,
                        t.minutes,
                        t.created_at
                    FROM transactions t
                    LEFT JOIN users u ON u.id = t.user_id
                    ORDER BY t.id DESC
                    LIMIT ?
                """, (limit,))
                rows = cursor.fetchall()
                return [
                    {
                        "id": row["id"],
                        "mac_address": row["mac_address"],
                        "amount": float(row["amount"]),
                        "minutes": int(row["minutes"]),
                        "created_at": str(row["created_at"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error fetching recent transactions: {e}")
            return []

    def export_csv_data(self) -> str:
        """Export all transactions to a CSV string."""
        transactions = self.get_recent_transactions(limit=5000)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Transaction ID", "MAC Address", "Amount (PHP)", "Minutes Credited", "Timestamp"])

        for tx in transactions:
            writer.writerow([
                tx["id"],
                tx["mac_address"],
                tx["amount"],
                tx["minutes"],
                tx["created_at"],
            ])

        return output.getvalue()
