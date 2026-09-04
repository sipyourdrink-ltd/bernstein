from unittest.mock import MagicMock
import pytest
from target import PaymentService

def test_payment():
    mock = MagicMock(spec=PaymentService)
    mock.non_existent_method_xyz.return_value = 100
    assert mock.non_existent_method_xyz() == 100
