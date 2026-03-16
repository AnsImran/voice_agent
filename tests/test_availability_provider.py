from tools.availability_provider import (
    MockAvailabilityProvider,
    compute_selection_quote,
    normalize_service,
    normalize_service_plan,
    resolve_service_selection,
)


def test_normalize_service_aliases():
    assert normalize_service("day care") == "daycare"
    assert normalize_service("sleepover") == "boarding"
    assert normalize_service("basic bath") == "grooming"
    assert normalize_service("a-la-bark") == "training"


def test_mock_provider_is_deterministic_for_same_inputs():
    provider = MockAvailabilityProvider()
    slots_one = provider.get_slots(
        service="daycare",
        date="2026-03-20",
        time_preference="morning",
        dog_size="medium",
    )
    slots_two = provider.get_slots(
        service="daycare",
        date="2026-03-20",
        time_preference="morning",
        dog_size="medium",
    )

    assert [slot.__dict__ for slot in slots_one] == [slot.__dict__ for slot in slots_two]


def test_mock_provider_supports_all_major_services():
    provider = MockAvailabilityProvider()
    services = ["daycare", "boarding", "grooming", "training"]
    for service in services:
        slots = provider.get_slots(
            service=service,
            date="2026-03-20",
            time_preference="anytime",
            dog_size="small",
        )
        assert slots, f"Expected slots for service={service}"


def test_normalize_service_plan_aliases():
    assert normalize_service_plan("golden leash club card") == "golden_leash_club"
    assert normalize_service_plan("Golden Leaf Club card") == "golden_leash_club"
    assert normalize_service_plan("drop-in daycare") is None


def test_resolve_service_selection_preserves_existing_plan():
    family, plan = resolve_service_selection(
        value="daycare",
        existing_family="daycare",
        existing_plan="golden_leash_club",
    )
    assert family == "daycare"
    assert plan == "golden_leash_club"


def test_compute_selection_quote_for_golden_leash():
    quote = compute_selection_quote(
        service_family="daycare",
        service_plan="golden_leash_club",
        dog_size="medium",
    )
    assert quote["subtotal"] == 775.0
    assert quote["total"] == 775.0
    assert quote["billing_cycle"] == "monthly"
