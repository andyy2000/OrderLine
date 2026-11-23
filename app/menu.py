"""Menu and restaurant data: models, loading, search, and opening hours.

Everything about prices lives here and in order.py. The LLM never computes a
price; it only picks item ids and option names that this code validates.
All money is integer cents.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, time
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator
o
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Short forms callers and staff use for sizes.
SIZE_ALIASES = {
    "sm": "small", "s": "small", "lg": "large", "l": "large", "lrg": "large",
    "med": "medium", "m": "medium", "pt": "pint", "qt": "quart",
    "reg": "regular", "big": "large", "little": "small",
}


def fmt_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def normalize(text: str) -> str:
    """Lowercase, drop apostrophes and punctuation, collapse spaces."""
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9一-鿿]+", " ", text)
    return " ".join(text.split())


# Words speech-to-text hears fine on its own; boosting them wastes the keyterm budget.
ORDINARY_MENU_WORDS = {
    "chicken", "beef", "pork", "shrimp", "fish", "with", "and", "fried", "rice", "soup", "sauce",
    "vegetable", "vegetables", "house", "special", "roast", "combo", "mixed", "hot", "sour", "egg",
    "drop", "noodle", "noodles", "chinese", "onion", "onions", "meat", "crab", "baby", "curry", "style",
    "home", "garlic", "sauteed", "black", "bean", "snow", "peas", "chili", "pepper", "steak", "mushroom",
    "mushrooms", "white", "brown", "small", "large", "can", "soda", "roll", "rolls", "spare", "ribs",
    "boneless", "sweet", "orange", "lemon", "sesame", "honey", "spicy", "crispy", "plain", "side",
    "order", "dinner", "lunch", "all", "day", "the", "of", "a", "in", "on", "pieces", "piece",
}
# Terms phone speech-to-text gets wrong in this cuisine; boosted whenever the menu uses them.
CUISINE_KEYTERMS = [
    "General Tso's", "Kung Po", "Kung Pao", "Lo Mein", "Mei Fun", "Egg Foo Young", "Chow Mein",
    "Moo Shu", "Chop Suey", "Wonton", "Szechuan", "Hunan", "Rangoon", "Yat Gaw Mein", "Tofu",
    "Bean Curd", "Teriyaki", "Mongolian", "Bourbon", "Cashew", "Pu Pu Platter", "Egg Roll",
]
HEAD_WORDS = {"chicken", "beef", "pork", "shrimp", "soup", "tofu"}
# Words that say which way a caller wants a dish that comes several ways.
VARIANT_WORDS = {"combo", "combination", "special", "plate", "regular", "dinner", "lunch", "entree", "pint", "quart",
                 "small", "large", "pt", "qt"}

# Words callers add that carry no information about which dish they mean.
FILLER_WORDS = {"a", "an", "the", "of", "with", "and", "some", "order", "orders", "please",
                "one", "two", "three", "four", "five", "large", "small", "medium", "pint",
                "quart", "lg", "sm", "size", "get", "want", "like", "can", "i", "me", "to"}

_SOUND_RULES = [
    (r"ph", "f"), (r"ck", "k"), (r"qu", "k"), (r"^kn", "n"), (r"^wr", "r"), (r"^ts", "s"),
    (r"^x", "s"), (r"wh", "w"), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"sh|ch|tch", "x"),
    (r"gh", ""), (r"z", "s"), (r"dg|j", "g"), (r"y", "i"), (r"(ao|au|ow)", "o"),
    (r"(ai|ay|ei|ey)", "e"), (r"(oo|ou|ew|ue)", "u"),
]


def sound_key(word: str) -> str:
    """A crude phonetic key so 'ragoon' ~ 'rangoon', 'sauce' ~ 'tsos', 'main' ~ 'mein'.

    Speech-to-text hears dish names phonetically; this makes two spellings that
    sound alike compare as similar.
    """
    w = word.lower()
    for pattern, repl in _SOUND_RULES:
        w = re.sub(pattern, repl, w)
    if not w:
        return ""
    head, rest = w[0], re.sub(r"[aeiou]", "", w[1:])
    rest = re.sub(r"(.)\1+", r"\1", rest)  # collapse doubles
    return head + rest


def sound_text(text: str) -> str:
    return " ".join(sound_key(t) for t in normalize(text).split())


def content_tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t not in FILLER_WORDS}


class Option(BaseModel):
    name: str
    price_cents: int = 0


class OptionGroup(BaseModel):
    name: str
    min: int = 0
    max: int = 1
    options: list[Option]
    replaces: str | None = None  # a swap: choosing an option replaces this included side on the ticket

    @property
    def required(self) -> bool:
        return self.min > 0


class Size(BaseModel):
    name: str
    price_cents: int


class Item(BaseModel):
    id: str
    name: str
    category: str
    price_cents: int | None = None  # used when the item has no sizes
    sizes: list[Size] = Field(default_factory=list)
    option_groups: list[OptionGroup] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    kitchen_name: str | None = None  # printed on the ticket, e.g. Chinese name
    description: str | None = None
    short_name: str | None = None  # how the menu digest lists it inside its category ("General Tso's Chicken" in Combos)
    includes: list[str] = Field(default_factory=list)  # comes with: ["Pork Fried Rice", "Pork Egg Roll"]
    tags: list[str] = Field(default_factory=list)      # "spicy", "popular", "vegetarian"
    confirm: str | None = None  # something a person must check before going live (a guessed price, etc.)
    available: bool = True

    def comes_with(self) -> str:
        return " and ".join(self.includes) if len(self.includes) <= 2 else \
            ", ".join(self.includes[:-1]) + " and " + self.includes[-1]

    @model_validator(mode="after")
    def _has_a_price(self) -> "Item":
        if not self.sizes and self.price_cents is None:
            raise ValueError(f"item {self.id} needs either price_cents or sizes")
        return self

    def price_label(self) -> str:
        if self.sizes:
            return ", ".join(f"{s.name} {fmt_money(s.price_cents)}" for s in self.sizes)
        return fmt_money(self.price_cents or 0)


class Restaurant(BaseModel):
    id: str
    name: str
    phone: str  # the real line calls are transferred to, E.164 e.g. +18045550123
    address: str
    timezone: str = "America/New_York"
    tax_rate: float = 0.0  # e.g. 0.053 for 5.3%
    pickup_minutes: int = 15
    hours: dict[str, list[tuple[str, str]]]  # "mon": [["11:00", "22:00"]]
    payment_note: str = "Pay at pickup."
    confirm: str | None = None  # e.g. "tax rate and phone number still need checking"


class Menu(BaseModel):
    restaurant: Restaurant
    items: list[Item]
    category_notes: dict[str, str] = Field(default_factory=dict)    # "Chicken": "Served with white rice"
    category_confirm: dict[str, str] = Field(default_factory=dict)  # category-wide guesses a person must check

    @model_validator(mode="after")
    def _unique_ids(self) -> "Menu":
        ids = [i.id for i in self.items]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate item ids: {sorted(dupes)}")
        return self

    # ---- lookup -------------------------------------------------------
    def get(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            if item.category not in seen:
                seen.append(item.category)
        return seen

    def search(self, query: str, limit: int = 5) -> list[tuple[Item, float]]:
        """Fuzzy-match what a caller said to menu items. Returns (item, score), best first.

        Three signals per label (name or alias): spelling similarity, how it
        sounds (for speech-to-text errors), and shared meaningful words
        ("broccoli" and "chicken", not "large" or "a").
        """
        q = normalize(query)
        if not q:
            return []
        q_tokens = content_tokens(q) or set(q.split())
        q_sound = sound_text(" ".join(sorted(q_tokens))) if q_tokens else sound_text(q)
        scored: list[tuple[Item, float, bool]] = []
        for item in self.items:
            best, exact = 0.0, False
            for label in self._labels(item):
                n = normalize(label)
                if q == n:
                    best, exact = 1.0, True
                    break
                n_tokens = content_tokens(n) or set(n.split())
                spelling = SequenceMatcher(None, q, n).ratio()
                sound = SequenceMatcher(None, q_sound, sound_text(" ".join(sorted(n_tokens)))).ratio()
                overlap = len(q_tokens & n_tokens) / max(len(q_tokens | n_tokens), 1)
                covered = len(q_tokens & n_tokens) / max(len(q_tokens), 1)
                score = max(spelling, sound * 0.95, 0.5 * sound + 0.5 * overlap, 0.45 * spelling + 0.55 * covered)
                best = max(best, score)
            if best >= 0.5:
                scored.append((item, round(best, 3), exact))
        # an exact name/alias match outranks a same-score fuzzy match ("House Special Lo Mein Special")
        scored.sort(key=lambda t: (not t[2], -t[1], t[0].name))
        return [(item, score) for item, score, _ in scored[:limit]]

    def resolve(self, query: str) -> tuple[Item | None, list[Item]]:
        """Pick the one item a caller means, or return the candidates if it's unclear.

        Returns (item, []) when confident, (None, candidates) when two items are
        close (ask the caller) and (None, []) when nothing matches.
        """
        matches = self.search(query, limit=8)
        if not matches:
            return None, []
        top, top_score = matches[0]
        runners = [item for item, score in matches[1:] if score >= top_score - 0.12]
        confident = top_score >= 0.97 or (top_score >= 0.72 and not runners)
        if confident:
            # "bourbon chicken" when the menu has Bourbon Chicken, ...Combo and ...Special:
            # ask which, unless the caller already said "combo", "special", etc.
            variants = self.variants(top)
            q_words = set(normalize(query).split())
            if variants and not (q_words & VARIANT_WORDS):
                return None, [top, *variants]
            return top, []
        return None, [top, *runners] if top_score >= 0.5 else []

    @staticmethod
    def _labels(item: Item) -> list[str]:
        """Name, aliases, and option+name combos like 'Shrimp Lo Mein' or 'Diet Coke Can Soda'."""
        labels = [item.name, *item.aliases]
        for group in item.option_groups:
            if group.replaces or group.name == "Spice level" or any(len(o.name.split()) > 3 for o in group.options):
                continue  # swaps and spice levels aren't how callers name a dish
            for opt in group.options:
                labels.append(f"{opt.name} {item.name}")
                for alias in item.aliases[:3]:
                    labels.append(f"{opt.name} {alias}")
        return labels

    def stt_keyterms(self, max_tokens: int = 300) -> list[str]:
        """Short phrases for speech-to-text keyterm boosting, within the provider's budget.

        Deepgram allows about 500 tokens across all keyterms and recommends 20-50
        terms, so we boost the words a phone line mishears ("General Tso's",
        "Kung Po", "Lo Mein", "Rangoon") rather than every full dish name.
        Common words like "chicken" or "fried rice" are heard fine without help.
        """
        freq = Counter(w for item in self.items for w in set(normalize(item.name).split()))

        def rare(word: str) -> bool:
            return word not in ORDINARY_MENU_WORDS and freq[word] <= 3

        terms: list[str] = []
        lower: set[str] = set()

        def add(phrase: str) -> None:
            ascii_phrase = unicodedata.normalize("NFKD", phrase).encode("ascii", "ignore").decode()
            clean = re.sub(r"[^A-Za-z' ]", "", ascii_phrase).strip()
            if clean and clean.lower() not in lower and not any(clean.lower() in t for t in lower):
                lower.add(clean.lower())
                terms.append(clean)

        names = [re.sub(r"\(.*?\)", "", item.name).strip() for item in self.items]
        all_text = " ".join(normalize(n) for n in names)
        for phrase in CUISINE_KEYTERMS:  # known hard-to-hear terms, if this menu uses them
            if normalize(phrase) in all_text:
                add(phrase)
        for name in names:
            words = name.split()
            norm = normalize(name).split()
            idx = [i for i, w in enumerate(norm) if rare(w)]
            if not idx:
                continue
            lo, hi = idx[0], idx[-1]
            if hi + 1 < len(words) and norm[hi + 1] in HEAD_WORDS:
                hi += 1  # "Bourbon Chicken", "Mongolian Beef"
            add(" ".join(words[lo:hi + 1]))

        budget, kept = 0, []
        for term in terms:
            cost = int(len(term.split()) * 1.6) + 1  # rough tokens; apostrophes and rare words cost extra
            if budget + cost > max_tokens:
                break
            budget += cost
            kept.append(term)
        return kept

    def confirmations(self) -> list[tuple[str, str]]:
        """Everything a person must check before the menu is used on real calls."""
        notes = [(item.id, item.confirm) for item in self.items if item.confirm]
        notes = [(f"category {cat}", note) for cat, note in self.category_confirm.items()] + notes
        if self.restaurant.confirm:
            notes.insert(0, ("restaurant", self.restaurant.confirm))
        return notes

    def base_name(self, item: Item) -> str:
        return item.short_name or item.name

    def variants(self, item: Item) -> list[Item]:
        """Other items that are the same dish served another way (regular / combo / special)."""
        base = normalize(self.base_name(item))
        return [i for i in self.items if i.id != item.id and normalize(self.base_name(i)) == base]

    # ---- hours --------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.restaurant.timezone))

    def is_open(self, when: datetime | None = None) -> bool:
        when = when or self.now()
        when = when.astimezone(ZoneInfo(self.restaurant.timezone))
        day = WEEKDAYS[when.weekday()]
        current = when.time()
        for start, end in self.restaurant.hours.get(day, []):
            if _parse(start) <= current < _parse(end):
                return True
        return False

    def hours_text(self) -> str:
        parts = []
        for day in WEEKDAYS:
            spans = self.restaurant.hours.get(day, [])
            label = ", ".join(f"{_say(s)} to {_say(e)}" for s, e in spans) or "closed"
            parts.append(f"{day.title()}: {label}")
        return "; ".join(parts)


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _say(hhmm: str) -> str:
    t = _parse(hhmm)
    hour = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{hour}:{t.minute:02d} {suffix}" if t.minute else f"{hour} {suffix}"


def load_menu(path: str | Path) -> Menu:
    with open(path, encoding="utf-8") as f:
        return Menu.model_validate(json.load(f))


MENU_DIR = Path(__file__).resolve().parent.parent / "data" / "menus"


def load_restaurant_menu(restaurant_id: str) -> Menu:
    path = MENU_DIR / f"{restaurant_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no menu file at {path}")
    return load_menu(path)
