from __future__ import annotations

from app.developer.masking import SensitiveDataMasker


def test_secrets_are_always_masked_when_company_masking_is_disabled() -> None:
    result = SensitiveDataMasker(mask_company_information=False).mask(
        'password=hello token:abc123 Authorization: Bearer top-secret https://user:pass@example.com "api_key": "two words"'
    )

    assert "hello" not in result.text
    assert "abc123" not in result.text
    assert "top-secret" not in result.text
    assert "user:pass" not in result.text
    assert "two words" not in result.text
    assert result.counts["secret"] == 5


def test_company_values_use_repeatable_aliases_without_masking_public_ip() -> None:
    source = (
        "target=10.20.30.40 peer=10.20.30.40 public=8.8.8.8 "
        r"path=C:\Users\honggildong\Documents\result.txt "
        r"share=\\fileserver\team\result.txt host=wlc-seoul-01 domain=gw01.example.corp"
    )
    result = SensitiveDataMasker(mask_company_information=True).mask(source)

    assert "10.20.30.40" not in result.text
    assert result.text.count("INTERNAL_IP_01") == 2
    assert "8.8.8.8" in result.text
    assert "honggildong" not in result.text
    assert "fileserver" not in result.text
    assert "wlc-seoul-01" not in result.text
    assert "example.corp" not in result.text
    assert "USER_PATH" in result.text
    assert "SHARE_PATH_01" in result.text
    assert "DEVICE_01" in result.text
    assert "INTERNAL_DOMAIN_01" in result.text
    assert "10.20.30.40" not in result.summary


def test_company_values_remain_when_company_masking_is_disabled() -> None:
    result = SensitiveDataMasker(mask_company_information=False).mask("host=wlc-seoul-01 ip=10.20.30.40")

    assert "wlc-seoul-01" in result.text
    assert "10.20.30.40" in result.text
