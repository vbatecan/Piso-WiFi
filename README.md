# PISO WIFI System

A Python-based PISO WIFI management system designed for Orange Pi One that enables pay-per-use WiFi access control. The system allows users to purchase internet time credits and automatically manages their access based on remaining balance.

## Features

- Pay-per-use WiFi access (1 peso = 1 minute default, configurable)
- MAC address-based device tracking and access control
- Bandwidth management with QoS traffic shaping (`default` vs `premium` plans)
- Automatic access blocking when time balance depletes
- Transaction logging and audit deduction history
- Web-based management dashboard and admin panel
- **Flexible Network Topologies (LAN-to-WLAN Production Standard)**:
  - **Topology A (Ethernet WAN to Wireless AP)**: Connect ISP fiber/router via RJ-45 LAN cable (`eth0`) and broadcast hotspot via WiFi (`wlan0`). Delivers zero RF contention, full line-rate speeds, and carrier-drop detection.
  - **Topology B (Dual Wireless WLAN-to-WLAN)**: Repeats upstream WiFi (`wlan1`) out to customer hotspot (`wlan0`).
  - **Topology C (Cellular LTE-to-WLAN)**: 4G/5G USB modem dongle (`usb0`/`wwan0`) to WiFi (`wlan0`).
  - **Smart Automatic Uplink Detection (`INTERNET_INTERFACE=auto`)**: Dynamically resolves default gateway routes and physical carrier state.
- **Operator & Administrator Documentation**: Complete hardware wiring, deployment, and troubleshooting guide in the [**Piso-WiFi Operator & Administrator User Manual**](docs/USER_MANUAL.md).
- **Modular OOP Architecture**:
  - Strongly-typed Domain Models and Enums (`PlanType`, `UserStatus`, `DeductionType`, `DeviceSignalQuality`)
  - Typed Configuration (`AppConfig`, `NetworkConfig`, `BandwidthConfig`)
  - Thread-safe Database Repository pattern for SQLite (`DatabaseManager`, `UserRepository`)
  - Pluggable Network Subsystem with Command Runner abstraction (`FirewallManager`, `TrafficShaper`, `AccessPointManager`, `DeviceDiscovery`, `NetworkController`)
  - Separated Business Logic (`UserService`) and Background Session Loop (`TimeService`)
  - Modern Flask Blueprints (`auth`, `dashboard`, `debug`) with Application Factory (`create_app`)
- **100% Mockable Test Suite**: 96 unit and integration tests executable without root privileges or physical WiFi hardware
- Containerized development environment (Docker Compose)
- Production-ready deployment for Orange Pi One and Linux SBCs

## Documentation & Manuals

- [📖 **Piso-WiFi Operator & Administrator User Manual**](docs/USER_MANUAL.md) — Comprehensive guide covering hardware topologies, Orange Pi / Raspberry Pi setup, Allan 1239 coin selector wiring, `.env` configuration, captive portal operation, bandwidth shaping, and troubleshooting.

## Screenshots
<img width="1791" height="902" alt="image" src="https://github.com/user-attachments/assets/b7fec1e2-3ef3-42b9-ac1a-9ada3d66fabb" />
<img width="1793" height="886" alt="image" src="https://github.com/user-attachments/assets/1ba90ede-76c4-463d-b5a2-110b8555a582" />
<img width="1790" height="900" alt="image" src="https://github.com/user-attachments/assets/7a70efc9-39e6-4d3c-9ea3-e551d0c0ea69" />
<img width="1811" height="900" alt="image" src="https://github.com/user-attachments/assets/0edc74ff-ef1f-4758-99ca-6f01a8bc6101" />


## Architecture & Project Structure

```
Piso-WiFi/
├── main.py                     # Application entrypoint & service orchestration
├── docs/
│   └── USER_MANUAL.md          # Comprehensive Operator & Administrator Manual
├── user_manager.py             # Backward-compatibility facade -> UserService
├── network_controller.py       # Backward-compatibility facade -> NetworkController
├── time_manager.py             # Backward-compatibility facade -> TimeService
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Container definition
├── docker-compose.yml          # Containerized deployment
├── templates/                  # Jinja2 HTML templates
│   ├── base.html
│   ├── index.html
│   └── login.html
├── piso_wifi/                  # Core modular package
│   ├── config.py               # AppConfig, NetworkConfig, BandwidthConfig
│   ├── models/
│   │   ├── enums.py            # PlanType, UserStatus, DeductionType, SignalQuality
│   │   └── entities.py         # User, Transaction, TimeLog, DeviceInfo, BandwidthLimit
│   ├── database/
│   │   ├── connection.py       # Thread-safe DatabaseManager connection manager
│   │   └── repository.py       # UserRepository data access layer
│   ├── network/
│   │   ├── command_runner.py   # CommandRunner interface (System & Mock runners)
│   │   ├── firewall.py         # FirewallManager (iptables NAT, forwarding, rules)
│   │   ├── qos.py              # TrafficShaper / QoSManager (tc HTB / SFQ shaping)
│   │   ├── access_point.py     # AccessPointManager (hostapd & dnsmasq configs)
│   │   ├── discovery.py        # DeviceDiscovery (leases & station dumps)
│   │   └── controller.py       # NetworkController facade with DI
│   ├── services/
│   │   ├── user_service.py     # UserService business logic & account operations
│   │   └── time_service.py     # TimeService background daemon thread
│   └── web/
│       ├── app.py              # create_app Flask application factory
│       ├── auth.py             # @admin_required authentication guard
│       └── routes/
│           ├── auth.py         # /login, /logout routes
│           ├── dashboard.py    # /, /add_time, /deduct_time, /manage_plan, etc.
│           └── debug.py        # /debug/connections diagnostics
└── tests/
    ├── test_network_controller.py
    ├── test_network_modular.py
    ├── test_repository.py
    ├── test_time_service.py
    ├── test_user_manager.py
    └── test_web.py
```

## Running & Testing

### 1. Isolated Testing with `pytest` (Zero Root Required)

All network interactions are decoupled and mockable:

```bash
# Run the complete test suite (90 tests)
pytest

# Or run with Python module
python3 -m pytest -v
```

### 2. Testing Without Installing Services (Mock Dev Mode)

To explore and test the web dashboard without needing `hostapd`, `dnsmasq`, or root `iptables`:

```bash
# Start in mock mode with uv (no clutter, isolated dependencies)
MOCK_NETWORK=1 uv run python main.py

# Or with standard Python / virtualenv
MOCK_NETWORK=1 python3 main.py
```
Open `http://localhost:5000` in your browser.

### 3. Running with Docker Compose (Isolated Container)

```bash
# Start containerized Piso-WiFi
docker compose up --build

# Stop and clean up containers/volumes
docker compose down -v
```

### 4. Production Hardware Deployment (Orange Pi / Linux SBC)

1. Flash the latest Armbian OS to your Orange Pi One
2. Install system dependencies:
   ```bash
   sudo apt update
   sudo apt install python3-pip hostapd dnsmasq
   ```

3. Clone and install the application as described in Quick Start
4. Configure the WiFi interface:
   ```bash
   sudo ./scripts/setup_wifi.sh
   ```

5. Enable and start the services:
   ```bash
   sudo systemctl enable pisowifi
   sudo systemctl start pisowifi
   ```

## Configuration

Key configuration options in `.env` (see [User Manual Configuration Guide](docs/USER_MANUAL.md#section-3-configuration--deployment) for complete details):

- `WIFI_INTERFACE`: Wireless interface for hotspot broadcast (default: `wlan0`)
- `INTERNET_INTERFACE`: Upstream WAN interface (`auto` for auto-detection, `eth0` for wired Ethernet, `wlan1` for WiFi repeater, `usb0` for LTE modem)
- `AP_SSID`: Broadcast WiFi network name (default: `PisoWiFi`)
- `AP_PASSWORD`: WiFi access password (default: `pisowifi123`)
- `AP_IP`: Gateway IP address for captive portal (default: `192.168.4.1`)
- `RATE_PESOS_PER_MINUTE`: Time conversion rate (default: `0.2` = 5 minutes per 1 Peso)
- `ADMIN_USERNAME`: Administrator login username (default: `admin`)
- `ADMIN_PASSWORD`: Administrator login password (default: `admin123`)
- `DATABASE_URL`: SQLite database path (default: `sqlite:///config/piso_wifi.db`)

## API Documentation

The system provides a REST API for integration:

- `POST /api/v1/purchase`: Add credit to a device
- `GET /api/v1/devices`: List connected devices
- `GET /api/v1/balance`: Check remaining balance

See the [API documentation](docs/api.md) for detailed endpoints and usage.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For support and questions:
- Open an issue on GitHub
- Join our [Discord community](https://discord.gg/pisowifi)
- Email: support@pisowifi.com

## Acknowledgments

- Orange Pi community
- Contributors and testers
- Open source projects used in this system

### Windows-Specific Setup

For Windows users, a special Docker configuration is provided that uses bridge networking instead of WiFi interfaces:

1. Make sure you have Docker Desktop for Windows installed and running

2. Use Windows Terminal or PowerShell to run these commands:
   ```powershell
   # Clone the repository
   git clone https://github.com/llTheBlankll/piso-wifi.git
   cd piso-wifi

   # Start using Windows configuration
   docker-compose -f docker-compose.windows.yml up -d
   ```

3. Verify the setup:
   ```powershell
   # Check container status
   docker ps | findstr piso-wifi

   # Check container logs
   docker-compose -f docker-compose.windows.yml logs -f
   ```

4. Access the application:
   - Web interface: http://localhost:5000
   - API endpoint: http://localhost:5000/api/v1

5. Stop the container:
   ```powershell
   docker-compose -f docker-compose.windows.yml down
   ```

Note: The Windows configuration uses bridge networking instead of direct WiFi interface access. This is suitable for development and testing, but for production deployment, use the Linux configuration on Orange Pi or similar hardware.

Common Issues on Windows:
- If you get permission errors, make sure Docker Desktop has Windows Defender Firewall access
- If the container can't start, try restarting Docker Desktop
- Make sure Hyper-V and WSL2 are properly enabled in Windows features
