"""Main entrypoint for Piso-WiFi management system."""

import logging
import signal
import sys
import time
from typing import Optional, Tuple

from piso_wifi.config import AppConfig
from piso_wifi.network.controller import NetworkController
from piso_wifi.services.time_service import TimeService
from piso_wifi.services.user_service import UserService
from piso_wifi.web import create_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("piso_wifi")

# Module-level app instance for WSGI / test compatibility
default_config = AppConfig()
app = create_app(config=default_config)


def init_services(
    config: Optional[AppConfig] = None,
) -> Tuple[UserService, NetworkController, TimeService]:
    """Initialize core application services.

    Args:
        config: Application configuration. Defaults to AppConfig().

    Returns:
        Tuple[UserService, NetworkController, TimeService]: The initialized services.
    """
    if config is None:
        config = AppConfig()

    logger.info("Initializing Piso-WiFi core services...")

    # Initialize user service
    logger.info("Initializing user service...")
    user_service = UserService(config=config)
    logger.info("User service initialized successfully")

    # Initialize network controller with retry logic
    logger.info("Initializing network controller...")
    max_retries = 3
    retry_count = 0
    network_controller = None

    if not config.setup_completed:
        logger.warning(
            "====================================================================\n"
            " Piso-WiFi is unconfigured (First Run Setup Required)!\n"
            " Launching web server in Onboarding Mode.\n"
            " Please open: http://%s:%s/onboarding to complete configuration.\n"
            "====================================================================",
            config.host if config.host != "0.0.0.0" else "localhost",
            config.port,
        )
        network_controller = NetworkController(
            config=config.network,
            auto_start=False,
            skip_system_checks=True,
        )
    else:
        while retry_count < max_retries:
            try:
                network_controller = NetworkController(
                    config=config.network,
                    auto_start=True,
                )
                logger.info("Network controller initialized successfully")
                break
            except Exception as e:
                retry_count += 1
                logger.error(
                    f"Network controller initialization attempt {retry_count}/{max_retries} failed: {e}"
                )
                if retry_count >= max_retries:
                    raise
                time.sleep(5)

    # Initialize time service
    logger.info("Initializing time service...")
    time_service = TimeService(
        user_service=user_service,
        network_controller=network_controller,
        check_interval=config.check_interval,
    )
    logger.info("Time service initialized successfully")

    return user_service, network_controller, time_service


def main():
    """Main application runner."""
    try:
        config = AppConfig()
        logger.info("Starting Piso-WiFi application...")

        # Initialize services
        user_service, network_controller, time_service = init_services(config)

        # Create web application with initialized services
        web_app = create_app(
            config=config,
            user_service=user_service,
            network_controller=network_controller,
        )

        # Handle graceful shutdown
        def handle_signal(sig, frame):
            logger.info("Shutdown signal received, stopping services...")
            try:
                time_service.stop()
            except Exception as e:
                logger.debug("Error stopping time service: %s", e)

            coin_svc = web_app.config.get("COIN_SERVICE")
            if coin_svc and hasattr(coin_svc, "close"):
                try:
                    coin_svc.close()
                except Exception as e:
                    logger.debug("Error closing coin service: %s", e)

            buzzer_svc = web_app.config.get("BUZZER_SERVICE")
            if buzzer_svc and hasattr(buzzer_svc, "close"):
                try:
                    buzzer_svc.close()
                except Exception as e:
                    logger.debug("Error closing buzzer service: %s", e)

            display_svc = web_app.config.get("DISPLAY_SERVICE")
            if display_svc and hasattr(display_svc, "close"):
                try:
                    display_svc.close()
                except Exception as e:
                    logger.debug("Error closing display service: %s", e)

            sys.exit(0)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        # Start background time service if setup is complete
        if config.setup_completed:
            logger.info("Starting background time service...")
            time_service.start()
        else:
            logger.info("Setup incomplete; background time service deferred until onboarding is finished.")


        # Run web server
        logger.info(f"Starting web server on {config.host}:{config.port}...")
        web_app.run(
            host=config.host,
            port=config.port,
            debug=config.debug,
            use_reloader=False,
        )

    except Exception as e:
        logger.critical(f"Fatal startup error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()