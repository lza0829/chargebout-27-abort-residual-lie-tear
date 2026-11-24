from __future__ import annotations
import os
import tempfile

# 必须在导入 app.config 之前指定独立测试库。
_TMP = tempfile.mkdtemp(prefix="chargebout-test-")
os.environ["CHARGEBOUT_DB"] = os.path.join(_TMP, "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


def _reset_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    seed_if_empty()


@pytest.fixture()
def client():
    _reset_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db_path():
    _reset_db()
    return DB_PATH


@pytest.fixture()
def open_session(client):
    """在 1 号桩（河甲一号，单左枪，通用峰谷）开一笔会话，返回会话数据。"""

    def _open(pile_id: int = 1):
        r = client.post("/api/开会话", json={"桩位编号": pile_id})
        assert r.status_code == 200, r.text
        return r.json()

    return _open
