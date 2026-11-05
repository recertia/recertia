"""Official bar file loader. No network. No invented rows. No silent truncation."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STAMP = "# official_bars v1"
HEADER = ["date", "symbol", "open", "high", "low", "close", "volume"]
PAPER_EVIDENCE_FLOOR = 30
PAPER_WINDOW_FILLS = 20


class RefuseError(Exception):
    """The file is not an official bar file, or the paper gates are not met."""


@dataclass(frozen=True)
class Bar:
    date: str
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def default_bar_path() -> Path:
    """Operator override, else the committed MIT Plotly AAPL tape."""

    override = os.environ.get("RECERTIA_OFFICIAL_BARS")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "official" / "aapl-plotly.csv"


def load_official_bars(path: Path | str) -> tuple[Bar, ...]:
    file_path = Path(path)
    if not file_path.is_file():
        raise RefuseError(f"missing official bar file: {file_path}")
    lines = file_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != STAMP:
        raise RefuseError("not an official bar file: missing stamp")
    index = 1
    source = ""
    while index < len(lines) and lines[index].startswith("#"):
        if lines[index].startswith("# source:"):
            source = lines[index].split(":", 1)[1].strip()
        index += 1
    if not source:
        raise RefuseError("not an official bar file: missing source")
    body = lines[index:]
    reader = csv.reader(body)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise RefuseError("not an official bar file: missing header") from exc
    if header != HEADER:
        raise RefuseError("not an official bar file: bad header")
    bars: list[Bar] = []
    previous = ""
    symbol = ""
    for row in reader:
        if len(row) != len(HEADER) or any(not cell.strip() for cell in row):
            raise RefuseError("not an official bar file: bad row")
        date, row_symbol, open_, high, low, close, volume = row
        _require_date(date)
        if previous and date <= previous:
            raise RefuseError("not an official bar file: dates must increase")
        previous = date
        if symbol and row_symbol != symbol:
            raise RefuseError("not an official bar file: one symbol")
        symbol = row_symbol
        bar = Bar(
            date,
            row_symbol,
            _price(open_, "open"),
            _price(high, "high"),
            _price(low, "low"),
            _price(close, "close"),
            _volume(volume),
        )
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
            bar.open, bar.close, bar.high
        ):
            raise RefuseError("not an official bar file: high/low inconsistent")
        bars.append(bar)
    if len(bars) < 2:
        raise RefuseError("not an official bar file: need at least two bars")
    return tuple(bars)


def tape_gate(bars: tuple[Bar, ...]) -> str | None:
    """Return a skip reason when the tape cannot support the locked paper gate."""

    fills = len(bars) - 1
    if fills <= 0 or fills % PAPER_WINDOW_FILLS != 0:
        return "horizon_remainder"
    if fills // PAPER_WINDOW_FILLS < PAPER_EVIDENCE_FLOOR:
        return "below_evidence_floor"
    return None


def _require_date(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise RefuseError("not an official bar file: bad date") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise RefuseError("not an official bar file: bad date")


def _price(value: str, name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise RefuseError(f"not an official bar file: bad {name}") from exc
    if number <= 0:
        raise RefuseError(f"not an official bar file: bad {name}")
    return number


def _volume(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise RefuseError("not an official bar file: bad volume") from exc
    if number < 0:
        raise RefuseError("not an official bar file: bad volume")
    return number
