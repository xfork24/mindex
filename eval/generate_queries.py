"""Generate an unlabelled 60-text / 12-video evaluation worksheet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

QUERIES = {
    "动作顺序": [
        "一个人推开门，停顿后走进房间",
        "一个人把杯子放下，然后转身离开",
        "两个人握手后拥抱",
        "一个人奔跑时回头看身后",
        "有人从坐姿突然站起并指向对方",
        "有人拾起地上的物品，仔细端详",
        "一个人递出东西，对方犹豫后接过",
        "有人关上窗户，然后拉上窗帘",
        "一个人走到桌边坐下并低头",
        "两人擦肩而过，其中一人停下回头",
    ],
    "多条件场景": [
        "雨夜，一个人独自站在街边",
        "白天，两个人坐在餐桌两侧交谈",
        "昏暗的室内，一人站着一人坐着",
        "车里的人透过车窗看向外面",
        "楼道里，一个人背对镜头离开",
        "空旷房间中有人站在窗边",
        "街道上有人穿过人群奔跑",
        "夜晚，人物坐在有台灯的桌旁",
        "户外逆光下，两个人面对面站着",
        "有人在门口犹豫，迟迟没有进去",
    ],
    "含蓄情绪": [
        "一个人强忍眼泪，努力保持平静",
        "人物欲言又止，最后低下头",
        "听到对方的话后，人物露出惊讶神情",
        "有人愤怒地瞪着对方，但没有说话",
        "一个人露出勉强的笑容",
        "人物看起来紧张，反复看向门口",
        "一个人释然地笑了",
        "人物失望地移开视线",
        "两个人相视而笑，气氛轻松",
        "一个人沉默地承受指责",
    ],
    "镜头语言": [
        "近景，一个人眼含泪水",
        "从人物背后拍摄他走进房间",
        "俯拍人物独自坐着",
        "镜头慢慢推近一个沉默的人",
        "低角度拍摄站立的人物",
        "两人的对话以正反打镜头呈现",
        "远景中一个人独自走在街道上",
        "特写人物握紧的手",
        "镜头跟随一个行走的人",
        "隔着玻璃拍摄人物的侧脸",
    ],
    "对白原句": [
        "我不想再见到你",
        "你到底在隐瞒什么",
        "别担心，我会回来的",
        "我们已经没有时间了",
        "这件事跟你没有关系",
        "你为什么现在才告诉我",
        "对不起，是我错了",
        "我相信你",
        "你先听我把话说完",
        "从今以后，我们各走各的路",
    ],
    "对白含义与易混淆": [
        "有人口头答应，但表情明显不情愿",
        "两人表面客气，气氛却很紧张",
        "一个人告别后独自离开",
        "人物解释误会，对方逐渐放松",
        "有人向另一个人道歉，得到原谅",
        "一个人拒绝别人的请求",
        "人物试图安慰正在难过的人",
        "同样是微笑，但人物表现出讽刺",
        "人物因为好消息而哭泣",
        "有人急切地追问事情真相",
    ],
}


def generate(output: Path):
    rows = []
    for category_index, (category, texts) in enumerate(QUERIES.items()):
        for index, text in enumerate(texts):
            dev = index < 3 or (index == 3 and category_index < 2)
            rows.append(
                {
                    "id": f"text-{category_index + 1:02}-{index + 1:02}",
                    "split": "dev" if dev else "test",
                    "category": category,
                    "text": text,
                    "granularity": "moment",
                    "relevant": [],
                }
            )
    for index in range(12):
        rows.append(
            {
                "id": f"video-{index + 1:02}",
                "split": "video-test",
                "video": f"videos/query-{index + 1:02}.mp4",
                "category": "视频相似检索",
                "relevant": [],
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/queries.jsonl"))
    args = parser.parse_args()
    rows = generate(args.output)
    print(f"Wrote {len(rows)} unlabelled queries to {args.output}; annotation is required before evaluation.")
