"""Acuris Geo MCP server — customer-facing agent access to AV + geocoding.

MCP-on-top-of-our-API: a PURE TRANSLATOR. Every tool call forwards as a normal
HTTPS request with the CUSTOMER'S OWN API key (Authorization: Bearer <key> or
X-Acuris-Key from the MCP client config) plus the real client IP in
X-Forwarded-For — to the main box for most countries, to the UK PAF satellite
for gbr (the same routing the Excel add-in uses). This layer touches no DB, no
billing, no auth logic: credit decrement, refund-on-failure, the UK trial cap
and velocity guards all apply verbatim because the request IS an API request.
An MCP tool call bills exactly like the endpoint it wraps; there is no new SKU.

Scope (owner decision 2026-07-25): address validation (with the free US/CA
census enrichment) and geocoding ONLY — no email/phone/trust tools here.

Run modes:
  hosted (production):  python3 acuris_mcp.py            # streamable HTTP :5006
                        (nginx proxies https://api.acuris-geo.com/mcp to it)
  local (stdio):        python3 acuris_mcp.py --stdio    # key from ACURIS_API_KEY

Never raises into the MCP transport: every tool returns a JSON-shaped dict,
with {"error": ...} on failure.
"""
from __future__ import annotations

import argparse
import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# --------------------------------------------------------------------------
# Config (env-overridable; defaults are the production wiring)
# --------------------------------------------------------------------------
# Default is the PUBLIC API so this file runs unchanged on customer machines
# (stdio mode / PyPI package). The hosted acuris-mcp.service pins the loopback
# upstream via Environment=ACURIS_MCP_MAIN_BASE=http://127.0.0.1:5001.
MAIN_BASE = os.environ.get("ACURIS_MCP_MAIN_BASE", "https://api.acuris-geo.com")
PAF_BASE = os.environ.get("ACURIS_MCP_PAF_BASE", "https://paf.acuris-geo.com")
HOST = os.environ.get("ACURIS_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ACURIS_MCP_PORT", "5006"))
TIMEOUT_S = float(os.environ.get("ACURIS_MCP_TIMEOUT_S", "60"))
BATCH_MAX_ROWS = 50  # bounds one tool call's latency; loop the tool for more

PRICING_URL = "https://www.acuris-geo.com/acuris-pricing/"

NEED_KEY = {
    "error": "api_key_required",
    "message": (
        "This tool needs an Acuris API key. Easiest path: call the "
        "request_trial_key tool with your email for an instant free trial key "
        "(100 credits, 7 days, no card). Then reconnect this MCP server with "
        'the header "Authorization: Bearer <your key>". Plans: ' + PRICING_URL
    ),
}

INSTRUCTIONS = (
    "Acuris Geo: address validation (240+ countries, honest verified/corrected/"
    "partial verdicts) and geocoding, with FREE census & geographic enrichment "
    "for street-accurate USA/Canada matches (FIPS, census tract, block group, "
    "CBSA/MSA, congressional district, school district; Canadian equivalents). "
    "UK addresses run against the official Royal Mail PAF (UDPRN + UPRN). "
    "Authenticate by connecting with the header 'Authorization: Bearer "
    "<acuris api key>' (or X-Acuris-Key). No key yet? Use request_trial_key. "
    "Costs are per successful lookup and stated on each tool; failed lookups "
    "are refunded automatically."
)

mcp = FastMCP(
    "acuris-geo",
    instructions=INSTRUCTIONS,
    website_url="https://www.acuris-geo.com/",
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    # The SDK's DNS-rebinding protection defaults to localhost-only Hosts; we
    # sit behind nginx on a public domain, so allow it explicitly.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["api.acuris-geo.com", "127.0.0.1:5006", "localhost:5006"],
        allowed_origins=["https://api.acuris-geo.com", "https://www.acuris-geo.com"],
    ),
)

_BEARER_RE = re.compile(r"^\s*Bearer\s+(.+?)\s*$", re.I)


# --------------------------------------------------------------------------
# Credentials + upstream plumbing
# --------------------------------------------------------------------------
def _client_credentials(ctx: Context | None) -> tuple[str | None, str | None]:
    """(api_key, client_ip) from the HTTP request headers; stdio mode falls
    back to the ACURIS_API_KEY environment variable (no IP)."""
    key = None
    ip = None
    try:
        req = ctx.request_context.request if ctx and ctx.request_context else None
    except Exception:
        req = None
    if req is not None:
        try:
            h = req.headers
            auth = h.get("authorization") or ""
            m = _BEARER_RE.match(auth)
            if m:
                key = m.group(1)
            key = (h.get("x-acuris-key") or key or "").strip() or None
            if not key:
                # URL-config clients (e.g. Smithery) pass the key as a query
                # param appended to the endpoint URL rather than a header.
                try:
                    qp = req.query_params
                    key = (qp.get("apiKey") or qp.get("api_key")
                           or qp.get("key") or "").strip() or None
                except Exception:
                    pass
            # nginx sets X-Forwarded-For to the real client; keep the first hop.
            xff = (h.get("x-forwarded-for") or "").split(",")[0].strip()
            ip = xff or (req.client.host if req.client else None)
        except Exception:
            pass
    if not key:
        key = (os.environ.get("ACURIS_API_KEY") or "").strip() or None
    return key, ip


def _headers(key: str | None, ip: str | None) -> dict:
    h = {"X-Acuris-Client": "acuris-mcp/1.0"}
    if key:
        h["X-Acuris-Key"] = key
    if ip:
        # Forward the real caller so per-IP velocity/abuse caps bind to the
        # customer, not to this service's address.
        h["X-Forwarded-For"] = ip
    return h


async def _request(method: str, base: str, path: str, *, key: str | None,
                   ip: str | None, json_body: Any = None,
                   params: dict | None = None) -> tuple[int, Any]:
    """Single upstream choke point (tests monkeypatch this). Returns
    (status_code, parsed_body). Network errors surface as status 0."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.request(method, base + path, json=json_body,
                                     params=params, headers=_headers(key, ip))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": (r.text or "")[:500]}
    except Exception as e:
        return 0, {"error": "upstream_unreachable", "message": str(e)[:200]}


def _fielded_input(street, house_number, city, state, postcode,
                   address_line) -> Any:
    """Free-text line OR fielded object — the same dual shape /validate takes."""
    if address_line and not any((street, house_number, city, postcode)):
        return address_line
    out = {}
    for k, v in (("street", street), ("house_number", house_number),
                 ("city", city), ("state", state), ("postcode", postcode)):
        if v:
            out[k] = v
    return out or (address_line or "")


def _base_for(country: str) -> str:
    return PAF_BASE if (country or "").strip().lower() == "gbr" else MAIN_BASE


def _wrap(status: int, body: Any) -> dict:
    """Uniform tool result: upstream body + the HTTP status for context."""
    if isinstance(body, dict):
        return {"http_status": status, **body}
    return {"http_status": status, "result": body}


# --------------------------------------------------------------------------
# Tools — address validation
# --------------------------------------------------------------------------
@mcp.tool()
async def validate_address(country: str, address_line: str = "",
                           street: str = "", house_number: str = "",
                           city: str = "", state: str = "", postcode: str = "",
                           ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Validate and standardize one postal address (240+ countries; UK via the
    official Royal Mail PAF with UDPRN + UPRN).

    Give `country` as ISO-3 lowercase (e.g. usa, can, deu, gbr) plus EITHER a
    free-text `address_line` OR fielded parts (street, house_number, city,
    state, postcode). Returns an honest verdict — status "verified" (exact) or
    "corrected" (fixed), or partial_match with reasons — plus the standardized
    form, confidence, coordinates with an explicit accuracy_type, and, for
    street-accurate USA/Canada matches, a FREE `enrichment` block (census
    tract, block group, FIPS, CBSA/MSA, congressional district, school
    district / Canadian equivalents).

    Cost: 1 validation credit per successful lookup; no-match and errors are
    refunded automatically (you pay for results, not attempts)."""
    key, ip = _client_credentials(ctx)
    if not key:
        return NEED_KEY
    c = (country or "").strip().lower()
    body = {"country": c,
            "input": _fielded_input(street, house_number, city, state,
                                    postcode, address_line)}
    status, resp = await _request("POST", _base_for(c), "/validate",
                                  key=key, ip=ip, json_body=body)
    return _wrap(status, resp)


@mcp.tool()
async def validate_addresses_batch(addresses: list, country: str = "",
                                   ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Validate up to 50 addresses in one call (mixed countries allowed).

    `addresses` is a list where each entry is either a free-text string, or an
    object {"input": <string or fielded object>, "country": "iso3"}. Entries
    without their own country use the `country` parameter. UK (gbr) rows are
    routed individually to the Royal Mail PAF service; everything else goes
    through the batch endpoint. Results come back in the same order, each with
    the same shape as validate_address.

    Cost: 1 validation credit per successfully validated row; failed rows are
    refunded. For files bigger than 50 rows, call this tool repeatedly."""
    key, ip = _client_credentials(ctx)
    if not key:
        return NEED_KEY
    if not isinstance(addresses, list) or not addresses:
        return {"error": "addresses_must_be_nonempty_list"}
    if len(addresses) > BATCH_MAX_ROWS:
        return {"error": "batch_too_large", "max": BATCH_MAX_ROWS,
                "received": len(addresses),
                "message": "Send at most %d addresses per call; loop the tool "
                           "for larger sets." % BATCH_MAX_ROWS}
    default_c = (country or "").strip().lower()

    # Partition: gbr rows go per-row to paf; the rest ride one batch call.
    norm: list[tuple[str, Any]] = []
    for entry in addresses:
        if isinstance(entry, dict) and ("input" in entry or "address" in entry):
            e_input = entry.get("input", entry.get("address"))
            e_c = (entry.get("country") or default_c or "").strip().lower()
        else:
            e_input, e_c = entry, default_c
        norm.append((e_c, e_input))

    results: list[Any] = [None] * len(norm)
    main_rows = [(i, c, inp) for i, (c, inp) in enumerate(norm) if c != "gbr"]
    gbr_rows = [(i, c, inp) for i, (c, inp) in enumerate(norm) if c == "gbr"]

    if main_rows:
        payload = {"addresses": [{"input": inp, "country": c}
                                 for _, c, inp in main_rows]}
        status, resp = await _request("POST", MAIN_BASE, "/validate/batch",
                                      key=key, ip=ip, json_body=payload)
        rows = resp.get("results") if isinstance(resp, dict) else None
        if not isinstance(rows, list):
            err = _wrap(status, resp)
            for i, _, _ in main_rows:
                results[i] = err
        else:
            for (i, _, _), row in zip(main_rows, rows):
                results[i] = row
            for i, _, _ in main_rows[len(rows):]:
                results[i] = {"error": "not_processed",
                              "message": "partial batch — retry this row"}
    for i, c, inp in gbr_rows:
        status, resp = await _request("POST", PAF_BASE, "/validate", key=key,
                                      ip=ip,
                                      json_body={"country": "gbr", "input": inp})
        results[i] = _wrap(status, resp)
    return {"count": len(results), "results": results}


@mcp.tool()
async def enrich_us_ca_address(country: str, address_line: str = "",
                               street: str = "", house_number: str = "",
                               city: str = "", state: str = "",
                               postcode: str = "",
                               ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Address → census & geographic codes (USA + Canada), FREE with validation.

    USA: state & county FIPS, census tract (11-digit GEOID), block group,
    CBSA/MSA, congressional district (119th Congress), school district.
    Canada: province, census division, dissemination area, census subdivision,
    census tract, CMA/CA, federal electoral district. Sources: US Census
    Bureau TIGER/Line; Statistics Canada (open data).

    The enrichment is attached when the address resolves to a house-number- or
    street-accurate match; centroid-only matches return no enrichment (the
    point could fall in the wrong area). `country` must be "usa" or "can".

    Cost: 1 validation credit (the enrichment itself is free); failed lookups
    are refunded."""
    c = (country or "").strip().lower()
    if c not in ("usa", "can"):
        return {"error": "unsupported_country",
                "message": "Enrichment covers usa and can only."}
    out = await validate_address(country=c, address_line=address_line,
                                 street=street, house_number=house_number,
                                 city=city, state=state, postcode=postcode,
                                 ctx=ctx)
    if isinstance(out, dict) and "enrichment" not in out and "error" not in out:
        out["enrichment"] = None
        out["enrichment_note"] = ("No enrichment: the address did not resolve "
                                  "to a house-number- or street-accurate match.")
    return out


# --------------------------------------------------------------------------
# Tools — geocoding
# --------------------------------------------------------------------------
@mcp.tool()
async def geocode_address(country: str, street: str = "",
                          house_number: str = "", city: str = "",
                          state: str = "", postcode: str = "", q: str = "",
                          ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Forward-geocode an address to coordinates (lat/lng) with an explicit
    accuracy_type (rooftop, street_interpolated, street_centroid,
    postcode_centroid, locality_centroid) — you always know what you got.

    `country` is ISO-3 lowercase. Give fielded parts or a single line in `q`.
    For UK (gbr) addresses use validate_address instead — it returns rooftop
    coordinates from Royal Mail PAF.

    Cost: 1 credit per successful geocode; failures are refunded."""
    key, ip = _client_credentials(ctx)
    if not key:
        return NEED_KEY
    c = (country or "").strip().lower()
    if c == "gbr":
        return {"error": "use_validate_address",
                "message": "UK geocoding runs through validate_address "
                           "(Royal Mail PAF, rooftop coordinates)."}
    params = {"country": c}
    for k, v in (("street", street), ("hno", house_number), ("city", city),
                 ("state", state), ("postalcode", postcode), ("q", q)):
        if v:
            params[k] = v
    status, resp = await _request("GET", MAIN_BASE, "/geocode", key=key,
                                  ip=ip, params=params)
    return _wrap(status, resp)


@mcp.tool()
async def reverse_geocode(lat: float, lng: float, country: str = "",
                          radius_m: int = 0, limit: int = 0,
                          ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Coordinates → nearest address(es). Returns the closest reference
    addresses to (lat, lng), nearest first, with distances.

    Optional: `country` (ISO-3) to constrain the search, `radius_m`, `limit`.

    Cost: 1 credit per successful lookup; an empty radius (no addresses found)
    is refunded."""
    key, ip = _client_credentials(ctx)
    if not key:
        return NEED_KEY
    params: dict = {"lat": lat, "lng": lng}
    if country:
        params["country"] = (country or "").strip().lower()
    if radius_m:
        params["radius_m"] = radius_m
    if limit:
        params["limit"] = limit
    status, resp = await _request("GET", MAIN_BASE, "/reverse", key=key,
                                  ip=ip, params=params)
    return _wrap(status, resp)


@mcp.tool()
async def expand_uk_postcode(postcode: str,
                             ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Expand a UK postcode into every Royal Mail delivery point at that
    postcode — organisation, building, street, town, county, plus UDPRN, UPRN
    and rooftop coordinates for each address.

    Cost: 1 credit per postcode, regardless of how many addresses come back;
    an unknown/invalid postcode is refunded automatically."""
    key, ip = _client_credentials(ctx)
    if not key:
        return NEED_KEY
    status, resp = await _request("POST", PAF_BASE, "/postcode-lookup",
                                  key=key, ip=ip,
                                  json_body={"postcode": postcode,
                                             "country": "gbr"})
    return _wrap(status, resp)


# --------------------------------------------------------------------------
# Tools — account
# --------------------------------------------------------------------------
@mcp.tool()
async def check_balance(ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Show the connected API key's remaining credit balances (validation,
    geocode, email, phone pools; trial expiry; the UK free-trial PAF counter
    for trial keys). Free — never consumes credits."""
    key, ip = _client_credentials(ctx)
    if not key:
        return NEED_KEY
    status, resp = await _request("GET", MAIN_BASE, "/account/balances",
                                  key=key, ip=ip)
    return _wrap(status, resp)


@mcp.tool()
async def request_trial_key(email: str,
                            ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Get an instant FREE Acuris trial API key — 100 credits, valid 7 days,
    no card, no signup form. Idempotent: asking again with the same email
    within 24h returns the SAME key.

    After receiving the key, reconfigure this MCP server connection with the
    header "Authorization: Bearer <key>" (or set ACURIS_API_KEY for stdio).
    Trial limits: shared per-IP daily caps and a lifetime cap of 40 UK (Royal
    Mail PAF) lookups. Paid plans: https://www.acuris-geo.com/acuris-pricing/"""
    _, ip = _client_credentials(ctx)
    status, resp = await _request("POST", MAIN_BASE, "/dev-key", key=None,
                                  ip=ip, json_body={"email": email})
    out = _wrap(status, resp)
    if status == 200 and isinstance(resp, dict) and resp.get("api_key"):
        out["next_step"] = ("Store this key, then reconnect the acuris-geo "
                            "MCP server with header 'Authorization: Bearer "
                            "<api_key>'.")
    return out


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Acuris Geo MCP server")
    ap.add_argument("--stdio", action="store_true",
                    help="run over stdio (local clients; key from ACURIS_API_KEY)")
    args = ap.parse_args()
    mcp.run(transport="stdio" if args.stdio else "streamable-http")


if __name__ == "__main__":
    main()
