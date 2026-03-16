from tasks.dog_weight_task import derive_dog_size_from_weight


def test_derive_dog_size_thresholds():
    assert derive_dog_size_from_weight(10) == "small"
    assert derive_dog_size_from_weight(19) == "small"
    assert derive_dog_size_from_weight(20) == "medium"
    assert derive_dog_size_from_weight(60) == "medium"
    assert derive_dog_size_from_weight(61) == "large"
    assert derive_dog_size_from_weight(100) == "large"
    assert derive_dog_size_from_weight(101) == "x-large"
