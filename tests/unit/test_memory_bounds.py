"""In-process run cache and telemetry buffers drop the oldest past the cap."""

from __future__ import annotations

from recertia.api.bounded import BoundedDict
from recertia.telemetry import Telemetry


def test_runs_cache_drops_the_oldest() -> None:
    cache: BoundedDict[str, int] = BoundedDict(max_size=2)
    cache["a"] = 1
    cache["b"] = 2
    cache["b"] = 3
    cache["c"] = 4
    assert list(cache) == ["b", "c"]
    assert len(cache) == 2
    cache["d"] = 5
    assert list(cache) == ["c", "d"]
    assert len(cache) == 2


def test_telemetry_deques_do_not_grow_past_the_cap() -> None:
    tel = Telemetry(max_events=2, max_spans=2)
    tel.emit("policy.changed", tenant_id="t", n=1)
    tel.emit("policy.changed", tenant_id="t", n=2)
    tel.emit("policy.changed", tenant_id="t", n=3)
    assert len(tel.events) == 2
    assert [event.attributes["n"] for event in tel.events] == [2, 3]
    for index in range(3):
        with tel.span("hop", tenant_id="t", n=index):
            pass
    assert len(tel.spans) == 2
    assert [span.attributes["n"] for span in tel.spans] == [1, 2]
