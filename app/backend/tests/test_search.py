"""Tests for search routing, URL detection, and finance/weather/news classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from search import (
    _deduplicate,
    _extract_location,
    _is_finance_query,
    _is_news_query,
    _is_weather_query,
    _parse_rss,
    extract_urls,
    needs_search,
)

# ── needs_search ───────────────────────────────────────────────────────────────

class TestNeedsSearch:
    # Should trigger search
    def test_weather_triggers(self):
        assert needs_search("weather in chicago today")

    def test_news_triggers(self):
        assert needs_search("latest headlines")

    def test_today_triggers(self):
        assert needs_search("what happened today")

    def test_bitcoin_triggers(self):
        assert needs_search("bitcoin price")

    def test_recent_year_triggers(self):
        assert needs_search("what happened in 2025")

    def test_how_much_is_pattern(self):
        assert needs_search("how much is a plane ticket")

    def test_who_is_president(self):
        assert needs_search("who is the current president of france")

    def test_standings_triggers(self):
        assert needs_search("NBA standings")

    # Should NOT trigger search
    def test_coding_question_no_search(self):
        assert not needs_search("how do I reverse a list in python")

    def test_math_question_no_search(self):
        assert not needs_search("what is 15 percent of 200")

    def test_writing_question_no_search(self):
        assert not needs_search("write me a cover letter for a software job")

    def test_general_knowledge_no_search(self):
        assert not needs_search("explain the difference between tcp and udp")

    def test_definition_no_search(self):
        assert not needs_search("what is recursion")

    def test_url_in_message_skips_search(self):
        assert not needs_search("what do you think about https://example.com")

    def test_bare_domain_skips_search(self):
        assert not needs_search("check out joeriggs.com")

    def test_www_domain_skips_search(self):
        assert not needs_search("go to www.github.com")

    def test_sql_acronym_no_search(self):
        assert not needs_search("how do I write a SQL JOIN")

    def test_api_acronym_no_search(self):
        assert not needs_search("how does a REST API work")


# ── extract_urls ───────────────────────────────────────────────────────────────

class TestExtractUrls:
    def test_https_url(self):
        assert "https://example.com" in extract_urls("visit https://example.com today")

    def test_http_url(self):
        assert "http://example.com" in extract_urls("see http://example.com for details")

    def test_www_url(self):
        urls = extract_urls("go to www.github.com for code")
        assert any("www.github.com" in u for u in urls)

    def test_bare_domain_com(self):
        urls = extract_urls("check out joeriggs.com")
        assert any("joeriggs.com" in u for u in urls)

    def test_bare_domain_io(self):
        urls = extract_urls("look at fastapi.io")
        assert any("fastapi.io" in u for u in urls)

    def test_malformed_https_colon_no_slash(self):
        urls = extract_urls("what do you think about https:joeriggs.com")
        assert len(urls) >= 1

    def test_no_url_returns_empty(self):
        assert extract_urls("what is the capital of france") == []

    def test_multiple_urls(self):
        urls = extract_urls("see https://a.com and https://b.com")
        assert len(urls) == 2

    def test_url_with_path(self):
        urls = extract_urls("check https://github.com/user/repo")
        assert any("/user/repo" in u for u in urls)


# ── _is_weather_query ──────────────────────────────────────────────────────────

class TestIsWeatherQuery:
    def test_weather_word(self):
        assert _is_weather_query("weather in new york")

    def test_forecast_word(self):
        assert _is_weather_query("forecast for tomorrow")

    def test_rain_word(self):
        assert _is_weather_query("will it rain today")

    def test_temperature_word(self):
        assert _is_weather_query("what is the temperature outside")

    def test_non_weather(self):
        assert not _is_weather_query("what is the capital of france")

    def test_non_weather_news(self):
        assert not _is_weather_query("latest news today")


# ── _is_news_query ─────────────────────────────────────────────────────────────

class TestIsNewsQuery:
    def test_news_word(self):
        assert _is_news_query("latest news")

    def test_headlines_word(self):
        assert _is_news_query("today's headlines")

    def test_breaking_word(self):
        assert _is_news_query("breaking: explosion in city")

    def test_weather_query_not_routed_to_news(self):
        # "weather today" has "today" in _NEWS_TERMS but should not hit news
        assert not _is_news_query("weather today in london")

    def test_non_news(self):
        assert not _is_news_query("how do I write a for loop")


# ── _is_finance_query ──────────────────────────────────────────────────────────

class TestIsFinanceQuery:
    # Should trigger
    def test_stock_word(self):
        assert _is_finance_query("apple stock price")

    def test_bitcoin_alone(self):
        assert _is_finance_query("bitcoin")

    def test_ethereum_alone(self):
        assert _is_finance_query("ethereum price today")

    def test_nasdaq_alone(self):
        assert _is_finance_query("nasdaq today")

    def test_etf_word(self):
        assert _is_finance_query("best etf to buy")

    def test_earnings_word(self):
        assert _is_finance_query("nvidia earnings report")

    def test_ipo_word(self):
        assert _is_finance_query("upcoming ipo 2025")

    def test_dollar_ticker(self):
        assert _is_finance_query("what is $AAPL trading at")

    def test_unambiguous_crypto(self):
        assert _is_finance_query("how do I buy solana")

    def test_ambiguous_name_with_signal(self):
        assert _is_finance_query("apple shares fell today")

    def test_ambiguous_name_without_signal_no_trigger(self):
        # "apple" alone, without any finance signal, should NOT trigger
        assert not _is_finance_query("apple pie recipe")

    def test_meta_without_signal_no_trigger(self):
        assert not _is_finance_query("metadata extraction in python")

    def test_gold_without_signal_no_trigger(self):
        assert not _is_finance_query("gold color for my bedroom")

    def test_oil_without_signal_no_trigger(self):
        assert not _is_finance_query("oil change for my car")

    def test_silver_without_signal_no_trigger(self):
        assert not _is_finance_query("silver lining in the clouds")

    def test_dow_without_signal_no_trigger(self):
        assert not _is_finance_query("download the file")

    # Common false positives — must NOT trigger finance search
    def test_sql_no_trigger(self):
        assert not _is_finance_query("how do I write a SQL query")

    def test_html_no_trigger(self):
        assert not _is_finance_query("how does HTML work")

    def test_api_no_trigger(self):
        assert not _is_finance_query("REST API tutorial")

    def test_market_alone_no_trigger(self):
        assert not _is_finance_query("the market for electric cars is growing")

    def test_price_alone_no_trigger(self):
        assert not _is_finance_query("what is the price of a coffee")

    def test_index_alone_no_trigger(self):
        assert not _is_finance_query("index out of range error in python")

    def test_trade_alone_no_trigger(self):
        assert not _is_finance_query("trade routes in ancient rome")

    def test_fund_alone_no_trigger(self):
        assert not _is_finance_query("fund a new startup idea")

    def test_options_alone_no_trigger(self):
        assert not _is_finance_query("options for handling errors in python")


# ── _extract_location ──────────────────────────────────────────────────────────

class TestExtractLocation:
    def test_strips_weather_word(self):
        loc = _extract_location("weather in chicago")
        assert "chicago" in loc.lower()
        assert "weather" not in loc.lower()

    def test_strips_forecast_word(self):
        loc = _extract_location("forecast for new york")
        assert "new york" in loc.lower()

    def test_strips_today(self):
        loc = _extract_location("weather today in london")
        assert "london" in loc.lower()
        assert "today" not in loc.lower()

    def test_falls_back_to_full_query_when_all_stripped(self):
        # All words are stopwords — should return original query
        result = _extract_location("weather in the forecast")
        assert result  # must not be empty


# ── _deduplicate ───────────────────────────────────────────────────────────────

class TestDeduplicate:
    def test_removes_exact_url_duplicate(self):
        items = [
            {"title": "Story A", "url": "https://a.com"},
            {"title": "Story B", "url": "https://a.com"},  # same URL
        ]
        result = _deduplicate(items, "title", "url")
        assert len(result) == 1
        assert result[0]["title"] == "Story A"

    def test_removes_similar_titles(self):
        items = [
            {"title": "Apple reports record quarterly earnings today", "url": "https://a.com"},
            {"title": "Apple reports record quarterly earnings", "url": "https://b.com"},
        ]
        result = _deduplicate(items, "title", "url")
        assert len(result) == 1

    def test_keeps_distinct_stories(self):
        items = [
            {"title": "Apple stock rises sharply", "url": "https://a.com"},
            {"title": "Tesla recalls one million vehicles", "url": "https://b.com"},
            {"title": "NASA launches new Mars mission", "url": "https://c.com"},
        ]
        result = _deduplicate(items, "title", "url")
        assert len(result) == 3

    def test_empty_input(self):
        assert _deduplicate([], "title", "url") == []


# ── _parse_rss ─────────────────────────────────────────────────────────────────

_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Markets rally on strong jobs data</title>
      <link>https://news.example.com/1</link>
      <description>Stocks climbed Friday...</description>
      <pubDate>Mon, 06 Apr 2026 10:00:00 GMT</pubDate>
      <source url="https://reuters.com">Reuters</source>
    </item>
    <item>
      <title>Fed holds rates steady</title>
      <link>https://news.example.com/2</link>
      <description>The Federal Reserve...</description>
      <pubDate>Mon, 06 Apr 2026 09:30:00 GMT</pubDate>
      <source url="https://ap.com">AP</source>
    </item>
    <item>
      <title>Markets rally on strong jobs data</title>
      <link>https://news.example.com/3</link>
      <description>Duplicate story...</description>
      <pubDate>Mon, 06 Apr 2026 10:05:00 GMT</pubDate>
      <source url="https://other.com">Other</source>
    </item>
  </channel>
</rss>"""

class TestParseRss:
    def test_parses_titles(self):
        articles = _parse_rss(_SAMPLE_RSS, max_results=10)
        titles = [a["title"] for a in articles]
        assert "Markets rally on strong jobs data" in titles

    def test_parses_source(self):
        articles = _parse_rss(_SAMPLE_RSS, max_results=10)
        sources = [a["source"] for a in articles]
        assert "Reuters" in sources

    def test_deduplicates_similar_titles(self):
        articles = _parse_rss(_SAMPLE_RSS, max_results=10)
        # 3 items but 2 have near-identical titles — should deduplicate to 2
        assert len(articles) == 2

    def test_max_results_respected(self):
        articles = _parse_rss(_SAMPLE_RSS, max_results=1)
        assert len(articles) == 1

    def test_strips_html_from_description(self):
        rss = """<?xml version="1.0"?><rss><channel><item>
          <title>Test</title><link>https://x.com</link>
          <description><![CDATA[<p>Clean <b>text</b> here</p>]]></description>
          <pubDate>Mon, 06 Apr 2026</pubDate>
        </item></channel></rss>"""
        articles = _parse_rss(rss, max_results=5)
        assert "<p>" not in articles[0]["desc"]
        assert "<b>" not in articles[0]["desc"]
        assert "Clean" in articles[0]["desc"]
