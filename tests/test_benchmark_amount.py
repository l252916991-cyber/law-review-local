import pytest

from app.benchmark_amount import crime_amount, format_amount


@pytest.mark.parametrize(("text", "expected", "method"), [
    ("骗取四人现金共计18500元；另一次骗取现金共计3500元。", 18500, "explicit_total"),
    ("用游戏机兑换20000元，扣除费用后实际得款16000余元，分得赃款3000元。", 16000, "actual_proceeds"),
    ("盗走现金500元及电脑。经鉴定，被盗电脑价值4176元。", 4676, "component_sum"),
    ("盗走钱包。钱包内有1400余元现金。被盗车辆价值为2887元。", 4287, "component_sum"),
    ("骗取12000元；骗取20000元和6000元；骗取11500元（已退赔5000元）。", 49500, "component_sum"),
    ("案发后查获赃款9800元并退赔500元。", None, "no_confident_amount"),
])
def test_reference_free_crime_amount(text, expected, method):
    value, audit = crime_amount(text)
    assert value == expected and audit["method"] == method


def test_formats_integer_and_decimal():
    assert format_amount(60500.0) == "[金额]60500元<eoa>"
    assert format_amount(12.5) == "[金额]12.5元<eoa>"
