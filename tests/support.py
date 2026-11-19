"""Keep synthetic tests independent of local credentials, settings and CA paths."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch


def load_fixture():
    """Return a fresh checked-in fixture and no subscriber environment values."""
    path = Path(__file__).parent / 'fixtures' / 'settings.json'
    return json.loads(path.read_text()), {}


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(('MORPHEUS_', 'QA_'))}
        env_patch = patch.dict(os.environ, clean, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        runner_patch = patch('qa.runner.load', side_effect=load_fixture)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
