"""Generate an offline relevance review sheet; confirmation remains an explicit human action."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from mindex.shared.config import Config
from mindex.shared.jsonio import save
from mindex.shared.review_style import review_style
from mindex.shared.storage import Store


def render_review(config: Config, run: Path, queries: Path) -> Path:
    store = Store.open(config)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    entries = [json.loads(line) for line in queries.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(not entry.get("text", "").strip() for entry in entries):
        raise ValueError("该复核页只接受已填写文字的查询")
    segments = store.segments(state["source_id"], "moment")
    cards = [
        {
            "id": row["id"],
            "start": row["start"],
            "end": row["end"],
            "video": quote(os.path.relpath(store.resolve_media(row["path"]), run)),
            "thumbnail": quote(os.path.relpath(store.resolve_media(row["thumbnail"]), run)),
        }
        for row in segments
    ]
    data = json.dumps({"entries": entries, "segments": cards}, ensure_ascii=False).replace("<", "\\u003c")
    target = run / "label-review.html"
    template = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>中文查询相关性复核</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>__STYLE__</style>
<h1>中文查询相关性复核</h1><p>逐条观看并补全相关片段，修改查询或相关度，再勾选“已复核”。候选共 __COUNT__ 个精细片段。
此页不使用模型描述作为真值，也不计算检索质量。对白查询需结合原声核对。</p>
<label>复核人 <input id="reviewer" type="text" placeholder="请填写实际复核人"></label>
<p>相关度：1 部分相关、2 主要相关、3 完全相关。初选来自草稿，仍需人工检查全部相关目标。</p>
<div id="queries"></div><button id="export">下载已确认标注</button><p id="notice" role="status"></p>
<dialog id="player"><button id="close-player">关闭播放</button><p id="player-title"></p><video id="player-video" controls></video></dialog>
<script>
const data=__DATA__;
const box=document.getElementById('queries');
const player=document.getElementById('player'),playerVideo=document.getElementById('player-video');
document.getElementById('close-player').onclick=()=>player.close();
player.onclose=()=>{playerVideo.pause();playerVideo.removeAttribute('src');playerVideo.load();};
function el(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
for(const entry of data.entries){
 const section=el('section');section.dataset.id=entry.id;
 section.append(el('h2',entry.id+' · '+entry.split));
 const input=el('input');input.type='text';input.value=entry.text;input.className='query';section.append(input);
 const confirm=el('input');confirm.type='checkbox';confirm.className='confirmed';
 const label=el('label','已复核此查询及相关目标 ');label.prepend(confirm);section.append(label);
 const details=el('details'),summary=el('summary','展开候选，观看并选择相关片段');details.append(summary);
 const grid=el('div');grid.className='grid';
 for(const segment of data.segments){
  const article=el('article');article.dataset.segment=segment.id;
  const image=el('img');image.src=segment.thumbnail;image.alt='候选 '+segment.id;image.loading='lazy';image.style.width='100%';
  const play=el('button','播放候选');play.type='button';play.onclick=()=>{playerVideo.src=segment.video;playerVideo.poster=segment.thumbnail;document.getElementById('player-title').textContent=segment.start.toFixed(3)+'–'+segment.end.toFixed(3)+' 秒';player.showModal();};
  article.append(image,play,el('small',segment.start.toFixed(3)+'–'+segment.end.toFixed(3)+' 秒 · '+segment.id));
  const checked=el('input');checked.type='checkbox';checked.className='relevant';
  const prior=(entry.relevant||[]).find(t=>t.segment_id===segment.id);
  checked.checked=Boolean(prior);const relevance=el('label','相关 ');relevance.prepend(checked);
  const grade=el('select');grade.className='grade';for(const value of [1,2,3]){const option=el('option',String(value));option.value=value;grade.append(option);}grade.value=prior?.grade||3;
  relevance.append(grade);article.append(relevance);grid.append(article);
 }
 details.append(grid);section.append(details);box.append(section);
}
document.getElementById('export').onclick=()=>{
 const reviewer=document.getElementById('reviewer').value.trim();
 try{
  if(!reviewer)throw Error('请填写实际复核人');
  const entries=data.entries.map(entry=>{
   const section=[...box.children].find(n=>n.dataset.id===entry.id);
   if(!section.querySelector('.confirmed').checked)throw Error(entry.id+' 尚未复核');
   const text=section.querySelector('.query').value.trim();if(!text)throw Error(entry.id+' 查询为空');
   const relevant=[...section.querySelectorAll('article')].filter(n=>n.querySelector('.relevant').checked).map(n=>({segment_id:n.dataset.segment,grade:Number(n.querySelector('.grade').value)}));
   if(!relevant.length)throw Error(entry.id+' 尚未标注相关目标；无答案请另建诊断集');
   return {...entry,text,relevant,label_status:'confirmed',confirmed_by:reviewer};
  });
  const blob=new Blob([entries.map(v=>JSON.stringify(v)).join('\\n')+'\\n'],{type:'application/x-ndjson'});
  const url=URL.createObjectURL(blob),link=el('a');link.href=url;link.download='queries.reviewed.jsonl';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  document.getElementById('notice').textContent='已导出，请用 e2e freeze 冻结文件后再评分。';
 }catch(e){document.getElementById('notice').textContent=e.message;}
};
</script></html>"""
    target.write_text(
        template.replace("__STYLE__", review_style())
        .replace("__DATA__", data)
        .replace("__COUNT__", str(len(cards))), encoding="utf-8"
    )
    save(
        run / "label-review-info.json",
        {
            "queries": str(queries.resolve()),
            "page": str(target.resolve()),
            "queries_count": len(entries),
            "candidate_count": len(cards),
            "confirmation": "pending-human-review",
        },
    )
    return target
