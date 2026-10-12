# HTML sources

`fixtures/source_catalog.json` contains 93 HTML entry candidates covering all Greek regions, national news, sports, public notices and international reporting. An entry is not a live uptime claim. The worker checks public HTTPS, robots and readable links before activation, then fetches linked article HTML. RSS, Atom, XML input and publisher API JSON are rejected.

Public-fact candidates receive the application's configured internal-analysis and original-reporting policy when `NEWS_PUBLIC_HTML_ENABLED=true`. Known restricted entries remain excluded. The policy does not assert a publisher redistribution or photo license. Text/image reuse flags require a separately documented grant.

Each source supports an HTML entry URL, additional `entry_urls`, country/language, publisher coverage IDs, organization/syndication identity, polling interval and optional link selector/URL patterns. Publisher coverage is exposed separately from event geography. A site redesign, denied robots, JavaScript-only content or an access challenge can make a candidate unusable; inspect its recorded failure and configure another permitted HTML entry when available.

AI web search is optional and budgeted. It supplies candidate URLs, not article facts. URLs discovered through search or outgoing HTML citations must pass the same fetch, extraction, evidence and verification stages. Discovery is bounded by maximum sources, URL depth, page limits and per-tick capacity.

GeoNames attribution and import coverage are described in README. City IDs do not imply legal municipality boundaries. For municipality-specific ads, import the exact official municipality register and use affected/occurred geographic roles. Do not infer an event's location from its publisher's address.
