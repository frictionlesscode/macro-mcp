import importlib

import pytest

calibrate = importlib.import_module("calibrate")


def cal_db():
    import sqlite3

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(calibrate.SCHEMA)
    return conn


def test_pct_error_signed():
    assert calibrate.pct_error(110, 100) == pytest.approx(10.0)
    assert calibrate.pct_error(90, 100) == pytest.approx(-10.0)


def test_pct_error_undefined_when_truth_zero():
    assert calibrate.pct_error(5, 0) is None


def test_percentile_matches_median_for_two_points():
    assert calibrate.percentile([10.0, 20.0], 0.5) == pytest.approx(15.0)


def test_percentile_single_value():
    assert calibrate.percentile([42.0], 0.9) == 42.0


def test_parse_macros_rejects_wrong_count():
    with pytest.raises(SystemExit):
        calibrate.parse_macros("1,2,3", "truth")


def test_parse_macros_rejects_negative():
    with pytest.raises(SystemExit):
        calibrate.parse_macros("-1,0,0,0,0", "truth")


def test_summarise_bias_direction():
    # consistent over-estimate
    s = calibrate.summarise([10.0, 12.0, 8.0, 11.0, 9.0])
    assert s["bias"] > 0
    assert s["n"] == 5


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_add_and_report_end_to_end(capsys):
    conn = cal_db()
    calibrate.cmd_add(conn, Args(
        name="Chobani 150g", condition="mass_known", truth="90,16,7,0,0",
        est="110,18,8,1,0", est_confidence="high", notes=None,
    ))
    row = conn.execute("SELECT * FROM sample").fetchone()
    assert row["truth_kcal"] == 90
    assert row["est_kcal"] == 110

    calibrate.cmd_report(conn, Args())
    out = capsys.readouterr().out
    assert "mass_known" in out
    assert "mass_unknown" in out
    assert "M1 gate: NOT satisfied" in out  # only 1 sample, min is 5


def test_gate_satisfied_with_enough_samples(capsys):
    conn = cal_db()
    for i in range(5):
        calibrate.cmd_add(conn, Args(
            name=f"item{i}", condition="mass_known", truth="100,10,10,10,2",
            est="105,11,9,10,2", est_confidence="medium", notes=None,
        ))
        calibrate.cmd_add(conn, Args(
            name=f"restaurant{i}", condition="mass_unknown", truth="600,30,60,20,5",
            est="500,25,55,18,4", est_confidence="low", notes=None,
        ))
    calibrate.cmd_report(conn, Args())
    out = capsys.readouterr().out
    assert "M1 gate: satisfied" in out
