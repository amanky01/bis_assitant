"""
app/tools/verification.py
──────────────────────────
BIS mark verification tools — each callable by the ReAct agent.

Tools:
  verify_cml        — ISI mark (manakonline.in)
  verify_r_number   — CRS electronics (crsbis.in)
  verify_huid       — Gold hallmark (huid.manakonline.in)
  check_category_match — IS number ↔ product type
  detect_fake_mark  — composite confidence score

All scrapers use httpx (async). Playwright used only for JS-heavy pages.
All return plain strings — the LLM reads them and forms the final answer.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Patterns ──────────────────────────────────────────────────────────────────

CML_RE = re.compile(r"CM[/\\]L[-\s]?(\d{7,8})", re.IGNORECASE)
R_NUM_RE = re.compile(r"R[-\s]?(\d{7,8})", re.IGNORECASE)
HUID_RE = re.compile(r"\b([A-HJ-NP-Z0-9]{6})\b")  # exclude I,O to avoid confusion

# ── Shared HTTP client ────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; BISAssistant/1.0)"
                )
            },
            follow_redirects=True,
        )
    return _client


# ── IS standard → product category map (fallback, full map in MongoDB) ─────────

_IS_MAP: dict[str, dict[str, Any]] = {
    "IS 694":   {"categories": ["cable", "wire", "pvc cable", "flexible cable"], "safety_critical": True},
    "IS 1554":  {"categories": ["cable", "pvc insulated cable", "wiring cable"], "safety_critical": True},
    "IS 8737":  {"categories": ["lpg regulator", "pressure regulator", "gas regulator"], "safety_critical": True},
    "IS 3196":  {"categories": ["lpg cylinder", "gas cylinder", "cylinder"], "safety_critical": True},
    "IS 8828":  {"categories": ["mcb", "circuit breaker", "miniature circuit breaker"], "safety_critical": True},
    "IS 1293":  {"categories": ["plug", "socket", "socket outlet", "switch"], "safety_critical": True},
    "IS 4151":  {"categories": ["helmet", "motorcycle helmet", "two-wheeler helmet"], "safety_critical": True},
    "IS 9873":  {"categories": ["toy", "toys", "children's toys"], "safety_critical": True},
    "IS 269":   {"categories": ["cement", "portland cement", "opc cement"], "safety_critical": False},
    "IS 16103": {"categories": ["led lamp", "led bulb", "led light"], "safety_critical": False},
    "IS 12701": {"categories": ["water tank", "plastic tank", "storage tank"], "safety_critical": False},
}


# ── Tool: verify_cml ──────────────────────────────────────────────────────────

@tool
async def verify_cml(cml_number: str) -> str:
    """
    Verify an ISI Mark CM/L license number (format: CM/L-XXXXXXX) against
    the BIS database at manakonline.in. Returns manufacturer name, IS standard,
    product description, validity dates, and current license status.
    Use this whenever the user provides or asks about a CM/L number.
    """
    match = CML_RE.search(cml_number)
    if not match:
        return (
            "[invalid_format] CM/L number format invalid. "
            "Expected format: CM/L-XXXXXXX (7-8 digits). "
            f"Received: '{cml_number}'"
        )

    digits = match.group(1)
    normalized = f"CM/L-{digits}"
    await asyncio.sleep(0.3)  # respectful rate limiting

    try:
        client = _get_http()
        resp = await client.post(
            "https://www.manakonline.in/MANAK/searchProductDetails",
            data={"licNo": normalized, "action": "search"},
        )
        resp.raise_for_status()
        return _parse_cml_html(resp.text, normalized)
    except httpx.HTTPError as exc:
        logger.warning(f"CML scrape failed for {normalized}: {exc}")
        return f"[portal_unreachable] Could not reach manakonline.in: {exc}"


def _parse_cml_html(html: str, cml: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")

    if not table or "no record" in html.lower():
        return (
            f"[not_found] CM/L number {cml} does not exist in BIS database. "
            "This indicates a FAKE or fabricated ISI mark."
        )

    data: dict[str, str] = {}
    for row in table.find_all("tr")[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 2:
            data[cells[0].lower().strip()] = cells[1].strip()

    if not data:
        return f"[not_found] CM/L {cml} not found in database."

    status = data.get("status", "UNKNOWN").upper()
    verdict = {
        "ACTIVE": "GENUINE — License is currently active and valid.",
        "CANCELLED": "INVALID — License has been cancelled. Product may be FAKE.",
        "SUSPENDED": "INVALID — License is suspended. Do not trust this mark.",
        "EXPIRED": "INVALID — License has expired.",
    }.get(status, f"UNKNOWN status: {status}")

    return (
        f"CM/L Verification Result for {cml}\n"
        f"Verdict: {verdict}\n"
        f"Status: {status}\n"
        f"Manufacturer: {data.get('manufacturer name', data.get('name', 'N/A'))}\n"
        f"IS Standard: {data.get('is standard', data.get('is no', 'N/A'))}\n"
        f"Product: {data.get('product', data.get('description', 'N/A'))}\n"
        f"Valid From: {data.get('valid from', data.get('grant date', 'N/A'))}\n"
        f"Valid Until: {data.get('valid upto', data.get('valid up to', 'N/A'))}\n"
        f"Address: {data.get('address', 'N/A')}\n"
        f"Brands: {data.get('brands', data.get('brand', 'N/A'))}"
    )


# ── Tool: verify_r_number ─────────────────────────────────────────────────────

@tool
async def verify_r_number(r_number: str) -> str:
    """
    Verify a CRS R-number (format: R-XXXXXXXX) for electronics and IT products
    against the BIS CRS database at crsbis.in.
    Use for mobile phones, LED lamps, laptops, CCTV cameras, adapters etc.
    """
    match = R_NUM_RE.search(r_number)
    if not match:
        return (
            "[invalid_format] R-number format invalid. "
            "Expected: R-XXXXXXXX (8 digits). "
            f"Received: '{r_number}'"
        )

    digits = match.group(1)
    normalized = f"R-{digits}"
    await asyncio.sleep(0.3)

    try:
        client = _get_http()
        resp = await client.get(
            "https://www.crsbis.in/BIS/Lims_registrationc.do",
            params={"hmode": "getLimsData", "regNo": normalized},
        )
        resp.raise_for_status()
        return _parse_r_number_html(resp.text, normalized)
    except httpx.HTTPError as exc:
        return f"[portal_unreachable] Could not reach crsbis.in: {exc}"


def _parse_r_number_html(html: str, r_num: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")

    if not table or "no record" in html.lower():
        return (
            f"[not_found] R-number {r_num} does not exist in CRS database. "
            "This product may not be BIS registered."
        )

    data: dict[str, str] = {}
    for row in table.find_all("tr")[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) >= 2:
            data[cells[0].lower().strip()] = cells[1].strip()

    status = data.get("status", "UNKNOWN").upper()
    verdict = "GENUINE — Registration valid." if "VALID" in status else f"INVALID — Status: {status}"

    return (
        f"CRS R-Number Verification for {r_num}\n"
        f"Verdict: {verdict}\n"
        f"Status: {status}\n"
        f"Applicant: {data.get('applicant name', 'N/A')}\n"
        f"Model: {data.get('model', 'N/A')}\n"
        f"Product: {data.get('product', 'N/A')}\n"
        f"Valid Until: {data.get('valid upto', 'N/A')}"
    )


# ── Tool: verify_huid ─────────────────────────────────────────────────────────

@tool
async def verify_huid(huid: str) -> str:
    """
    Verify a BIS Hallmark HUID (6-character alphanumeric code laser-engraved
    on gold jewellery) at huid.manakonline.in.
    Returns purity, hallmarking centre, jeweller name, and hallmark date.
    """
    huid = huid.upper().strip()
    if not re.match(r"^[A-HJ-NP-Z0-9]{6}$", huid):
        return (
            f"[invalid_format] HUID '{huid}' is invalid. "
            "Must be exactly 6 alphanumeric characters."
        )

    await asyncio.sleep(0.3)

    try:
        client = _get_http()
        resp = await client.get(
            "https://huid.manakonline.in/MANAK/getHUIDDetails",
            params={"huid": huid},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"[portal_unreachable] Could not reach huid.manakonline.in: {exc}"

    # Try JSON first (newer portal), fall back to HTML
    try:
        data = resp.json()
        if not data or data.get("errorCode"):
            return (
                f"[not_found] HUID {huid} not found. "
                "Jewellery may not be BIS hallmarked or HUID is invalid."
            )
        return (
            f"HUID Verification for {huid}\n"
            f"Verdict: GENUINE — BIS Hallmarked\n"
            f"Purity: {data.get('purity', 'N/A')}\n"
            f"AHC Centre: {data.get('ahcName', 'N/A')}\n"
            f"Jeweller: {data.get('jewllerName', 'N/A')}\n"
            f"Hallmark Date: {data.get('hallmarkDate', 'N/A')}"
        )
    except Exception:
        soup = BeautifulSoup(resp.text, "lxml")
        if "no record" in resp.text.lower() or not soup.find("table"):
            return f"[not_found] HUID {huid} not found in hallmark database."
        return f"[parsed] HUID {huid} found. Refer to huid.manakonline.in for full details."


# ── Tool: check_category_match ────────────────────────────────────────────────

@tool
async def check_category_match(is_number: str, product_description: str) -> str:
    """
    Check whether an IS standard number is appropriate for the stated product type.
    Use this after verify_cml returns an IS number to detect category-mismatch fraud
    (real CM/L license applied to a completely different product category).
    Example: IS 694 is for cables — if found on a cement bag, that is fraud.
    """
    is_norm = is_number.upper().strip()

    # Try MongoDB first for the full map
    try:
        from app.db.mongo import MongoDB
        doc = await MongoDB.col(settings.col_standards).find_one(
            {"is_number": is_norm},
            {"_id": 0, "categories": 1, "safety_critical": 1}
        )
    except Exception:
        doc = None

    std = doc or _IS_MAP.get(is_norm)

    if not std:
        return (
            f"[unknown_standard] IS standard '{is_norm}' not found in reference database. "
            "Cannot confirm category match."
        )

    expected = std.get("categories", [])
    product_lower = product_description.lower()
    match = any(cat.lower() in product_lower or product_lower in cat.lower() for cat in expected)
    safety = std.get("safety_critical", False)

    if match:
        return (
            f"[category_match] IS standard {is_norm} is appropriate for '{product_description}'.\n"
            f"Expected categories: {', '.join(expected)}\n"
            f"Safety critical: {'YES' if safety else 'No'}"
        )
    else:
        warn = " ⚠️ SAFETY RISK — Do not use this product." if safety else ""
        return (
            f"[category_MISMATCH] IS standard {is_norm} is NOT for '{product_description}'.\n"
            f"IS {is_norm} is meant for: {', '.join(expected)}\n"
            f"This is a strong indicator of FRAUD — a real CM/L number being misused.{warn}"
        )


# ── Tool: detect_fake_mark ────────────────────────────────────────────────────

@tool
async def detect_fake_mark(
    cml_number: str,
    product_description: str,
) -> str:
    """
    Run a comprehensive fake mark detection check for a CM/L number and product.
    Runs 3 checks: format validation, database existence/status, category match.
    Returns a confidence score and detailed breakdown.
    Use this when the user asks 'is this product genuine' or 'is this ISI mark real'.
    """
    score = 0.0
    checks: list[str] = []
    failed: list[str] = []

    # Check 1: Format (15%)
    if CML_RE.search(cml_number):
        score += 0.15
        checks.append("✓ CM/L format is valid")
    else:
        failed.append("✗ CM/L format is invalid")

    # Check 2: DB existence + status (60%)
    match = CML_RE.search(cml_number)
    is_standard = None
    if match:
        digits = match.group(1)
        normalized = f"CM/L-{digits}"
        await asyncio.sleep(0.3)
        try:
            client = _get_http()
            resp = await client.post(
                "https://www.manakonline.in/MANAK/searchProductDetails",
                data={"licNo": normalized, "action": "search"},
            )
            html = resp.text
            soup = BeautifulSoup(html, "lxml")
            table = soup.find("table")

            if not table or "no record" in html.lower():
                failed.append("✗ CM/L not found in BIS database (FAKE)")
            else:
                data: dict[str, str] = {}
                for row in table.find_all("tr")[1:]:
                    cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                    if len(cells) >= 2:
                        data[cells[0].lower()] = cells[1]

                status = data.get("status", "").upper()
                if status == "ACTIVE":
                    score += 0.60
                    checks.append(f"✓ CM/L exists in database — Status: ACTIVE")
                    is_standard = data.get("is standard", data.get("is no"))
                elif status:
                    score += 0.10  # exists but invalid
                    failed.append(f"✗ CM/L exists but status is {status} (not valid)")
                else:
                    failed.append("✗ CM/L found but status unknown")
        except Exception as exc:
            failed.append(f"✗ Could not reach BIS portal: {exc}")

    # Check 3: Category match (25%)
    if is_standard:
        is_norm = is_standard.upper().strip()
        std = _IS_MAP.get(is_norm)
        if std:
            expected = std.get("categories", [])
            product_lower = product_description.lower()
            category_ok = any(
                cat.lower() in product_lower or product_lower in cat.lower()
                for cat in expected
            )
            if category_ok:
                score += 0.25
                checks.append(f"✓ IS standard {is_norm} matches product category")
            else:
                failed.append(
                    f"✗ CATEGORY MISMATCH — {is_norm} is for {', '.join(expected[:2])}, "
                    f"not for '{product_description}'"
                )
        else:
            checks.append(f"~ IS standard {is_norm} not in local map — category check skipped")
            score += 0.10  # partial credit

    # Verdict
    if score >= 0.85:
        verdict = "HIGH CONFIDENCE — GENUINE"
    elif score >= 0.60:
        verdict = "MODERATE CONFIDENCE — Likely genuine but verify further"
    elif score >= 0.30:
        verdict = "LOW CONFIDENCE — Suspicious, likely fake"
    else:
        verdict = "VERY LOW CONFIDENCE — Almost certainly FAKE"

    lines = [
        f"Fake Mark Detection: {cml_number} on '{product_description}'",
        f"Overall Confidence: {score:.0%}",
        f"Verdict: {verdict}",
        "",
        "Checks passed:",
        *[f"  {c}" for c in checks],
    ]
    if failed:
        lines += ["", "Checks failed:", *[f"  {f}" for f in failed]]

    return "\n".join(lines)
