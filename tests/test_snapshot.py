import pytest

from tests.factories import make_snapshot


@pytest.mark.parametrize(
    ("grid_v", "grid_hz", "expected"),
    [(230.0, 50.0, True), (0.0, 0.0, False), (150.0, 50.0, False), (230.0, 0.0, False)],
)
def test_grid_present(grid_v, grid_hz, expected):
    assert make_snapshot(grid_v=grid_v, grid_hz=grid_hz).grid_present is expected


@pytest.mark.parametrize(
    ("grid_v", "currently_present", "expected"),
    [
        (175.0, True, True),
        (165.0, True, False),
        (175.0, False, False),
        (185.0, False, True),
    ],
)
def test_grid_present_given_hysteresis(grid_v, currently_present, expected):
    assert make_snapshot(grid_v=grid_v).grid_present_given(currently_present) is expected


def test_grid_present_given_still_needs_a_frequency():
    assert not make_snapshot(grid_v=230.0, grid_hz=0.0).grid_present_given(True)
