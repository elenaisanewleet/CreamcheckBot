"""Общая подготовка окружения для тестов.

Модули читают переменные окружения на импорте, поэтому выставляем их здесь —
conftest.py pytest импортирует раньше самих тестов.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("ANALYTICS_DB", os.path.join(tempfile.mkdtemp(prefix="cc-test-"), "test.db"))
os.environ.setdefault("LOG_FILE", "")
os.environ.setdefault("DASHBOARD_TOKEN", "test-token")
