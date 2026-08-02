"""Public synthetic Chinese calibration corpus for the pinned P5B model."""

from __future__ import annotations

import math
from dataclasses import dataclass

from amadeus_desktop.embedding_backend import EmbeddingBackend, validate_normalized_vector
from amadeus_desktop.hybrid_retrieval import CalibrationResult, calibrate_threshold


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    """One rewrite plus a related-but-incorrect document."""

    query: str
    positive_document: str
    negative_document: str


# These facts are deliberately synthetic, generic, and safe to publish. Each
# negative stays in the same broad topic while describing a different fact.
CALIBRATION_CASES: tuple[CalibrationCase, ...] = (
    CalibrationCase(
        "我平时最喜欢喝无糖拿铁",
        "用户最常喝的是不加糖的拿铁咖啡。",
        "用户习惯在睡前喝一杯温热牛奶。",
    ),
    CalibrationCase(
        "我每天早晨都会跑步",
        "用户有每天清晨慢跑的习惯。",
        "用户通常在周末傍晚去游泳。",
    ),
    CalibrationCase(
        "我现在住在上海",
        "用户目前的常住城市是上海。",
        "用户计划下个月去杭州短途旅行。",
    ),
    CalibrationCase(
        "我的生日在五月十八日",
        "用户的生日日期是五月十八号。",
        "用户把年度体检预约在六月十二号。",
    ),
    CalibrationCase(
        "我家的猫叫团子",
        "用户养的猫名字是团子。",
        "用户邻居家的小狗名叫豆豆。",
    ),
    CalibrationCase(
        "我吃饭时不喜欢香菜",
        "用户的饮食偏好是不加香菜。",
        "用户对芒果过敏，需要避开含芒果的甜点。",
    ),
    CalibrationCase(
        "我的电脑一直使用深色模式",
        "用户在电脑上偏好开启深色主题。",
        "用户把手机字体调成了较大字号。",
    ),
    CalibrationCase(
        "我上班通常乘坐地铁",
        "用户日常通勤的主要方式是地铁。",
        "用户周末喜欢骑自行车去公园。",
    ),
    CalibrationCase(
        "我最常听的音乐是爵士乐",
        "用户最经常收听爵士音乐。",
        "用户做家务时通常播放科普播客。",
    ),
    CalibrationCase(
        "我最喜欢阅读科幻小说",
        "用户偏爱的书籍类型是科学幻想小说。",
        "用户空闲时常看历史纪录片。",
    ),
    CalibrationCase(
        "我习惯上午处理重要工作",
        "用户倾向在早上完成最重要的工作。",
        "用户一般在晚饭后安排语言学习。",
    ),
    CalibrationCase(
        "我最喜欢的颜色是蓝色",
        "用户最偏爱的颜色为蓝色。",
        "用户卧室墙面使用的是米白色。",
    ),
    CalibrationCase(
        "我每周一都会吃素",
        "用户在每个星期一选择素食。",
        "用户工作日做饭时习惯少放盐。",
    ),
    CalibrationCase(
        "会议开始前十分钟提醒我",
        "用户希望会议提前十分钟收到提醒。",
        "用户需要在每个月月底检查账单。",
    ),
    CalibrationCase(
        "我最近正在学习日语",
        "用户当前学习的外语是日语。",
        "用户去年参加过一门法语入门课。",
    ),
    CalibrationCase(
        "我做饭习惯使用橄榄油",
        "用户烹饪时通常选用橄榄油。",
        "用户会定期用蜂蜡保养家里的木质家具。",
    ),
    CalibrationCase(
        "我一般晚上十一点睡觉",
        "用户通常在夜间十一点左右入睡。",
        "用户每天清晨六点开始整理房间。",
    ),
    CalibrationCase(
        "我的家乡是成都",
        "用户来自成都，家乡在成都。",
        "用户很想再次去重庆品尝当地小吃。",
    ),
    CalibrationCase(
        "我有一点恐高",
        "用户对高处感到害怕。",
        "用户乘船时间过长时容易晕船。",
    ),
    CalibrationCase(
        "我每周六给绿植浇水",
        "用户固定在星期六为家中植物浇水。",
        "用户每天早晨会给鱼缸里的鱼喂食。",
    ),
    CalibrationCase(
        "我穿四十二码的鞋",
        "用户的鞋码是四十二码。",
        "用户购买上衣时通常选择中号。",
    ),
    CalibrationCase(
        "坐飞机时我更喜欢靠窗座位",
        "用户乘飞机偏好选择窗边的位置。",
        "用户在电影院通常选择中间排的座位。",
    ),
    CalibrationCase(
        "我每周都会给妈妈打电话",
        "用户保持每星期联系母亲的习惯。",
        "用户每个月会和以前的同事聚餐一次。",
    ),
    CalibrationCase(
        "我的合成项目代号叫北斗",
        "用户为这个虚构项目设定的代号是北斗。",
        "用户把另一份演示文档归档为知一。",
    ),
)


@dataclass(frozen=True, slots=True)
class EmbeddingCalibrationReport:
    """Aggregate-only output; source text is intentionally not retained."""

    calibration: CalibrationResult
    positive_scores: tuple[float, ...]
    negative_scores: tuple[float, ...]


def calibrate_backend(
    backend: EmbeddingBackend,
    cases: tuple[CalibrationCase, ...] = CALIBRATION_CASES,
) -> EmbeddingCalibrationReport:
    """Embed 24 positive and 24 hard-negative pairs and enforce the 0.05 gap."""

    if len(cases) != 24:
        raise ValueError("the fixed calibration corpus must contain exactly 24 cases")
    queries = tuple(backend.embed_query(case.query) for case in cases)
    documents = tuple(
        document for case in cases for document in (case.positive_document, case.negative_document)
    )
    document_vectors = backend.embed_documents(documents)
    if len(document_vectors) != 48:
        raise ValueError("the calibration backend returned an invalid document count")

    positives: list[float] = []
    negatives: list[float] = []
    for index, query_vector in enumerate(queries):
        validate_normalized_vector(query_vector, dimension=backend.dimension)
        positive_vector = document_vectors[index * 2]
        negative_vector = document_vectors[index * 2 + 1]
        validate_normalized_vector(positive_vector, dimension=backend.dimension)
        validate_normalized_vector(negative_vector, dimension=backend.dimension)
        positives.append(_cosine(query_vector, positive_vector))
        negatives.append(_cosine(query_vector, negative_vector))

    calibration = calibrate_threshold(positives, negatives)
    return EmbeddingCalibrationReport(
        calibration=calibration,
        positive_scores=tuple(positives),
        negative_scores=tuple(negatives),
    )


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("calibration vector dimensions do not match")
    score = math.fsum(a * b for a, b in zip(left, right, strict=True))
    if not math.isfinite(score):
        raise ValueError("calibration produced a non-finite similarity")
    return max(-1.0, min(1.0, score))


assert len(CALIBRATION_CASES) == 24
