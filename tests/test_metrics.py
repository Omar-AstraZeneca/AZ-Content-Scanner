from app.metrics.pilot_metrics import _f1_score


def test_f1_is_zero_when_precision_and_recall_are_zero():
    assert _f1_score(0.0, 0.0) == 0.0


def test_f1_is_undefined_when_a_metric_is_undefined():
    assert _f1_score(None, 0.5) is None


def test_f1_for_perfect_precision_and_recall():
    assert _f1_score(1.0, 1.0) == 1.0