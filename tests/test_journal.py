import csv

from bot.updown import Journal, Window


def _win():
    return Window(asset="BTC", tf="5m", slug="btc-updown-5m-1000", market_id="1", condition_id="", start_ts=1000,
                  end_ts=1300, token_up="a", token_dn="b", title="t", liquidity=1.0)


def test_journal_writes_both_price_models_and_the_outcome(tmp_path):
    j = Journal(tmp_path)
    j.snap(_win(), 1100.0, 0.61, {"ask": 0.6, "bid": 0.59}, {"ask": 0.41, "bid": 0.4}, 80000.0, "chainlink", 80010.0, None, 5,
           p_bl=0.61, p_cl=0.55, spot_cl=80008.0)
    j.flush()
    rows = list(csv.DictReader(open(tmp_path / "calibration.csv", encoding="utf-8")))
    assert rows[0]["p_blend"] == "0.6100" and rows[0]["p_chainlink"] == "0.5500" and rows[0]["spot_cl"] == "80008"
    j.outcome("btc-updown-5m-1000", 1.0, 80020.0)
    out = list(csv.DictReader(open(tmp_path / "outcomes.csv", encoding="utf-8")))
    assert out[0]["pred_up"] == "1" and out[0]["actual_up"] == "1"


def test_journal_archives_files_with_an_old_header(tmp_path):
    (tmp_path / "calibration.csv").write_text("ts,slug\n1,x\n", encoding="utf-8")
    Journal(tmp_path)
    assert any(p.name.startswith("calibration_old_") for p in tmp_path.iterdir())
    assert (tmp_path / "calibration.csv").read_text(encoding="utf-8").startswith("ts,slug,asset")
