"""
app/tools/compliance.py
────────────────────────
Tool: get_compliance_guide
Returns the certification process for a given product type.
Data is static + enriched from vector DB if available.
"""
from __future__ import annotations

from langchain_core.tools import tool

from app.core.logging import get_logger

logger = get_logger(__name__)

# Static compliance guides — covers the most common product categories.
# The agent can supplement with vector search + web RAG for rare categories.

_GUIDES: dict[str, str] = {
    "electrical": """
BIS Certification Process — Electrical Products (IS scheme / ISI Mark)

Applicable IS standards: IS 694 (cables), IS 8828 (MCB), IS 1293 (plugs/sockets) etc.

Steps:
1. Identify the applicable IS standard for your product (visit bis.gov.in → Product Certification → Mandatory Products list).
2. Register on the BIS portal: manakonline.in → Apply Online → Product Certification.
3. Pay the application fee (₹1,000–₹5,000 depending on category).
4. BIS issues a Sample Testing Order (STO). Get product samples tested at a BIS-recognised lab.
5. BIS officers conduct a factory audit (manufacturing facility + QMS inspection).
6. If samples pass and audit is satisfactory, BIS grants a CM/L license.
7. You may now apply the ISI mark on products. Mark must include CM/L number.
8. Annual surveillance audits + periodic sample re-testing to maintain license.

Timeline: 3–6 months typically.
Fee: Application + testing + audit = ₹20,000–₹80,000 approximately.
Validity: 1–3 years (renewable).
""",
    "electronics": """
BIS Certification Process — Electronics / IT Products (CRS Scheme)

Applicable products: Mobile phones, LED drivers, laptops, CCTV cameras, UPS, set-top boxes etc.

Steps:
1. Identify the CRS mandatory product category (bis.gov.in → CRS).
2. Get the product tested at a BIS-recognised/NABL lab for the applicable IS standard.
3. Apply online at bis.gov.in → CRS → Registration.
4. Submit test reports, product details, and manufacturer information.
5. BIS reviews and issues an R-number (format: R-XXXXXXXX).
6. Mark the R-number on the product packaging/label.

Key difference from ISI: No factory audit — lab testing is sufficient.
Timeline: 4–8 weeks.
Validity: 2 years (must renew with fresh test report).
""",
    "gold": """
BIS Hallmarking Process — Gold Jewellery

Mandatory since June 2021 for all gold jewellery sold in India.

Steps:
1. Jeweller must register with BIS: bis.gov.in → Hallmarking → Jeweller Registration.
2. Pay annual registration fee (₹5,000–₹25,000 depending on number of outlets).
3. Send gold jewellery to a BIS-recognised Assaying and Hallmarking Centre (AHC).
4. AHC tests purity (fire assay / XRF), applies hallmark stamps, assigns HUID.
5. HUID is laser-engraved on each piece — 6-character alphanumeric, globally unique.
6. Jeweller can only sell hallmarked pieces. Consumers can verify HUID at huid.manakonline.in.

HUID marks include: BIS logo (triangle) + purity mark (22K/18K/14K) + AHC code + HUID.
""",
    "lpg": """
BIS Certification Process — LPG Equipment (Safety Critical)

Applicable: LPG cylinders (IS 3196), pressure regulators (IS 8737), flexible hoses (IS 9573).

All LPG products are MANDATORY ISI — cannot be sold without valid CM/L.

Steps:
1. Identify the applicable IS standard.
2. Apply for ISI certification (same process as electrical — see above).
3. Factory audits are more rigorous for safety-critical products.
4. Surveillance audits are more frequent (quarterly for LPG).

Note: Selling non-ISI LPG equipment is a criminal offence under the Petroleum Act.
Always verify CM/L on any LPG equipment before purchase.
""",
    "default": """
BIS Product Certification — General Process

For most products, BIS offers two main schemes:

1. ISI Mark (Scheme I) — for domestic manufacturers
   - Applicable to 500+ mandatory product categories
   - Requires lab testing + factory audit + CM/L license
   - Apply at manakonline.in

2. CRS Scheme (Scheme II) — for electronics/IT
   - Requires lab testing only (no factory audit)
   - Applicable to electronics, IT, and telecom products
   - Apply at bis.gov.in → CRS

3. Foreign Manufacturers Certification Scheme (FMCS)
   - Same as ISI but for imported products
   - Same CM/L format — verifiable at manakonline.in

For specific guidance, tell me your product type and I can give you the
exact IS standard, applicable scheme, and step-by-step process.
""",
}

_KEYWORDS: dict[str, list[str]] = {
    "electrical": ["cable", "wire", "mcb", "switch", "socket", "plug", "electrical", "wiring"],
    "electronics": ["mobile", "phone", "laptop", "led", "cctv", "adapter", "ups", "electronics", "it product"],
    "gold": ["gold", "jewellery", "jewelry", "hallmark", "huid"],
    "lpg": ["lpg", "gas", "cylinder", "regulator", "petroleum"],
}


@tool
async def get_compliance_guide(product_type: str) -> str:
    """
    Get the step-by-step BIS certification process for a product type.
    Covers: electrical products, electronics/IT (CRS), gold jewellery (hallmarking),
    LPG equipment, and general certification guidance.
    Use this when user asks 'how do I get BIS certified' or 'what is the certification process'.
    """
    product_lower = product_type.lower()

    # Match to a guide category
    for category, keywords in _KEYWORDS.items():
        if any(kw in product_lower for kw in keywords):
            logger.debug(f"Compliance guide: '{product_type}' → {category}")
            return _GUIDES[category].strip()

    return _GUIDES["default"].strip()
