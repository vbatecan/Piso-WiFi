"""Sales reporting and business intelligence routes for Piso-WiFi."""

import logging
from flask import Blueprint, Response, current_app, jsonify, render_template, request

from piso_wifi.services.analytics_service import AnalyticsService
from piso_wifi.services.notification_service import NotificationService
from piso_wifi.web.auth import admin_required

logger = logging.getLogger(__name__)

reports_bp = Blueprint("reports", __name__)


def _get_analytics_service() -> AnalyticsService:
    service = current_app.config.get("ANALYTICS_SERVICE")
    if service is None:
        cfg = current_app.config.get("CONFIG")
        db_path = getattr(cfg, "db_path", "config/piso_wifi.db") if cfg else "config/piso_wifi.db"
        service = AnalyticsService(db_path=db_path)
        current_app.config["ANALYTICS_SERVICE"] = service
    return service


def _get_notification_service() -> NotificationService:
    service = current_app.config.get("NOTIFICATION_SERVICE")
    if service is None:
        service = NotificationService()
        current_app.config["NOTIFICATION_SERVICE"] = service
    return service


@reports_bp.route("/reports", methods=["GET"])
@admin_required
def index():
    """Render the business intelligence and sales dashboard."""
    analytics = _get_analytics_service()
    today_summary = analytics.get_summary(period="day")
    week_summary = analytics.get_summary(period="week")
    month_summary = analytics.get_summary(period="month")
    all_summary = analytics.get_summary(period="all")
    denominations = analytics.get_denomination_breakdown()
    hourly_heatmap = analytics.get_hourly_heatmap()
    recent_txs = analytics.get_recent_transactions(limit=25)

    return render_template(
        "reports.html",
        today=today_summary,
        week=week_summary,
        month=month_summary,
        all_time=all_summary,
        denominations=denominations,
        hourly=hourly_heatmap,
        transactions=recent_txs,
    )


@reports_bp.route("/api/reports/summary", methods=["GET"])
@admin_required
def api_summary():
    """Return JSON sales metrics for a specified period."""
    analytics = _get_analytics_service()
    period = request.args.get("period", "day")
    summary = analytics.get_summary(period=period)
    return jsonify({"success": True, "summary": summary})


@reports_bp.route("/api/reports/hourly", methods=["GET"])
@admin_required
def api_hourly():
    """Return JSON hourly revenue breakdown."""
    analytics = _get_analytics_service()
    hourly = analytics.get_hourly_heatmap()
    return jsonify({"success": True, "hourly": hourly})


@reports_bp.route("/api/reports/export-csv", methods=["GET"])
@admin_required
def api_export_csv():
    """Download transactions as a CSV spreadsheet."""
    analytics = _get_analytics_service()
    csv_data = analytics.export_csv_data()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=pisowifi_transactions.csv"},
    )


@reports_bp.route("/api/reports/test-webhook", methods=["POST"])
@admin_required
def api_test_webhook():
    """Test dispatching a notification alert to a webhook URL."""
    notifier = _get_notification_service()
    data = request.get_json(silent=True) or request.form
    webhook_url = data.get("webhook_url", "").strip()

    if not webhook_url:
        return jsonify({"success": False, "error": "Webhook URL is required"}), 400

    success = notifier.test_webhook(webhook_url)
    return jsonify({"success": success})
