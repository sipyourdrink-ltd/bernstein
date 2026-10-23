from unittest.mock import MagicMock


def test_payment():
    mock = MagicMock()
    mock.non_existent_method_xyz.return_value = 100
    assert mock.non_existent_method_xyz() == 100
