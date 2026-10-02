# Print Server Docker

A self-contained CUPS appliance with a Home Assistant-style setup experience.
Run one container, open port `8080`, create the owner account, and let the
server discover and configure driverless IPP printers.

## Features

- First-run web onboarding on port `8080`
- Password-protected management interface
- CUPS IPP server on port `631`
- Automatic Bonjour/DNS-SD printer discovery
- Private-subnet IPP discovery fallback for Docker Desktop
- Manual `ipp://` and `ipps://` setup
- Driverless IPP Everywhere queues
- Default-printer selection and test-page printing
- Persistent accounts, settings, CUPS queues, logs, and spool
- Multi-platform image for `linux/amd64` and `linux/arm64`

## Quick start

Clone the repository and run:

```sh
docker compose up -d
```

Or run the published test image directly:

```sh
docker run -d \
  --name print-server \
  --restart unless-stopped \
  -p 631:631 \
  -p 8080:8080 \
  -v print-server-data:/data \
  -v print-server-config:/etc/cups \
  -v print-server-logs:/var/log/cups \
  -v print-server-spool:/var/spool/cups \
  avichadda/print-server-docker:test
```

Open:

```text
http://DOCKER_HOST_IP:8080
```

The setup wizard asks for:

1. An administrator username
2. A password of at least 10 characters
3. An optional private network to scan, such as `192.168.1.0/24`

After setup, the server searches for printers and presents compatible devices
with an **Add** button.

## Printer discovery

The appliance uses two discovery methods.

### Bonjour/DNS-SD

`ippfind` and Avahi discover `_ipp._tcp` and `_ipps._tcp` services. Multicast
discovery works best with host networking on a native Linux or NAS Docker
host:

```sh
docker run -d \
  --name print-server \
  --restart unless-stopped \
  --network host \
  -v print-server-data:/data \
  -v print-server-config:/etc/cups \
  -v print-server-logs:/var/log/cups \
  -v print-server-spool:/var/spool/cups \
  avichadda/print-server-docker:test
```

With host networking, the interface remains available on port `8080` and CUPS
on port `631`.

### Private-subnet fallback

Docker Desktop and bridged Docker networks commonly do not forward multicast
DNS from the LAN. The setup interface can scan up to 1,024 addresses in
private IPv4 ranges for IPP on TCP port 631 and test common driverless printer
endpoints.

Example:

```text
192.168.1.0/24
```

Discovery is restricted to private IPv4 networks. Public ranges and scans
larger than 1,024 addresses are rejected.

## Existing Canon G3010 setup

The Canon used while developing this appliance was discovered at:

```text
ipp://192.168.1.22/ipp/print
```

The resulting shared CUPS queue is available to clients at:

```text
ipp://DOCKER_HOST_IP:631/printers/Canon_G3010
```

The interface also supports adding this URI manually if discovery is
unavailable.

## Optional environment configuration

The web interface is the preferred setup method. Environment variables remain
available for unattended or backward-compatible deployment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PRINTER_URI` | empty | Create a queue automatically at startup |
| `PRINTER_NAME` | `Canon_G3010` | Automatic queue name |
| `PRINTER_MODEL` | `everywhere` | CUPS model for automatic setup |
| `PRINTER_INFO` | `Canon G3010` | Automatic queue display name |
| `PRINTER_LOCATION` | empty | Automatic queue location |
| `ENABLE_AVAHI` | `true` | Run D-Bus and Avahi for DNS-SD |
| `CUPS_START_TIMEOUT` | `20` | Service startup timeout in seconds |
| `PRINT_SERVER_DATA_DIR` | `/data` | Manager database and secret storage |

## Persistent data

The supplied Compose configuration stores:

| Volume | Container path | Contents |
| --- | --- | --- |
| `manager-data` | `/data` | Owner account, settings, session secret |
| `cups-config` | `/etc/cups` | CUPS configuration and queues |
| `cups-logs` | `/var/log/cups` | CUPS logs |
| `cups-spool` | `/var/spool/cups` | Queued print jobs |

Do not remove these volumes during a normal upgrade. Running
`docker compose down -v` permanently deletes the owner account, queues, and
pending jobs.

## Client setup

Add a configured queue using:

```text
ipp://DOCKER_HOST_IP:631/printers/QUEUE_NAME
```

For the original server and Canon queue:

```text
ipp://192.168.1.5:631/printers/Canon_G3010
```

macOS command-line example:

```sh
sudo lpadmin \
  -p Docker_Canon \
  -E \
  -v ipp://192.168.1.5:631/printers/Canon_G3010 \
  -m everywhere
```

## Operations

Check service health:

```sh
docker compose ps
curl --fail http://localhost:8080/api/health
```

Inspect queues:

```sh
docker compose exec cups lpstat -t
```

View logs:

```sh
docker compose logs cups
docker compose exec cups tail -n 100 /var/log/cups/error_log
```

## Build and test

Build locally:

```sh
docker build -t avichadda/print-server-docker:test .
```

Run the Python regression tests inside the image:

```sh
docker run --rm \
  --entrypoint python3 \
  -e PYTHONPATH=/workspace \
  -v "$PWD:/workspace" \
  avichadda/print-server-docker:test \
  -m unittest discover -s /workspace/tests -v
```

Publish both supported platforms:

```sh
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag avichadda/print-server-docker:test \
  --push \
  .
```

## Security

- The setup interface requires an owner account.
- Passwords are stored using Werkzeug's password hashing.
- State-changing forms require a session-bound CSRF token.
- Login attempts are rate-limited per client address.
- Printer names and IPP URIs are validated before invoking CUPS commands.
- Subnet discovery is restricted to bounded private IPv4 networks.
- Remote CUPS administration remains disabled.
- No administrator credentials are embedded in the image.

This appliance is intended for a trusted local network. Do not publish ports
`631` or `8080` directly to the internet.
