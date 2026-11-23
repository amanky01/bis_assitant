"""Deterministic interpretation of official BIS registry records."""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from pymongo.errors import PyMongoError

from app.core.config import get_settings
from app.core.exceptions import DatabaseError, DomainNotAllowedError, ScraperError
from app.core.official_http import fetch_official
from app.db.mongo import MongoDB

settings = get_settings()
CML_RE = re.compile(r"\bCM[/\\]L[-\s]?(\d{7,8})\b", re.IGNORECASE)
R_NUM_RE = re.compile(r"\bR[-\s]?(\d{8})\b", re.IGNORECASE)
HUID_RE = re.compile(r"\bHUID\s*(?:(?:number|code)\s*)?(?:[:#-]\s*)?([A-HJ-NP-Z0-9]{6})\b", re.IGNORECASE)
HUID_CODE_RE = re.compile(r"[A-HJ-NP-Z0-9]{6}", re.IGNORECASE)

CML_URL = "https://www.manakonline.in/MANAK/searchProductDetails"
CRS_URL = "https://www.crsbis.in/BIS/Lims_registrationc.do"
HUID_URL = "https://huid.manakonline.in/MANAK/getHUIDDetails"


class Outcome(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    RECORD_FOUND = "record_found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    UNRECOGNIZED = "unrecognized_response"


@dataclass
class RegistryResult:
    outcome: Outcome
    identifier: str
    source: str
    fields: dict[str, str] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def render(self) -> str:
        descriptions = {
            Outcome.ACTIVE: "The registry returned a matching record marked active or valid.",
            Outcome.INACTIVE: "The registry returned a matching record marked inactive or invalid.",
            Outcome.RECORD_FOUND: "The registry returned a matching hallmark record.",
            Outcome.NOT_FOUND: "The registry reported that it found no matching record.",
            Outcome.UNAVAILABLE: "The official registry could not be reached. Verification is inconclusive.",
            Outcome.UNRECOGNIZED: "The registry response could not be confirmed. Verification is inconclusive.",
        }
        lines = [
            f"**Registry lookup for {self.identifier}**",
            descriptions[self.outcome],
            f"[Official registry]({self.source}) · {self.checked_at:%Y-%m-%d %H:%M UTC}",
        ]
        for label, value in self.fields.items():
            # Render registry values as plain text inside Markdown.
            escaped = re.sub(r"([\\`*_{}\[\]<>|~])", r"\\\1", " ".join(value.split())[:300])
            lines.append(f"- {label}: {escaped}")
        lines.append("A registry lookup alone cannot authenticate the physical item.")
        return "\n\n".join(lines)


def _parse_table(body: bytes, identifier: str, source: str) -> RegistryResult:
    soup = BeautifulSoup(body, "lxml")
    identifiers = (
        {"license no", "licence no", "lic no", "cm l no", "license number", "licence number", "cm l number"}
        if source == CML_URL else
        {"registration no", "registration number", "reg no", "r number", "r no"}
    )
    pattern = CML_RE if source == CML_URL else R_NUM_RE
    expected_digits = pattern.fullmatch(identifier).group(1)
    matched_records = []
    for table in soup.find_all("table"):
        rows = {}
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) == 2:
                label = " ".join(re.findall(r"[a-z0-9]+", cells[0].get_text(" ", strip=True).lower()))
                rows[label] = cells[1].get_text(" ", strip=True)
        for key in identifiers & rows.keys():
            value = rows[key]
            match = pattern.fullmatch(value)
            if value == expected_digits or (match and match.group(1) == expected_digits):
                matched_records.append(rows)
                break
    if not matched_records:
        messages = {"no record found", "no records found", "no record found.", "no records found."}
        if any(value.strip().lower() in messages for value in soup.stripped_strings):
            return RegistryResult(Outcome.NOT_FOUND, identifier, source)
        return RegistryResult(Outcome.UNRECOGNIZED, identifier, source)
    if len(matched_records) != 1:
        return RegistryResult(Outcome.UNRECOGNIZED, identifier, source)
    record = matched_records[0]
    status = (record.get("status") or record.get("license status") or record.get("registration status") or "").upper()
    positive = {"ACTIVE"} if source == CML_URL else {"VALID", "ACTIVE"}
    negative = {"INVALID", "INACTIVE", "CANCELLED", "SUSPENDED", "EXPIRED", "WITHDRAWN"}
    outcome = Outcome.UNRECOGNIZED
    if status in positive:
        outcome = Outcome.ACTIVE
    elif status in negative:
        outcome = Outcome.INACTIVE
    fields = {"Status": status} if status else {}
    for label, keys in (
        ("Manufacturer", ("manufacturer name", "applicant name", "name")),
        ("IS standard", ("is standard", "is no")),
        ("Product", ("product", "description")),
        ("Model", ("model",)),
        ("Valid until", ("valid upto", "valid up to")),
    ):
        value = next((record[key] for key in keys if record.get(key)), "")
        if value:
            fields[label] = value
    return RegistryResult(outcome, identifier, source, fields)


def _parse_huid(body: bytes, identifier: str) -> RegistryResult:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return RegistryResult(Outcome.UNRECOGNIZED, identifier, HUID_URL)
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip().lower() in {"no record found", "huid not found"}:
            return RegistryResult(Outcome.NOT_FOUND, identifier, HUID_URL)
        if (
            not payload.get("errorCode")
            and str(payload.get("huid") or payload.get("huidNumber")).upper() == identifier
            and isinstance(payload.get("purity"), (str, int, float))
            and str(payload["purity"]).strip()
            and any(isinstance(payload.get(key), str) and payload[key].strip()
                    for key in ("hallmarkDate", "ahcName", "jewllerName", "jewellerName"))
        ):
            fields = {"Purity": str(payload["purity"])}
            for label, key in (("Hallmark date", "hallmarkDate"), ("Hallmarking centre", "ahcName")):
                if isinstance(payload.get(key), str):
                    fields[label] = payload[key]
            return RegistryResult(Outcome.RECORD_FOUND, identifier, HUID_URL, fields)
    return RegistryResult(Outcome.UNRECOGNIZED, identifier, HUID_URL)


async def lookup_registry(identifier: str, source: str) -> RegistryResult:
    try:
        if source == CML_URL:
            body, _ = await fetch_official(
                source, method="POST", data={"licNo": identifier, "action": "search"}
            )
        elif source == CRS_URL:
            body, _ = await fetch_official(
                source, params={"hmode": "getLimsData", "regNo": identifier}
            )
        elif source == HUID_URL:
            body, _ = await fetch_official(source, params={"huid": identifier})
        else:
            raise ValueError("Unknown BIS registry")
    except (httpx.HTTPError, OSError, TimeoutError, DomainNotAllowedError, ScraperError):
        return RegistryResult(Outcome.UNAVAILABLE, identifier, source)
    if source == HUID_URL:
        return _parse_huid(body, identifier)
    return _parse_table(body, identifier, source)


def requested_lookups(message: str) -> list[tuple[str, str]]:
    identifiers = [(f"CM/L-{m.group(1)}", CML_URL) for m in CML_RE.finditer(message)]
    identifiers.extend((f"R-{m.group(1)}", CRS_URL) for m in R_NUM_RE.finditer(message))
    identifiers.extend((m.group(1).upper(), HUID_URL) for m in HUID_RE.finditer(message)
                      )
    return list(dict.fromkeys(identifiers))


@tool
async def verify_cml(cml_number: str) -> str:
    """Look up a CM/L licence in the official registry."""
    match = CML_RE.fullmatch(cml_number.strip())
    if not match:
        return "[invalid_format] Expected CM/L followed by 7–8 digits."
    return (await lookup_registry(f"CM/L-{match.group(1)}", CML_URL)).render()


@tool
async def verify_r_number(r_number: str) -> str:
    """Look up an eight-digit CRS registration number in the official registry."""
    match = R_NUM_RE.fullmatch(r_number.strip())
    if not match:
        return "[invalid_format] Expected R- followed by eight digits."
    return (await lookup_registry(f"R-{match.group(1)}", CRS_URL)).render()


@tool
async def verify_huid(huid: str) -> str:
    """Look up a six-character hallmark identifier in the official registry."""
    identifier = huid.strip().upper()
    if not HUID_CODE_RE.fullmatch(identifier):
        return "[invalid_format] Expected a six-character HUID."
    return (await lookup_registry(identifier, HUID_URL)).render()


@tool
async def check_category_match(is_number: str, product_description: str) -> str:
    """Compare a product description with the reference categories for an IS standard."""
    standard = is_number.upper().strip()
    try:
        record = await MongoDB.col(settings.col_standards).find_one(
            {"is_number": standard}, {"categories": 1}
        )
    except (DatabaseError, PyMongoError):
        record = None
    categories = record.get("categories", []) if record else []
    if not categories:
        return "[unknown_standard] No reference categories are available."
    product = product_description.casefold().strip()
    if any(product == category.casefold() for category in categories):
        return "[category_match] The description matches a reference category."
    return "[category_unconfirmed] Compare the exact product and model with the official licence scope."


@tool
async def detect_fake_mark(cml_number: str, product_description: str) -> str:
    """Check the registry and explain the limits of verifying a physical mark."""
    result = await verify_cml.ainvoke({"cml_number": cml_number})
    return f"{result}\n\nCompare the manufacturer, product, and model against the official licence scope."
