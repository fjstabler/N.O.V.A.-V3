"""Self-directed OSINT: breach/footprint/domain/reputation/photo-metadata checks.

Every tool here is scoped to something the user supplies about themselves — an
email they own, a domain they administer, a suspicious indicator, a local
photo, or a free-text query. These tests pin that scope down (no name+DOB
input exists to test against) alongside the usual success/error/degradation
paths. HTTP is faked via ``httpx.MockTransport`` (matching ``test_surfaces.py``);
WHOIS's raw sockets are faked by monkeypatching ``asyncio.open_connection``;
DNS is faked by monkeypatching ``dns.resolver.Resolver``; EXIF tests exercise
real Pillow-generated images rather than mocking the image library.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import dns.resolver
import httpx
import pytest
from PIL import ExifTags, Image

from nova.context import NovaContext
from nova.runtime.errors import MissingDependency, PermissionDenied, SkillError
from nova.skills.builtin import osint as osint_module
from nova.skills.builtin.osint import (
    OsintSkill,
    _classify_indicator,
    _dms_to_decimal,
    _extract_field,
    _gps_to_decimal,
    _strip_html,
)

# --------------------------------------------------------------------- http


def patch_httpx(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Route every httpx.AsyncClient in the module under test through a MockTransport."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    original_init = httpx.AsyncClient.__init__

    def with_transport(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", with_transport)
    return requests


# ---------------------------------------------------------------------- dns


class FakeResolver:
    """Stands in for dns.resolver.Resolver, keyed by (qname, rtype)."""

    zones: ClassVar[dict[str, dict[str, list[str]]]] = {}

    def __init__(self) -> None:
        self.timeout = 0.0
        self.lifetime = 0.0

    def resolve(self, qname: str, rtype: str) -> list[str]:
        records = FakeResolver.zones.get(qname, {}).get(rtype)
        if not records:
            raise dns.resolver.NXDOMAIN(qnames=[qname])
        return records


def set_dns_zones(
    monkeypatch: pytest.MonkeyPatch, zones: dict[str, dict[str, list[str]]]
) -> None:
    FakeResolver.zones = zones
    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)


# -------------------------------------------------------------------- whois


class FakeWhoisWriter:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeWhoisReader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data


def set_whois_responses(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, bytes | type[Exception]]
) -> list[tuple[str, int]]:
    """Fake asyncio.open_connection, keyed by host. A value that is an
    exception type is raised instead of returning a connection."""
    calls: list[tuple[str, int]] = []

    async def fake_open_connection(host: str, port: int) -> tuple[FakeWhoisReader, FakeWhoisWriter]:
        calls.append((host, port))
        outcome = responses.get(host, b"")
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("simulated failure")
        return FakeWhoisReader(outcome), FakeWhoisWriter()

    monkeypatch.setattr(osint_module.asyncio, "open_connection", fake_open_connection)
    return calls


IANA_REFERRAL = b"""
domain:       COM

organisation: VeriSign Global Registry Services
refer:        whois.verisign-grs.com

source:       IANA
"""

DOMAIN_WHOIS = b"""
Domain Name: EXAMPLE.COM
Registrar: RESERVED-Internet Assigned Numbers Authority
Creation Date: 1995-08-14T04:00:00Z
Registry Expiry Date: 2026-08-13T04:00:00Z
Domain Status: clientDeleteProhibited https://icann.org/epp#clientDeleteProhibited
"""


# --------------------------------------------------------------------- images


def make_jpeg(
    tmp_path: Path,
    name: str,
    *,
    with_exif: bool = False,
    with_gps: bool = False,
) -> Path:
    img = Image.new("RGB", (4, 4), color="red")
    path = tmp_path / name
    if not with_exif:
        img.save(path, "jpeg")
        return path
    exif = img.getexif()
    exif[271] = "TestMake"
    exif[272] = "TestModel"
    exif[306] = "2024:01:01 12:00:00"
    exif[305] = "TestSoftware"
    if with_gps:
        exif[ExifTags.IFD.GPSInfo] = {
            1: "N",
            2: (51.0, 30.0, 0.0),
            3: "W",
            4: (0.0, 7.0, 0.0),
        }
    img.save(path, "jpeg", exif=exif)
    return path


# ---------------------------------------------------------------- availability


def test_skill_is_available_by_default(ctx: NovaContext) -> None:
    available, reason = OsintSkill(ctx).is_available()
    assert available is True
    assert reason == ""


def test_skill_is_unavailable_when_disabled(ctx: NovaContext) -> None:
    ctx.store.patch({"osint": {"enabled": False}}, persist=False)
    available, reason = OsintSkill(ctx).is_available()
    assert available is False
    assert "disabled" in reason


def test_every_osint_tool_is_read_only_and_never_gated(ctx: NovaContext) -> None:
    specs = {s.name: s for s in OsintSkill(ctx).collect_tools()}
    assert set(specs) == {
        "check_breach_exposure",
        "search_footprint",
        "check_domain",
        "check_reputation",
        "read_image_metadata",
    }
    assert not any(s.mutating for s in specs.values())
    assert not any(s.destructive for s in specs.values())


# ------------------------------------------------------------------- breach


async def test_check_breach_exposure_rejects_a_non_email(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="doesn't look like an email"):
        await OsintSkill(ctx).check_breach_exposure(email="not-an-email")


async def test_check_breach_exposure_requires_an_api_key(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="No Have I Been Pwned API key"):
        await OsintSkill(ctx).check_breach_exposure(email="me@example.com")


async def test_check_breach_exposure_reports_known_breaches(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"hibp_api_key": "hibp-key"}}, persist=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["hibp-api-key"] == "hibp-key"
        assert "me%40example.com" in str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "Title": "Adobe",
                    "BreachDate": "2013-10-04",
                    "DataClasses": ["Email addresses", "Passwords"],
                },
                {
                    "Title": "LinkedIn",
                    "BreachDate": "2012-05-05",
                    "DataClasses": ["Email addresses"],
                },
            ],
        )

    patch_httpx(monkeypatch, handler)

    reply = await OsintSkill(ctx).check_breach_exposure(email="me@example.com")

    assert "2 known breach(es)" in reply
    assert "Adobe (2013-10-04): Email addresses, Passwords" in reply
    assert "LinkedIn (2012-05-05): Email addresses" in reply


async def test_check_breach_exposure_handles_a_clean_email(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"hibp_api_key": "hibp-key"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(404))

    reply = await OsintSkill(ctx).check_breach_exposure(email="clean@example.com")

    assert "doesn't appear in any known breach" in reply


async def test_check_breach_exposure_rejects_a_bad_key(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"hibp_api_key": "wrong-key"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(401))

    with pytest.raises(SkillError, match="rejected the API key"):
        await OsintSkill(ctx).check_breach_exposure(email="me@example.com")


async def test_check_breach_exposure_surfaces_rate_limiting(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"hibp_api_key": "hibp-key"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(429))

    with pytest.raises(SkillError, match="rate-limited"):
        await OsintSkill(ctx).check_breach_exposure(email="me@example.com")


# ---------------------------------------------------------------- footprint


async def test_search_footprint_rejects_an_empty_query(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="Give me something to search for"):
        await OsintSkill(ctx).search_footprint(query="   ")


async def test_search_footprint_requires_an_api_key(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="No Brave Search API key"):
        await OsintSkill(ctx).search_footprint(query="Jane Doe")


async def test_search_footprint_reports_results(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"brave_api_key": "brave-key"}}, persist=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-subscription-token"] == "brave-key"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Jane Doe &amp; Co",
                            "description": "Software <b>engineer</b>",
                            "url": "https://example.com/jane",
                        }
                    ]
                }
            },
        )

    patch_httpx(monkeypatch, handler)

    reply = await OsintSkill(ctx).search_footprint(query="Jane Doe")

    assert "Jane Doe &amp; Co" in reply  # HTML entities untouched, only tags stripped
    assert "Software engineer" in reply  # <b> tag stripped
    assert "https://example.com/jane" in reply


async def test_search_footprint_reports_no_results(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"brave_api_key": "brave-key"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(200, json={"web": {"results": []}}))

    reply = await OsintSkill(ctx).search_footprint(query="nobody at all xyzzy")

    assert "No results" in reply


# ------------------------------------------------------------------- domain


async def test_check_domain_rejects_a_non_domain(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="doesn't look like a domain"):
        await OsintSkill(ctx).check_domain(domain="not a domain")


async def test_check_domain_combines_dns_whois_and_subdomains(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_dns_zones(
        monkeypatch,
        {
            "example.com": {
                "A": ["93.184.216.34"],
                "MX": ["10 mail.example.com"],
                "NS": ["ns1.example.com", "ns2.example.com"],
                "TXT": ['"v=spf1 include:_spf.example.com ~all"'],
            },
            "_dmarc.example.com": {"TXT": ['"v=DMARC1; p=reject"']},
        },
    )
    set_whois_responses(
        monkeypatch, {"whois.iana.org": IANA_REFERRAL, "whois.verisign-grs.com": DOMAIN_WHOIS}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "crt.sh" in request.url.host
        return httpx.Response(
            200,
            json=[
                {"name_value": "www.example.com"},
                {"name_value": "mail.example.com\napi.example.com"},
                {"name_value": "*.example.com"},
            ],
        )

    patch_httpx(monkeypatch, handler)

    reply = await OsintSkill(ctx).check_domain(domain="EXAMPLE.COM.")  # trailing dot, mixed case

    assert "A: 93.184.216.34" in reply
    assert "MX: 10 mail.example.com" in reply
    assert "NS: ns1.example.com, ns2.example.com" in reply
    assert "SPF: v=spf1 include:_spf.example.com ~all" in reply
    assert "DMARC: v=DMARC1; p=reject" in reply
    assert "Registrar: RESERVED-Internet Assigned Numbers Authority" in reply
    assert "Expires: 2026-08-13T04:00:00Z" in reply
    assert "www.example.com" in reply
    assert "api.example.com" in reply


async def test_check_domain_degrades_gracefully_when_whois_is_unreachable(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_dns_zones(monkeypatch, {"example.com": {"A": ["93.184.216.34"]}})
    set_whois_responses(monkeypatch, {"whois.iana.org": OSError})
    patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=[]))

    reply = await OsintSkill(ctx).check_domain(domain="example.com")

    assert "A: 93.184.216.34" in reply
    assert "WHOIS" not in reply  # dropped, not raised


async def test_check_domain_reports_missing_dnspython(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "dns.resolver" or name.startswith("dns."):
            raise ImportError(f"no module named {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    set_whois_responses(monkeypatch, {})
    patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=[]))

    reply = await OsintSkill(ctx).check_domain(domain="example.com")

    assert "dnspython isn't installed" in reply


# --------------------------------------------------------------- reputation


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("203.0.113.5", "ip"),
        ("2001:db8::1", "ip"),
        ("d41d8cd98f00b204e9800998ecf8427e", "hash"),  # md5
        ("da39a3ee5e6b4b0d3255bfef95601890afd80709", "hash"),  # sha1
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "hash"),  # sha256
        ("example.com", "domain"),
        ("not-quite-a-hash-abc123", "domain"),
    ],
)
def test_classify_indicator(value: str, kind: str) -> None:
    assert _classify_indicator(value) == kind


async def test_check_reputation_rejects_an_empty_indicator(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="Give me an IP, domain, or file hash"):
        await OsintSkill(ctx).check_reputation(indicator="   ")


async def test_check_reputation_requires_at_least_one_api_key(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="No threat-intel API key configured"):
        await OsintSkill(ctx).check_reputation(indicator="203.0.113.5")


async def test_check_reputation_queries_all_three_sources_for_an_ip(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch(
        {
            "osint": {
                "virustotal_api_key": "vt-key",
                "abuseipdb_api_key": "abuse-key",
                "shodan_api_key": "shodan-key",
            }
        },
        persist=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "www.virustotal.com":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "last_analysis_stats": {
                                "malicious": 2,
                                "suspicious": 1,
                                "harmless": 60,
                                "undetected": 10,
                            }
                        }
                    }
                },
            )
        if host == "api.abuseipdb.com":
            return httpx.Response(
                200, json={"data": {"abuseConfidenceScore": 15, "totalReports": 3}}
            )
        if host == "api.shodan.io":
            return httpx.Response(
                200,
                json={
                    "org": "Example Org",
                    "ports": [22, 80, 443],
                    "hostnames": ["host.example.com"],
                },
            )
        raise AssertionError(f"unexpected host {host}")

    requests = patch_httpx(monkeypatch, handler)

    reply = await OsintSkill(ctx).check_reputation(indicator="203.0.113.5")

    assert "2 malicious, 1 suspicious, 60 harmless (of 73 vendors)" in reply
    assert "15% abuse confidence, 3 report(s)" in reply
    assert "org Example Org, ports 22, 80, 443" in reply
    assert "Hostnames: host.example.com" in reply
    assert {r.url.host for r in requests} == {
        "www.virustotal.com",
        "api.abuseipdb.com",
        "api.shodan.io",
    }


async def test_check_reputation_only_queries_virustotal_for_a_domain(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AbuseIPDB and Shodan only make sense for IPs — configuring all three
    # keys must not send a domain to either of them.
    ctx.store.patch(
        {
            "osint": {
                "virustotal_api_key": "vt-key",
                "abuseipdb_api_key": "abuse-key",
                "shodan_api_key": "shodan-key",
            }
        },
        persist=False,
    )
    requests = patch_httpx(
        monkeypatch,
        lambda request: httpx.Response(404),
    )

    reply = await OsintSkill(ctx).check_reputation(indicator="example.com")

    assert {r.url.host for r in requests} == {"www.virustotal.com"}
    assert "no record for example.com" in reply


async def test_check_reputation_virustotal_reports_an_unknown_hash(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"virustotal_api_key": "vt-key"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(404))

    reply = await OsintSkill(ctx).check_reputation(
        indicator="d41d8cd98f00b204e9800998ecf8427e"
    )

    assert "no record for d41d8cd98f00b204e9800998ecf8427e" in reply


async def test_check_reputation_shodan_reports_no_index(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"osint": {"shodan_api_key": "shodan-key"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(404))

    reply = await OsintSkill(ctx).check_reputation(indicator="203.0.113.9")

    assert "no indexed information" in reply


# ----------------------------------------------------------------- imagery


async def test_read_image_metadata_resolves_within_the_sandbox(
    ctx: NovaContext, tmp_path: Path
) -> None:
    make_jpeg(tmp_path, "photo.jpg", with_exif=True, with_gps=True)
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)

    reply = await OsintSkill(ctx).read_image_metadata(path="photo.jpg")

    assert "Make: TestMake" in reply
    assert "Model: TestModel" in reply
    assert "DateTime: 2024:01:01 12:00:00" in reply
    assert "Software: TestSoftware" in reply
    assert "GPS location embedded: 51.500000, -0.116667" in reply


async def test_read_image_metadata_refuses_a_path_outside_the_sandbox(
    ctx: NovaContext, tmp_path: Path
) -> None:
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)

    with pytest.raises(PermissionDenied):
        await OsintSkill(ctx).read_image_metadata(path="/etc/hosts")


async def test_read_image_metadata_reports_no_exif(ctx: NovaContext, tmp_path: Path) -> None:
    make_jpeg(tmp_path, "plain.jpg", with_exif=False)
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)

    reply = await OsintSkill(ctx).read_image_metadata(path="plain.jpg")

    assert reply == "No EXIF metadata found in this image."


async def test_read_image_metadata_reports_no_gps_when_absent(
    ctx: NovaContext, tmp_path: Path
) -> None:
    make_jpeg(tmp_path, "no_gps.jpg", with_exif=True, with_gps=False)
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)

    reply = await OsintSkill(ctx).read_image_metadata(path="no_gps.jpg")

    assert "Make: TestMake" in reply
    assert "No GPS location embedded." in reply
    assert "⚠" not in reply


async def test_read_image_metadata_rejects_a_non_image_file(
    ctx: NovaContext, tmp_path: Path
) -> None:
    bogus = tmp_path / "not_a_photo.jpg"
    bogus.write_text("this is definitely not image data")
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)

    with pytest.raises(SkillError, match="doesn't look like an image"):
        await OsintSkill(ctx).read_image_metadata(path="not_a_photo.jpg")


async def test_read_image_metadata_reports_missing_pillow(
    ctx: NovaContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError(f"no module named {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"irrelevant")
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)

    with pytest.raises(MissingDependency):
        await OsintSkill(ctx).read_image_metadata(path="photo.jpg")


# ------------------------------------------------------------ pure helpers


def test_extract_field_matches_case_insensitively_and_trims() -> None:
    text = "Domain Name: EXAMPLE.COM\nRegistrar:   Example Registrar Inc.  \n"
    assert _extract_field(text, "registrar") == "Example Registrar Inc."


def test_extract_field_returns_empty_when_absent() -> None:
    assert _extract_field("nothing useful here", "Registrar") == ""


def test_strip_html_removes_tags_but_keeps_entities() -> None:
    assert _strip_html("<b>Jane</b> &amp; friends") == "Jane &amp; friends"


def test_strip_html_handles_none_and_empty() -> None:
    assert _strip_html("") == ""


def test_dms_to_decimal_handles_all_hemispheres() -> None:
    assert _dms_to_decimal((51.0, 30.0, 0.0), "N") == pytest.approx(51.5)
    assert _dms_to_decimal((51.0, 30.0, 0.0), "S") == pytest.approx(-51.5)
    assert _dms_to_decimal((0.0, 7.0, 0.0), "E") == pytest.approx(0.116667, abs=1e-5)
    assert _dms_to_decimal((0.0, 7.0, 0.0), "W") == pytest.approx(-0.116667, abs=1e-5)


def test_gps_to_decimal_returns_none_when_incomplete() -> None:
    assert _gps_to_decimal({}) is None
    assert _gps_to_decimal({1: "N", 2: (51.0, 30.0, 0.0)}) is None  # missing lon


def test_gps_to_decimal_converts_a_full_ifd() -> None:
    ifd = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    lat, lon = _gps_to_decimal(ifd)  # type: ignore[misc]
    assert lat == pytest.approx(51.5)
    assert lon == pytest.approx(-0.116667, abs=1e-5)
