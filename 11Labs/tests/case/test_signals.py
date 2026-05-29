"""Tests for the v2 Case-level signal vocabulary.

The signal discriminated union is the contract between every external
surface (simulator UI, cron, telematics, Twilio webhook,
ElevenLabs callbacks, dealer port) and the Phase 4 CaseDriver. These
tests pin down:

- Every signal round-trips through JSON cleanly (Pydantic).
- The discriminator selects the right concrete class on deserialize.
- The targeting classifiers correctly bucket every signal.
- Extra fields are rejected (frozen + strict config).
- The union covers every concrete signal class declared in the module.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from guidepoint.case import (
    BusinessHoursClosed,
    BusinessHoursOpened,
    CallEnded,
    CallOutcome,
    CaseId,
    CaseSignal,
    CustomerOptedIn,
    CustomerOptedOut,
    DealerConfirmed,
    DealerRejected,
    DealerSlotsListed,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InitialReminderDue,
    OfferedSlot,
    SlotId,
    TimerFired,
    VehicleEnteredDealer,
    VehicleExitedDealer,
    is_case_targeted,
    is_customer_targeted,
    is_vehicle_targeted,
    is_world_signal,
)
from guidepoint.case import _signals as signals_module
from guidepoint.master_data import VehicleVin


_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)


def _sample_outcome() -> CallOutcome:
    return CallOutcome(
        result="answered",
        business_outcome="booked",
        booked_slot_id=SlotId("slot_a"),
        elevenlabs_conversation_id="conv_x",
        started_at=_NOW,
        ended_at=_NOW,
        duration_seconds=10.0,
    )


def _sample_slot() -> OfferedSlot:
    return OfferedSlot(
        id=SlotId("slot_a"),
        starts_at=datetime(2026, 5, 26, 13, 30, tzinfo=UTC),
        display="Tuesday, May 26 - 8:30 AM",
    )


# Every concrete signal class with one valid construction. The
# parametrize uses this so every test that walks the union touches
# every variant.
_SAMPLES: tuple[tuple[str, BaseModel], ...] = (
    (
        "call_ended",
        CallEnded(timestamp=_NOW, case_id=CaseId("case_1"), outcome=_sample_outcome()),
    ),
    (
        "dealer_slots_listed",
        DealerSlotsListed(
            timestamp=_NOW, case_id=CaseId("case_1"), slots=(_sample_slot(),)
        ),
    ),
    (
        "dealer_confirmed",
        DealerConfirmed(timestamp=_NOW, case_id=CaseId("case_1"), slot_id=SlotId("slot_a")),
    ),
    (
        "dealer_rejected",
        DealerRejected(
            timestamp=_NOW,
            case_id=CaseId("case_1"),
            slot_id=SlotId("slot_a"),
            reason="no tech available",
        ),
    ),
    (
        "initial_reminder_due",
        InitialReminderDue(timestamp=_NOW, case_id=CaseId("case_1")),
    ),
    (
        "final_reminder_due",
        FinalReminderDue(timestamp=_NOW, case_id=CaseId("case_1")),
    ),
    (
        "timer_fired",
        TimerFired(timestamp=_NOW, case_id=CaseId("case_1"), name="silence_nudge"),
    ),
    (
        "vehicle_entered_dealer",
        VehicleEnteredDealer(timestamp=_NOW, vehicle_vin=VehicleVin("1C4RJFBG5NC123456")),
    ),
    (
        "vehicle_exited_dealer",
        VehicleExitedDealer(timestamp=_NOW, vehicle_vin=VehicleVin("1C4RJFBG5NC123456")),
    ),
    (
        "customer_opted_out",
        CustomerOptedOut(timestamp=_NOW, customer_phone="+13135550000"),
    ),
    (
        "customer_opted_in",
        CustomerOptedIn(timestamp=_NOW, customer_phone="+13135550000"),
    ),
    ("business_hours_opened", BusinessHoursOpened(timestamp=_NOW)),
    ("business_hours_closed", BusinessHoursClosed(timestamp=_NOW)),
    ("end_of_business_day_reached", EndOfBusinessDayReached(timestamp=_NOW)),
)


_ADAPTER: TypeAdapter[BaseModel] = TypeAdapter(CaseSignal)


class TestRoundTrip:
    @pytest.mark.parametrize("signal_type,signal", _SAMPLES, ids=[s[0] for s in _SAMPLES])
    def test_each_signal_round_trips_through_json(
        self, signal_type: str, signal: BaseModel
    ) -> None:
        del signal_type
        encoded = signal.model_dump(mode="json")
        decoded = _ADAPTER.validate_python(encoded)
        assert decoded == signal
        assert type(decoded) is type(signal)


class TestDiscriminator:
    @pytest.mark.parametrize("signal_type,signal", _SAMPLES, ids=[s[0] for s in _SAMPLES])
    def test_discriminator_picks_correct_class(
        self, signal_type: str, signal: BaseModel
    ) -> None:
        encoded = signal.model_dump(mode="json")
        assert encoded["signal_type"] == signal_type
        decoded = _ADAPTER.validate_python(encoded)
        assert type(decoded) is type(signal)

    def test_unknown_signal_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python(
                {"signal_type": "not_a_real_signal", "timestamp": _NOW.isoformat()}
            )

    def test_missing_signal_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python({"timestamp": _NOW.isoformat()})


class TestStrictness:
    def test_extra_field_rejected_on_concrete_signal(self) -> None:
        with pytest.raises(ValidationError):
            CallEnded.model_validate(
                {
                    "signal_type": "call_ended",
                    "timestamp": _NOW.isoformat(),
                    "case_id": "case_1",
                    "outcome": _sample_outcome().model_dump(mode="json"),
                    "rogue": True,
                }
            )

    def test_signals_are_frozen(self) -> None:
        signal = InitialReminderDue(timestamp=_NOW, case_id=CaseId("case_1"))
        with pytest.raises(ValidationError):
            signal.case_id = CaseId("case_2")  # type: ignore[misc]


class TestTargeting:
    @pytest.mark.parametrize(
        "signal",
        [
            CallEnded(timestamp=_NOW, case_id=CaseId("c"), outcome=_sample_outcome()),
            DealerSlotsListed(timestamp=_NOW, case_id=CaseId("c"), slots=()),
            DealerConfirmed(timestamp=_NOW, case_id=CaseId("c"), slot_id=SlotId("s")),
            DealerRejected(timestamp=_NOW, case_id=CaseId("c"), slot_id=SlotId("s")),
            InitialReminderDue(timestamp=_NOW, case_id=CaseId("c")),
            FinalReminderDue(timestamp=_NOW, case_id=CaseId("c")),
            TimerFired(timestamp=_NOW, case_id=CaseId("c"), name="nudge"),
        ],
    )
    def test_case_targeted_signals(self, signal: BaseModel) -> None:
        assert is_case_targeted(signal)
        assert not is_vehicle_targeted(signal)
        assert not is_customer_targeted(signal)
        assert not is_world_signal(signal)

    @pytest.mark.parametrize(
        "signal",
        [
            VehicleEnteredDealer(
                timestamp=_NOW, vehicle_vin=VehicleVin("1C4RJFBG5NC123456")
            ),
            VehicleExitedDealer(
                timestamp=_NOW, vehicle_vin=VehicleVin("1C4RJFBG5NC123456")
            ),
        ],
    )
    def test_vehicle_targeted_signals(self, signal: BaseModel) -> None:
        assert is_vehicle_targeted(signal)
        assert not is_case_targeted(signal)
        assert not is_customer_targeted(signal)
        assert not is_world_signal(signal)

    @pytest.mark.parametrize(
        "signal",
        [
            CustomerOptedOut(timestamp=_NOW, customer_phone="+13135550000"),
            CustomerOptedIn(timestamp=_NOW, customer_phone="+13135550000"),
        ],
    )
    def test_customer_targeted_signals(self, signal: BaseModel) -> None:
        assert is_customer_targeted(signal)
        assert not is_case_targeted(signal)
        assert not is_vehicle_targeted(signal)
        assert not is_world_signal(signal)

    @pytest.mark.parametrize(
        "signal",
        [
            BusinessHoursOpened(timestamp=_NOW),
            BusinessHoursClosed(timestamp=_NOW),
            EndOfBusinessDayReached(timestamp=_NOW),
        ],
    )
    def test_world_signals(self, signal: BaseModel) -> None:
        assert is_world_signal(signal)
        assert not is_case_targeted(signal)
        assert not is_vehicle_targeted(signal)
        assert not is_customer_targeted(signal)


class TestUnionCoverage:
    """Adding a signal class without adding it to the union is a bug."""

    def test_every_concrete_signal_is_in_the_union(self) -> None:
        # Walk the module for every Pydantic model that exposes a
        # signal_type literal — those are the concrete signals.
        concrete: set[type[BaseModel]] = set()
        for _name, obj in inspect.getmembers(signals_module, inspect.isclass):
            if not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            fields = getattr(obj, "model_fields", {})
            if "signal_type" in fields:
                concrete.add(obj)

        # CaseSignal is Annotated[Union[...], Field(...)]; first arg of
        # the Annotated metadata is the union.
        annotated_args = get_args(CaseSignal)
        union_types = set(get_args(annotated_args[0]))

        assert concrete == union_types, (
            f"Concrete signals not in the union: {concrete - union_types}; "
            f"union members not declared in the module: {union_types - concrete}"
        )

    def test_every_signal_has_a_unique_signal_type(self) -> None:
        seen: set[str] = set()
        for _name, obj in inspect.getmembers(signals_module, inspect.isclass):
            if not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            fields = getattr(obj, "model_fields", {})
            field = fields.get("signal_type")
            if field is None:
                continue
            # The default value of the Literal-typed field is the unique tag.
            tag = field.default
            assert tag not in seen, f"Duplicate signal_type tag: {tag!r}"
            seen.add(tag)
