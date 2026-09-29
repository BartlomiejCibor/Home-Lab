#!/usr/bin/env python3
"""Read-only Proxmox cluster morning report. Stdout is delivered verbatim by cron."""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

NODES = [
    {"label": "Node 1 — Dell", "name": "Serwer", "ip": "10.0.10.12"},
    {"label": "Node 2 — Lenovo", "name": "MT2ThinkCentre", "ip": "10.0.10.11"},
]


def run(command: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 125, "", str(exc)


def ssh(ip: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
    return run([
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
        "-o", "ServerAliveInterval=5", f"root@{ip}", command,
    ], timeout)


def json_ssh(ip: str, command: str, timeout: int = 30):
    rc, out, err = ssh(ip, command, timeout)
    if rc != 0:
        raise RuntimeError(err or out or f"SSH rc={rc}")
    return json.loads(out)


def gib(value: float | int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def pct(used: float, total: float) -> float:
    return 100.0 * used / total if total else 0.0


def badge_percent(value: float) -> str:
    if value >= 95:
        return "🔴"
    if value >= 85:
        return "⚠️"
    return "🟢"


def duration(seconds: int | float) -> str:
    seconds = int(seconds or 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days} d {hours} h {minutes} min"


def smart_disks(ip: str) -> list[dict]:
    rc, out, _ = ssh(ip, "smartctl --scan-open 2>/dev/null", 30)
    if rc not in (0, 4, 64) and not out:
        return []
    devices = []
    for line in out.splitlines():
        token = line.split()
        if token and token[0].startswith("/dev/") and token[0] not in devices:
            devices.append(token[0])

    results = []
    for dev in devices:
        rc, raw, err = ssh(ip, f"smartctl -j -i -H -A {dev}", 40)
        try:
            data = json.loads(raw)
        except Exception:
            results.append({"device": dev, "error": err or raw or f"rc={rc}"})
            continue
        attrs = {}
        for item in data.get("ata_smart_attributes", {}).get("table", []):
            attrs[item.get("name", "")] = item.get("raw", {}).get("value")
        def attr(*names):
            for name in names:
                if name in attrs:
                    return attrs[name]
            return None
        results.append({
            "device": dev,
            "model": data.get("model_name", "nieznany model"),
            "passed": data.get("smart_status", {}).get("passed"),
            "temp": data.get("temperature", {}).get("current"),
            "hours": data.get("power_on_time", {}).get("hours", attr("Power_On_Hours")),
            "reallocated": attr("Reallocated_Sector_Ct", "Reallocated_Event_Count"),
            "pending": attr("Current_Pending_Sector"),
            "uncorrectable": attr("Offline_Uncorrectable", "Reported_Uncorrect"),
            "crc": attr("UDMA_CRC_Error_Count"),
            "life": attr("Remaining_Lifetime_Perc", "SSD_Life_Left"),
            "percentage_used": data.get("nvme_smart_health_information_log", {}).get("percentage_used"),
            "exit": rc,
        })
    return results


def smart_line(disk: dict) -> str:
    if "error" in disk:
        return f"🔴 `{disk['device']}` — brak odczytu SMART: {disk['error']}"
    passed = disk.get("passed")
    reallocated = disk.get("reallocated") or 0
    pending = disk.get("pending") or 0
    uncorrectable = disk.get("uncorrectable") or 0
    temp = disk.get("temp")
    if passed is False or pending or uncorrectable:
        icon = "🔴"
    elif reallocated or (isinstance(temp, (int, float)) and temp >= 50):
        icon = "⚠️"
    else:
        icon = "🟢"
    details = [f"SMART: {'PASSED' if passed else 'UNKNOWN/FAIL'}"]
    if temp is not None:
        details.append(f"{temp}°C")
    if disk.get("hours") is not None:
        details.append(f"{disk['hours']} h")
    details.append(f"realloc/pending/uncorr: {reallocated}/{pending}/{uncorrectable}")
    if disk.get("life") is not None:
        details.append(f"life: {disk['life']}%")
    elif disk.get("percentage_used") is not None:
        details.append(f"NVMe used: {disk['percentage_used']}%")
    return f"{icon} `{disk['device']}` {disk['model']} — " + ", ".join(details)


def qga_status(node_ip: str, vmid: int) -> str:
    rc, _, _ = ssh(node_ip, f"qm agent {vmid} ping", 12)
    return "🟢 QGA" if rc == 0 else "⚠️ QGA niedostępny"


def tcp_status(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def main() -> int:
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    print("## 📋 Codzienny raport klastra Proxmox")
    print(f"**Data:** {now:%Y-%m-%d %H:%M:%S} Europe/Warsaw\n")

    node_data = {}
    for node in NODES:
        try:
            status = json_ssh(node["ip"], f"pvesh get /nodes/{node['name']}/status --output-format json")
            node_data[node["name"]] = {"online": True, "status": status, "smart": smart_disks(node["ip"])}
        except Exception as exc:
            node_data[node["name"]] = {"online": False, "error": str(exc), "smart": []}

    resources = []
    try:
        resources = json_ssh("10.0.10.11", "pvesh get /cluster/resources --type vm --output-format json", 40)
    except Exception:
        try:
            resources = json_ssh("10.0.10.12", "pvesh get /cluster/resources --type vm --output-format json", 40)
        except Exception:
            pass

    for node in NODES:
        info = node_data[node["name"]]
        print(f"## {node['label']} `{node['ip']}`")
        if not info["online"]:
            print(f"🔴 **OFFLINE** — {info['error']}\n")
            continue
        st = info["status"]
        memory = st.get("memory", {})
        swap = st.get("swap", {})
        root = st.get("rootfs", {})
        mem_p = pct(memory.get("used", 0), memory.get("total", 0))
        swap_p = pct(swap.get("used", 0), swap.get("total", 0))
        root_p = pct(root.get("used", 0), root.get("total", 0))
        cpu_p = float(st.get("cpu", 0)) * 100
        load = "/".join(str(x) for x in st.get("loadavg", []))
        print(f"🟢 **ONLINE** — `{st.get('pveversion', 'wersja nieznana')}`")
        print(f"- 🟢 CPU: **{cpu_p:.1f}%**, load: `{load}`, uptime: {duration(st.get('uptime', 0))}")
        print(f"- {badge_percent(mem_p)} RAM: **{mem_p:.1f}%** ({gib(memory.get('used', 0))}/{gib(memory.get('total', 0))})")
        print(f"- {badge_percent(swap_p)} Swap: **{swap_p:.1f}%** ({gib(swap.get('used', 0))}/{gib(swap.get('total', 0))})")
        print(f"- {badge_percent(root_p)} Root: **{root_p:.1f}%** ({gib(root.get('used', 0))}/{gib(root.get('total', 0))})")
        print("\n### Dyski SMART")
        if info["smart"]:
            for disk in info["smart"]:
                print(f"- {smart_line(disk)}")
        else:
            print("- ⚠️ Brak danych SMART")

        if node["name"] == "Serwer":
            rc, zpool, _ = ssh(node["ip"], "zpool status -x 2>/dev/null", 20)
            if rc == 0:
                icon = "🟢" if "healthy" in zpool.lower() else "⚠️"
                print(f"- {icon} ZFS: {zpool.splitlines()[0] if zpool else 'brak pooli'}")

        print("\n### VM/LXC")
        guests = sorted((r for r in resources if r.get("node") == node["name"]), key=lambda x: int(x.get("vmid", 0)))
        if not guests:
            print("- ⚠️ Brak danych o gościach")
        for guest in guests:
            running = guest.get("status") == "running"
            icon = "🟢" if running else "⚪"
            typ = "VM" if guest.get("type") == "qemu" else "LXC"
            base = f"{icon} **{typ} {guest.get('vmid')} — {guest.get('name', '-')}**: {guest.get('status')}"
            if running:
                cpu = float(guest.get("cpu") or 0) * 100
                ram = pct(guest.get("mem") or 0, guest.get("maxmem") or 0)
                if int(guest.get("vmid", 0)) == 100:
                    base += f", CPU {cpu:.1f}%, RAM przydzielona przez PVE {gib(guest.get('maxmem') or 0)} (brak wiarygodnej metryki gościa)"
                else:
                    base += f", CPU {cpu:.1f}%, RAM {ram:.1f}% ({gib(guest.get('mem') or 0)}/{gib(guest.get('maxmem') or 0)})"
                if guest.get("type") == "lxc" and guest.get("maxdisk"):
                    diskp = pct(guest.get("disk") or 0, guest.get("maxdisk") or 0)
                    base += f", dysk {diskp:.1f}% ({gib(guest.get('disk') or 0)}/{gib(guest.get('maxdisk') or 0)})"
                elif guest.get("maxdisk"):
                    base += f", dysk przydzielony {gib(guest.get('maxdisk') or 0)}"
                if guest.get("type") == "qemu":
                    base += f", {qga_status(node['ip'], int(guest['vmid']))}"
            print(f"- {base}")
        print()

    print("## Klaster i usługi")
    rc, quorum, _ = ssh("10.0.10.11", "pvecm status", 20)
    if rc == 0:
        quorate = "Quorate:          Yes" in quorum or "Quorate: Yes" in quorum
        expected = total = "?"
        for line in quorum.splitlines():
            if line.strip().startswith("Expected votes:"):
                expected = line.split(":", 1)[1].strip()
            elif line.strip().startswith("Total votes:"):
                total = line.split(":", 1)[1].strip()
        print(f"- {'🟢' if quorate else '🔴'} Quorum: **{'TAK' if quorate else 'NIE'}**, głosy {total}/{expected}")
    else:
        print("- 🔴 Nie udało się odczytać quorum")
    rc, qdev, _ = ssh("10.0.10.11", "systemctl is-active corosync-qdevice", 15)
    print(f"- {'🟢' if qdev == 'active' else '⚠️'} QDevice: **{qdev or 'nieznany'}**")

    services = [
        ("Grafana", "10.0.30.10", 3000),
        ("Prometheus", "10.0.30.12", 9090),
        ("qBittorrent", "10.0.30.14", 8080),
        ("OPNsense GUI", "10.0.10.1", 443),
    ]
    for name, host, port in services:
        ok = tcp_status(host, port)
        print(f"- {'🟢' if ok else '🔴'} {name}: `{host}:{port}` {'dostępny' if ok else 'niedostępny'}")

    print("\n## Zalecenia")
    warnings = []
    if qdev != "active":
        warnings.append("⚠️ QDevice jest nieaktywny; klaster działa na dwóch głosach i pozostaje podatny na utratę quorum po awarii jednego noda.")
    for node in NODES:
        for disk in node_data[node["name"]].get("smart", []):
            if disk.get("passed") is False or (disk.get("pending") or 0) or (disk.get("uncorrectable") or 0):
                warnings.append(f"🔴 Pilnie sprawdź {node['name']} {disk.get('device')}: SMART/media errors.")
    if not warnings:
        warnings.append("🟢 Brak nowych krytycznych problemów w odczytanych danych.")
    for warning in warnings:
        print(f"- {warning}")
    print("- ℹ️ Firewall PVE jest celowo zastąpiony filtracją OPNsense.")
    print("- ℹ️ Retencja `keep-last=1` pozostaje świadomie zaakceptowanym ryzykiem pojemnościowym.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
