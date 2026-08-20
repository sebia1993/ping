from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MaskResult:
    text: str
    counts: dict[str, int]

    @property
    def summary(self) -> str:
        labels = {
            "secret": "비밀번호·토큰",
            "internal_ip": "내부 IP",
            "user_path": "사용자 경로",
            "share_path": "공유 경로",
            "internal_domain": "사내 도메인",
            "device": "장비·서버 이름",
            "user": "사용자 정보",
        }
        parts = [f"{labels[key]} {count}건" for key, count in self.counts.items() if count]
        return ", ".join(parts) if parts else "마스킹된 항목 없음"


class SensitiveDataMasker:
    """Mask secrets always and company identifiers when the option is enabled."""

    _secret_assignment = re.compile(
        r"(?i)([\"']?\b(?:password|passwd|pwd|token|api[ _-]?key|secret|session(?:id|_id)?|cookie)\b[\"']?)"
        r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    )
    _authorization = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")
    _url_credentials = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
    _ipv4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
    _windows_user_path = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+(?:\\[^\s]*)?")
    _unc_path = re.compile(r"(?i)\\\\[^\\\s]+\\[^\s]+")
    _internal_domain = re.compile(
        r"(?i)(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)+(?:corp|local|internal|lan)(?![\w.-])"
    )
    _keyed_device = re.compile(
        r"(?i)\b(host(?:name)?|device|server|장비|서버)(\s*[:=]\s*)([a-z0-9][a-z0-9._-]{2,})"
    )
    _email = re.compile(r"(?i)(?<![\w.+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w.-])")

    def __init__(self, *, mask_company_information: bool = True) -> None:
        self.mask_company_information = mask_company_information
        self._aliases: dict[str, dict[str, str]] = {}
        self._counts: dict[str, int] = {}

    def mask(self, text: str) -> MaskResult:
        self._aliases = {}
        self._counts = {}
        masked = self._mask_secrets(str(text or ""))
        if self.mask_company_information:
            masked = self._mask_company_data(masked)
        return MaskResult(masked, dict(self._counts))

    def _mask_secrets(self, text: str) -> str:
        def assignment(match: re.Match[str]) -> str:
            self._increment("secret")
            return f"{match.group(1)}{match.group(2)}[MASKED_SECRET]"

        text = self._secret_assignment.sub(assignment, text)

        def authorization(match: re.Match[str]) -> str:
            self._increment("secret")
            return f"{match.group(1)}[MASKED_SECRET]"

        text = self._authorization.sub(authorization, text)

        def url_credentials(match: re.Match[str]) -> str:
            self._increment("secret")
            return f"{match.group(1)}[MASKED_SECRET]@"

        return self._url_credentials.sub(url_credentials, text)

    def _mask_company_data(self, text: str) -> str:
        text = self._windows_user_path.sub(self._mask_user_path, text)
        text = self._unc_path.sub(lambda match: self._alias("share_path", match.group(0), "SHARE_PATH"), text)
        text = self._email.sub(lambda match: self._alias("user", match.group(0), "USER"), text)
        text = self._internal_domain.sub(
            lambda match: self._alias("internal_domain", match.group(0), "INTERNAL_DOMAIN"),
            text,
        )

        def keyed_device(match: re.Match[str]) -> str:
            replacement = self._alias("device", match.group(3), "DEVICE")
            return f"{match.group(1)}{match.group(2)}{replacement}"

        text = self._keyed_device.sub(keyed_device, text)
        return self._ipv4.sub(self._mask_ipv4, text)

    def _mask_user_path(self, match: re.Match[str]) -> str:
        value = match.group(0)
        parts = value.split("\\")
        suffix = "\\".join(parts[3:]) if len(parts) > 3 else ""
        self._increment("user_path")
        return "USER_PATH" + (f"\\{suffix}" if suffix else "")

    def _mask_ipv4(self, match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return value
        if not _is_company_ipv4(address):
            return value
        return self._alias("internal_ip", value, "INTERNAL_IP")

    def _alias(self, category: str, value: str, prefix: str) -> str:
        aliases = self._aliases.setdefault(category, {})
        normalized = value.casefold()
        existing = aliases.get(normalized)
        if existing is not None:
            return existing
        alias = f"{prefix}_{len(aliases) + 1:02d}"
        aliases[normalized] = alias
        self._increment(category)
        return alias

    def _increment(self, category: str) -> None:
        self._counts[category] = self._counts.get(category, 0) + 1


def _is_company_ipv4(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.version != 4:
        return False
    value = int(address)
    private_ranges = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
    )
    return any(value >= int(network.network_address) and value <= int(network.broadcast_address) for network in private_ranges)
