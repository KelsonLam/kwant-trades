from __future__ import annotations

import pytest

from kwanttrades.cli import main
from kwanttrades.execution.journal import Journal
from kwanttrades.report import esc, render_report
from kwanttrades.types import Leg, OptionQuote, Right, Side, TradeIdea
from datetime import date
from tests.test_app_fakes import FakeEngine, long_call_idea


def eng(cfg):
    return FakeEngine(lambda a: [long_call_idea(a.chain, a.symbol)])


def base(tmp_path):
    return ["--config", str(tmp_path / "none.toml"), "--log-file", str(tmp_path / "x.log"), "--db", str(tmp_path / "j.sqlite")]


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for c in ("scan", "run", "shadow", "backtest", "record", "report", "status"):
        assert c in out


def test_scan_synthetic_prints_table_and_source_line(tmp_path, capsys):
    rc = main(base(tmp_path) + ["scan", "--provider", "synthetic", "--symbols", "SPY", "--no-news"], engine_factory=eng)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Ranked ideas" in out and "long_call" in out and "PASS" in out
    assert "Data source: synthetic" in out and "SPY" in out and "ATM IV" in out
    assert not (tmp_path / "j.sqlite").exists()  # scan never writes the live journal


def test_scan_json_output(tmp_path, capsys):
    import json
    rc = main(base(tmp_path) + ["scan", "--provider", "synthetic", "--symbols", "SPY", "--no-news", "--json"], engine_factory=eng)
    assert rc == 0 and json.loads(capsys.readouterr().out)["mode"] == "scan"


def test_bad_config_is_one_line_error(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not [valid", encoding="utf-8")
    rc = main(["--config", str(bad), "--log-file", str(tmp_path / "x.log"), "status"])
    err = capsys.readouterr().err.strip().splitlines()
    assert rc == 2 and len(err) == 1 and err[0].startswith("error:")


def test_bad_broker_mode_and_symbol(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text('[broker]\nmode = "casino"\n', encoding="utf-8")
    assert main(["--config", str(cfgp), "--log-file", str(tmp_path / "x.log"), "status"]) == 2
    assert "broker.mode" in capsys.readouterr().err
    assert main(base(tmp_path) + ["scan", "--provider", "synthetic", "--symbols", "SP;Y"], engine_factory=eng) == 2


def test_alpaca_live_without_ack_exits_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("KWANT_ALLOW_LIVE", raising=False)
    cfgp = tmp_path / "c.toml"
    cfgp.write_text('[broker]\nmode = "alpaca_live"\n', encoding="utf-8")
    rc = main(["--config", str(cfgp), "--log-file", str(tmp_path / "x.log"), "--db", str(tmp_path / "j.sqlite"),
               "run", "--once", "--provider", "synthetic", "--symbols", "SPY"], engine_factory=eng)
    assert rc == 2 and "cannot start alpaca_live" in capsys.readouterr().err


def test_shadow_once_and_status_and_report(tmp_path, capsys):
    b = base(tmp_path)
    assert main(b + ["shadow", "--once", "--provider", "synthetic", "--symbols", "SPY", "--no-news"], engine_factory=eng) == 0
    out = capsys.readouterr().out
    assert "shadow" in out
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.orders() == [] or all(o.id.startswith("shadow-") for o in j.orders())
    j.close()
    assert main(b + ["status"]) == 0
    assert "Equity" in capsys.readouterr().out
    out_html = tmp_path / "r.html"
    assert main(b + ["report", "--out", str(out_html)]) == 0
    assert out_html.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_status_without_journal(tmp_path, capsys):
    assert main(base(tmp_path) + ["status"]) == 0
    assert "No journal yet" in capsys.readouterr().out


def test_backtest_cli_simulated(tmp_path, capsys):
    jp, hp = tmp_path / "bt.json", tmp_path / "bt.html"
    rc = main(base(tmp_path) + ["backtest", "--source", "simulated", "--days", "2", "--symbols", "SPY", "--step-minutes", "120",
                                "--json", str(jp), "--report", str(hp)], engine_factory=eng)
    out = capsys.readouterr().out
    assert rc == 0 and "CAVEATS" in out and "Option prices are synthetic" in out
    assert jp.exists() and "<svg" in hp.read_text(encoding="utf-8")


def test_report_escapes_script_strings(tmp_path, capsys):
    evil = "<script>alert(1)</script>"
    html = render_report({"title": evil, "tiles": [(evil, evil, None)], "equity": [("a", 1.0), ("b", 2.0)],
                          "open_positions": [{"symbol": evil, "strategy": evil, "contracts": 1}],
                          "closed_trades": [{"symbol": evil, "pnl": 1.0, "exit_reason": evil}],
                          "decisions": [{"symbol": evil, "reasons": [evil], "passed": False}], "caveats": [evil]})
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html
    assert esc(None) == "" and esc("<b>") == "&lt;b&gt;"
    # through the CLI: a journaled decision with a hostile strategy name
    q = OptionQuote("SPY", date(2026, 12, 18), 500.0, Right.CALL, 1.0, 1.1)
    idea = TradeIdea("SPY", evil, [Leg(q, Side.BUY)], 1.05, 1.1, 105.0, float("inf"), 0.4, 10.0, 0.1)
    jp = tmp_path / "j.sqlite"
    j = Journal(str(jp))
    j.record_decision(idea, "BLOCK", 0)
    j.close()
    out = tmp_path / "r.html"
    assert main(base(tmp_path) + ["report", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "<script>alert" not in text and "&lt;script&gt;" in text
