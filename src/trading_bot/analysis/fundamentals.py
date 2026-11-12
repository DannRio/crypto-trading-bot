"""Read-only, attributable fundamental and exchange-event evidence.

These providers add context, not a price forecast. Missing coverage remains
unknown, and current API responses must never be used as point-in-time history
in a backtest. External descriptions and announcements are untrusted text.
"""

from __future__ import annotations

import copy
import html
import ipaddress
import json
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


UTC = timezone.utc
COIN_LIST_URL = "https://api.coingecko.com/api/v3/coins/list?include_platform=false"
PROTOCOLS_URL = "https://api.llama.fi/protocols"
ANNOUNCEMENTS_URL = "https://api.kucoin.com/api/v3/announcements"
ALLOWED_API_HOSTS = frozenset({"api.coingecko.com", "api.kucoin.com", "api.llama.fi"})
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,159}\Z")
_BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,29}\Z")


class EvidenceError(ValueError):
    """An error with a deliberately safe, credential-free public message."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat().replace("+00:00", "Z") if value else None


def _date(value: Any) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (float, int)) and math.isfinite(value):
            return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, UTC)
        if isinstance(value, str) and value:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _number(value: Any, *, negative: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and (negative or result >= 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(value: Any, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value[:20_000])
    clean = html.unescape(" ".join(parser.parts))
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", clean)).strip()[:limit]


def safe_public_url(value: Any, *, kucoin_only: bool = False) -> str | None:
    """Validate links for display; links returned by providers are never fetched."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None
        if kucoin_only and (host not in {"kucoin.com", "www.kucoin.com"}
                           or not parsed.path.startswith("/announcement/")):
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                return None
        return value
    except (ValueError, TypeError):
        return None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_json(url: str, *, timeout: float, headers: dict, max_bytes: int) -> Any:
    opener = build_opener(_NoRedirects())
    with opener.open(Request(url, headers=headers, method="GET"), timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise EvidenceError("Provider response exceeded the size limit.")
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise EvidenceError("Provider response exceeded the size limit.")
    return json.loads(raw.decode("utf-8"))


@dataclass(frozen=True)
class FetchedJson:
    data: Any
    retrieved_at: datetime
    cached: bool = False


class JsonHttpClient:
    """Bounded GET-only HTTP adapter with a process-local, thread-safe TTL cache."""

    def __init__(self, *, fetcher: Callable | None = None,
                 clock: Callable[[], datetime] | None = None,
                 timeout_seconds: float = 4, max_bytes: int = 8_000_000,
                 max_cache_entries: int = 64, coingecko_demo_key: str | None = None) -> None:
        if not 0 < timeout_seconds <= 15 or not 1024 <= max_bytes <= 20_000_000:
            raise ValueError("Invalid HTTP limits.")
        if not 1 <= max_cache_entries <= 256:
            raise ValueError("Invalid cache capacity.")
        self.fetcher = fetcher or _download_json
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_cache_entries = max_cache_entries
        self._key = coingecko_demo_key if coingecko_demo_key is not None else os.getenv("COINGECKO_DEMO_API_KEY")
        self._cache: OrderedDict[str, FetchedJson] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, url: str, *, ttl_seconds: int = 300) -> FetchedJson:
        if not safe_public_url(url):
            raise EvidenceError("Unsupported evidence endpoint.")
        parsed = urlsplit(url)
        if (parsed.hostname not in ALLOWED_API_HOSTS
                or parsed.fragment or not 0 <= ttl_seconds <= 86_400):
            raise EvidenceError("Unsupported evidence endpoint.")
        with self._lock:
            now = _utc(self.clock())
            cached = self._cache.get(url)
            if cached and 0 <= (now - cached.retrieved_at).total_seconds() < ttl_seconds:
                self._cache.move_to_end(url)
                return FetchedJson(copy.deepcopy(cached.data), cached.retrieved_at, True)
            headers = {"Accept": "application/json", "User-Agent": "crypto-trading-bot/evidence"}
            if parsed.hostname == "api.coingecko.com" and self._key:
                headers["x-cg-demo-api-key"] = self._key
            try:
                data = self.fetcher(url, timeout=self.timeout_seconds, headers=headers, max_bytes=self.max_bytes)
            except EvidenceError:
                raise
            except HTTPError as exc:
                raise EvidenceError(f"Provider returned HTTP {exc.code}.") from None
            except (URLError, TimeoutError, OSError):
                raise EvidenceError("Provider network request failed.") from None
            except Exception:
                # Never expose URLs, response text, exception details or API keys.
                raise EvidenceError("Provider response could not be read.") from None
            item = FetchedJson(copy.deepcopy(data), _utc(self.clock()))
            self._cache[url] = item
            self._cache.move_to_end(url)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
            return FetchedJson(copy.deepcopy(data), item.retrieved_at)


def _source(provider: str, url: str, *, result: FetchedJson | None = None,
            as_of: datetime | None = None, status: str = "ok", detail: str | None = None,
            now: datetime) -> dict:
    return {"provider": provider, "url": url, "status": status,
            "retrieved_at": _iso(result.retrieved_at) if result else None,
            "attempted_at": _iso(now),
            "as_of": _iso(as_of), "cached": result.cached if result else False,
            "cache_age_seconds": max(0, (now - result.retrieved_at).total_seconds()) if result else None,
            "detail": detail}


def _matches_asset(text: str, base: str, name: str | None) -> bool:
    # Symbols are exact and case-sensitive. Short/common English symbols require
    # an explicit ticker marker; this avoids ONE/NEAR/IN appearing in ordinary text.
    escaped = re.escape(base)
    explicit = rf"(?:\({escaped}\)|\${escaped}\b|\b{escaped}(?:/|-)(?:USDT|USDC|USD|BTC)\b)"
    if re.search(explicit, text):
        return True
    if name and name.casefold() != base.casefold() and len(name) >= 4:
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            return True
    common = {"ONE", "NEAR", "GAS", "ON", "IN", "FOR", "AND", "OR", "THE", "ALL", "NEW", "TIME", "HIGH", "LOW"}
    return len(base) >= 3 and base not in common and bool(re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", text))


def _event_class(title: str) -> tuple[str, str]:
    if re.search(r"\b(delist\w*|exploit\w*|hack(?:ed|ing)?|compromis\w*)\b|security incident", title, re.I):
        return "adverse_event", "high"
    if re.search(r"\b(suspend\w*|suspension|halt\w*)\b", title, re.I):
        return "service_disruption", "high"
    if re.search(r"\b(unlock\w*|vesting|migration|swap)\b", title, re.I):
        return "supply_or_token_change", "medium"
    return "announcement", "info"


class FundamentalService:
    """Collect current evidence without turning missing coverage into approval."""

    def __init__(self, *, http: JsonHttpClient | None = None,
                 clock: Callable[[], datetime] | None = None, enabled: bool = True,
                 include_defillama: bool = True, max_announcement_pages: int = 2,
                 lookback_days: int = 30) -> None:
        if not 1 <= max_announcement_pages <= 3 or not 1 <= lookback_days <= 30:
            raise ValueError("Invalid announcement limits.")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.http = http or JsonHttpClient(clock=self.clock)
        self.enabled = enabled
        self.include_defillama = include_defillama
        self.max_announcement_pages = max_announcement_pages
        self.lookback_days = lookback_days

    def collect(self, base: str, name: str | None = None,
                coingecko_id: str | None = None, now: datetime | None = None) -> dict:
        current = _utc(now or self.clock())
        report = {"status": "unavailable", "retrieved_at": _iso(current),
                  "identity": None, "metrics": {}, "project_urls": [], "events": [],
                  "sources": [], "risk_flags": [], "coverage_gaps": [],
                  "review_required": False, "historical_backtest_eligible": False,
                  "interpretation": "Current sourced context; no directional score or calibrated probability."}
        if not self.enabled:
            report.update(status="disabled", coverage_gaps=["Fundamental and event evidence was disabled."])
            return report
        if not isinstance(base, str) or not _BASE.fullmatch(base):
            report["coverage_gaps"].append("Asset identity is invalid; no providers were queried.")
            return report
        base = base.upper()
        name = _text(name, 160) or None
        self._coin(report, base, name, coingecko_id, current)
        identity = report["identity"] or {}
        self._announcements(report, base, identity.get("name") or name, current)
        if self.include_defillama and identity:
            self._defillama(report, identity["coingecko_id"], current)
        else:
            report["coverage_gaps"].append("Protocol usage, fees and revenue are unavailable without an exact provider identity and enabled coverage.")
        report["coverage_gaps"].extend([
            "A verified future token-unlock schedule is not provided; supply figures are not an unlock calendar.",
            "Announcements cover a bounded KuCoin feed, not all project, governance, security or global news sources.",
        ])
        report["risk_flags"] = list(dict.fromkeys(report["risk_flags"]))
        report["coverage_gaps"] = list(dict.fromkeys(report["coverage_gaps"]))
        if any(s["status"] in {"ok", "partial", "stale"} for s in report["sources"]):
            report["status"] = "partial" if report["coverage_gaps"] else "ok"
        return report

    def _coin(self, report: dict, base: str, name: str | None, coin_id: str | None, now: datetime) -> None:
        url = COIN_LIST_URL
        try:
            if coin_id is None:
                candidates = self.http.get(COIN_LIST_URL, ttl_seconds=3600).data
                if not isinstance(candidates, list):
                    raise EvidenceError("CoinGecko identity response has an unsupported shape.")
                candidates = [c for c in candidates if isinstance(c, dict)
                              and str(c.get("symbol", "")).casefold() == base.casefold()]
                if name:
                    candidates = [c for c in candidates if str(c.get("name", "")).casefold() == name.casefold()]
                if len(candidates) != 1:
                    raise EvidenceError("CoinGecko asset identity is missing or ambiguous; no ticker-only guess was made.")
                coin_id = candidates[0].get("id")
            if not isinstance(coin_id, str) or not _SLUG.fullmatch(coin_id):
                raise EvidenceError("CoinGecko asset ID is invalid.")
            url = f"https://api.coingecko.com/api/v3/coins/{quote(coin_id, safe='')}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
            result = self.http.get(url, ttl_seconds=300)
            data = result.data
            if not isinstance(data, dict) or data.get("id") != coin_id or str(data.get("symbol", "")).casefold() != base.casefold():
                raise EvidenceError("CoinGecko response does not match the resolved asset.")
            market = data.get("market_data")
            if not isinstance(market, dict):
                raise EvidenceError("CoinGecko market data is unavailable.")
            observed = _date(market.get("last_updated") or data.get("last_updated"))
            status, detail = "ok", None
            if observed is None or observed > now + timedelta(minutes=5):
                status, detail = "partial", "Provider observation time is missing or invalid."
                observed = None
            elif now - observed > timedelta(hours=24):
                status, detail = "stale", "Provider market metadata is older than 24 hours."
            report["sources"].append(_source("CoinGecko", url, result=result, as_of=observed, status=status, detail=detail, now=now))
            if detail:
                report["coverage_gaps"].append(detail)
            report["identity"] = {"coingecko_id": coin_id, "symbol": base,
                                  "name": _text(data.get("name"), 160),
                                  "source_url": f"https://www.coingecko.com/en/coins/{coin_id}"}
            for source_key, target in [("market_cap", "market_cap_usd"), ("fully_diluted_valuation", "fully_diluted_valuation_usd")]:
                value = market.get(source_key)
                numeric = _number(value.get("usd")) if isinstance(value, dict) else None
                if numeric is not None:
                    report["metrics"][target] = numeric
            for key in ("circulating_supply", "total_supply", "max_supply"):
                numeric = _number(market.get(key))
                if numeric is not None:
                    report["metrics"][key] = numeric
            metrics = report["metrics"]
            total, circulating = metrics.get("total_supply"), metrics.get("circulating_supply")
            if total and circulating is not None and circulating <= total:
                metrics["circulating_to_total_ratio"] = circulating / total
            elif total is not None and circulating is not None and circulating > total:
                report["coverage_gaps"].append("Reported circulating supply exceeds total supply; no dilution ratio was calculated.")
            if not metrics:
                report["coverage_gaps"].append("No usable numeric supply or valuation metrics were returned.")
            links = data.get("links") if isinstance(data.get("links"), dict) else {}
            websites = links.get("homepage") if isinstance(links.get("homepage"), list) else []
            for candidate in websites[:5] + [links.get("whitepaper")]:
                safe = safe_public_url(candidate)
                if safe and safe not in report["project_urls"]:
                    report["project_urls"].append(safe)
            notice = _text(data.get("public_notice"), 500)
            if notice:
                report["risk_flags"].append("CoinGecko project notice: " + notice)
                if _event_class(notice)[1] == "high":
                    report["review_required"] = True
        except Exception as exc:
            reason = str(exc) if isinstance(exc, EvidenceError) else "CoinGecko evidence could not be validated."
            report["sources"].append(_source("CoinGecko", url, status="unavailable", detail=reason, now=now))
            report["coverage_gaps"].append(reason)

    def _announcements(self, report: dict, base: str, name: str | None, now: datetime) -> None:
        seen: set[str] = set()
        oldest: datetime | None = None
        total_pages = 1
        examined = 0
        bad = 0
        result: FetchedJson | None = None
        url = ANNOUNCEMENTS_URL
        try:
            for page in range(1, self.max_announcement_pages + 1):
                url = ANNOUNCEMENTS_URL + "?" + urlencode({"currentPage": page, "pageSize": 50, "lang": "en_US"})
                result = self.http.get(url, ttl_seconds=300)
                payload = result.data
                if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
                    raise EvidenceError("KuCoin announcement response is unavailable.")
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise EvidenceError("KuCoin announcement response has an unsupported shape.")
                total_pages = int(data.get("totalPage", 1))
                for item in data["items"][:50]:
                    examined += 1
                    if not isinstance(item, dict):
                        bad += 1
                        continue
                    published = _date(item.get("cTime"))
                    if not published or published > now:
                        bad += 1
                        continue
                    oldest = min(oldest, published) if oldest else published
                    if published < now - timedelta(days=self.lookback_days):
                        continue
                    title = _text(item.get("annTitle"), 240)
                    summary = _text(item.get("annDesc"), 800)
                    if not _matches_asset(title + " " + summary, base, name):
                        continue
                    link = safe_public_url(item.get("annUrl"), kucoin_only=True)
                    if not link:
                        bad += 1
                        continue
                    identifier = str(item.get("annId") or link)
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    category, severity = _event_class(title + " " + summary)
                    event = {"id": identifier, "title": title, "summary": summary,
                             "category": category, "severity": severity,
                             "source_url": link, "provider": "KuCoin",
                             "published_at": _iso(published), "event_at": None,
                             "retrieved_at": _iso(result.retrieved_at),
                             "asset_specific": True,
                             "interpretation": "Official announcement matched to the asset; event classification requires source review."}
                    report["events"].append(event)
                    if severity == "high":
                        report["review_required"] = True
                        report["risk_flags"].append("Review official KuCoin announcement: " + title)
                if page >= total_pages:
                    break
            truncated = total_pages > self.max_announcement_pages
            detail = f"Examined {examined} announcements; no comprehensive project-news or unlock-calendar coverage."
            if truncated:
                report["coverage_gaps"].append("KuCoin announcement feed exceeded the page budget; older matching events may be missing.")
            if bad:
                report["coverage_gaps"].append("Some announcements had invalid dates, links or payloads and were excluded.")
            report["sources"].append(_source("KuCoin announcements", ANNOUNCEMENTS_URL, result=result,
                                               as_of=None, status="partial" if truncated or bad else "ok",
                                               detail=detail, now=now))
            report["announcement_coverage"] = {"lookback_days": self.lookback_days,
                                                "examined": examined, "oldest_seen_at": _iso(oldest),
                                                "truncated": truncated}
            report["events"].sort(key=lambda e: e["published_at"], reverse=True)
            if not report["events"]:
                report["coverage_gaps"].append("No asset-matched announcements were found in the checked feed; this does not establish absence of event risk.")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, EvidenceError) else "KuCoin announcement evidence could not be validated."
            report["sources"].append(_source("KuCoin announcements", ANNOUNCEMENTS_URL, result=result,
                                               status="unavailable", detail=reason, now=now))
            report["coverage_gaps"].append(reason)

    def _defillama(self, report: dict, coin_id: str, now: datetime) -> None:
        url = PROTOCOLS_URL
        try:
            result = self.http.get(url, ttl_seconds=1800)
            protocols = result.data
            if not isinstance(protocols, list):
                raise EvidenceError("DefiLlama protocol list has an unsupported shape.")
            matches = [p for p in protocols if isinstance(p, dict) and p.get("gecko_id") == coin_id]
            if len(matches) != 1:
                raise EvidenceError("No unique DefiLlama protocol mapping exists for this CoinGecko asset; no ticker or child-protocol aggregation was guessed.")
            protocol = matches[0]
            slug = protocol.get("slug")
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                raise EvidenceError("DefiLlama protocol slug is invalid.")
            tvl = _number(protocol.get("tvl"))
            if tvl is not None:
                report["metrics"]["protocol_tvl_usd"] = tvl
            report["sources"].append(_source("DefiLlama TVL", url, result=result, status="partial",
                                               detail="Exact CoinGecko-ID mapping; provider observation time is unspecified.", now=now))
            report["coverage_gaps"].append("DefiLlama current TVL has retrieval provenance but no guaranteed observation timestamp.")
            for data_type, metric in [("dailyFees", "protocol_fees_24h_usd"), ("dailyRevenue", "protocol_revenue_24h_usd")]:
                url = f"https://api.llama.fi/summary/fees/{quote(slug, safe='')}?" + urlencode({"dataType": data_type,
                     "excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"})
                try:
                    metric_result = self.http.get(url, ttl_seconds=900)
                    data = metric_result.data
                    if (not isinstance(data, dict) or data.get("gecko_id") != coin_id
                            or data.get("latestFetchIsOk") is False or data.get("disabled") is True):
                        raise EvidenceError("DefiLlama fee/revenue identity or collection status is not valid.")
                    value = _number(data.get("total24h"), negative=True)
                    if value is None:
                        raise EvidenceError("DefiLlama daily metric is unavailable.")
                    report["metrics"][metric] = value
                    report["sources"].append(_source("DefiLlama " + data_type, url, result=metric_result,
                                                       status="partial", detail="Provider-reported trailing daily metric; observation timestamp unspecified.", now=now))
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, EvidenceError) else "DefiLlama metric could not be validated."
                    report["sources"].append(_source("DefiLlama " + data_type, url, status="unavailable", detail=reason, now=now))
                    report["coverage_gaps"].append(reason)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, EvidenceError) else "DefiLlama evidence could not be validated."
            report["sources"].append(_source("DefiLlama", url, status="unavailable", detail=reason, now=now))
            report["coverage_gaps"].append(reason)
