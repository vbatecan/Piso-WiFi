"""Unit tests for CoinSlotService hardware listener, pulse accumulator, and payment session manager."""

import time
from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.config import AppConfig, CoinSlotConfig
from piso_wifi.services.coin_service import CoinSlotService
from piso_wifi.web import create_app


@pytest.fixture
def mock_user_service():
    """Mock UserService for crediting coins."""
    service = MagicMock()
    service.add_time.return_value = True
    return service


@pytest.fixture
def coin_service(mock_user_service):
    """Instantiate CoinSlotService in simulated software mode."""
    cfg = CoinSlotConfig(
        enabled=True,
        mode="simulated",
        board_preset="raspberry_pi",
        signal_pin=18,
        relay_pin=23,
        pulses_per_peso=1,
        pulse_timeout_ms=100,
        session_timeout_seconds=60,
    )
    return CoinSlotService(
        config=cfg,
        user_service=mock_user_service,
        minutes_per_peso=5.0,
    )


def test_coin_service_init(coin_service):
    """Verify default initial state of CoinSlotService."""
    assert coin_service.config.enabled is True
    assert coin_service.config.mode == "simulated"
    assert coin_service.gpio_available is False
    assert coin_service.get_active_session() is None
    assert coin_service.total_pulses_detected == 0
    assert coin_service.total_pesos_credited == 0


def test_start_and_get_session(coin_service):
    """Test starting and querying an active payment countdown session."""
    result = coin_service.start_payment_session("02:00:00:aa:bb:cc", duration_seconds=45)
    assert result["success"] is True
    session = result["session"]
    assert session["mac_address"] == "02:00:00:AA:BB:CC"
    assert session["duration_seconds"] == 45
    assert session["remaining_seconds"] <= 45
    assert session["accumulated_pesos"] == 0

    active = coin_service.get_active_session()
    assert active is not None
    assert active["mac_address"] == "02:00:00:AA:BB:CC"


def test_session_expiration(coin_service):
    """Test that expired payment sessions return None."""
    with patch("time.time") as mock_time:
        mock_time.return_value = 1000.0
        coin_service.start_payment_session("02:00:00:11:22:33", duration_seconds=30)

        # Before expiry
        mock_time.return_value = 1015.0
        active = coin_service.get_active_session()
        assert active is not None
        assert active["remaining_seconds"] == 15

        # After expiry
        mock_time.return_value = 1035.0
        assert coin_service.get_active_session() is None


def test_cancel_session(coin_service):
    """Test canceling an active payment session."""
    coin_service.start_payment_session("02:00:00:11:22:33", duration_seconds=60)
    assert coin_service.get_active_session() is not None

    coin_service.cancel_payment_session()
    assert coin_service.get_active_session() is None


def test_pulse_accumulation_and_debounce_flush(coin_service, mock_user_service):
    """Test multi-pulse coin accumulation and automated crediting via user_service."""
    coin_service.start_payment_session("02:00:00:AA:BB:CC", duration_seconds=60)

    # 5 pulses for ₱5 coin
    for _ in range(5):
        coin_service.on_pulse_detected()

    assert coin_service.total_pulses_detected == 5

    # Trigger debounce flush manually to avoid waiting for thread timer in unit test
    if coin_service._debounce_timer and coin_service._debounce_timer.is_alive():
        coin_service._debounce_timer.cancel()
    coin_service._flush_pending_pulses()

    mock_user_service.add_time.assert_called_once_with("02:00:00:AA:BB:CC", 5.0)
    assert coin_service.total_pesos_credited == 5
    assert len(coin_service.recent_events) == 1
    assert coin_service.recent_events[0]["pesos"] == 5
    assert coin_service.recent_events[0]["pulses"] == 5


def test_simulate_coin_denominations(coin_service, mock_user_service):
    """Test simulating ₱1, ₱5, ₱10, ₱20 coins."""
    # ₱1
    res1 = coin_service.simulate_coin(1, mac_address="02:00:00:AA:BB:CC")
    assert res1["success"] is True
    assert res1["denomination"] == 1
    assert res1["pulses"] == 1
    assert res1["minutes_credited"] == 5.0

    # ₱10
    res10 = coin_service.simulate_coin(10, mac_address="02:00:00:AA:BB:CC")
    assert res10["success"] is True
    assert res10["denomination"] == 10
    assert res10["pulses"] == 10
    assert res10["minutes_credited"] == 50.0

    # ₱20
    res20 = coin_service.simulate_coin(20, mac_address="02:00:00:AA:BB:CC")
    assert res20["success"] is True
    assert res20["denomination"] == 20
    assert res20["pulses"] == 20
    assert res20["minutes_credited"] == 100.0


def test_simulate_invalid_denomination(coin_service):
    """Test rejected invalid denomination."""
    res = coin_service.simulate_coin(7)
    assert res["success"] is False
    assert "Invalid coin denomination" in res["error"]


def test_relay_toggle(coin_service):
    """Test energizing and de-energizing the relay."""
    assert coin_service.set_relay(True) is True
    assert coin_service._relay_state is True

    assert coin_service.set_relay(False) is True
    assert coin_service._relay_state is False


def test_get_status(coin_service):
    """Test comprehensive status dict generation."""
    status = coin_service.get_status()
    assert status["enabled"] is True
    assert status["mode"] == "simulated"
    assert status["signal_pin"] == 18
    assert status["relay_pin"] == 23
    assert status["total_pulses"] == 0
    assert status["total_pesos"] == 0
    assert status["active_session"] is None


@pytest.fixture
def app_with_coin(coin_service, mock_user_service):
    """Flask test app with coin service injected."""
    cfg = AppConfig(setup_completed=True)
    app = create_app(
        config=cfg,
        user_service=mock_user_service,
        network_controller=MagicMock(),
        coin_service=coin_service,
    )
    app.config["TESTING"] = True
    return app


def test_api_insert_coin_and_session(app_with_coin):
    """Test customer portal API endpoints: /api/coin/insert_coin, /api/coin/session, /api/coin/cancel_session."""
    client = app_with_coin.test_client()

    # 1. Start payment session
    res = client.post("/api/coin/insert_coin", json={"mac_address": "02:00:00:55:66:77"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["session"]["mac_address"] == "02:00:00:55:66:77"

    # 2. Query session
    res = client.get("/api/coin/session")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["has_active_session"] is True
    assert data["session"]["mac_address"] == "02:00:00:55:66:77"

    # 3. Cancel session
    res = client.post("/api/coin/cancel_session")
    assert res.status_code == 200
    assert res.get_json()["success"] is True

    # 4. Verify session is gone
    res = client.get("/api/coin/session")
    data = res.get_json()
    assert data["has_active_session"] is False
    assert data["session"] is None


def test_api_diagnostics_coin_endpoints(app_with_coin):
    """Test admin diagnostics endpoints: /api/diagnostics/coin/simulate, /api/diagnostics/coin/relay, /api/diagnostics/coin/status."""
    client = app_with_coin.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    # Simulate coin
    res = client.post(
        "/api/diagnostics/coin/simulate",
        json={"denomination": 5, "mac_address": "02:00:00:AA:BB:CC"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["denomination"] == 5

    # Toggle relay
    res = client.post("/api/diagnostics/coin/relay", json={"state": True})
    assert res.status_code == 200
    assert res.get_json()["relay_state"] is True

    # Get coin status
    res = client.get("/api/diagnostics/coin/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["enabled"] is True
    assert data["total_pulses"] >= 5


def test_coin_service_hardware_extensions(mock_user_service):
    """Verify that CoinSlotService invokes BuzzerService, DisplayService, and AntiFishingDetector."""
    mock_buzzer = MagicMock()
    mock_display = MagicMock()
    mock_anti_fishing = MagicMock()
    mock_anti_fishing.record_pulse.return_value = MagicMock(is_valid=True)

    cfg = CoinSlotConfig(
        enabled=True,
        mode="gpio",
        pulses_per_peso=1,
        pulse_timeout_ms=50,
        session_timeout_seconds=30,
    )
    service = CoinSlotService(
        config=cfg,
        user_service=mock_user_service,
        minutes_per_peso=5.0,
        buzzer_service=mock_buzzer,
        display_service=mock_display,
        anti_fishing_detector=mock_anti_fishing,
    )

    # 1. Start payment session triggers buzzer and display
    service.start_payment_session("02:00:00:AA:BB:CC", duration_seconds=30)
    mock_buzzer.beep_session_start.assert_called_once()
    mock_display.update_coin_session.assert_called_with(30, 0)

    # 2. Pulse detection calls anti-fishing validation
    service.on_pulse_detected(pulse_width_ms=50.0)
    mock_anti_fishing.record_pulse.assert_called_once()

    # 3. Cancel payment session triggers display idle update
    service.cancel_payment_session()
    mock_display.update_idle.assert_called_once()

    # Flush any lingering pulse from step 2
    service._flush_pending_pulses()
    mock_buzzer.reset_mock()

    # 4. Flush / simulate triggers coin beep and coin session update
    service.simulate_coin(5, mac_address="02:00:00:AA:BB:CC")
    mock_buzzer.beep_coin.assert_called_with(5)

    # 5. Service close
    service.close()
    assert service.gpio_available is False
