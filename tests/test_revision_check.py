"""修订核查：对照已发布表、持久化、同号/旧修订交错、撤下单向性。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

WARDS = ["内科", "外科", "儿科"]
WEEKS = ["2026-W44", "2026-W45", "2026-W46"]
# 可发布：计数 1 的小格会连带隐藏 (0,0)(0,1)(1,0)(1,1)
COUNTS_OK = [[1, 4, 5], [3, 4, 5], [6, 7, 8]]


def make_csv(counts, wards=WARDS, weeks=WEEKS, test_id_prefix="T"):
    """按 病区×周次 生成完整 CSV；同一格内多条阳性记录各自独立检验号。"""
    lines = ["检验号,病区,周次,是否阳性"]
    n = 0
    for i, row in enumerate(counts):
        for j, c in enumerate(row):
            for _ in range(c):
                n += 1
                lines.append(f"{test_id_prefix}{n:04d},{wards[i]},{weeks[j]},是")
    n += 1
    lines.append(f"N{n:04d},{wards[0]},{weeks[0]},否")
    n += 1
    lines.append(f"N{n:04d},{wards[-1]},{weeks[-1]},否")
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "app.db")


@pytest.fixture()
def client(db_path):
    return TestClient(create_app(db_path))


def upload(client, data: bytes):
    return client.post(
        "/upload", files={"file": ("tests.csv", data, "text/csv")},
        follow_redirects=False,
    )


def check(client, revision, data: bytes):
    return client.post(
        "/revision-check",
        data={"revision": str(revision)},
        files={"file": ("rev.csv", data, "text/csv")},
        follow_redirects=False,
    )


def publish_baseline(client):
    assert upload(client, make_csv(COUNTS_OK)).status_code == 303
    assert client.post("/confirm", follow_redirects=False).status_code == 303
    r = client.get("/published")
    assert r.status_code == 200
    return r


# ---------------------------------------------------------------- 未变化

def test_unchanged_revision_keeps_publication(client):
    publish_baseline(client)
    r = check(client, 1, make_csv(COUNTS_OK))
    assert r.status_code == 200
    assert "未变化" in r.text
    assert "继续供研究组使用" in r.text
    # 表格仍在
    assert r.text.count(">—</td>") == 4
    assert "已核查的最高修订号：1" in r.text


def test_only_hidden_cell_changed_keeps_publication(client):
    """修订只改变隐藏格数值、公开信息不变：应继续供数。

    病区按中文排序后为 儿科/内科/外科，隐藏块为
    (儿科,W44)(儿科,W45)(内科,W44)(内科,W45)。把隐藏块的数值
    (6,7 / 1,4) 改为 (7,6 / 0,5)，公开格与行列合计都不变。
    """
    publish_baseline(client)
    # 行序按 WARDS = 内科/外科/儿科 传入；交叉表排序后为 儿科/内科/外科，
    # 隐藏块的数值由 (6,7 / 1,4) 变为 (7,6 / 0,5)。
    revised = [[0, 5, 5], [3, 4, 5], [7, 6, 8]]
    r = check(client, 1, make_csv(revised))
    assert r.status_code == 200
    assert "未变化" in r.text
    assert "继续供研究组使用" in r.text
    assert r.text.count(">—</td>") == 4
    assert "<td>1</td>" not in r.text


# ---------------------------------------------------------------- 变化撤下

def test_public_cell_change_withdraws(client):
    publish_baseline(client)
    revised = [row[:] for row in COUNTS_OK]
    revised[2][2] = 9  # (2,2) 是公开格，合计也随之一并变化
    r = check(client, 1, make_csv(revised))
    assert r.status_code == 200
    assert "已撤下" in r.text
    assert "公开格数值发生变化" in r.text
    # 撤下后网页不再显示表格
    assert "<table" not in r.text
    assert ">—</td>" not in r.text
    r2 = client.get("/published")
    assert "<table" not in r2.text
    assert "本汇总表已撤下" in r2.text


def test_ward_range_change_withdraws(client):
    publish_baseline(client)
    r = check(client, 1, make_csv(
        COUNTS_OK, wards=["内科", "外科", "急诊"], weeks=WEEKS))
    assert r.status_code == 200
    assert "已撤下" in r.text
    assert "病区范围发生变化" in r.text
    assert "急诊" in r.text and "儿科" in r.text
    assert "<table" not in r.text


def test_week_range_change_allowed_to_be_reported(client):
    """周次集合变化允许完成核查，但必须撤下。"""
    publish_baseline(client)
    weeks = ["2026-W44", "2026-W45", "2026-W47"]
    r = check(client, 1, make_csv(COUNTS_OK, weeks=weeks))
    assert r.status_code == 200
    assert "周次范围发生变化" in r.text
    assert "2026-W47" in r.text
    assert "<table" not in r.text


def test_cells_reordered_in_file_not_a_change(client):
    """CSV 行序/列出现顺序不改变内容：不算变化。"""
    publish_baseline(client)
    data = make_csv(COUNTS_OK)
    lines = data.decode("utf-8").splitlines()
    body = lines[1:]
    shuffled = "\n".join([lines[0]] + list(reversed(body)) + [""])
    r = check(client, 1, shuffled.encode("utf-8"))
    assert r.status_code == 200
    assert "未变化" in r.text


# ---------------------------------------------------------------- 无效上传

def test_conflict_upload_rejected_state_unchanged(client):
    publish_baseline(client)
    conflict = make_csv(COUNTS_OK, test_id_prefix="X") + \
        "X0001,外科,2026-W46,是\n".encode("utf-8")
    r = check(client, 1, conflict)
    assert r.status_code == 400
    assert "冲突" in r.text
    # 状态未记录，表格仍在
    assert "已核查的最高修订号" not in r.text
    assert "<table" in r.text


def test_bad_revision_number_rejected(client):
    publish_baseline(client)
    for bad in ("0", "-1", "abc", ""):
        r = client.post(
            "/revision-check",
            data={"revision": bad},
            files={"file": ("rev.csv", make_csv(COUNTS_OK), "text/csv")},
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert "修订号" in r.text
    assert client.get("/published").text.count(">—</td>") == 4


def test_missing_file_rejected(client):
    publish_baseline(client)
    r = client.post("/revision-check", data={"revision": "1"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "CSV" in r.text
    assert "<table" in r.text  # 现行表格仍展示


def test_unparseable_csv_rejected(client):
    publish_baseline(client)
    r = check(client, 1, "检验号,病区\nA,内\n".encode("utf-8"))
    assert r.status_code == 400
    assert "<table" in r.text  # 现行表格仍展示
    r = client.get("/published")
    assert "已核查的最高修订号" not in r.text


# ---------------------------------------------------------------- 同号/旧修订

def test_same_revision_different_file_rejected(client):
    publish_baseline(client)
    assert check(client, 1, make_csv(COUNTS_OK)).status_code == 200
    revised = [row[:] for row in COUNTS_OK]
    revised[2][2] = 9
    r = check(client, 1, make_csv(revised))
    assert r.status_code == 409
    assert "已有不同文件" in r.text
    # 既有结论（未变化）与表格保持
    page = client.get("/published")
    assert "已核查的最高修订号：1" in page.text
    assert "<table" in page.text
    assert page.text.count(">—</td>") == 4


def test_same_revision_same_file_is_idempotent(client):
    publish_baseline(client)
    assert check(client, 1, make_csv(COUNTS_OK)).status_code == 200
    r = check(client, 1, make_csv(COUNTS_OK))
    assert r.status_code == 200
    assert "已核查的最高修订号：1" in r.text
    assert "<table" in r.text


def test_older_revision_after_newer_is_rejected(client):
    """较旧修订晚完成也不能覆盖较新结论。"""
    publish_baseline(client)
    assert check(client, 2, make_csv(COUNTS_OK)).status_code == 200
    r = check(client, 1, make_csv(COUNTS_OK))
    assert r.status_code == 409
    assert "较旧修订不能覆盖较新结论" in r.text
    page = client.get("/published")
    assert "已核查的最高修订号：2" in page.text
    assert "<table" in page.text


def test_older_withdrawing_revision_cannot_withdraw_after_newer_ok(client):
    """较旧的撤回结论晚到，不得覆盖较新的“未变化”结论。"""
    publish_baseline(client)
    assert check(client, 2, make_csv(COUNTS_OK)).status_code == 200
    revised = [row[:] for row in COUNTS_OK]
    revised[2][2] = 9
    r = check(client, 1, make_csv(revised))
    assert r.status_code == 409
    page = client.get("/published")
    assert "<table" in page.text
    assert "本汇总表已撤下" not in page.text


def test_older_ok_revision_cannot_republish_after_withdraw(client):
    """撤下后，较旧的“未变化”修订晚到也不能恢复公开。"""
    publish_baseline(client)
    revised = [row[:] for row in COUNTS_OK]
    revised[2][2] = 9
    assert check(client, 2, make_csv(revised)).status_code == 200
    r = check(client, 1, make_csv(COUNTS_OK))
    assert r.status_code == 409
    page = client.get("/published")
    assert "本汇总表已撤下" in page.text
    assert "<table" not in page.text


def test_newer_unchanged_after_withdraw_stays_withdrawn(client):
    """撤下后即使更新修订公开信息与旧表一致，也不自动重新公开。"""
    publish_baseline(client)
    revised = [row[:] for row in COUNTS_OK]
    revised[2][2] = 9
    assert check(client, 1, make_csv(revised)).status_code == 200
    page = client.get("/published")
    assert "本汇总表已撤下" in page.text

    r = check(client, 2, make_csv(COUNTS_OK))
    assert r.status_code == 200
    assert "不会自动重新公开" in r.text
    assert "<table" not in r.text
    assert "已核查的最高修订号：2" in r.text
    # 撤下时记录的是首次撤下的修订号
    assert "基于修订 1 的核查结论" in r.text


def test_withdrawn_state_no_table_and_no_hidden_counts(client):
    publish_baseline(client)
    revised = [row[:] for row in COUNTS_OK]
    revised[0][0] = 2  # 隐藏格改为 2，合计变化 -> 撤下
    r = check(client, 1, make_csv(revised))
    assert "已撤下" in r.text
    assert "<td>1</td>" not in r.text
    assert "<td>2</td>" not in r.text
    assert ">—</td>" not in r.text
    assert "<table" not in r.text


# ---------------------------------------------------------------- 持久化

def test_revision_state_survives_restart(db_path):
    c1 = TestClient(create_app(db_path))
    publish_baseline(c1)
    revised = [row[:] for row in COUNTS_OK]
    revised[2][2] = 9
    assert check(c1, 3, make_csv(revised)).status_code == 200

    c2 = TestClient(create_app(db_path))
    page = c2.get("/published")
    assert "已核查的最高修订号：3" in page.text
    assert "本汇总表已撤下" in page.text
    assert "<table" not in page.text
    # 重启后同类无效上传仍不改变状态
    assert check(c2, 2, make_csv(COUNTS_OK)).status_code == 409
    page = c2.get("/published")
    assert "已核查的最高修订号：3" in page.text


def test_revision_check_requires_publication(client):
    r = check(client, 1, make_csv(COUNTS_OK))
    assert r.status_code == 303
    assert r.headers["location"] == "/upload"


def test_coverage_relaxed_for_revision_but_conflict_still_enforced(client):
    """修订允许病区/周次集合变化（不再要求 2～3 个），同号冲突仍整份拒绝。"""
    publish_baseline(client)
    # 只有一个病区、一个周次：覆盖校验放宽，允许核查 -> 范围变化撤下
    data = ("检验号,病区,周次,是否阳性\n"
            "R0001,内科,2026-W44,是\n"
            "R0002,内科,2026-W44,否\n").encode("utf-8")
    r = check(client, 1, data)
    assert r.status_code == 200
    assert "病区范围发生变化" in r.text
    assert "周次范围发生变化" in r.text

    # 同号冲突仍然整份拒绝
    conflict = ("检验号,病区,周次,是否阳性\n"
                "R0001,内科,2026-W44,是\n"
                "R0001,内科,2026-W44,否\n").encode("utf-8")
    r = check(client, 2, conflict)
    assert r.status_code == 400
    assert "冲突" in r.text


# ---------------------------------------------------------------- 示例文件

def test_sample_revision_files(client):
    """samples/ 中的修订示例：未变化继续供数，公开值变化撤下。"""
    from pathlib import Path

    samples = Path(__file__).resolve().parent.parent / "samples"
    base = (samples / "ok_3x3.csv").read_bytes()
    assert upload(client, base).status_code == 303
    assert client.post("/confirm", follow_redirects=False).status_code == 303

    r = check(client, 1, (samples / "revision_unchanged.csv").read_bytes())
    assert r.status_code == 200
    assert "未变化" in r.text and "继续供研究组使用" in r.text
    assert r.text.count(">—</td>") == 4

    r = check(client, 2, (samples / "revision_changed.csv").read_bytes())
    assert r.status_code == 200
    assert "已撤下" in r.text
    assert "公开格数值发生变化" in r.text
    assert "<table" not in r.text
