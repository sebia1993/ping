from __future__ import annotations

import ipaddress
import re


IPV4_ONLY_MESSAGE = "IPv4 주소만 입력 가능합니다."


def validate_target(target: str) -> tuple[bool, str]:
    return validate_ipv4_address(target)


def validate_ipv4_address(target: str) -> tuple[bool, str]:
    value = target.strip()
    if not value:
        return False, "대상 IPv4 주소를 입력하세요."
    if any(char.isspace() for char in value):
        return False, "IPv4 주소에는 공백을 포함할 수 없습니다."
    if "://" in value or "/" in value:
        return False, IPV4_ONLY_MESSAGE

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False, IPV4_ONLY_MESSAGE

    if address.version != 4:
        return False, IPV4_ONLY_MESSAGE
    return True, ""


def parse_ipv4_targets(text: str) -> tuple[list[str], list[str]]:
    targets: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for raw_value in re.split(r"[\s,]+", text.strip()):
        value = raw_value.strip()
        if not value:
            continue

        valid, _message = validate_ipv4_address(value)
        if not valid:
            invalid.append(value)
            continue

        normalized = str(ipaddress.ip_address(value))
        if normalized not in seen:
            seen.add(normalized)
            targets.append(normalized)

    return targets, invalid
