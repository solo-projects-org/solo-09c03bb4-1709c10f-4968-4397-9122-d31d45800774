"""修订核查：把修订后的完整 CSV 与已发布的安全表对照。

核对对象仅限已发布页面上公开的信息：
- 病区集合、周次集合（范围）；
- 已发布表中公开格的数值；
- 行合计、列合计、总计。

已发布表中被隐藏的格本来就不对外公开。修订只改变隐藏格数值、而全部公开
信息不变时，已发布汇总仍然准确，可以继续供研究组使用。反向地：范围或任一
公开值变化，旧表即不再代表修订后的数据，必须撤下。

本模块返回的原因面向数据员，只描述“哪里变了”，不包含任何格计数，
避免在核查页面泄露隐藏格数值。
"""

from __future__ import annotations

import hashlib
import json


def content_digest(records) -> str:
    """修订文件的内容指纹（去重后的记录，与行序无关）。

    用于“同一修订号必须是同一份文件”的判定：记录集合相同即视为同一份文件，
    仅行序/重复行/编码差异不会误判为不同文件。
    """
    canonical = sorted(
        (r.test_id, r.ward, r.week, r.positive) for r in records
    )
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _reorder(counts: list[list[int]], wards: list[str], weeks: list[str],
             target_wards: list[str], target_weeks: list[str]):
    """把修订交叉表按已发布表的病区/周次顺序重排（集合已确认相同）。"""
    wi = {w: i for i, w in enumerate(wards)}
    ki = {w: j for j, w in enumerate(weeks)}
    return ([[counts[wi[w]][ki[k]] for k in target_weeks] for w in target_wards])


def compare_with_publication(
    publication: dict,
    wards: list[str],
    weeks: list[str],
    counts: list[list[int]],
) -> list[str]:
    """对照修订交叉表与已发布 payload。

    返回变化原因列表；空列表表示已发布信息仍然成立（未变化）。
    原因文本不含任何计数。
    """
    pub_wards = list(publication.get("wards") or [])
    pub_weeks = list(publication.get("weeks") or [])

    def _label_problem(kind: str, published: list[str], revised: list[str]):
        added = sorted(set(revised) - set(published))
        removed = sorted(set(published) - set(revised))
        parts = []
        if added:
            parts.append("新增 " + "、".join(added))
        if removed:
            parts.append("缺少 " + "、".join(removed))
        return f"{kind}范围发生变化（" + "；".join(parts) + "）"

    reasons: list[str] = []
    if set(pub_wards) != set(wards):
        reasons.append(_label_problem("病区", pub_wards, wards))
    if set(pub_weeks) != set(weeks):
        reasons.append(_label_problem("周次", pub_weeks, weeks))
    if reasons:
        # 行列维度已不对应，逐格比较没有意义；范围变化本身即足以撤下
        return reasons

    counts = _reorder(counts, wards, weeks, pub_wards, pub_weeks)

    public_changed: list[str] = []
    for i, row in enumerate(publication.get("cells") or []):
        for j, cell in enumerate(row):
            if cell.get("hidden"):
                continue  # 隐藏格数值不属于公开信息，不参与对照
            if cell.get("count") != counts[i][j]:
                public_changed.append(f"{pub_wards[i]} × {pub_weeks[j]}")
    if public_changed:
        shown = "、".join(public_changed[:10])
        if len(public_changed) > 10:
            shown += f" 等 {len(public_changed)} 个格"
        reasons.append(f"公开格数值发生变化：{shown}")

    n_r, n_c = len(counts), len(counts[0])
    row_totals = [sum(counts[i]) for i in range(n_r)]
    col_totals = [sum(counts[i][j] for i in range(n_r)) for j in range(n_c)]
    if publication.get("row_totals") != row_totals:
        reasons.append("行合计发生变化")
    if publication.get("col_totals") != col_totals:
        reasons.append("列合计发生变化")
    if publication.get("grand_total") != sum(row_totals):
        reasons.append("总合计发生变化")
    return reasons
