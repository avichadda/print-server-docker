from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


QUEUE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
ATTRIBUTE_PATTERN = re.compile(
    r"^\s*(printer-info|printer-make-and-model|printer-name)"
    r" \([^)]*\) = (.+)$",
    re.MULTILINE,
)
IPP_TEST = Path("/usr/share/cups/ipptool/get-printer-attributes.test")


class PrinterServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveredPrinter:
    name: str
    uri: str
    model: str
    source: str


def run_command(
    arguments: list[str], *, timeout: int = 30, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PrinterServiceError(
            f"Command timed out after {timeout} seconds: {arguments[0]}"
        ) from error
    except OSError as error:
        raise PrinterServiceError(
            f"Unable to execute {arguments[0]}: {error}"
        ) from error

    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise PrinterServiceError(
            message or f"{arguments[0]} exited with status {result.returncode}"
        )
    return result


def validate_queue_name(name: str) -> str:
    value = name.strip()
    if not QUEUE_NAME_PATTERN.fullmatch(value):
        raise PrinterServiceError(
            "Queue names must start with a letter or number and contain only "
            "letters, numbers, periods, underscores, or hyphens."
        )
    return value


def validate_printer_uri(uri: str) -> str:
    value = uri.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"ipp", "ipps"} or not parsed.hostname:
        raise PrinterServiceError(
            "Printer URI must be an ipp:// or ipps:// address with a hostname."
        )
    if parsed.username or parsed.password:
        raise PrinterServiceError("Credentials are not allowed in printer URIs.")
    return value


def validate_subnets(raw_subnets: str) -> list[ipaddress.IPv4Network]:
    values = [value.strip() for value in raw_subnets.split(",") if value.strip()]
    networks: list[ipaddress.IPv4Network] = []
    address_count = 0

    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as error:
            raise PrinterServiceError(f"Invalid discovery subnet: {value}") from error
        if not isinstance(network, ipaddress.IPv4Network):
            raise PrinterServiceError("Only IPv4 discovery subnets are supported.")
        if not network.is_private:
            raise PrinterServiceError(
                f"Discovery is restricted to private networks: {value}"
            )
        address_count += max(network.num_addresses - 2, 0)
        if address_count > 1024:
            raise PrinterServiceError(
                "Discovery is limited to 1,024 addresses at a time."
            )
        networks.append(network)

    return networks


def list_printers() -> list[dict[str, object]]:
    devices = run_command(["lpstat", "-v"], check=False)
    states = run_command(["lpstat", "-p"], check=False)
    default_result = run_command(["lpstat", "-d"], check=False)

    uris: dict[str, str] = {}
    for line in devices.stdout.splitlines():
        match = re.match(r"^device for ([^:]+): (.+)$", line)
        if match:
            uris[match.group(1)] = match.group(2)

    printer_states: dict[str, tuple[str, bool]] = {}
    for line in states.stdout.splitlines():
        match = re.match(
            r"^printer (\S+) (?:is (idle)|now printing .+|disabled)(?:\.| )(.*)$",
            line,
        )
        if match:
            status = "idle" if match.group(2) else "busy"
            printer_states[match.group(1)] = (status, "disabled" not in line)

    default_name = ""
    default_match = re.search(
        r"system default destination: (\S+)", default_result.stdout
    )
    if default_match:
        default_name = default_match.group(1)

    return [
        {
            "name": name,
            "uri": uri,
            "status": printer_states.get(name, ("unknown", True))[0],
            "enabled": printer_states.get(name, ("unknown", True))[1],
            "default": name == default_name,
        }
        for name, uri in sorted(uris.items())
    ]


def add_printer(
    name: str,
    uri: str,
    info: str,
    location: str,
    *,
    set_default: bool,
) -> None:
    queue_name = validate_queue_name(name)
    printer_uri = validate_printer_uri(uri)
    arguments = [
        "lpadmin",
        "-p",
        queue_name,
        "-E",
        "-v",
        printer_uri,
        "-m",
        "everywhere",
        "-D",
        info.strip()[:127] or queue_name,
        "-L",
        location.strip()[:127],
        "-o",
        "printer-is-shared=true",
    ]
    run_command(arguments, timeout=60)
    run_command(["cupsaccept", queue_name])
    run_command(["cupsenable", queue_name])
    if set_default:
        run_command(["lpadmin", "-d", queue_name])


def remove_printer(name: str) -> None:
    run_command(["lpadmin", "-x", validate_queue_name(name)])


def set_default_printer(name: str) -> None:
    run_command(["lpadmin", "-d", validate_queue_name(name)])


def print_test_page(name: str) -> str:
    queue_name = validate_queue_name(name)
    result = run_command(
        ["lp", "-d", queue_name, "/usr/share/cups/data/testprint"],
        timeout=30,
    )
    return result.stdout.strip()


def discover_printers(raw_subnets: str) -> tuple[list[DiscoveredPrinter], list[str]]:
    discoveries: dict[str, DiscoveredPrinter] = {}
    warnings: list[str] = []

    try:
        for printer in _discover_dnssd():
            discoveries[printer.uri] = printer
    except PrinterServiceError as error:
        warnings.append(f"DNS-SD discovery unavailable: {error}")

    networks = validate_subnets(raw_subnets)
    if networks:
        for printer in _discover_subnets(networks):
            discoveries.setdefault(printer.uri, printer)

    return sorted(discoveries.values(), key=lambda item: item.name.lower()), warnings


def _discover_dnssd() -> list[DiscoveredPrinter]:
    command = [
        "ippfind",
        "-T",
        "5",
        "_ipp._tcp",
        "_ipps._tcp",
        "--exec",
        "printf",
        "%s\\t%s\\n",
        "{service_name}",
        "{}",
        ";",
    ]
    result = run_command(command, timeout=10, check=False)
    if result.returncode not in {0, 1}:
        raise PrinterServiceError(result.stderr.strip() or "ippfind failed")

    printers: list[DiscoveredPrinter] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            uri = validate_printer_uri(parts[1])
        except PrinterServiceError:
            continue
        printers.append(
            DiscoveredPrinter(
                name=parts[0].strip() or urlsplit(uri).hostname or "IPP Printer",
                uri=uri,
                model="Driverless IPP",
                source="DNS-SD",
            )
        )
    return printers


def _discover_subnets(
    networks: list[ipaddress.IPv4Network],
) -> list[DiscoveredPrinter]:
    addresses = [
        str(address)
        for network in networks
        for address in network.hosts()
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        open_hosts = [
            address
            for address, is_open in zip(
                addresses, executor.map(_ipp_port_open, addresses)
            )
            if is_open
        ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(_probe_ipp_host, open_hosts))
    return [printer for printer in results if printer is not None]


def _ipp_port_open(address: str) -> bool:
    try:
        with socket.create_connection((address, 631), timeout=0.35):
            return True
    except OSError:
        return False


def _probe_ipp_host(address: str) -> DiscoveredPrinter | None:
    for path in ("/ipp/print", "/ipp/printer", "/ipp/port1"):
        uri = f"ipp://{address}{path}"
        try:
            result = run_command(
                ["ipptool", "-tv", uri, str(IPP_TEST)],
                timeout=6,
                check=False,
            )
        except PrinterServiceError:
            continue
        if result.returncode != 0:
            continue

        attributes = {
            match.group(1): match.group(2).strip()
            for match in ATTRIBUTE_PATTERN.finditer(result.stdout)
        }
        name = (
            attributes.get("printer-info")
            or attributes.get("printer-name")
            or f"IPP Printer {address}"
        )
        model = attributes.get("printer-make-and-model", "Driverless IPP")
        return DiscoveredPrinter(
            name=name,
            uri=uri,
            model=model,
            source="Network scan",
        )
    return None
