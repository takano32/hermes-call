from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_call.store import Store, validate_call


def payload(**kwargs):
    return {"extension": "1001", "message": "hello", "request_id": "a" * 32, **kwargs}


@pytest.mark.parametrize(
    "change",
    [
        {"extension": "9999"},
        {"extension": "1001@evil"},
        {"extension": 1001},
        {"message": " "},
        {"message": "a" * 2001},
        {"request_id": "../bad"},
        {"delay_seconds": 1},
        {"at": "2027-01-01"},
    ],
)
def test_reject_untrusted_fields(change):
    with pytest.raises(ValueError):
        validate_call(payload(**change), ("1001",))


def test_atomic_idempotency_and_state_recovery(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    call = validate_call(payload(), ("1001",))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.add(call), range(8)))
    assert sum(created for _, created in results) == 1
    assert len(store.list()) == 1
    with pytest.raises(ValueError, match="different"):
        store.add({**call, "message": "changed"})
    assert store.transition(call["id"], "dialing", "active")
    assert not store.transition(call["id"], "dialing", "active")
    store.recover()
    assert store.get(call["id"])["state"] == "unknown"
    _, created = store.add(call)
    assert not created  # Even ambiguous calls cannot be accidentally redialed.
