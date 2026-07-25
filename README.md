# Acuris Geo MCP Server

Give any AI agent — Claude, Cursor, ChatGPT, Gemini CLI and every other
[MCP](https://modelcontextprotocol.io)-capable client — direct, self-describing
access to **Acuris address validation and geocoding**: 240+ countries, honest
`verified` / `corrected` / partial verdicts, UK addresses against the official
Royal Mail PAF (UDPRN + UPRN), and **free census & geographic enrichment** for
street-accurate USA/Canada matches (census tract, block group, FIPS, CBSA/MSA,
congressional district, school district — Canadian equivalents included).

The server is a thin layer **on top of the Acuris REST API**: every tool call
is billed, refunded and rate-limited exactly like the API call it wraps, under
**your own API key**. Failed lookups are refunded automatically — you pay for
results, not attempts.

## Quick start — hosted endpoint (recommended, zero install)

**Claude Code**

```bash
claude mcp add --transport http acuris-geo https://api.acuris-geo.com/mcp \
  --header "Authorization: Bearer YOUR_ACURIS_API_KEY"
```

**Claude Desktop / any JSON-config client**

```json
{
  "mcpServers": {
    "acuris-geo": {
      "type": "http",
      "url": "https://api.acuris-geo.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_ACURIS_API_KEY" }
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`)

```json
{ "mcpServers": { "acuris-geo": {
    "url": "https://api.acuris-geo.com/mcp",
    "headers": { "Authorization": "Bearer YOUR_ACURIS_API_KEY" } } } }
```

**No API key yet?** Connect without one and call the `request_trial_key` tool
with your email — you get an instant free trial key (100 credits, 7 days, no
card, no signup form). Then reconnect with the key.

## Tools

| Tool | What it does | Cost |
|---|---|---|
| `validate_address` | Validate & standardize one address (240+ countries; UK via Royal Mail PAF with UDPRN + UPRN). Street-accurate USA/Canada matches include the free census enrichment block. | 1 credit |
| `validate_addresses_batch` | Up to 50 addresses per call, mixed countries. Results in input order. | 1 credit / row |
| `enrich_us_ca_address` | Address → census tract, block group, county/state FIPS, CBSA/MSA, congressional district, school district (USA) / dissemination area, census division, CMA/CA, federal electoral district (Canada). | 1 credit (enrichment free) |
| `geocode_address` | Forward geocoding with an explicit `accuracy_type` on every result. | 1 credit |
| `reverse_geocode` | Coordinates → nearest addresses with distances. | 1 credit |
| `expand_uk_postcode` | UK postcode → every Royal Mail delivery point (organisation, UDPRN, UPRN, rooftop coordinates). | 1 credit / postcode |
| `check_balance` | Remaining credit balances for the connected key. | free |
| `request_trial_key` | Instant free trial key by email (idempotent for 24h). | free |

No-match and error responses are **refunded automatically**.

## Local (stdio) mode

```bash
pip install acuris-mcp        # or: uvx acuris-mcp --stdio
ACURIS_API_KEY=YOUR_KEY acuris-mcp --stdio
```

Claude Desktop stdio config:

```json
{
  "mcpServers": {
    "acuris-geo": {
      "command": "uvx",
      "args": ["acuris-mcp", "--stdio"],
      "env": { "ACURIS_API_KEY": "YOUR_KEY" }
    }
  }
}
```

Same tools, same billing. The hosted endpoint is recommended — zero install
and always current.

## Docs & links

- Full MCP docs: https://api.acuris-geo.com/docs/mcp
- REST API reference: https://api.acuris-geo.com/docs
- Pricing: https://www.acuris-geo.com/acuris-pricing/
- US/Canada enrichment: https://www.acuris-geo.com/us-canada-address-enrichment/
- Excel add-in (same engine, no code): https://www.acuris-geo.com/excel-add-in/

## License

MIT (this connector). The Acuris API itself is a commercial service —
see the [terms](https://api.acuris-geo.com/docs/legal-terms).
