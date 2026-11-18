"""修订核查状态的事务级交错测试（直接调用 Database，绕开 HTTP）。

覆盖 HTTP 层难以稳定复现的时序：
- 较旧的修订在较新修订之后才写入 -> 拒绝，最高修订号不变；
- 同号不同文件 -> 拒绝；
- 撤下后，较旧或较新的“未变化”都不能恢复公开；
- 无效上传不写状态。
"""

import pytest

from app.db import Database


def make_pub():
    return {
        "wards": ["内科", "外科"],
        "weeks": ["W1", "W2"],
        "cells": [[{"hidden": False, "count": 3}, {"hidden": False, "count": 4}],
                  [{"hidden": False, "count": 5}, {"hidden": False, "count": 6}]],
        "row_totals": [7, 11],
        "col_totals": [8, 10],
        "grand_total": 18,
        "n_records": 18,
        "n_hidden": 0,
        "source_filename": "base.csv",
    }


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "app.db"))
    assert d.publish(make_pub())
    yield d
    d.close()


def test_older_revision_cannot_overwrite_newer(db):
    status, state = db.record_revision_check(5, "digest5", "unchanged", None)
    assert status == "recorded" and state["max_revision"] == 5

    status, state = db.record_revision_check(4, "digest4", "withdrawn", "原因")
    assert status == "stale"
    assert state["max_revision"] == 5
    saved = db.get_revision_check()
    assert saved["max_revision"] == 5
    assert saved["withdrawn"] is False


def test_same_revision_different_digest_rejected(db):
    assert db.record_revision_check(1, "aaa", "unchanged", None)[0] == "recorded"
    status, state = db.record_revision_check(1, "bbb", "withdrawn", "原因")
    assert status == "same_revision"
    assert state["max_revision_digest"] == "aaa"
    saved = db.get_revision_check()
    assert saved["withdrawn"] is False
    assert saved["last_outcome"] == "unchanged"


def test_withdrawn_is_one_way(db):
    assert db.record_revision_check(2, "d2", "withdrawn", "公开值变化")[0] == "recorded"
    saved = db.get_revision_check()
    assert saved["withdrawn"] is True
    assert saved["withdrawn_revision"] == 2
    withdrawn_at = saved["withdrawn_at"]

    # 更新的修订判定为未变化：仍保持撤下，且保留首次撤下记录
    status, state = db.record_revision_check(3, "d3", "unchanged", None)
    assert status == "recorded"
    assert state["withdrawn"] is True
    assert state["withdrawn_revision"] == 2
    assert state["withdrawn_at"] == withdrawn_at
    assert state["max_revision"] == 3
    assert state["last_outcome"] == "unchanged"
    assert db.get_revision_check()["withdrawn"] is True


def test_replay_same_revision_same_digest_idempotent(db):
    db.record_revision_check(1, "d1", "withdrawn", "原因")
    first = db.get_revision_check()
    status, state = db.record_revision_check(1, "d1", "withdrawn", "原因")
    assert status == "recorded"
    assert db.get_revision_check() == first


def test_rejected_check_leaves_state_untouched(db):
    db.record_revision_check(3, "d3", "withdrawn", "原因")
    before = db.get_revision_check()
    assert db.record_revision_check(2, "d2", "unchanged", None)[0] == "stale"
    assert db.record_revision_check(3, "other", "unchanged", None)[0] == "same_revision"
    assert db.get_revision_check() == before


def test_no_publication_rejected(tmp_path):
    d = Database(str(tmp_path / "empty.db"))
    try:
        status, state = d.record_revision_check(1, "d1", "unchanged", None)
        assert status == "no_publication"
        assert d.get_revision_check() is None
    finally:
        d.close()


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "app.db")
    d1 = Database(path)
    d1.publish(make_pub())
    d1.record_revision_check(4, "d4", "withdrawn", "范围变化")
    expected = d1.get_revision_check()
    d1.close()

    d2 = Database(path)
    try:
        assert d2.get_revision_check() == expected
        assert d2.get_revision_check()["withdrawn"] is True
    finally:
        d2.close()
