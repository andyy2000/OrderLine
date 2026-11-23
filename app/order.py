"""The cart: adding, changing and pricing line items, with every rule enforced.

Each method either succeeds or raises OrderError with a plain-English message
the LLM can turn into a question for the caller ("Wonton Soup needs a size:
Pint or Quart").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .menu import SIZE_ALIASES, Item, Menu, fmt_money, normalize

MAX_QUANTITY = 20
MAX_NOTES = 100
MAX_LINES = 40
MAX_TOTAL_CENTS = 30_000  # bigger phone orders go to a person (catering, pranks)


class OrderError(Exception):
    """A rule was broken; the message is safe to read to the caller."""


@dataclass
class LineItem:
    item_id: str
    name: str
    kitchen_name: str | None
    size: str | None
    options: list[str]
    quantity: int
    notes: str
    unit_price_cents: int
    includes: list[str] = field(default_factory=list)  # sides that come with it, for the ticket

    @property
    def line_total_cents(self) -> int:
        return self.unit_price_cents * self.quantity

    def describe(self, with_includes: bool = False) -> str:
        size = f" ({self.size})" if self.size else ""
        extras = f", {', '.join(self.options)}" if self.options else ""
        notes = f", note: {self.notes}" if self.notes else ""
        comes = f" (comes with {' and '.join(self.includes)})" if with_includes and self.includes else ""
        return f"{self.quantity} x {self.name}{size}{extras}{notes}{comes}"

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "kitchen_name": self.kitchen_name,
            "size": self.size,
            "options": list(self.options),
            "quantity": self.quantity,
            "notes": self.notes,
            "includes": list(self.includes),
            "unit_price_cents": self.unit_price_cents,
            "line_total_cents": self.line_total_cents,
        }


@dataclass
class Cart:
    menu: Menu
    lines: list[LineItem] = field(default_factory=list)
    sold_out: set[str] = field(default_factory=set)

    # ---- edits --------------------------------------------------------
    def add_item(
        self,
        item_id: str,
        size: str | None = None,
        options: list[str] | None = None,
        quantity: int = 1,
        notes: str = "",
    ) -> LineItem:
        item = self.menu.get(item_id)
        if item is None:
            raise OrderError(f"There is no menu item with id '{item_id}'. Use search_menu first.")
        if not item.available or item.id in self.sold_out:
            raise OrderError(f"Sorry, {item.name} is sold out today.")
        if len(self.lines) >= MAX_LINES:
            raise OrderError("That order is too large for a phone order. Please transfer to staff.")
        line = self._build_line(item, size, options or [], quantity, notes)
        if self.subtotal_cents + line.line_total_cents > MAX_TOTAL_CENTS:
            raise OrderError("That order is over our phone-order limit. Please transfer to staff.")
        self.lines.append(line)
        return line

    def update_item(
        self,
        line_index: int,
        size: str | None = None,
        options: list[str] | None = None,
        quantity: int | None = None,
        notes: str | None = None,
    ) -> LineItem:
        old = self._line(line_index)
        item = self.menu.get(old.item_id)
        assert item is not None
        new = self._build_line(
            item,
            size if size is not None else old.size,
            options if options is not None else old.options,
            quantity if quantity is not None else old.quantity,
            notes if notes is not None else old.notes,
        )
        self.lines[line_index] = new
        return new

    def remove_item(self, line_index: int) -> LineItem:
        self._line(line_index)
        return self.lines.pop(line_index)

    def clear(self) -> None:
        self.lines.clear()

    # ---- totals -------------------------------------------------------
    @property
    def subtotal_cents(self) -> int:
        return sum(line.line_total_cents for line in self.lines)

    @property
    def tax_cents(self) -> int:
        # Round half up to the cent.
        return int(self.subtotal_cents * self.menu.restaurant.tax_rate + 0.5)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.tax_cents

    def summary(self) -> dict:
        return {
            "lines": [
                {"line_index": i, "description": line.describe(),
                 "line_total": fmt_money(line.line_total_cents)}
                for i, line in enumerate(self.lines)
            ],
            "subtotal": fmt_money(self.subtotal_cents),
            "tax": fmt_money(self.tax_cents),
            "total": fmt_money(self.total_cents),
            "item_count": sum(line.quantity for line in self.lines),
        }

    # ---- internals ----------------------------------------------------
    def _line(self, index: int) -> LineItem:
        if not isinstance(index, int) or index < 0 or index >= len(self.lines):
            raise OrderError(
                f"There is no line {index} in the order. Call get_order to see the line numbers."
            )
        return self.lines[index]

    def _build_line(
        self, item: Item, size: str | None, options: list[str], quantity: int, notes: str
    ) -> LineItem:
        if not isinstance(quantity, int) or quantity < 1:
            raise OrderError("Quantity must be at least 1.")
        if quantity > MAX_QUANTITY:
            raise OrderError(
                f"{quantity} is more than we take by phone for one item. Please transfer to staff."
            )
        notes = (notes or "").strip()
        if len(notes) > MAX_NOTES:
            raise OrderError("That special instruction is too long; keep it short.")

        size_name, price = self._resolve_size(item, size)
        chosen = self._resolve_options(item, options)
        unit = price + sum(opt_price for _, opt_price in chosen)
        # A swap option ("Shrimp Roll" in a group that replaces "Pork Egg Roll") changes what
        # comes with the dish instead of being listed as an extra.
        includes = list(item.includes)
        plain_options: list[str] = []
        swaps = {opt.name: group.replaces for group in item.option_groups if group.replaces
                 for opt in group.options}
        for name, _ in chosen:
            replaced = swaps.get(name)
            if replaced and replaced in includes:
                includes[includes.index(replaced)] = name
            else:
                plain_options.append(name)
        return LineItem(
            item_id=item.id,
            name=item.name,
            kitchen_name=item.kitchen_name,
            size=size_name,
            options=plain_options,
            quantity=quantity,
            notes=notes,
            unit_price_cents=unit,
            includes=includes,
        )

    @staticmethod
    def _resolve_size(item: Item, size: str | None) -> tuple[str | None, int]:
        if not item.sizes:
            if size:
                raise OrderError(f"{item.name} only comes in one size.")
            return None, item.price_cents or 0
        names = " or ".join(s.name for s in item.sizes)
        if not size:
            raise OrderError(f"{item.name} needs a size: {names}.")
        wanted = normalize(size)
        wanted = SIZE_ALIASES.get(wanted, wanted)
        for s in item.sizes:
            if normalize(s.name) == wanted:
                return s.name, s.price_cents
        raise OrderError(f"{item.name} doesn't come in '{size}'. Sizes are {names}.")

    @staticmethod
    def _resolve_options(item: Item, options: list[str]) -> list[tuple[str, int]]:
        chosen: list[tuple[str, int]] = []
        counts: dict[str, int] = {g.name: 0 for g in item.option_groups}
        for raw in options:
            wanted = normalize(raw)
            match = None
            for group in item.option_groups:
                for opt in group.options:
                    if normalize(opt.name) == wanted:
                        match = (group, opt)
                        break
                if match:
                    break
            if match is None:
                allowed = "; ".join(
                    f"{g.name}: {', '.join(o.name for o in g.options)}" for g in item.option_groups
                ) or "none"
                raise OrderError(f"'{raw}' isn't an option for {item.name}. Options are {allowed}.")
            group, opt = match
            if any(name == opt.name for name, _ in chosen):
                continue  # said twice; ignore the repeat
            counts[group.name] += 1
            if counts[group.name] > group.max:
                raise OrderError(f"{item.name} allows at most {group.max} choice(s) for {group.name}.")
            chosen.append((opt.name, opt.price_cents))
        for group in item.option_groups:
            if counts[group.name] < group.min:
                names = ", ".join(o.name for o in group.options)
                raise OrderError(f"{item.name} needs a choice for {group.name}: {names}.")
        return chosen
