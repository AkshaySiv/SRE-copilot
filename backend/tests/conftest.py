from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="srecopilot-test-")
os.environ.update({
    "SRECOPILOT_DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
    "SRECOPILOT_DATA_DIR": _tmp,
    "SRECOPILOT_SEED_DEMO": "true",
    "SRECOPILOT_PLUGIN_MODULES": '["tests.scripted_llm"]',
    "SRECOPILOT_AUTH_MODE": "none",
})
