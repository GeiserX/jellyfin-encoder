"""The startup notice on images published under the old name.

The old-name image is built with IMAGE_MOVED_TO set to the new image; the new-name image
carries it empty. Only the first must log the notice.
"""
import logging
import os
import sys

import pytest

APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, APP_DIR)

import monitor  # noqa: E402


def test_old_name_image_logs_the_move(monkeypatch, caplog):
    monkeypatch.setenv('IMAGE_MOVED_TO', 'drumsergio/quality-gate-encoder')
    with caplog.at_level(logging.WARNING):
        monitor.warn_if_image_moved()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'deprecated' in warnings[0].getMessage()
    assert 'drumsergio/quality-gate-encoder' in warnings[0].getMessage()
    assert '2027-03-31' in warnings[0].getMessage()


@pytest.mark.parametrize('value', [None, '', '  '])
def test_new_name_image_logs_nothing(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv('IMAGE_MOVED_TO', raising=False)
    else:
        monkeypatch.setenv('IMAGE_MOVED_TO', value)
    with caplog.at_level(logging.DEBUG):
        monitor.warn_if_image_moved()
    assert caplog.records == []
