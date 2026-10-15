import json

from bot.pricefeeds import Binance, Bitget, Bybit, Coinbase, Kraken, OKX, PriceHub

ASSETS = ["BTC", "ETH", "HYPE"]


def test_binance_parse_uses_mid_of_book_ticker():
    spec = Binance(ASSETS)
    msg = json.dumps({"stream": "btcusdt@bookTicker", "data": {"s": "BTCUSDT", "b": "100.0", "a": "102.0"}})
    assert spec.parse(msg) == [("BTC", 101.0)]


def test_binance_does_not_list_hype():
    assert "HYPE" not in Binance(ASSETS).want


def test_coinbase_parse():
    spec = Coinbase(ASSETS)
    msg = json.dumps({"type": "ticker", "product_id": "HYPE-USD", "price": "90.83", "best_bid": "90.82", "best_ask": "90.84"})
    assert spec.parse(msg) == [("HYPE", 90.83)]
    assert spec.parse(json.dumps({"type": "subscriptions"})) == []


def test_kraken_okx_bybit_bitget_parse():
    k = json.dumps({"channel": "ticker", "data": [{"symbol": "ETH/USD", "bid": 2500.0, "ask": 2500.2, "last": 2500.1}]})
    assert Kraken(ASSETS).parse(k)[0][0] == "ETH"
    o = json.dumps({"arg": {"channel": "tickers"}, "data": [{"instId": "BTC-USDT", "bidPx": "80000", "askPx": "80002", "last": "80001"}]})
    assert OKX(ASSETS).parse(o) == [("BTC", 80001.0)]
    b = json.dumps({"topic": "tickers.BTCUSDT", "data": {"symbol": "BTCUSDT", "lastPrice": "80010"}})
    assert Bybit(ASSETS).parse(b) == [("BTC", 80010.0)]
    g = json.dumps({"arg": {"channel": "ticker"}, "data": [{"instId": "BTCUSDT", "bidPr": "80000", "askPr": "80004", "lastPr": "80002"}]})
    assert Bitget(ASSETS).parse(g) == [("BTC", 80002.0)]


def test_composite_is_median_and_ignores_stale_and_outlier_exchanges():
    hub = PriceHub(ASSETS)
    now = 1000.0
    hub.on_exchange("a", "BTC", 100.0, now)
    hub.on_exchange("b", "BTC", 101.0, now)
    hub.on_exchange("c", "BTC", 500.0, now)            # broken exchange: must not move the median
    hub.on_exchange("d", "BTC", 99.0, now - 10)        # stale: does not count
    assert hub.composite("BTC", now) == 101.0


def test_basis_correction_maps_exchange_level_to_chainlink_level():
    hub = PriceHub(ASSETS)
    now = 1000.0
    hub.on_exchange("a", "BTC", 100.0, now)
    hub.on_chainlink("BTC", 99.9, now, now)             # Chainlink 0.1% below the exchanges
    hub.on_exchange("a", "BTC", 102.0, now + 1)         # the exchanges move up
    assert abs(hub.est_spot("BTC", now + 1) - 102.0 * 0.999) < 1e-6


def test_reference_price_needs_the_start_to_have_been_observed():
    hub = PriceHub(ASSETS)
    for i in range(10):
        hub.on_chainlink("BTC", 100.0 + i, 1000.0 + i, 1000.0 + i)
    assert hub.ref_at("BTC", 1004.5) == 104.0           # last tick at or before the timestamp
    assert hub.ref_at("BTC", 900.0) is None              # the bot had not started listening yet


def test_twap_obs_averages_ticks_inside_the_range():
    hub = PriceHub(ASSETS)
    for i in range(10):
        hub.on_chainlink("BTC", 100.0 + i, 1000.0 + i, 1000.0 + i)
    assert hub.twap_obs("BTC", 1005.0, 1009.0) == sum(range(105, 110)) / 5
    assert hub.twap_obs("BTC", 2000.0, 2010.0) is None
