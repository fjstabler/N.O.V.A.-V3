"""OSINT: check your OWN exposure — breaches, search footprint, domains, indicators, photos.

Every tool here takes something the user supplies about themselves: an email they
own, a domain they administer, a suspicious indicator they encountered, a photo on
their own disk, or a free-text query they choose. None of them accept a name plus
date of birth or any other identity key, and none of them cross-reference multiple
sources into a profile of a third party — that would make this a people-search /
skip-tracing tool, which this deliberately is not. ``OsintSkill.prompt_hint`` bakes
the same constraint into the system prompt so the model does not repurpose these
single-purpose lookups into one.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import re
from typing import Annotated, Any
from urllib.parse import quote

from ...config.schema import OsintSettings
from ...runtime.errors import MissingDependency, SkillError
from ...system.files import FileSandbox
from ..base import Param, Skill, tool

#: MD5 (32), SHA1 (40) or SHA256 (64) hex digest.
_HASH_RE = re.compile(r"^[a-fA-F0-9]{32}$|^[a-fA-F0-9]{40}$|^[a-fA-F0-9]{64}$")
_TAG_RE = re.compile(r"<[^>]+>")


class OsintSkill(Skill):
    name = "osint"
    description = (
        "Check your own exposure: breach history for an email, what search engines show "
        "for a name or handle, a domain's DNS/WHOIS/certificates, reputation for a "
        "suspicious indicator, and hidden GPS/metadata in a photo before you share it."
    )
    category = "OSINT"
    prompt_hint = (
        "These tools check the USER's own exposure — never use them to look up, profile, "
        "or gather information about another person, even if asked. There is no name+DOB "
        "lookup here, on purpose: check_breach_exposure takes only an email the user owns, "
        "search_footprint takes a free-text query the user chooses (their own name/handle, "
        "not someone else's), check_domain takes a domain the user administers, "
        "check_reputation takes a suspicious indicator (IP/domain/hash) the user "
        "encountered, and read_image_metadata reads a local photo the user provides. Do "
        "not chain these calls together to build a profile of a third party."
    )

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.osint.enabled:
            return False, "OSINT tools disabled in settings"
        return True, ""

    @property
    def _settings(self) -> OsintSettings:
        return self.ctx.settings.osint

    @property
    def sandbox(self) -> FileSandbox:
        # Rebuilt from current settings each time so a change to the file roots
        # takes effect without restarting — the same roots the server skill uses.
        return FileSandbox(tuple(self.ctx.settings.server.file_roots))

    # ------------------------------------------------------------------ breach

    @tool(
        "Check Have I Been Pwned for known data breaches involving an email address you "
        "own. Needs an HIBP API key configured under Settings → OSINT."
    )
    async def check_breach_exposure(
        self,
        email: Annotated[str, Param("An email address you own", examples=("me@example.com",))],
    ) -> str:
        email = email.strip()
        if "@" not in email or " " in email:
            raise SkillError("That doesn't look like an email address.")
        settings = self._settings
        if not settings.hibp_api_key:
            raise SkillError(
                "No Have I Been Pwned API key configured — add one under Settings → OSINT "
                "(hibp_api_key) to check breach exposure."
            )
        import httpx

        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"hibp-api-key": settings.hibp_api_key, "user-agent": "nova-assistant"},
        ) as client:
            response = await client.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{quote(email, safe='')}",
                params={"truncateResponse": "false"},
            )
        if response.status_code == 404:
            return f"Good news — {email} doesn't appear in any known breach."
        if response.status_code == 401:
            raise SkillError("Have I Been Pwned rejected the API key — check Settings → OSINT.")
        if response.status_code == 429:
            raise SkillError("Have I Been Pwned rate-limited this request — try again shortly.")
        response.raise_for_status()
        breaches = response.json() or []
        if not breaches:
            return f"Good news — {email} doesn't appear in any known breach."
        lines = [f"{email} appears in {len(breaches)} known breach(es):"]
        for breach in breaches[:10]:
            classes = ", ".join(breach.get("DataClasses") or [])
            lines.append(f"- {breach.get('Title')} ({breach.get('BreachDate')}): {classes}")
        if len(breaches) > 10:
            lines.append(f"...and {len(breaches) - 10} more.")
        return "\n".join(lines)

    # --------------------------------------------------------------- footprint

    @tool(
        "Search the web for a name, handle, email or phrase and read back what comes up — "
        "for checking your own footprint ('what shows up if you search my name'). Needs a "
        "Brave Search API key configured under Settings → OSINT."
    )
    async def search_footprint(
        self,
        query: Annotated[
            str,
            Param("What to search for — your own name, handle or email", examples=("Jane Doe",)),
        ],
    ) -> str:
        query = query.strip()
        if not query:
            raise SkillError("Give me something to search for.")
        settings = self._settings
        if not settings.brave_api_key:
            raise SkillError(
                "No Brave Search API key configured — add one under Settings → OSINT "
                "(brave_api_key) to search."
            )
        import httpx

        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"X-Subscription-Token": settings.brave_api_key, "Accept": "application/json"},
        ) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 6},
            )
        response.raise_for_status()
        results = (response.json().get("web") or {}).get("results") or []
        if not results:
            return f"No results for '{query}'."
        lines = [f"Top results for '{query}':"]
        for i, item in enumerate(results[:6], start=1):
            title = _strip_html(item.get("title", ""))
            snippet = _strip_html(item.get("description", ""))
            lines.append(f"{i}. {title} — {snippet} ({item.get('url', '')})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ domain

    @tool(
        "Look up DNS records, WHOIS registration, and certificate-transparency subdomains "
        "for a domain you own or administer — SPF/DMARC checks, expiry, what's been issued "
        "against it."
    )
    async def check_domain(
        self,
        domain: Annotated[
            str, Param("A domain you own or administer", examples=("example.com",))
        ],
    ) -> str:
        domain = domain.strip().lower().rstrip(".")
        if not domain or "." not in domain:
            raise SkillError("That doesn't look like a domain — try something like 'example.com'.")
        dns_result, whois_result, subdomains_result = await asyncio.gather(
            self._dns_records(domain),
            self._whois(domain),
            self._subdomains(domain),
        )
        sections = [s for s in (dns_result, whois_result, subdomains_result) if s]
        return "\n\n".join(sections) if sections else f"Nothing found for {domain}."

    async def _dns_records(self, domain: str) -> str:
        try:
            import dns.resolver
        except ImportError:
            return (
                "DNS: dnspython isn't installed — "
                "run `pip install 'nova-core[osint]'` to enable this."
            )

        def _query() -> dict[str, list[str]]:
            resolver = dns.resolver.Resolver()
            resolver.timeout = 5.0
            resolver.lifetime = 5.0
            records: dict[str, list[str]] = {}
            for rtype in ("A", "AAAA", "MX", "NS", "TXT"):
                try:
                    records[rtype] = [str(r).strip('"') for r in resolver.resolve(domain, rtype)]
                except Exception:  # noqa: BLE001 - absence of a record type is normal
                    records[rtype] = []
            try:
                records["DMARC"] = [
                    str(r).strip('"') for r in resolver.resolve(f"_dmarc.{domain}", "TXT")
                ]
            except Exception:  # noqa: BLE001 - most domains have no DMARC record
                records["DMARC"] = []
            return records

        records = await asyncio.to_thread(_query)
        lines = ["DNS:"]
        for rtype in ("A", "AAAA", "MX", "NS"):
            if records.get(rtype):
                lines.append(f"  {rtype}: {', '.join(records[rtype])}")
        txt = records.get("TXT") or []
        spf = next((t for t in txt if "v=spf1" in t), None)
        lines.append(f"  SPF: {spf or 'not set'}")
        dmarc = records.get("DMARC") or []
        lines.append(f"  DMARC: {dmarc[0] if dmarc else 'not set'}")
        return "\n".join(lines)

    async def _whois(self, domain: str) -> str:
        tld = domain.rsplit(".", 1)[-1]
        try:
            iana = await self._whois_query("whois.iana.org", tld)
        except (OSError, TimeoutError):
            return ""
        server = _extract_field(iana, "refer") or "whois.iana.org"
        try:
            raw = await self._whois_query(server, domain)
        except (OSError, TimeoutError):
            return ""
        fields = {
            "Registrar": _extract_field(raw, "Registrar"),
            "Registered": _extract_field(raw, "Creation Date"),
            "Expires": _extract_field(raw, "Registry Expiry Date"),
            "Status": _extract_field(raw, "Domain Status"),
        }
        present = {k: v for k, v in fields.items() if v}
        if not present:
            return ""
        return "WHOIS:\n" + "\n".join(f"  {k}: {v}" for k, v in present.items())

    async def _whois_query(self, server: str, query: str) -> str:
        reader, writer = await asyncio.open_connection(server, 43)
        try:
            writer.write(f"{query}\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), timeout=10.0)
        finally:
            writer.close()
        return data.decode("utf-8", "replace")

    async def _subdomains(self, domain: str) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
                response = await client.get(
                    "https://crt.sh/", params={"q": f"%.{domain}", "output": "json"}
                )
            response.raise_for_status()
            entries = response.json() or []
        except (httpx.HTTPError, ValueError):
            # crt.sh is free/keyless and best-effort — often flaky or slow; just
            # drop this section rather than failing the whole domain check.
            return ""
        names: set[str] = set()
        for entry in entries:
            for value in str(entry.get("name_value", "")).split("\n"):
                cleaned = value.strip().lstrip("*.")
                if cleaned:
                    names.add(cleaned)
        if not names:
            return ""
        sorted_names = sorted(names)
        shown = sorted_names[:20]
        lines = ["Certificate-transparency subdomains:"] + [f"  {n}" for n in shown]
        if len(sorted_names) > 20:
            lines.append(f"  ...and {len(sorted_names) - 20} more.")
        return "\n".join(lines)

    # -------------------------------------------------------------- reputation

    @tool(
        "Check reputation for a suspicious indicator — an IP, domain, or file hash you "
        "encountered, e.g. in an email or a log — against VirusTotal, AbuseIPDB and "
        "Shodan. Not for looking up a person. Needs at least one of those API keys "
        "configured under Settings → OSINT."
    )
    async def check_reputation(
        self,
        indicator: Annotated[
            str,
            Param(
                "A suspicious IP, domain, or file hash (MD5/SHA1/SHA256)",
                examples=("203.0.113.5", "d41d8cd98f00b204e9800998ecf8427e"),
            ),
        ],
    ) -> str:
        indicator = indicator.strip()
        if not indicator:
            raise SkillError("Give me an IP, domain, or file hash to check.")
        settings = self._settings
        if not any(
            (settings.virustotal_api_key, settings.abuseipdb_api_key, settings.shodan_api_key)
        ):
            raise SkillError(
                "No threat-intel API key configured — add a VirusTotal, AbuseIPDB, or "
                "Shodan key under Settings → OSINT to check reputation."
            )
        kind = _classify_indicator(indicator)
        calls = []
        if settings.virustotal_api_key:
            calls.append(self._virustotal(indicator, kind))
        if settings.abuseipdb_api_key and kind == "ip":
            calls.append(self._abuseipdb(indicator))
        if settings.shodan_api_key and kind == "ip":
            calls.append(self._shodan(indicator))
        if not calls:
            return f"No configured source can check a {kind} indicator like this."
        results = await asyncio.gather(*calls)
        sections = [s for s in results if s]
        return "\n\n".join(sections) if sections else f"No reputation data found for {indicator}."

    async def _virustotal(self, indicator: str, kind: str) -> str:
        import httpx

        endpoint = {"ip": "ip_addresses", "domain": "domains", "hash": "files"}[kind]
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.request_timeout,
                headers={"x-apikey": self._settings.virustotal_api_key},
            ) as client:
                response = await client.get(
                    f"https://www.virustotal.com/api/v3/{endpoint}/{quote(indicator, safe='')}"
                )
            if response.status_code == 404:
                return f"VirusTotal: no record for {indicator}."
            response.raise_for_status()
            stats = (
                response.json().get("data", {}).get("attributes", {}).get("last_analysis_stats")
                or {}
            )
        except httpx.HTTPError as exc:
            return f"VirusTotal: request failed ({exc})."
        total = sum(stats.values())
        return (
            f"VirusTotal: {stats.get('malicious', 0)} malicious, "
            f"{stats.get('suspicious', 0)} suspicious, "
            f"{stats.get('harmless', 0)} harmless (of {total} vendors)."
        )

    async def _abuseipdb(self, ip: str) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=self._settings.request_timeout,
                headers={"Key": self._settings.abuseipdb_api_key, "Accept": "application/json"},
            ) as client:
                response = await client.get(
                    "https://api.abuseipdb.com/api/v2/check",
                    params={"ipAddress": ip, "maxAgeInDays": 90},
                )
            response.raise_for_status()
            data = response.json().get("data", {})
        except httpx.HTTPError as exc:
            return f"AbuseIPDB: request failed ({exc})."
        score = data.get("abuseConfidenceScore", 0)
        reports = data.get("totalReports", 0)
        return f"AbuseIPDB: {score}% abuse confidence, {reports} report(s)."

    async def _shodan(self, ip: str) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
                response = await client.get(
                    f"https://api.shodan.io/shodan/host/{quote(ip, safe='')}",
                    params={"key": self._settings.shodan_api_key},
                )
            if response.status_code == 404:
                return "Shodan: no indexed information for this IP."
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            return f"Shodan: request failed ({exc})."
        ports = sorted(data.get("ports") or [])
        line = f"Shodan: org {data.get('org') or 'unknown'}, ports {', '.join(map(str, ports))}."
        hostnames = data.get("hostnames") or []
        if hostnames:
            line += f" Hostnames: {', '.join(hostnames)}."
        return line

    # ---------------------------------------------------------------- imagery

    @tool(
        "Read EXIF metadata from a local photo — camera make/model, timestamp, and any "
        "embedded GPS coordinates — to check what a photo would reveal before you share it."
    )
    async def read_image_metadata(
        self,
        path: Annotated[
            str, Param("Path to a local image file", examples=("~/Pictures/photo.jpg",))
        ],
    ) -> str:
        try:
            from PIL import ExifTags, Image, UnidentifiedImageError
        except ImportError as exc:
            raise MissingDependency("image metadata", "Pillow", "vision") from exc

        resolved = self.sandbox.resolve(path)

        def _read() -> str:
            data = resolved.read_bytes()
            try:
                with Image.open(io.BytesIO(data)) as img:
                    exif = img.getexif()
            except UnidentifiedImageError as exc:
                raise SkillError(f"'{path}' doesn't look like an image I can read.") from exc
            if not exif:
                return "No EXIF metadata found in this image."
            tags = {ExifTags.TAGS.get(tag_id, tag_id): value for tag_id, value in exif.items()}
            wanted = ("Make", "Model", "DateTime", "Software")
            lines = [f"{key}: {tags[key]}" for key in wanted if key in tags]
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
            coords = _gps_to_decimal(gps_ifd) if gps_ifd else None
            if coords:
                lines.append(f"⚠ GPS location embedded: {coords[0]:.6f}, {coords[1]:.6f}")
            else:
                lines.append("No GPS location embedded.")
            return "\n".join(lines)

        return await asyncio.to_thread(_read)


def _classify_indicator(value: str) -> str:
    try:
        ipaddress.ip_address(value)
        return "ip"
    except ValueError:
        pass
    if _HASH_RE.match(value):
        return "hash"
    return "domain"


def _extract_field(text: str, field: str) -> str:
    match = re.search(rf"^\s*{re.escape(field)}:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "")


def _dms_to_decimal(dms: tuple[Any, Any, Any], ref: str) -> float:
    degrees, minutes, seconds = (float(v) for v in dms)
    decimal = degrees + minutes / 60 + seconds / 3600
    return -decimal if ref in ("S", "W") else decimal


def _gps_to_decimal(gps_ifd: dict[int, Any]) -> tuple[float, float] | None:
    try:
        lat, lat_ref, lon, lon_ref = gps_ifd[2], gps_ifd[1], gps_ifd[4], gps_ifd[3]
    except KeyError:
        return None
    return _dms_to_decimal(lat, lat_ref), _dms_to_decimal(lon, lon_ref)
