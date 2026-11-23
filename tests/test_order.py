import pytest

from app.order import Cart, OrderError


@pytest.fixture
def cart(menu):
    return Cart(menu)


def test_prices_sizes_and_option_upcharges(cart):
    cart.add_item("general-tso", size="Large", options=["Extra spicy", "Fried rice"])
    assert cart.lines[0].unit_price_cents == 1295 + 150
    cart.add_item("egg-roll", quantity=3)
    assert cart.subtotal_cents == 1445 + 3 * 175


def test_tax_rounds_half_up(cart):
    cart.add_item("egg-roll", quantity=2)  # 350 * 0.053 = 18.55 -> 19
    assert cart.tax_cents == 19
    assert cart.total_cents == 369


def test_size_required_and_aliases(cart):
    with pytest.raises(OrderError, match="needs a size: Pint or Quart"):
        cart.add_item("wonton-soup")
    assert cart.add_item("wonton-soup", size="qt").size == "Quart"
    assert cart.add_item("general-tso", size="lg").size == "Large"
    with pytest.raises(OrderError, match="doesn't come in"):
        cart.add_item("wonton-soup", size="gallon")
    with pytest.raises(OrderError, match="only comes in one size"):
        cart.add_item("egg-roll", size="Large")


def test_required_option_group(cart):
    with pytest.raises(OrderError, match="needs a choice for Protein"):
        cart.add_item("lo-mein", size="Large")
    line = cart.add_item("lo-mein", size="Large", options=["shrimp"])
    assert line.options == ["Shrimp"] and line.unit_price_cents == 1095 + 150


def test_option_limits_and_unknown_options(cart):
    with pytest.raises(OrderError, match="at most 1"):
        cart.add_item("general-tso", size="Small", options=["Mild", "Spicy"])
    with pytest.raises(OrderError, match="isn't an option"):
        cart.add_item("general-tso", size="Small", options=["Extra cheese"])
    line = cart.add_item("general-tso", size="Small", options=["Mild", "mild"])  # repeat ignored
    assert line.options == ["Mild"]


def test_unknown_and_sold_out_items(menu):
    cart = Cart(menu, sold_out={"crab-rangoon"})
    with pytest.raises(OrderError, match="no menu item"):
        cart.add_item("orange-chicken")
    with pytest.raises(OrderError, match="sold out"):
        cart.add_item("crab-rangoon")


def test_quantity_and_notes_limits(cart):
    with pytest.raises(OrderError):
        cart.add_item("egg-roll", quantity=0)
    with pytest.raises(OrderError, match="more than we take"):
        cart.add_item("egg-roll", quantity=21)
    with pytest.raises(OrderError, match="too long"):
        cart.add_item("egg-roll", notes="x" * 101)


def test_update_and_remove(cart):
    cart.add_item("sesame-chicken", size="Large")
    cart.add_item("soda", options=["Coke"])
    cart.update_item(0, size="Small")
    assert cart.lines[0].size == "Small" and cart.lines[0].unit_price_cents == 895
    cart.update_item(1, quantity=3)
    assert cart.lines[1].quantity == 3 and cart.lines[1].options == ["Coke"]
    removed = cart.remove_item(1)
    assert removed.item_id == "soda" and len(cart.lines) == 1
    with pytest.raises(OrderError, match="no line 5"):
        cart.remove_item(5)
    with pytest.raises(OrderError):
        cart.update_item(0, size="Huge")
    assert cart.lines[0].size == "Small"  # failed update left the line unchanged
