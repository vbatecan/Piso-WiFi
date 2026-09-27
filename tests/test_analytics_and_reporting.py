"""Unit tests for AnalyticsService, NotificationService, and /reports dashboard."""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.config import AppConfig
from piso_wifi.services.analytics_service import AnalyticsService
from piso_wifi.services.notification_service import NotificationService
from piso_wifi.web import create_app


@pytest.fixture
def temp_db_with_transactions():
    """Create a temporary SQLite database populated with test transactions."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT UNIQUE NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            minutes INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("INSERT INTO users (id, mac_address) VALUES (1, '02:00:00:AA:BB:CC')")
    c.execute("INSERT INTO users (id, mac_address) VALUES (2, '02:00:00:DD:EE:FF')")

    # Insert transactions: ₱1, ₱5, ₱10, ₱20
    c.execute("INSERT INTO transactions (user_id, amount, minutes, created_at) VALUES (1, 1.0, 5, datetime('now'))")
    c.execute("INSERT INTO transactions (user_id, amount, minutes, created_at) VALUES (1, 5.0, 25, datetime('now'))")
    c.execute("INSERT INTO transactions (user_id, amount, minutes, created_at) VALUES (2, 10.0, 50, datetime('now'))")
    c.execute("INSERT INTO transactions (user_id, amount, minutes, created_at) VALUES (2, 20.0, 100, datetime('now'))")

    conn.commit()
    conn.close()

    yield path

    if os.path.exists(path):
        os.remove(path)


def test_analytics_summary(temp_db_with_transactions):
    """Test AnalyticsService calculation of gross revenue and counts."""
    service = AnalyticsService(db_path=temp_db_with_transactions)

    summary = service.get_summary(period="all")
    assert summary["total_revenue"] == 36.0
    assert summary["total_minutes"] == 180
    assert summary["total_transactions"] == 4
    assert summary["unique_users"] == 2
    assert summary["average_ticket"] == 9.0


def test_analytics_denomination_breakdown(temp_db_with_transactions):
    """Test AnalyticsService breakdown by coin denomination."""
    service = AnalyticsService(db_path=temp_db_with_transactions)
    breakdown = service.get_denomination_breakdown()

    assert breakdown["p1"]["count"] == 1
    assert breakdown["p1"]["total_pesos"] == 1.0

    assert breakdown["p5"]["count"] == 1
    assert breakdown["p5"]["total_pesos"] == 5.0

    assert breakdown["p10"]["count"] == 1
    assert breakdown["p10"]["total_pesos"] == 10.0

    assert breakdown["p20"]["count"] == 1
    assert breakdown["p20"]["total_pesos"] == 20.0


def test_analytics_hourly_and_csv_export(temp_db_with_transactions):
    """Test hourly revenue calculation and CSV export formatting."""
    service = AnalyticsService(db_path=temp_db_with_transactions)

    # Hourly heatmap
    hourly = service.get_hourly_heatmap()
    assert isinstance(hourly, dict)
    assert len(hourly) == 24
    assert sum(hourly.values()) == 36.0

    # CSV export
    csv_data = service.export_csv_data()
    assert "Transaction ID,MAC Address,Amount (PHP)" in csv_data
    assert "02:00:00:AA:BB:CC" in csv_data
    assert "20.0" in csv_data


def test_notification_service():
    """Test NotificationService dispatching logic."""
    service = NotificationService()

    with patch.object(service, "_send_http_post", return_value=True) as mock_post:
        # Alert (blocking for test)
        res = service.send_alert(
            title="WAN Offline",
            message="Internet link eth0 disconnected",
            webhook_url="https://discord.com/api/webhooks/123/xyz",
            blocking=True,
        )
        assert res is True
        mock_post.assert_called_once()

        # Report (blocking for test)
        mock_post.reset_mock()
        res_rep = service.send_daily_report(
            summary={"total_revenue": 100.0, "total_transactions": 10, "unique_users": 5, "period": "day"},
            webhook_url="https://api.telegram.org/bot123/sendMessage",
            blocking=True,
        )
        assert res_rep is True
        mock_post.assert_called_once()


@pytest.fixture
def app_with_analytics(temp_db_with_transactions):
    """Flask app configured with AnalyticsService."""
    cfg = AppConfig(setup_completed=True, db_path=temp_db_with_transactions)
    app = create_app(config=cfg)
    app.config["TESTING"] = True
    app.config["ANALYTICS_SERVICE"] = AnalyticsService(db_path=temp_db_with_transactions)
    return app


def test_reports_web_routes(app_with_analytics):
    """Test /reports, summary API, hourly API, and CSV export."""
    client = app_with_analytics.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    # 1. Page view
    res = client.get("/reports")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Sales &amp; Revenue Analytics" in html or "Sales & Revenue Analytics" in html
    assert "All-Time Revenue" in html
    assert "₱1 Coins" in html

    # 2. JSON summary API
    res_sum = client.get("/api/reports/summary?period=all")
    assert res_sum.status_code == 200
    data = res_sum.get_json()
    assert data["success"] is True
    assert data["summary"]["total_revenue"] == 36.0

    # 3. Hourly API
    res_hr = client.get("/api/reports/hourly")
    assert res_hr.status_code == 200
    assert res_hr.get_json()["success"] is True

    # 4. CSV download
    res_csv = client.get("/api/reports/export-csv")
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.content_type
    assert "02:00:00:AA:BB:CC" in res_csv.get_data(as_text=True)
